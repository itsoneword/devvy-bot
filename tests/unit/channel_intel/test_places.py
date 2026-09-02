from datetime import UTC, datetime

from src.channel_intel.repositories import PlaceRepository


async def test_init_db_creates_tables(db):
    cur = await db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = {r[0] for r in await cur.fetchall()}
    assert {
        "places",
        "place_images",
        "place_references",
        "reference_places",
        "visits",
        "posts",
        "preferences",
        "kb_users",
        "comments",
        "places_fts",
        "images_fts",
        "refs_fts",
    } <= names


async def test_upsert_inserts_then_merges(db):
    repo = PlaceRepository(db)
    r1 = await repo.upsert_place(name="Cliff Trail", type="hike", features=["sunset"])
    assert r1.place.id and r1.place.features == ["sunset"]
    r2 = await repo.upsert_place(
        name="cliff trail", price="free", features=["sunset", "animals"]
    )
    assert r2.place.id == r1.place.id
    assert r2.place.price == "free"
    assert r2.place.features == ["sunset", "animals"]
    assert r2.ambiguous == []
    assert len(await repo.list_all()) == 1


async def test_upsert_ambiguous_returns_candidates(db):
    repo = PlaceRepository(db)
    a = await repo.upsert_place(name="Bistro", lat=1.0, lon=1.0)
    b = await repo.upsert_place(name="Bistro", lat=9.0, lon=9.0)
    r = await repo.upsert_place(name="Bistro")  # no coords -> matches both by name
    assert r.place is None
    assert set(r.ambiguous) == {a.place.id, b.place.id}


async def test_places_fts_trigger_syncs_on_insert(db):
    now = datetime.now(UTC)
    await db.execute(
        "INSERT INTO places(name, my_notes, created_at, updated_at) "
        "VALUES (?,?,?,?)",
        ("Cliff Cafe", "great coffee", now, now),
    )
    await db.commit()
    cur = await db.execute(
        "SELECT name FROM places_fts WHERE places_fts MATCH 'coffee'"
    )
    rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["Cliff Cafe"]
