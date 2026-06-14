---
name: solaris-self-enrollment
description: Use when a household-tier speaker asks to set themselves up / enrol their own voice — phrases like "Setup starten", "richte mich ein", "enrolle meine Stimme", "merk dir meine Stimme", "ich möchte mich anmelden". This is the FIRST-RUN / owner entry point (#396): with zero enrolments an unknown speaker resolves to `household` (not `guest`, #351), so the guest-onboarding greeting (#375) never fires for the first person — yet they still need to bootstrap a voice profile. Collects a name + derived uid, drives the spoken 3-sample voice enrolment (the gatekeeper captures the audio across the sample turns, #386), and files a `pending_residents` request via `register_pending_resident` (#376). It files a *request*; it does NOT grant resident access — an admin approves in the browser admin UI (#355). Same admin-approval gate as the guest path, just initiated by voice instead of off the guest greeting.
version: 1.0.0
author: Solaris
license: MIT
---

# Solaris — Self-Enrollment (the spoken "Setup starten" entry point)

## Overview

This is the **self-service** voice-enrolment flow: a household-tier speaker asks
to set themselves up, and Solaris walks them through enrolling a voice profile and
filing a resident request. It is the first-run / owner counterpart to the
guest-onboarding greeting (#375): with **zero** enrolments an unknown speaker is
resolved to `household` (not `guest`, #351), so the guest greeting never fires
for the first person — but they still need a way to bootstrap their voice. That
way is this skill, triggered by an explicit spoken request.

It rides the same two onboarding tools as the guest registration flow (#376/#386):

- **`start_voice_enrollment(uid)`** — opens the enrolment capture for the chosen
  uid. After this call, each turn where the speaker says their name is captured
  by the gatekeeper (it is HA's voice/STT provider) and embedded in-process; the
  engine never sees the audio.
- **`register_pending_resident(uid, display_name)`** — reads the enrolment result
  and, **only on a successful enrol**, files the `pending_residents` row for the
  admin step (#355). A timeout (speaker-ID off) or a failed enrol is surfaced
  honestly: no pending row, no false success.

It does **not** create an account, grant any resident capability, or approve the
request — that is the admin-side provisioning (#355), and the user has chosen
**always admin-approve** even for the first resident (no auto-admin). The flow
ends at *"filed, awaiting approval"*.

## When to use

Trigger when the speaker **explicitly asks to set themselves up / be recognised
by voice**, e.g.:

> *"Setup starten"*, *"richte mich ein"*, *"enrolle meine Stimme"*, *"merk dir
> meine Stimme"*, *"ich möchte mich anmelden"*, *"kannst du mich anlegen?"*

**Do not** trigger:

- For a `guest` turn — that is `solaris-guest-onboarding` (#375) → `resident-registration`.
  This skill is the `household`-initiated path. (If the turn is `guest`, use the
  guest flow.)
- For a speaker who is **already an enrolled, approved resident** asking general
  things — re-enrolling an existing resident is an admin re-enrol, not this.
- To approve a request or grant access — this flow only *files* the request.
- On an off-hand mention of "setup" mid-task — only on a clear request to enrol
  *themselves* by voice.

## Consent first — this captures biometrics + a name

Before opening the enrolment, name what you are about to collect and why. Voice
samples are a biometric identifier and the name is PII; the speaker should know
that before the first sample. Keep it to a sentence, in the household language:

> *"Klar — dafür nehme ich ein paar kurze Stimmproben auf, damit ich dich beim
> nächsten Mal an der Stimme erkenne, und ich brauche einen Namen. Die Anfrage
> geht danach zur Freigabe an die Verwaltung — bis dahin lege ich noch kein Konto
> an. Ist das okay für dich?"*

If they decline the recording, don't open the enrolment — leave it there; never
file a request without the consented capture.

## Operating sequence

### 1. Collect the name and derive a uid

Ask for the name they want to be known by:

> *"Wie heißt du — welchen Namen soll ich verwenden?"*

From the spoken name, derive a **uid**: lowercase, ASCII letters/digits with `.`,
`_` or `-` (e.g. *"Michael"* → `michael`, or `michael.dopp` if a plainer
`michael` is likely to collide). The uid must match `^[a-z0-9][a-z0-9._-]{0,63}$`
— the tool validates it and returns `invalid_uid` if it doesn't. Don't read the
uid out as a technical token; confirm the name back warmly:

> *"Schön, Michael."*

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

If `start_voice_enrollment` returns `invalid_uid`, re-derive the uid (or ask for
a simpler name) and try once more. If it returns `enroll_store_unavailable`, the
capture backend isn't ready — tell the speaker honestly that voice enrolment
isn't available right now; don't file a request.

### 3. File the pending request

After the samples, call **`register_pending_resident`** with the uid and the
display name. Handle the result:

- **`ok: true`** (status `pending`) → the voice enrolled and the request is
  filed. Confirm (step 4).
- **`reason: enroll_incomplete`** → fewer than `needed` samples landed; gather one
  more utterance (*"Einmal noch — sag bitte deinen Namen."*) and call
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

> *"Super — ich habe deine Stimme aufgenommen und deine Anfrage zur Freigabe
> geschickt. Sie muss noch von der Verwaltung bestätigt werden; das kannst du
> selbst im Admin-Bereich im Browser machen. Sobald sie freigegeben ist, erkenne
> ich dich an der Stimme als Bewohner:in."*

Never imply an account now exists or that they are "now a resident".

## Speaker-ID off — file nothing, say so honestly

Voice enrolment only runs when speaker recognition is active (the gatekeeper
captures the samples). If it's off, `register_pending_resident` returns
`speaker_id_disabled` and files **nothing**. Don't pretend it worked and don't
hang waiting:

> *"Im Moment ist die Sprechererkennung nicht aktiv, deshalb kann ich deine
> Stimme noch nicht aufnehmen — und ohne die Stimmprobe lege ich auch keine
> Anfrage an. Sobald die Sprechererkennung läuft, machen wir das zusammen
> fertig."*

## Guards

- **Files a request, not an account.** Registration is gated on admin approval
  (#355), first resident included (no auto-admin). Never imply the speaker is a
  resident, or that an account/profile exists, before approval.
- **Consent before capture.** Name the biometric + PII collection and get a yes
  before `start_voice_enrollment`. A declined recording means no enrolment and no
  request.
- **No false success.** A timeout (`speaker_id_disabled`) or a failed/incomplete
  enrol files **nothing** — report it honestly and don't claim a filed request.
- **Voice is biometric.** Never read enrolment audio, embeddings, the uid, or any
  uid list aloud. The tools own the samples and never echo them.
- **One enrolment at a time.** Don't open a second `start_voice_enrollment` while
  one is mid-capture for the same speaker — finish or let it time out.

## Related

- `#396` — this first-run/owner self-enrolment gap (household ≠ guest).
- `#375` / `solaris-guest-onboarding` — the guest greeting + fork (the parallel path
  for heard-but-below-threshold speakers).
- `solaris-resident-registration` — the guest-side registration flow this mirrors.
- `#376` / `#386` — the `start_voice_enrollment` + `register_pending_resident`
  tools and the reverse enroll-stash (gatekeeper captures PCM across the sample
  turns and enrols in-process).
- `#355` — admin approval + provisioning that turns a filed request into a
  resident account.
- `#343` — the conversational-onboarding epic.
