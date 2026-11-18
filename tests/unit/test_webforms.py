"""webform_* tools (dv-5a25): owner-only mount, submit gating, deterministic filler.

No browser and no network: the filler engine runs against a fake page driver,
and the tool client against a fake child-process runner. Claim values are
obviously fake.

Contract the implementation follows (the tests are the spec):

* ``src.webforms.FORMS``: ``{form_id: FormDef}``; ``FormDef.domain``.
* ``src.webforms.engine.run_form(form, data, driver, shots_dir, submit)``
  returns a dict: ``status`` in filled / failed / unsupported / submitted /
  unconfirmed, ``screenshots`` (paths), ``failure`` (page, field, expected,
  actual, validation, screenshot), ``warnings``, ``url``, ``message``.
* The driver has: open(url), fill_field(page, key, value), read_field(page,
  key), upload(page, key, paths), upload_pending(), validation_errors(),
  next_page(), screenshot(path), click_submit(), url(), confirmation().
* ``src.claude.webform_tools``: ``WEBFORM_SPECS``, ``WebformClient(runner,
  clock)`` where ``runner`` is ``async (job: dict) -> dict`` (the child
  process) and ``clock`` is ``() -> float`` seconds; ``services.webforms``.
"""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from src.claude.agent_tools import ALL_TOOL_SPECS, available_tool_specs
from src.claude.tool_context import AgentServices, TurnContext
from src.profiles.registry import (
    ProfileDefinition,
    ProfileRegistry,
    load_profile_registry,
)
from src.security import policy

FORM = "william_russell_claim"
NAMES = {"webform_fill", "webform_submit"}

DATA: Dict[str, Any] = {
    "claim_type": "medical",
    "name": "Test Claimant",
    "address": "1 Test Street, Testville",
    "dob": "01/02/1980",
    "policy": "POL-000",
    "member": "MEM-000",
    "email": "claimant@example.invalid",
    "phone": "+000000000",
    "symptoms_from": "01/03/2026",
    "symptoms": "test symptoms",
    "consult_date": "05/03/2026",
    "diagnosis": "test diagnosis",
    "bills": [
        {
            "date": "05/03/2026",
            "invoice": "consultation",
            "amount": "50",
            "currency": "EUR",
        },
        {
            "date": "06/03/2026",
            "invoice": "lab test",
            "amount": "20.5",
            "currency": "EUR",
        },
    ],
    "payout": {"previous_account": True, "last4": "0000"},
    "files": [],
}


# -- fake page driver ----------------------------------------------------------


class FakeDriver:
    def __init__(self) -> None:
        self.page = 1
        self.values: Dict[Any, Any] = {}
        self.filled: List[Any] = []
        self.uploads: List[Any] = []
        self.shots: List[str] = []
        self.opened: List[str] = []
        self.submitted = False
        self.pending_polls = 0
        self.errors_on_page: Dict[int, List[str]] = {}
        self.read_override: Dict[Any, str] = {}
        self.cur_url = "https://www.william-russell.com/members/claims/"
        self.url_after_next: Optional[str] = None
        self.url_after_submit: Optional[str] = None
        self.confirm: Optional[str] = None

    def open(self, url: str) -> None:
        self.opened.append(url)

    def fill_field(self, page: int, key: str, value: Any) -> None:
        self.values[(page, key)] = value
        self.filled.append((page, key, value))

    def read_field(self, page: int, key: str) -> Any:
        if (page, key) in self.read_override:
            return self.read_override[(page, key)]
        return self.values.get((page, key), "")

    def upload(self, page: int, key: str, paths: List[str]) -> None:
        self.uploads.append((page, key, list(paths)))
        self.pending_polls = 3

    def upload_pending(self) -> bool:
        if self.pending_polls > 0:
            self.pending_polls -= 1
            return self.pending_polls > 0 or getattr(self, "stuck", False)
        return getattr(self, "stuck", False)

    def validation_errors(self) -> List[str]:
        return self.errors_on_page.get(self.page, [])

    def next_page(self) -> None:
        self.page += 1
        if self.url_after_next:
            self.cur_url = self.url_after_next

    def screenshot(self, path: str) -> None:
        Path(path).write_bytes(b"png")
        self.shots.append(path)

    def click_submit(self) -> None:
        self.submitted = True
        if self.url_after_submit:
            self.cur_url = self.url_after_submit

    def url(self) -> str:
        return self.cur_url

    def confirmation(self) -> Optional[str]:
        return self.confirm if self.submitted else None


