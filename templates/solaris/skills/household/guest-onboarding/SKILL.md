---
name: solaris-guest-onboarding
description: On an unknown-speaker (guest) session start, greet them and offer the register-as-resident vs stay-a-guest fork.
kind: hook
scope: household
event: guest-session-start
version: 2.0.0
author: Solaris
license: MIT
---

# Solaris — Guest Onboarding (unknown-speaker greeting)

**Binds:** `guest-session-start` (the gatekeeper heard a voice but matched no
enrolled resident — the turn runs the ephemeral `guest` profile, uid `guest`)

The front door for a voice Solaris does not recognise. Greet the unknown speaker
once, explain the situation warmly, and offer the fork: register as a resident
(gated on admin approval) or stay a guest. This skill only greets and hands off —
it does not create accounts or enrol voices itself.

## What a guest can / cannot do

- **Can:** ask questions (Q&A + web look-ups) and do simple home control —
  lights, media (play/pause/volume), read device state.
- **Cannot:** anything that persists — no notes, memory, timers, scenes, or
  admin/platform actions. A guest turn is ephemeral; nothing is remembered.

## What to do on the event

### 1. Greet + offer the fork — once
Warm and clear, in the household language; this is a welcome, not an error:

> *"Hallo — schön, dass du da bist. Ich kenne deine Stimme noch nicht. Zwei
> Möglichkeiten: Ich kann dich als Bewohner:in anmelden — das muss kurz von der
> Verwaltung freigegeben werden — oder du bleibst Gast. Als Gast kann ich Fragen
> beantworten und Licht und Musik steuern; merken kann ich mir dabei nichts."*

Offer this **once** per conversation. If the guest opens with a concrete request,
answer it first, then mention the offer briefly.

### 2a. Stays a guest
Confirm and move on; serve guest-tier requests, don't re-pitch:

> *"Alles klar, dann bist du mein Gast. Frag mich, was du möchtest."*

### 2b. Chooses to register — hand off
Explain what's coming, then run the registration dialog:

> *"Gern. Dafür brauche ich deinen Namen und ein paar gesprochene Sätze, damit ich
> deine Stimme wiedererkenne. Am Ende geht das zur Freigabe an die Verwaltung —
> bis die zustimmt, bist du noch Gast und ich lege kein Konto an."*

1. Collect a name + chosen uid, call **`start_voice_enrollment`** with the uid
   (returns the sample count needed, 3).
2. Guide the speaker through **three short utterances**, one per turn:
   *"Sag bitte deinen Namen."* → *"Danke. Noch einmal."* → *"Und ein letztes Mal."*
3. After the third, call **`register_pending_resident`** with the uid + display
   name. On a successful enrol it files a pending resident request for the admin;
   nothing lands an account until an admin approves.

Honest-failure paths the tool returns:
- `speaker_id_disabled` (timeout — speaker recognition is off): say enrolment needs
  active speaker recognition; offer to carry on as a guest, don't retry in a loop.
- `enroll_incomplete`: fewer than the needed utterances captured — prompt for one more.
- any other failure: report it, file nothing, offer to retry.

## Guards

- **Offer once** per conversation; after a choice or decline, don't re-pitch.
- **No false promise**: registration is a *request* gated on admin approval.
- **Stay in the guest tier**; never grant a resident-only capability.
- **Never leak resident data** to a guest (who lives here, others' notes/memory).
- **Voice is biometric**: never read enrolment audio, embeddings, or a uid list
  aloud; the engine never sees the raw audio.
