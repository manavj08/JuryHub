"""Weighted rubric scoring and cross-judge normalization.

Pure functions over plain data so they can be unit-tested without a database.
The method is written up in JUDGING.md; the short version:

    s[j,p] = mu + quality[p] + bias[j] + noise

`s` is judge j's weighted rubric score for project p. `bias[j]` is how much
more generous than average judge j is. It is estimated with a ridge penalty
(`shrinkage`, in units of pseudo-reviews), so a judge with one review is
assumed to be roughly average rather than being blamed or credited for the one
project they happened to see. A judge who gives every project the same score
has zero spread, which breaks z-scores (division by zero) but is harmless here.

The normalized score reported for a project is mu + quality[p]: the score it
would have got from an average judge, on the original rubric scale.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field


def weighted_score(values: dict[str, float], criteria: list[dict]) -> float | None:
    """Weighted mean of criterion scores, rescaled to 1..5.

    `criteria` rows need key, weight, min_score, max_score. Returns None if
    any criterion is missing, because a partial review is not a review.
    """
    total_w = 0.0
    acc = 0.0
    for c in criteria:
        if c["key"] not in values or values[c["key"]] is None:
            return None
        lo, hi = c["min_score"], c["max_score"]
        unit = (float(values[c["key"]]) - lo) / (hi - lo)       # 0..1
        acc += c["weight"] * (1 + 4 * unit)                      # 1..5
        total_w += c["weight"]
    return acc / total_w if total_w else None


@dataclass
class JudgeStat:
    judge_id: str
    n: int
    mean_given: float
    spread: float           # population std of the scores this judge gave
    bias: float             # estimated generosity, in rubric points
    flags: list[str] = field(default_factory=list)


@dataclass
class ProjectResult:
    project_id: str
    n: int
    raw_mean: float
    normalized: float
    stderr: float
    raw_rank: int = 0
    rank: int = 0

    @property
    def movement(self) -> int:
        return self.raw_rank - self.rank      # positive = moved up after normalization


def normalize(reviews: list[tuple[str, str, float]], shrinkage: float = 2.0,
              tol: float = 1e-10, max_iter: int = 2000):
    """Fit the additive bias model.

    reviews: (judge_id, project_id, weighted_score) triples.
    Returns (mu, quality: dict, bias: dict, residual_sd).

    Block coordinate descent on a strictly convex objective (shrinkage > 0),
    so it converges to the unique minimum. With shrinkage == 0 the model is
    only identified up to a constant per connected component; we still
    converge, and mu absorbs the level.
    """
    if not reviews:
        return 0.0, {}, {}, 0.0
    mu = sum(s for _, _, s in reviews) / len(reviews)
    by_p: dict[str, list[tuple[str, float]]] = defaultdict(list)
    by_j: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for j, p, s in reviews:
        by_p[p].append((j, s))
        by_j[j].append((p, s))
    q = {p: 0.0 for p in by_p}
    b = {j: 0.0 for j in by_j}
    for _ in range(max_iter):
        delta = 0.0
        for p, rows in by_p.items():
            new = sum(s - mu - b[j] for j, s in rows) / len(rows)
            delta = max(delta, abs(new - q[p]))
            q[p] = new
        for j, rows in by_j.items():
            new = sum(s - mu - q[p] for p, s in rows) / (len(rows) + shrinkage)
            delta = max(delta, abs(new - b[j]))
            b[j] = new
        if delta < tol:
            break
    # Keep bias centred on zero so "bias" reads as relative generosity.
    shift = sum(b.values()) / len(b)
    b = {j: v - shift for j, v in b.items()}
    q = {p: v + shift for p, v in q.items()}
    resid = [s - mu - q[p] - b[j] for j, p, s in reviews]
    dof = max(1, len(reviews) - len(q) - len(b) + 1)
    sd = math.sqrt(sum(r * r for r in resid) / dof)
    return mu, q, b, sd


def components(reviews: list[tuple[str, str, float]]) -> list[set[str]]:
    """Connected groups of projects, linked through shared judges.

    Bias is only identifiable within a group: if two groups share no judge,
    nothing in the data says which group's judges were more generous, so
    adjusted scores are comparable inside a group but not across groups.
    """
    adj: dict[str, set[str]] = defaultdict(set)
    for j, p, _ in reviews:
        adj["j:" + j].add("p:" + p)
        adj["p:" + p].add("j:" + j)
    seen: set[str] = set()
    groups = []
    for start in adj:
        if start in seen:
            continue
        stack, group = [start], set()
        while stack:
            node = stack.pop()
            if node in group:
                continue
            group.add(node)
            stack.extend(adj[node] - group)
        seen |= group
        projects = {n[2:] for n in group if n.startswith("p:")}
        if projects:
            groups.append(projects)
    return sorted(groups, key=lambda g: -len(g))


def rank_projects(reviews: list[tuple[str, str, float]], shrinkage: float = 2.0,
                  submitted_at: dict[str, str] | None = None):
    """Full results table: raw means, normalized scores, ranks and movement.

    Tie-breaks, in order: normalized score, number of reviews, raw mean,
    earlier submission, project id. Deterministic, so re-running on the same
    data always produces the same order.
    """
    submitted_at = submitted_at or {}
    mu, q, b, sd = normalize(reviews, shrinkage)
    by_p: dict[str, list[float]] = defaultdict(list)
    by_j: dict[str, list[float]] = defaultdict(list)
    for j, p, s in reviews:
        by_p[p].append(s)
        by_j[j].append(s)

    results = []
    for p, scores in by_p.items():
        n = len(scores)
        results.append(ProjectResult(
            project_id=p, n=n, raw_mean=sum(scores) / n,
            normalized=mu + q[p], stderr=sd / math.sqrt(n) if n else 0.0,
        ))

    def order(key):
        return sorted(results, key=lambda r: (-round(key(r), 9), -r.n, -round(r.raw_mean, 9),
                                              submitted_at.get(r.project_id, ""), r.project_id))

    for i, r in enumerate(order(lambda r: r.raw_mean), 1):
        r.raw_rank = i
    ranked = order(lambda r: r.normalized)
    for i, r in enumerate(ranked, 1):
        r.rank = i

    judges = []
    for j, scores in by_j.items():
        m = sum(scores) / len(scores)
        spread = math.sqrt(sum((x - m) ** 2 for x in scores) / len(scores))
        flags = []
        if len(scores) >= 2 and spread < 1e-9:
            flags.append("identical scores on every review")
        if len(scores) <= 2:
            flags.append("few reviews, bias shrunk toward zero")
        if abs(b[j]) >= 0.5:
            flags.append("notably generous" if b[j] > 0 else "notably harsh")
        judges.append(JudgeStat(j, len(scores), m, spread, b[j], flags))
    judges.sort(key=lambda s: -abs(s.bias))
    return {"mu": mu, "residual_sd": sd, "projects": ranked, "judges": judges,
            "shrinkage": shrinkage, "reviews": len(reviews), "components": components(reviews)}
