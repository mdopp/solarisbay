# OSCAR

**OSCAR** is a household AI assistant that ServiceBay can deploy as
one click and Hermes can consume as a skill pack independently.

## What's in this repo

- **Hermes skills** (`templates/oscar-household/skills/`) — five
  procedures Hermes loads at runtime: `audit-query`, `debug-set`,
  `dynamic-skills`, `media-ingestion-multimodal`, `status`.
- **ServiceBay templates** (`templates/{ollama,hermes,hermes-webui,oscar-household}/`)
  — the four-pod deployment recipe that wires local LLM, agent
  runtime, chat UI, and the OSCAR-specific glue (skill mount, voice
  bridge, DB init).
- **OSCAR stack** (`stacks/oscar/stack.yml`) — bundles the four
  templates so a ServiceBay operator can install with one click.
- **Voice gatekeeper image source** (`voice-gatekeeper/`) — Python
  Wyoming-protocol bridge from HA Voice PE satellites to Hermes,
  built into `ghcr.io/mdopp/oscar-gatekeeper:latest` and referenced
  by `oscar-household`'s pod yaml.
- **Database image source** (`database/`) — Alembic schema-init
  container that runs `alembic upgrade head` against the OSCAR pod's
  local SQLite (`oscar.db`) on every pod start. Built into
  `ghcr.io/mdopp/oscar-household-init:latest`.

## Two install paths

**ServiceBay route (recommended for households running ServiceBay):**

1. ServiceBay → Settings → Registries → Add `mdopp/oscar`
   (`https://github.com/mdopp/oscar.git`).
2. After save, the four OSCAR templates and the `oscar` stack appear
   in the wizard.
3. Install the stack. The `oscar-household` template's `post-deploy.py`
   handles skill delivery to Hermes (via ServiceBay's
   asset-transport mechanism, [mdopp/servicebay#1156]), DB schema
   init, MCP wiring, and voice-bridge container startup.

**Standalone Hermes route (Hermes outside ServiceBay):**

1. In the Hermes dashboard: Skills → Install from Git URL → paste
   `mdopp/oscar` (or the full HTTPS URL).
2. Hermes clones to `~/.hermes/plugins/oscar/`, reads `plugin.yaml`,
   and runs `__init__.py:on_load(ctx)` to register the five skills.
3. The voice bridge and DB schema-init are NOT installed via this
   route — they're ServiceBay-deployed containers. A standalone
   Hermes that wants the gatekeeper would need to run
   `ghcr.io/mdopp/oscar-gatekeeper:latest` on its own.

## Repository layout

```
oscar/
├── README.md                       # this file
├── plugin.yaml                     # Hermes plugin manifest
├── __init__.py                     # Hermes plugin entrypoint
├── templates/                       # ServiceBay templates (legacy layout)
│   ├── ollama/
│   ├── hermes/
│   ├── hermes-webui/
│   └── oscar-household/
│       ├── template.yml
│       ├── post-deploy.py
│       ├── variables.json
│       ├── README.md
│       ├── CHANGELOG.md
│       └── skills/                 # Hermes skill pack
│           ├── audit-query/
│           ├── debug-set/
│           ├── dynamic-skills/
│           ├── media-ingestion-multimodal/
│           └── status/
├── voice-gatekeeper/               # Docker image source
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── src/
│   └── tests/
├── database/                       # Docker image source (alembic)
│   ├── Dockerfile
│   ├── alembic.ini
│   └── migrations/
├── stacks/
│   └── oscar/
│       ├── stack.yml               # templates: [ollama, hermes, hermes-webui, oscar-household]
│       └── README.md
└── .github/workflows/
    └── build-images.yml            # publishes the two GHCR images
```

## Image build

`.github/workflows/build-images.yml` publishes:

- `ghcr.io/mdopp/oscar-gatekeeper:latest` (and version tags) from
  `voice-gatekeeper/Dockerfile`.
- `ghcr.io/mdopp/oscar-household-init:latest` (and version tags) from
  `database/Dockerfile`.

Triggered on push to `main` and tags `v*`.

## License

MIT. See [LICENSE](LICENSE).
