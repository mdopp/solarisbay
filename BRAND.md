# Solilos — Brand

> **Solilos** is a second brain you talk to. Put your thinking into words —
> by voice or in chat — and it holds, connects, and gives it back, alive,
> when you ask.

**Tagline:** Help yourself think.
**Voice:** "Sol" · wake phrase "Hey Sol"
**Home:** SolBay, on ServiceBay

---

## The name

Solilos descends from *soliloquium* — the word St. Augustine coined around
386 AD for his *Soliloquia*, which is nothing but a dialogue between Augustine
and his own Reason. There was no word for *speaking alone with one's own soul*,
so he made one: *solus* (alone) + *loquī* (to speak). Solilos is that idea,
made to remember.

Read the word and it splits into three — and it reads the same forwards and
backwards, with **you** at the center:

- **SOL** — soul · sun · source. Your knowledge, your way of thinking, the
  light you cast on a question.
- **I** — you. The one who speaks; the interface; the figure standing at the
  center of the wordmark.
- **LOS** — to speak is to let loose. The *-loquy* of soliloquy is Latin
  *loquī*, "to speak"; a thought spoken is a thought released. **SOL** becomes
  **LOS** — the soul, set into words.

A soliloquy on a stage is spoken alone, yet overheard. **Solilos is the
listener that was always missing:** you think aloud, and for the first time
something holds it — and connects it to everything else you know, and to what
the world knows.

`solilos` is a true palindrome; the wordmark puts the human figure on the
mirror axis. The tagline carries the same three-beat rhythm as the name.

## Voice & wake

- You call it **Sol** — the living face of Solilos: the sun that lights your
  stored knowledge, the one who turns to you when you speak.
- Wake phrase: **"Hey Sol."**
- Solilos is the corpus and the home; **Sol is who answers when you call.**

## Tagline

**Help yourself think.**

- **Display / emphasis:** `Help. Yourself. Think.` — three deliberate beats,
  echoing SOL · I · LOS.
- **Inline / spoken:** *Help yourself think.* — one breath, the idiom intact.
- Rule of thumb: punctuated form for hero/display; smooth form for body,
  captions, and voice.

Supporting lines:
- Origin line: *A soliloquy you can keep.*
- Elevator: *Solilos is a soliloquy you can keep — put your mind into words,
  and it knows the rest.*

## What it is

A personal knowledge base and agent: your chats, documents, notes, and the
digital reflection of your thinking — captured, connected, and reachable by
voice ("Hey Sol") or chat. It runs **on ServiceBay**, which is its harbor:

> **ServiceBay** hosts **SolBay** (the home & store) · **Solilos** is the
> soul that lives there · **Sol** is the voice you summon.

**SolBay** is the home stack — the harbor where the soul rests (your chats,
documents, knowledge) and the machinery that brings it alive runs. The `*Bay`
sibling to ServiceBay. Components *inside* SolBay carry bare role names
(`gatekeeper`, `schema-init`) — the namespace already says whose they
are.

## Tone of voice

Soul **and** clarity. Warm and invitational, a little mythic at the origin,
plain-spoken in the promise. Never self-help-cheesy, never cold-tech. It speaks
*to* you, as the part of you that remembers everything and has read the rest.

## Domains

- Brand: **solilos.ai** (primary) · **solilos.io**
- Home: **solilos.de**
- `solilos.com` is registered but parked (no live site) — acquisition target,
  not blocking.

## Naming map (for the rename — see issue #138)

| Layer | Today (OSCAR) | Becomes |
|---|---|---|
| Repo + ServiceBay registry | `mdopp/oscar` | `mdopp/solbay` |
| Brand / soul | OSCAR | **Solilos** |
| Voice / wake | "OSCAR" | **Sol** / "Hey Sol" |
| Home stack + pod | `oscar-household`, `stacks/oscar` | **SolBay**, `stacks/solbay` |
| Home template dir | `templates/oscar-household` | `templates/solbay` |
| Hermes plugin + stack name | `name: oscar`, `~/.hermes/plugins/oscar/` | `name: solbay`, `~/.hermes/plugins/solbay/` |
| Components (in-stack, bare roles) | `oscar-gatekeeper`, `oscar-household-init`, `oscar-data` | `gatekeeper`, `schema-init`, `solilos-data` |
| Chat pod | `oscar-chat` image, `templates/hermes-chat`, pkg `oscar_chat` | `solilos-chat` image + `templates/solilos-chat` template + pod + `solilos-chat/` source dir, pkg `solilos_chat` |
| Published images (GHCR, brand-prefixed) | `oscar-gatekeeper`, `oscar-household-init`, `oscar-chat`, `oscar-gatekeeper-ml` | `solilos-gatekeeper`, `solilos-schema-init`, `solilos-chat`, `solilos-gatekeeper-ml` |
| Python projects | `oscar-gatekeeper`, `oscar-schema`, `oscar-chat` | `solilos-gatekeeper`, `solilos-schema`, `solilos-chat` |
| Hermes skill names | `oscar-status`, `oscar-audit-query`, `oscar-debug-set`, `oscar-daily-chronicle`, `oscar-dynamic-skills`, `oscar-notes-search`, `oscar-room-enrollment`, `oscar-custom-*` | `sol-status`, `sol-audit-query`, `sol-debug-set`, `sol-daily-chronicle`, `sol-dynamic-skills`, `sol-notes-search`, `sol-room-enrollment`, `sol-custom-*` |
| Wyoming program / MCP server names | `oscar-gatekeeper-asr/-tts`, `oscar-gatekeeper-rooms` | `solilos-gatekeeper-asr/-tts`, `solilos-gatekeeper-rooms` |
| Notes namespace (tags + folders) | `#oscar/…`, `oscar/journal`, `oscar/ingested`, `oscar/stub` | `#solilos/…`, `solilos/journal`, `solilos/ingested`, `solilos/stub` |
| HA onboarding account + token file | `oscar` user, `.oscar-long-lived-token` | `solilos` user, `.solilos-long-lived-token` |
| Env vars | `OSCAR_*` | `SOLILOS_*` |
| Data | `oscar.db`, `/var/lib/oscar`, `/opt/data/skills/oscar`, `{{DATA_DIR}}/oscar-household` | `solilos.db`, `/var/lib/solilos`, `/opt/data/skills/solilos`, `{{DATA_DIR}}/solbay` |

Inside SolBay, in-stack containers keep bare role names; the brand prefix
returns only on published artifacts (GHCR images, Python projects) where they
must be identifiable. Two deliberate stems: home/voice on `sol`/`solbay`
(stack, pod, template dir, plugin, Hermes skill names, voice handle), brand
artifacts on `solilos` (images, Python projects, env vars `SOLILOS_*`, data
paths, notes namespace, Wyoming/MCP program names). The chat pod is the one
brand-prefixed *template* (`solilos-chat`) so it lines up with its
`solilos-chat` image and `solilos-chat/` source dir, alongside the role-named
source dirs (`voice-gatekeeper/`, `database/`).
Unchanged: `hermes`, `hermes-webui` (retired), `ollama`; generic package
`gatekeeper`; `HERMES_*` / `GATEKEEPER_*` / `DEFAULT_UID`. The rename is a
coordinated migration — see #138.

---

*Brand v1.0 — origin: soliloquium (Augustine, c. 386 AD).*
