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

**Backfill.** One-time full generation over `a1..a5`. Those sections are mechanical
commit subjects, poorer than the curated GitHub notes for `a5`. Accepted — the GitHub
releases keep their prose, changelog is the mechanical spine.

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

Dropped entirely: `chore` (including all of `chore(deps)`), `ci`, `build`, `test`,
`style`, `refactor`.

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
- Retro-fitting curated prose into backfilled `a1..a5` sections.

## Verification

- Backfill over `a1..a5` renders each release, no crash.
- No Renovate subject appears anywhere in the output.
- A breaking commit (`feat(pg_cron)!:`, `feat!:`) lands under Breaking changes, not
  Features.
- Slicing the top section from `CHANGELOG.md` yields exactly one release's content, no
  leading/trailing heading bleed.
- Every entry's `(#N)` resolves to the right PR.
