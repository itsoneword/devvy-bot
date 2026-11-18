"""dv-75f1: FLUX mockups, a style spec in design terms, a critic that scores
the render per style dimension. Tests pin the contract of the new modules:

- ``src.designloop.stylespec``: ``parse_spec(text) -> StyleSpec`` (``.status``,
  ``.look_block()``), ``StyleSpecError`` names the bad section.
- ``src.model_router.flux_plain.FluxPlainCaller`` (+ ``flux_credentials()``).
- ``src.designloop.mockups.make_mockups(...)``.
- ``src.designloop.critic.DIMENSIONS`` / ``parse_mockup(text, threshold=7)``.
"""

import base64
import json
import re
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from src.model_router import CallRequest, ModelCallError

REPO = Path(__file__).resolve().parents[3]
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 16

SPEC = {
    "status": "approved",
    "palette": {
        "primary": {"hex": "#7A9E7E", "role": "buttons, active tab"},
        "surface": {"hex": "#F4F7F4", "role": "page background"},
    },
    "typography": {
        "family": "Inter",
        "roles": {
            "title": {"weight": 700, "size": 22},
            "body": {"weight": 400, "size": 15},
        },
    },
    "radii": {"button": 14, "card": 20},
    "borders": {"card": {"width": 1, "color": "#FFFFFF66"}},
    "elevation": {"card": {"offset_y": 8, "blur": 24, "opacity": 0.12}},
    "glass": {"blur": 20, "tint": "#FFFFFF33", "highlight": "#FFFFFF99"},
    "spacing": [4, 8, 12, 16, 24],
    "components": {
        "button_primary": {
            "description": "filled sage pill",
            "states": {"default": "sage fill", "pressed": "darker sage"},
        }
    },
}


def spec_text(**changes):
    data = json.loads(json.dumps(SPEC))
    for key, value in changes.items():
        if value is None:
            data.pop(key)
        else:
            data[key] = value
    return yaml.safe_dump(data)


# ---------------------------------------------------------------- C1


def test_C1_valid_spec_parses_and_gives_a_look_block():
    from src.designloop.stylespec import parse_spec

    spec = parse_spec(spec_text())
    assert spec.status == "approved"
    look = spec.look_block()
    assert "#7A9E7E" in look and "Inter" in look
    assert look == parse_spec(spec_text()).look_block()  # deterministic


@pytest.mark.parametrize(
    "section",
    [
        "palette",
        "typography",
        "radii",
        "borders",
        "elevation",
        "glass",
        "spacing",
        "components",
    ],
)
def test_C1_missing_section_is_refused_naming_it(section):
    from src.designloop.stylespec import StyleSpecError, parse_spec

    with pytest.raises(StyleSpecError, match=section):
        parse_spec(spec_text(**{section: None}))


def test_C1_malformed_hex_is_refused():
    from src.designloop.stylespec import StyleSpecError, parse_spec

    bad = dict(SPEC["palette"], primary={"hex": "sage", "role": "buttons"})
    with pytest.raises(StyleSpecError, match="palette"):
        parse_spec(spec_text(palette=bad))


def test_C1_config_carries_style_spec_and_mockups():
    from src.designloop.steps import parse_config

    raw = {
        "render": ["x.py"],
        "gate": ["g.py"],
        "python": "python3",
        "style_spec": "docs/style-spec.yaml",
        "mockups": {"preset": "best", "count": 4},
        "threshold": 7,
    }
    config = parse_config(raw)
    assert config is not None
    assert config.style_spec == "docs/style-spec.yaml"
    assert config.mockups is not None
    assert config.threshold == 7
    assert parse_config(dict(raw, threshold=2)) is None  # 5..9 only


# ---------------------------------------------------------------- C2


class FakeClient:
    """Stands in for httpx.AsyncClient; records the request."""

    calls = []
    answer = None

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        FakeClient.calls.append((url, kw))
        return FakeClient.answer

    async def get(self, url, **kw):
        # dv-c237: the caller asks /health before each submit.
        assert str(url).endswith("/health")
        return httpx.Response(
            200,
            json={"generator": "idle", "generator_last_seen_sec": 1},
            request=httpx.Request("GET", str(url)),
        )


def _resp(content=None, js=None):
    request = httpx.Request("POST", "https://flux.invalid/v1/generate")
    if js is not None:
        return httpx.Response(200, json=js, request=request)
    return httpx.Response(200, content=content, request=request)


@pytest.fixture
def flux(monkeypatch):
    from src.model_router import flux_plain

    FakeClient.calls = []
    monkeypatch.setattr(flux_plain.httpx, "AsyncClient", FakeClient)
    monkeypatch.setattr(
        flux_plain, "flux_credentials", lambda: ("tok123", "https://flux.invalid")
    )
    return flux_plain


def _req(**kw):
    return CallRequest(
        purpose="design_mockup",
        prompt="a button",
        system_prompt="",
        harness="flux",
        image_output=True,
        **kw,
    )


