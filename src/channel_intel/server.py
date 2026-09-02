"""MCP tool functions for the channel-KB Store.

Plain async functions wrapped in a FastMCP HTTP app with bearer auth.
"""

from dataclasses import asdict
from datetime import datetime
from typing import Any, Optional

from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from mcp.server.fastmcp import FastMCP
from src.channel_intel.models import Place, PlaceImage, Preferences, Reference, Visit
from src.channel_intel.store import Store

# ---------------------------------------------------------------------------
# Dict helpers — convert dataclass to JSON-serialisable dict.
# datetime fields are converted to ISO strings; everything else passes through.
# ---------------------------------------------------------------------------


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def _place_dict(p: Place) -> dict[str, Any]:
    return {k: _iso(v) for k, v in asdict(p).items()}


def _image_dict(img: PlaceImage) -> dict[str, Any]:
    return {k: _iso(v) for k, v in asdict(img).items()}


def _reference_dict(ref: Reference) -> dict[str, Any]:
    return {k: _iso(v) for k, v in asdict(ref).items()}


def _visit_dict(visit: Visit) -> dict[str, Any]:
    return {k: _iso(v) for k, v in asdict(visit).items()}


def _prefs_dict(prefs: Preferences) -> dict[str, Any]:
    return {k: _iso(v) for k, v in asdict(prefs).items()}


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------


async def tool_find_places(
    store: Store,
    *,
    features: Optional[list[str]] = None,
    type: Optional[str] = None,
    owner: str = "me",
) -> list[dict[str, Any]]:
    """Return places matching optional feature/type filters."""
    places = await store.find_places(owner=owner, features=features, type=type)
    return [_place_dict(p) for p in places]


async def tool_get_place(
    store: Store,
    *,
    place_id: int,
    owner: str = "me",
) -> Optional[dict[str, Any]]:
    """Return a single place by ID, or None if not found."""
    place = await store.get_place(place_id)
    if place is None:
        return None
    return _place_dict(place)


async def tool_search(
    store: Store,
    *,
    query: str,
    owner: str = "me",
) -> list[dict[str, Any]]:
    """Full-text search over places."""
    places = await store.search(query, owner=owner)
    return [_place_dict(p) for p in places]


async def tool_find_images_for(
    store: Store,
    *,
    place_id: Optional[int] = None,
    tags: Optional[list[str]] = None,
    owner: str = "me",
) -> list[dict[str, Any]]:
    """Return images for a place and/or matching tags."""
    images = await store.find_images_for(place_id=place_id, tags=tags, owner=owner)
    return [_image_dict(img) for img in images]


async def tool_get_preferences(
    store: Store,
    *,
    owner: str = "me",
) -> dict[str, Any]:
    """Return the owner's preferences."""
    prefs = await store.get_preferences(owner)
    return _prefs_dict(prefs)


async def tool_upsert_place(
    store: Store,
    *,
    name: str,
    owner: str = "me",
    **fields: Any,
) -> dict[str, Any]:
    """Create or update a place by name. Returns place dict and ambiguous list."""
    result = await store.upsert_place(name=name, owner=owner, **fields)
    return {
        "place": _place_dict(result.place) if result.place is not None else None,
        "ambiguous": result.ambiguous,
    }


async def tool_save_reference(
    store: Store,
    *,
    source_channel: Optional[str] = None,
    text: Optional[str] = None,
    image_refs: Optional[list[int]] = None,
    tags: Optional[list[str]] = None,
    notes: Optional[str] = None,
    owner: str = "me",
) -> dict[str, Any]:
    """Save a raw channel reference (link, text snippet, etc.)."""
    ref = await store.save_reference(
        source_channel=source_channel,
        text=text,
        image_refs=image_refs,
        tags=tags,
        notes=notes,
        owner=owner,
    )
    return _reference_dict(ref)


