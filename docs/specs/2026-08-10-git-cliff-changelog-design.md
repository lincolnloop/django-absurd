# git-cliff changelog

## Problem

No `CHANGELOG.md`. Release notes hand-written into GitHub Release body each cut
(`v0.1.0a1`..`a5`). Nothing in repo records what changed; `pip`-only consumers and
anyone reading the source tree see nothing. Notes exist only on github.com.

History is well-suited to generation: PR titles gated by Conventional Commits
(`.github/workflows/pr-title.yml`), squash-merge, so every commit on `main` is
`type(scope): subject (#N)`.

## Decisions

**Both artifacts.** `CHANGELOG.md` checked in; the newest section is also the GitHub
Release body. One source of truth, no divergence.

**Generated now, hand-editable later.** Content today is 100% generated. Mechanics built
for hand editing from day one — `--prepend` only, never `-o` after the backfill, and the
release body is sliced out of the _file_, not out of cliff's stdout. When prose starts
landing in the changelog PR, no step changes.

**Local, human-driven.** git-cliff runs on the maintainer's machine as a step in the
`release` skill. No CI, no bot token, no branch-protection bypass. Fits existing three
gates.

**Released sections only.** The checked-in file never carries an `Unreleased` heading. A
section appears when a version is cut, via `--unreleased --tag <version> --prepend`, and
not before. Rendering unreleased work into the file would bake in whatever WIP commits
happened to be on the branch that day — commits that squash-merge collapses anyway.

**Backfill, then hand-fix once.** Conventional Commits only became mandatory around
`#131`; earlier titles are plain prose (`Events`, `Cleanup & retention`,
`Read-only admin + ORM access for Absurd queue tables (#17)`) and parse into no type.
Generate over `a1..a5` anyway, then rewrite the unparsed entries into the right sections
as part of building this — a one-time pass over five releases, and the first exercise of
the hand-editing the whole design is built around. Drawn from each release's curated
GitHub notes, reviewed by the maintainer in the PR like any other content.

**Renovate dropped entirely.** `chore(deps)` is ~60% of commits and, by construction,
100% dev/CI tooling — pre-commit hooks, GitHub actions, docker tags, the pinned dev
group. Runtime deps are declared as ranges (`Django>=6.0`, `croniter>=6.0`,
`absurd-sdk>=0.5.0,<0.6.0`) and `renovate.json` sets `rangeStrategy: update-lockfile`,
so Renovate only ever moves `uv.lock`, never a floor. Floor changes are hand-authored
and already land under Features or Breaking changes. Nothing a library consumer needs is
lost by dropping the type.

## Shape

`cliff.toml` at repo root. `git-cliff` pinned exactly in the `dev` dependency group
(matches every other dev pin; Renovate then tracks it). Invoked `uv run git-cliff`.

Sections, in order:

- `Breaking changes` — any `!` marker or `BREAKING CHANGE:` footer, regardless of type
- `Features` (`feat`)
- `Bug fixes` (`fix`)
- `Performance` (`perf`)
- `Documentation` (`docs`)
- `Requirements` (`build`) — see below

Dropped entirely: `chore` (including all of `chore(deps)`), `ci`, `test`, `style`,
`refactor`.

### Floor changes stay visible

Raising a supported floor (`Django`, `absurd-sdk`, `croniter`, Python) is user-visible
and must appear. Two layers:

- **Convention.** A floor change is titled `feat` or `feat!` with the floor named in the
  subject, never `chore(deps)`. Already the practice — the `absurd-sdk` move landed as
  `feat!: regenerate the schema as a single 0.5.0 install (#169)`.
- **Safety net.** The `build` type is kept, as `Requirements`. Renovate's
  `semanticCommitType` is pinned to `chore` in `renovate.json`, so a `build(...)` commit
  can only be human-authored — nothing automated can leak in, and a floor bump titled
  `build(deps):` is caught rather than silently dropped.

Each entry carries its PR link. Each release heading links the `compare/<prev>...<this>`
range.

A release containing only dropped types renders an empty section. Acceptable — a release
with no user-visible change should say so.

## Release flow

`.claude/skills/release/SKILL.md` gains a changelog step between GATE 1 (version chosen)
and GATE 2 (cut approval):

1. branch from up-to-date `origin/main`
2. `uv run git-cliff --unreleased --tag <version> --prepend CHANGELOG.md` — the tag does
   not exist yet, `--tag` supplies the heading
3. commit, PR, merge — **this PR is the hand-edit seam**; prose added here flows to the
   release body for free
4. slice the top section out of merged `CHANGELOG.md` into a notes file
5. GATE 2 shows that file; `gh release create --notes-file <it>`

Step 2 also replaces the skill's current "summarize what changed" step —
`git cliff --unreleased` is a better input to the version decision than a raw `git log`.

## Out of scope

- Any CI automation. Manual by choice.
- A changelog page on the docs site.
- Any record of dependency updates. If a runtime floor ever needs surfacing, it is a
  hand-authored commit and gets a `feat`/`fix` title like any other change.

## Verification

- Backfill over `a1..a5` renders each release, no crash.
- No Renovate subject appears anywhere in the output.
- No unparsed prose subject survives in the final file — every `a1..a5` entry sits under
  a real section.
- The `absurd-sdk` 0.5.0 floor move appears under Breaking changes.
- A breaking commit (`feat(pg_cron)!:`, `feat!:`) lands under Breaking changes, not
  Features.
- Slicing the top section from `CHANGELOG.md` yields exactly one release's content, no
  leading/trailing heading bleed.
- Every entry's `(#N)` resolves to the right PR.
