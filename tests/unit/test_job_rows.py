"""model_router.job_rows: the one writer of models.yaml jobs: rows (dv-6104)."""

from pathlib import Path

import pytest
import yaml

from src.model_router import job_rows

FILE = """\
# Owner notes: keep this header.
jobs:
  pump_review: {models: {claude: opus}}
"""


def test_set_keeps_the_header_and_undo_restores(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(FILE, encoding="utf-8")
    change = job_rows.set_job_model(path, "pump_review", "sonnet")
    assert change is not None and change.old == "opus" and not change.unset
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Owner notes: keep this header.\n")
    job_rows.undo_job_model(path, change)
    assert yaml.safe_load(path.read_text()) == yaml.safe_load(FILE)


def test_undo_of_a_created_file_removes_it(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    change = job_rows.set_job_model(path, "pump_architect", "opus")
    assert change is not None and change.created and change.unset
    assert yaml.safe_load(path.read_text()) == {
        "jobs": {"pump_architect": {"models": {"claude": "opus"}}}
    }
    job_rows.undo_job_model(path, change)
    assert not path.exists()


def test_undo_refused_after_a_hand_edit(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(FILE, encoding="utf-8")
    change = job_rows.set_job_model(path, "pump_review", "sonnet")
    assert change is not None
    path.write_text(FILE.replace("opus", "fable"), encoding="utf-8")
    raw = path.read_bytes()
    with pytest.raises(job_rows.RowError):
        job_rows.undo_job_model(path, change)
    assert path.read_bytes() == raw


def test_unparsable_file_is_by_hand_and_untouched(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text("jobs: [not, a, mapping]\n", encoding="utf-8")
    raw = path.read_bytes()
    with pytest.raises(job_rows.RowError) as err:
        job_rows.set_job_model(path, "pump_review", "sonnet")
    assert err.value.by_hand
    assert path.read_bytes() == raw


def test_unknown_job_is_refused(tmp_path: Path) -> None:
    with pytest.raises(job_rows.RowError):
        job_rows.set_job_model(tmp_path / "models.yaml", "pump_nope", "sonnet")
    assert not (tmp_path / "models.yaml").exists()


def test_choices_per_harness(tmp_path: Path) -> None:
    routes = job_rows.load(tmp_path / "absent.yaml")
    claude = job_rows.model_choices("claude", routes)
    assert {"sonnet", "opus"} <= set(claude)
    codex = job_rows.model_choices("openai_codex", routes)
    assert "sonnet" not in codex and any("codex" in m for m in codex)
    assert job_rows.model_choices("openrouter", routes) == ()


def test_routes_path_follows_setting_then_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MODEL_ROUTES_PATH", str(tmp_path / "env.yaml"))
    assert job_rows.routes_path(tmp_path / "set.yaml") == tmp_path / "set.yaml"
    assert job_rows.routes_path(None) == tmp_path / "env.yaml"
