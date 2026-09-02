"""Tests for ImageRepository, ReferenceRepository, VisitRepository,
PostRepository, and PreferenceRepository."""

from src.channel_intel.repositories import (
    ImageRepository,
    PlaceRepository,
    PostRepository,
    PreferenceRepository,
    ReferenceRepository,
    VisitRepository,
)


async def test_attach_and_find_images(db):
    pr, ir = PlaceRepository(db), ImageRepository(db)
    p = (await pr.upsert_place(name="Lake")).place
    imgs = await ir.attach_images(
        place_id=p.id,
        items=[{"file_path": "/x/a.jpg", "caption": "calm lake", "tags": ["water"]}],
    )
    assert imgs[0].place_id == p.id and imgs[0].id
    found = await ir.find_images_for(place_id=p.id)
    assert found[0].file_path == "/x/a.jpg"
    by_tag = await ir.find_images_for(tags=["water"])
    assert len(by_tag) == 1


async def test_record_visit_marks_place_visited(db):
    pr, vr = PlaceRepository(db), VisitRepository(db)
    p = (await pr.upsert_place(name="Trail")).place
    v = await vr.record_visit(
        place_id=p.id, date="2026-05-01", costs={"food": 20}, narrative="nice"
    )
    assert v.id and v.costs == {"food": 20}
    assert (await pr.get(p.id)).status == "visited"


async def test_preferences_upsert(db):
    pref = PreferenceRepository(db)
    await pref.update_preferences(
        liked_features=["sunset"], home_location="Town", budget_norms={"hotel": 100}
    )
    got = await pref.get_preferences()
    assert got.liked_features == ["sunset"] and got.budget_norms["hotel"] == 100
    # second update merges/overrides
    await pref.update_preferences(home_location="City")
    assert (await pref.get_preferences()).home_location == "City"


async def test_save_reference_links_place(db):
    pr, rr = PlaceRepository(db), ReferenceRepository(db)
    p = (await pr.upsert_place(name="Spot")).place
    ref = await rr.save_reference(source_channel="@guide", text="visit Spot")
    assert ref.id
    await rr.link_reference_to_place(reference_id=ref.id, place_id=p.id)
    assert await rr.places_for_reference(ref.id) == [p.id]


async def test_post_roundtrip(db):
    pr2 = PostRepository(db)
    post = await pr2.create_post(place_ids=[1, 2], body="draft text")
    assert post.id and post.status == "draft"
    assert (await pr2.get_post(post.id)).body == "draft text"


async def test_refs_fts_search_after_save(db):
    rr = ReferenceRepository(db)
    await rr.save_reference(text="hidden waterfall hike")
    cur = await db.execute("SELECT text FROM refs_fts WHERE refs_fts MATCH 'waterfall'")
    assert len(await cur.fetchall()) == 1  # trigger-synced FTS works
