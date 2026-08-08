# Docs site rewrite — example-first how-to

Scope: `docs/web/` only (8 published pages). README and `django_absurd/AGENTS.md` out of
scope, deferred.

Goal: every concept led by short terse example. No jibberish, no internals reader never
types. Docs read as how-to, not reference narrative.

Target: ~1600 lines → ~1000.

## House style

Applies to every page. These rules ARE the spec — per-page notes below are just where
they bite hardest.

1. **Example first.** Every `##` concept opens with a code block ≤10 lines. Nothing
   between heading and code block.
2. **Then ≤3 sentences.** Prose explains only what example can't show. No restating code
   in English.
3. **One example per concept.** Sync + async twin collapse into `=== "Sync"` /
   `=== "Async"` tabs on one example. Enable `content.tabs.link` in `zensical.toml` so
   tab choice syncs page-wide.
4. **Page opener ≤2 sentences.** Frontmatter, H1, one line saying what page is for, then
   straight to first concept.
5. **Cut internals.** Delete anything reader never types or reads back:
   - pg_cron job-name namespacing scheme (`_dj:<db>:<source>:<name>`)
   - `isinstance(bound, Task)` holds
   - `run_after` wrapper-row mechanics beyond "a second row appears, named
     `<path>:run_after`"
   - "mirrors the SDK's signatures" tours
   - which module a logger child comes from
6. **Admonitions only for damage.** `!!! warning` reserved for data loss / destructive
   commands (`absurd_flush`, pg_cron teardown, kill-switch). Everything else demoted.
7. **Django surface before Absurd-specifics.** Where a page covers both, plain Django
   API comes first and finishes before any django-absurd-only surface starts. Never
   interleave — a reader who only needs Django's API stops reading partway down and has
   missed nothing.

## Caveat tiers

Length decides location, not topic.

| Tier | Test                                  | Where                                                      |
| ---- | ------------------------------------- | ---------------------------------------------------------- |
| 1    | One line, belongs to one concept      | Bullet directly under that concept                         |
| 2    | Page-wide, owned by no single concept | Short bullet list at page bottom                           |
| 3    | Needs paragraphs                      | Not a gotcha — promote to concept with own example, or cut |

Tier 3 is the load-bearing rule. Most convoluted blocks today are concepts wearing a
warning costume. Hard cap: **>2 lines and it can't be a bullet → promote or delete.**

Consequence: most pages lose their trailing warnings dump. Page ending in warnings dump
reads unfinished.

One exception where hiding IS right: genuine background a reader may want but never
needs → collapsed `??? note`. Applies to pg_cron cluster architecture only.

## Per-page

### `index.md`

Quickstart already example-led. Trim install/requirements prose. Keep 5 numbered steps.

### `tasks.md`

**Django surface first, Absurd-specific second.** Today the basic Django loop is split
in half by the ~100-line `absurd_params` section. New order:

1. Enqueue
2. Read the result
3. Run it later (`run_after`)
4. Retries & spawn options (`absurd_params`)
5. Idempotency keys

Absurd params stay on this page — a reader looking up "enqueue with 3 retries" gets one
place to look.

- **"Define a task" section is cut.** `index.md` already teaches `@task`. Page opens on
  the enqueue example, decorator inside the same block (you need one to enqueue). The
  two facts that section carried — `async def` works, task can live in any importable
  module — become one line under it.
- "Retries & spawn options": lead with `@absurd_params` decorator example, then per-call
  `.bind()` example, then field table. Cut the three composition paragraphs → 2 tier-1
  bullets.
- Idempotency-key admonition (27 lines) → tier 3. Promote to H3 "Idempotency keys" with
  the collision example + 3 bullets (queue-scoped; different queues never collide; held
  until cleanup sweeps the row).
- `run_after`: keep example, cut wrapper narrative to 2 bullets.
- `max_attempts=None` paragraph → 1 bullet.

### `workflows.md`

Biggest cut, ~390 → ~200.

- Delete accessor tour at top. Replace with one steps example; accessor choice explained
  by the tabs themselves.
- Steps, sleep, events: each one concept, one tabbed example.
- `## Caveats` dissolves. Tier-1 bullets move up next to their concept
  (`absurd_sdk.TimeoutError` under Timeout; JSON round-trip under Steps; events vs
  `cleanup_ttl` under Events). Tier-2 remainder at bottom, ~3 bullets: no catch-all
  `except`; effectively-once; Absurd-backend-only.
- "`await_task_result` is not provided" → 1 bullet.
- "Our own exceptions" → move to `configuration.md` (config-shaped), leave 1 bullet.
- Keep the API table.

### `cron-jobs.md`

- Two how-tos: beat, pg_cron. Each opens with settings example then run command.
- pg_cron cluster-architecture paragraph → `??? note "How jobs reach your database"`.
- Admin-authoring section: keep two-step flow, cut
  resolution/`loaddata`/bypass-`.save()` prose to bullets.
- Operator prerequisites section stays at bottom — real how-to, different reader (DBA).
  Keep GRANT block verbatim.
- Keep destructive warnings (kill switch, teardown).

### `cleanup.md`

Light trim. Already close. Keep both `absurd_flush` warnings.

### `configuration.md`

Lead with one complete settings block, then the two queue-declaration forms, then
`OPTIONS` table, then check-ID table. Gains "Our own exceptions" from `workflows.md`.

### `logging.md` (new)

Extracted from `how-it-works.md`. Real how-to with an example, currently buried in a
concepts page. Opens with the `LOGGING` dict, then the two logger names.

### `testing.md`

- Keep the canonical `dj_absurd` example as opener.
- `freeze_time` 25-line paragraph → tier 3. Example + bullets (enter block before
  enqueue; no nesting; absolute elapsed time; install time-machine yourself).
- Snapshot field tables stay.
- Snapshot-caveat trio (attempts-created, sleeping-ambiguity, failure-None) stays —
  already bulleted.

### `how-it-works.md`

Only non-how-to page, stays that way. Short concept map + outbound links. Loses Logging.

## `zensical.toml`

- `nav`:
  `Home · Tasks · Workflows · Cron Jobs · Cleanup · Logging · Testing · Configuration · How it works`.
  Configuration moves down — reference, and index quickstart already covers minimal
  setup.
- `theme.features`: add `content.tabs.link`.
- New page needs `icon` frontmatter matching existing lucide set.

## Non-goals

- No content invention. Every fact in rewritten docs already exists in current docs or
  current code. Rewrite is reshaping and cutting, not researching.
- No API changes. Docs only.
- README, `AGENTS.md`: deferred.

## Verification

- Cross-links: every in-page anchor referenced elsewhere must still exist after headings
  move. Sweep `ag --hidden` over `docs/`, `README.md`, `django_absurd/AGENTS.md`,
  `examples/` for `.md#anchor` refs; fix breaks.
- Site builds clean.
- `pre-commit run --all-files` (prettier owns Markdown formatting).
- No test run — docs only, no behavior change.
