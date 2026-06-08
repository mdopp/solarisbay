# Solilos

**Solilos** is a household AI assistant that ServiceBay can deploy as
one click and Hermes can consume as a skill pack independently.

## What's in this repo

- **Hermes skills** (`templates/solilos/skills/household/`) — household
  procedures Hermes loads at runtime: `audit-query`, `debug-set`,
  `chat-compactor`, `dynamic-skills`, `media-ingestion-multimodal`,
  `problem-summarizer`, `room-enrollment`, `status`.
- **Operator skill pack** (`templates/solilos/skills/admin-soul/`) — the
  admin-facing soul Hermes loads alongside the household skills:
  `admin-diagnose` (drill service → container → logs), `admin-logs`
  (targeted log deep-dive), `admin-act` (lifecycle + mutate actions via
  the `servicebay_admin` MCP), and a `SOUL.md` operator disposition.
- **ServiceBay templates** (`templates/{ollama,solilos}/`) — two services
  (#271): `ollama` (the local LLM engine) and the merged `solilos`
  service — one Pod holding the Hermes runtime + chat UI + household glue
  (skills, voice bridge, DB init) + operator soul, one tile.
- **Solilos stack** (`stacks/solbay/stack.yml`) — bundles the two
  templates so a ServiceBay operator can install with one click.
- **Voice gatekeeper image source** (`voice-gatekeeper/`) — Python
  Wyoming-protocol bridge from HA Voice PE satellites to Hermes,
  built into `ghcr.io/mdopp/solilos-gatekeeper:latest` and referenced
  by `solbay`'s pod yaml.
- **Database image source** (`database/`) — Alembic schema-init
  container that runs `alembic upgrade head` against the Solilos pod's
  local SQLite (`solilos.db`) on every pod start. Built into
  `ghcr.io/mdopp/solilos-schema-init:latest`.
- **Chat proxy image source** (`solilos-chat/`) — a small, stateless
  aiohttp proxy serving a static chat page over Hermes' native session
  API, built into `ghcr.io/mdopp/solilos-chat:latest` and deployed as the
  `chat` container of the `solilos` service at `chat.<publicDomain>`.

## Two install paths

**ServiceBay route (recommended for households running ServiceBay):**

1. ServiceBay → Settings → Registries → Add `mdopp/solbay`
   (`https://github.com/mdopp/solbay.git`).
2. After save, the `ollama` + `solilos` templates and the `solbay` stack
   appear in the wizard.
3. Install the stack. The `solilos` template's `post-deploy.py`
   sequences Hermes config + SOUL.md, skill delivery to Hermes (via
   ServiceBay's asset-transport mechanism, [mdopp/servicebay#1156]), DB
   schema init, MCP wiring (household + operator), and voice-bridge
   container startup.

**Standalone Hermes route (Hermes outside ServiceBay):**

1. In the Hermes dashboard: Skills → Install from Git URL → paste
   `mdopp/solbay` (or the full HTTPS URL).
2. Hermes clones to `~/.hermes/plugins/solbay/`, reads `plugin.yaml`,
   and runs `__init__.py:on_load(ctx)` to register the household skills.
3. The voice bridge and DB schema-init are NOT installed via this
   route — they're ServiceBay-deployed containers. A standalone
   Hermes that wants the gatekeeper would need to run
   `ghcr.io/mdopp/solilos-gatekeeper:latest` on its own.

## Repository layout

```
solbay/
├── README.md                       # this file
├── plugin.yaml                     # Hermes plugin manifest
├── __init__.py                     # Hermes plugin entrypoint
├── templates/                       # ServiceBay templates
│   ├── ollama/                       # the local LLM engine — its own service
│   └── solilos/                      # the merged assistant service (#271)
│       ├── template.yml             # one Pod: hermes + config-agent + chat
│       │                            #   + gatekeeper + admin-soul containers
│       ├── post-deploy.py           # one ordered setup script
│       ├── variables.json
│       ├── SOUL.md                  # Sol's durable identity
│       └── skills/
│           ├── household/           # household Hermes skill pack
│           │   ├── audit-query/
│           │   ├── debug-set/
│           │   ├── dynamic-skills/
│           │   ├── media-ingestion-multimodal/
│           │   └── status/
│           └── admin-soul/          # operator skill pack + SOUL.md
├── voice-gatekeeper/               # Docker image source
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── src/
│   └── tests/
├── database/                       # Docker image source (alembic)
│   ├── Dockerfile
│   ├── alembic.ini
│   └── migrations/
├── solilos-chat/                   # Docker image source (chat proxy)
│   ├── Dockerfile
│   ├── pyproject.toml
│   ├── src/
│   └── tests/
├── stacks/
│   └── solbay/
│       ├── stack.yml               # templates: [ollama, solilos]
│       └── README.md
└── .github/workflows/
    └── build-images.yml            # publishes the GHCR images
```

## Image build

`.github/workflows/build-images.yml` publishes:

- `ghcr.io/mdopp/solilos-gatekeeper:latest` (and version tags) from
  `voice-gatekeeper/Dockerfile`.
- `ghcr.io/mdopp/solilos-schema-init:latest` (and version tags) from
  `database/Dockerfile`.

Triggered on push to `main` and tags `v*`.

## License

MIT. See [LICENSE](LICENSE).
