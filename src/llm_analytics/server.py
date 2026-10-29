"""FastAPI read API + background indexer loop (milestones 3-4 of dv-dd8f).

App-factory + __main__ shape mirrors src/channel_intel/server.py (plain
FastAPI instead of FastMCP). Read-only service: no mutating endpoints; the
only writes happen in the background indexer, which runs the existing sync
scan in asyncio.to_thread on its own sqlite3 connection every SCAN_INTERVAL
seconds. All URLs are relative (no absolute URL generation, no redirects).
"""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, cast

import structlog
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.staticfiles import StaticFiles

from .config import AnalyticsConfig
from .db import connect, init_db
from .indexer import Indexer
from .pricing import PricingTable
from .store import Store

logger = structlog.get_logger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# api/calls/{id}/raw whitelist — metadata only, never content text or tool
# inputs. tool_use blocks are reduced to their name/id.
_RAW_TOP_FIELDS = (
    "type",
    "timestamp",
    "requestId",
    "uuid",
    "parentUuid",
    "entrypoint",
    "cwd",
    "version",
)


# ---------------------------------------------------------------------------
# Background indexer
# ---------------------------------------------------------------------------


def _run_scan(config: AnalyticsConfig) -> None:
    """One sync scan cycle on a private sqlite3 connection (worker thread)."""
    pricing = PricingTable.load(config.pricing_path)
    conn = connect(config.db_path)
    try:
        init_db(conn)
        Indexer(conn, config, pricing).scan_once()
    finally:
        conn.close()


async def _indexer_loop(config: AnalyticsConfig, stop: asyncio.Event) -> None:
    """Scan immediately, then every SCAN_INTERVAL seconds until stopped."""
    while True:
        try:
            await asyncio.to_thread(_run_scan, config)
        except Exception as exc:  # never let one bad scan kill the loop
            logger.error("indexer_scan_failed", error=str(exc))
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.scan_interval)
        except TimeoutError:
            continue
        return


# ---------------------------------------------------------------------------
# Raw-line whitelisting
# ---------------------------------------------------------------------------


def _read_line_at(path: str, offset: int) -> str | None:
    """Re-read one line from (path, offset); None if the file is gone/short."""
    file_path = Path(path)
    if not file_path.exists():
        return None
    with file_path.open("rb") as handle:
        handle.seek(0, 2)
        if handle.tell() <= offset:
            return None
        handle.seek(offset)
        return handle.readline().decode("utf-8", errors="replace")


def _whitelist_raw(entry: dict[str, Any]) -> dict[str, Any]:
    """Metadata-only projection of a transcript line (no content text)."""
    out: dict[str, Any] = {}
    for key in _RAW_TOP_FIELDS:
        if key in entry:
            out[key] = entry[key]
    message = entry.get("message")
    if isinstance(message, dict):
        message_out: dict[str, Any] = {}
        if "model" in message:
            message_out["model"] = message["model"]
        if "usage" in message:
            message_out["usage"] = message["usage"]
        tool_uses = []
        content = message.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_uses.append({"id": block.get("id"), "name": block.get("name")})
        if tool_uses:
            message_out["tool_uses"] = tool_uses
        out["message"] = message_out
    return out


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def _parse_user(raw: str | None) -> int | str | None:
    """Parse the user_id query param: numeric TG id, 'local' (no bot user), or None."""
    if raw is None or raw == "":
        return None
    if raw == "local":
        return "local"
    try:
        return int(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="user_id must be an integer or 'local'"
        ) from exc


def _store(request: Request) -> Store:
    return cast(Store, request.app.state.store)


