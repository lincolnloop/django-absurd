---
name: release
description:
  Use when cutting a django-absurd release to PyPI — deciding the next version (with the
  human), drafting notes, and creating the GitHub Release that triggers publish.yml.
  Heavy human-in-the-loop: the human chooses the version and approves the cut; the pypi
  environment reviewer is a second, built-in gate.
---

# release

## Overview

How django-absurd ships to PyPI. Releases are **driven from GitHub Releases**, not tag
pushes: `.github/workflows/publish.yml` triggers on `release: published`, builds
(`uv build`), publishes via **Trusted Publishing** (OIDC, no tokens), and attaches the
wheel + sdist to the release. The version is derived from the `v*` tag by **hatch-vcs**
— PEP 440, no file to bump.

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
→ `a2` → `a3`). The history: `git tag --list 'v*' | sort -V`.

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
2. **Summarize what changed** since the last tag — merged PRs / commits
   (`git log --oneline <last-tag>..origin/main`). This is the input to the version
   decision and the notes.
3. **GATE 1 — version choice (human decides).** Present the change summary and the
   candidate versions from "Choosing the version" above, each with its reasoning and
   your recommendation. STOP. Do not pick for them. Proceed only with an explicitly
   chosen version string.
4. **Draft notes** for the chosen version (user-facing). `gh release create` can
   `--generate-notes`, or hand-write `--notes`.
5. **GATE 2 — cut approval.** Show the final version + pre-release flag + notes. STOP
   for an explicit "yes." Cutting is outward and effectively irreversible — a published
   PyPI version can never be reused.
6. **Create the release** (creates the tag AND triggers `publish.yml`):

   ```bash
   gh release create v0.1.0aN --target main --prerelease \
     --title v0.1.0aN --generate-notes        # or --notes "..."
   ```

   Use `--prerelease` for any `a`/`b`/`rc`; omit it only for a final release.
   `--target main` ties the tag to `main`'s HEAD.

7. **GATE 3 — approve the PyPI deployment (human, in GitHub).** The publish job waits on
   the `pypi` environment reviewer. Tell the human: **Actions → the running "Publish to
   PyPI" run → Review deployments → approve `pypi`.** The assistant cannot approve it.
8. **Verify.** The workflow attaches wheel + sdist to the release and PyPI shows the
   version. Confirm `pip install --pre django-absurd==<version>` resolves (pre-releases
   need `--pre`) and the release page has the two assets.

## Guardrails

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
