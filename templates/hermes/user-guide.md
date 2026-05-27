---
lucide_icon: "bot"
tagline: "A self-improving AI agent that lives on your home server — chat with it on Signal / Telegram / Discord, or via the web dashboard."
recommended_apps:
  - name: "Signal"
    url: "https://signal.org/"
    platforms: ["ios", "android", "desktop"]
    note: "Chat with Hermes from any device — the agent runs your local LLM, the chat is end-to-end encrypted on the messaging side. Set up the Signal gateway in the dashboard."
  - name: "Telegram"
    url: "https://telegram.org/"
    platforms: ["ios", "android", "desktop"]
    note: "Same idea as Signal — talk to Hermes from a chat app you already use. Bridge configured under Gateways in the dashboard."
---

# Getting started with Hermes

Hermes is an autonomous agent — it remembers conversations, can run
small scripts, and talks to whichever LLM you point it at. By default
it points at the **Ollama** container on this server, so everything
stays local: no cloud LLM bills, no chats leaving the house.

## Open the dashboard

The *Open* button on this card lands at the Hermes web dashboard.
That's where you:

- pick which LLM model to use (Ollama models you've pulled show up
  in the dropdown),
- bridge chat platforms (Signal / Telegram / Discord) so you can
  message the agent from your phone,
- see the agent's memory + recent thoughts,
- get an API key for scripts that want to talk to Hermes directly.

## Bridging a chat platform

The most common setup is **Signal**:

1. In the dashboard, open *Gateways → Signal*.
2. The dashboard walks you through linking a phone number Hermes can
   use. Use a number that isn't your personal one — a Twilio number
   or a free secondary SIM works well.
3. Once linked, send Hermes a message from your own Signal account.
   It'll reply within a few seconds.

The same flow works for Telegram and Discord — different chat app,
same gateway pattern.

## Picking an LLM

Hermes defaults to **`gemma3:4b`** on Ollama — small enough to run on
a homelab CPU, smart enough to handle daily-driver conversations.
If you have a GPU and want more capable answers, pull a bigger model
from the *Models* page on the Ollama portal card and switch Hermes
to it in *Settings → LLM Provider*.

## Privacy

Everything Hermes processes stays on this server:

- conversation history sits in the local SQLite under
  `/mnt/data/stacks/hermes`,
- LLM inference runs in the Ollama container (also local),
- the chat-platform bridge only talks to Signal/Telegram/Discord
  on outbound traffic.

No analytics, no third-party telemetry — the agent's own logs are
the only record.
