"""Tests for the dv-c5d9 reconciler: verdict validation + deterministic
verdict->op mapping (the only producer of supersede/flag_conflict ops)."""

import pytest

from src.evaluator.reconciler import (
    FactReconciler,
    Verdict,
    map_verdicts,
    validate_reconciler_output,
)


def _cand(content: str, scope: str = "private", **kw):
    op = {
        "op": "add",
        "tier": "general",
        "content": content,
        "label": None,
        "supersedes_id": None,
        "scope": scope,
        "kind": "user_fact",
        "source_ref": None,
    }
    op.update(kw)
    return op


class TestValidateReconcilerOutput:
    def test_parses_verdicts_by_index(self):
        raw = (
            '[{"index": 0, "verdict": "duplicate", "existing_id": 7},'
            ' {"index": 1, "verdict": "new"}]'
        )
        verdicts = validate_reconciler_output(raw, 2)
        assert [v.verdict for v in verdicts] == ["duplicate", "new"]
        assert verdicts[0].existing_id == 7

    def test_missing_verdict_defaults_to_new(self):
        """Reconciler sloppiness must never drop a fact."""
        verdicts = validate_reconciler_output("[]", 2)
        assert [v.verdict for v in verdicts] == ["new", "new"]

    def test_unknown_verdict_degrades_to_unsure(self):
        raw = '[{"index": 0, "verdict": "better", "existing_id": 3}]'
        assert validate_reconciler_output(raw, 1)[0].verdict == "unsure"

    def test_contradicts_without_id_degrades_to_unsure(self):
        raw = '[{"index": 0, "verdict": "contradicts"}]'
        assert validate_reconciler_output(raw, 1)[0].verdict == "unsure"

    def test_bad_index_is_ignored(self):
        raw = '[{"index": 5, "verdict": "duplicate", "existing_id": 1}]'
        assert validate_reconciler_output(raw, 1)[0].verdict == "new"

    def test_fenced_json_and_safety_flag(self):
        raw = (
            "```json\n"
            '[{"index": 0, "verdict": "contradicts", "existing_id": 2, '
            '"safety": true}]\n```'
        )
        v = validate_reconciler_output(raw, 1)[0]
        assert v.verdict == "contradicts" and v.safety is True

    def test_non_array_raises(self):
        with pytest.raises(ValueError):
            validate_reconciler_output('{"nope": 1}', 1)

    def test_revokes_round_trips_incl_fenced(self):
        raw = (
            "```json\n"
            '[{"index": 0, "verdict": "contradicts", "existing_id": 2, '
            '"safety": true, "revokes": true}]\n```'
        )
        v = validate_reconciler_output(raw, 1)[0]
        assert v.verdict == "contradicts" and v.revokes is True

    def test_revokes_defaults_false(self):
        raw = '[{"index": 0, "verdict": "contradicts", "existing_id": 2}]'
        assert validate_reconciler_output(raw, 1)[0].revokes is False

    def test_revokes_without_id_degrades_to_unsure(self):
        """A revocation with nothing to supersede keeps the candidate."""
        raw = '[{"index": 0, "verdict": "contradicts", "revokes": true}]'
        v = validate_reconciler_output(raw, 1)[0]
        assert v.verdict == "unsure"


class TestMapVerdicts:
    SCOPES = {"private": {1, 2, 3}, "family": {10}}

    def test_new_and_unsure_become_adds(self):
        cands = [_cand("a"), _cand("b")]
        verdicts = [Verdict(0, "new"), Verdict(1, "unsure")]
        mapped = map_verdicts(cands, verdicts, self.SCOPES)
        assert [op["op"] for op in mapped.ops] == ["add", "add"]
        assert mapped.already_saved == {}
        # The hint never leaks into a mapped op — the verdict is authoritative.
        assert all(
            "supersedes_id" not in op or op["op"] == "supersede" for op in mapped.ops
        )

    def test_duplicate_is_dropped_and_counted(self):
        mapped = map_verdicts(
            [_cand("a")], [Verdict(0, "duplicate", existing_id=2)], self.SCOPES
        )
        assert mapped.ops == []
        assert mapped.already_saved == {0: 2}

    def test_contradiction_maps_to_supersede(self):
        mapped = map_verdicts(
            [_cand("a")], [Verdict(0, "contradicts", existing_id=3)], self.SCOPES
        )
        assert len(mapped.ops) == 1
        assert mapped.ops[0]["op"] == "supersede"
        assert mapped.ops[0]["supersedes_id"] == 3
        assert mapped.safety_flags == []

    def test_safety_contradiction_keeps_both_and_flags(self):
        mapped = map_verdicts(
            [_cand("a")],
            [Verdict(0, "contradicts", existing_id=2, safety=True)],
            self.SCOPES,
        )
        assert mapped.ops[0]["op"] == "add"  # never invalidated
        assert mapped.safety_flags == [(2, 0)]

    def test_out_of_scope_id_degrades_to_keep_both(self):
        """A family-scoped candidate cannot supersede a private fact."""
        mapped = map_verdicts(
            [_cand("a", scope="family")],
            [Verdict(0, "contradicts", existing_id=3)],  # 3 is private
            self.SCOPES,
        )
        assert mapped.ops[0]["op"] == "add"
        assert mapped.safety_flags == []

    def test_unknown_id_degrades_to_add(self):
        mapped = map_verdicts(
            [_cand("a")], [Verdict(0, "duplicate", existing_id=999)], self.SCOPES
        )
        assert mapped.ops[0]["op"] == "add"
        assert mapped.already_saved == {}


