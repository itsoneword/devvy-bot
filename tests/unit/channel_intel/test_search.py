"""Tests for find_places soft ranking and FTS search."""

from src.channel_intel.repositories import PlaceRepository


async def test_find_places_ranks_but_keeps_unknowns(db):
    repo = PlaceRepository(db)
    await repo.upsert_place(
        name="Sunset Peak",
        type="viewpoint",
        features=["sunset", "animals"],
        price="free",
    )
    await repo.upsert_place(name="Mystery Spot")  # no features, no price
    res = await repo.find_places(features=["sunset"], type="viewpoint")
    names = [p.name for p in res]
    assert "Sunset Peak" in names and "Mystery Spot" in names  # nothing dropped
    assert names[0] == "Sunset Peak"  # boosted first
    assert res[0].score > res[-1].score


async def test_search_fts(db):
    repo = PlaceRepository(db)
    await repo.upsert_place(name="Cliffside Cafe", my_notes="great coffee")
    hits = await repo.search("coffee")
    assert [p.name for p in hits] == ["Cliffside Cafe"]
