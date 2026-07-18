# autoloop-issues — how to run (mdopp/solarisbay)

`autoloop-issues` is the **orchestrator** of a multi-agent pipeline. It spawns a fresh sub-agent per stage (Planner → Builder → Verify), coordinated through `.claude/state/work-queue.json`, so the loop session stays clean. Solaris is a bundle of ServiceBay artifacts (the `ollama` + `solaris` templates, the `solaris-chat` Solaris Engine and its skill packs, the `voice-gatekeeper` Python service, the `database` schema-init) that runs **on ServiceBay** at `<SERVICEBAY_BOX>`.

## Self-paced loop (recommended)
```
/loop /autoloop-issues
```
`/loop` re-fires the orchestrator on its own cadence. The work queue persists progress between firings; each stage runs in its own sub-agent context.

## What each stage does
- **Planner** (`stages/planner.md`) — grooms/clusters open issues into queue units, decomposes epics, routes ServiceBay-platform bugs upstream, runs the box e2e smoke when the queue is dry, and **bounces every underspecified issue to `needs_refinement[]` with a specific question.**
- **Builder** (`stages/builder.md`) — implements one unit onto the persistent `batch/<id>` branch with **fast gates**; runs the **full** suite + diff-coverage + CI and merges at the batch boundary. Security/privacy units open as a **draft** PR and are never auto-merged.
- **Verify** (`stages/verify.md`) — batched real-box `/verify` for path-mandated changes, deployed through ServiceBay onto `<SERVICEBAY_BOX>`; gates the release/tag. Runs in the background so the builder keeps building the next batch.

## Where human attention goes
1. Drain `needs_refinement[]` — sharpen ambiguous issues / answer the planner's questions.
2. Review `review[]` — the security/privacy **draft** PRs (these never merge on their own).
Everything else runs without you.

## Releases
Solaris has **no release-please** — a release is cut by pushing a `v*` tag (triggers `build-images.yml`). The loop never tags or bumps versions; it logs a suggestion in `release_warnings[]` when a green-verified batch is worth releasing. Cutting the tag is your call.

## Cross-repo
Solaris runs **on** ServiceBay. Platform bugs found during `/verify` or the e2e smoke are filed in **mdopp/servicebay** (issue, not PR) and tracked in `upstream_waits[]`; the local issue is marked blocked and unblocked when the upstream fix closes.

## Stop / reset
Interrupt the session (the queue persists; mid-batch builds resume on the same branch). Reset: `rm .claude/state/work-queue.json` (recreated from `work-queue-template.json`).

## Tuning models
The orchestrator sets a model per stage (table in `SKILL.md` Step 2): builder `opus` for real code / `haiku` for lint sweeps, planner & verify `sonnet`. Don't downgrade the builder for real code.

## When NOT to run
- Another session is editing files here (orchestrator exits on a dirty tree).
- You haven't reviewed any of the first autonomous PRs yet.
- You're mid-incident on the ServiceBay box.