def _run(driver: FakeDriver, tmp_path: Path, data: Any = None, submit: bool = False):
    from src.webforms import FORMS
    from src.webforms.engine import run_form

    return run_form(
        FORMS[FORM], copy.deepcopy(data or DATA), driver, tmp_path, submit=submit
    )


# -- C4: deterministic filler ---------------------------------------------------


def test_C4_clean_fill_covers_five_pages_and_never_submits(tmp_path: Path) -> None:
    d = FakeDriver()
    res = _run(d, tmp_path)
    assert res["status"] == "filled"
    assert d.submitted is False
    assert len(res["screenshots"]) == 5
    assert all(Path(p).exists() for p in res["screenshots"])
    sent = {v for _, _, v in d.filled}
    for key in ("name", "address", "policy", "member", "email", "diagnosis"):
        assert DATA[key] in sent, key
    assert "EUR" in sent and "20.5" in sent and "0000" in sent


def test_C4_readback_mismatch_is_a_structured_failure(tmp_path: Path) -> None:
    d = FakeDriver()
    d.read_override[(1, "name")] = "Someone Else"
    res = _run(d, tmp_path)
    assert res["status"] == "failed"
    f = res["failure"]
    assert set(f) >= {"page", "field", "expected", "actual", "validation", "screenshot"}
    assert f["expected"] == DATA["name"] and f["actual"] == "Someone Else"
    assert d.submitted is False


def test_C4_validation_error_stops_with_the_text(tmp_path: Path) -> None:
    d = FakeDriver()
    d.errors_on_page[3] = ["This field is required."]
    res = _run(d, tmp_path)
    assert res["status"] == "failed"
    assert "This field is required." in json.dumps(res["failure"])
    assert res["failure"]["screenshot"]
    assert d.submitted is False


def test_C4_unfinished_upload_is_a_failure(tmp_path: Path) -> None:
    f = tmp_path / "inv.pdf"
    f.write_bytes(b"%PDF")
    d = FakeDriver()
    d.stuck = True  # type: ignore[attr-defined]
    res = _run(d, tmp_path, {**DATA, "files": [str(f)]})
    assert res["status"] == "failed"
    assert "upload" in json.dumps(res["failure"]).lower()
    assert d.submitted is False


def test_C4_upload_waits_for_the_page_not_a_fixed_sleep(tmp_path: Path) -> None:
    f = tmp_path / "inv.pdf"
    f.write_bytes(b"%PDF")
    d = FakeDriver()
    res = _run(d, tmp_path, {**DATA, "files": [str(f)]})
    assert res["status"] == "filled"
    assert d.uploads and d.uploads[0][2] == [str(f)]
    assert d.pending_polls == 0


@pytest.mark.parametrize(
    "patch",
    [
        {"claim_type": "maternity"},
        {"payout": {"previous_account": False}},
        {"payout": {"previous_account": True}},
        {"bills": []},
    ],
)
def test_C4_unsupported_claims_are_refused_before_any_page(
    tmp_path: Path, patch: Dict[str, Any]
) -> None:
    d = FakeDriver()
    res = _run(d, tmp_path, {**DATA, **patch})
    assert res["status"] in ("unsupported", "failed")
    assert d.filled == [] and d.submitted is False


