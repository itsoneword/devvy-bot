"""Unit tests for the Tier-2 scoring rubric (dv-300d)."""

import json

import pytest

from src.llm_analytics.rubric import (
    GENERIC_CRITERIA,
    PROFILE_OVERLAY_CRITERIA,
    ContextTurn,
    build_judge_prompt,
    criteria_for,
    parse_judge_output,
)


class TestCriteriaFor:
    def test_generic_profile_has_no_overlay(self) -> None:
        assert criteria_for("devops") == GENERIC_CRITERIA
        assert criteria_for(None) == GENERIC_CRITERIA

    def test_chef_gets_overlay_appended(self) -> None:
        criteria = criteria_for("chef")
        assert criteria[: len(GENERIC_CRITERIA)] == GENERIC_CRITERIA
        assert criteria[len(GENERIC_CRITERIA) :] == PROFILE_OVERLAY_CRITERIA["chef"]


def _valid_payload(
    criteria: tuple[str, ...], scores: dict[str, int] | None = None
) -> str:
    scores = scores or {}
    return json.dumps(
        {
            "criteria": {
                name: {
                    "score": scores.get(name, 3),
                    "rationale": f"rationale for {name}",
                }
                for name in criteria
            },
            "summary": "overall summary",
            "flags": ["some_flag"],
        }
    )


class TestParseJudgeOutput:
    def test_parses_generic_profile(self) -> None:
        raw = _valid_payload(GENERIC_CRITERIA, {"grounding": 1, "calibration": 1})
        parsed = parse_judge_output(raw, "devops")
        assert set(parsed.scores) == set(GENERIC_CRITERIA)
        assert parsed.scores["grounding"]["score"] == 1
        # total = sum of the 5 generic scores: 1 + 1 + 3*3 = 11
        assert parsed.total == 11.0
        assert parsed.summary == "overall summary"
        assert parsed.flags == ["some_flag"]

    def test_parses_chef_overlay_but_total_excludes_it(self) -> None:
        criteria = criteria_for("chef")
        raw = _valid_payload(criteria, {"chef_anchor_named": 1})
        parsed = parse_judge_output(raw, "chef")
        assert "chef_anchor_named" in parsed.scores
        # total ignores the overlay entirely (3*5 generic = 15)
        assert parsed.total == 15.0

    def test_strips_markdown_fences(self) -> None:
        raw = "```json\n" + _valid_payload(GENERIC_CRITERIA) + "\n```"
        parsed = parse_judge_output(raw, None)
        assert parsed.total == 15.0

    def test_rationale_capped_at_300_chars(self) -> None:
        long_rationale = "x" * 500
        payload = json.loads(_valid_payload(GENERIC_CRITERIA))
        payload["criteria"]["grounding"]["rationale"] = long_rationale
        parsed = parse_judge_output(json.dumps(payload), None)
        assert len(parsed.scores["grounding"]["rationale"]) <= 300

    def test_summary_capped_at_300_chars(self) -> None:
        payload = json.loads(_valid_payload(GENERIC_CRITERIA))
        payload["summary"] = "y" * 500
        parsed = parse_judge_output(json.dumps(payload), None)
        assert len(parsed.summary) <= 300

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_judge_output("not json at all", None)

    def test_missing_criterion_raises(self) -> None:
        payload = json.loads(_valid_payload(GENERIC_CRITERIA))
        del payload["criteria"]["grounding"]
        with pytest.raises(ValueError, match="grounding"):
            parse_judge_output(json.dumps(payload), None)

    def test_score_out_of_range_raises(self) -> None:
        payload = json.loads(_valid_payload(GENERIC_CRITERIA))
        payload["criteria"]["grounding"]["score"] = 7
        with pytest.raises(ValueError, match="out of range"):
            parse_judge_output(json.dumps(payload), None)

    def test_missing_criteria_key_raises(self) -> None:
        with pytest.raises(ValueError, match="criteria"):
            parse_judge_output(json.dumps({"summary": "x"}), None)


class TestBuildJudgePrompt:
    def test_includes_all_criteria_and_tool_calls(self) -> None:
        prompt = build_judge_prompt(
            "chef",
            "how do I make shakshuka",
            "here's a recipe",
            [ContextTurn("user", "hi"), ContextTurn("assistant", "hello")],
            ["WebSearch"],
        )
        for name in criteria_for("chef"):
            assert name in prompt
        assert "WebSearch" in prompt
        assert "how do I make shakshuka" in prompt
        assert "here's a recipe" in prompt

    def test_no_tool_calls_renders_none(self) -> None:
        prompt = build_judge_prompt("devops", "q", "a", [], [])
        assert "(none)" in prompt
