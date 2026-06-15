# Solaris — Brand

> **Solaris** is a second brain you talk to. Put your thinking into words —
> by voice or in chat — and it holds, connects, and gives it back, alive,
> when you ask.

**Tagline:** Help yourself think.
**Voice:** "Solaris" · wake phrase "Hey Solaris"
**Home:** SolarisBay, on ServiceBay

---

## The name

<!-- DRAFT: Solaris narrative, pending owner review -->

**Solaris** takes its name from Stanisław Lem's 1961 novel — the planet whose
entire surface is a single sentient ocean: a vast, thinking thing that listens,
remembers, and answers back in forms drawn from the minds of those who study it.
That is the picture: a mind you can talk to, made of everything you have ever
put into it.

It keeps the older idea it grew from, too. The name's root is *soliloquy* —
from St. Augustine's *Soliloquia* (c. 386 AD), a dialogue between Augustine and
his own Reason. Augustine had no word for *speaking alone with one's own soul*,
so he coined one: *solus* (alone) + *loquī* (to speak). Solaris is that
soliloquy **made to remember**: you speak with your own soul, and this time
something holds every word.

- **Sol** — soul · sun · source. Your knowledge, your way of thinking, the
  light you cast on a question.
- **The ocean** — Lem's thinking sea: not a tool that fetches, but a mind that
  holds the whole of what you've said and connects it.
- **The soliloquy** — a thought spoken is a thought released. To speak aloud
  and be heard, fully, is the thing Solaris is for.

A soliloquy on a stage is spoken alone, yet overheard. **Solaris is the
listener that was always missing:** you think aloud, and for the first time
something holds it — and connects it to everything else you know, and to what
the world knows.

## Voice & wake

- You call it **Solaris** — the living face of Solaris: the sun that lights your
  stored knowledge, the one who turns to you when you speak.
- Wake phrase: **"Hey Solaris."**
- Solaris is the corpus and the home; **Solaris is who answers when you call.**

## Tagline

**Help yourself think.**

- **Display / emphasis:** `Help. Yourself. Think.` — three deliberate beats:
  the soul, the self, the thought set loose.
- **Inline / spoken:** *Help yourself think.* — one breath, the idiom intact.
- Rule of thumb: punctuated form for hero/display; smooth form for body,
  captions, and voice.

Supporting lines:
- Origin line: *A soliloquy you can keep.*
- Elevator: *Solaris is a soliloquy you can keep — put your mind into words,
  and it knows the rest.*

## What it is

A personal knowledge base and agent: your chats, documents, notes, and the
digital reflection of your thinking — captured, connected, and reachable by
voice ("Hey Solaris") or chat. It runs **on ServiceBay**, which is its harbor:

> **ServiceBay** hosts **SolarisBay** (the home & store) · **Solaris** is the
> soul that lives there · **Solaris** is the voice you summon.

**SolarisBay** is the home stack — the harbor where the soul rests (your chats,
documents, knowledge) and the machinery that brings it alive runs. The `*Bay`
sibling to ServiceBay. Components *inside* SolarisBay carry bare role names
(`gatekeeper`, `schema-init`) — the namespace already says whose they
are.

## Tone of voice

Soul **and** clarity. Warm and invitational, a little mythic at the origin,
plain-spoken in the promise. Never self-help-cheesy, never cold-tech. It speaks
*to* you, as the part of you that remembers everything and has read the rest.

## Domains

- Brand: **solaris.ai** (primary) · **solaris.io**
- Home: **solaris.de**
- `solaris.com` is registered but parked (no live site) — acquisition target,
  not blocking.

## Naming map (for the rename — see issue #138)

| Layer | Today (OSCAR) | Becomes |
|---|---|---|
| Repo + ServiceBay registry | `mdopp/oscar` | `mdopp/solarisbay` |
| Brand / soul | OSCAR | **Solaris** |
| Voice / wake | "OSCAR" | **Solaris** / "Hey Solaris" |
| Home stack + pod | `oscar-household`, `stacks/oscar` | **SolarisBay**, `stacks/solarisbay` |
| Home template dir | `templates/oscar-household` | `templates/solarisbay` |
| Hermes plugin + stack name | `name: oscar`, `~/.hermes/plugins/oscar/` | `name: solarisbay`, `~/.hermes/plugins/solarisbay/` |
| Components (in-stack, bare roles) | `oscar-gatekeeper`, `oscar-household-init`, `oscar-data` | `gatekeeper`, `schema-init`, `solaris-data` |
| Chat pod | `oscar-chat` image, `templates/hermes-chat`, pkg `oscar_chat` | `solaris-chat` image + `templates/solaris-chat` template + pod + `solaris-chat/` source dir, pkg `solaris_chat` |
| Published images (GHCR, brand-prefixed) | `oscar-gatekeeper`, `oscar-household-init`, `oscar-chat`, `oscar-gatekeeper-ml` | `solaris-gatekeeper`, `solaris-schema-init`, `solaris-chat`, `solaris-gatekeeper-ml` |
| Python projects | `oscar-gatekeeper`, `oscar-schema`, `oscar-chat` | `solaris-gatekeeper`, `solaris-schema`, `solaris-chat` |
| Hermes skill names | `oscar-status`, `oscar-audit-query`, `oscar-debug-set`, `oscar-daily-chronicle`, `oscar-dynamic-skills`, `oscar-notes-search`, `oscar-room-enrollment`, `oscar-custom-*` | `solaris-status`, `solaris-audit-query`, `solaris-debug-set`, `solaris-daily-chronicle`, `solaris-dynamic-skills`, `solaris-notes-search`, `solaris-room-enrollment`, `solaris-custom-*` |
| Wyoming program / MCP server names | `oscar-gatekeeper-asr/-tts`, `oscar-gatekeeper-rooms` | `solaris-gatekeeper-asr/-tts`, `solaris-gatekeeper-rooms` |
| Notes namespace (tags + folders) | `#oscar/…`, `oscar/journal`, `oscar/ingested`, `oscar/stub` | `#solaris/…`, `solaris/journal`, `solaris/ingested`, `solaris/stub` |
| HA onboarding account + token file | `oscar` user, `.oscar-long-lived-token` | `solaris` user, `.solaris-long-lived-token` |
| Env vars | `OSCAR_*` | `SOLARIS_*` |
| Data | `oscar.db`, `/var/lib/oscar`, `/opt/data/skills/oscar`, `{{DATA_DIR}}/oscar-household` | `solaris.db`, `/var/lib/solaris`, `/opt/data/skills/solaris`, `{{DATA_DIR}}/solarisbay` |

Inside SolarisBay, in-stack containers keep bare role names; the brand prefix
returns only on published artifacts (GHCR images, Python projects) where they
must be identifiable. Two deliberate stems: home/voice on `solaris`/`solarisbay`
(stack, pod, template dir, plugin, Hermes skill names, voice handle), brand
artifacts on `solaris` (images, Python projects, env vars `SOLARIS_*`, data
paths, notes namespace, Wyoming/MCP program names). The chat pod is the one
brand-prefixed *template* (`solaris-chat`) so it lines up with its
`solaris-chat` image and `solaris-chat/` source dir, alongside the role-named
source dirs (`voice-gatekeeper/`, `database/`).
Unchanged: `hermes`, `hermes-webui` (retired), `ollama`; generic package
`gatekeeper`; `HERMES_*` / `GATEKEEPER_*` / `DEFAULT_UID`. The rename is a
coordinated migration — see #138.

---

*Brand v1.0 — origin: Lem's *Solaris* (1961) over Augustine's soliloquium (c. 386 AD).*
