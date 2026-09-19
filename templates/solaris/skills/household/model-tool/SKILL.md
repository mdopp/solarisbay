---
name: solaris-model-tool
description: The .model dot-command — welches Modell gerade läuft, und die Grafikkarte für eine gewählte Zeit von Haushalt auf Erweitert umschalten.
kind: tool
scope: household
tool-id: model
tool-label: Modell
command: .model
tool-api-path: /api/portal/models
tool-item-id-field: id
tool-actions: model.set, model.lease, model.release
tool-cell-schema: {"id": "id", "title": "title", "subtitle": "status_text", "meta": ["detail"], "badge": "badge", "actions": ["model.set"]}
tool-action-params: {"model.set": {"profile": "$profile", "hours": "$hours"}}
version: 3.0.0
author: Solaris
license: MIT
---

# Solaris — Modell (`.model`)

**Usage:** `.model` zeigt je Wahl eine Zeile — welches Modell wie lange laufen
soll — und schaltet auf Knopfdruck um.

Es gibt **zwei** Zustände, und die Zeile sagt genau einen Unterschied: gehört
die Grafikkarte dem Haus, oder ist sie für ein größeres Modell freigegeben.

| Zeile | Was sie bedeutet |
|---|---|
| **Haushalt (Normalzustand)** | die Karte gehört dem Haus: Solaris antwortet so schnell wie möglich, Sprechen läuft flott, die Suche in Notizen und Dokumenten versteht auch sinngemäß Gemeintes — hierher kommt man immer zurück |
| **Erweitert · 1 h / 4 h / bis morgen 07:00** | die Karte ist für ein größeres Modell frei. Wer sie benutzt, wählt sein Modell selbst. Das Haus antwortet weiter, nur langsamer — auch beim Sprechen — und die Suche findet solange nur Stichwörter |

