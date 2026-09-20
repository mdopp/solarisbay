# pi-web — the coding-agent web UI on the box

[PI WEB](https://pi-web.dev/) (`@jmfederico/pi-web`) is a browser surface for the
[Pi Coding Agent](https://github.com/earendil-works/pi/tree/main/packages/coding-agent):
agent sessions that keep running in real repositories on this box after the
browser disconnects. This template deploys it at `pi.<publicDomain>` behind
Authelia and wires it to the box's own model server.

## What it is made of

| | |
|---|---|
| Image | `ghcr.io/mdopp/solaris-pi-web:latest`, built from `pi-web/Dockerfile` in this repo |
| Containers | `sessiond` (owns the sessions, terminals and the model runtime), `web` (HTTP/WebSocket), `model-gate` (the door to the model, #1435), `autoloop` (works labelled tickets) |
| Network | isolated netns, `hostPort` 8504 |
| Route | `pi.<publicDomain>`, internal exposure, Authelia forward-auth `one_factor` |
| Model | llama-server on this box, via the `model-gate` container; the list is the Pi extension `solaris-llama` (#1435) |
| Volumes | `{{DATA_DIR}}/pi-web/data` → `/data`, `{{DATA_DIR}}/pi-web/workspace` → `/workspace`, ServiceBays Agenten-Paket → `/opt/servicebay` (nur lesend) |

### Why the image is ours

Upstream publishes an **npm package only**. Its `docker/` directory is a
Dockerfile you are expected to build yourself, and no OCI image is pushed to
GHCR or Docker Hub. So this repo builds one, pinned to a specific
`@jmfederico/pi-web` version, in the same `build-images.yml` matrix as the
engine and the gatekeeper. Bumping PI WEB is a `PI_WEB_VERSION` change in
`pi-web/Dockerfile`, in its own commit — never a silent `latest` drift under a
running session.

### Why it is not on host networking

ADR 0007 Decision 2's carve-out list is **closed**; a new service does not join
it by arguing its case, and Decision 3 says explicitly that needing to reach a
loopback-bound sibling is not a reason either. So the pod runs in its own
network namespace, publishes 8504 as a `hostPort`, and addresses llama-server
as `http://host.containers.internal:11435/v1` — the hop the `model-gate`
container makes on the sessions' behalf. That path answers because the
sibling half already landed in #1344: the process on `LLAMA_PORT` binds
`0.0.0.0`, so loopback — where the pasta-proxied pod path arrives — reaches it.
Since #1416 that process is the llama template's mode policy proxy and
llama-server itself sits behind it on loopback 11434. That port's `LAN` side is
open on purpose since #1420 (the operator wants llama to serve its models in
the home network without a login); it says nothing about this one.

`PI_WEB_PORT` carries `blockLanAccess: true`, for a reason that does not apply
to llama: PI WEB has no login of its own (upstream states
plainly that it assumes trusted users and is not a sandbox), so a published
port reachable from the WLAN would be a way around Authelia. nginx reaches it
over loopback; a laptop on the WLAN does not.

The service never talks to `llama.<publicDomain>`. That route exists for a
human with a browser and is Authelia-gated; a service on the same box that used
it would meet a login page.

## How the model is configured

PI WEB has no LLM settings of its own — the model runtime belongs to the Pi
Coding Agent. Two halves configure it, and since #1435 they are deliberately
different halves: **the connection** comes from a file, **the model list** comes
from a Pi extension that fetches it.

### The connection: `models.json`

The post-deploy writes
`{{DATA_DIR}}/pi-web/data/pi-agent/models.json`, and it declares nothing but
where the provider is:

```json
{
  "providers": {
    "solaris-llama": {
      "baseUrl": "http://127.0.0.1:11437/v1",
      "api": "openai-completions",
      "apiKey": "llama",
      "compat": {
        "supportsDeveloperRole": false,
        "supportsReasoningEffort": false,
        "thinkingFormat": "chat-template"
      }
    }
  }
}
```

- **`api: "openai-completions"`, not Pi's built-in `llama.cpp` provider.** The
  built-in one discovers models in a directory of its own; this box runs the
  router off a pinned presets file.
- **`apiKey` is a placeholder.** llama-server ships no authentication and there
  is no key to hold; Pi hides models whose provider has no auth configured at
  all, so a dummy value is what makes them appear. Upstream's own Ollama
  example does the same.
- **`baseUrl` is the pod's own gate**, not the host's `LLAMA_PORT`. This pod has
  its own network namespace; the containers of a pod share it, so `127.0.0.1`
  here is the `model-gate` container beside the sessions.
- **There is no `models` array, on purpose.** This file is the seed Pi needs
  before any extension has run — **it is not the source of truth for the model
  list.** If the extension below fails to load, PI WEB shows this provider with
  no models at all rather than quietly serving an outdated list, and the
  sessiond log names the extension that failed.

### The list: the `solaris-llama` extension

`pi-web/extensions/solaris-llama.js` registers the **same provider** a second
time, in Pi's **native** form (`pi.registerProvider(provider)`), with a real
`fetchModels` that asks the model gate. Pi's own background catalog refresh —
15 s after the session daemon starts, then hourly, catalogs treated as fresh for
four hours — then keeps the model picker current. **No script, no systemd timer,
no recurring restart.**

Why it had to be the native form: in `@earendil-works/pi-ai/dist/models.js`,
`createProvider` sets `refreshModels: fetchModels ? … : undefined`, and a
provider that arrives in **config** form — which is what a `models.json` entry
becomes — brings no `fetchModels`. The hourly refresh existed all along and had
nothing to call for us. That is why `models.json`, written on 13.09. before
#1431 made all four presets visible, still listed three of them six days later
and `gemma-4-12b` could not be picked at all.

**One restart, not a recurring one.** Providers are captured when
`pi-web-sessiond` starts and frozen for its lifetime, so installing or changing
the extension needs `systemctl --user restart pi-web-sessiond` once. The
`pi-web-extensions` init container copies it into `/data/pi-agent/extensions`
before `sessiond` comes up, so an ordinary deploy **is** that restart. Later
changes to the model *list* need nothing at all — the background refresh reaches
into the provider that is already registered.

**Where the names live.** Once a fetch exists, pi-ai merges the two lists by id
and a fetched entry **replaces** the hand-written one — display name,
`contextWindow`, `maxTokens` and `chatTemplateKwargs` included. So the gate
serves them: the table is `PRESETS` in `pi-web/pi_model_gate.py`, and
`GET /v1/models` there answers the router's own list with a `pi` block added to
every entry. One table, shipped with the pod, instead of a file on a data volume
that can go stale.

```
GET /v1/models  →  { "data": [ { "id": "qwen3.8-27b", …,
                      "pi": { "name": "Qwen 3.8 27B (Programmieren)",
                              "reasoning": true, "input": ["text"],
                              "contextWindow": 81920, "maxTokens": 16384,
                              "compat": { "thinkingFormat": "chat-template",
                                          "chatTemplateKwargs": {
                                            "enable_thinking": false } } } } ] }
```

- **Both Qwen presets say `reasoning: true`, and each pins its own value.** Pi
  only sends `chat_template_kwargs` for a model it has been told can reason, and
  that is the only way to send `enable_thinking: false` at all.
- **The window is ours, not the router's.** A preset is served with the
  `ctx-size` its profile in `templates/llama/post-deploy.py` names; the router's
  `n_ctx_train` is far larger and promising Pi that much would be a request the
  server cannot honour.

### A preset that disappears

**Decided, not left open:** a preset the router has stopped serving **vanishes
from Pi's list**, it is never marked and left selectable. The list the gate
answers with is always the router's own — `PRESETS` only *describes* the entries
in it and can add none — so dropping a preset from `presets.ini` takes it out of
the picker at the next refresh. The one window in which a retired preset can
still be offered is between the daemon restoring its persisted catalog and that
first refresh 15 s later; that is Pi's own persistence and we do not fight it.

The opposite direction is handled too, because it is the case that actually bit
us: a preset the router serves but `PRESETS` has no row for is **offered under
its own id** with a conservative 32k window, rather than hidden until somebody
remembers to add it.

## Which model in which mode

llama-server is a **router** since #1416: one process, several presets, and
the client picks one with the `model` field of its request. The GPU lease no
longer swaps the server — it sets the **mode**: the voice stack's device, the
embeddings server, and the set of presets a client may ask for while it stands,
which a policy proxy on 11435 enforces.

Three modes since #1435 (operator, 2026-09-19):

| Mode (lease) | Sprache | Gedächtnissuche | Presets allowed |
|---|---|---|---|
| **Haushalt** (no lease) | GPU | an | `gemma-4-e4b` |
| **Foundry** (`foundry`) | GPU | an | `gemma-4-e4b`, `gemma-4-12b` |
| **Erweitert** (`erweitert`) | CPU | **aus** | every preset the router knows |

`thinking` and `coding` are still accepted as older names of `erweitert`, so
nothing pointed at them breaks.

**A PI WEB session no longer has to ask anybody for the mode.** Picking a model
with `/model` takes the one that model needs — see the next section. The
Modell-Kachel in Solaris (#1374/#1381) is still the phone-side route to the same
thing, and it is who the session names when the window is already somebody
else's.

The first turn after a mode change pays a 10–20 s load on top of the ~56 s
switch, because the router loads a preset on demand. That is a slow answer, not
an error, and the session says so while it waits.

### Thinking is the client's switch now

The server used to be told once (`--reasoning off` in the coding profile,
#1321, after goose aborted on reasoning traces). One router serving four models
cannot carry that setting for all of them, so it is gone and **the client sends
it per request**: `chat_template_kwargs.enable_thinking`.

Pi sends that field only for a model declared `reasoning: true`, which is why
both Qwen presets are — the coding preset then pins the literal `false` (no
trace, tool calls work) and the Denken preset the literal `true`, because in a
Denken window the trace is the point. Gemma has no thinking mode and stays
`reasoning: false`.

Every client on this box must now do the same. Solaris' own Engine does
(#1416); **aider, goose, Continue and anything else pointed at 11435 send
nothing by default and will get a reasoning trace from the 27B** — they need
the same `chat_template_kwargs` in their own provider configuration.

### The model gate — picking a model activates it (#1435)

What answers on 11435 is not the router but a **mode policy proxy** in front of
it (the router itself moved to loopback 11434 and has no policy at all). It
reads the lease's `allowed` set on every request, so a session asking for
`qwen3.8-27b` during a household evening used to be **refused with 409** and
that was the end of it.

Since #1435 the sessions do not talk to 11435 directly. `models.json` points
them at **`http://127.0.0.1:11437/v1`** — the `model-gate` container of this
pod, which every other container reaches over the pod's shared network
namespace. It forwards verbatim, and on a 409 it asks the box for the **least
intrusive** mode that permits the wanted preset:

| Preset picked | Mode taken |
|---|---|
| `gemma-4-e4b` | none — Haushalt already allows it |
| `gemma-4-12b` | `foundry` — voice and vault search stay on the GPU |
| `qwen3.6-35b-a3b`, `qwen3.8-27b` | `erweitert` |

**How it reaches the lease at all.** It does not: `/api/model-lease` on the
Engine is loopback-only and carries no token — being able to reach it *is* the
authorisation — and this pod has its own netns. So the gate writes the wish to
`/data/model-lease/request.json` (a correlation id, the mode, the TTL — never a
secret) and the host's `pi-web-lease-broker.path` starts a **oneshot** service
that makes the call as holder `pi-web` and answers in `status.json` under the
same id. Exactly the bridge the Engine already uses for itself
(`gpu_lease_request.json` + `solaris-gpu-lease-broker`, #1333), for exactly the
same reason.

**While it switches**, a streamed session is told so in plain German rather than
left to look hung — *„Ich hole gerade den Modus Erweitert …"*, then a line every
15 s.

**It never steals.** A window somebody else holds comes back as a 409 naming the
holder and the end time, and pointing at the Modell-Kachel.

**It gives the card back.** The window is 900 s, renewed every 300 s while the
pod is working, and released once nothing has come through for 300 s. If the
gate dies without releasing, the box reclaims the mode 600 s after the last
renewal on its own (#1361: two missed renewals) — the TTL is the outer net, not
the mechanism.

The autoloop uses the same door and the same port, so there is one
implementation of this and not two. What still reaches its protocol is a
refusal the gate could not resolve, named in plain German, and the ticket is
picked up again next pass.

### The retired lease unit (#1392), and the unit that is not its return

Until v0.63 the post-deploy installed a host-side systemd unit
`pi-web-model-lease.service`, `BindsTo=pi-web.service`, that took the coding
lease whenever PI WEB started. That coupling is what forced PI WEB to stay
switched off (#1373) — a reboot, or ServiceBay's own start on every deploy,
would otherwise load Qwen, move voice onto the CPU and leave the household
assistant slow for up to four hours nobody asked for.

The upgrade retires it rather than deleting it quietly: the post-deploy stops
the unit, `disable`s it (which is what drops the `pi-web.service.wants` link —
a unit file removed while still enabled comes back with the next PI WEB start),
removes the unit file and the `{{DATA_DIR}}/pi-web/pi-web-lease.py` script copy,
and gives back a window still filed under holder `pi-web` — `GET` first, `DELETE`
only if it is ours, so the model tile's own window is never touched. All of that
still runs on every deploy.

`pi-web-lease-broker` above is **not** that unit coming back. What was wrong was
never that a unit existed: it was `BindsTo=pi-web.service` on a service that runs
around the clock, so the card was taken by PI WEB merely being up. The broker is
demand-driven — a `.path` watcher on the wish file and a `Type=oneshot` service
that ends when the call is made — and the premise that pi must never hold the
card was lifted by the operator on 2026-09-19.

## Why it now runs around the clock

PI WEB runs like any other service on the box: `pi.<publicDomain>` answers
without anybody starting anything first. ServiceBay's kube-write path emits
`[Install] WantedBy=default.target` into every `.kube` unit it renders, which is
what Quadlet turns into the `default.target.wants` link, and the post-deploy no
longer strips it back out. A box upgraded from #1373 carries a `.kube` this
template *stripped*, and ServiceBay only rewrites that file when the rendered
spec changed — so the post-deploy adds the section back when it is missing,
reloads the generator, and starts the service. (`systemctl enable` is not the
tool for it: a Quadlet-generated unit cannot be enabled; the generator makes
the link from `[Install]` itself.)

The run-state log of #1373/#1377/#1378 is gone with the reason for it: nothing
has to remember whether the operator had PI WEB running, because the answer is
now always "yes".

## Git-Zugang für private Repositories

Eine PI-WEB-Sitzung ist eine Shell in einem Ordner unter `/workspace`. „Add a
project" zeigt auf einen Ordner, **der schon existiert** — geklont wird über den
Knopf „Repo klonen" (siehe unten) oder im Terminal, und beides braucht
Zugangsdaten im Container, sonst bleibt ein `git clone` eines privaten
Repositories an der Anmeldung hängen (#1395).

**Was der Betreiber einträgt** — im ServiceBay-Assistenten, einmal:

| Variable | Wert |
|---|---|
| `PI_WEB_GIT_TOKEN` | das Token (Typ `secret`, kein Vorgabewert, wird nicht erzeugt) |
| `PI_WEB_GIT_USER` | `x-access-token` (GitHub-Konvention; GitLab: `oauth2`) |
| `PI_WEB_GIT_HOST` | `github.com` |

**Ein GitHub-Token dafür anlegen:** GitHub → Settings → Developer settings →
Personal access tokens → **Fine-grained tokens** → *Generate new token*.
*Repository access* auf **Only select repositories** stellen und genau die
Repositories auswählen, in denen hier gearbeitet wird; unter *Repository
permissions* reicht **Contents: Read and write** — mehr braucht `git clone`,
`fetch` und `push` nicht. Eine Laufzeit setzen (90 Tage sind ein guter
Kompromiss) und den Wert direkt in den Assistenten kopieren; GitHub zeigt ihn
nur einmal.

**Wie das Token in den Container kommt.** Nicht über `sessiond` oder `web`,
sondern nur über einen eigenen Init-Container `pi-web-git-credentials`, der als
`USER node` läuft und daraus einmal pro Start schreibt:

- `/data/pi-web/git-credentials`, Modus **0600**, Eigentümer `node` — das
  Format des eingebauten Helfers `git credential-store`, eine Zeile
  `https://<user>:<token>@<host>`.
- `/data/home/.gitconfig` (`$HOME` im Image) mit
  `credential.helper = store --file=/data/pi-web/git-credentials` und
  `safe.directory` für `/workspace` und `*` — ohne das verweigert Git jeden
  Checkout, dessen Dateien einer anderen UID gehören.

Der Init-Container muss **nach** `pi-web-data-perms` laufen: dessen
`chmod -R a+rwX /data` würde eine 0600-Datei bei jedem Start wieder öffnen.

**Was das schützt — und was nicht.** Das Token steht in keinem Argument, in
keiner Remote-URL und in keiner Shell-History; es taucht in `ps` nicht auf und
wird nirgends ausgegeben. `sessiond` und `web` tragen die Variable *nicht*, eine
Sitzung findet sie also nicht in ihrer eigenen Umgebung. Sie kann die Datei
`/data/pi-web/git-credentials` aber lesen — sie läuft als `node`, und genau das
ist der Zweck der Datei; wer eine Sitzung hat, hat das Token. Und weil
ServiceBay keinen Secret-Mount kennt (jedes Secret dieses Repositories erreicht
seinen Pod als gerendertes `value:`, siehe `templates/solaris/template.yml`),
ist der Wert für `podman inspect` dieses einen Init-Containers sichtbar. Der
Zuschnitt ist deshalb das Token selbst: fine-grained, nur die Repositories, nur
`Contents`.

Ein geleertes `PI_WEB_GIT_TOKEN` entfernt die Datei beim nächsten Start wieder —
Widerrufen heißt also: Token auf GitHub löschen, Feld im Assistenten leeren,
neu ausrollen.

> Nach einem Upgrade sind die drei Variablen neu: ServiceBay übernimmt für neue
> Variablen keine Installations-Überschreibungen, das Feld muss im Assistenten
> einmal ausgefüllt werden.

## Ein ServiceBay-Token je Projekt

Damit der Agent in einer Sitzung diese Box *lesen* kann — Dienstliste, Logs,
gerenderte Service-Definitionen — bekommt jedes Projekt sein eigenes,
schreibgeschütztes ServiceBay-Token (#1395).

**Warum das hier anders aussieht als bei claude-dev.** Bei claude-dev trägt der
MCP-Eintrag des Projekts dessen `sb_`-Token, und dieser Eintrag *ist* der
Besitznachweis (servicebay#2680). **Pi kennt kein MCP** — upstream sagt das
ausdrücklich („It intentionally does not include built-in MCP … build CLI tools
with READMEs") — es gibt hier also keine MCP-Konfigurationsdatei, in die ein
Token gehören könnte. An ihre Stelle tritt ein kleines Kommando im Container:

```
pi-web-project add <Projekt>        # Token anlegen
pi-web-project get services         # damit lesen (ruft `servicebay` auf)
pi-web-project list                 # welche Projekte eins haben
pi-web-project remove <Projekt>     # Token widerrufen
```

`get` spricht seit #1398 keine ServiceBay-Route mehr selbst, sondern ruft das
mitgelieferte Agenten-CLI auf (siehe den nächsten Abschnitt); geblieben ist hier
nur die Frage, **mit wessen Token** gelesen wird.

**Die drei Regeln**, weil sie das Verhalten erklären, das sonst überrascht:

- **Der Eintrag ist der Besitznachweis.** Ein Projekt gehört uns genau dann,
  wenn `/data/servicebay/projects/<Name>.json` existiert und ein `sb_`-Token
  nennt. Dieser eine Datensatz ist gleichzeitig Kennzeichen und Zugangsdatum,
  Token und Eintrag können also nicht auseinanderlaufen.
- **Nichts wird übernommen, nur weil es da ist.** Es gibt keinen Abgleich beim
  Start, der für neu aufgetauchte Ordner Token anlegt, und keine Markierungsdatei
  zum Mitmachen. `add` tippt ein Mensch, einmal, pro Projekt — was von Hand
  geklont wurde, bleibt unangetastet, und `remove` weist es ab statt zu raten.
- **`remove` löscht keine Dateien.** Es widerruft das Token und entfernt den
  Eintrag; das Arbeitsverzeichnis bleibt liegen. Danach scheitert derselbe
  Lesezugriff mit **401**.

**Woher das Eltern-Token kommt.** Aus der Variablen `PI_WEB_SB_TOKEN` — **leer
lassen**: `mintApiToken` heißt, dass ServiceBay bei der Installation selbst
eines anlegt, nur mit Leserecht und ohne Ablauf, und bei einer erneuten
Installation dasselbe wiederverwendet. Kein Handgriff für den Betreiber. Ein
selbst eingetragener Wert gewinnt.

> Ausdrücklich **nicht** `SB_READ_TOKEN`: das widerruft und erneuert ServiceBay
> bei jedem Ausrollen, und Kind-Token werden mit ihrem Elternteil ungültig
> (servicebay#2049) — jeder Deploy hätte damit sämtliche Projekt-Token
> abgeräumt, ohne dass irgendwo etwas danach aussieht.

Der Wert erreicht nur den Init-Container `pi-web-sb-token`, der ihn nach
`/data/servicebay/parent-token` (Modus 0600, Eigentümer `node`) schreibt und
nebenbei die Modi der Projekt-Einträge wiederherstellt — `pi-web-data-perms`
öffnet mit `chmod -R a+rwX /data` bei jedem Start sonst auch die. `sessiond` und
`web` tragen die Variable nicht; in der Umgebung einer Sitzung steht nur
`SERVICEBAY_API_URL`, und das ist kein Geheimnis. Wie beim Git-Token gilt: wer
eine Sitzung hat, kann die Dateien unter `/data` lesen — der Zuschnitt ist
deshalb das Recht selbst, `read` und sonst nichts.

> Nach einem Upgrade sind `PI_WEB_SB_TOKEN` und `SERVICEBAY_API_URL` neue
> Variablen; ServiceBay übernimmt für neue Variablen keine
> Installations-Überschreibungen, der Assistent muss also einmal durchlaufen
> werden (das Token-Feld dabei leer lassen).

## Wissen und Fähigkeiten im Container

Ein Coding-Agent, der diese Box nicht kennt, erfindet ihre Regeln neu. ServiceBay
pflegt deshalb ein **Agenten-Paket** und liefert es auf die Box aus — den
Assist-Katalog (Architekturentscheidungen, Rezepte, Leitfäden, Fußangeln), ein
abhängigkeitsfreies Agenten-CLI und eine `AGENTS.md`
(servicebay#2906–#2909). Dieses Template hängt es nur noch ein und übersetzt es
in die Formen, die Pi wirklich liest (#1398 Scheibe A).

**Der Mount.** Drei Unterverzeichnisse des ausgelieferten Checkouts —
`agent-cli`, `agent-docs`, `assists` — landen **nur lesend** unter
`/opt/servicebay` in `sessiond`, `web` und `autoloop`. Bewusst die
Unterverzeichnisse und nicht die Wurzel: dort liegen auch ServiceBays eigene
Repository-Dateien, und dessen `CLAUDE.md` in einem Container, der an einem
*anderen* Projekt arbeitet, wäre eine zweite Anweisungsquelle, der das Modell
folgt. Der Pfad steht **fest** im Pod-Spec — `/mnt/data/servicebay/agent-kit/
checkout/{agent-cli,agent-docs,assists}` — und ist bewusst keine eigene
Installationsvariable: ServiceBay setzt den Standardwert einer *neu
hinzugekommenen* Variable beim Upgrade eines bestehenden Dienstes nicht ein
(servicebay#2913), der Mount-Pfad wäre dann leer gerendert und der Pod liefe in
eine Neustart-Schleife, während der Installationsauftrag `done` meldet (#1403).
Auch nicht `{{DATA_DIR}}` (ein erster Versuch in #1404): die Variable ist **pro
Dienst** skaliert — box-verifiziert als `/mnt/data/stacks/pi-web` für diesen
Pod, nicht das flache `/mnt/data`, das der Name nahelegt —, während dieser
Checkout ServiceBays eigenes Auslieferungsziel ist, dieselbe Kopie für jeden
Abnehmer gleich welchen Dienstes. Fährt die Box ein ServiceBay, das noch nichts
ausliefert, startet PI WEB trotzdem — der Mount ist dann leer, der
Init-Container schreibt genau eine Zeile ins Log und endet mit 0, und
`servicebay` sagt es beim Aufruf in einem Satz.

**Der Befehl `servicebay`.** Das CLI selbst ist ServiceBays; auf `$PATH` liegt
nur ein Aufruf davon, der zwei Dinge weiß, die das CLI nicht wissen kann: wo es
liegt, und **mit wessen Token** es läuft. Arbeitet die Sitzung in einem Projekt,
das `pi-web-project add` bekommen hat, ist es dessen eigenes Token; sonst das des
Pods. Übergeben wird immer nur der *Pfad* der Token-Datei
(`SERVICEBAY_MCP_TOKEN_FILE`) — das CLI hat aus gutem Grund keinen
`--token`-Schalter, denn `/proc/<pid>/cmdline` ist für alle lesbar.

```
servicebay services            # Dienstliste
servicebay logs solaris        # Unit- und Podman-Logs
servicebay assists --query backup
servicebay assist adr-0007-container-network-isolation-and-carveouts
```

**Assists als Pi-Skills.** Pi lädt Skills nach dem Agent-Skills-Standard: ein
Verzeichnis je Skill mit einer `SKILL.md`, deren Kopf `name` und `description`
nennt. Ein Assist hat stattdessen `title`/`whenToUse`/`kind`/`tags` — derselbe
Inhalt in anderer Form, also wird er **erzeugt** und nicht verlinkt. Der
Init-Container `pi-web-agent-kit` schreibt bei **jedem Start**
`/data/pi-agent/skills/servicebay/<id>/SKILL.md` aus dem Mount: `description` ist
Titel und `whenToUse` zusammen, denn genau die Zeile entscheidet, ob Pi den Skill
öffnet. Unveränderte Dateien werden nicht angefasst, zurückgezogene Assists
verschwinden — und ein **leerer** Mount lässt die vorhandenen Skills stehen,
statt aus ServiceBays Auslieferungsfehler hier einen zweiten, stillen zu machen.
So wirkt eine geänderte Architekturentscheidung ohne neue Version: ServiceBay
frischt den Checkout stündlich auf, der nächste Start übernimmt ihn.

**Eine laufende Sitzung erfährt, dass sich das Kit bewegt hat (#1454).** Erzeugt
wird nur beim Pod-Start, aufgefrischt wird stündlich — eine Sitzung, die schon
läuft, behält ihren Text. Das ist ausgerechnet die Sitzung, die gerade in das
Problem läuft, das die Korrektur behebt: am 20.9.2026 lag die Berichtigung um
21:52 auf der Box, und dieselbe Sitzung lief bis 22:30 weiter, ohne sie je zu
sehen. Deshalb schreibt `pi-web-agent-kit` im selben Lauf, in dem er die Kopien
erzeugt, einen **Stempel** nach `/data/pi-agent/servicebay-kit.json`: `rev`,
`at` und je eine SHA-256 über jede Kit-Datei, aus der diese Kopien gemacht
wurden. Die Pi-Erweiterung `solaris-kit-notice.js` liest ihn beim Sitzungsstart,
vergleicht ihn zu jedem Zug mit dem Mount und schickt **eine** Zeile, die die
geänderten Dateien beim Namen nennt — „The ServiceBay agent kit changed since
this session started (assists/…). Re-read what you are relying on." Kein
erzwungenes Nachlesen, keine Wiederholung: erst eine *weitere* Änderung ist
wieder eine Zeile wert.

**Warum eine Pi-Erweiterung und nicht der Sitzungs-Daemon.** Das Ticket sagt
„der sessiond vergleicht", aber der Daemon ist `@jmfederico/pi-web` — ein
fremdes Bündel, das wir nicht ändern. Pis eigene Erweiterungs-Schnittstelle hat
die Naht dafür: `turn_start` und `pi.sendMessage(…, { deliverAs: "steer" })`,
nach `docs/extensions.md` des angehefteten `pi-coding-agent` „delivered after
the current assistant turn finishes executing its tool calls, before the next
LLM call". Die Zeile ist damit eine echte Nachricht in der Sitzung: im Verlauf
sichtbar und dieser Erweiterung zugeschrieben. Das Modell-Gate
(`pi_model_gate.py`) hätte denselben Satz an die ausgehende Anfrage hängen
können, aber das ändert ein Gespräch von außerhalb Pis eigenem Modell —
unsichtbar im Verlauf, unsichtbar in Pis Kontextrechnung und für den Betreiber
nicht nachlesbar. Und **SHA-256 statt mtime**: ServiceBays Auffrischung schreibt
den Checkout neu, ob sich sein Inhalt geändert hat oder nicht; ein
mtime-Stempel meldete stündlich eine Änderung, die nie stattgefunden hat.

**Die `AGENTS.md`, zweimal.** Global schreibt derselbe Init-Container
`/data/pi-agent/AGENTS.md` — ein kurzer Vorspann dieser Box (wo `/workspace`
liegt, dass `servicebay` auf dem `$PATH` steht, dass die Gates dem Projekt
gehören) und darunter unverändert die ausgelieferte Datei. Kein Symlink, weil Pi
keine Einbindung kennt: Vorspann und Handbuch müssen **eine** Datei sein. Je
Projekt legt `pi-web-project add` — und damit auch der Knopf „Repo klonen" —
einen fünfzeiligen Zeiger auf die globale Datei ab, **nur** wenn das Projekt
keine eigene `AGENTS.md`/`CLAUDE.md` mitbringt. Eine vorhandene wird nie
überschrieben: Pi nimmt den ersten Treffer im Verzeichnis, unsere Datei stünde
sonst vor den Konventionen des Projekts.

**Das Zuhause, in das der Rückfallweg führt.** Pi liest sein Agentenverzeichnis
aus `PI_CODING_AGENT_DIR` — und wenn die Variable in einem Prozess fehlt, aus
`~/.pi/agent`. Auf der Box war genau das die Lücke (#1422): das ganze
Verzeichnis lag unter `/data/pi-agent`, `$HOME/.pi` gab es nicht und
`$XDG_CONFIG_HOME` zeigte auf ein leeres `/data/config`. Ein `pi`, das ohne die
Variable startet — ein Terminal in der Sitzung, alles, was das Modell selbst
aufruft — fand dort nichts und stand ohne Handbuch und ohne Skills da, während
der Init-Schritt Erfolg meldete.

Deshalb stehen `HOME`, `XDG_CONFIG_HOME` und `PI_CODING_AGENT_DIR` jetzt im
Pod-Spec und nicht mehr nur im Image, für `sessiond`, `web`, `autoloop` und den
Erzeuger selbst; `XDG_CONFIG_HOME` liegt als `/data/home/.config` im selben
Zuhause wie die `.gitconfig` aus #1360, und der Init-Container legt
`$HOME/.pi/agent` als Verweis auf `/data/pi-agent`. Beide Wege enden damit im
selben Verzeichnis, gleich welche Variable ein Prozess befragt. `/data` ist das
dauerhafte Volume, also überlebt das einen Neustart.

`SERVICEBAY_API_URL` trägt aus demselben Ticket nun auch `web` und `autoloop`:
gemessen war sie nur in `sessiond` gesetzt, und ein CLI ohne Endpunkt beantwortet
nichts — was von außen wie ein Token-Problem aussieht. Die Adresse ist kein
Geheimnis; das Token bleibt in seiner 0600-Datei.

### Die Erweiterung `pi-subagents` — eine getroffene Entscheidung, kein Versehen

Pi gibt dem Modell vier Werkzeuge — `read`, `write`, `edit`, `bash` — und alles,
was dabei anfällt, landet im selben Gespräch. `pi-subagents` gibt ihm die
Möglichkeit, eine Teilarbeit an einen eigenen Agenten mit eigenem Kontext
abzugeben; der Hauptfaden bleibt frei. Genau darum hat der Betreiber sie am
13.9.2026 verlangt (#1423).

**Woher sie kommt.** `npm:pi-subagents`, Repository
`github.com/nicobailon/pi-subagents`, gelistet auf `pi.dev/packages/pi-subagents`.
Das ist ein **Fremdpaket**: Herausgeber ist `nicobailon`, nicht `earendil-works`
wie pi selbst. Die Aufnahme in das Paketverzeichnis von pi.dev ist eine gewisse
Bestätigung, aber keine Herkunft aus dem Kernprojekt. Sie ist die erste fremde
Erweiterung in diesem Container.

**Was sie darf.** Eine pi-Erweiterung läuft mit denselben Werkzeugen wie der
Agent: `read`, `write`, `edit` und `bash` in diesem Container. Dieser Container
hat Zugriff auf `/workspace` — also auf jedes Repository, das hier ausgecheckt
ist, samt der Git-Zugangsdaten aus #1360 — und auf die ServiceBay-CLI mit dem
Lese-Token dieses Pods. Eine Erweiterung kann demnach alles, was eine Sitzung
kann. Das ist kein Nebeneffekt der Installation, sondern ihr Wesen.

**Ohne feste Version — ausdrücklich so entschieden.** Der Eintrag lautet
`pi install npm:pi-subagents`, ohne `@<version>`: **jeder Pod-Bau zieht die
jeweils neueste Fassung.** Das Ticket hatte das Gegenteil vorgeschlagen (eine
feste Version, wie beim py-cord-Fork in `mdopp/foundry-chronicle#145`); der
Betreiber hat am 13.9.2026 anders entschieden und will die aktuelle Fassung.
Wer das später pinnen möchte, ändert damit eine Entscheidung und nicht einen
Fehler — bitte mit dem Betreiber, nicht nebenbei.

**Warum im Template und nicht nur zur Laufzeit.** `pi install` schreibt die
`settings.json` im Agentenverzeichnis auf dem dauerhaften Volume, ein Neustart
übersteht das also. Ein Pod-**Neubau** fängt mit einem leeren Volume an — was
nicht deklariert ist, fehlt danach. Deshalb installiert der Init-Container
`pi-web-extensions` sie bei jedem Start; ein fehlgeschlagener Aufruf (kein Netz
beim Booten) bricht den Pod nicht ab, sondern lässt stehen, was auf dem Volume
liegt.

### Die eigene Erweiterung `solaris-llama` (#1435)

Derselbe Init-Container legt seit #1435 auch **unsere** Erweiterung ab — nicht
aus einem Paketverzeichnis, sondern aus dem Abbild: `/opt/solaris/pi-extensions`
wird nach `/data/pi-agent/extensions` kopiert, mit `rm -rf` vor `cp -a`, damit
ein Stand von gestern nicht danebenliegen bleibt. Pi lädt **jede** Datei in
diesem Verzeichnis, eine Altfassung wäre also eine zweite Anbieter-Anmeldung und
keine tote Datei.

Sie meldet den Anbieter `solaris-llama` in Pis **nativer** Form an und bringt
damit ein `fetchModels` mit — das ist der einzige Grund, warum Pis eigener
stündlicher Katalog-Abgleich für uns überhaupt etwas zu tun hat. Siehe „How the
model is configured" oben. **Ein** Neustart von `pi-web-sessiond` ist dafür
nötig, und den leistet der gewöhnliche Deploy, weil der Init-Container vor dem
Daemon läuft. Danach nichts Wiederkehrendes mehr.

## Der Knopf „Repo klonen"

Ein Repository kommt auf die Box, ohne dass jemand ein Terminal öffnet: in PI WEB
im Projekt **Werkstatt** der Reiter **Repo klonen**, Adresse eintragen, klicken.
Der Klon landet unter `/workspace/<name>` und ist danach ein **eigenes Projekt**
in der Liste, in dem sich sofort eine Sitzung starten lässt (#1395).

**Beim ersten Mal**, solange es noch gar kein Projekt gibt: die Befehlspalette
öffnen und *„Werkstatt für geklonte Repositories anlegen"* ausführen. Das legt
`/workspace` selbst als Projekt an — mehr ist einmalig nicht zu tun, danach ist
der Reiter immer da.

**Was der Knopf annimmt und was nicht.** Erlaubt sind genau die Formen, die auch
claude-dev annimmt: `https://…`, `http://…`, `ssh://…` und
`git@server:benutzer/projekt.git`. Alles andere — `file://`, ein Pfad auf der
Box, ein Wort mit einem Bindestrich am Anfang — wird abgelehnt, mit einem Satz,
der sagt, was stattdessen dort hingehört. Der Ordnername wird aus dem letzten
Teil der Adresse abgeleitet (`.git` fällt weg) und muss dieselbe Namensregel
erfüllen wie bei `pi-web-project`, damit der Token-Schritt danach nicht an einem
Namen scheitert, den der Klon-Schritt noch durchgelassen hat.

**Es wird nie etwas überschrieben.** Liegt unter `/workspace` schon ein Ordner
dieses Namens, bricht der Knopf ab und sagt das — Git wird dafür gar nicht erst
gestartet. Nichts wird umbenannt und nichts gelöscht.

**Was ein Klick auslöst**, in dieser Reihenfolge: `git clone` mit den
hinterlegten Zugangsdaten (Scheibe A), dann `pi-web-project add <name>` für das
eigene Leserecht des Projekts (Scheibe C), dann `POST /api/projects` beim
Wirt, damit der Klon in der Projektliste steht. Schlägt der Token-Schritt fehl,
gilt der Klon trotzdem als erfolgreich: der Ordner ist da, und die Meldung sagt,
wie sich das Leserecht nachholen lässt. Ein Fehlschlag beim Klonen selbst wird
in Klartext übersetzt — abgelehnte Anmeldung, unbekannter Server, kein
Repository unter der Adresse — mit Gits eigener Meldung darunter, aus der
Zugangsdaten herausgestrichen sind.

### Warum das ein Plugin ist und keine zweite Oberfläche

PI WEB bringt seine Projektverwaltung mit; daneben eine eigene
Konfigurationsseite zu stellen wie bei claude-dev war die ausdrückliche
Entscheidung *dagegen* (mdopp, 2026-09-08). Upstream sieht für genau diesen Fall
Plugins vor, und der eingebaute Git-Teil benutzt dieselben öffentlichen
Verträge — wir hängen uns also nicht an, sondern benutzen die vorgesehene Tür.

Der Vertrag bestimmt dabei den Zuschnitt: Ein Server-Plugin darf **nur** einen
Workspace-Provider beitragen, und sein Rückkanal `backend.request` ist nur in
einem Projekt erreichbar, das dieser Provider **exklusiv besitzt**. Deshalb ist
`/workspace` selbst das Projekt „Werkstatt", in dem der Knopf sitzt — und
deshalb beansprucht unser Provider ausdrücklich **nichts** darunter: die Klone
gehören dem eingebauten Git-Plugin, mit Worktrees und Diff-Ansicht. Der Knopf
fügt eine Tür hinzu und nimmt nichts weg. Kommandos laufen über das
`execFile` des Wirts, das argumentbasiert ist und **keine Shell** benutzt — eine
eingetippte Adresse kann also kein zweites Kommando werden.

### Wie das Plugin installiert wird

Es liegt im **Image** (`pi-web/plugins/solaris-clone`, kopiert nach
`/opt/solaris/pi-web-plugins`), nicht im Asset-Baum des Templates: Es ist gegen
die Plugin-API der in `pi-web/Dockerfile` angehefteten PI-WEB-Version
geschrieben, die beiden gehören zusammen und werden zusammen angehoben.

Der Init-Container `pi-web-plugins` kopiert es bei jedem Start nach
`/data/pi-web/plugins/` — das ist die lokale Plugin-Quelle, die PI WEB von sich
aus durchsucht. Ein dort gefundenes Plugin ist **standardmäßig aktiv**;
*Settings → PI WEB plugins* braucht man nur zum **Ab**schalten, nicht zum
Freischalten. Es ist also nichts zu klicken, damit der Reiter nach dem Ausrollen
da ist.

> **Der Server-Teil wird beim Start von `sessiond` aktiviert** — eine Änderung
> daran braucht also einen Neustart des Sitzungs-Dienstes, und PI WEB weist eine
> Anfrage an eine veraltete Fassung mit *„reload after the session daemon
> restarts"* ab. Beim Ausrollen über ServiceBay passiert das von selbst, weil der
> Pod ohnehin neu startet. Nur wer die Dateien unter `/data` von Hand ändert,
> muss `sessiond` selbst neu starten; für die Browser-Hälfte allein genügt ein
> Neuladen der Seite.

## Pi-Autoloop

Neben der Weboberfläche läuft im selben Pod ein dritter Prozess, `autoloop`. Er
holt sich Tickets von GitHub und lässt Pi sie kopflos abarbeiten.
Arbeitsteilung: **Claude schneidet zu, Pi baut ab.**

**Ein Ticket freigeben.** Auf dem Ticket das Label **`pi:ready`** setzen — mehr
nicht. Kein Label, kein Zugriff: der Loop sieht ausschließlich offene Tickets
mit diesem Label, und ausschließlich in den Repositories, die in
`PI_AUTOLOOP_REPOS` stehen. Was dort nicht steht, wird nicht einmal geklont.

**Was dann passiert.** Höchstens ein Ticket je Runde (Standard: alle fünf
Minuten nachsehen):

1. Der Loop legt die Sperre `refs/autoloop/claim/<Nummer>` im Repository an.
   Wer sie zuerst anlegt, arbeitet; jeder zweite Loop bekommt von GitHub
   **HTTP 422 „Reference already exists"** und lässt das Ticket in Ruhe. Es ist
   dieselbe Sperre, die die Claude-Seite benutzt — deshalb greifen die beiden
   nie nach demselben Ticket.
2. Das Repository wird nach `/workspace/autoloop/<besitzer>/<repo>/<nummer>`
   geklont.
3. `pi --mode json` bekommt das Ticket als Auftrag, mit dem Preset
   `qwen3.8-27b`. Der Loop fordert **keine** GPU an — der Modus kommt aus der
   Modell-Kachel in Solaris (dort „Fokus Programmieren"). Wird das Preset doch einmal abgelehnt (409), steht
   der Grund im Klartext im Protokoll, statt dass der Lauf still nichts tut.
4. Danach laufen die Prüfungen des Zielrepositories — was es selbst mitbringt
   (`ruff`, `pytest`, `npm run lint`, `npm test`). Ein Werkzeug, das dieser
   Container nicht hat, steht im Protokoll als *übersprungen* und gilt nie als
   bestanden.
5. Commit auf den Zweig `pi/<nummer>-<kurztitel>`, Push, Pull Request mit
   `Refs #<nummer>`.

**Zusammengeführt wird nie etwas.** Der Loop öffnet den PR und hört auf.
Zusammenführen entscheidet ein Mensch oder die Claude-Seite. Ist das Gate rot
oder das Zeitlimit (Standard eine Stunde) erreicht, wird der PR als **Entwurf**
geöffnet — abgebrochen, aber sichtbar, und **ohne Nachbesserungsschleife**.

**Wo das Protokoll steht.** Als Kommentar am Pull Request und im Container unter
`/data/pi-web/autoloop/<besitzer>-<repo>-<nummer>.log` (auf der Box unter
`<DATA_DIR>/pi-web/data/pi-web/autoloop/`). Es nennt Modell, Dauer, Zweig, PR,
das Ergebnis jedes Gates und eine Zusammenfassung dessen, was Pi getan hat.

**Noch einmal arbeiten lassen.** Am Ende gibt der Loop die Sperre wieder frei.
Dass ein Ticket trotzdem nicht sofort erneut angefasst wird, liegt am
gepushten Zweig: solange `pi/<nummer>-…` im Repository steht, überspringt der
Loop das Ticket. Wer eine zweite Runde will, löscht diesen Zweig.

**Anschalten.** Standardmäßig ist der Loop **aus** (`PI_AUTOLOOP_ENABLED` =
`false`) und schreibt das einmal je Runde in sein Log. Vor dem Anschalten muss
`PI_WEB_GIT_TOKEN` drei Berechtigungen haben: `Issues: Read`,
`Pull requests: Read and write`, `Contents: Read and write`. Der Token steht
nirgends in einer Umgebungsvariable — der Loop liest ihn aus derselben
0600-Datei, aus der auch `git` ihn nimmt, und startet Pi ohne ihn.

## Not in the `solarisbay` stack

The stack is the household assistant — the model server plus the Solaris
service. PI WEB is a developer tool that happens to live on the same box, like
`paperless`, so it installs on its own.

## Verifying on the box

- `https://pi.<publicDomain>/` unauthenticated → **302** to Authelia; after
  login the UI loads over WebSocket.
- The model picker lists all four presets in every mode (#1431), and `curl -s
  http://127.0.0.1:11435/v1/models | jq '.mode, [.data[] | {id,
  allowed_in_mode}]'` names the standing mode and marks the same ids — with the
  card at Haushalt only `gemma-4-e4b` is `true`.
- With the card at **Haushalt**, `/model` → *Qwen 3.8 27B* in a session: the
  answer begins with *„Ich hole gerade den Modus Erweitert …"*, and after the
  switch the model answers in the same turn. `curl -s
  http://127.0.0.1:8787/api/model-lease` then names holder `pi-web`, mode
  `erweitert`.
- `/model` → *Gemma 4 12B* takes **`foundry`** and not `erweitert` — check the
  holder's `model` field above, and that `solaris-whisper.service` is still on
  `cuda` (`grep WHISPER_DEVICE {{DATA_DIR}}/solarisbay/voice-device.env`).
- Leave the pod idle for 6 minutes: the same `curl` reports `"state":"none"` —
  the gate gave the card back without anyone asking.
- With the Modell-Kachel holding the card as `widget`, a `/model` pick in a
  session answers with a sentence naming `widget` and the end time, and the
  lease still reports holder `widget`: nothing was stolen.
- `systemctl --user status pi-web-model-lease` reports **not-found**, while
  `systemctl --user status pi-web-lease-broker.path` is **active (waiting)** and
  `pi-web-lease-broker.service` is **inactive (dead)** between wishes — it runs
  only while one is pending.
- `grep -c Install ~/.config/containers/systemd/pi-web.kube` is 1 — the service
  is up now and comes back after a reboot.
- From another LAN device, `curl -m 3 http://<box>:8504/` must fail — the
  `blockLanAccess` rule on `PI_WEB_PORT`. `http://<box>:11435/v1/models` must
  **answer**: llama's LAN side is open by decision (#1420), PI WEB's is not.
- In einer Sitzung im Projektordner: `pi-web-project add <Projekt>` meldet eine
  Token-Kennung, `pi-web-project get services` beantwortet die Dienstliste, und
  nach `pi-web-project remove <Projekt>` scheitert derselbe Aufruf mit **401** —
  das Arbeitsverzeichnis liegt danach noch da. `ls -l
  /data/servicebay/projects/` zeigt `-rw------- node`.
- Agenten-Paket (setzt ServiceBay ≥ 5.34.0 auf der Box voraus): `ls
  /opt/servicebay/{agent-cli,agent-docs,assists}` ist gefüllt, und ein Schreiben
  dorthin scheitert mit „Read-only file system".
- `servicebay services --json` liefert im Projektordner die Dienstliste und
  `servicebay assist adr-0007-container-network-isolation-and-carveouts` den
  Text der Entscheidung. `ps auxww | grep servicebay` zeigt kein Token.
- `pi list` nennt neben dem `relays`-Paket von PI WEB auch `pi-subagents`, und
  `cat /data/pi-agent/settings.json` führt es unter `packages`.
- `jq -r '.rev, .at, (.files | length)' /data/pi-agent/servicebay-kit.json`
  nennt eine Revision, einen Zeitpunkt und so viele Dateien wie
  `ls /opt/servicebay/assists/*.md | wc -l` plus eins. Wer in einer laufenden
  Sitzung prüfen will, ob die Zeile kommt, wartet die nächste Auffrischung ab
  (oder lässt ServiceBay ausliefern) und schaut, ob die Sitzung im nächsten Zug
  genau eine Zeile mit den geänderten Dateinamen bekommt.
- `ls /data/pi-agent/extensions` zeigt `solaris-llama.js` und
  `solaris-kit-notice.js`, und
  `jq '.providers["solaris-llama"] | has("models")' /data/pi-agent/models.json`
  antwortet `false` — die Liste kommt aus der Erweiterung, nicht aus der Datei.
- `podman exec pi-web-model-gate curl -s 127.0.0.1:11437/v1/models | jq
  '[.data[] | {id, name: .pi.name, ctx: .pi.contextWindow}]'` nennt **alle vier**
  Presets mit den deutschen Namen — und nur die, die der Router gerade bedient.
- Im Modellwähler einer Sitzung stehen dieselben vier Namen. Nach einem
  `systemctl --user restart pi-web-sessiond` sagt
  `journalctl --user -u pi-web-sessiond | grep global-provider` „baseline
  bootstrapped and frozen" mit `solaris-llama` in `providerIds`.
- `ls /data/pi-agent/skills/servicebay | wc -l` nennt so viele Skills wie
  `ls /opt/servicebay/assists/*.md | wc -l`, und `head -4
  /data/pi-agent/AGENTS.md` zeigt den Vorspann dieser Box.
- `readlink -f ~/.pi/agent` nennt in jeder der drei Umgebungen `/data/pi-agent`,
  und `env | grep -E '^(HOME|XDG_CONFIG_HOME|PI_CODING_AGENT_DIR)='` zeigt in
  `sessiond`, `web` und `autoloop` dieselben drei Werte. `podman exec pi-web-web
  printenv SERVICEBAY_API_URL` nennt die Adresse statt nichts.
- Der Startkopf einer neuen Sitzung nennt die geladene `AGENTS.md` und die
  Skills; `/skill:adr-0007-container-network-isolation-and-carveouts` öffnet den
  Text in der Sitzung.
- Ein frisch geklontes Projekt hat eine `AGENTS.md` mit dem Zeiger; ein Klon
  eines Repositories, das eine eigene mitbringt, hat unverändert dessen Fassung.
- `ls /data/pi-web/plugins/solaris-clone` zeigt das Plugin, und unter
  *Settings → PI WEB plugins* steht „solaris-clone" als aktiv — ohne dass jemand
  es eingeschaltet hat.
- Befehlspalette → *„Werkstatt für geklonte Repositories anlegen"*: `/workspace`
  taucht als Projekt auf und hat den Reiter **Repo klonen**.
- Dort eine öffentliche Repo-Adresse eintragen und klicken: der Klon liegt unter
  `/workspace/<name>`, steht nach dem Aktualisieren als eigenes Projekt in der
  Liste, und `pi-web-project list` nennt für ihn eine Token-Kennung.
- Derselbe Klick ein zweites Mal meldet, dass der Ordner schon existiert — und
  `ls -la /workspace/<name>` zeigt denselben Stand wie vorher.
- Eine abgelehnte Adresse (`file:///etc/passwd`) meldet einen Satz auf Deutsch,
  und im Log von `sessiond` steht kein `git`-Aufruf dazu.
- In einer Sitzung: `git clone https://github.com/<privates Repo>.git` läuft ohne
  Rückfrage durch, `git -C /workspace/<name> fetch` ebenso. `ls -l
  /data/pi-web/git-credentials` zeigt `-rw------- node`, und
  `grep -r <Token-Präfix> ~/.bash_history` sowie `ps auxww` finden nichts.
- Autoloop aus (Standard): `podman logs pi-web-autoloop` meldet je Runde
  „switched off", und unter `/workspace/autoloop` liegt nichts.
- Autoloop an: ein Wegwerf-Ticket mit `pi:ready` markieren. Das Log nennt Klon,
  Modell und Gates, unter `/data/pi-web/autoloop/` liegt ein Protokoll, und im
  Repository steht ein PR mit `Refs #<nummer>` — nicht zusammengeführt.
- Sperre: während der Lauf läuft, liefert
  `gh api repos/<repo>/git/matching-refs/autoloop/claim` den Ref des Tickets,
  und ein zweiter Anlauf auf dasselbe Ticket
  (`gh api --method POST repos/<repo>/git/refs -f ref=refs/autoloop/claim/<n>
  -f sha=$(git rev-parse origin/main)`) scheitert mit **422 „Reference already
  exists"**. Nach dem Lauf ist der Ref wieder weg.
- `podman exec pi-web-autoloop env | grep -i token` findet nichts.