async def tool_link_reference_to_place(
    store: Store,
    *,
    reference_id: int,
    place_id: int,
) -> dict[str, Any]:
    """Associate an existing reference with a place."""
    await store.link_reference_to_place(reference_id=reference_id, place_id=place_id)
    return {"ok": True}


async def tool_record_visit(
    store: Store,
    *,
    place_id: int,
    date: Optional[str] = None,
    activities: Optional[str] = None,
    costs: Optional[dict[str, Any]] = None,
    narrative: Optional[str] = None,
    photo_refs: Optional[list[int]] = None,
    owner: str = "me",
) -> dict[str, Any]:
    """Record a visit to a place."""
    visit = await store.record_visit(
        place_id=place_id,
        date=date,
        activities=activities,
        costs=costs,
        narrative=narrative,
        photo_refs=photo_refs,
        owner=owner,
    )
    return _visit_dict(visit)


async def tool_attach_images(
    store: Store,
    *,
    place_id: Optional[int],
    items: list[dict[str, Any]],
    owner: str = "me",
) -> list[dict[str, Any]]:
    """Attach image records to a place."""
    images = await store.attach_images(place_id=place_id, items=items, owner=owner)
    return [_image_dict(img) for img in images]


async def tool_update_preferences(
    store: Store,
    *,
    owner: str = "me",
    **fields: Any,
) -> dict[str, Any]:
    """Update the owner's preferences."""
    prefs = await store.update_preferences(owner=owner, **fields)
    return _prefs_dict(prefs)


# ---------------------------------------------------------------------------
# Bearer auth helper
# ---------------------------------------------------------------------------


def check_bearer(header: Optional[str], token: Optional[str]) -> bool:
    """Return True if the request is authorised.

    When *token* is None (auth disabled) every request is allowed.
    Otherwise the *header* must be ``Bearer <token>``.
    """
    if not token:
        return True
    return header == f"Bearer {token}"


# ---------------------------------------------------------------------------
# FastMCP app factory
# ---------------------------------------------------------------------------


