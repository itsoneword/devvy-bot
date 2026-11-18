"""Owner 13.11.26: mockups go paid when the run costs at most paid_max_usd."""

from pathlib import Path

from src.designloop.mockups import PAID_IMAGE_USD, MockupStep
from src.designloop.steps import _parse_mockups


def _step(count: int, budget: float) -> MockupStep:
    return MockupStep(spec=None, spec_path=Path("x"), count=count, paid_max_usd=budget)  # type: ignore[arg-type]


def test_paid_when_run_fits_budget() -> None:
    assert _step(4, 0.5).paid
    assert 4 * PAID_IMAGE_USD <= 0.5


def test_free_when_budget_zero_or_too_small() -> None:
    assert not _step(4, 0.0).paid
    assert not _step(4, 0.1).paid


def test_parse_paid_max_usd() -> None:
    assert _parse_mockups({"count": 4}).paid_max_usd == 0.0
    assert _parse_mockups({"paid_max_usd": 0.5}).paid_max_usd == 0.5
    for bad in (-1, True, "0.5"):
        try:
            _parse_mockups({"paid_max_usd": bad})
        except ValueError:
            continue
        raise AssertionError(bad)


def test_parse_key_ref() -> None:
    assert _parse_mockups({}).key is None
    assert _parse_mockups({"key": "acme:K"}).key == "acme:K"
    for bad in ("nocolon", 5):
        try:
            _parse_mockups({"key": bad})
        except ValueError:
            continue
        raise AssertionError(bad)


def test_openrouter_key_ref_reads_that_key_only(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from src.model_router import routes

    class Store:
        def env(self, service: str) -> dict:
            return {"K": "v"} if service == "s" else {}

    monkeypatch.setattr("src.security.keystore.default_keystore", lambda: Store())
    assert routes.openrouter_key("s:K") == "v"
    assert routes.openrouter_key("s:MISSING") is None
    assert routes.openrouter_key("other:K") is None
