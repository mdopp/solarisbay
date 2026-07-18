"""Solaris gatekeeper — voice-pipeline orchestrator.

Listens for Wyoming-protocol connections from satellites (HA Voice PE or
wyoming-satellite clients), drives whisper for STT and piper for TTS, and
hands off the conversation step to the Solaris Engine.

Spec: gatekeeper/README.md
"""

__version__ = "0.1.0"