def build_app(store: Store, token: Optional[str]) -> Starlette:
    """Build and return the FastMCP Starlette app with bearer-auth middleware."""
    mcp = FastMCP("channel-intel")

    @mcp.tool()
    async def find_places(
        features: Optional[list[str]] = None,
        type: Optional[str] = None,
        owner: str = "me",
    ) -> list[dict[str, Any]]:
        """Return places matching optional feature/type filters."""
        return await tool_find_places(store, features=features, type=type, owner=owner)

    @mcp.tool()
    async def get_place(
        place_id: int,
        owner: str = "me",
    ) -> Optional[dict[str, Any]]:
        """Return a single place by ID, or None if not found."""
        return await tool_get_place(store, place_id=place_id, owner=owner)

    @mcp.tool()
    async def search(
        query: str,
        owner: str = "me",
    ) -> list[dict[str, Any]]:
        """Full-text search over places."""
        return await tool_search(store, query=query, owner=owner)

    @mcp.tool()
    async def find_images_for(
        place_id: Optional[int] = None,
        tags: Optional[list[str]] = None,
        owner: str = "me",
    ) -> list[dict[str, Any]]:
        """Return images for a place and/or matching tags."""
        return await tool_find_images_for(
            store, place_id=place_id, tags=tags, owner=owner
        )

    @mcp.tool()
    async def get_preferences(
        owner: str = "me",
    ) -> dict[str, Any]:
        """Return the owner's preferences."""
        return await tool_get_preferences(store, owner=owner)

    @mcp.tool()
    async def upsert_place(
        name: str,
        owner: str = "me",
        type: Optional[str] = None,
        location: Optional[str] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        travel_time_from_home: Optional[str] = None,
        price: Optional[str] = None,
        opening_hours: Optional[str] = None,
        features: Optional[list[str]] = None,
        status: Optional[str] = None,
        liked: Optional[bool] = None,
        my_notes: Optional[str] = None,
        source: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create or update a place by name."""
        provided: dict[str, Any] = {}
        if type is not None:
            provided["type"] = type
        if location is not None:
            provided["location"] = location
        if lat is not None:
            provided["lat"] = lat
        if lon is not None:
            provided["lon"] = lon
        if travel_time_from_home is not None:
            provided["travel_time_from_home"] = travel_time_from_home
        if price is not None:
            provided["price"] = price
        if opening_hours is not None:
            provided["opening_hours"] = opening_hours
        if features is not None:
            provided["features"] = features
        if status is not None:
            provided["status"] = status
        if liked is not None:
            provided["liked"] = liked
        if my_notes is not None:
            provided["my_notes"] = my_notes
        if source is not None:
            provided["source"] = source
        return await tool_upsert_place(store, name=name, owner=owner, **provided)

    @mcp.tool()
    async def save_reference(
        source_channel: Optional[str] = None,
        text: Optional[str] = None,
        image_refs: Optional[list[int]] = None,
        tags: Optional[list[str]] = None,
        notes: Optional[str] = None,
        owner: str = "me",
    ) -> dict[str, Any]:
        """Save a raw channel reference."""
        return await tool_save_reference(
            store,
            source_channel=source_channel,
            text=text,
            image_refs=image_refs,
            tags=tags,
            notes=notes,
            owner=owner,
        )

    @mcp.tool()
    async def link_reference_to_place(
        reference_id: int,
        place_id: int,
    ) -> dict[str, Any]:
        """Associate an existing reference with a place."""
        return await tool_link_reference_to_place(
            store, reference_id=reference_id, place_id=place_id
        )

    @mcp.tool()
    async def record_visit(
        place_id: int,
        date: Optional[str] = None,
        activities: Optional[str] = None,
        costs: Optional[dict[str, Any]] = None,
        narrative: Optional[str] = None,
        photo_refs: Optional[list[int]] = None,
        owner: str = "me",
    ) -> dict[str, Any]:
        """Record a visit to a place."""
        return await tool_record_visit(
            store,
            place_id=place_id,
            date=date,
            activities=activities,
            costs=costs,
            narrative=narrative,
            photo_refs=photo_refs,
            owner=owner,
        )

    @mcp.tool()
    async def attach_images(
        items: list[dict[str, Any]],
        place_id: Optional[int] = None,
        owner: str = "me",
    ) -> list[dict[str, Any]]:
        """Attach image records to a place."""
        return await tool_attach_images(
            store, place_id=place_id, items=items, owner=owner
        )

    @mcp.tool()
    async def update_preferences(
        owner: str = "me",
        liked_features: Optional[list[str]] = None,
        liked_place_ids: Optional[list[int]] = None,
        budget_norms: Optional[dict[str, Any]] = None,
        home_location: Optional[str] = None,
        default_radius: Optional[str] = None,
    ) -> dict[str, Any]:
        """Update the owner's preferences."""
        provided: dict[str, Any] = {}
        if liked_features is not None:
            provided["liked_features"] = liked_features
        if liked_place_ids is not None:
            provided["liked_place_ids"] = liked_place_ids
        if budget_norms is not None:
            provided["budget_norms"] = budget_norms
        if home_location is not None:
            provided["home_location"] = home_location
        if default_radius is not None:
            provided["default_radius"] = default_radius
        return await tool_update_preferences(store, owner=owner, **provided)

    app: Starlette = mcp.streamable_http_app()

    _token = token

    class Auth(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            if not check_bearer(request.headers.get("authorization"), _token):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return await call_next(request)  # type: ignore[no-any-return]

    app.add_middleware(Auth)
    return app


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def main() -> None:
    """Start the uvicorn server using settings from environment variables."""
    import uvicorn  # type: ignore[import-untyped]

    from src.channel_intel.config import ServerConfig

    cfg = ServerConfig.from_env()
    store = await Store.open(cfg.db_path)
    try:
        config = uvicorn.Config(
            build_app(store, cfg.token),
            host=cfg.host,
            port=cfg.port,
            log_level="info",
        )
        await uvicorn.Server(config).serve()
    finally:
        await store.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
