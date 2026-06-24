# /dream Knowledge Distillation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or superpowers:executing-plans
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `/dream` — a command that distills the durable "why" from the project's
specs/plans into `docs/WHY.md` and retires consumed/obsolete docs into a
`docs/HISTORY.md` ledger with recoverable git SHA links.

**Architecture:** Two single-purpose project skills (`capture-why`, `archive-specs`)
plus a `/dream` command that runs them in order (capture before delete). Deliverables
are markdown skill/command files and human-first docs — verified by running the skills
against the real repo, not by unit tests.

**Tech Stack:** Claude Code project skills (`.claude/skills/<name>/SKILL.md`) and
commands (`.claude/commands/<name>.md`); markdown docs under `docs/`; git/`gh` for SHA
links.

## Global Constraints

- Source spec: `docs/specs/2026-06-24-dream-knowledge-distillation-design.md` (copied
  verbatim where quoted below).
- **Avoid staleness at all costs.** `WHY.md`/`HISTORY.md` never duplicate current
  behavior (that lives in code / AGENTS.md / README). Only sanctioned "stale" =
  explanatory rationale ("tried X, chose Y because…").
- **Filter rule:** keep a fact only if NOT knowing it would let someone make a wrong or
  redundant decision; drop reversible plumbing and trial-and-error.
- **Why, not how:** `WHY.md` carries no file/class/module names and no structure map;
  not a changelog.
- **Audience:** `WHY.md` + `HISTORY.md` are human-first (readable prose), useful to
  LLMs; human readability wins on conflict.
- **`HISTORY.md` entry:**
  `<authored-date> — <brief summary> → [view @<sha>](<origin/main blob URL>)`;
  authored-date = the `YYYY-MM-DD` filename prefix.
- **SHA source:** `origin/main` blob URL; the file must exist at that SHA. Record link,
  then delete.
- **Safety:** `archive-specs` has a hard confirm gate; never delete a file not first
  recorded in `HISTORY.md`.
- **Ordering:** `/dream` runs `capture-why` then `archive-specs`.
- **Naming:** verb-first; no leading-underscore names (project CLAUDE.md conventions).
- Repo: `lincolnloop/django-absurd`. Blob URL form:
  `https://github.com/lincolnloop/django-absurd/blob/<sha>/<path>`.

---

### Task 1: Relocate docs out of `superpowers/` and fix CLAUDE.md paths

**Files:**

- Move: `docs/superpowers/specs/*` → `docs/specs/`, `docs/superpowers/plans/*` →
  `docs/plans/`
- Remove: `docs/superpowers/` (empty after move)
- Modify: `CLAUDE.md` (the `docs/superpowers/` reference on line 5)

**Interfaces:**

- Produces: stable doc locations `docs/specs/` and `docs/plans/` that every later task
  and the two skills read from.

- [ ] **Step 1: Move the spec and plan files with git**

```bash
git mv docs/superpowers/specs/* docs/specs/
mkdir -p docs/plans && git mv docs/superpowers/plans/* docs/plans/
rmdir docs/superpowers/specs docs/superpowers/plans docs/superpowers
```

- [ ] **Step 2: Update the CLAUDE.md path reference**

Change the "Specs + plans live in `docs/superpowers/`." line to point at `docs/specs/`
and `docs/plans/`.

- [ ] **Step 3: Verify no stale references remain**

Run:
`grep -rn "docs/superpowers" . --include=*.md --include=*.py ; test ! -d docs/superpowers && echo OK`
Expected: no `docs/superpowers` hits in tracked files; prints `OK`.

- [ ] **Step 4: Verify the suite still passes (packaging test excludes docs from the
      dist regardless)**

Run: `PGPORT=5433 uv run pytest -q` Expected: PASS (no change to shipped artifacts).

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "docs: move specs/plans out of superpowers/ into docs/{specs,plans}"
```

---

### Task 2: Add the superpowers + caveman section to CLAUDE.md

**Files:**

- Modify: `CLAUDE.md`

**Interfaces:**

- Produces: a user-facing section listing the available superpowers skills and caveman
  mode. No downstream dependency.

- [ ] **Step 1: Add a "Tooling available in this project" section**

Add a CLAUDE.md section telling the user (in prose) that this repo has the
**superpowers** skills (brainstorming, writing-plans/specs, TDD, systematic-debugging,
revdiff, etc.) and **caveman** mode (compressed responses, toggled with `/caveman`), and
when to reach for them. State only what is true; do not enumerate skills that aren't
installed.

- [ ] **Step 2: Verify**

Run: `grep -niE "superpowers|caveman" CLAUDE.md` Expected: the new section is present
and mentions both.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md && git commit -m "docs: note superpowers skills + caveman mode in CLAUDE.md"
```

