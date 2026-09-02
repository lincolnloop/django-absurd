# `benchmarks/` — why the rig is configured this way

[`README.md`](README.md) is the actions: how to start the server, run the stages, read
the report. This file is the reasoning behind the configuration — the sizing evidence,
the things that are deliberately NOT variables, and what a results file can and cannot
honestly say about the server it measured. Rationale belongs here; the README stays
short.

## The five variables

`db_bench` in the root `compose.yaml` takes `BENCH_SHARED_BUFFERS` (1GB),
`BENCH_MAX_CONNECTIONS` (100), `BENCH_TMPFS_SIZE` (4g), `BENCH_CPUS` (unset) and
`BENCH_MEMORY` (unset), each spelled `${VAR:-default}` the way `PGPORT_BENCH` already
was.

The defaults were picked for the reference machine — 14 cores, 24 GB, an 11.7 GB Docker
VM — and 1 GB of shared buffers plus a 4 GB tmpfs is about 5 GB of that VM. Nobody has
run this on a 4 GB VM, and that is the point: the combination cannot fit one, so
Postgres would either refuse to start or die when the tmpfs filled. A rig meant to run
on more than one machine cannot hardcode the one it was built on.

**The 4g tmpfs is headroom, not a requirement of the data.** Measured: the database
itself is 69 MB, and two full `worker_knobs` runs used 355 MB. 4g covers a full
eight-stage run's WAL; `512m` is comfortable for single-stage work. That is why the
README tells a small machine to lower it rather than declaring itself unsupported.

**A too-small tmpfs fails late and there is no preflight check for it.** The server
starts, the run proceeds, and Postgres dies partway through on a write error, having
burned the run. Nothing in the harness reads the tmpfs size — that check is not built.
If it ever is, it belongs at the top of `stages.main`, before the first measurement,
where the cost of being wrong is a second rather than forty minutes.

**`BENCH_CPUS` / `BENCH_MEMORY` are wired, not tuned.** They exist for deferred
CPU-allocation work. Nothing here has measured what a container CPU limit does to any
number in a results file, and nothing in this directory may claim it has.

### `0`, not empty, is how compose spells "no limit"

`cpus: ${BENCH_CPUS:-}` looks like the natural way to write "unset" and breaks the file
outright:

```
error while interpolating services.x.cpus: failed to cast to expected type:
strconv.ParseFloat: parsing "": invalid syntax
```

At `${BENCH_CPUS:-0}` compose omits `cpus` from the rendered config entirely, which is
Docker's own "unlimited" — verified with `docker compose --profile bench config`, both
unset and with the variable exported empty (`:-` substitutes on an empty value as well
as an absent one, so both land on 0). Getting this wrong is silent in the direction that
matters: a literal `cpus: 0` in the file would not throttle the server, but a wrong
guess in the other direction fails to start a container nobody was watching.

## What is deliberately not a variable

- **`synchronous_commit`, `fsync`** — durability is the experiment's premise. Every task
  rate this rig prints is a commit rate in disguise, so a variable here invites someone
  to benchmark non-durably by accident and publish the number, which is exactly the
  unlabelled claim the harness exists to refuse. Hardcoded on. The tmpfs already makes
  fsync cost a memcpy; that is a change of medium, recorded as one, not a change of
  request.
- **`shared_preload_libraries`, `pg_stat_statements.track`** — the measurement
  instrument. Runs taken with and without the tracking are not comparable, so it is
  always on and never a flag. `all` rather than `top` because Absurd's claim path runs
  inside PL/pgSQL and `top` would record the function call while hiding every statement
  in it.
- **`cluster_name`** — provenance. It has to describe what the server IS (`bench-tmpfs`,
  the only thing in a results file saying the data directory was RAM), not what someone
  passed on the command line. `report.describe_storage_medium` keys its "not for
  publishing" warning off this exact name.

## Three consistency knobs, measured and refused

Autovacuum off per queue table, a `checkpoint_timeout` beyond a run's length with an
explicit inter-rep `CHECKPOINT`, and an `ANALYZE` between preload and workers. All three
were built, run, and **refused on the numbers**; nothing in the harness applies any of
them, which is why no results file records them.

Method: six `worker_knobs` runs at `--max-workers 10` on `db_bench`, three of them
UNCHANGED baselines and one per knob, judged on the two things a knob claims to improve
— the within-rep drain profile (least-squares trend over a rep's 25 equal-count
`profile_slices`, read as total fractional drift across the drain) and the cross-rep
`cv`. The three baselines set the noise bracket: mean cv 3.4/6.3/9.0%, mean slope
+14/+17/+33%, mean `profile_cv` 9.4/9.2/13.7%. A knob landing inside that bracket
measured nothing.