async def test_C2_flux_raw_png_with_bearer_and_curl_user_agent(flux):
    FakeClient.answer = _resp(content=PNG)
    result = await flux.FluxPlainCaller()(_req())
    assert result.image == PNG
    assert result.cost_usd == 0
    url, kw = FakeClient.calls[0]
    assert url.startswith("https://flux.invalid")
    assert kw["headers"]["Authorization"] == "Bearer tok123"
    assert kw["headers"]["User-Agent"].startswith("curl/")
    assert kw["json"]["prompt"] == "a button"


async def test_C2_flux_json_base64_answer(flux):
    data = "data:image/png;base64," + base64.b64encode(PNG).decode()
    FakeClient.answer = _resp(js={"images": [data]})
    result = await flux.FluxPlainCaller()(_req())
    assert result.image == PNG


async def test_C2_flux_missing_credentials_is_an_error_not_a_fallback(monkeypatch):
    from src.model_router import flux_plain

    monkeypatch.setattr(flux_plain, "flux_credentials", lambda: None)
    with pytest.raises(ModelCallError):
        await flux_plain.FluxPlainCaller()(_req())


def test_C2_flux_endpoint_only_in_the_adapter():
    adapter = REPO / "src/model_router/flux_plain.py"
    assert adapter.is_file()
    stray = []
    for root in ("src", "scripts", "agents"):
        for path in (REPO / root).rglob("*.py"):
            if path != adapter and "/v1/generate" in path.read_text(encoding="utf-8"):
                stray.append(str(path.relative_to(REPO)))
    assert stray == []


def test_C2_flux_url_is_never_a_literal():
    host = "eblu" + "sha"
    for root in ("src", "scripts", "tests", "docs"):
        for path in (REPO / root).rglob("*"):
            if path.is_file() and path.suffix in {".py", ".md", ".yaml", ".json"}:
                assert host not in path.read_text(
                    encoding="utf-8", errors="ignore"
                ), path


def test_C2_flux_routes_exist():
    from src.model_router import routes

    for purpose in ("design_mockup", "design_mockup_paid", "design_mockup_prompt"):
        assert purpose in routes.DEFAULT_CALL_ROUTES


# ---------------------------------------------------------------- C3


class FakeRouter:
    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        if request.image_output:
            return SimpleNamespace(text="", image=PNG, cost_usd=0)
        subjects = [
            {"subject": f'A card titled "Weekly plan" variant {i}', "size": "390x844"}
            for i in range(4)
        ]
        return SimpleNamespace(text=json.dumps({"subjects": subjects}), image=None)


@pytest.fixture
def router(monkeypatch):
    from src.designloop import mockups

    fake = FakeRouter()
    monkeypatch.setattr(mockups, "get_router", lambda: fake)
    return fake


async def _make(tmp_path, **kw):
    from src.designloop.mockups import make_mockups
    from src.designloop.stylespec import parse_spec

    return await make_mockups(
        spec=parse_spec(spec_text()),
        brief_text='Add a weekly plan card with the button "Start workout".',
        out_dir=tmp_path,
        **kw,
    )


async def test_C3_default_four_free_flux_mockups_with_look_block(router, tmp_path):
    from src.designloop.stylespec import parse_spec

    paths = await _make(tmp_path)
    assert len(paths) == 4 and all(Path(p).read_bytes() == PNG for p in paths)
    images = [r for r in router.requests if r.image_output]
    assert {r.purpose for r in images} == {"design_mockup"}
    look = parse_spec(spec_text()).look_block()
    assert all(look in r.prompt and "Weekly plan" in r.prompt for r in images)


async def test_C3_count_is_clamped_3_to_4(router, tmp_path):
    paths = await _make(tmp_path, count=9)
    assert len(paths) == 4
    router.requests.clear()
    assert len(await _make(tmp_path, count=1)) == 3


async def test_C3_paid_flag_uses_paid_purpose_only(router, tmp_path):
    await _make(tmp_path, paid=True)
    purposes = {r.purpose for r in router.requests if r.image_output}
    assert purposes == {"design_mockup_paid"}


async def test_C3_no_mockups_block_makes_no_mockup_call(router, tmp_path):
    from src.designloop.mockups import mockup_step

    # a board without design.mockups / approved spec has no step at all
    assert mockup_step(config=SimpleNamespace(mockups=None, style_spec=None)) is None
    assert router.requests == []


# ---------------------------------------------------------------- C4


DIMS = (
    "palette",
    "typography",
    "shape_radius",
    "depth",
    "spacing_density",
    "component_fidelity",
    "layout_fidelity",
    "text_correctness",
)


