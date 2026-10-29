"""Embedded uvicorn servers must not take over SIGTERM from main.py (25.10.26:
/restart shut only the API server down and the bot kept running)."""

import asyncio
import signal

import uvicorn

from src.utils.embedded_uvicorn import EmbeddedServer


async def _app(scope, receive, send):  # minimal ASGI app
    if scope["type"] == "lifespan":
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


async def test_serve_keeps_the_process_sigterm_handler():
    def mine(signum, frame):  # pragma: no cover - never called
        pass

    old = signal.signal(signal.SIGTERM, mine)
    try:
        server = EmbeddedServer(
            uvicorn.Config(_app, host="127.0.0.1", port=0, log_level="warning")
        )
        task = asyncio.create_task(server.serve())
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.02)
        assert server.started
        assert signal.getsignal(signal.SIGTERM) is mine
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        assert signal.getsignal(signal.SIGTERM) is mine
    finally:
        signal.signal(signal.SIGTERM, old)