def test_C4_more_than_six_bills_is_refused(tmp_path: Path) -> None:
    bill = DATA["bills"][0]
    d = FakeDriver()
    res = _run(d, tmp_path, {**DATA, "bills": [bill] * 7})
    assert res["status"] in ("unsupported", "failed")
    assert d.filled == []


def test_C4_large_total_carries_a_physician_warning(tmp_path: Path) -> None:
    big = {**DATA, "bills": [{**DATA["bills"][0], "amount": "900"}]}
    res = _run(FakeDriver(), tmp_path, big)
    assert res["status"] == "filled"
    assert res["warnings"]


def test_C4_engine_makes_no_model_call() -> None:
    import src.webforms.engine as engine

    src = Path(engine.__file__).read_text()
    for banned in ("claude", "codex", "model_router", "anthropic"):
        assert banned not in src.lower(), banned


# -- C5: domain pin, uploads -----------------------------------------------------


def test_C5_url_pin() -> None:
    from src.webforms import FORMS
    from src.webforms.engine import url_allowed

    form = FORMS[FORM]
    assert form.domain.endswith("william-russell.com")
    assert url_allowed(form, "https://www.william-russell.com/members/claims/")
    assert url_allowed(form, "https://william-russell.com/x")
    assert url_allowed(form, "https://sub.william-russell.com/x")
    assert not url_allowed(form, "https://evil.example/x")
    assert not url_allowed(form, "https://william-russell.com.evil.example/x")
    assert not url_allowed(form, "https://evilwilliam-russell.com/x")
    assert not url_allowed(form, "http://127.0.0.1/x")
    assert not url_allowed(form, "file:///etc/passwd")


def test_C5_navigation_off_domain_aborts_the_run(tmp_path: Path) -> None:
    d = FakeDriver()
    d.url_after_next = "https://evil.example/phish"
    res = _run(d, tmp_path)
    assert res["status"] == "failed"
    assert "evil.example" in json.dumps(res)
    assert d.submitted is False
    assert d.page < 5


def _owner_services(runner: Any, clock: Any = None) -> AgentServices:
    from src.claude.webform_tools import WebformClient

    client = WebformClient(runner, clock=clock) if clock else WebformClient(runner)
    services = AgentServices(
        event_bus=None,
        task_repo=None,
        profile_registry=ProfileRegistry([_profile("coach", True)]),
    )
    services.webforms = client
    return services


def _spec(name: str) -> Any:
    from src.claude.webform_tools import WEBFORM_SPECS

    return next(s for s in WEBFORM_SPECS if s.name == name)


class Runner:
    def __init__(self, result: Optional[Dict[str, Any]] = None) -> None:
        self.jobs: List[Dict[str, Any]] = []
        self.result = result or {
            "status": "filled",
            "screenshots": [],
            "warnings": [],
            "url": "https://www.william-russell.com/members/claims/",
            "message": "ok",
        }

    async def __call__(self, job: Dict[str, Any]) -> Dict[str, Any]:
        self.jobs.append(copy.deepcopy(job))
        res = dict(self.result)
        if job.get("submit"):
            res = {**res, "status": "submitted"}
        return res


def _ctx(tmp_path: Path, user: int = 5, role: str = "owner") -> TurnContext:
    ws = tmp_path / f"ws{user}"
    ws.mkdir(exist_ok=True)
    return TurnContext(
        profile_slug="coach",
        working_dir=ws,
        user_id=user,
        role=role,
        memory_agent_id="coach",
    )


def _call(
    spec: Any, services: AgentServices, ctx: TurnContext, data: Any
) -> Dict[str, Any]:
    return asyncio.run(
        spec.handler(services, ctx, {"form": FORM, "data": copy.deepcopy(data)})
    )