---

### Task 3: Author the `capture-why` skill

**Files:**

- Create: `.claude/skills/capture-why/SKILL.md`

**Interfaces:**

- Consumes: `docs/specs/`, `docs/plans/`, and the codebase (for grounding).
- Produces: `docs/WHY.md` (created/refreshed). `/dream` (Task 5) invokes this skill by
  name `capture-why`.

- [ ] **Step 1: Author the skill via the writing-skills sub-skill**

REQUIRED SUB-SKILL: invoke `superpowers:writing-skills` to create
`.claude/skills/capture-why/SKILL.md`. Frontmatter `name: capture-why`; `description`
must state when to use it, e.g.: "Distill the durable 'why' (intent + load-bearing
rationale) from docs/specs and docs/plans into docs/WHY.md, grounded against the code.
Use when consolidating project reasoning or refreshing WHY.md. Never deletes files."

The skill body must encode this contract (from the spec):

- Read `docs/specs/`, `docs/plans/`, and ground every retained "why" against the current
  code; drop any whose subject no longer exists in code.
- Apply the **filter rule** (Global Constraints) — keep only load-bearing reasons; drop
  reversible plumbing/trial-and-error.
- Write `docs/WHY.md` (human-first prose) under stable thematic sections (Intent;
  Database & connection; Schema & migrations — including the maintainer "regenerate SQL
  delta with `absurdctl` when bumping the pin" note; Tasks, enqueue & worker;
  Deliberately-not-doing). Include the top banner pointing to code/AGENTS.md/README for
  "how".
- **Why, not how:** forbid file/class/module names and structure description; forbid
  changelog phrasing.
- Re-run = merge/refresh: fold in new material, REPLACE any "why" now wrong; keep the
  sanctioned "tried X, chose Y" form.
- Never delete or move any file.

- [ ] **Step 2: Verify the skill loads and runs**

Invoke `capture-why` in this repo. Confirm it produces `docs/WHY.md`. Expected: file
exists with the stable sections.

- [ ] **Step 3: Verify the altitude (the load-bearing check)**

Run:
`grep -nE "\.py|class |def |SKILL|backends|worker\.py|checks\.py" docs/WHY.md || echo "no structure references — good"`
Expected: prints the "good" line (no file/class/module names leaked into WHY.md).

- [ ] **Step 4: Commit**

```bash
git add .claude/skills/capture-why/SKILL.md docs/WHY.md
git commit -m "feat: add capture-why skill; generate docs/WHY.md"
```

---

### Task 4: Author the `archive-specs` skill

**Files:**

- Create: `.claude/skills/archive-specs/SKILL.md`

**Interfaces:**

- Consumes: `docs/specs/`, `docs/plans/`, `docs/WHY.md` (to judge "fully captured"),
  `git`/`gh` (for the `origin/main` SHA).
- Produces: `docs/HISTORY.md` (append-only ledger) and deletions of retired docs.
  `/dream` (Task 5) invokes this skill by name `archive-specs`.

- [ ] **Step 1: Author the skill via the writing-skills sub-skill**

REQUIRED SUB-SKILL: invoke `superpowers:writing-skills` to create
`.claude/skills/archive-specs/SKILL.md`. Frontmatter `name: archive-specs`;
`description`, e.g.: "Retire obsolete or fully-captured specs/plans: record each in
docs/HISTORY.md with an origin/main SHA link, then delete it — behind a confirm gate.
Use after capture-why when cleaning up docs/specs and docs/plans."

The skill body must encode this contract (from the spec):

- Identify candidates: a doc is retireable if it is **obsolete** (superseded by a later
  doc / abandoned approach / contradicted by code) or **fully captured** in
  `docs/WHY.md`.
- For each candidate, resolve the `origin/main` blob SHA URL (the file must exist at
  that SHA — use the latest `origin/main` commit that contains the file).
