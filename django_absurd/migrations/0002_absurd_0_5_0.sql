-- Absurd migration SQL bundle
-- Range: 0.4.0 -> 0.5.0

-- begin migration: 0.4.0-0.5.0.sql [bundled]
-- Migration from 0.4.0 to main
--
-- Adds bounded retry-strategy validation and removes Absurd's uuid-ossp
-- dependency without deleting workflow data.

create or replace function absurd.get_schema_version ()
  returns text
  language sql
as $$
  select '0.5.0'::text;
$$;

create or replace function absurd.retry_delay_seconds (
  p_strategy jsonb,
  p_attempt integer
)
  returns double precision
  language plpgsql
  immutable
as $$
declare
  v_limit constant double precision := 86400;
  v_kind text;
  v_base double precision;
  v_factor double precision;
  v_max_seconds double precision;
  v_exponent integer := greatest(coalesce(p_attempt, 1) - 1, 0);
begin
  if p_strategy is null then
    return 0;
  end if;
  if jsonb_typeof(p_strategy) <> 'object' then
    raise exception sqlstate 'AB003'
      using message = 'retry_strategy must be a JSON object';
  end if;

  v_kind := coalesce(p_strategy->>'kind', 'none');
  if v_kind not in ('none', 'fixed', 'exponential') then
    raise exception sqlstate 'AB003'
      using message = format('Unsupported retry strategy kind "%s"', v_kind);
  end if;
  if v_kind = 'none' then
    return 0;
  end if;

  v_base := coalesce(
    (p_strategy->>'base_seconds')::double precision,
    case when v_kind = 'fixed' then 60 else 30 end
  );
  if v_base not between 0 and v_limit then
    raise exception sqlstate 'AB003'
      using message = 'retry_strategy.base_seconds must be between 0 and 86400';
  end if;
  if v_kind = 'fixed' then
    return v_base;
  end if;

  v_factor := coalesce((p_strategy->>'factor')::double precision, 2);
  v_max_seconds := coalesce(
    (p_strategy->>'max_seconds')::double precision,
    v_limit
  );
  if v_factor < 0 or v_factor::text in ('NaN', 'Infinity', '-Infinity') then
    raise exception sqlstate 'AB003'
      using message = 'retry_strategy.factor must be a finite non-negative number';
  end if;
  if v_max_seconds not between 0 and v_limit then
    raise exception sqlstate 'AB003'
      using message = 'retry_strategy.max_seconds must be between 0 and 86400';
  end if;
  if v_base = 0 then
    return 0;
  end if;

  begin
    return least(v_base * power(v_factor, v_exponent), v_max_seconds);
  exception
    when numeric_value_out_of_range then
      return case when v_factor < 1 then 0 else v_max_seconds end;
  end;
exception
  when invalid_text_representation or numeric_value_out_of_range then
    raise exception sqlstate 'AB003'
      using message = 'retry_strategy contains an invalid number';
end;
$$;

create or replace function absurd.portable_uuidv7 ()
  returns uuid
  language plpgsql
  volatile
as $$
declare
  ts_ms bigint;
  b bytea;
  rnd bytea;
  i int;
begin
  if to_regprocedure('pg_catalog.uuidv7()') is not null then
    return pg_catalog.uuidv7 ();
  end if;
  ts_ms := floor(extract(epoch from absurd.current_time()) * 1000)::bigint;
  rnd := uuid_send(pg_catalog.gen_random_uuid ());
  b := repeat(E'\\000', 16)::bytea;
  for i in 0..5 loop
    b := set_byte(b, i, ((ts_ms >> ((5 - i) * 8)) & 255)::int);
  end loop;
  for i in 6..15 loop
    b := set_byte(b, i, get_byte(rnd, i));
  end loop;
  b := set_byte(b, 6, ((get_byte(b, 6) & 15) | (7 << 4)));
  b := set_byte(b, 8, ((get_byte(b, 8) & 63) | 128));
  return encode(b, 'hex')::uuid;
end;
$$;

-- Absurd no longer requires uuid-ossp. Drop the extension when it is only
-- present from earlier Absurd installs; leave it alone if other database
-- objects depend on it. Never use CASCADE here.
do $$
begin
  drop extension if exists "uuid-ossp";
exception
  when dependent_objects_still_exist then
    raise notice 'uuid-ossp was not dropped because other database objects depend on it';
end;
$$;

create or replace function absurd.spawn_task (
  p_queue_name text,
  p_task_name text,
  p_params jsonb,
  p_options jsonb default '{}'::jsonb
)
  returns table (
    task_id uuid,
    run_id uuid,
    attempt integer,
    created boolean
  )
  language plpgsql
