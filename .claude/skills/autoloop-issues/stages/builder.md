# Stage: Builder — mdopp/solbay

You are the **Builder** sub-agent. You run in fresh context, take **one unit** from the shared queue (or seal the batch), and return one line. You own implement → fast-gate → commit → (at the batch boundary) seal → push → CI → merge.

Read first: the orchestrator's shared rules in `.claude/skills/autoloop-issues/SKILL.md` and the project `CLAUDE.md`. Shared queue: `.claude/state/work-queue.json`. The orchestrator's context line gives **mode** (`build`/`seal`), and for `build` the **unit id**, **gate**, and **security** flag.

## The gate split — the point of this design

| | When | What runs |
|---|---|---|
| **Fast gate** | after **every** unit (per-issue) | `ruff check . && ruff format --check .`; **if the unit touched `voice-gatekeeper/**`** also `cd voice-gatekeeper && pytest -q`; **if it touched `templates/**` or `stacks/**`** also hand-validate (YAML/JSON parses; changed `SKILL.md` frontmatter `name`/`description`/`version` valid + no leftover `TODO (rewrite)` if the issue was to finish it; `template.yml` mount names match volumes and declared ports don't collide). |
| **Full gate** | once, at the **batch seal** | `ruff check . && ruff format --check . && cd voice-gatekeeper && pip install -e '.[test]' && pytest -q --cov=gatekeeper --cov-report=xml && cd .. && python scripts/check-diff-coverage.py` → push → CI (image paths only). |

Solilos has no arch-ratchet (flat repo) — the structural check the generic template runs per issue is, here, ruff + the template/skill hand-validation. The `voice-gatekeeper` pytest suite is small enough to serve as the changed-tests run when that package is touched; **don't** run it for template/skill/docs-only units (it isn't affected). The full suite + diff-coverage is the safety net at the seal — and since you accumulate on one branch in one session, a red full-run is a cheap in-context bisect.

`scripts/check-diff-coverage.py` fails the seal if changed lines under `voice-gatekeeper/src/gatekeeper/` fall below the floor; it's a no-op when no such lines changed. Don't loosen the floor to make it pass — add the test.

---

## Mode: `build` — implement one unit onto the batch branch

### 1. Get on the batch branch
- `batch` null → create it: `git checkout main && git pull --ff-only && git checkout -b batch/$(date +%Y-%m-%d)<letter>`; set `batch={branch, units:[], count:0, sealed:false}`.
- Else → `git checkout <batch.branch>` (it persists across firings). **If the branch is behind `main`, `git rebase origin/main` immediately** — an out-of-date batch (e.g. created before a skill change) leaves the on-disk `stages/` playbooks stale for the next stage dispatch. Conflict-free when the batch's filesets are disjoint from what moved on `main`.

Set the unit `status:"in_progress"`, `in_progress` to the unit id.

**Build-ahead is safe during a background Verify.** A prior batch may be in `verify_state` `verifying`/`owed` (being checked on the box) while you build the next one — expected. Building writes neither `main` nor the box, so it overlaps the background Verify safely. You only ever build here; **sealing** is what waits for the verify to clear (the orchestrator gates that, not you).

### 2. Read the unit
- **Cluster** → read *every* member issue + its referenced files; implement all members as one coherent themed change (organize the diff by theme, not by issue).
- **Issue** → read the body, referenced files, ~50 lines around any line ref. The gatekeeper package is `voice-gatekeeper/src/gatekeeper/` (entrypoint `__main__.py`).
- **lint-sweep** → see §Lint-sweep.
- **Ambiguous** (planner missed it) → don't guess: post the specific question (comment hygiene), move the unit to `needs_refinement[]`, revert partial work, return.

### 3. Implement — scope discipline
Smallest change that satisfies `acceptance`. **No** drive-by refactors / new abstractions / "improve while I'm here". `[Refactor]` units stay within the named module; a needed neighbouring change is a separate unit. Comments only for a non-obvious *why* (per `CLAUDE.md`). When a bug/feature touches `voice-gatekeeper/src/gatekeeper/`, add or extend a test under `voice-gatekeeper/tests/` so the change is covered (and the diff-coverage floor is met).

### 4. Fast gate (per unit)
Run the fast gate (table above) for the paths this unit touched. The pytest step reads the working tree, so run it **before** committing if you want it to see uncommitted code (it picks up installed sources — commit then run is fine for this package). A real failure → fix the root cause; never mock around or skip it. Lint must stay clean.

### 5. Commit to the batch branch (no push)
- Conventional Commits; scope mirrors the path: `fix(gatekeeper):`, `feat(skill):`, `fix(template):`, `feat(solbay):`, `chore(db):`, `docs:`. **No parens beyond the conventional `(scope)`** (a stray paren can make release tooling run green but cut nothing).
- Body ends with `Closes #<N>` — **one line per member issue** for a cluster.
- **No push, no PR, no CI.** Update the queue: unit `status:"built"`, append member issues to `batch.units`, bump `batch.count` by the issue count, clear `in_progress`. Return.

