"""Corpus-driven tests for the deterministic episodic guard (dv-7517 step 3).

Ground truth is the 2026-08-14 manual cleanup of coach#u222222222:
- positive set = the 64 rows closed as 'cleanup-episodic-2026-08-14'
  (labeled episodic junk, verbatim);
- negative set = the active user_facts that cleanup deliberately preserved
  (recorded before it ran), including all [product-ref] rows and the pinned
  constraint phrasings.

The hard requirement is ZERO false positives on the negative set — a durable
fact silently dropped is the failure mode to avoid. Recall on the positive
set is documented, not forced to 100%: rows protected by durable vetoes
(medical context, package-label data, stated patterns) are accepted misses;
the prompt rule (step 2) remains the primary defense.
"""

import json
from pathlib import Path

import pytest

from src.evaluator.episodic_guard import classify

_CORPUS = json.loads(
    (Path(__file__).parent / "fixtures" / "episodic_corpus_20260814.json").read_text()
)

# Documented recall floor on the labeled positive set (55/64 = 86% at tuning
# time). A word-list edit that drops below this lost real coverage.
RECALL_FLOOR = 0.85


def test_zero_false_positives_on_preserved_durable_facts():
    misfires = [
        (row["id"], classify(row["content"], tier=row["tier"]))
        for row in _CORPUS["negative"]
        if classify(row["content"], tier=row["tier"]) is not None
    ]
    assert misfires == []


def test_recall_on_labeled_episodic_rows():
    caught = sum(
        1
        for row in _CORPUS["positive"]
        if classify(row["content"], tier=row["tier"]) is not None
    )
    recall = caught / len(_CORPUS["positive"])
    assert recall >= RECALL_FLOOR, f"recall {recall:.0%} below {RECALL_FLOOR:.0%}"


def test_product_ref_rows_never_rejected():
    product_rows = [
        r for r in _CORPUS["negative"] if "Product reference" in r["content"]
    ]
    assert product_rows, "corpus must contain the product-ref rows"
    assert all(classify(r["content"], tier=r["tier"]) is None for r in product_rows)


def test_pinned_and_core_tiers_never_rejected():
    # An allergy/constraint phrased with a date and kcal must not be lost.
    content = "12.08 dinner reaction: allergic swelling after peanuts, ~200 kcal meal"
    assert classify(content, tier="pinned") is None
    assert classify(content, tier="core") is None


@pytest.mark.parametrize(
    "content,expected",
    [
        ("08.08 breakfast: 2 eggs + bread 60g — ~542 kcal / Б46.3", "single meal log"),
        (
            "Day 10.08.26 nutrition tracking closed: total ~1703 kcal",
            "day nutrition summary",
        ),
        (
            "Apple Watch 07.08: Move 858/600 kcal, Exercise 176/40min",
            "daily activity readout",
        ),
        ("11.08 evening: beer 0.5L — ~215 kcal", "dated log line"),
        ("Slept 5 hours last night, feels wrecked", "single night's sleep"),
        (
            "Ночь 11->12.08.2026: отбой 22:30, уснул быстро, подъём 7:10",
            "single night's sleep",
        ),
        (
            "12.08.2026 morning fasted weight: 84.15kg (day 2 of tracking)",
            "single weigh-in",
        ),
        (
            "Night 11->12.08.2026 bedroom temperature was 19.9C, felt cold",
            "single night's conditions",
        ),
        (
            "11.08.2026 около 18-19:00 пользователь выкурил джойнт (ТГК)",
            "dated one-off consumption",
        ),
        ("Leg press session: did 4x12 at 90kg", "single session actuals"),
        ("Вчера 12.08 съела 3 Raffaello вечером", "dated one-off consumption"),
    ],
)
def test_episodic_shapes_rejected(content, expected):
    assert classify(content) == expected


@pytest.mark.parametrize(
    "content",
    [
        "usually skips breakfast on training days",
        "Goal: lose body fat, target 1600-1700 kcal/day",
        "allergic to walnuts (confirmed 12.08 after dinner reaction)",
        "Product reference: Corona Extra beer — 148 kcal/355ml",
        "Weigh-in 14.08 (Friday): 66.1kg. Trend: 30.07=66.3 → 14.08=66.1",
        # Protocol-defining weigh-in: comparability conditions, not a reading.
        "2026-07-30 weigh-in 66.3kg; for comparability future weigh-ins "
        "should use the same morning conditions",
        "Right shoulder subluxates — no overhead pressing, injury constraint",
    ],
)
def test_durable_shapes_kept(content):
    assert classify(content) is None
