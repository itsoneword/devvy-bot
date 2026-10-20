"""dv-5206 phase 3: the fast "core" subset run before every restart.

`make test-core` selects tests by a registered `core` pytest marker. These
tests pin what the subset must contain (window, evaluator, items, policy
parity, export drift), that it stays within the ~1 min budget, and that the
restart checklist in CLAUDE.md names it next to the backend parity gate.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# The private checkout has the export tooling; the public tree does not ship
# it (nor the private CLAUDE.md), so the checks about them cannot run there.
PRIVATE_ONLY = pytest.mark.skipif(
    not (REPO / "scripts" / "export_public.py").exists(),
    reason="export tooling and private CLAUDE.md are not shipped",
)

# Set in the child pytest of the timing test, so a core run never recurses
# into another core run even if this file were ever marked core by mistake.
_GUARD = "DV5206_CORE_TIMING_CHILD"

# Wall-clock budget for the core subset. Scenario: "about 60 s or less";
# a little slack for a loaded box, still far below the untrimmed ~72 s+.
CORE_BUDGET_S = 70.0


def _pytest(*args: str, timeout: float = 300) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env[_GUARD] = "1"
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--no-cov", *args],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def core_ids() -> list[str]:
    """Node ids selected by `-m core`, collected in a clean subprocess."""
    proc = _pytest("--collect-only", "-qq", "-m", "core")
    return [
        line.strip()
        for line in proc.stdout.splitlines()
        if "::" in line and line.strip().startswith("tests/")
    ]


def _makefile_recipe(target: str) -> str:
    text = (REPO / "Makefile").read_text()
    m = re.search(
        rf"^{re.escape(target)}:[^\n]*\n((?:\t[^\n]*\n?)*)", text, re.MULTILINE
    )
    assert m, f"Makefile has no `{target}:` target"
    return m.group(1)


# --- scenario 1: make test-core runs window, evaluator, items, parity, export drift


def test_makefile_test_core_target_selects_core_marker() -> None:
    recipe = _makefile_recipe("test-core")
    assert "pytest" in recipe
    assert re.search(r"-m\s+['\"]?core\b", recipe), recipe
    # addopts forces --cov; the budget only holds without it.
    assert "--no-cov" in recipe, recipe


@pytest.mark.parametrize(
    "area, required",
    [
        ("window", "tests/unit/test_core_window_cycle.py::"),
        ("window", "tests/unit/test_orchestrator_window_accounting.py::"),
        ("window", "tests/unit/test_window_repository.py::"),
        ("window", "tests/unit/test_over_threshold_guard.py::"),
        ("evaluator", "tests/unit/test_evaluator/"),
        ("items", "tests/unit/test_items/"),
        ("policy parity", "tests/unit/test_security/test_policy_parity.py::"),
        pytest.param(
            "export drift",
            "tests/unit/test_scripts/test_export_public.py::"
            "test_every_tracked_file_is_classified",
            marks=PRIVATE_ONLY,
        ),
        pytest.param(
            "export drift",
            "tests/unit/test_scripts/test_export_public.py::"
            "test_no_shipped_file_carries_personal_data",
            marks=PRIVATE_ONLY,
        ),
    ],
)
def test_core_marker_covers_required_area(
    core_ids: list[str], area: str, required: str
) -> None:
    hits = [i for i in core_ids if i.startswith(required)]
    assert hits, f"core subset has no {area} tests matching {required!r}"


# --- scenario 2: make test-core finishes in about 60 s or less


def test_core_subset_passes_within_budget() -> None:
    if os.environ.get(_GUARD):
        pytest.skip("inside a core-subset child run")
    start = time.monotonic()
    proc = _pytest("-m", "core", "-q", timeout=600)
    elapsed = time.monotonic() - start
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-25:])
    # rc 5 = nothing selected: the marker is not applied anywhere yet.
    assert proc.returncode == 0, f"core subset failed (rc={proc.returncode}):\n{tail}"
    assert elapsed <= CORE_BUDGET_S, f"core subset took {elapsed:.1f}s:\n{tail}"


# --- scenario 3: CLAUDE.md restart checklist lists make test-core near parity gate


@PRIVATE_ONLY
def test_claude_md_restart_checklist_lists_test_core_next_to_parity_gate() -> None:
    text = (REPO / "CLAUDE.md").read_text()
    m = re.search(r"^### Deployment\n(.*?)(?=^### )", text, re.MULTILINE | re.DOTALL)
    assert m, "CLAUDE.md has no Deployment section"
    deployment = m.group(1)
    assert "make test-core" in deployment, "Deployment block does not name it"
    lines = deployment.splitlines()
    gate = next(i for i, ln in enumerate(lines) if "parity gate" in ln.lower())
    core = next(i for i, ln in enumerate(lines) if "make test-core" in ln)
    assert abs(core - gate) <= 10, (
        f"`make test-core` (line {core}) is not next to the parity gate "
        f"(line {gate}) in the Deployment block"
    )


# --- added guards


def test_core_marker_is_registered() -> None:
    proc = _pytest("--markers")
    assert re.search(r"^@pytest\.mark\.core\b", proc.stdout, re.MULTILINE), (
        "`core` marker not registered in [tool.pytest.ini_options] markers"
    )


def test_core_subset_is_a_strict_subset(core_ids: list[str]) -> None:
    """Marker must not leak onto everything, nor onto this (recursive) file."""
    assert core_ids, "nothing is marked core"
    assert not any(i.startswith("tests/unit/test_core_subset.py::") for i in core_ids)
    # Env-sensitive config tests (see dv-5206 phase 1 note) stay out of core.
    assert not any(i.startswith("tests/unit/test_config.py::") for i in core_ids)