**Autovacuum off: measurably worse, and the interference it removes is load-bearing.**
Mean slope +60% and mean `profile_cv` 20.2% — the slope above every baseline on every
rung of the concurrency ladder (78/94/68/50/32% against the worst baseline's
43/29/28/21/16%), so five independent measurements agreeing, not one outlier. Every rep
accelerated monotonically through its own drain: `concurrency_2` slices ran 173 → 492
tasks/s inside one rep against 211 → 331 with autovacuum on, and the ladder's medians
came in 10-20% LOW. Cross-rep cv did tighten (3.7% overall, 1.1-1.6% on the low rungs)
but sits inside the baseline bracket, so the offer is a measured within-rep loss for an
unmeasurable cross-rep gain. The premise was sound — `r_bench` really does reach 10,000
dead tuples against 5,000 live, 24 autovacuums fired across the six runs, `t_bench`
peaked at 23 — so what the numbers say is that autovacuum's mid-rep work is what keeps a
drain flat rather than what disturbs it. The mechanism was not isolated; the ANALYZE arm
below rules out drain-start statistics being the difference.

**No checkpoint mid-run: the interference is not there to remove.** On this server a
whole run's checkpoints write ~2.7 MB — `pg_stat_checkpointer` after an unchanged run
reads 3 timed checkpoints and 337 buffers written, to RAM, paced over minutes. So there
is nothing for a rep to pay for, and the arm agrees: mean cv 9.7%, slope +30%,
`profile_cv` 11.7%, every figure inside the baseline bracket. The arm did do what it
claimed (`num_timed` 0 across the run, 24 explicit checkpoints, WAL down from 256 MB to
96 MB), so this is a measurement of the knob and not of a knob that failed to apply.
Both halves are refused together: nothing that writes 2.7 MB to a tmpfs can be worth a
compose change plus a step between every rep.

The tmpfs hazard did not materialise either way: `/var/lib/postgresql` peaked at 356 MB
of 4 GB across all six runs, WAL included. That headroom is bounded by `max_wal_size` (1
GB, unchanged) and not by the timeout, which is why raising the timeout alone was safe
to test.

**ANALYZE after the preload: no measurable effect.** Mean cv 5.3%, slope +16%,
`profile_cv` 9.1% — inside the baseline bracket on all three, at its good end. A step
that costs a rep something and buys a number nothing can distinguish from an unchanged
run is not worth keeping. Rate mode therefore never needed its own decision: the mode
with a preload to describe showed nothing, so nothing was carried to the mode without
one.

**The interference that dwarfs all three is between runs, not inside one.** The baseline
run taken FIRST — on a server up an hour but idle since its migrate, so no query on it
had ever run — measured 30-35% below two later unchanged repeats that agreed within 8%
(`concurrency_8` 587 against 936 and 870; `concurrency_4` 432 against 641 and 598), and
the winning batch size changed from run to run. That is the opposite direction from the
uptime drift [`README.md`](README.md) records under "Restarting Postgres between
stages", where a long-loaded server reads SLOWER than a restarted one, so idle uptime
and loaded uptime are evidently not the same variable. One observation each way is not a
rule; it is where the remaining leverage is, and no knob in this section touches it.

Two mechanics worth knowing before re-running any of this: table `reloptions` survive
`TRUNCATE`, so an `ALTER TABLE ... SET (autovacuum_enabled = false)` applied by hand
holds for a whole run; and `checkpoint_timeout` is SIGHUP-only, so `ALTER SYSTEM` plus
`pg_reload_conf()` tests it without the restart that would empty the tmpfs.

## Recording is what makes them worth configuring

Configurable-but-unrecorded is worse than hardcoded: two results files would differ for
reasons nothing in them explains. So `host.collect_host_context` records
`shared_buffers` and `max_connections` on every measurement, and
`report.describe_server_resources` prints them, mixed-valued the way a git SHA is when a
directory holds stages from two servers.

**Read off the server, never out of the harness's environment.** The variables size the
container at `up` time; the process taking the measurement need never have seen them,
and a server already running answers for what it was actually started with rather than
for whatever the current shell happens to export.

### The container-limit gap, and the choice made about it

Container CPU and memory limits are invisible to SQL. There is nothing to query, so
there are only bad options and one acceptable one.

`host.py` records `cpu_count` from `os.cpu_count()`, and that number is the HOST's — it
is the right number for what it describes, because driver, producer and every worker
process run on the host, and the `process_scaling` ladder is derived from it. It is the
wrong number for the server: limit the container to 4 CPUs and `cpu_count` still
reads 14. So the report labels it `host cpu count` and gives the server its own line,
and `requested_container_cpus` / `requested_container_memory` carry `BENCH_CPUS` /
`BENCH_MEMORY` when the harness's environment has them, printing `unknown` when it does
not.

`unknown` and not `unlimited`: an absent variable means nobody told this process, not
that no limit exists — whoever ran `docker compose up` may have set one in a shell the
harness never saw. The field name says `requested` for the same reason. A requested
limit is the weakest of the fields in that block, and it is still strictly better than
reporting host cores as though they described the server.
