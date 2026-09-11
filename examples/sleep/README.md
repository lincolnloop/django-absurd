# django-absurd — sleep example

Demonstrates **durable sleep** (`context.sleep_for`) with django-absurd and nanodjango.

- Start a three-message onboarding sequence: **welcome** → sleep → **tip** → sleep →
  **nudge**. Each message is a checkpoint; each gap is a real suspension, not a busy
  wait or an in-memory timer.
- Watch the sequence page auto-refresh, showing which messages have sent and — while
  a sleep is in progress — the exact wake time Postgres is holding for it.
- **Kill the worker mid-sleep and watch it resume on schedule:** start a sequence with
  a wait long enough to catch (`docker compose up`, submit with e.g. 60 seconds), then
  `docker compose restart worker` while the page shows "asleep". The sequence still
  wakes and completes on time — nothing about the sleep lived in the worker process.
- Browse queue tables in the auto-registered admin — the Checkpoints and Runs inlines
  on a task are the same rows this page reads.

django-absurd is installed from the local checkout so the demo runs against this
branch's code.

## Run

```
docker compose up
```

- `http://localhost:8000/` — start a sequence
- `http://localhost:8000/admin/` — read-only queue tables (login: **admin** / **admin**)

Running more than one example at once? Override the published port:
`APP_PORT=8010 docker compose up`.

Tear down: `docker compose down -v`

## Test

Runs in the demo's own Docker stack, the way CI runs it:

```
docker compose up -d --build --wait db
docker compose run --rm --build app sh -c "cd /app && pytest"
```

`tests/test_sequence.py` exercises the sleeps themselves using the `dj_absurd` fixture's
`freeze_time`/`shift` — the same durable-time pattern documented in
[django_absurd/AGENTS.md](../../django_absurd/AGENTS.md#move-durable-time) — so the
suite moves through both cooldowns instantly instead of waiting on the wall clock.
