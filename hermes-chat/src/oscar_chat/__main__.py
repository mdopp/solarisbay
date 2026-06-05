"""Entrypoint: run the chat proxy."""

from __future__ import annotations

import asyncio

from oscar_chat.config import settings
from oscar_chat.hermes import HermesClient
from oscar_chat.logging import log
from oscar_chat.server import serve


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
        )
    )


if __name__ == "__main__":
    main()