as $$
declare
  v_task_id uuid := absurd.portable_uuidv7();
  v_run_id uuid := absurd.portable_uuidv7();
  v_attempt integer := 1;
  v_headers jsonb;
  v_retry_strategy jsonb;
  v_max_attempts integer;
  v_cancellation jsonb;
  v_idempotency_key text;
  v_existing_task_id uuid;
  v_existing_run_id uuid;
  v_existing_attempt integer;
  v_row_count integer;
  v_storage_mode text := 'unpartitioned';
  v_task_inserted boolean := false;
  v_now timestamptz := absurd.current_time();
  v_params jsonb := coalesce(p_params, 'null'::jsonb);
begin
  if p_task_name is null or length(trim(p_task_name)) = 0 then
    raise exception 'task_name must be provided';
  end if;

  if p_options is not null then
    v_headers := p_options->'headers';
    v_retry_strategy := p_options->'retry_strategy';
    perform absurd.retry_delay_seconds(v_retry_strategy, 1);
    if p_options ? 'max_attempts' then
      v_max_attempts := (p_options->>'max_attempts')::int;
      if v_max_attempts is not null and v_max_attempts < 1 then
        raise exception 'max_attempts must be >= 1';
      end if;
    end if;
    v_cancellation := p_options->'cancellation';
    v_idempotency_key := p_options->>'idempotency_key';
  end if;

  if v_idempotency_key is not null then
    select storage_mode into v_storage_mode
    from absurd.queues
    where queue_name = p_queue_name;

    v_storage_mode := coalesce(v_storage_mode, 'unpartitioned');
    if v_storage_mode not in ('unpartitioned', 'partitioned') then
      raise exception 'Unsupported queue storage mode "%"', v_storage_mode;
    end if;

    if v_storage_mode = 'partitioned' then
      -- Reserve idempotency key via dedicated side table.
      execute format(
        'insert into absurd.%I (idempotency_key, task_id)
         values ($1, $2)
         on conflict (idempotency_key) do nothing',
        'i_' || p_queue_name
      )
      using v_idempotency_key, v_task_id;

      get diagnostics v_row_count = row_count;

      if v_row_count = 0 then
        execute format(
          'select i.task_id, t.last_attempt_run, t.attempts
             from absurd.%I i
             join absurd.%I t on t.task_id = i.task_id
            where i.idempotency_key = $1
              for key share of i',
          'i_' || p_queue_name,
          't_' || p_queue_name
        )
        into v_existing_task_id, v_existing_run_id, v_existing_attempt
        using v_idempotency_key;

        if v_existing_task_id is null then
          raise exception 'Idempotency key "%" in queue "%" was concurrently cleaned up', v_idempotency_key, p_queue_name
            using errcode = '40001',
                  hint = 'Retry spawn_task with the same idempotency key.';
        end if;

        if v_existing_run_id is null then
          raise exception 'Idempotency key "%" in queue "%" resolved to task "%" without a run', v_idempotency_key, p_queue_name, v_existing_task_id;
        end if;

        return query select v_existing_task_id, v_existing_run_id, v_existing_attempt, false;
        return;
      end if;
    else
      -- Unpartitioned queues keep the original unique(idempotency_key)
      -- behavior directly on t_<queue>.
      execute format(
        'insert into absurd.%I (task_id, task_name, params, headers, retry_strategy, max_attempts, cancellation, enqueue_at, first_started_at, state, attempts, last_attempt_run, completed_payload, cancelled_at, idempotency_key)
         values ($1, $2, $3, $4, $5, $6, $7, $8, null, ''pending'', $9, $10, null, null, $11)
         on conflict (idempotency_key) do nothing',
        't_' || p_queue_name
      )
      using v_task_id, p_task_name, v_params, v_headers, v_retry_strategy, v_max_attempts, v_cancellation, v_now, v_attempt, v_run_id, v_idempotency_key;

      get diagnostics v_row_count = row_count;

      if v_row_count = 0 then
        execute format(
          'select task_id, last_attempt_run, attempts
             from absurd.%I
            where idempotency_key = $1',
          't_' || p_queue_name
        )
        into v_existing_task_id, v_existing_run_id, v_existing_attempt
        using v_idempotency_key;

        return query select v_existing_task_id, v_existing_run_id, v_existing_attempt, false;
        return;
      end if;

      v_task_inserted := true;
    end if;
  end if;

  if not v_task_inserted then
    execute format(
      'insert into absurd.%I (task_id, task_name, params, headers, retry_strategy, max_attempts, cancellation, enqueue_at, first_started_at, state, attempts, last_attempt_run, completed_payload, cancelled_at, idempotency_key)
       values ($1, $2, $3, $4, $5, $6, $7, $8, null, ''pending'', $9, $10, null, null, $11)',
      't_' || p_queue_name
    )
    using v_task_id, p_task_name, v_params, v_headers, v_retry_strategy, v_max_attempts, v_cancellation, v_now, v_attempt, v_run_id, v_idempotency_key;
  end if;

  execute format(
    'insert into absurd.%I (run_id, task_id, attempt, state, available_at, wake_event, event_payload, result, failure_reason)
     values ($1, $2, $3, ''pending'', $4, null, null, null, null)',
    'r_' || p_queue_name
  )
  using v_run_id, v_task_id, v_attempt, v_now;

  return query select v_task_id, v_run_id, v_attempt, true;