def answer(scores=None, notes=None, drop=None):
    scores = scores or {}
    dims = {
        d: {
            "score": scores.get(d, 9),
            "evidence": (notes or {}).get(d, f"the {d} chip matches"),
        }
        for d in DIMS
    }
    if drop:
        dims.pop(drop)
    return json.dumps(
        {
            "criteria": {
                k: "ok"
                for k in (
                    "hierarchy",
                    "spacing_rhythm",
                    "alignment",
                    "density",
                    "consistency",
                )
            },
            "dimensions": dims,
        }
    )


def test_C4_dimensions_are_the_eight_names():
    from src.designloop.critic import DIMENSIONS

    assert tuple(DIMENSIONS) == DIMS


def test_C4_overall_is_lowest_dimension_and_verdict_is_computed():
    from src.designloop.critic import parse_mockup

    ok = parse_mockup(answer())
    assert ok.score == 9 and ok.verdict == "ready"
    low = parse_mockup(answer({"palette": 5, "depth": 8}))
    assert low.score == 5 and low.verdict == "revise"
    edge = parse_mockup(answer({"depth": 7}))
    assert edge.verdict == "ready"
    assert parse_mockup(answer({"depth": 7}), threshold=8).verdict == "revise"


def test_C4_missing_dimension_or_empty_evidence_is_refused():
    from src.designloop.critic import parse_mockup

    with pytest.raises(ModelCallError):
        parse_mockup(answer(drop="depth"))
    with pytest.raises(ModelCallError):
        parse_mockup(answer(notes={"palette": "  "}))


def test_C4_score_out_of_range_is_refused():
    from src.designloop.critic import parse_mockup

    with pytest.raises(ModelCallError):
        parse_mockup(answer({"palette": 11}))


def test_C4_no_spec_schema_unchanged():
    from src.designloop.critic import CRITERIA, CRITIC_SCHEMA

    assert "reference_match" in CRITERIA
    assert "dimensions" not in CRITIC_SCHEMA["properties"]


# ---------------------------------------------------------------- C5


def test_C5_low_dimension_becomes_feedback_line():
    from src.designloop.critic import parse_mockup
    from src.designloop.loop import notes_of

    c = parse_mockup(
        answer({"palette": 4}, {"palette": "primary button is teal, not sage"})
    )
    notes = notes_of(c)
    assert any(
        n.startswith("palette 4/10:") and "primary button is teal" in n for n in notes
    )
    assert not any(n.startswith("depth") for n in notes)


async def test_C5_critic_call_attaches_render_mockup_and_spec(monkeypatch, tmp_path):
    from src.designloop import critic as critic_mod

    seen = []

    class R:
        async def complete(self, request):
            seen.append(request)
            return SimpleNamespace(text=answer())

    monkeypatch.setattr(critic_mod, "get_router", lambda: R())
    render, mockup = tmp_path / "r.png", tmp_path / "m.png"
    render.write_bytes(PNG)
    mockup.write_bytes(PNG)
    brief = SimpleNamespace(text="brief", refs=(), owner_led=False)
    result = await critic_mod.critique_mockup(
        brief, [render], mockup=mockup, spec_text="SPECTEXT", threshold=7
    )
    assert result.verdict == "ready"
    req = seen[0]
    assert req.purpose == "design_critic"
    assert str(render.resolve()) in req.images and str(mockup.resolve()) in req.images
    assert "target mockup" in req.prompt and "SPECTEXT" in req.prompt


async def test_C5_loop_revises_on_low_dimension_and_stops_when_it_clears(tmp_path):
    from src.designloop.brief import build_brief  # noqa: F401
    from src.designloop.critic import parse_mockup
    from src.designloop.loop import run_loop

    from .test_loop import BRANCHES, Steps

    rounds = []

    def verdicts(n, branch):
        rounds.append(n)
        return parse_mockup(answer({"palette": 4} if n == 1 else {}))

    steps = Steps(tmp_path, verdicts=verdicts)
    brief = SimpleNamespace(screen_id="feed", text="b", refs=(), owner_led=False)
    outcome = await run_loop(brief, **steps.kwargs())
    assert outcome.stop == "ready" and outcome.rounds == 2
    assert any(
        "palette 4/10" in line for fb in steps.feedback[1].values() for line in fb
    )
    assert len(BRANCHES) == 3


# ---------------------------------------------------------------- C6


async def test_C6_offer_text_shows_all_eight_scores_and_the_mockup(tmp_path):
    from src.designloop.critic import parse_mockup
    from src.designloop.loop import Outcome, Variant, offer_text

    c = parse_mockup(answer({"palette": 8, "depth": 7}))
    variant = Variant(branch="design/dv-1/v1", critique=c)
    mockup = tmp_path / "mockup-2.png"
    outcome = Outcome(stop="ready", rounds=1, variants=[variant], offered=[variant])
    outcome.mockup = mockup
    text = offer_text("Feed", outcome)
    for dim in DIMS:
        assert dim in text
    assert "palette 8/10" in text or "palette: 8/10" in text
    assert "mockup-2" in text
    assert re.search(r"reply with the number", text)
