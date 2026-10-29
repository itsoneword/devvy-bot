"""uvicorn server for HTTP apps that run inside the bot process.

`uvicorn.Server.serve()` replaces the process's SIGINT/SIGTERM handlers for as
long as it runs and only re-raises the signal after its own graceful shutdown
finishes. Inside the bot that steals SIGTERM from `main.py`: on 25.10.26 a
`/restart` shut the API server down, which then waited on an open long-poll
(a job's one-hour policy ask) while the bot kept running with port 8080 dead.
`main.py` owns shutdown; embedded servers must not touch signal handlers.
"""

import contextlib
from typing import Iterator

import uvicorn


class EmbeddedServer(uvicorn.Server):
    """A uvicorn server that leaves the process's signal handlers alone."""

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield
