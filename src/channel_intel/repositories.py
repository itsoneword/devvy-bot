"""Channel KB repositories: Place persistence and dedup/merge upsert."""

import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Optional

import aiosqlite

from src.channel_intel.models import (
    Place,
    PlaceImage,
    Post,
    Preferences,
    Reference,
    Visit,
)


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Return distance in metres between two (lat, lon) pairs."""
    R = 6371000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dphi = math.radians(b[0] - a[0])
    dl = math.radians(b[1] - a[1])
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


_DEDUP_DISTANCE_M = 500.0


@dataclass
class UpsertResult:
    place: Optional[Place]
    ambiguous: list[int] = field(default_factory=list)


class PlaceRepository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def upsert_place(
        self,
        *,
        name: str,
        owner: str = "me",
        **fields: Any,
    ) -> UpsertResult:
        """Insert or merge a place, deduplicating by name + proximity."""
        now = datetime.now(UTC)
        candidates = await self._find_candidates(name=name, owner=owner, **fields)

        if len(candidates) == 0:
            place = Place.new(
                name=name, owner=owner, created_at=now, updated_at=now, **fields
            )
            place = await self._insert(place)
            return UpsertResult(place=place)

        if len(candidates) == 1:
            merged = candidates[0].merged_with(name=name, updated_at=now, **fields)
            merged = await self._update(merged)
            return UpsertResult(place=merged)

        # >1 candidates — ambiguous
        return UpsertResult(
            place=None,
            ambiguous=[c.id for c in candidates if c.id is not None],
        )

    async def get(self, place_id: int) -> Optional[Place]:
        cur = await self._conn.execute("SELECT * FROM places WHERE id = ?", (place_id,))
        row = await cur.fetchone()
        return Place.from_row(row) if row else None

    async def list_all(self, owner: str = "me") -> list[Place]:
        cur = await self._conn.execute(
            "SELECT * FROM places WHERE owner = ? ORDER BY id", (owner,)
        )
        rows = await cur.fetchall()
        return [Place.from_row(r) for r in rows]

    async def find_places(
        self,
        *,
        owner: str = "me",
        features: Optional[list[str]] = None,
        type: Optional[str] = None,
    ) -> list[Place]:
        """Return all owner's places sorted by soft relevance score DESC."""
        places = await self.list_all(owner)
        requested_features = features or []
        for place in places:
            place_features = place.features or []
            feature_hits = sum(1 for f in requested_features if f in place_features)
            type_bonus = 0.5 if (type and place.type == type) else 0.0
            place.score = 1.0 + 0.5 * feature_hits + type_bonus
        places.sort(key=lambda p: p.score or 0.0, reverse=True)
        return places

    async def search(self, query: str, *, owner: str = "me") -> list[Place]:
        """Full-text search over places_fts, filtered by owner."""
        cur = await self._conn.execute(
            """
            SELECT p.* FROM places p
            JOIN places_fts f ON p.id = f.rowid
            WHERE p.owner = ? AND places_fts MATCH ?
            ORDER BY rank
            """,
            (owner, query),
        )
        rows = await cur.fetchall()
        return [Place.from_row(r) for r in rows]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _find_candidates(
        self, *, name: str, owner: str, **fields: Any
    ) -> list[Place]:
        """Return existing places that are dedup-equivalent to the incoming record."""
        cur = await self._conn.execute(
            "SELECT * FROM places WHERE owner = ? AND lower(name) = lower(?)",
            (owner, name),
        )
        rows = await cur.fetchall()
        existing = [Place.from_row(r) for r in rows]

        incoming_lat: Optional[float] = fields.get("lat")
        incoming_lon: Optional[float] = fields.get("lon")

        candidates: list[Place] = []
        for p in existing:
            if (
                incoming_lat is not None
                and incoming_lon is not None
                and p.lat is not None
                and p.lon is not None
            ):
                # Both have coords — check proximity
                dist = _haversine_m((p.lat, p.lon), (incoming_lat, incoming_lon))
                if dist <= _DEDUP_DISTANCE_M:
                    candidates.append(p)
            else:
                # At least one side lacks coords → name match is sufficient
                candidates.append(p)

        return candidates

    async def _insert(self, place: Place) -> Place:
        cur = await self._conn.execute(
            """
            INSERT INTO places (
                owner, name, type, location, lat, lon,
                travel_time_from_home, price, opening_hours, features,
                status, liked, my_notes, source, enriched_at,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            self._to_row(place),
        )
        await self._conn.commit()
        from dataclasses import replace

        return replace(place, id=cur.lastrowid)

    async def _update(self, place: Place) -> Place:
        await self._conn.execute(
            """
            UPDATE places SET
                name=?, type=?, location=?, lat=?, lon=?,
                travel_time_from_home=?, price=?, opening_hours=?, features=?,
                status=?, liked=?, my_notes=?, source=?, enriched_at=?,
                updated_at=?
            WHERE id=?
            """,
            (
                place.name,
                place.type,
                place.location,
                place.lat,
                place.lon,
                place.travel_time_from_home,
                place.price,
                place.opening_hours,
                json.dumps(place.features) if place.features is not None else None,
                place.status,
                int(place.liked) if place.liked is not None else None,
                place.my_notes,
                place.source,
                place.enriched_at.isoformat() if place.enriched_at else None,
                place.updated_at.isoformat(),
                place.id,
            ),
        )
        await self._conn.commit()
        return place

    @staticmethod
    def _to_row(place: Place) -> tuple[Any, ...]:
        """Return the INSERT value tuple (excludes id)."""
        return (
            place.owner,
            place.name,
            place.type,
            place.location,
            place.lat,
            place.lon,
            place.travel_time_from_home,
            place.price,
            place.opening_hours,
            json.dumps(place.features) if place.features is not None else None,
            place.status,
            int(place.liked) if place.liked is not None else None,
            place.my_notes,
            place.source,
            place.enriched_at.isoformat() if place.enriched_at else None,
            place.created_at.isoformat(),
            place.updated_at.isoformat(),
        )


# ---------------------------------------------------------------------------
# ImageRepository
# ---------------------------------------------------------------------------


class ImageRepository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def attach_images(
        self,
        *,
        place_id: Optional[int],
        items: list[dict[str, Any]],
        owner: str = "me",
    ) -> list[PlaceImage]:
        now = datetime.now(UTC)
        result: list[PlaceImage] = []
        for item in items:
            tags = item.get("tags")
            cur = await self._conn.execute(
                """
                INSERT INTO place_images
                    (owner, place_id, file_path, caption, tags, vision_model,
                     confirmed, created_at)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    owner,
                    place_id,
                    item["file_path"],
                    item.get("caption"),
                    json.dumps(tags) if tags is not None else None,
                    item.get("vision_model"),
                    int(item.get("confirmed", False)),
                    now.isoformat(),
                ),
            )
            img = PlaceImage(
                id=cur.lastrowid,
                owner=owner,
                place_id=place_id,
                file_path=item["file_path"],
                caption=item.get("caption"),
                tags=tags,
                vision_model=item.get("vision_model"),
                confirmed=bool(item.get("confirmed", False)),
                created_at=now,
            )
            result.append(img)
        await self._conn.commit()
        return result

    async def find_images_for(
        self,
        *,
        place_id: Optional[int] = None,
        tags: Optional[list[str]] = None,
        owner: str = "me",
    ) -> list[PlaceImage]:
        if place_id is not None:
            cur = await self._conn.execute(
                "SELECT * FROM place_images WHERE owner = ? AND place_id = ?",
                (owner, place_id),
            )
        else:
            cur = await self._conn.execute(
                "SELECT * FROM place_images WHERE owner = ?",
                (owner,),
            )
        rows = await cur.fetchall()
        images = [PlaceImage.from_row(r) for r in rows]
        if tags:
            tag_set = set(tags)
            images = [
                img for img in images if img.tags and tag_set.intersection(img.tags)
            ]
        return images

    async def set_confirmed(
        self,
        image_id: int,
        *,
        place_id: Optional[int] = None,
        tags: Optional[list[str]] = None,
    ) -> PlaceImage:
        if place_id is not None and tags is not None:
            await self._conn.execute(
                "UPDATE place_images SET confirmed=1, place_id=?, tags=? WHERE id=?",
                (place_id, json.dumps(tags), image_id),
            )
        elif place_id is not None:
            await self._conn.execute(
                "UPDATE place_images SET confirmed=1, place_id=? WHERE id=?",
                (place_id, image_id),
            )
        elif tags is not None:
            await self._conn.execute(
                "UPDATE place_images SET confirmed=1, tags=? WHERE id=?",
                (json.dumps(tags), image_id),
            )
        else:
            await self._conn.execute(
                "UPDATE place_images SET confirmed=1 WHERE id=?",
                (image_id,),
            )
        await self._conn.commit()
        cur = await self._conn.execute(
            "SELECT * FROM place_images WHERE id = ?", (image_id,)
        )
        row = await cur.fetchone()
        return PlaceImage.from_row(row)