@pytest.mark.parametrize(
    "name,ext,size,ok",
    [
        ("a.pdf", "", 100, True),
        ("a.jpg", "", 100, True),
        ("a.png", "", 100, True),
        ("a.exe", "", 100, False),
        ("a.sh", "", 100, False),
        ("big.pdf", "", 11 * 1024 * 1024, False),
    ],
)
def test_C5_upload_extension_and_size(
    tmp_path: Path, name: str, ext: str, size: int, ok: bool
) -> None:
    ctx = _ctx(tmp_path)
    f = ctx.working_dir / name
    f.write_bytes(b"x" * size)
    runner = Runner()
    res = _call(
        _spec("webform_fill"), _owner_services(runner), ctx, {**DATA, "files": [str(f)]}
    )
    assert bool(runner.jobs) == ok
    assert bool(res.get("isError")) == (not ok)


def test_C5_upload_total_over_10mb_refused(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    paths = []
    for i in range(3):
        f = ctx.working_dir / f"p{i}.pdf"
        f.write_bytes(b"x" * (4 * 1024 * 1024))
        paths.append(str(f))
    runner = Runner()
    res = _call(
        _spec("webform_fill"), _owner_services(runner), ctx, {**DATA, "files": paths}
    )
    assert res.get("isError") and not runner.jobs


def test_C5_upload_outside_own_workspace_refused(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, user=5)
    other = _ctx(tmp_path, user=6)
    secret = other.working_dir / "theirs.pdf"
    secret.write_bytes(b"x")
    link = ctx.working_dir / "link.pdf"
    link.symlink_to(secret)
    runner = Runner()
    svc = _owner_services(runner)
    for p in (
        str(secret),
        str(link),
        str(ctx.working_dir / ".." / "ws6" / "theirs.pdf"),
        "/etc/hostname",
    ):
        res = _call(_spec("webform_fill"), svc, ctx, {**DATA, "files": [p]})
        assert res.get("isError"), p
    assert not runner.jobs


def test_C5_relative_upload_resolves_inside_workspace(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    (ctx.working_dir / "inv.pdf").write_bytes(b"x")
    runner = Runner()
    res = _call(
        _spec("webform_fill"),
        _owner_services(runner),
        ctx,
        {**DATA, "files": ["inv.pdf"]},
    )
    assert not res.get("isError")
    sent = runner.jobs[0]["data"]["files"]
    assert sent == [str((ctx.working_dir / "inv.pdf").resolve())]


def test_C5_run_output_goes_under_the_users_workspace(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    runner = Runner()
    _call(_spec("webform_fill"), _owner_services(runner), ctx, DATA)
    out = Path(runner.jobs[0]["out_dir"]).resolve()
    assert (ctx.working_dir / "claims").resolve() in out.parents


# -- C1: owner-only mount ---------------------------------------------------------


def _profile(
    slug: str, web_forms: bool, roles: Any = ("owner", "guest")
) -> ProfileDefinition:
    return ProfileDefinition(
        slug=slug,
        name=slug,
        working_dir=Path(f"/tmp/{slug}"),
        memory_dir=Path(f"/tmp/{slug}/memory"),
        persona=Path(f"/tmp/{slug}/persona.md"),
        default=True,
        web_forms=[FORM] if web_forms else [],
    )


def test_C1_specs_registered_on_both_transports() -> None:
    from src.claude.webform_tools import WEBFORM_SPECS

    assert {s.name for s in WEBFORM_SPECS} == NAMES
    assert NAMES <= {s.name for s in ALL_TOOL_SPECS}


@pytest.mark.parametrize(
    "has_client,slug,role,web_forms,expected",
    [
        (True, "coach", "owner", True, True),
        (True, "coach#u5", "owner", True, True),
        (True, "coach", "guest", True, False),
        (True, "coach#u7", "guest", True, False),
        (True, "coach", None, True, False),
        (True, "coach", "owner", False, False),
        (False, "coach", "owner", True, False),
    ],
)
def test_C1_mounting(
    has_client: bool, slug: str, role: Any, web_forms: bool, expected: bool
) -> None:
    services = AgentServices(
        event_bus=None,
        task_repo=None,
        profile_registry=ProfileRegistry([_profile("coach", web_forms)]),
    )
    services.webforms = _owner_services(Runner()).webforms if has_client else None
    ctx = TurnContext(profile_slug=slug, user_id=5, role=role)
    names = {s.name for s in available_tool_specs(services, ctx)}
    assert (NAMES <= names) == expected
    assert bool(NAMES & names) == expected


@pytest.mark.parametrize("tool", ["webform_fill", "webform_submit"])
@pytest.mark.parametrize("role", ["guest", None])
def test_C1_handlers_recheck_the_role(tmp_path: Path, tool: str, role: Any) -> None:
    runner = Runner()
    res = _call(
        _spec(tool),
        _owner_services(runner),
        (
            _ctx(tmp_path, role=role or "guest")
            if role
            else TurnContext(
                profile_slug="coach", working_dir=tmp_path, user_id=5, role=None
            )
        ),
        DATA,
    )
    assert res.get("isError")
    assert not runner.jobs


def test_C1_handler_refuses_a_profile_without_the_form(tmp_path: Path) -> None:
    runner = Runner()
    svc = _owner_services(runner)
    svc.profile_registry = ProfileRegistry([_profile("coach", False)])
    for tool in NAMES:
        assert _call(_spec(tool), svc, _ctx(tmp_path), DATA).get("isError")
    assert not runner.jobs


def test_C1_unknown_form_id_is_refused(tmp_path: Path) -> None:
    runner = Runner()
    res = asyncio.run(
        _spec("webform_fill").handler(
            _owner_services(runner),
            _ctx(tmp_path),
            {"form": "other_form", "data": DATA},
        )
    )
    assert res.get("isError") and not runner.jobs


# -- C2: policy ----------------------------------------------------------------


@pytest.mark.parametrize("full_access", [True, False])
def test_C2_submit_asks_with_a_preview_of_what_is_filed(full_access: bool) -> None:
    f = "/home/testuser/ws/claims/inv.pdf"
    args = {"form": FORM, "data": {**DATA, "files": [f]}}
    turn = policy.TurnPolicy(
        working_directory=Path("/home/testuser/Devvybot/devvy_bot"),
        full_access=full_access,
    )
    d = policy.decide("mcp__agent__webform_submit", args, turn)
    assert d.action == policy.ASK
    text = d.reason + "\n" + d.preview
    assert FORM in text or "William Russell" in text
    assert "Test Claimant" in text
    for needle in ("05/03/2026", "50", "20.5", "EUR", "inv.pdf"):
        assert needle in text, needle
    fill = policy.decide("mcp__agent__webform_fill", args, turn)
    assert fill.action == policy.ALLOW


def test_C2_submit_ask_survives_wrapped_arguments_and_foreign_server() -> None:
    turn = policy.TurnPolicy(
        working_directory=Path("/home/testuser/x"), full_access=False
    )
    args = {"form": FORM, "data": DATA}
    assert (
        policy.decide("mcp__agent__webform_submit", {"arguments": args}, turn).action
        == policy.ASK
    )
    assert policy.decide("mcp__other__webform_submit", args, turn).action == policy.ASK
    assert policy.decide("mcp__other__webform_fill", args, turn).action == policy.ASK


def test_C2_parity_rows_exist() -> None:
    text = (
        Path(__file__).parent / "test_security" / "test_policy_parity.py"
    ).read_text()
    assert "mcp__agent__webform_submit" in text and "mcp__agent__webform_fill" in text


# -- C3: submit gating --------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def test_C3_submit_without_a_prior_fill_is_refused(tmp_path: Path) -> None:
    runner = Runner()
    res = _call(_spec("webform_submit"), _owner_services(runner), _ctx(tmp_path), DATA)
    assert res.get("isError") and not runner.jobs


def test_C3_submit_after_identical_fill_submits_exactly_that_data(
    tmp_path: Path,
) -> None:
    runner = Runner()
    svc = _owner_services(runner)
    ctx = _ctx(tmp_path)
    assert not _call(_spec("webform_fill"), svc, ctx, DATA).get("isError")
    res = _call(_spec("webform_submit"), svc, ctx, DATA)
    assert not res.get("isError")
    assert [j["submit"] for j in runner.jobs] == [False, True]
    assert runner.jobs[1]["data"] == runner.jobs[0]["data"]
    assert runner.jobs[1]["form"] == FORM


def test_C3_key_order_does_not_matter_but_any_value_does(tmp_path: Path) -> None:
    runner = Runner()
    svc = _owner_services(runner)
    ctx = _ctx(tmp_path)
    _call(_spec("webform_fill"), svc, ctx, DATA)
    reordered = dict(reversed(list(DATA.items())))
    assert not _call(_spec("webform_submit"), svc, ctx, reordered).get("isError")
    changed = copy.deepcopy(DATA)
    changed["bills"][0]["amount"] = "500"
    n = len(runner.jobs)
    assert _call(_spec("webform_submit"), svc, ctx, changed).get("isError")
    assert len(runner.jobs) == n


def test_C3_changed_upload_content_is_refused(tmp_path: Path) -> None:
    runner = Runner()
    svc = _owner_services(runner)
    ctx = _ctx(tmp_path)
    f = ctx.working_dir / "inv.pdf"
    f.write_bytes(b"one")
    data = {**DATA, "files": [str(f)]}
    _call(_spec("webform_fill"), svc, ctx, data)
    f.write_bytes(b"two")
    n = len(runner.jobs)
    assert _call(_spec("webform_submit"), svc, ctx, data).get("isError")
    assert len(runner.jobs) == n


def test_C3_fill_older_than_30_minutes_is_stale(tmp_path: Path) -> None:
    clock = Clock()
    runner = Runner()
    svc = _owner_services(runner, clock)
    ctx = _ctx(tmp_path)
    _call(_spec("webform_fill"), svc, ctx, DATA)
    clock.t += 29 * 60
    assert not _call(_spec("webform_submit"), svc, ctx, DATA).get("isError")
    _call(_spec("webform_fill"), svc, ctx, DATA)
    clock.t += 31 * 60
    n = len(runner.jobs)
    assert _call(_spec("webform_submit"), svc, ctx, DATA).get("isError")
    assert len(runner.jobs) == n


def test_C3_another_users_fill_does_not_count(tmp_path: Path) -> None:
    runner = Runner()
    svc = _owner_services(runner)
    _call(_spec("webform_fill"), svc, _ctx(tmp_path, user=5), DATA)
    n = len(runner.jobs)
    assert _call(_spec("webform_submit"), svc, _ctx(tmp_path, user=6), DATA).get(
        "isError"
    )
    assert len(runner.jobs) == n


def test_C3_failed_fill_does_not_unlock_submit(tmp_path: Path) -> None:
    bad = Runner(
        {"status": "failed", "failure": {"page": 2, "field": "name"}, "screenshots": []}
    )
    svc = _owner_services(bad)
    ctx = _ctx(tmp_path)
    _call(_spec("webform_fill"), svc, ctx, DATA)
    n = len(bad.jobs)
    assert _call(_spec("webform_submit"), svc, ctx, DATA).get("isError")
    assert len(bad.jobs) == n


# -- C6: submit outcome -----------------------------------------------------------------


def _submit_driver(**kw: Any) -> FakeDriver:
    d = FakeDriver()
    for k, v in kw.items():
        setattr(d, k, v)
    return d


def test_C6_confirmation_message_means_submitted(tmp_path: Path) -> None:
    d = _submit_driver(confirm="Thank you")
    res = _run(d, tmp_path, submit=True)
    assert d.submitted and res["status"] == "submitted"


def test_C6_redirect_to_a_non_form_page_means_submitted(tmp_path: Path) -> None:
    d = _submit_driver(url_after_submit="https://www.william-russell.com/thank-you/")
    res = _run(d, tmp_path, submit=True)
    assert res["status"] == "submitted"
    assert res["url"].endswith("/thank-you/")


def test_C6_no_sign_of_success_is_unconfirmed_and_says_do_not_resubmit(
    tmp_path: Path,
) -> None:
    d = _submit_driver()
    res = _run(d, tmp_path, submit=True)
    assert res["status"] == "unconfirmed"
    assert res["url"]
    assert res["screenshots"] and Path(res["screenshots"][-1]).exists()
    low = res["message"].lower()
    assert "do not resubmit" in low and "email" in low


def test_C6_validation_error_after_submit_is_not_submitted(tmp_path: Path) -> None:
    d = _submit_driver(url_after_submit="https://www.william-russell.com/thank-you/")
    d.errors_on_page[5] = ["There was a problem with your submission."]
    res = _run(d, tmp_path, submit=True)
    assert res["status"] != "submitted"


def test_C6_failed_fill_never_clicks_submit(tmp_path: Path) -> None:
    d = _submit_driver()
    d.read_override[(1, "name")] = "x"
    res = _run(d, tmp_path, submit=True)
    assert res["status"] == "failed" and not d.submitted


# -- C7: profile key, settings, preflight ------------------------------------------------

_YAML = """
profiles:
  - slug: coach
    name: Coach
    working_dir: /tmp/coach
    memory_dir: /tmp/coach/m
    persona: /tmp/coach/p.md
    default: true
    web_forms: [{forms}]
    permission_policy:
      allowed_roles: [owner, guest]
"""


def _load(tmp_path: Path, forms: str) -> ProfileRegistry:
    cfg = tmp_path / "profiles.yaml"
    cfg.write_text(_YAML.format(forms=forms), encoding="utf-8")
    return load_profile_registry(cfg, sandbox_default=True, data_root=None)


def test_C7_key_loads_on_a_guest_allowed_sandboxed_profile(tmp_path: Path) -> None:
    reg = _load(tmp_path, FORM)
    prof = reg.get_by_slug("coach")
    assert prof is not None and list(prof.web_forms) == [FORM]
    assert prof.sandbox is not False


def test_C7_unknown_form_id_is_a_load_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="web_forms"):
        _load(tmp_path, "no_such_form")


def test_C7_key_is_documented() -> None:
    root = Path(__file__).resolve().parents[2]
    for rel in ("config/profiles.example.yaml", "docs/configuration.md"):
        assert "web_forms" in (root / rel).read_text(), rel
    assert "WEBFORMS_BROWSERS_PATH" in (root / "docs/configuration.md").read_text()


def test_C7_setting_exists() -> None:
    from src.config.settings import Settings

    assert "webforms_browsers_path" in Settings.model_fields


def test_C7_runtime_problem_reports_a_missing_browser(tmp_path: Path) -> None:
    from src.webforms import runtime_problem

    on = ProfileRegistry([_profile("coach", True)])
    off = ProfileRegistry([_profile("coach", False)])
    # A profile that does not enable the key never fails the gate.
    assert runtime_problem(off, str(tmp_path / "nope")) is None
    # Enabled + an empty browsers dir: Chromium is missing (or Playwright is).
    empty = tmp_path / "browsers"
    empty.mkdir()
    problem = runtime_problem(on, str(empty))
    assert problem and (
        "chromium" in problem.lower() or "playwright" in problem.lower()
    )


def test_C7_preflight_wires_the_runtime_check() -> None:
    root = Path(__file__).resolve().parents[2]
    assert "runtime_problem" in (root / "src/boot/check.py").read_text()
