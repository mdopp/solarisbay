"""Entrypoint: run the chat proxy."""

from __future__ import annotations

import asyncio

from solilos_chat.config import settings
from solilos_chat.hermes import HermesClient
from solilos_chat.logging import log
from solilos_chat.server import serve


def main() -> None:
    log.info(
        "chat.boot", host=settings.host, port=settings.port, hermes=settings.hermes_url
    )
    hermes = HermesClient(settings.hermes_url, settings.hermes_token)
    asyncio.run(
        serve(
            settings.host,
            settings.port,
            hermes=hermes,
            remote_user_header=settings.remote_user_header,
            default_uid=settings.default_uid,
            remote_groups_header=settings.remote_groups_header,
            admin_group=settings.admin_group,
            skills_dir=settings.skills_dir,
            soul_path=settings.soul_path,
            config_agent_url=settings.config_agent_url,
            agent_token=settings.hermes_token,
            logout_url=settings.logout_url,
        )
    )


if __name__ == "__main__":
    main()