- **Hard confirm gate:** present the full list (file, one-line summary, SHA URL) and
  require explicit user approval before any deletion.
- On approval, for each: append `<authored-date> — <summary> → [view @<sha>](<url>)` to
  `docs/HISTORY.md` (authored-date from the filename prefix), THEN `git rm` the file.
  Record-link-then-delete; never delete a file not first recorded.
- Never distill or write `WHY.md` (that is `capture-why`'s job).

- [ ] **Step 2: Verify the confirm gate and dry-run listing**

Invoke `archive-specs`; confirm it lists candidates with summary + resolvable SHA URLs
and pauses for approval before deleting anything. Expected: no file deleted without
explicit approval.

- [ ] **Step 3: Verify SHA links resolve**

For each entry it proposes, run:
`gh api repos/lincolnloop/django-absurd/contents/<path>?ref=<sha> --jq .name` Expected:
returns the filename (file exists at that SHA).

- [ ] **Step 4: Commit the skill (no doc deletions yet)**

```bash
git add .claude/skills/archive-specs/SKILL.md
git commit -m "feat: add archive-specs skill"
```

---

### Task 5: Author the `/dream` command

**Files:**

- Create: `.claude/commands/dream.md`

**Interfaces:**

- Consumes: the `capture-why` and `archive-specs` skills (Tasks 3–4).
- Produces: the `/dream` command.

- [ ] **Step 1: Write the command**

Create `.claude/commands/dream.md` instructing: run `capture-why` first (refresh
`docs/WHY.md`), then run `archive-specs` (retire consumed/obsolete docs). State the
ordering rationale (capture before delete) and that `archive-specs`' confirm gate still
applies. Keep it short — it orchestrates, it doesn't reimplement.

- [ ] **Step 2: Verify**

Invoke `/dream`. Confirm it runs `capture-why` then `archive-specs` in that order.
Expected: WHY.md refreshed before any deletion is proposed.

- [ ] **Step 3: Commit**

```bash
git add .claude/commands/dream.md
git commit -m "feat: add /dream command (capture-why then archive-specs)"
```

---

### Task 6: Run `/dream` for real — distill + clean up the current docs

**Files:**

- Create: `docs/WHY.md` (final content), `docs/HISTORY.md`
- Remove: the obsolete/captured docs under `docs/specs/` and `docs/plans/`

**Interfaces:**

- Consumes: everything above.

- [ ] **Step 1: Run `/dream` against the real repo**

Invoke `/dream`. `capture-why` refreshes `docs/WHY.md`; `archive-specs` proposes the
retirement list (the audit already flagged obsolete docs: `tasks-api-worker` superseded
by lazy-discovery; the `host-dev-tox-matrix` version-matrix doc; the
`migration-wrapping` Docker-app model; the `queue-models`/`configurable-absurd-database`
deleted-settings specs).

- [ ] **Step 2: Review and approve the retirement list at the confirm gate**

Confirm each SHA URL resolves before approving deletion.

- [ ] **Step 3: Verify the end state**

Run: `cat docs/HISTORY.md` and confirm every retired doc has an entry with a working SHA
link; `ls docs/specs docs/plans` shows only docs still considered live. Expected: no
orphaned deletions (every deleted file is in HISTORY.md); WHY.md present.

- [ ] **Step 4: Verify the suite still passes**

Run: `PGPORT=5433 uv run pytest -q` Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "docs: distill WHY.md + retire consumed specs/plans into HISTORY.md"
```

---

## Self-Review

- **Spec coverage:** WHY.md (T3), HISTORY.md (T4), capture-why (T3), archive-specs (T4),
  `/dream` ordering (T5), filter/staleness/why-not-how/audience rules (Global
  Constraints + T3 contract + T3 Step 3 check), code-grounding (T3 contract), SHA +
  confirm-gate safety (T4 contract + Steps 2–3), folder move + CLAUDE.md (T1–T2), real
  cleanup (T6). All spec sections map to a task.
- **Placeholder scan:** no TBD/TODO; each skill task gives a concrete behavioral
  contract + an observable verification (skill bodies are authored by writing-skills
  during execution, per the project's "no coding ahead" rule — intentionally not
  pre-written here).
- **Type/name consistency:** skill names `capture-why` / `archive-specs` and doc paths
  `docs/WHY.md` / `docs/HISTORY.md` used identically across tasks and the `/dream`
  command.
