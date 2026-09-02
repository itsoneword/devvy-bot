from src.channel_intel.store import Store


async def test_store_opens_and_roundtrips(tmp_path):
    store = await Store.open(tmp_path / "kb.db")
    r = await store.upsert_place(name="X", features=["sunset"])
    assert (await store.get_place(r.place.id)).name == "X"
    assert [p.name for p in await store.find_places(features=["sunset"])] == ["X"]
    await store.close()


async def test_store_reopen_existing_db_is_idempotent(tmp_path):
    """init_db must survive a restart against an already-initialized DB
    (regression: bare CREATE TABLE crashed channel-intel.service on restart)."""
    path = tmp_path / "kb.db"
    store = await Store.open(path)
    r = await store.upsert_place(name="Persisted", features=["sunset"])
    await store.close()

    reopened = await Store.open(path)  # must not raise "table places already exists"
    assert (await reopened.get_place(r.place.id)).name == "Persisted"
    await reopened.close()


async def test_store_visit_and_prefs(tmp_path):
    store = await Store.open(tmp_path / "kb.db")
    p = (await store.upsert_place(name="Trail", type="hike")).place
    await store.record_visit(place_id=p.id, costs={"food": 10})
    assert (await store.get_place(p.id)).status == "visited"
    await store.update_preferences(liked_features=["sunset"])
    assert (await store.get_preferences()).liked_features == ["sunset"]
    await store.close()
