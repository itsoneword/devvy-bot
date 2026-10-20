"""Tests for job_visible_to (dv-8c19): the single authorization rule shared
by the agent tools (schedule_list/schedule_remove) and the /schedules
command."""

from src.scheduler.scheduler import job_visible_to


def _row(created_by: int, target_chat_ids: str = "") -> dict:
    return {
        "job_id": "j1",
        "created_by": created_by,
        "target_chat_ids": target_chat_ids,
    }


class TestJobVisibleTo:
    def test_creator_sees_own_job(self) -> None:
        row = _row(created_by=111)
        assert job_visible_to(row, user_id=111, role="owner") is True

    def test_other_owner_role_still_denied(self) -> None:
        """Two owner-role users must not see each other's attributed jobs —
        deliberately NOT 'owner sees everything'."""
        row = _row(created_by=111)
        assert job_visible_to(row, user_id=222, role="owner") is False

    def test_target_of_job_is_visible_even_if_not_creator(self) -> None:
        """A job one family member scheduled FOR another (e.g. the live
        friday-weighin-reminder): the recipient must see/manage it too."""
        row = _row(created_by=111, target_chat_ids="222")
        assert job_visible_to(row, user_id=222, role="guest") is False
        assert (
            job_visible_to(row, user_id=222, role="guest", requester_chat_id=222)
            is True
        )

    def test_multi_target_membership(self) -> None:
        row = _row(created_by=111, target_chat_ids="222,333")
        assert (
            job_visible_to(row, user_id=999, role="guest", requester_chat_id=333)
            is True
        )
        assert (
            job_visible_to(row, user_id=999, role="guest", requester_chat_id=444)
            is False
        )

    def test_legacy_unattributed_row_owner_only(self) -> None:
        row = _row(created_by=0)
        assert job_visible_to(row, user_id=111, role="owner") is True
        assert job_visible_to(row, user_id=111, role="guest") is False
        assert job_visible_to(row, user_id=None, role=None) is False

    def test_no_user_id_denied_for_attributed_row(self) -> None:
        row = _row(created_by=111)
        assert job_visible_to(row, user_id=None, role="owner") is False
