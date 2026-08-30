from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.config import settings

logger = logging.getLogger(__name__)

# Default Telegram DC addresses (used when overriding port)
_DC_IPS = {
    1: "149.154.175.53",
    2: "149.154.167.51",
    3: "149.154.175.100",
    4: "149.154.167.91",
    5: "91.108.56.130",
}


class TelegramPool:
    """Синглтон-пул Telethon-клиентов с ленивой инициализацией и graceful shutdown.

    Manages a dict of TelegramClient instances keyed by worker UUID.
    Each worker gets a per-worker semaphore (3 concurrent requests).
    """

    _instance: TelegramPool | None = None

    def __init__(self) -> None:
        self._clients: dict[UUID, TelegramClient] = {}
        self._semaphores: dict[UUID, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

    @classmethod
    def get_instance(cls) -> TelegramPool:
        """Return the global singleton; create it lazily if needed."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton (for tests only)."""
        cls._instance = None

    async def get_or_create(
        self,
        worker_id: UUID,
        bot_token: str,
        session_string: str | None,
        api_id: int,
        api_hash: str,
    ) -> TelegramClient:
        """Return an existing client or create and connect a new one for worker_id."""
        # Fast path: connected client exists (no lock needed)
        if worker_id in self._clients:
            client = self._clients[worker_id]
            if client.is_connected():
                return client

        # Slow path: create or reconnect under global lock
        async with self._lock:
            # Double-check after acquiring lock
            if worker_id in self._clients:
                client = self._clients[worker_id]
                if client.is_connected():
                    return client
                # Reconnect existing disconnected client (under lock, so only once)
                await client.connect()
                return client

            session = (
                StringSession(session_string) if session_string else StringSession()
            )
            client = TelegramClient(session, api_id, api_hash)

            # Override DC port if non-standard (e.g. port 80 when 443 blocked)
            dc_port = settings.tg_dc_port
            if dc_port != 443:
                dc_id = client.session.dc_id or 2
                addr = _DC_IPS.get(dc_id, "149.154.167.51")
                client.session.set_dc(dc_id, addr, dc_port)
                logger.info("TelegramPool: DC%d port overridden to %d", dc_id, dc_port)

            if session_string:
                logger.info("TelegramPool: connecting worker %s via session", worker_id)
                await client.connect()
            else:
                logger.info("TelegramPool: starting worker %s via bot_token", worker_id)
                await client.start(bot_token=bot_token)

            self._clients[worker_id] = client
            return client

    def semaphore(self, worker_id: UUID) -> asyncio.Semaphore:
        """Lazily create a semaphore (3 concurrent requests) per worker_id."""
        if worker_id not in self._semaphores:
            self._semaphores[worker_id] = asyncio.Semaphore(3)
        return self._semaphores[worker_id]

    async def remove(self, worker_id: UUID) -> None:
        """Disconnect and remove a client from the pool."""
        client = self._clients.pop(worker_id, None)
        self._semaphores.pop(worker_id, None)
        if client:
            logger.info("TelegramPool: disconnected worker %s", worker_id)
            await client.disconnect()

    async def shutdown(self) -> None:
        """Disconnect all clients and clear the pool (called during lifespan shutdown)."""
        logger.info("TelegramPool: shutting down %d client(s)", len(self._clients))
        for worker_id, client in list(self._clients.items()):
            try:
                await client.disconnect()
                logger.info("TelegramPool: disconnected worker %s", worker_id)
            except Exception:
                logger.exception(
                    "TelegramPool: error disconnecting worker %s", worker_id
                )
        self._clients.clear()
        self._semaphores.clear()

    def health(self) -> list[dict]:
        """Return worker status list without exposing secrets.

        Each entry contains worker_id (first 8 chars of UUID) and connected (bool).
        """
        result = []
        for worker_id, client in self._clients.items():
            result.append(
                {
                    "worker_id": str(worker_id)[:8],
                    "connected": client.is_connected(),
                }
            )
        return result

    @property
    def active_count(self) -> int:
        """Number of currently tracked clients."""
        return len(self._clients)


# Module-level convenience reference (backward-compatible)
telegram_pool = TelegramPool.get_instance()
