---
name: solaris-self-enrollment
description: When a household-tier speaker asks to set themselves up by voice, drive the 3-sample enrolment and file a pending resident request.
kind: hook
scope: household
event: self-enroll-request
version: 2.0.0
author: Solaris
license: MIT
---

# Solaris — Self-Enrollment (the spoken "Setup starten" entry point)

**Binds:** `self-enroll-request` (a `household`-tier speaker explicitly asks to set
themselves up / enrol their own voice — *"Setup starten"*, *"richte mich ein"*,
*"enrolle meine Stimme"*, *"ich möchte mich anmelden"*, *"kannst du mich anlegen?"*)

The first-run / owner entry point (#396): with zero enrolments an unknown speaker
resolves to `household` (not `guest`, #351), so the guest greeting never fires for
the first person — yet they still need to bootstrap a voice profile. This is the
`household`-initiated counterpart to the guest path; it files a *request* and does
**not** grant resident access (an admin approves, even for the first resident).

Rides the same two onboarding tools:
- **`start_voice_enrollment(uid)`** — opens the capture; each subsequent turn where
  the speaker says their name is captured + embedded by the gatekeeper in-process
  (the engine never sees audio). Returns `samples_needed` (3).
- **`register_pending_resident(uid, display_name)`** — files `pending_residents`
  **only on a successful enrol**; a timeout / failed enrol is surfaced honestly.

Do **not** run for a `guest` turn (that is `guest-onboarding` →
`resident-registration`), to re-enrol an already-approved resident (admin re-enrol),
or on an off-hand mention of "setup" mid-task.

## Consent first — this captures biometrics + a name

Name what you collect and why, and get a yes before opening enrolment:

> *"Klar — dafür nehme ich ein paar kurze Stimmproben auf, damit ich dich an der
> Stimme erkenne, und ich brauche einen Namen. Die Anfrage geht danach zur Freigabe
> an die Verwaltung — bis dahin lege ich kein Konto an. Ist das okay?"*

If they decline the recording, don't open enrolment and file nothing.

## What to do on the event

### 1. Collect the name, derive a uid
Ask for the name. Derive a uid (lowercase ASCII letters/digits with `.`/`_`/`-`,
matching `^[a-z0-9][a-z0-9._-]{0,63}$`; "Michael" → `michael` or `michael.dopp` if
`michael` would collide). Confirm warmly (*"Schön, Michael."*); don't read the uid
as a token. Honour a uid the speaker offers after normalising it.

### 2. Open enrolment + drive the sample turns
Call **`start_voice_enrollment`** with the uid; it returns `samples_needed` (3).
Prompt for each sample as a **separate turn** (each reply = one captured sample):

> 1. *"Sag bitte einmal deinen Namen."*
> 2. *"Danke — noch einmal, bitte."*
> 3. *"Und ein letztes Mal."*

Nothing about the audio is read back. On `invalid_uid`, re-derive and retry once.
On `enroll_store_unavailable`, say voice enrolment isn't available right now and
file nothing.

### 3. File the pending request
Call **`register_pending_resident`** with the uid + display name:
- **`ok: true`** (status `pending`) → enrolled and filed; confirm (step 4).
- **`enroll_incomplete`** → gather one more utterance and call again.
- **`speaker_id_disabled`** → speaker recognition is off; the request timed out and
  **nothing** was filed — say so honestly (see below).
- **`missing_display_name` / `invalid_uid` / `no_enroll_request`** → re-collect the
  missing piece and restart from there.

### 4. Confirm — filed, awaiting approval
On `ok: true`, make all three explicit: voice captured, request filed, approval
pending — they are **not yet a resident**:

> *"Super — ich habe deine Stimme aufgenommen und deine Anfrage zur Freigabe
> geschickt. Sie muss noch von der Verwaltung bestätigt werden — das kannst du im
> Admin-Bereich im Browser machen. Sobald sie freigegeben ist, erkenne ich dich an
> der Stimme als Bewohner:in."*

## Speaker-ID off — file nothing, say so

If `register_pending_resident` returns `speaker_id_disabled`, nothing was filed:

> *"Im Moment ist die Sprechererkennung nicht aktiv, deshalb kann ich deine Stimme
> noch nicht aufnehmen — und ohne Stimmprobe lege ich keine Anfrage an. Sobald sie
> läuft, machen wir das zusammen fertig."*

## Guards

- **Files a request, not an account** (no auto-admin, first resident included).
- **Consent before capture**: a declined recording means no enrolment, no request.
- **No false success**: a timeout/failed/incomplete enrol files nothing.
- **Voice is biometric**: never read enrolment audio, embeddings, or the uid aloud.
- **One enrolment at a time** per speaker.
