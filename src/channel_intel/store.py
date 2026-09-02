"""Single façade over all channel-KB repositories."""

from pathlib import Path
from typing import Any, Optional

import aiosqlite

from .db import connect, init_db
from .models import Place, PlaceImage, Post, Preferences, Reference, Visit
from .repositories import (
    ImageRepository,
    PlaceRepository,
    PostRepository,
    PreferenceRepository,
    ReferenceRepository,
    UpsertResult,
    VisitRepository,
)


class Store:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._places = PlaceRepository(conn)
        self._images = ImageRepository(conn)
        self._refs = ReferenceRepository(conn)
        self._visits = VisitRepository(conn)
        self._posts = PostRepository(conn)
        self._prefs = PreferenceRepository(conn)

    @classmethod
    async def open(cls, path: str | Path) -> "Store":
        conn = await connect(path)
        await init_db(conn)
        return cls(conn)

    async def close(self) -> None:
        await self._conn.close()

    # ------------------------------------------------------------------
    # Places
    # ------------------------------------------------------------------

    async def upsert_place(
        self,
        *,
        name: str,
        owner: str = "me",
        **fields: Any,
    ) -> UpsertResult:
        return await self._places.upsert_place(name=name, owner=owner, **fields)

    async def get_place(self, place_id: int) -> Optional[Place]:
        return await self._places.get(place_id)

    async def find_places(
        self,
        *,
        owner: str = "me",
        features: Optional[list[str]] = None,
        type: Optional[str] = None,
    ) -> list[Place]:
        return await self._places.find_places(owner=owner, features=features, type=type)

    async def search(self, query: str, *, owner: str = "me") -> list[Place]:
        return await self._places.search(query, owner=owner)

    # ------------------------------------------------------------------
    # Images
    # ------------------------------------------------------------------

    async def attach_images(
        self,
        *,
        place_id: Optional[int],
        items: list[dict[str, Any]],
        owner: str = "me",
    ) -> list[PlaceImage]:
        return await self._images.attach_images(
            place_id=place_id, items=items, owner=owner
        )

    async def find_images_for(
        self,
        *,
        place_id: Optional[int] = None,
        tags: Optional[list[str]] = None,
        owner: str = "me",
    ) -> list[PlaceImage]:
        return await self._images.find_images_for(
            place_id=place_id, tags=tags, owner=owner
        )

    async def set_confirmed(
        self,
        image_id: int,
        *,
        place_id: Optional[int] = None,
        tags: Optional[list[str]] = None,
    ) -> PlaceImage:
        return await self._images.set_confirmed(image_id, place_id=place_id, tags=tags)

    # ------------------------------------------------------------------
    # References
    # ------------------------------------------------------------------

    async def save_reference(
        self,
        *,
        source_channel: Optional[str] = None,
        text: Optional[str] = None,
        image_refs: Optional[list[int]] = None,
        tags: Optional[list[str]] = None,
        notes: Optional[str] = None,
        owner: str = "me",
    ) -> Reference:
        return await self._refs.save_reference(
            source_channel=source_channel,
            text=text,
            image_refs=image_refs,
            tags=tags,
            notes=notes,
            owner=owner,
        )

    async def link_reference_to_place(
        self, *, reference_id: int, place_id: int
    ) -> None:
        await self._refs.link_reference_to_place(
            reference_id=reference_id, place_id=place_id
        )

    async def places_for_reference(self, reference_id: int) -> list[int]:
        return await self._refs.places_for_reference(reference_id)

    # ------------------------------------------------------------------
    # Visits
    # ------------------------------------------------------------------

    async def record_visit(
        self,
        *,
        place_id: int,
        date: Optional[str] = None,
        activities: Optional[str] = None,
        costs: Optional[dict[str, Any]] = None,
        narrative: Optional[str] = None,
        photo_refs: Optional[list[int]] = None,
        owner: str = "me",
    ) -> Visit:
        return await self._visits.record_visit(
            place_id=place_id,
            date=date,
            activities=activities,
            costs=costs,
            narrative=narrative,
            photo_refs=photo_refs,
            owner=owner,
        )

    # ------------------------------------------------------------------
    # Posts
    # ------------------------------------------------------------------

    async def create_post(
        self,
        *,
        place_ids: Optional[list[int]] = None,
        body: Optional[str] = None,
        selected_image_refs: Optional[list[int]] = None,
        status: str = "draft",
        owner: str = "me",
    ) -> Post:
        return await self._posts.create_post(
            place_ids=place_ids,
            body=body,
            selected_image_refs=selected_image_refs,
            status=status,
            owner=owner,
        )

    async def get_post(self, post_id: int) -> Optional[Post]:
        return await self._posts.get_post(post_id)

    # ------------------------------------------------------------------
    # Preferences
    # ------------------------------------------------------------------

    async def get_preferences(self, owner: str = "me") -> Preferences:
        return await self._prefs.get_preferences(owner)

    async def update_preferences(
        self, *, owner: str = "me", **fields: Any
    ) -> Preferences:
        return await self._prefs.update_preferences(owner=owner, **fields)
