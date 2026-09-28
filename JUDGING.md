# Judging in Scoreline

This document covers how judges get their projects, how a review becomes a
number, how those numbers are adjusted for judge generosity, and what that
adjustment does to the DOGFOOD fixture data. Every figure below comes from the
seeded database and can be reproduced from the organizer console or
`GET /api/events/evt_01/results`.

## 1. Who judges what

**Tracks.** A judge is attached to zero or more tracks. Zero means "any track".
A judge can only ever be assigned a project in one of their tracks. This is
checked in Python and again by the `assignments_respect_tracks` trigger, so no
code path can get it wrong.

**Conflicts of interest.** A judge can declare a conflict from any project in
their queue. The conflict is recorded against the *team*, the assignment is
removed, and the `assignments_respect_conflicts` trigger stops that judge from
ever being assigned that team again. A person cannot be a judge and a
participant in the same event (trigger `event_members_no_judge_participant`).

**Automatic assignment** (`services.auto_assign`) tops every live project up
to the event's target, 3 judges by default:

1. Projects with the fewest assigned judges choose first.
2. For each project, candidate judges are those on its track, without a
   conflict, and not already assigned.
3. Candidates are ordered by current workload, then by
   `sha256(judge_id + project_id)`.
4. The least-loaded judges are assigned until the target is reached. Any
   project that still falls short is reported by name.

The hash tie-break spreads work evenly without randomness. Running it twice on
the same data gives the same assignments, so an organizer can explain any
single assignment after the fact. Existing assignments are never moved.

On the fixtures, eight projects had only two reviews. Filling the gaps creates
eight assignments, which go to the least-loaded eligible judges. Tomas Varga
(jdg_01) and Anya Sokolova (jdg_23) had one review each, so they get the extra
work first.

**Isolation.** A judge sees exactly their assignment rows. The scoresheet,
the review API and the score listing all go through `assignment_or_403` or an
equivalent check. There is no endpoint that returns another judge's scores to
a judge. Organizers can read any judge's scores in events they organize, and
every such read is written to the audit trail.

## 2. From a review to a number

Each criterion has a weight `w` (any positive number; only the ratios matter)
and a range, 1 to 5 by default. A judge must score every criterion before a
review can be submitted. Drafts are kept but never counted.

The weighted score of one review is

    s = Σ w_c · (1 + 4 · (x_c − min_c) / (max_c − min_c))  /  Σ w_c

This is the weighted mean, rescaled to 1–5 so criteria with different ranges
mix fairly. The fixtures carry no weights, so Scoreline defaults to
functionality 0.40, quality 0.35, innovation 0.25. Weights can be changed at
any time until results are published, and every change is audited with its
old and new values.

## 3. The adjustment for judge generosity

### The problem

Judges differ in how generous they are. With three reviews per project, which
three judges you drew matters. In the fixtures, Wei Lindqvist's reviews
average 4.25 and Hiro Tanaka's average 3.12. The most common fix, z-scoring
each judge, divides by the judge's standard deviation. That breaks in two
ways on this data:

- Iva Petrova (jdg_07) gave 4/4/4 to all three of her projects. Her standard
  deviation is zero, so z-scores divide by zero.
- Tomas Varga (jdg_01) and Anya Sokolova (jdg_23) reviewed one project each.
  A z-score from one data point is undefined. A mean from one data point says
  more about the project than about the judge.

### The model

Scoreline fits an additive model:

    s[j,p] = μ + q[p] + b[j] + ε

where `μ` is the overall mean, `q[p]` is how good project `p` is, `b[j]` is
how generous judge `j` is, and `ε` is noise. The published score for a project
is `μ + q[p]`: the score an average judge would have given it, on the familiar
1–5 scale.

The fit minimizes

    Σ (s − μ − q[p] − b[j])²  +  λ · Σ b[j]²

