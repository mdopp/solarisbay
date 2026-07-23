---
name: solaris-task-tool
description: The .task / .todo dot-command — create a task and live-filter the Aufgaben list, tap to complete/edit. The reference kind:tool plugin (ADR 0011).
kind: tool
scope: household
tool-id: task
tool-label: Aufgabe
command: .task
tool-api-path: /api/portal/tasks?done=1
tool-actions: task.set_status, task.add, task.update
tool-cell-schema: {"title": "title", "meta": ["due"]}
version: 1.0.0
author: Solaris
license: MIT
---

# Solaris — Aufgaben (`.task`)

**Usage:** `.task <Text>` legt eine Aufgabe an *und* filtert die bestehende
Aufgabenliste live nach demselben Text (`.task list` zeigt alle). Ein Tipp auf
das Kästchen erledigt sie, ein Tipp auf den Titel öffnet die Bearbeitung — das
Anlegen·Finden·Bearbeiten-Muster (#967).

The first `.tool` expressed declaratively (ADR 0011, #1004): the server
auto-registers the `tool-actions` (`task.set_status`, `task.add`,
`task.update`) from this def instead of hand-wiring them, and serves the whole
surface at `/api/defs/tool`. The client keeps its existing inline card until the
tool-registry dispatch lands (#1005); this def does not change its behaviour.

- **List/search:** `GET /api/portal/tasks?done=1` (`tool-api-path`) — open plus
  the last week's resolved tasks.
- **Cell:** the shared `renderListCell` renders `title` with `due` as the meta
  line (`tool-cell-schema`), the same row the Aufgaben doorway page uses.
