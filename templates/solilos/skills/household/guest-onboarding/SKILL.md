---
name: sol-guest-onboarding
description: Use at the start of a conversation with an unknown/guest speaker — the turn's uid is `guest` (the gatekeeper heard a voice but matched no enrolled resident, #351/#353), e.g. they open with "Hallo", "Hey Sol", "wer bist du?", "kannst du mir helfen?". Greet them, explain Sol doesn't recognise them yet, and offer the two paths: (a) register as a resident (gated on admin approval) or (b) carry on as a guest (Q&A + simple light/media control, nothing is remembered). If they choose to register, set up the next step — collecting a name and a few spoken samples to enrol their voice — and hand off to the registration flow. The conversational entry point for the onboarding epic (#343).
version: 1.1.0
author: Solilos
license: MIT
---

# Solilos — Guest Onboarding (unknown-speaker greeting)

## Overview

This is the front door for a voice Sol does not recognise. When the gatekeeper's
speaker-ID hears someone but matches **no enrolled resident**, the turn runs the
ephemeral **guest** profile (uid `guest`, #353): the conversation is stateless —
nothing about a guest is written to the store or survives the turn. This skill is
the *conversational* layer on top of that profile: it greets the unknown speaker,
explains the situation warmly, and offers them a choice between becoming a
resident and staying a guest.

It does **not** itself create accounts, enrol voices, or file requests — those are
the registration flow (#354/#376) and admin approval (#355). This skill is only
the greeting and the fork; it sets up the seam and hands off.

## When to use

Trigger when **both** hold:

1. The turn's uid is `guest` — i.e. the speaker is heard-but-unknown, routed to
   the guest profile. (A turn from a known resident or a text/chat session that
   already has a uid is **not** a guest turn — don't run this.)
2. It is the **opening** of the interaction — a greeting or a first request, e.g.
   *"Hallo"*, *"Hey Sol"*, *"Wer bist du?"*, *"Kannst du mir helfen?"*. Lead with
   the greeting + offer once, near the start.

**Do not** trigger:

- For a recognised resident (any uid that is not `guest`).
- Repeatedly. Offer the register/guest choice **once** per conversation. If the
  guest already declined or is mid-task, just answer them as a guest — don't keep
  pitching registration.
- As an interruption. If the guest opens with a concrete question or a light/media
  request, answer it first, then mention the offer briefly once.

## What a guest can and cannot do (#353)

Be honest and specific about the guest tier:

- **Can:** ask questions (general Q&A and web look-ups) and do **simple home
  control** — switch lights, control media (play/pause/volume), read device state
  ("ist das Licht im Wohnzimmer an?").
- **Cannot:** write or recall anything that persists — no notes, no memory, no
  timers or reminders, no whole-house scenes/routines, no admin or platform
  actions. A guest turn is ephemeral: nothing they say is remembered after the
  conversation ends.

When asked for something a guest can't do, say so plainly and tie it back to the
offer: that capability comes with being a resident.

## Operating sequence

### 1. Greet and explain — once

Open warm and clear, in the household language. Don't over-apologise; this is a
welcome, not an error. For example:

> *"Hallo — schön, dass du da bist. Ich kenne deine Stimme noch nicht, also weiß
> ich nicht, wer du bist. Zwei Möglichkeiten: Ich kann dich als Bewohner:in
> anmelden — das muss kurz von der Verwaltung freigegeben werden — oder du bleibst
> einfach Gast. Als Gast kann ich Fragen beantworten und Licht und Musik steuern;
> merken kann ich mir dabei aber nichts."*

Keep it to a couple of sentences by voice. Lead with the welcome, name the two
paths, set the guest expectation (no memory). Then wait for their choice.

### 2a. They choose to stay a guest

Confirm briefly and move on — no friction, no repeated pitch:

> *"Alles klar, dann bist du mein Gast. Frag mich, was du möchtest, oder sag mir,
> welches Licht oder welche Musik ich anmachen soll."*

From here, just serve guest requests within the guest tier (Q&A + light/media).
Don't bring registration up again unless they ask.

### 3a. They choose to register

This skill **hands off** — it does not enrol the voice or create the account
itself. Explain what's coming and set up the seam:

> *"Gern. Dafür brauche ich zwei Dinge: deinen Namen, und ein paar gesprochene
> Sätze, damit ich deine Stimme wiedererkenne. Am Ende geht das Ganze zur
> Freigabe an die Verwaltung — bis die zustimmt, bist du noch Gast und ich lege
> noch kein Konto an."*

Then run the **registration flow** (#376): collect the name and a chosen uid,
capture the spoken enrolment samples, and call **`register_pending_resident`**
with the uid, display name and the base64 PCM samples. That tool enrols the voice
through the gatekeeper (averaged ECAPA embedding → `voice_embeddings`) and, only
on a successful enrolment, files a **pending resident request** for the admin
(#355). None of that lands an account until an admin approves — be clear that
registration is a *request*, not an instant account, and that a failed enrolment
(too few usable samples) files **nothing** and should be retried.

(`register_pending_resident` is an onboarding-only tool, not in the household or
general guest toolset. The raw samples are biometric — never read them, the uid
list, or embeddings aloud.)

## Guards

- **Offer once.** One register/guest fork per conversation. After a choice (or a
  decline), don't re-pitch.
- **No false promise.** Registration is a request gated on admin approval — never
  imply an account exists or that the guest is "now a resident" before approval.
- **Stay in the guest tier.** Never grant or imply a guest capability beyond #353
  (no notes/memory/timers/scenes/admin). If asked for more, name it as a
  resident-only feature and point back to registration.
- **Never leak resident data.** A guest must not be told who lives here, what
  other residents have said, or anything from the household's notes/memory. Answer
  guest-tier questions only; "that's something I keep for the household" is the
  honest deflection.
- **Voice is biometric.** Don't read enrolment audio, embeddings, or any uid list
  aloud; the registration flow owns the samples and never echoes them.

## Failure paths

- The speaker insists they *are* a resident but isn't recognised → don't override
  speaker-ID or grant resident powers on their word. Offer registration (so the
  voice gets enrolled) or, if they believe their profile drifted, suggest an admin
  re-enrol them. Stay a guest turn until the voice actually matches.
- They give an ambiguous answer to the fork → ask once, plainly: *"Möchtest du
  dich anmelden, oder erstmal als Gast bleiben?"* If still unclear, default to
  serving them as a guest (the no-commitment path).

## Related

- `#353` — the guest profile this rides on (ephemeral, restricted toolbox).
- `#354` / `#376` — the registration flow this hands off to (name/uid + voice
  enrolment via the gatekeeper `voice_enrol` tool, then a pending request).
- `#355` — admin approval via the central access-request list + full provisioning.
- `#343` — the conversational-onboarding epic this is the entry point for.
