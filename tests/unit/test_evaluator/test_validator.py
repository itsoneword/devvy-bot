"""Tests for evaluator output validator (memory op schema)."""

import json

import pytest

from src.evaluator.validator import validate_evaluator_output


class TestValidateEvaluatorOutput:
    """Validate evaluator JSON into add/supersede memory ops."""

    def test_core_tier_accepted(self):
        ops = validate_evaluator_output(
            '[{"op":"add","tier":"core","content":"Age 34, strength not mass"}]'
        )
        assert ops[0]["tier"] == "core"

    def test_validates_add_and_supersede_ops(self):
        raw = (
            '[{"op":"add","tier":"pinned","content":"allergic to peanuts",'
            '"label":"allergies"},'
            '{"op":"supersede","tier":"general","content":"has an oven",'
            '"supersedes_id":7}]'
        )
        ops = validate_evaluator_output(raw)
        assert ops[0] == {
            "op": "add",
            "tier": "pinned",
            "content": "allergic to peanuts",
            "label": "allergies",
            "supersedes_id": None,
            "scope": "private",
            "kind": "user_fact",
            "source_ref": None,
        }
        assert ops[1]["op"] == "supersede"
        assert ops[1]["supersedes_id"] == 7

    def test_empty_array_returns_empty_list(self):
        assert validate_evaluator_output("[]") == []

    def test_unwraps_extractions_object(self):
        raw = '{"extractions":[{"op":"add","tier":"general","content":"likes tea"}]}'
        ops = validate_evaluator_output(raw)
        assert len(ops) == 1
        assert ops[0]["content"] == "likes tea"

    def test_add_defaults_label_none_and_supersedes_none(self):
        ops = validate_evaluator_output('[{"op":"add","tier":"general","content":"x"}]')
        assert ops[0]["label"] is None
        assert ops[0]["supersedes_id"] is None

    def test_bad_op_entry_is_skipped_not_raised(self):
        ops = validate_evaluator_output(
            '[{"op":"delete","tier":"general","content":"x"}]'
        )
        assert ops == []

    def test_bad_tier_entry_is_skipped_not_raised(self):
        ops = validate_evaluator_output('[{"op":"add","tier":"junk","content":"x"}]')
        assert ops == []

    def test_missing_content_entry_is_skipped_not_raised(self):
        ops = validate_evaluator_output('[{"op":"add","tier":"general"}]')
        assert ops == []

    def test_empty_content_entry_is_skipped_not_raised(self):
        ops = validate_evaluator_output(
            '[{"op":"add","tier":"general","content":"  "}]'
        )
        assert ops == []

    def test_oversized_content_entry_is_skipped_not_raised(self):
        big = "x" * 3000
        ops = validate_evaluator_output(
            f'[{{"op":"add","tier":"general","content":"{big}"}}]'
        )
        assert ops == []

    def test_supersede_missing_id_downgrades_to_add(self):
        """A supersede the LLM forgot to attach an id to must not be
        dropped -- it still carries a real fact worth keeping."""
        ops = validate_evaluator_output(
            '[{"op":"supersede","tier":"general","content":"has an oven"}]'
        )
        assert len(ops) == 1
        assert ops[0]["op"] == "add"
        assert ops[0]["content"] == "has an oven"
        assert ops[0]["supersedes_id"] is None

    def test_supersede_non_integer_id_downgrades_to_add(self):
        ops = validate_evaluator_output(
            '[{"op":"supersede","tier":"general","content":"x",' '"supersedes_id":"7"}]'
        )
        assert len(ops) == 1
        assert ops[0]["op"] == "add"

    def test_one_malformed_entry_does_not_discard_the_rest_of_the_batch(self):
        """The bug this guards: a single bad op used to raise and drop the
        ENTIRE response, including otherwise-valid facts -- costly right at
        a 150k refresh where the window rolls immediately after and there's
        no retry."""
        raw = json.dumps(
            [
                {"op": "add", "tier": "general", "content": "good fact one"},
                {"op": "supersede", "tier": "general", "content": "bad, no id"},
                {"op": "delete", "tier": "general", "content": "invalid op"},
                {"op": "add", "tier": "general", "content": "good fact two"},
            ]
        )
        ops = validate_evaluator_output(raw)
        contents = [o["content"] for o in ops]
        assert "good fact one" in contents
        assert "good fact two" in contents
        assert "bad, no id" in contents  # downgraded to add, not dropped
        assert "invalid op" not in contents  # unrecoverable, skipped
        assert len(ops) == 3

    def test_rejects_non_string_label(self):
        ops = validate_evaluator_output(
            '[{"op":"add","tier":"general","content":"x","label":5}]'
        )
        assert ops == []

    def test_kind_passthrough_and_default(self):
        ops = validate_evaluator_output(
            '[{"op":"add","tier":"general","content":"research summary",'
            '"kind":"conversation_result","source_ref":"notes/research.md"},'
            '{"op":"add","tier":"general","content":"likes tea"}]'
        )
        assert ops[0]["kind"] == "conversation_result"
        assert ops[0]["source_ref"] == "notes/research.md"
        # Missing kind must not lose the fact — defaults to user_fact.
        assert ops[1]["kind"] == "user_fact"
        assert ops[1]["source_ref"] is None

    def test_invalid_kind_defaults_to_user_fact(self):
        ops = validate_evaluator_output(
            '[{"op":"add","tier":"general","content":"x","kind":"junk"}]'
        )
        assert ops[0]["kind"] == "user_fact"

    def test_invalidate_op_valid(self):
        ops = validate_evaluator_output('[{"op":"invalidate","target_id":42}]')
        assert ops == [{"op": "invalidate", "target_id": 42, "scope": "private"}]

    def test_invalidate_strips_content(self):
        """Content-less ops accept no text — the structural guarantee that
        no op rewrites an existing row lives in the validator."""
        ops = validate_evaluator_output(
            '[{"op":"invalidate","target_id":42,"content":"sneaky rewrite"}]'
        )
        assert ops == [{"op": "invalidate", "target_id": 42, "scope": "private"}]

    def test_invalidate_without_int_id_is_skipped(self):
        """Nothing to save by downgrading: no content means no add target."""
        ops = validate_evaluator_output(
            '[{"op":"invalidate","target_id":"42x"},{"op":"invalidate"}]'
        )
        assert ops == []

    def test_flag_conflict_valid(self):
        ops = validate_evaluator_output('[{"op":"flag_conflict","ids":[3,9]}]')
        assert ops == [{"op": "flag_conflict", "ids": [3, 9], "scope": "private"}]

    def test_flag_conflict_bad_ids_skipped(self):
        for ids in ("[3]", "[3,9,12]", '["a","b"]', "null"):
            ops = validate_evaluator_output(f'[{{"op":"flag_conflict","ids":{ids}}}]')
            assert ops == [], f"ids={ids} should be skipped"

    def test_malformed_json_raises(self):
        with pytest.raises((ValueError, json.JSONDecodeError)):
            validate_evaluator_output("not json at all")

    def test_json_not_array_raises(self):
        with pytest.raises(ValueError, match="array"):
            validate_evaluator_output('{"op":"add"}')

    def test_markdown_fences_stripped(self):
        raw = '```json\n[{"op":"add","tier":"general","content":"hi"}]\n```'
        assert len(validate_evaluator_output(raw)) == 1

    def test_leading_trailing_whitespace(self):
        raw = '  \n  [{"op":"add","tier":"general","content":"hi"}]  \n  '
        assert len(validate_evaluator_output(raw)) == 1


def test_screen_fields_pass_through_only_when_set():
    from src.evaluator.validator import validate_evaluator_output

    raw = (
        '[{"op":"add","tier":"general","content":"a","revokes":true,'
        '"conflicts_with_id":4},'
        '{"op":"add","tier":"general","content":"b","revokes":null,'
        '"conflicts_with_id":null}]'
    )
    first, second = validate_evaluator_output(raw)
    assert first["revokes"] is True and first["conflicts_with_id"] == 4
    assert "revokes" not in second and "conflicts_with_id" not in second