# ---------------------------------------------------------------------------
# ReferenceRepository
# ---------------------------------------------------------------------------


class ReferenceRepository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

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
        now = datetime.now(UTC)
        cur = await self._conn.execute(
            """
            INSERT INTO place_references
                (owner, source_channel, text, image_refs, tags, notes, created_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                owner,
                source_channel,
                text,
                json.dumps(image_refs) if image_refs is not None else None,
                json.dumps(tags) if tags is not None else None,
                notes,
                now.isoformat(),
            ),
        )
        await self._conn.commit()
        return Reference(
            id=cur.lastrowid,
            owner=owner,
            source_channel=source_channel,
            text=text,
            image_refs=image_refs,
            tags=tags,
            notes=notes,
            created_at=now,
        )

    async def link_reference_to_place(
        self, *, reference_id: int, place_id: int
    ) -> None:
        await self._conn.execute(
            "INSERT OR IGNORE INTO reference_places (reference_id, place_id) VALUES (?,?)",
            (reference_id, place_id),
        )
        await self._conn.commit()

    async def places_for_reference(self, reference_id: int) -> list[int]:
        cur = await self._conn.execute(
            "SELECT place_id FROM reference_places WHERE reference_id = ? ORDER BY place_id",
            (reference_id,),
        )
        rows = await cur.fetchall()
        return [r["place_id"] for r in rows]


# ---------------------------------------------------------------------------
# VisitRepository
# ---------------------------------------------------------------------------


class VisitRepository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

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
        now = datetime.now(UTC)
        cur = await self._conn.execute(
            """
            INSERT INTO visits
                (owner, place_id, date, activities, costs, narrative,
                 photo_refs, created_at)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                owner,
                place_id,
                date,
                activities,
                json.dumps(costs) if costs is not None else None,
                narrative,
                json.dumps(photo_refs) if photo_refs is not None else None,
                now.isoformat(),
            ),
        )
        await self._conn.execute(
            "UPDATE places SET status='visited', updated_at=? WHERE id=?",
            (now.isoformat(), place_id),
        )
        await self._conn.commit()
        return Visit(
            id=cur.lastrowid,
            owner=owner,
            place_id=place_id,
            date=date,
            activities=activities,
            costs=costs,
            narrative=narrative,
            photo_refs=photo_refs,
            created_at=now,
        )