end;
$$;

create or replace function absurd.fail_run (
  p_queue_name text,
  p_run_id uuid,
  p_reason jsonb,
  p_retry_at timestamptz default null
)
  returns void
  language plpgsql
as $$
declare
  v_task_id uuid;
  v_attempt integer;
  v_run_state text;
  v_retry_strategy jsonb;
  v_max_attempts integer;
  v_now timestamptz := absurd.current_time();
  v_next_attempt integer;
  v_delay_seconds double precision := 0;
  v_next_available timestamptz;
  v_first_started timestamptz;
  v_cancellation jsonb;
  v_max_duration bigint;
  v_task_cancel boolean := false;
  v_new_run_id uuid;
  v_task_state_after text;
  v_recorded_attempt integer;
  v_last_attempt_run uuid := p_run_id;
  v_cancelled_at timestamptz := null;
begin
  execute format(
    'select r.task_id, r.attempt, r.state
       from absurd.%I r
      where r.run_id = $1
      for update',
    'r_' || p_queue_name
  )
  into v_task_id, v_attempt, v_run_state
  using p_run_id;

  if v_task_id is null then
    raise exception 'Run "%" cannot be failed in queue "%"', p_run_id, p_queue_name;
  end if;

  if v_run_state = 'cancelled' then
    raise exception sqlstate 'AB001' using message = 'Task has been cancelled';
  end if;

  if v_run_state = 'failed' then
    raise exception sqlstate 'AB002' using message = format('Run "%s" has already failed in queue "%s"', p_run_id, p_queue_name);
  end if;

  if v_run_state not in ('running', 'sleeping') then
    raise exception 'Run "%" cannot be failed in queue "%"', p_run_id, p_queue_name;
  end if;

  execute format(
    'select retry_strategy, max_attempts, first_started_at, cancellation
       from absurd.%I
      where task_id = $1
      for update',
    't_' || p_queue_name
  )
  into v_retry_strategy, v_max_attempts, v_first_started, v_cancellation
  using v_task_id;

  execute format(
    'update absurd.%I
        set state = ''failed'',
            wake_event = null,
            failed_at = $2,
            failure_reason = $3
      where run_id = $1',
    'r_' || p_queue_name
  ) using p_run_id, v_now, p_reason;

  v_next_attempt := v_attempt + 1;
  v_task_state_after := 'failed';
  v_recorded_attempt := v_attempt;

  if v_max_attempts is null or v_next_attempt <= v_max_attempts then
    if p_retry_at is not null then
      v_next_available := p_retry_at;
    else
      -- Legacy tasks may contain retry strategies that predate validation. If
      -- one is invalid, fail the task permanently rather than wedging the queue.
      begin
        v_delay_seconds := absurd.retry_delay_seconds(v_retry_strategy, v_attempt);
        v_next_available := v_now + (v_delay_seconds * interval '1 second');
      exception
        when sqlstate 'AB003' then
          v_next_available := null;
      end;
    end if;

    if v_next_available < v_now then
      v_next_available := v_now;
    end if;

    if v_cancellation is not null then
      v_max_duration := (v_cancellation->>'max_duration')::bigint;
      if v_max_duration is not null and v_first_started is not null then
        if extract(epoch from (v_next_available - v_first_started)) >= v_max_duration then
          v_task_cancel := true;
        end if;
      end if;
    end if;

    if not v_task_cancel and v_next_available is not null then
      v_task_state_after := case when v_next_available > v_now then 'sleeping' else 'pending' end;
      v_new_run_id := absurd.portable_uuidv7();
      v_recorded_attempt := v_next_attempt;
      v_last_attempt_run := v_new_run_id;
      execute format(
        'insert into absurd.%I (run_id, task_id, attempt, state, available_at, wake_event, event_payload, result, failure_reason)
         values ($1, $2, $3, $4, $5, null, null, null, null)',
        'r_' || p_queue_name
      )
      using v_new_run_id, v_task_id, v_next_attempt, v_task_state_after, v_next_available;
    end if;
  end if;

  if v_task_cancel then
    v_task_state_after := 'cancelled';
    v_cancelled_at := v_now;
    v_recorded_attempt := greatest(v_recorded_attempt, v_attempt);
    v_last_attempt_run := p_run_id;
  end if;

  execute format(
    'update absurd.%I
        set state = $2,
            attempts = greatest(attempts, $3),
            last_attempt_run = $4,
            cancelled_at = coalesce(cancelled_at, $5)
      where task_id = $1',
    't_' || p_queue_name
  ) using v_task_id, v_task_state_after, v_recorded_attempt, v_last_attempt_run, v_cancelled_at;

  execute format(
    'delete from absurd.%I where run_id = $1',
    'w_' || p_queue_name
  ) using p_run_id;
end;
$$;

-- end migration: 0.4.0-0.5.0.sql
