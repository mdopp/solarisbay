"""Shared test setup for the gatekeeper suite.

gatekeeper.config initializes a singleton `settings = Settings.from_env()`
at import; every setting has an env default, so no seeding is needed —
this file exists for future shared fixtures.
"""

from __future__ import annotations
