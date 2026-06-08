"""Solilos Hermes plugin entrypoint.

Registers the bundled household skills (templates/solilos/skills/household/)
with Hermes when this repo is cloned into ~/.hermes/plugins/solbay/ via
Hermes' "Install from URL" flow.

NOT used when Solilos is deployed via ServiceBay (mdopp/solbay registry).
In that case ServiceBay's asset transport ships the merged solilos template's
skills/household/ to Hermes' bind-mount target at /opt/data/skills/solilos/,
and Hermes' built-in skill loader picks them up from the filesystem.

Hermes plugin contract:
  https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins
"""

from __future__ import annotations
from pathlib import Path


_SKILLS_ROOT = Path(__file__).parent / "templates" / "solilos" / "skills" / "household"
_HERMES_BINDMOUNT = Path("/opt/data/skills/solilos")


def _already_loaded_via_bindmount() -> bool:
    """Detect the ServiceBay-managed deployment case so we don't double-
    register the same skills. If Hermes is running inside the Solilos pod
    with the SB-managed bind-mount in place, the skill files are
    already discoverable at /opt/data/skills/solilos/ — no need (and a
    potential conflict) to also register them from this plugin path.
    """
    if not _HERMES_BINDMOUNT.is_dir():
        return False
    try:
        return any(_HERMES_BINDMOUNT.iterdir())
    except OSError:
        return False


def on_load(ctx):
    """Called by Hermes when the plugin is loaded.

    Walks templates/solilos/skills/household/ and registers each
    `SKILL.md` via ctx.register_skill(name, path). Skill names are the
    immediate-subdirectory names (audit-query, debug-set, …).
    """
    if _already_loaded_via_bindmount():
        # ServiceBay-deployed Hermes: the bind-mount path is already
        # populated and Hermes' built-in loader scans it. Skip plugin-
        # path registration to avoid duplicate-skill warnings.
        return

    if not _SKILLS_ROOT.is_dir():
        return

    for entry in sorted(_SKILLS_ROOT.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith("."):
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        ctx.register_skill(entry.name, str(skill_md))