### `security: true` unit — pre-merge draft gate (Solilos's deliberate choice)
Solilos touches biometric speaker-ID, per-resident privacy, and gateway/HA credentials, so security/privacy changes get **human eyes before they ship** (the pre-merge opt-in, not post-deploy review). Build it on its **own** branch off `main` (not the shared batch branch — it must not ride a batch that auto-merges):
```bash
git checkout main && git pull --ff-only && git checkout -b sec/issue-<N>-<slug>
```
implement → fast gate → commit `Closes #<N>` → push → `gh pr create --draft` with a full body (What/Why/Risk/Rollback/Verification). Then label the issue `autoloop-open`, add `{issue, pr, flag:"security", since:<now>}` to `review[]`, and **return — do not merge.** The loop never merges a draft; a human reviews and merges it. (More than 3 such drafts accumulating without review is orchestrator hard-exit #2.)

### Lint-sweep unit
Implement the one file/rule named. Size guard: ≤2 source files (+ tests), ≤120 LOC net, one warning class or one file. If even a bite-size fix won't fit → mark in `blocked[]` and return. Lint-sweep commits ride the batch branch (no `Closes #`); record `{file, rule}` in `lint_sweep[]` at seal.

---

## Mode: `seal` — ship the accumulated batch (expensive pipeline, once)

Precondition (re-assert): (`batch.count >= 8` **or** `queue[]` has no `planned` unit) **and** `verify_state.status` is clear (`green`/`null`, not `owed`/`verifying`/`red`). Mid-batch, or a prior batch still in verify → do nothing, return "not ready to seal".

### 1. Full gate
```bash
git checkout <batch.branch> && git rebase origin/main
ruff check . && ruff format --check .
cd voice-gatekeeper && pip install -e '.[test]' && pytest -q --cov=gatekeeper --cov-report=xml && cd ..
python scripts/check-diff-coverage.py
```
A full-suite/coverage failure the fast gate missed → identify the culprit commit (atomic `Closes #N` — cheap in-context bisect), fix, re-run. Push only when green: `git push -u origin <batch.branch>`.

### 2. One PR for the whole batch
`gh pr create` with a real body (no `--fill`): **What** (the batch's themes), **Why** (one `Closes #<N>` per issue), **Risk**, **Rollback**, **Verification** checklist (full gates + real-box `/verify` if path-mandated).

### 3. Merge gate (`main` is unprotected → `--auto` no-ops; gate manually)
**CI applies only to the paths in `ci.yml`** (`**/*.py`, `**/pyproject.toml`, `ruff.toml`, `.pre-commit-config.yaml`, `ci.yml`) — a **template-only / skill-only / docs-only** batch triggers **no CI**, so for those the gate is the full gate above + real-box `/verify`. If the batch touched any CI path: `gh pr checks <PR#> --watch`. Green → `gh pr merge <PR#> --merge --delete-branch`, then `git checkout main && git pull --ff-only`. Red twice on the same SHA → post the failing-job link, leave open, return (orchestrator hard-exit #1).

### 4. Hand off to Verify
If **any** merged file is path-mandated (list below), set `verify_state={sha:"<merge SHA>", status:"owed", detail:"<which paths + a concrete /verify checklist>", since:<now>}` — the orchestrator launches Verify **in the background** next firing (it flips `owed`→`verifying`); the release/tag stays blocked until green. Move the batch's units → `completed[]`, mark `lint_sweep[]`, and **reset `batch` to `null`**. You only ever set `verify_state` to `owed`; `verifying`/`green`/`red` are written by the orchestrator (from the background agent's result file), never by you.

### Path-mandated paths (trigger `verify_state=owed`)
```
templates/**          (any template.yml, variables.json, post-deploy.py, or skills/)
stacks/**
voice-gatekeeper/**
database/**
plugin.yaml
__init__.py
```
Rationale: these are verified by deploying the changed artifact through ServiceBay onto the box and checking the Solilos runtime — not by CI alone (CI only builds images). For `voice-gatekeeper`/`database` the live image only exists on the box **after** merge publishes it to GHCR, so a path-mandated image change is verified post-merge by the background Verify (`stages/verify.md`).

## Return
- build: `Builder: built gatekeeper (#92,#94) onto batch/2026-..a, fast gate green, count 2/8.`
- seal: `Builder: sealed batch → PR #45 merged (closes #92 #94 #101); verify_state=owed (templates/ + voice-gatekeeper/).`
- security: `Builder: drafted #88 → PR #46 (draft), added to review[]; NOT merged.`

## Never
- Run the full suite per unit (seal's job) — fast gate only mid-batch.
- Push / open a PR / trigger CI / merge a normal unit while mid-batch.
- Auto-merge a `security:true` unit — draft + `review[]`, human merges.
- Loosen the diff-coverage floor or skip a test to go green — fix the root cause / add the test.
- Guess past an ambiguous issue — bounce to `needs_refinement[]`.
- Bump versions in `pyproject.toml` or push `v*` tags — releases are the user's call.
