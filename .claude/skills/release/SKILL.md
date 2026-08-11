---
name: release
description:
  Use when cutting a django-absurd release to PyPI — deciding the next version (with the
  human), landing the CHANGELOG.md section, and creating the GitHub Release that triggers
  publish.yml. Heavy human-in-the-loop: the human chooses the version and approves the
  cut; the pypi environment reviewer is a second, built-in gate.
---

# release

## Overview

How django-absurd ships to PyPI. Releases are **driven from GitHub Releases**, not tag
pushes: `.github/workflows/publish.yml` triggers on `release: published`, builds
(`uv build`), publishes via **Trusted Publishing** (OIDC, no tokens), and attaches the
wheel + sdist to the release. The version is derived from the `v*` tag by **hatch-vcs**
— PEP 440, no file to bump.

The release notes are **not written at cut time**. `CHANGELOG.md` is the single source:
its top section is added on a PR before the cut (`git-cliff --prepend`, then hand-edited
there), and the GitHub Release body is a verbatim slice of that section. Write the prose
once, in the changelog PR, where it is reviewable.

This is a **heavy human-in-the-loop** workflow. The assistant prepares and proposes; the
**human decides and approves** at every consequential step. Three gates:

1. **Version choice** — the human picks the version. NEVER auto-increment and proceed;
   present options with reasoning and stop for an explicit choice.
2. **Cut approval** — the human approves the exact version + notes before the release is
   created.
3. **PyPI deployment** — the `pypi` GitHub environment has a required reviewer; the
   publish job pauses until a human approves the deployment in the Actions run (the
   assistant cannot approve it).

Never bypass a gate. Never `git tag && git push` a version tag directly — that creates
no Release and won't publish.

## Choosing the version — the human decides

