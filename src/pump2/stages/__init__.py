"""Model stages. A module exposes ``prompt(board, card, wt) -> str`` and
``finish(board, card, wt) -> Outcome``; the runner does everything else."""

from __future__ import annotations

from typing import Any

from ..state import Board
from . import analyze, build, mergefix, review, tests
from .custom import CustomStage

MODULES: dict[str, Any] = {
    "analyze": analyze,
    "tests": tests,
    "build": build,
    "review": review,
    "mergefix": mergefix,
}


def module_for(board: Board, stage: str) -> Any:
    """The built-in module of ``stage``, else the board's custom stage of that
    name (dv-3b99), else None (merge, done, ... : not a model stage)."""
    if stage in MODULES:
        return MODULES[stage]
    spec = board.custom(stage)
    return CustomStage(spec) if spec else None
