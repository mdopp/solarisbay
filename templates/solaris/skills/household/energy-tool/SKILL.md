---
name: solaris-energy-tool
description: The .energy dot-command — show the household energy view (flow/trend/totals/circuits) inline.
kind: tool
scope: household
tool-id: energy
tool-label: Energie
command: .energy
tool-api-path: /api/portal/energy
tool-cell-schema: {}
version: 1.0.0
author: Solaris
license: MIT
---

# Solaris — Energie (`.energy`)

**Usage:** `.energy` zeigt die Energieübersicht (Fluss / Trend / Summen /
Stromkreise) direkt im Verlauf.

A `.tool` expressed declaratively (ADR 0011, #1006): served at `/api/defs/tool`
so it joins the tool registry. The card stays on its inline builder
(`buildEnergyCard`) — it is a display-only widget that reuses the energy page
renderer, not a create/list card, so the generic card/schema does not apply.
