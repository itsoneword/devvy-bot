"""The design critic: Codex, through the model router, with the renders as
images (dv-9133 phase 2).

Contract (docs/research/dv-9133/analysis.md, phase 2 step 4):
``src.designloop.critic.critique(brief, renders, references=()) -> Critique``
(async) does one ``get_router().complete(CallRequest(purpose="design_critic",
images=..., json_schema=...))``. The schema is fixed: ``criteria`` (6
criteria, each ``ok`` | ``fix: <what>``) and ``verdict`` ``ready`` |
``revise``. The critic always sees the current screen (``brief.refs``) next to
the variant renders. ``Critique.verdict`` / ``Critique.criteria``.
"""

import asyncio
import inspect
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.model_router import (
    CallRequest,
    CallResult,
    ModelCallError,
    ModelRouter,
    set_router,
)
from src.model_router.codex_plain import CodexPlainCaller
from src.model_router.harness import HarnessVersions
from src.storage.database import DatabaseManager
from src.storage.model_calls import ModelCallRepository
from tests.unit.test_designloop.helpers import FEED_FEATURES, conforming


class _Versions(HarnessVersions):
    async def get(self, harness: str) -> str:
        return f"{harness}-9.9"


class _NeverClaude:
    def __init__(self) -> None:
        self.calls: List[CallRequest] = []

    async def __call__(self, request: CallRequest) -> CallResult:
        self.calls.append(request)
        raise AssertionError("the design critic never runs on Claude")


class _FakeProcess:
    pid = 4242
    returncode = 0

    def __init__(self) -> None:
        self.received_input: bytes = b""

    async def communicate(self, input: bytes = b"") -> Any:  # noqa: A002
        self.received_input = input
        return b"", b""


@pytest.fixture
async def db(tmp_path):
    manager = DatabaseManager(f"sqlite:///{tmp_path / 'test.db'}")
    await manager.initialize()
    yield manager
    await manager.close()


@pytest.fixture(autouse=True)
def _reset_router():
    yield
    set_router(None)


async def _rows(db: DatabaseManager) -> List[dict]:
    async with db.get_connection() as conn:
        cur = await conn.execute("SELECT * FROM model_calls ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


async def _maybe(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _renders(tmp_path: Path) -> List[Path]:
    out = []
    for variant in ("v1", "v2"):
        for theme in ("dark", "light"):
            path = tmp_path / ".screens" / "design" / variant / f"main-{theme}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"\x89PNG")
            out.append(path)
    return out


async def test_critic_sends_renders_to_codex_as_images_and_gets_the_fixed_json(
    gymbro_repo, tmp_path, monkeypatch, db
):
    """End to end down to the `codex exec` argv: every render and every
    current-screen reference is an `-i` image, the brief goes over stdin,
    the answer is parsed into 6 criteria + a verdict, one row is recorded."""
    from src.designloop.brief import build_brief
    from src.designloop.critic import critique

    captured: Dict[str, Any] = {}
    process = _FakeProcess()

    async def fake_exec(*args: str, **kwargs: Any) -> _FakeProcess:
        argv = list(args)
        captured["argv"] = argv
        schema = json.loads(Path(argv[argv.index("--output-schema") + 1]).read_text())
        captured["schema"] = schema
        answer = conforming(schema, "the filter row crowds the first card")
        Path(argv[argv.index("-o") + 1]).write_text(json.dumps(answer))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    claude = _NeverClaude()
    set_router(
        ModelRouter(
            plain_callers={"openai_codex": CodexPlainCaller(), "claude": claude},
            recorder=ModelCallRepository(db),
            versions=_Versions(),
        )
    )
    brief = build_brief(gymbro_repo, "feed", "Make the feed calmer.")
    renders = _renders(tmp_path)

    result = await _maybe(critique(brief, renders))

    argv = captured["argv"]
    images = [str(Path(argv[i + 1]).resolve()) for i, t in enumerate(argv) if t == "-i"]
    for path in list(renders) + [Path(p) for p in brief.refs]:
        assert str(Path(path).resolve()) in images, path
    assert argv[-1] == "-"
    prompt = process.received_input.decode()
    for feature in FEED_FEATURES:
        assert feature in prompt, feature

    # The fixed schema: 6 criteria + verdict ready|revise.
    schema = captured["schema"]
    criteria = schema["properties"]["criteria"]["properties"]
    assert len(criteria) == 6
    assert set(schema["properties"]["verdict"]["enum"]) == {"ready", "revise"}

    assert result.verdict == "revise"
    assert set(result.criteria) == set(criteria)
    fixes = [v for v in result.criteria.values() if v != "ok"]
    assert len(fixes) == 1 and fixes[0].startswith("fix:")
    assert "filter row crowds the first card" in fixes[0]

    assert not claude.calls
    (row,) = await _rows(db)
    assert row["purpose"] == "design_critic" and row["harness"] == "openai_codex"
    assert row["outcome"] == "ok"


async def test_critic_refuses_a_criterion_that_is_neither_ok_nor_fix(
    gymbro_repo, tmp_path
):
    """`great!` is not `ok` or `fix: <what>`: the critique is not accepted as
    is (a vague verdict would reach the owner), and Claude never answers
    instead."""
    from src.designloop.brief import build_brief
    from src.designloop.critic import critique

    calls: List[CallRequest] = []

    async def codex(request: CallRequest) -> CallResult:
        calls.append(request)
        answer = conforming(request.json_schema or {}, "x")
        answer["criteria"] = {k: "great!" for k in answer["criteria"]}
        return CallResult(text=json.dumps(answer))

    claude = _NeverClaude()
    set_router(
        ModelRouter(
            plain_callers={"openai_codex": codex, "claude": claude},
            versions=_Versions(),
        )
    )
    brief = build_brief(gymbro_repo, "feed", "Make the feed calmer.")

    with pytest.raises((ModelCallError, ValueError)):
        await _maybe(critique(brief, _renders(tmp_path)))

    assert calls and all(r.purpose == "design_critic" for r in calls)
    assert all(r.images for r in calls)
    assert not claude.calls