# ---------------------------------------------------------------------------
# PostRepository
# ---------------------------------------------------------------------------


class PostRepository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def create_post(
        self,
        *,
        place_ids: Optional[list[int]] = None,
        body: Optional[str] = None,
        selected_image_refs: Optional[list[int]] = None,
        status: str = "draft",
        owner: str = "me",
    ) -> Post:
        now = datetime.now(UTC)
        cur = await self._conn.execute(
            """
            INSERT INTO posts
                (owner, place_ids, body, selected_image_refs, status,
                 created_at, updated_at)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                owner,
                json.dumps(place_ids) if place_ids is not None else None,
                body,
                (
                    json.dumps(selected_image_refs)
                    if selected_image_refs is not None
                    else None
                ),
                status,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        await self._conn.commit()
        return Post(
            id=cur.lastrowid,
            owner=owner,
            place_ids=place_ids,
            body=body,
            selected_image_refs=selected_image_refs,
            status=status,
            created_at=now,
            updated_at=now,
        )

    async def get_post(self, post_id: int) -> Optional[Post]:
        cur = await self._conn.execute("SELECT * FROM posts WHERE id = ?", (post_id,))
        row = await cur.fetchone()
        return Post.from_row(row) if row else None


# ---------------------------------------------------------------------------
# PreferenceRepository
# ---------------------------------------------------------------------------

_PREF_JSON_FIELDS = {"liked_features", "liked_place_ids", "budget_norms"}
_PREF_ALL_FIELDS = {
    "liked_features",
    "liked_place_ids",
    "budget_norms",
    "home_location",
    "default_radius",
}


class PreferenceRepository:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def update_preferences(
        self, *, owner: str = "me", **fields: Any
    ) -> Preferences:
        known = {k: v for k, v in fields.items() if k in _PREF_ALL_FIELDS}
        if not known:
            return await self.get_preferences(owner)

        # Build INSERT columns/values and UPDATE assignments for ON CONFLICT
        cols = ["owner"] + list(known.keys())
        vals: list[Any] = [owner]
        for k, v in known.items():
            if k in _PREF_JSON_FIELDS and v is not None:
                vals.append(json.dumps(v))
            else:
                vals.append(v)

        placeholders = ", ".join("?" * len(cols))
        col_names = ", ".join(cols)
        update_parts = ", ".join(f"{k} = excluded.{k}" for k in known.keys())

        await self._conn.execute(
            f"""
            INSERT INTO preferences ({col_names}) VALUES ({placeholders})
            ON CONFLICT(owner) DO UPDATE SET {update_parts}
            """,
            vals,
        )
        await self._conn.commit()
        return await self.get_preferences(owner)

    async def get_preferences(self, owner: str = "me") -> Preferences:
        cur = await self._conn.execute(
            "SELECT * FROM preferences WHERE owner = ?", (owner,)
        )
        row = await cur.fetchone()
        if row is None:
            return Preferences.empty(owner)
        return Preferences.from_row(row)
