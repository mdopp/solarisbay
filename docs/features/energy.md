# Energy page

`/p/energy` is the home-energy dashboard: a live PV / Haus / Netz / Akku flow
with correct directions, lifetime totals, per-circuit power, and 24h/7d trend
charts. Behind the same Authelia SSO as everything else.

<!-- screenshot: /p/energy -->

## What it does

- **Jetzt — the live flow.** Four current-power (W) tiles with a sign-corrected
  direction so supply and draw read the right way:
  - **PV** — currently generated solar (supply).
  - **Haus** — current house consumption (draw).
  - **Netz** — grid: `+W` = Bezug (draw/red), `−W` = Einspeisung (supply/green).
  - **Akku** — battery: `−W` = discharging (supply/green), `+W` = charging
    (draw/red).
- **Energie gesamt** — lifetime kWh counters (Einspeisung, Netzbezug, …), kept
  strictly separate from the flow so the kWh totals never leak into the live W
  picture.
- **Per-circuit list** — the remaining power sensors as an alphabetically
  sorted circuit breakdown.
- **Trend charts** — 24h and 7d power history for the flow sensors.

## How to use it

Open `/p/energy` (bookmarkable; also reachable from the desktop rail and the
mobile nav). Toggle the trend chart between **24h** and **7d**. Numbers are
rounded to ≤1 decimal place.

## How it works (brief)

- `GET /api/portal/energy` (`server.py::portal_energy`) does one HA `/api/states`
  read and buckets sensors into `flow` / `totals` / `circuits`
  (`engine/tools/ha.py::fetch_energy`, `_bucket_energy_states`). The sign
  convention lives in the `_ENERGY_FLOW` table (`sense` = supply / draw / grid /
  battery).
- `GET /api/portal/energy/history?range=24h|7d`
  (`server.py::portal_energy_history` → `fetch_energy_history`) returns the
  downsampled W time-series for the trend chart (168h for `7d`, else 24h).
- The page is served by the SPA shell at `/p/{type}` and rendered client-side
  (`renderEnergyPage`) — the same read-only, Authelia-gated aggregator pattern
  as the concept pages.

## Config / env

No page-specific config. It reads the household's Home Assistant:

| env | purpose |
|---|---|
| `HASS_URL` / `HASS_TOKEN` | Home Assistant API + token |

Returns `503 ha_unconfigured` when HA is not configured, `502 ha_unavailable`
when HA cannot be reached. The energy sensors themselves are whatever the
household exposes in HA (PV / grid / battery / house power + per-circuit
sensors).

See [`solaris-architecture.md`](../../solaris-architecture.md) for how the
engine talks to Home Assistant.