The penalty `λ` (the event's *bias shrinkage*, default 2) is a ridge penalty
on judge bias. It acts as if every judge had also scored `λ` reviews at
exactly the average. A judge with 11 reviews is barely affected. A judge with
one review has their estimate pulled most of the way back to zero. In Bayesian
terms this is a normal prior on bias, centered on "this judge is average",
and the result is the posterior mode.

### Solving it

The model is solved by block coordinate descent:

1. Set every `q[p]` to the mean of `s − μ − b[j]` over the project's reviews.
2. Set every `b[j]` to `Σ(s − μ − q[p]) / (n_j + λ)`.
3. Repeat until nothing moves by more than 1e-10.

With `λ > 0` the objective is strictly convex, so this converges to the unique
minimum; on the fixtures it takes 60 passes. Biases are then centered
to sum to zero, with the shift moved into `q`, so a bias of +0.3 means
"0.3 points more generous than the average judge". The code is 40 lines of
pure Python in `src/scoreline/scoring.py` and has no dependencies, so it can
be read in one sitting.

### What the model does with the awkward cases

- **Flat judge.** Iva Petrova's 4/4/4 pattern is not a problem for this
  model, because it never divides by spread. Her estimated bias is +0.29, so
  her 4s are read as roughly 3.7s from an average judge. Her reviews still
  count toward her projects. What the model cannot do is extract ranking
  information she did not give: she separated none of her projects. The
  console flags her by name with "identical scores on every review", and that
  is a conversation for the organizer to have, not something to paper over
  with math.
- **One-review judges.** Tomas Varga's single review of 2.00 was well below
  the other scores for that project. Without shrinkage (λ = 0) the model
  would call him 1.68 points harsh, the most extreme judge in the event,
  on the strength of one review. With λ = 2 his estimated bias is −0.46. The console flags him with
  "few reviews, bias shrunk toward zero".
- **Uneven coverage.** Projects with 2 reviews and projects with 5 are both
  estimated, but not equally precisely. Each score carries a standard error,
  `σ̂ / √n` (σ̂ = 0.72 on the fixtures). The console shows it as ± next to
  every score and turns review counts below the target red.
- **Duplicates.** prj_41 is a second copy of prj_07. It is excluded from the
  fit until the organizer chooses which copy to keep, so the same work is
  never counted twice. Its four reviews are kept as evidence.

### Connectivity

Bias is only identifiable between judges linked by shared projects. If one
group of judges reviews one group of projects and no judge crosses over,
nothing in the data says which group was more generous. Adjusted scores are
then comparable within a group but not across groups. Scoreline computes these
connected groups (`scoring.components`) and warns on the organizer overview
if there is more than one.

On the fixtures, all 30 judges and 40 live projects form a single connected
group. Judges who cover two tracks, such as Diego Herrera (developer tools and
education), stitch the tracks together. So one overall ranking is meaningful.
This property is tested in `tests/test_scoring.py`.

## 4. The proof on the fixture data

After import, 122 finished reviews of 40 live projects count toward the
ranking (126 in the file, minus the 4 on the duplicate). The overall mean is
3.56 and the residual standard deviation is 0.72.

### Top of the table

| Rank | Raw rank | Project | Reviews | Raw mean | Adjusted | ± |
|---:|---:|---|---:|---:|---:|---:|
| 1 | 1 | Iron Switch (prj_34) | 3 | 4.35 | 4.34 | 0.42 |
| 2 | 2 | Salt Ledger (prj_11) | 4 | 4.34 | 4.27 | 0.36 |
| 3 | 4 | Dry Relay (prj_25) | 3 | 4.12 | 4.17 | 0.42 |
| 4 | 7 | Salt Kiln (prj_16) | 3 | 4.07 | 4.15 | 0.42 |
| 5 | 3 | Still Beacon (prj_10) | 2 | 4.25 | 4.12 | 0.51 |
| 6 | 5 | Salt Loom (prj_37) | 4 | 4.09 | 4.08 | 0.36 |
| 7 | 6 | Slow Trail (prj_33) | 3 | 4.08 | 3.99 | 0.42 |
| 8 | 8 | Copper Kiln (prj_21) | 3 | 3.93 | 3.90 | 0.42 |

Still Beacon drops from 3rd to 5th. It had only two reviews, and one came
from Yuki Sato, among the most generous judges (+0.33). Salt Kiln moves up
from 7th to 4th: two of its three judges are among the strictest, Hiro Tanaka
(−0.45) and Marek Nowak (−0.22). The top two do not change.

### The biggest movers, and why

Of 40 projects, 32 change rank. That sounds dramatic until you look at the
size of the adjustments. The middle of the table is crowded: many projects sit
within 0.05 of their neighbours, so a small adjustment moves them many places.

- **Warm Beacon (prj_35), 18th to 28th.** All five of its judges are on the
  generous side (+0.09 to +0.25). The adjustment takes 0.14 off its score,
  3.49 to 3.35, which drops it ten places through the crowd.
- **Open Kiln (prj_18), 24th to 16th.** It had only two reviews: 4.30 from
  Noor Haddad and 2.60 from Diego Herrera. Both lean slightly harsh.

In both cases the change in score is smaller than the project's own error bar
(±0.32 and ±0.51). The honest reading is that places 10 to 35 are close to
a statistical tie. The console says this next to the table instead of letting
a rank number imply more precision than the data has.

### Sensitivity to the one tuning knob

Here is the ranking recomputed for different shrinkage values. ρ is Spearman
rank correlation with the published ranking (λ = 2).

| λ | Top three | ρ vs λ = 2 | ρ vs raw means |
|---:|---|---:|---:|
| 0 | Salt Kiln, Salt Ledger, Dry Relay | 0.83 | 0.78 |
| 0.5 | Salt Kiln, Iron Switch, Salt Ledger | 0.98 | 0.92 |
| 1 | Iron Switch, Salt Ledger, Salt Kiln | 0.995 | 0.95 |
| **2** | **Iron Switch, Salt Ledger, Dry Relay** | **1** | **0.96** |
| 5 | Iron Switch, Salt Ledger, Still Beacon | 0.995 | 0.98 |
| 20 | Iron Switch, Salt Ledger, Still Beacon | 0.98 | 0.99 |

- λ = 0 trusts one-review judges completely, and the order swings, which is
  the failure shrinkage exists to prevent.
- Very large λ collapses back to raw means.
- Between 1 and 5 the ranking barely moves, and the top two are the same at
  every setting from 1 upward. We chose 2 because it sits in the middle of
  that stable range, not because it produced a preferred winner.

### Leave-one-judge-out

As a stability check, the ranking was recomputed 30 times, each time with one
judge's reviews removed:

- Iron Switch stays first in 24 of 30 runs.
- The published top three (Iron Switch, Salt Ledger, Dry Relay) is unchanged,
  in the same order, in 17 of 30 runs.
- Across all 30 runs, only eight different projects ever reach the top three,
  and Iron Switch or Salt Ledger is in first place in 27 of them.

No single judge decides the winner.

## 5. Ties, publication and locking

**Ties** break, in order, on:

1. The adjusted score.
2. More reviews.
3. The higher raw mean.
4. Earlier submission.
5. Project id.

The ranking is fully deterministic.

**Hidden until published.** Results are hidden from everyone except the
event's organizers until they publish:

- The public results page returns 404.
- The results API returns 401 or 403.

**Publishing** does three things:

- It writes a snapshot of the full ranking into the audit trail.
- It shows the ranking publicly, without per-judge detail.
- It locks every score with the `review_scores_frozen_*` triggers.

An organizer can unpublish to correct a mistake. That action is audited too,
and the earlier snapshot remains in the trail for anyone to compare against.

## 6. Limits we know about

- **Additive, not multiplicative.** The model corrects a judge's *level*,
  not their *spread*. A judge who uses only 3s and 4s and one who uses the
  full 1–5 range get the same treatment. A per-judge scale term would handle
  that, but with 2–11 reviews per judge it would be estimated from too little
  data to trust. We flag zero-spread judges instead.
- **One noise level for everyone.** Every judge is assumed to be equally
  noisy. With this little data per judge, estimating per-judge noise would
  mostly fit noise.
- **Not a verdict.** The adjustment is a tool for the organizer, not a
  replacement for their judgement. The console shows raw and adjusted side
  by side, and the CSV export includes both.