def build_app(config: AnalyticsConfig) -> FastAPI:
    """Build the read-only API app with the background indexer lifespan."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        config.db_path.parent.mkdir(parents=True, exist_ok=True)

        def _ensure_schema() -> None:
            conn = connect(config.db_path)
            try:
                init_db(conn)
            finally:
                conn.close()

        await asyncio.to_thread(_ensure_schema)
        store = await Store.open(config.db_path)
        app.state.store = store
        stop = asyncio.Event()
        task = asyncio.create_task(_indexer_loop(config, stop))
        logger.info(
            "server_started",
            db=str(config.db_path),
            scan_interval=config.scan_interval,
        )
        try:
            yield
        finally:
            stop.set()
            try:
                await asyncio.wait_for(task, timeout=60)
            except TimeoutError:
                task.cancel()
            await store.close()
            logger.info("server_stopped")

    app = FastAPI(title="llm-analytics", lifespan=lifespan)

    @app.get("/api/health")
    async def health(request: Request) -> dict[str, Any]:
        return await _store(request).health()

    @app.get("/api/filters")
    async def filters(
        request: Request,
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
    ) -> dict[str, Any]:
        return await _store(request).filters(from_=from_, to=to)

    @app.get("/api/overview")
    async def overview(
        request: Request,
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
        user_id: str | None = None,
        profile: str | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        return await _store(request).overview(
            from_=from_,
            to=to,
            user_id=_parse_user(user_id),
            profile=profile,
            source=source,
        )

    @app.get("/api/quality")
    async def quality(
        request: Request,
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
        user_id: str | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        return await _store(request).quality(
            from_=from_, to=to, user_id=_parse_user(user_id), profile=profile
        )

    @app.get("/api/sessions")
    async def sessions(
        request: Request,
        from_: str | None = Query(default=None, alias="from"),
        to: str | None = None,
        user_id: str | None = None,
        profile: str | None = None,
        source: str | None = None,
        entrypoint: str | None = None,
        sort: str = "last_ts",
        order: str = "desc",
        limit: int = Query(default=50, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, Any]:
        try:
            return await _store(request).sessions_page(
                from_=from_,
                to=to,
                user_id=_parse_user(user_id),
                profile=profile,
                source=source,
                entrypoint=entrypoint,
                sort=sort,
                order=order,
                limit=limit,
                offset=offset,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/sessions/{session_id}")
    async def session_detail(request: Request, session_id: str) -> dict[str, Any]:
        result = await _store(request).session_detail(session_id)
        if result is None:
            raise HTTPException(status_code=404, detail="session not found")
        return result

    @app.get("/api/sessions/{session_id}/timeline")
    async def session_timeline(
        request: Request, session_id: str, agent_id: str = ""
    ) -> dict[str, Any]:
        result = await _store(request).session_timeline(session_id, agent_id)
        if result is None:
            raise HTTPException(status_code=404, detail="session not found")
        return result

    @app.get("/api/calls/{request_id}")
    async def call_detail(request: Request, request_id: str) -> dict[str, Any]:
        call = await _store(request).get_call(request_id)
        if call is None:
            raise HTTPException(status_code=404, detail="call not found")
        return call

    @app.get("/api/calls/{request_id}/raw")
    async def call_raw(
        request: Request, request_id: str, include_content: bool = False
    ) -> dict[str, Any]:
        if include_content:
            raise HTTPException(
                status_code=501,
                detail="content access not implemented (metadata-only service)",
            )
        location = await _store(request).call_location(request_id)
        if location is None:
            raise HTTPException(status_code=404, detail="call not found")
        path, offset = location
        line = await asyncio.to_thread(_read_line_at, path, offset)
        if line is None:
            raise HTTPException(
                status_code=404, detail="transcript file missing or rotated"
            )
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=404, detail="transcript line no longer parseable"
            ) from exc
        if not isinstance(entry, dict) or entry.get("requestId") != request_id:
            raise HTTPException(
                status_code=404, detail="transcript file rewritten (offset stale)"
            )
        return _whitelist_raw(entry)

    @app.get("/api/reconciliation")
    async def reconciliation(
        request: Request,
        user_id: int | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        return await _store(request).reconciliation(user_id=user_id, profile=profile)

    @app.get("/api/chains")
    async def chains(
        request: Request, since: str | None = None, until: str | None = None
    ) -> dict[str, Any]:
        return await _store(request).chains(since=since, until=until)

    @app.get("/api/chains/detail")
    async def chain_detail(
        request: Request,
        user_id: int,
        working_dir: str,
        since: str | None = None,
        until: str | None = None,
    ) -> dict[str, Any]:
        result = await _store(request).chain_detail(
            user_id=user_id, working_dir=working_dir, since=since, until=until
        )
        if result is None:
            raise HTTPException(status_code=404, detail="chain not found")
        return result

    @app.get("/api/chains/window")
    async def chain_window(
        request: Request, user_id: int, working_dir: str, window_id: int
    ) -> dict[str, Any]:
        """What filled the context of one window (dv-aeca)."""
        result = await _store(request).window_context(
            user_id=user_id, working_dir=working_dir, window_id=window_id
        )
        if result is None:
            raise HTTPException(status_code=404, detail="chain not found")
        return result

    app.mount("/", _RevalidatingStatic(directory=STATIC_DIR, html=True), name="static")
    return app


class _RevalidatingStatic(StaticFiles):
    """Static files the browser must revalidate on every load.

    The UI is unbundled ES modules: without this, browsers heuristically cache
    views/*.js and keep showing an old chart after a deploy (25.10). The etag
    keeps revalidation a cheap 304.
    """

    def file_response(self, *args: Any, **kwargs: Any) -> Any:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def main() -> None:
    """Start uvicorn using settings from environment variables."""
    import uvicorn  # type: ignore[import-untyped]

    config = AnalyticsConfig.from_env()
    uvicorn.run(build_app(config), host=config.host, port=config.port, log_level="info")


if __name__ == "__main__":
    main()
