---
name: solaris-resident-registration
description: On the onboarding hand-off (a guest chose to register), collect name+uid, drive spoken voice enrolment, and file a pending resident request for admin approval.
kind: hook
scope: household
event: registration-handoff
version: 2.0.0
author: Solaris
license: MIT
---

# Solaris — Resident Registration (the onboarding hand-off)

**Binds:** `registration-handoff` (a guest who heard the register/guest fork in
`guest-onboarding` chose "anmelden")

Walk the guest through becoming a *candidate* resident: collect the account data,
drive the spoken voice enrolment, and file a **pending request** for an admin to
approve. It files a *request* — it does **not** create an account or grant
resident access (#355). The speaker stays a guest until an admin approves.

It is the conversational layer over two onboarding-only tools:
- **`start_voice_enrollment(uid)`** — opens the capture; each subsequent turn where
  the speaker says their name is captured and embedded by the gatekeeper in-process
  (the engine never sees audio). Returns `samples_needed` (3).
- **`register_pending_resident(uid, display_name)`** — files the `pending_residents`
  row **only on a successful enrol**; a timeout / failed enrol is surfaced honestly.

## Consent first — this captures biometrics + a name

Before opening enrolment, name what you collect and why, and get a yes:

> *"Alles klar — dafür brauche ich deinen Namen, und ich nehme ein paar kurze
> Stimmproben auf, damit ich dich wiedererkenne. Am Ende geht die Anfrage zur
> Freigabe an die Verwaltung — bis dahin bleibst du Gast. Ist das okay?"*

If they decline the recording, don't open enrolment and file nothing.

## What to do on the event

### 1. Collect the name, derive a uid
Ask for the name. Derive a uid (lowercase ASCII letters/digits with `.`/`_`/`-`,
matching `^[a-z0-9][a-z0-9._-]{0,63}$`; "Anna Müller" → `anna` or `anna.mueller`
if `anna` would collide). Confirm warmly (*"Schön, dich kennenzulernen, Anna."*);
don't read the uid out as a token. Honour a uid the speaker offers after
normalising it.

### 2. Open enrolment + drive the sample turns
Call **`start_voice_enrollment`** with the uid; it returns `samples_needed` (3).
Prompt for each sample as a **separate turn** (each reply = one captured sample):

> 1. *"Sag bitte einmal deinen Namen."*
> 2. *"Danke — noch einmal, bitte."*
> 3. *"Und ein letztes Mal."*

Nothing about the audio is read back. On `invalid_uid`, re-derive and retry once.
On `enroll_store_unavailable`, say voice registration isn't available right now,
leave them a guest, file nothing.

### 3. File the pending request
Call **`register_pending_resident`** with the uid + display name:
- **`ok: true`** (status `pending`) → enrolled and filed; confirm (step 4).
- **`enroll_incomplete`** → gather one more utterance and call again.
- **`speaker_id_disabled`** → speaker recognition is off; the request timed out and
  **nothing** was filed — say so honestly (see below).
- **`missing_display_name` / `invalid_uid` / `no_enroll_request`** → re-collect the
  missing piece and restart from there; don't claim a registration that didn't go.

### 4. Confirm — filed, awaiting approval
On `ok: true`, make all three explicit: voice captured, request filed, approval
still pending — they are **not yet a resident**:

> *"Super — ich habe deine Stimme aufgenommen und deine Anfrage an die Verwaltung
> geschickt. Sobald sie freigegeben ist, erkenne ich dich als Bewohner:in. Bis
> dahin bist du noch Gast."*

## Speaker-ID off — file nothing, say so

If `register_pending_resident` returns `speaker_id_disabled`, nothing was filed.
Don't pretend it worked or hang waiting:

> *"Im Moment ist die Sprechererkennung nicht aktiv, deshalb kann ich deine Stimme
> noch nicht aufnehmen — und ohne Stimmprobe lege ich keine Anfrage an. Sag der
> Verwaltung Bescheid; sobald die Sprechererkennung läuft, machen wir es fertig."*

## Guards

- **Files a request, not an account**: never imply the speaker is a resident before
  approval.
- **Consent before capture**: a declined recording means no enrolment, no request.
- **No false success**: a timeout/failed/incomplete enrol files nothing.
- **Voice is biometric**: never read enrolment audio, embeddings, or the uid aloud.
- **Stay in the guest tier until approved** (no notes/memory/timers/resident data).