class TestRevocationSupersede:
    """dv-b9c7 step 2: a constraint and its own revocation can never coexist.

    Regression fixture from the real case in coach#u111111111: pinned fact
    (07.08.26 shoulder restrictions) stayed active alongside its own lift
    (11.08.26) because contradicts+safety mapped to keep-both.
    """

    SCOPES = {"private": {1, 2, 3}, "family": {10}}
    LIFT = "Плечо: ограничения сняты 11.08 — жимы разрешены снова"

    def test_revokes_supersedes_even_when_safety(self):
        mapped = map_verdicts(
            [_cand(self.LIFT, tier="pinned")],
            [Verdict(0, "contradicts", existing_id=2, safety=True, revokes=True)],
            self.SCOPES,
        )
        assert len(mapped.ops) == 1
        assert mapped.ops[0]["op"] == "supersede"
        assert mapped.ops[0]["supersedes_id"] == 2
        assert mapped.safety_flags == []

    def test_lexicon_backstop_fires_without_revokes(self):
        """Model called contradicts+safety but omitted revokes — the
        release wording ("сняты") still forces the supersede."""
        mapped = map_verdicts(
            [_cand(self.LIFT, tier="pinned")],
            [Verdict(0, "contradicts", existing_id=2, safety=True)],
            self.SCOPES,
        )
        assert mapped.ops[0]["op"] == "supersede"
        assert mapped.ops[0]["supersedes_id"] == 2
        assert mapped.safety_flags == []

    def test_lexicon_backstop_english(self):
        mapped = map_verdicts(
            [_cand("Shoulder restrictions LIFTED, pressing allowed")],
            [Verdict(0, "contradicts", existing_id=2, safety=True)],
            self.SCOPES,
        )
        assert mapped.ops[0]["op"] == "supersede"

    def test_backstop_never_fires_on_new_or_unsure(self):
        """No existing_id to supersede — lexical guessing is barred."""
        mapped = map_verdicts([_cand(self.LIFT)], [Verdict(0, "new")], self.SCOPES)
        assert mapped.ops[0]["op"] == "add"
        mapped = map_verdicts([_cand(self.LIFT)], [Verdict(0, "unsure")], self.SCOPES)
        assert mapped.ops[0]["op"] == "add"

    def test_genuine_safety_contradiction_still_keeps_both(self):
        """Keep-both survives for non-revocation safety contradictions."""
        mapped = map_verdicts(
            [_cand("allergic to hazelnuts")],
            [Verdict(0, "contradicts", existing_id=2, safety=True)],
            self.SCOPES,
        )
        assert mapped.ops[0]["op"] == "add"
        assert mapped.safety_flags == [(2, 0)]


class TestFactReconciler:
    async def test_reconcile_round_trip(self):
        seen = {}

        async def fake_invoke(prompt: str) -> str:
            seen["prompt"] = prompt
            return '[{"index": 0, "verdict": "duplicate", "existing_id": 4}]'

        rec = FactReconciler(invoke=fake_invoke)
        verdicts = await rec.reconcile(
            [_cand("likes tea")], "4 | user_fact | general | 01.08.26 | likes tea"
        )
        assert verdicts[0].verdict == "duplicate"
        assert "likes tea" in seen["prompt"]
        assert "scope difference is not contradiction" in seen["prompt"].lower()

    async def test_transport_failure_propagates(self):
        async def broken(prompt: str) -> str:
            raise RuntimeError("timeout after 120s (claude cli)")

        rec = FactReconciler(invoke=broken)
        with pytest.raises(RuntimeError):
            await rec.reconcile([_cand("x")], "(no existing facts)")
