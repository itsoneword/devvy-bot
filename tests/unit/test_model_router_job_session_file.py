"""dv-d2a3: run_job writes the turn's session id into the job dir, so LLM
Analytics links a bgjob to its transcript exactly."""

import io
from pathlib import Path

import pytest
from claude_agent_sdk import ResultMessage, SystemMessage

from src.model_router import job as job_mod
from src.model_router.job import BotLink


async def test_session_file_written_from_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = [
        SystemMessage(subtype="init", data={"session_id": "sess-init"}),
        ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sess-init",
            total_cost_usd=0.0,
            usage={},
            result="done",
        ),
    ]

    class FakeClient:
        def __init__(self, *_: object) -> None:
            pass

        async def connect(self) -> None: ...
        async def query(self, _: str) -> None: ...
        async def disconnect(self) -> None: ...

        async def receive_response(self):  # type: ignore[no-untyped-def]
            for m in stream:
                yield m

    monkeypatch.setattr(job_mod, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(job_mod, "job_options", lambda *_: None)
    monkeypatch.setattr(job_mod, "orphans", lambda *a, **k: [], raising=False)
    link = BotLink("http://127.0.0.1:1", "s")
    link.post = lambda path, body, timeout: {}  # type: ignore[method-assign]
    target = tmp_path / "session_id"
    spec = job_mod.JobSpec(
        purpose="background_job",
        name="pump-dv-0ecd-review-p2",
        prompt="p",
        model="sonnet",
        working_directory=tmp_path,
        full_access=False,
        unrestricted=False,
        session_file=target,
    )
    assert await job_mod.run_job(spec, link, out=io.StringIO()) == 0
    assert target.read_text() == "sess-init\n"


def test_no_session_file_by_default() -> None:
    spec = job_mod.JobSpec(
        purpose="background_job",
        name="j",
        prompt="p",
        model="sonnet",
        working_directory=Path("/tmp"),
        full_access=False,
        unrestricted=False,
    )
    assert spec.session_file is None
