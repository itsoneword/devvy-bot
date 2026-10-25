"""/internal/notify `deliver` with a profile the registry does not know
(dv-a9b0): routed by source as before, never a 4xx (the pump's outbox would
retry a rejected card forever)."""

from unittest.mock import AsyncMock

from src.events.bus import EventBus
from tests.unit.test_api import test_internal_endpoints as base


def test_deliver_to_an_unknown_profile_is_ignored_not_rejected() -> None:
    bus = EventBus()
    bus.publish = AsyncMock()  # type: ignore[method-assign]
    r = base.TestInternalNotifyDeliver._client(bus).post(
        "/internal/notify",
        json={"text": "q?", "profile": "nope", "deliver": True},
        headers=base.AUTH,
    )
    assert r.status_code == 200
    assert not bus.publish.call_args[0][0].deliver_profile
