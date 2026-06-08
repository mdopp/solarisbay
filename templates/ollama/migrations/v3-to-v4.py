#!/usr/bin/env python3
"""
Migration: ollama v3 → v4.

This is a config/pod-only hop, no on-disk data migration.
"""

from __future__ import annotations

import sys


def main() -> int:
    print("Ollama v3 → v4: config/pod-only hop, no on-disk data migration.")
    print("  - OLLAMA_CONTEXT_LENGTH default lowered to 32768 (#214).")
    print("  - Added OLLAMA_FLASH_ATTENTION (default 1).")
    print("  - Added OLLAMA_EMBED_MODEL (default nomic-embed-text), pre-pulled.")
    print("  Nothing to move or transform on disk; proceeding.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
