"""Channel KB domain models."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_list(raw: Any) -> Optional[list[Any]]:
    if isinstance(raw, str):
        result: list[Any] = json.loads(raw)
        return result
    return None


def _decode_dict(raw: Any) -> Optional[dict[str, Any]]:
    if isinstance(raw, str):
        result2: dict[str, Any] = json.loads(raw)
        return result2
    return None


@dataclass
class Place:
    id: Optional[int]
    owner: str
    name: str
    type: Optional[str]
    location: Optional[str]
    lat: Optional[float]
    lon: Optional[float]
    travel_time_from_home: Optional[str]
    price: Optional[str]
    opening_hours: Optional[str]
    features: Optional[list[str]]
    status: str
    liked: Optional[bool]
    my_notes: Optional[str]
    source: Optional[str]
    enriched_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime
    score: Optional[float] = None

    @classmethod
    def from_row(cls, row: Any) -> "Place":
        """Build a Place from an aiosqlite.Row."""
        raw_features = row["features"]
        if isinstance(raw_features, str):
            features: Optional[list[str]] = json.loads(raw_features)
        else:
            features = None

        enriched_at = row["enriched_at"]
        if isinstance(enriched_at, str):
            enriched_at = datetime.fromisoformat(enriched_at)

        created_at = row["created_at"]
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)

        updated_at = row["updated_at"]
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)

        liked_raw = row["liked"]
        liked: Optional[bool] = None if liked_raw is None else bool(liked_raw)

        return cls(
            id=row["id"],
            owner=row["owner"],
            name=row["name"],
            type=row["type"],
            location=row["location"],
            lat=row["lat"],
            lon=row["lon"],
            travel_time_from_home=row["travel_time_from_home"],
            price=row["price"],
            opening_hours=row["opening_hours"],
            features=features,
            status=row["status"],
            liked=liked,
            my_notes=row["my_notes"],
            source=row["source"],
            enriched_at=enriched_at,
            created_at=created_at,
            updated_at=updated_at,
        )

    @classmethod
    def new(
        cls,
        *,
        name: str,
        owner: str = "me",
        created_at: datetime,
        updated_at: datetime,
        **fields: Any,
    ) -> "Place":
        """Construct a new unsaved Place. Unknown keys in fields are silently ignored."""
        known = {
            "type",
            "location",
            "lat",
            "lon",
            "travel_time_from_home",
            "price",
            "opening_hours",
            "features",
            "status",
            "liked",
            "my_notes",
            "source",
            "enriched_at",
        }
        filtered = {k: v for k, v in fields.items() if k in known}
        return cls(
            id=None,
            owner=owner,
            name=name,
            type=filtered.get("type"),
            location=filtered.get("location"),
            lat=filtered.get("lat"),
            lon=filtered.get("lon"),
            travel_time_from_home=filtered.get("travel_time_from_home"),
            price=filtered.get("price"),
            opening_hours=filtered.get("opening_hours"),
            features=filtered.get("features"),
            status=filtered.get("status", "idea"),
            liked=filtered.get("liked"),
            my_notes=filtered.get("my_notes"),
            source=filtered.get("source"),
            enriched_at=filtered.get("enriched_at"),
            created_at=created_at,
            updated_at=updated_at,
        )

    def merged_with(self, *, name: str, **fields: Any) -> "Place":
        """Return a copy with non-None incoming fields overriding existing ones.

        id, owner, and created_at are always preserved from self.
        """
        from dataclasses import replace

        overrides: dict[str, Any] = {"name": name}
        allowed = {
            "type",
            "location",
            "lat",
            "lon",
            "travel_time_from_home",
            "price",
            "opening_hours",
            "features",
            "status",
            "liked",
            "my_notes",
            "source",
            "enriched_at",
            "updated_at",
        }
        for k, v in fields.items():
            if k in allowed and v is not None:
                overrides[k] = v

        return replace(self, **overrides)


# ---------------------------------------------------------------------------
# PlaceImage
# ---------------------------------------------------------------------------


@dataclass
class PlaceImage:
    id: Optional[int]
    owner: str
    place_id: Optional[int]
    file_path: str
    caption: Optional[str]
    tags: Optional[list[str]]
    vision_model: Optional[str]
    confirmed: bool
    created_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> "PlaceImage":
        created_at = row["created_at"]
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        return cls(
            id=row["id"],
            owner=row["owner"],
            place_id=row["place_id"],
            file_path=row["file_path"],
            caption=row["caption"],
            tags=_decode_list(row["tags"]),
            vision_model=row["vision_model"],
            confirmed=bool(row["confirmed"]),
            created_at=created_at,
        )


# ---------------------------------------------------------------------------
# Reference
# ---------------------------------------------------------------------------


@dataclass
class Reference:
    id: Optional[int]
    owner: str
    source_channel: Optional[str]
    text: Optional[str]
    image_refs: Optional[list[int]]
    tags: Optional[list[str]]
    notes: Optional[str]
    created_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> "Reference":
        created_at = row["created_at"]
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        return cls(
            id=row["id"],
            owner=row["owner"],
            source_channel=row["source_channel"],
            text=row["text"],
            image_refs=_decode_list(row["image_refs"]),
            tags=_decode_list(row["tags"]),
            notes=row["notes"],
            created_at=created_at,
        )


# ---------------------------------------------------------------------------
# Visit
# ---------------------------------------------------------------------------


@dataclass
class Visit:
    id: Optional[int]
    owner: str
    place_id: int
    date: Optional[str]
    activities: Optional[str]
    costs: Optional[dict]  # type: ignore[type-arg]
    narrative: Optional[str]
    photo_refs: Optional[list[int]]
    created_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> "Visit":
        created_at = row["created_at"]
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        return cls(
            id=row["id"],
            owner=row["owner"],
            place_id=row["place_id"],
            date=row["date"],
            activities=row["activities"],
            costs=_decode_dict(row["costs"]),
            narrative=row["narrative"],
            photo_refs=_decode_list(row["photo_refs"]),
            created_at=created_at,
        )


# ---------------------------------------------------------------------------
# Post
# ---------------------------------------------------------------------------


@dataclass
class Post:
    id: Optional[int]
    owner: str
    place_ids: Optional[list[int]]
    body: Optional[str]
    selected_image_refs: Optional[list[int]]
    status: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> "Post":
        created_at = row["created_at"]
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        updated_at = row["updated_at"]
        if isinstance(updated_at, str):
            updated_at = datetime.fromisoformat(updated_at)
        return cls(
            id=row["id"],
            owner=row["owner"],
            place_ids=_decode_list(row["place_ids"]),
            body=row["body"],
            selected_image_refs=_decode_list(row["selected_image_refs"]),
            status=row["status"],
            created_at=created_at,
            updated_at=updated_at,
        )


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


@dataclass
class Preferences:
    owner: str
    liked_features: Optional[list[str]]
    liked_place_ids: Optional[list[int]]
    budget_norms: Optional[dict]  # type: ignore[type-arg]
    home_location: Optional[str]
    default_radius: Optional[str]

    @classmethod
    def from_row(cls, row: Any) -> "Preferences":
        return cls(
            owner=row["owner"],
            liked_features=_decode_list(row["liked_features"]),
            liked_place_ids=_decode_list(row["liked_place_ids"]),
            budget_norms=_decode_dict(row["budget_norms"]),
            home_location=row["home_location"],
            default_radius=row["default_radius"],
        )

    @classmethod
    def empty(cls, owner: str) -> "Preferences":
        return cls(
            owner=owner,
            liked_features=None,
            liked_place_ids=None,
            budget_norms=None,
            home_location=None,
            default_radius=None,
        )
