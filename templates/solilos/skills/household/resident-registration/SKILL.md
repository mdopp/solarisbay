---
name: sol-resident-registration
description: Use when a guest (uid `guest`) has chosen to register as a resident — the registration flow the guest-onboarding greeting (#375) hands off to once the speaker picks "anmelden". Collects the data an account needs (a display name and a derived uid), drives the spoken voice-enrollment (prompts the speaker to say their name across a few sample turns so the gatekeeper can capture and enrol the voice, #386), files a pending resident request via `register_pending_resident` (#376), and confirms the request is in — voice captured, now awaiting admin approval. It files a *request*; it does NOT grant resident access (an admin approves, #355). The speaker stays a guest until then.
version: 1.0.0
author: Solilos
license: MIT
---

# Solilos — Resident Registration (the onboarding hand-off)

## Overview

This is the registration flow that `sol-guest-onboarding` (#375) hands off to: a
guest who heard the register/guest fork chose **"anmelden"**, and now Sol walks
them through becoming a *candidate* resident. It does three things, in order:
collect the account data (a name and a uid), drive the spoken **voice
enrolment**, and file a **pending resident request** for an admin to approve.

It is the conversational layer over two onboarding-only tools (#376/#386):

- **`start_voice_enrollment(uid)`** — opens the enrolment capture for the chosen
  uid. After this call, each turn where the speaker says their name is captured
  by the gatekeeper (it is HA's voice/STT provider) and embedded in-process; the
  engine never sees the audio.
- **`register_pending_resident(uid, display_name)`** — reads the enrolment
  result and, **only on a successful enrol**, files the `pending_residents` row
  for the admin step (#355). A timeout (speaker-ID off) or a failed enrol is
  surfaced honestly: no pending row, no false success.

It does **not** create an account, grant any resident capability, or approve the
request — that is the admin-side provisioning (#355). The flow ends at *"filed,
awaiting approval"*; the speaker remains a guest until an admin says yes.

## When to use

Trigger when **both** hold:

1. The turn's uid is `guest` — the speaker is heard-but-unknown (#353). A turn
   from a recognised resident or an already-identified chat session is **not**
   this flow.
2. The guest has **chosen to register** — they answered the #375 fork with
   "anmelden" / "ja, anmelden" / "ich will mich registrieren", or directly asked
   to become a resident.

**Do not** trigger:

- Before the guest has chosen to register — that is `sol-guest-onboarding`'s
  greeting and fork. This skill picks up *after* the choice.
- For a recognised resident, or to re-enrol an existing one (that is an admin
  re-enrol, not self-registration).
- To approve a request or grant access — this flow only *files* the request.

## Consent first — this captures biometrics + a name

Before opening the enrolment, name what you are about to collect and why. Voice
samples are a biometric identifier and the name is PII; the speaker should know
that before the first sample. Keep it to a sentence, in the household language:

> *"Alles klar — dafür brauche ich deinen Namen, und ich nehme dazu ein paar
> kurze Stimmproben auf, damit ich dich beim nächsten Mal wiedererkenne. Am Ende
> geht die Anfrage zur Freigabe an die Verwaltung — bis dahin bleibst du Gast und
> ich lege noch kein Konto an. Ist das okay für dich?"*

If they decline the recording, don't open the enrolment. You can still note that
registration needs a voice profile and leave them as a guest (the no-commitment
path) — never file a request without the consented capture.

## Operating sequence

### 1. Collect the name and derive a uid

Ask for the name they want to be known by:

> *"Wie heißt du — also welchen Namen soll ich verwenden?"*

From the spoken name, derive a **uid**: lowercase, ASCII letters/digits with
`.`, `_` or `-` (e.g. *"Anna Müller"* → `anna`, or `anna.mueller` if a plainer
`anna` is likely to collide). The uid must match `^[a-z0-9][a-z0-9._-]{0,63}$` —
the tool validates it and returns `invalid_uid` if it doesn't. Don't read the
uid out as a technical token; just confirm the name back warmly:

> *"Schön, dich kennenzulernen, Anna."*

(If the speaker offers a uid/handle themselves, honour it after normalising it to
that shape.)

### 2. Open the enrolment and drive the sample turns

Call **`start_voice_enrollment`** with the uid. On `ok` it returns
`samples_needed` (currently 3) — the number of times the speaker should say their
name so the gatekeeper can average a stable voice profile. Then prompt for each
sample as a **separate turn** (each spoken reply is one captured sample):

> 1. *"Sag bitte einmal deinen Namen."*
> 2. *"Danke — noch einmal, bitte."*
> 3. *"Und ein letztes Mal."*

Each of those turns is a normal voice turn that the gatekeeper captures and
embeds in-process; nothing about the audio is read back or echoed. Keep the
prompts short and friendly; don't explain the embedding mechanics.

If `start_voice_enrollment` returns `invalid_uid`, re-derive the uid (or ask the
speaker for a simpler name) and try once more. If it returns
`enroll_store_unavailable`, the capture backend isn't ready — tell the speaker
honestly that voice registration isn't available right now and leave them as a
guest; don't file a request.

### 3. File the pending request

After the samples, call **`register_pending_resident`** with the uid and the
display name. Handle the result:

- **`ok: true`** (status `pending`) → the voice enrolled and the request is
  filed. Confirm (step 4).
- **`reason: enroll_incomplete`** → fewer than `needed` samples landed; gather
  one more utterance (*"Einmal noch — sag bitte deinen Namen."*) and call
  `register_pending_resident` again.
- **`reason: speaker_id_disabled`** → the gatekeeper never picked up the capture
  because speaker recognition is off (the request timed out). Be honest: voice
  enrolment can't run right now, so the request was **not** filed. See below.
- **`reason: missing_display_name` / `invalid_uid` / `no_enroll_request`** →
  re-collect the missing piece (name or uid) and restart from the step that
  produced it; don't claim a registration that didn't go through.

### 4. Confirm — request filed, awaiting approval

On `ok: true`, close warmly and set the right expectation. Make all three things
explicit: the voice was captured, the request is filed, and approval is still
pending — they are **not yet a resident**:

> *"Super — ich habe deine Stimme aufgenommen und deine Anfrage an die Verwaltung
> geschickt. Sobald sie freigegeben ist, erkenne ich dich als Bewohner:in. Bis
> dahin bist du noch Gast, also merke ich mir noch nichts dauerhaft. Frag mich
> gern weiter, was du möchtest."*

Never imply an account now exists or that they are "now a resident".

## Speaker-ID off — file nothing, say so honestly

Voice enrolment only runs when speaker recognition is active (the gatekeeper
captures the samples). If it's off, `register_pending_resident` returns
`speaker_id_disabled` and files **nothing**. Don't pretend it worked and don't
hang waiting:

> *"Im Moment ist die Sprechererkennung nicht aktiv, deshalb kann ich deine
> Stimme noch nicht aufnehmen — und ohne die Stimmprobe lege ich auch keine
> Anfrage an. Sag der Verwaltung Bescheid, dass du dich anmelden möchtest; sobald
> die Sprechererkennung läuft, machen wir die Anmeldung zusammen fertig."*

Stay a guest turn; keep serving guest-tier requests.

## Guards

- **Files a request, not an account.** Registration is gated on admin approval
  (#355). Never imply the speaker is a resident, or that an account/profile
  exists, before approval.
- **Consent before capture.** Name the biometric + PII collection and get a yes
  before `start_voice_enrollment`. A declined recording means no enrolment and no
  request.
- **No false success.** A timeout (`speaker_id_disabled`) or a failed/incomplete
  enrol files **nothing** — report it honestly and don't claim a filed request.
- **Voice is biometric.** Never read enrolment audio, embeddings, the uid, or any
  uid list aloud. The tools own the samples and never echo them.
- **Stay in the guest tier until approved.** Through the whole flow the speaker
  is still a guest (#353): no notes, memory, timers, or resident data. This flow
  doesn't change that — admin approval does.

## Related

- `#375` / `sol-guest-onboarding` — the greeting + register/guest fork that hands
  off to this flow.
- `#376` / `#386` — the `start_voice_enrollment` + `register_pending_resident`
  tools and the reverse enroll-stash (gatekeeper captures PCM across the sample
  turns and enrols in-process).
- `#355` — admin approval + provisioning that turns a filed request into a
  resident account.
- `#343` — the conversational-onboarding epic this flow completes.
