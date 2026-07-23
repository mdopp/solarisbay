---
name: solaris-home-tool
description: The .home dot-command — filter smart-home devices by name/room and control them as live widget cards.
kind: tool
scope: household
tool-id: home
tool-label: Gerät
command: .home
tool-api-path: /api/portal/start/addable
tool-cell-schema: {}
version: 1.0.0
author: Solaris
license: MIT
---

# Solaris — Geräte (`.home`)

**Usage:** `.home <Name|Raum|Text>` filtert die Geräte live (Favoriten zuerst)
und rendert die Treffer als steuerbare Widget-Karten — Zustandsfarbe,
Live-Update, ★-Favorit.

A `.tool` expressed declaratively (ADR 0011, #1006): served at `/api/defs/tool`
so it joins the tool registry. The card stays on its inline builder
(`buildHomeCard`) — this is a WIDGET card (`hc-grid` device controls with live
SSE update, state colour, favourite toggle), NOT a list row, so the generic
list card/schema does not apply. The controls post to `/api/ha/call` and
`/api/favorites` directly, so this tool declares no card action.
