# Hermes — template changelog

## v3 — #940

OSCAR's dynamic skill compiler now drafts pending skills into a separate
host directory that Hermes' loader does not scan. The Hermes pod gets a
new bind mount, `{{DATA_DIR}}/oscar-household/skills-pending` at
`/opt/data/skills-pending`, alongside the existing
`/opt/data/skills/oscar` mount.

Operator impact: none on redeploy. The host directory is auto-created
(`type: DirectoryOrCreate`) and starts empty. The ServiceBay dashboard
gains a "Pending OSCAR skills" panel under **Settings → Integrations**
where the admin promotes or rejects drafts before they go live.

Before v3 the OSCAR `dynamic-skills` skill instructed Hermes to write
new skills directly under `/opt/data/skills/oscar/...` and to call
`restart_service hermes`, which auto-activated agent-generated code
with no human review — a prompt-injection risk. v3 retires that path.

## v2 — #829

The Hermes web dashboard is now exposed as a real service.

Before v2 the dashboard was nominally opt-in via `HERMES_DASHBOARD_PORT`
and a set of `HERMES_DASHBOARD*` env vars — but `hermes gateway run`
never reads those env vars, so the dashboard never actually started. It
also had no subdomain, so no NPM proxy host, no portal card and no URL:
an operator could not reach it at all.

v2:

- A second container, `hermes-dashboard`, runs `hermes dashboard` — the
  dashboard's real launch path. It shares the Hermes home volume
  (`/opt/data`) with the gateway container; upstream supports running
  the gateway and dashboard side by side.
- `HERMES_DASHBOARD_PORT` now defaults to `9119` (the dashboard is on by
  default) and the dead `HERMES_DASHBOARD*` env block is removed.
- A new `HERMES_SUBDOMAIN` variable (default `hermes`) gives the
  dashboard an internal-exposure NPM proxy host behind Authelia
  forward-auth — which in turn yields a portal card and a URL.

Operator impact: after redeploy the dashboard is reachable at
`https://hermes.<PUBLIC_DOMAIN>` (LAN-only, SSO-gated) and appears on the
ServiceBay portal. No data migration is needed — the dashboard reads the
same Hermes home the gateway already uses.