Do **not** mechanically bump the last tag. The version is a judgement call about what
changed and how stable it is. The assistant's job is to **lay out the options and the
reasoning, then ask** — present the change summary, map it to candidate versions, and
let the human choose. Surface disagreement (e.g. "these look like breaking changes, so
I'd lean beta over another alpha — your call").

**Where we are:** pre-1.0, the `0.1.0` line, shipping **alpha** pre-releases (`v0.1.0a1`
→ `a2` → … → `a5`). The history: `git tag --list 'v*' | sort -V`, or the section
headings in `CHANGELOG.md`.

**PEP 440 pre-release suffixes** (what `pip` does):

- `aN` (alpha), `bN` (beta), `rcN` (release candidate) — all install only with
  `pip install --pre`. Tag each with the GitHub **"pre-release"** flag.
- no suffix (`v0.1.0`) — the real release; installs by default.

**How to reason about the next number (present these, let the human pick):**

- **Another alpha** (`a(N+1)`) — still iterating toward `0.1.0`; API/behavior still
  moving. The default during this phase.
- **Move to beta / rc** (`b1` / `rc1`) — feature-set for `0.1.0` is settling; you want
  wider testing without more API churn. A deliberate phase change — confirm intent.
- **Bump the target line** (`0.2.0aN`, etc.) — only if scope grew enough that `0.1.0` no
  longer names it.
- **Semantic versioning** governs the target line. Pre-1.0 (`0.x`): the API is unstable,
  so a `0.MINOR` bump may include breaking changes; `0.x.PATCH` is for fixes. Post-1.0:
  MAJOR = breaking, MINOR = backward-compatible features, PATCH = fixes. The first
  stable cut is `v0.1.0` (no suffix) — a separate, explicit decision (see Guardrails).

## Steps

1. **Pre-flight.**
   - Release is cut from up-to-date `main`:
     `git fetch origin && git log --oneline origin/main -1`.
   - CI is green on that commit (`gh run list --branch main --limit 5`). Don't release
     red `main`.
2. **Summarize what changed** since the last tag:

   ```bash
   uv run git-cliff --unreleased
   ```

   That is every unreleased commit, already filtered and grouped exactly as it will
   appear in the changelog (`cliff.toml` drops `chore`/`ci`/`test`/`style`/`refactor`).
   It is the input to the version decision. Don't render it to a file yet — the version
   isn't chosen.

3. **GATE 1 — version choice (human decides).** Present the change summary and the
   candidate versions from "Choosing the version" above, each with its reasoning and
   your recommendation. STOP. Do not pick for them. Proceed only with an explicitly
   chosen version string.

4. **Land the changelog section** — a normal PR, before anything outward happens. `main`
   is branch-protected, so this cannot be pushed directly.

   ```bash
   git fetch origin
   git checkout -b changelog-v0.1.0aN origin/main
   uv run git-cliff --unreleased --tag v0.1.0aN --prepend CHANGELOG.md
   ```

   `--tag` supplies the heading for a tag that does not exist yet. `--prepend` inserts
   the new section at the top and **leaves every existing section untouched** — see the
   invariant below.

   **This PR is where the hand-written prose goes.** git-cliff gives you the commit
   subjects; a release usually wants more — a "Highlights" lead, "Upgrade notes" for
   anything breaking, better wording on a terse subject. Edit the new section in the
   file, in this PR, where a human reviews it. It costs nothing extra: the release body
   is a slice of this section, so whatever you write here flows into the GitHub Release
   for free.

   Three things to fix while you are in there:

   - **Check the date.** git-cliff stamps the heading with the generation date in UTC,
     not the release date — a run late on the 10th local time renders `2026-08-11`, and
     if the PR then sits for a day the published date is simply wrong. Set it to the day
     you expect to cut.
   - **Delete internal-only `docs:` entries.** Spec/plan retirement, agent-memory
     trimming, doc-site plumbing — real commits, but meaningless to someone who depends
     on the library. Keep the documentation changes a _user_ would care about.
   - **Triage the "N commits were skipped due to parse error(s)" warning** if git-cliff
     printed one. Rerun with `-vv` to see which. Anything that isn't a pre-mandate
     Renovate subject is user-visible work that would otherwise be omitted permanently:
     once the tag lands, that commit is below the next release's range boundary and no
     later run will ever see it. Add an entry by hand.

     _One-time, for the first release cut this way:_ the range reaches back before
     conventional commits were mandated (~#131), so this warning covers 13 commits.
     Twelve are Renovate; the thirteenth,
     `Cross-database pg_cron scheduling (restore xdist + test isolation) (#107)`, is
     real runtime work in `django_absurd/pg_cron/` and **must** get a hand-written
     entry. Afterwards the warning should be rare — treat any occurrence as something to
     look at, not as routine.

   Then, with the section final (prettier reflows the generated one-line bullets, so run
   this last):

   ```bash
   uv run pre-commit run --all-files
   git commit -am "chore: changelog for v0.1.0aN"
   git push -u origin changelog-v0.1.0aN && gh pr create --fill
   ```

   `chore:` is deliberate — `--fill` makes this subject the PR title, squash-merge makes
   the PR title the commit subject, and `cliff.toml` drops `chore` wholesale. Title it
   `docs:` and this PR comes back as a Documentation bullet in the NEXT release, every
   release, forever.

   Merge it before continuing. Nothing outward has happened yet — this is still an
   ordinary, revertible PR.

5. **Slice the release body** out of the merged changelog — the top section only:

   ```bash
   git fetch origin && git checkout main && git pull
   git log --oneline <changelog-merge-commit>..origin/main   # must be empty of kept types
   awk '/^## \[/ {n++} n == 1' CHANGELOG.md > /tmp/release-notes.md
   ```

   **That first check is not optional.** The section was generated before the changelog
   PR merged, but the tag lands on `main`'s HEAD at cut time — so anything merged in
   between falls in neither range and is lost for good. If that log shows a `feat`,
   `fix`, `perf`, `docs`, `build` or `revert`, go back to step 4 for another pass (a
   `chore`/`ci`/`test`/`style`/`refactor` is fine, it would not have rendered anyway).

   The counter increments on each release heading and the line prints only while the
   count is 1, so you get the newest heading plus its body and nothing of the release
   below it. Read the file before showing it: it must start with the `## [<version>]`
   heading and end with that section's last bullet, with no trace of the release below.

6. **GATE 2 — cut approval.** Show the final version + pre-release flag + the contents
   of `/tmp/release-notes.md`. STOP for an explicit "yes." Cutting is outward and
   effectively irreversible — a published PyPI version can never be reused.

   If the notes need changing, that is another PR against `CHANGELOG.md` — **hand-edit
   the existing top section; do not re-run the prepend.** The tag still does not exist,
   so `--tag` would render the identical range a second time and stack a duplicate
   section above the merged one. Then re-slice (step 5) and come back to this gate.

7. **Create the release** (creates the tag AND triggers `publish.yml`):

   ```bash
   gh release create v0.1.0aN --target main --prerelease \
     --title v0.1.0aN --notes-file /tmp/release-notes.md
   ```

   Use `--prerelease` for any `a`/`b`/`rc`; omit it only for a final release.
   `--target main` ties the tag to `main`'s HEAD. Never `--generate-notes` — it would
   ignore the notes the changelog PR just reviewed and re-derive its own from commit
   titles.

8. **GATE 3 — approve the PyPI deployment (human, in GitHub).** The publish job waits on
   the `pypi` environment reviewer. Tell the human: **Actions → the running "Publish to
   PyPI" run → Review deployments → approve `pypi`.** The assistant cannot approve it.
9. **Verify.** The workflow attaches wheel + sdist to the release and PyPI shows the
   version. Confirm `pip install --pre django-absurd==<version>` resolves (pre-releases
   need `--pre`) and the release page has the two assets.

## Guardrails

- **NEVER regenerate `CHANGELOG.md`.** `git-cliff -o CHANGELOG.md` (or `--output`, or a
  bare `git-cliff > CHANGELOG.md`) rewrites the file from scratch from the commit
  history and **destroys every hand edit in it** — including the `v0.1.0a2`, `a3` and
  `a4` sections, which predate conventional commits and exist ONLY as hand-written text
  that no regeneration can reproduce. The only way the file may ever change is
  `git-cliff --unreleased --tag <version> --prepend CHANGELOG.md` plus deliberate hand
  edits. There is no "just this once."
- Trusted Publishing (OIDC) — no API tokens. Auth is the `pypi` environment + the
  Publisher registered on pypi.org (a one-time manual PyPI setup, already done for this
  project).
- The `pypi` environment restricts deployments to `v*` tags and requires a reviewer
  (`marcgibbons`).
- **First stable (non-pre) `v0.1.0`** is its own deliberate decision — confirm the API
  is ready, and first ensure README + LICENSE + license metadata are in place (twine
  warns on a missing long_description for pre-releases today).
- A mistaken release: you can delete the GitHub release + its tag, but a published PyPI
  version is permanent — yank it, never reuse the number; cut the next pre-release
  instead.
- See `.github/workflows/publish.yml` for the authoritative pipeline.