Früher standen hier vier Zeilen („Haushalt + Denken", „Fokus Denken", „Fokus
Programmieren"). Sie stellten alle dieselbe Umgebung her und unterschieden sich
nur darin, welche Modelle sie erlaubten — vier Namen für zwei Zustände. Die
Modelle selbst gibt es unverändert; nur wählt sie jetzt der, der sie benutzt.

Ein Tipp nimmt die Karte **bis zu einer Zeit**, nicht „bis auf Weiteres".
Danach kommt sie von selbst zurück — niemand muss daran denken.

Auf einen Blick sagt jede Zeile dreierlei: rechts als fettes Kurzwort, **was
gerade passiert** — `läuft` / `wird geladen` / `wird freigegeben`, und gar
nichts, wenn die Zeile still ist; unter dem Titel **welches Modell, bis wann
und wer es hält** — „Qwen 27B · bis 19:42 · von pi-web", beim Haus „Gemma 4
e4b · Normalzustand". Zeilen, die nichts tun, nennen nur ihr Modell
(„größeres Modell"). Die Endzeit steht als **Uhrzeit**, nicht als Restdauer:
„noch 42 Min" muss man erst zur aktuellen Zeit dazurechnen, um zu wissen, wann
die Karte wieder frei ist.

**Der Halter steht immer dabei.** „Erweitert" ist der einzige Zustand, in dem
das Haus nicht bevorzugt bedient wird, und nicht nur die Kachel kann ihn
nehmen: ein Programmierwerkzeug darf das auch. Wer dann am Telefon steht und
einen langsamen Sprachassistenten erlebt, soll an derselben Zeile sehen, **dass**
jemand die Karte hat, **wer** das ist und **bis wann** — statt zu rätseln, ob
etwas kaputt ist.

Während eines Wechsels sprechen **zwei** Zeilen: die alte „wird freigegeben",
die neue „wird geladen".

Der Wechsel selbst geht schnell — das neue Modell lädt erst mit der **ersten
Frage** danach, und die dauert deshalb 10 bis 20 Sekunden länger als sonst.
Danach antwortet Solaris wieder im gewohnten Tempo. Es geht nichts verloren und
nichts läuft in einen Fehler: die Antwort kommt, sie lässt sich einmal bitten.

## Warum die Zeile die Wahl ist

Die Kachel löst **genau eine** Aktion je Werkzeug auf (ADR 0014): die erste
deklarierte Id, deren Parameter die Zeile füllen kann, gewinnt — eine zweite Id
aus denselben Feldern ist unerreichbar. Ein Profil mit drei Dauer-Knöpfen wäre
also ein Profil mit dreimal demselben Knopf. Darum ist jede Kombination aus
Profil und Dauer eine eigene Zeile, und die Zeile trägt beides: `profile` und
`hours`. Die Aktions-Kennung bleibt `model.set` — die App verträgt weniger
Zeilen, aber keine umbenannte Aktion (#1381). Betitelte Aktionen je Zeile
kommen mit #1381 B.

## Warum das Widget die Zeit hält, nicht das Telefon

Ein Dienst wie foundry oder pi-web hält sein eigenes Fenster und erneuert es aus
dem eigenen Prozess. Ein Telefon kann das nicht: es liegt zwei Sekunden nach dem
Tipp wieder in der Tasche. Also hält die **Engine** das Fenster (`holder:
widget`) und erneuert es bis zur gewählten Endzeit, dann gibt sie es zurück
(#1361 — ein Fenster, das niemand erneuert, endet nach der Karenz statt erst zur
Deadline). Eine automatische Verlängerung gibt es nicht: das gewählte Ende ist
das Ende.

- **Zeilen:** `GET /api/portal/models` (`tool-api-path`) — je Wahl `id`,
  `title`, `profile`, `hours`, `alias`, `state` (`active` = gerade geladen /
  `available` / `preparing` / `releasing`), das fertige Kurzwort `badge`
  („läuft" / „wird geladen" / „wird freigegeben" / leer), ein fertig
  formulierter `status_text` („Qwen 27B · bis 19:42 · von pi-web") und `detail`
  (das Modell: „größeres Modell"). `status_text` und `detail` sind **nie beide** gefüllt: die Kachel
  klebt Untertitel und Meta zu **einer** Zeile zusammen, also steht der
  Modellname genau einmal darin. Rohe Zustands- oder Zeitfelder zeigt die Kachel
  nie: sie stellt ein Feld unverändert dar, und weder „active" noch
  „1757336400" ist etwas, das jemand lesen will. Denselben Inhalt liefert
  `GET /napi/portal/models` über den Geräte-Token, den Weg, den die Kachel
  geht.
- **`hours`:** eine Zahl, Brüche eingeschlossen — „bis morgen 07:00" ist um
  Viertel nach sechs abends 12,75 und wird bei **jedem** Abruf neu gerechnet,
  damit die Zeile nicht über den Morgen hinausschießt. `0` heißt freigeben. Die
  Aktion rundet auf ganze Sekunden **auf** und deckelt bei 24 Stunden.
- **Aktionen:** `model.set` (`profile`, `hours`) ist die **einzige** Aktion der
  Kachel. `model.lease` (`model`, `until` als Dauer `1h`/`2h`/`4h` oder als
  Zielzeit `morgen 07:00` / `2026-09-09T07:00`, bis 24 h) und `model.release`
  bleiben für Chat und PWA, wo eine frei gesprochene Zeit möglich ist; im
  `tool-cell-schema` stehen sie nicht, sonst wäre `model.set` unerreichbar.
- **Umschalten:** ein anderes Profil, während eines läuft, gibt erst zurück und
  nimmt dann — dabei tragen zwei Zeilen gleichzeitig ein Kurzwort: die
  abgebende „wird freigegeben", die kommende „wird geladen". Hält ein
  **anderer** Dienst die Karte, sagt die Aktion das im Klartext und ändert
  nichts; die Zeile nennt den Halter („… · von pi-web") — in jedem Zustand,
  auch während des Ladens und des Freigebens.
- **Was ein Modus seit #1416/#1435 tut:** llama-server hält alle Presets
  gleichzeitig und lädt das an, nach dem gefragt wird. Ein Modus tauscht darum
  keinen Server mehr aus, sondern stellt die Umgebung (Sprache auf GPU oder
  CPU, Einbettungs-Server an oder aus) und legt fest, welche Presets
  währenddessen erlaubt sind; ein Proxy vor dem Router weist alles andere mit
  `409` ab. In „Haushalt" ist genau ein Preset erlaubt (`gemma-4-e4b`), in
  „Erweitert" alle — dort gibt es nichts mehr zu verweigern, der Klient wählt
  mit dem Feld `model`. Solaris selbst fragt das Preset an, das gerade geladen
  ist, statt das des Halters zu verdrängen.
- **In „Erweitert" pausiert die sinngemäße Suche:** der Einbettungs-Server
  passt nicht neben ein großes Modell auf die Karte (gemessen, #1434), also
  findet die Suche in Notizen und Dokumenten solange nur Stichwörter. Das ist
  Teil der Entscheidung, kein Ausfall — „Haushalt" bringt sie zurück.
- **Nachdenken kommt auf Zuruf, nicht auf Vorrat** (Operator 13.9.): auch in
  „Erweitert" antwortet Solaris normal. Erst wenn die Frage darum bittet —
  „denk mal nach", „überleg", „gründlich", „Schritt für Schritt", „rechne",
  „Logik" — überlegt das Modell ausführlich
  (`chat_template_kwargs.enable_thinking`). So kostet „mach das Licht aus" auch
  im offenen Fenster keine Denkzeit.
