"""OSCAR Hermes plugin entrypoint.

Registers the bundled skills (templates/oscar-household/skills/) with
Hermes when this repo is cloned into ~/.hermes/plugins/oscar/ via
Hermes' "Install from URL" flow.

NOT used when OSCAR is deployed via ServiceBay (mdopp/oscar registry).
In that case the oscar-household template's post-deploy.py copies the
skills into Hermes' bind-mount target at /opt/data/skills/oscar/, and
Hermes' built-in skill loader picks them up from the filesystem.

Hermes plugin contract:
  https://hermes-agent.nousresearch.com/docs/user-guide/features/plugins
"""

from __future__ import annotations
from pathlib import Path


_SKILLS_ROOT = Path(__file__).parent / "templates" / "oscar-household" / "skills"
_HERMES_BINDMOUNT = Path("/opt/data/skills/oscar")


def _already_loaded_via_bindmount() -> bool:
    """Detect the ServiceBay-managed deployment case so we don't double-
    register the same skills. If Hermes is running inside the OSCAR pod
    with the SB-managed bind-mount in place, the skill files are
    already discoverable at /opt/data/skills/oscar/ — no need (and a
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

    Walks templates/oscar-household/skills/ and registers each
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
