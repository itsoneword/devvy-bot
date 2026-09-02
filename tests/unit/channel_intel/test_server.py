"""Unit tests for channel_intel tool functions (no HTTP server required)."""

from pathlib import Path

from starlette.testclient import TestClient

from src.channel_intel.server import (
    build_app,
    check_bearer,
    tool_attach_images,
    tool_find_images_for,
    tool_find_places,
    tool_get_place,
    tool_get_preferences,
    tool_link_reference_to_place,
    tool_record_visit,
    tool_save_reference,
    tool_search,
    tool_update_preferences,
    tool_upsert_place,
)
from src.channel_intel.store import Store


async def test_tool_upsert_then_find(tmp_path):
    store = await Store.open(tmp_path / "kb.db")
    up = await tool_upsert_place(store, name="Ridge", features=["sunset"])
    assert up["place"]["name"] == "Ridge" and up["ambiguous"] == []
    res = await tool_find_places(store, features=["sunset"])
    assert res[0]["name"] == "Ridge"
    assert "price" in res[0]  # null surfaced, key present
    await store.close()


async def test_tool_get_place_found_and_missing(tmp_path):
    store = await Store.open(tmp_path / "kb.db")
    up = await tool_upsert_place(store, name="Lakeview")
    place_id = up["place"]["id"]
    got = await tool_get_place(store, place_id=place_id)
    assert got is not None
    assert got["name"] == "Lakeview"
    missing = await tool_get_place(store, place_id=99999)
    assert missing is None
    await store.close()


async def test_tool_search(tmp_path):
    store = await Store.open(tmp_path / "kb.db")
    await tool_upsert_place(store, name="SunsetHill", type="viewpoint")
    results = await tool_search(store, query="SunsetHill")
    assert any(r["name"] == "SunsetHill" for r in results)
    await store.close()


async def test_tool_attach_images_and_find(tmp_path):
    store = await Store.open(tmp_path / "kb.db")
    up = await tool_upsert_place(store, name="CafeNoir")
    place_id = up["place"]["id"]
    images = await tool_attach_images(
        store,
        place_id=place_id,
        items=[{"file_path": "/tmp/img1.jpg", "tags": ["coffee"]}],
    )
    assert len(images) == 1
    assert images[0]["file_path"] == "/tmp/img1.jpg"
    found = await tool_find_images_for(store, place_id=place_id)
    assert found[0]["file_path"] == "/tmp/img1.jpg"
    await store.close()


async def test_tool_record_visit(tmp_path):
    store = await Store.open(tmp_path / "kb.db")
    up = await tool_upsert_place(store, name="Trail77")
    place_id = up["place"]["id"]
    visit = await tool_record_visit(
        store, place_id=place_id, activities="hiking", costs={"food": 15}
    )
    assert visit["place_id"] == place_id
    assert visit["activities"] == "hiking"
    assert visit["costs"] == {"food": 15}
    # Place status should now be "visited"
    p = await tool_get_place(store, place_id=place_id)
    assert p is not None
    assert p["status"] == "visited"
    await store.close()


async def test_tool_preferences_round_trip(tmp_path):
    store = await Store.open(tmp_path / "kb.db")
    prefs = await tool_get_preferences(store)
    assert "liked_features" in prefs
    updated = await tool_update_preferences(
        store, liked_features=["cozy", "quiet"], home_location="Kyiv"
    )
    assert updated["liked_features"] == ["cozy", "quiet"]
    assert updated["home_location"] == "Kyiv"
    # Re-fetch to confirm persistence
    prefs2 = await tool_get_preferences(store)
    assert prefs2["liked_features"] == ["cozy", "quiet"]
    await store.close()


async def test_tool_save_reference_and_link(tmp_path):
    store = await Store.open(tmp_path / "kb.db")
    ref = await tool_save_reference(
        store,
        source_channel="@places_kyiv",
        text="Great rooftop bar",
        tags=["rooftop", "bar"],
    )
    assert ref["source_channel"] == "@places_kyiv"
    assert ref["id"] is not None
    up = await tool_upsert_place(store, name="Rooftop Bar")
    place_id = up["place"]["id"]
    result = await tool_link_reference_to_place(
        store, reference_id=ref["id"], place_id=place_id
    )
    assert result == {"ok": True}
    await store.close()


async def test_datetime_fields_are_strings(tmp_path):
    """All datetime fields in returned dicts must be ISO strings, not datetime objects."""
    store = await Store.open(tmp_path / "kb.db")
    up = await tool_upsert_place(store, name="TimeTest")
    place = up["place"]
    assert isinstance(place["created_at"], str)
    assert isinstance(place["updated_at"], str)
    ref = await tool_save_reference(store, text="test")
    assert isinstance(ref["created_at"], str)
    await store.close()


# ---------------------------------------------------------------------------
# check_bearer unit tests
# ---------------------------------------------------------------------------


def test_check_bearer() -> None:
    assert check_bearer("Bearer secret", "secret") is True
    assert check_bearer("Bearer wrong", "secret") is False
    assert check_bearer(None, "secret") is False
    assert check_bearer("anything", None) is True  # auth disabled when no token


# ---------------------------------------------------------------------------
# build_app auth middleware integration tests
# ---------------------------------------------------------------------------


async def test_build_app_auth_rejects_without_token(tmp_path: Path) -> None:
    """No/wrong auth header must yield 401 from our middleware."""
    store = await Store.open(tmp_path / "kb.db")
    app = build_app(store, token="secret")
    client = TestClient(app, raise_server_exceptions=False)
    # No auth header -> middleware must return 401 before MCP handshake
    r = client.post("/mcp", json={})
    assert r.status_code == 401
    await store.close()


async def test_build_app_auth_allows_valid_token(tmp_path: Path) -> None:
    """A valid bearer token must pass through the middleware (non-401 response)."""
    store = await Store.open(tmp_path / "kb.db")
    app = build_app(store, token="secret")
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/mcp", headers={"Authorization": "Bearer secret"}, json={})
    # The MCP protocol may reject the bare POST but must NOT be 401
    assert r.status_code != 401
    await store.close()


async def test_build_app_auth_rejects_wrong_token(tmp_path: Path) -> None:
    """A wrong bearer token must yield 401 from our middleware."""
    store = await Store.open(tmp_path / "kb.db")
    app = build_app(store, token="secret")
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    await store.close()
