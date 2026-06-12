# Solilos — house rules

Solilos is a household AI assistant packaged as ServiceBay artifacts: ServiceBay
Pod-YAML templates, the `solilos-chat` Sol Engine (agent core + chat surface,
the code-heavy part), its skill packs (markdown prompt content), the
`voice-gatekeeper` Wyoming bridge, the `database` alembic schema-init sidecar,
and the bundled `solbay` stack. It runs **on ServiceBay**. See `README.md` for
the full layout.

These rules apply to every session, human or agent.

## Commits

- **Conventional Commits**: `type(scope): subject` — `feat`/`fix`/`refactor`/
  `chore`/`docs`/`test`. Scope mirrors the path: `fix(gatekeeper):`,
  `feat(skill):`, `fix(template):`, `feat(solbay):`, `chore(db):`,
  `docs:`.
- **No parentheses in the subject** beyond the conventional `(scope)`. A stray
  `(...)` token can make release tooling run green but cut no release — keep
  subjects paren-free.

## Scope discipline

- Smallest change that solves the task. A bug fix doesn't need surrounding
  cleanup; a one-shot doesn't need a helper.
- Three similar lines beat a premature abstraction. No speculative
  error-handling, fallbacks, or feature flags for cases that can't happen.

## Comments

- Default to none. Add one only for a non-obvious *why* (a hidden constraint, a
  workaround, a surprising invariant). Don't narrate *what* the code does.

## Verify in the real environment

- Type-check + tests prove code correctness, not **feature** correctness.
- Template (`templates/**`), skill (`**/skills/*/SKILL.md`), `solilos-chat`,
  `voice-gatekeeper`, `database`/migration and `stacks/**` changes are verified
  by **deploying the changed artifact through ServiceBay onto the box** and
  checking the Solilos runtime — not by CI alone (CI only builds images). If
  you can't verify on the box, say so explicitly.

## Releases

- Releases are automated via **release-please**. It maintains a release PR that
  bumps the version + `CHANGELOG.md` from the conventional commits on `main`.
  **Merging that release PR** cuts the `vX.Y.Z` tag + GitHub release, which
  triggers `build-images.yml` to publish `solilos-gatekeeper`,
  `solilos-gatekeeper-ml`, `solilos-chat`, and `schema-init` to GHCR.
- Conventional Commits + paren-free subjects (above) are load-bearing:
  release-please derives the version bump and CHANGELOG from them.
- **Don't** hand-bump versions in `pyproject.toml` or create/push tags by hand —
  let release-please own that. Cutting a release = merging its release PR, which
  is a human/explicit-ask decision.

## Never

- `--no-verify` / skip hooks — fix the underlying failure instead.
- Loosen the lint baseline or a CI check to make it pass.

## Local gates

- Install hooks once: `pip install pre-commit && pre-commit install`.
- Lint: `ruff check . && ruff format --check .`
- Tests: `cd voice-gatekeeper && pip install -e '.[test]' && pytest -q`
- CI (`ci.yml`) runs lint + pytest on Python changes; image builds run in
  `build-images.yml`.

## Issues

- Capture **symptom + repro + starting-point files** — not a fix-plan or
  acceptance bullets. The fix is decided in the PR. Symptom-style issues age
  well; fix-plan-heavy bodies rot. See `.github/ISSUE_TEMPLATE/bug_report.md`.
