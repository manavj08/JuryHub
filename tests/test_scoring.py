"""The normalization math, on synthetic data where the truth is known."""

import random

import pytest

from scoreline.scoring import normalize, rank_projects, weighted_score

CRIT = [
    {"key": "functionality", "weight": 0.4, "min_score": 1, "max_score": 5},
    {"key": "quality", "weight": 0.35, "min_score": 1, "max_score": 5},
    {"key": "innovation", "weight": 0.25, "min_score": 1, "max_score": 5},
]


def test_weighted_score_is_weighted_mean_on_1_to_5():
    assert weighted_score({"functionality": 5, "quality": 5, "innovation": 5}, CRIT) == pytest.approx(5)
    assert weighted_score({"functionality": 1, "quality": 1, "innovation": 1}, CRIT) == pytest.approx(1)
    assert weighted_score({"functionality": 5, "quality": 1, "innovation": 1}, CRIT) == pytest.approx(1 + 4 * 0.4)


def test_weighted_score_rescales_other_ranges():
    crit = [{"key": "a", "weight": 1, "min_score": 0, "max_score": 10}]
    assert weighted_score({"a": 10}, crit) == pytest.approx(5)
    assert weighted_score({"a": 0}, crit) == pytest.approx(1)


def test_partial_review_has_no_score():
    assert weighted_score({"functionality": 4}, CRIT) is None


def synthetic(seed=7, projects=40, judges=12, per=3, noise=0.15):
    rng = random.Random(seed)
    quality = {f"p{i}": rng.uniform(-1, 1) for i in range(projects)}
    bias = {f"j{i}": rng.uniform(-0.8, 0.8) for i in range(judges)}
    js = list(bias)
    reviews = []
    # Random panels. (Fixed round-robin panels make every project in a group
    # share one set of judges; bias then cancels by construction and cannot
    # be measured. See "Connectivity" in JUDGING.md.)
    for p in quality:
        for j in rng.sample(js, per):
            reviews.append((j, p, 3 + quality[p] + bias[j] + rng.gauss(0, noise)))
    return quality, bias, reviews


def test_recovers_known_judge_bias():
    quality, bias, reviews = synthetic()
    _, q, b, _ = normalize(reviews, shrinkage=0.5)
    mean_true = sum(bias.values()) / len(bias)
    for j, true in bias.items():
        assert b[j] == pytest.approx(true - mean_true, abs=0.2)


def test_normalized_ranking_beats_raw_ranking_on_biased_data():
    quality, bias, reviews = synthetic(seed=3, noise=0.05)
    table = rank_projects(reviews, shrinkage=0.5)
    truth = sorted(quality, key=lambda p: -quality[p])
    def displacement(order):
        return sum(abs(order.index(p) - truth.index(p)) for p in truth)
    adjusted = [r.project_id for r in table["projects"]]
    raw = [r.project_id for r in sorted(table["projects"], key=lambda r: r.raw_rank)]
    assert displacement(adjusted) < displacement(raw)


def test_flat_judge_does_not_break_anything():
    # Iva Petrova's pattern: the same score for every project.
    reviews = [("flat", "a", 4.0), ("flat", "b", 4.0), ("flat", "c", 4.0),
               ("x", "a", 2.0), ("x", "b", 3.0), ("y", "c", 5.0), ("y", "b", 3.5)]
    table = rank_projects(reviews)
    flat = next(j for j in table["judges"] if j.judge_id == "flat")
    assert flat.spread == 0
    assert "identical scores on every review" in flat.flags
    assert all(r.normalized == r.normalized for r in table["projects"])  # no NaN


def test_shrinkage_pulls_single_review_judges_toward_zero():
    reviews = [("solo", "a", 1.0), ("x", "a", 3.0), ("x", "b", 3.0), ("y", "a", 3.0), ("y", "b", 3.0)]
    _, _, loose, _ = normalize(reviews, shrinkage=0.0)
    _, _, tight, _ = normalize(reviews, shrinkage=5.0)
    assert abs(tight["solo"]) < abs(loose["solo"])


def test_bias_is_centred():
    _, _, reviews = synthetic()
    _, _, b, _ = normalize(reviews)
    assert sum(b.values()) == pytest.approx(0, abs=1e-9)


def test_ranking_is_deterministic_and_ties_break_stably():
    reviews = [("j", "b", 3.0), ("j", "a", 3.0), ("k", "a", 3.0), ("k", "b", 3.0)]
    first = [r.project_id for r in rank_projects(reviews)["projects"]]
    for _ in range(5):
        assert [r.project_id for r in rank_projects(list(reversed(reviews)))["projects"]] == first
    assert first == ["a", "b"]  # equal on everything else, so id decides


def test_empty_input():
    table = rank_projects([])
    assert table["projects"] == [] and table["judges"] == []


def test_components_detects_disconnected_panels():
    from scoreline.scoring import components
    reviews = [("a", "p1", 3), ("a", "p2", 4), ("b", "p3", 2), ("c", "p3", 5)]
    groups = components(reviews)
    assert groups == [{"p1", "p2"}, {"p3"}]
    assert len(components(reviews + [("b", "p1", 3)])) == 1


def test_fixture_judging_graph_is_connected():
    import json
    from pathlib import Path
    from scoreline.scoring import components
    data = json.loads((Path(__file__).parent.parent / "data" / "fixtures.json").read_text())
    reviews = [(s["judge"], s["project"], 0.0) for s in data["scores"] if s["project"] != "prj_41"]
    assert len(components(reviews)) == 1
