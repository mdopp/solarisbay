---
name: solaris-enrollment
description: On an unknown-speaker (guest) session start, or when someone asks to set themselves up by voice, greet + offer the register-vs-guest fork, drive the spoken voice enrolment, and file a pending resident request for admin approval.
kind: hook
scope: household
event: guest-session-start
version: 3.0.0
author: Solaris
license: MIT
---

# Solaris — Enrollment (the voice-onboarding flow)

**Binds:** `guest-session-start` (the front door — the gatekeeper heard a voice but
matched no enrolled resident, so the turn runs the ephemeral `guest` profile,
uid `guest`).

One flow, three entry points: greet an unknown speaker and offer the fork
(register vs stay a guest), drive the spoken voice enrolment, and file a **pending
request** for an admin to approve. Enrolment files a *request* — it does **not**
create an account or grant resident access (#355). The speaker stays a guest until
an admin approves, first resident included.

It is the conversational layer over two onboarding-only tools; **the tools drive
the wording** — follow what they return, don't script the samples from prose:
- **`start_voice_enrollment(uid)`** — opens the capture and returns a `say` field:
  speak that line **verbatim**. Each subsequent turn is captured + embedded by the
  gatekeeper in-process (the engine never sees audio). Returns `samples_needed` (3).
- **`register_pending_resident(uid, display_name)`** — files the `pending_residents`
  row **only on a successful enrol** (#376); a timeout / failed / incomplete enrol
  is surfaced honestly — no pending row, no false success.

## Entry points

### 1. Unknown speaker (this event) — greet + offer the fork, once
Warm and clear, in the household language; this is a welcome, not an error:

> *"Hallo — schön, dass du da bist. Ich kenne deine Stimme noch nicht. Zwei
> Möglichkeiten: Ich kann dich als Bewohner:in anmelden — das muss kurz von der
> Verwaltung freigegeben werden — oder du bleibst Gast. Als Gast kann ich Fragen
> beantworten und Licht und Musik steuern; merken kann ich mir dabei nichts."*

Offer this **once** per conversation. If the guest opens with a concrete request,
answer it first, then mention the offer briefly.

- **Stays a guest:** confirm and serve guest-tier requests, don't re-pitch —
  *"Alles klar, dann bist du mein Gast. Frag mich, was du möchtest."*
- **Chooses to register:** run the consent + enrolment flow below.

### 2. Household-tier "Setup starten" (the first-run / owner path, #396)
With zero enrolments an unknown speaker resolves to `household`, not `guest` (#351),
so the greeting above never fires for the *first* person — yet they still need to
bootstrap a voice profile. When a `household`-tier speaker explicitly asks to set
themselves up (*"Setup starten"*, *"richte mich ein"*, *"enrolle meine Stimme"*,
*"ich möchte mich anmelden"*, *"kannst du mich anlegen?"*), run the same flow.

Do **not** run it for an off-hand mention of "setup" mid-task, to re-enrol an
already-approved resident (that's an admin re-enrol), or when the speaker declines.

## What a guest can / cannot do

- **Can:** ask questions (Q&A + web look-ups) and simple home control — lights,
  media (play/pause/volume), read device state.
- **Cannot:** anything that persists — no notes, memory, timers, scenes, or
  admin/platform actions. A guest turn is ephemeral; nothing is remembered.

## Consent first — this captures biometrics + a name

Before opening enrolment, name what you collect and why, and get a yes:

> *"Gern — dafür brauche ich deinen Namen, und ich nehme ein paar kurze Stimmproben
> auf, damit ich dich wiedererkenne. Am Ende geht die Anfrage zur Freigabe an die
> Verwaltung — bis dahin bleibst du Gast und ich lege kein Konto an. Ist das okay?"*

If they decline the recording, don't open enrolment and file nothing.

## The flow — collect, capture, file

### 1. Collect the name, derive a uid
Ask for the name. Derive a uid yourself (lowercase ASCII letters/digits with
`.`/`_`/`-`, matching `^[a-z0-9][a-z0-9._-]{0,63}$`; "Anna Müller" → `anna`, or
`anna.mueller` if `anna` would collide). Confirm warmly
(*"Schön, dich kennenzulernen, Anna."*); never read the uid out as a token. Honour
a uid the speaker offers after normalising it.

### 2. Open enrolment + drive the sample turns
Call **`start_voice_enrollment`** with the uid. It returns a `say` line — **speak
it verbatim** and do NOT ask the speaker to repeat their name; the content of the
utterances is irrelevant, only the sound of the voice matters. Each following turn
is one captured sample; three are needed. On `invalid_uid`, re-derive and retry
once; on `enroll_store_unavailable`, say voice enrolment isn't available right now,
leave them a guest, file nothing.

### 3. File the pending request
After the third utterance, call **`register_pending_resident`** with the uid +
display name:
- **`ok: true`** (status `pending`) → enrolled and filed; confirm (step 4).
- **`enroll_incomplete`** → gather one more utterance and call again.
- **`speaker_id_disabled`** → speaker recognition is off; the request timed out and
  **nothing** was filed — say so honestly (see below).
- **`enroll_failed`** → the gatekeeper couldn't extract a voice embedding; report
  it, file nothing, offer to retry.
- **`missing_display_name` / `invalid_uid` / `no_enroll_request`** → re-collect the
  missing piece and restart from there; don't claim a registration that didn't go.

### 4. Confirm — filed, awaiting approval
On `ok: true`, make all three explicit: voice captured, request filed, approval
still pending — they are **not yet a resident**:

> *"Super — ich habe deine Stimme aufgenommen und deine Anfrage an die Verwaltung
> geschickt. Sobald sie freigegeben ist — das geht im Admin-Bereich im Browser —
> erkenne ich dich an der Stimme als Bewohner:in. Bis dahin bist du noch Gast."*

## Speaker-ID off — file nothing, say so

If `register_pending_resident` returns `speaker_id_disabled`, nothing was filed.
Don't pretend it worked or hang waiting:

> *"Im Moment ist die Sprechererkennung nicht aktiv, deshalb kann ich deine Stimme
> noch nicht aufnehmen — und ohne Stimmprobe lege ich keine Anfrage an. Sag der
> Verwaltung Bescheid; sobald die Sprechererkennung läuft, machen wir es fertig."*

## Guards

- **Offer once** per conversation; after a choice or decline, don't re-pitch.
- **Files a request, not an account**: never imply the speaker is a resident before
  approval (first resident included, no auto-admin).
- **Consent before capture**: a declined recording means no enrolment, no request.
- **No false success**: a timeout / failed / incomplete enrol files nothing.
- **Stay in the guest tier until approved**: never grant a resident-only capability,
  and never leak resident data to a guest (who lives here, others' notes/memory).
- **Voice is biometric**: never read enrolment audio, embeddings, or a uid list
  aloud; the engine never sees the raw audio.
- **One enrolment at a time** per speaker.
