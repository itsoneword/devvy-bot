"""dv-4517: per-turn cost from the CLI's session-cumulative total."""

from src.model_router.turn_cost import SessionCostTracker


def test_fresh_then_resumed_turns_record_deltas() -> None:
    t = SessionCostTracker()
    assert t.turn_cost(1.0, resumed_from=None, final_session_id="s") == 1.0
    d = t.turn_cost(1.5, resumed_from="s", final_session_id="s")
    assert abs(d - 0.5) < 1e-9
    d = t.turn_cost(2.0, resumed_from="s", final_session_id="s2")  # fork id
    assert abs(d - 0.5) < 1e-9
    assert abs(t.turn_cost(2.2, resumed_from="s2", final_session_id="s2") - 0.2) < 1e-9


def test_reset_uses_total() -> None:
    t = SessionCostTracker()
    t.turn_cost(5.0, resumed_from=None, final_session_id="s")
    assert t.turn_cost(0.3, resumed_from="s", final_session_id="s") == 0.3


def test_resumed_without_baseline_uses_token_fallback() -> None:
    t = SessionCostTracker()
    assert (
        t.turn_cost(15.0, resumed_from="s", final_session_id="s", fallback=0.1) == 0.1
    )
    assert abs(t.turn_cost(15.2, resumed_from="s", final_session_id="s") - 0.2) < 1e-9
