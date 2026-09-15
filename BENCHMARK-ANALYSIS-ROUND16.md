# Round 16: the first full pass

Date: 2026-09-15. Branch `solver` at `36cafa0` (batch 10 plus the user's effort change), 333 tests passing.
One run of the focus problem `5cb294c18288`, no repairs, no regenerations. Per the economy rule no Opus
adjudicator was used; correctness was checked by diffing the new candidate against run 20's, which an
adjudicator had verified on 12,632 inputs, over 1,650 inputs from the new oracle's generator: zero mismatches.

## 1. Headline

**`passed_all_gates` in 131 seconds at $0.36.** Every tier ran, nothing was degraded, nothing was rejected.

| Check | Result |
|---|---|
| Hand-traced examples | 5 of 5 (one the reference crashed on, counted as an error, not a disagreement) |
| Edge cases | 8 of 8, zero invalid |
| Small tier | 200 of 200, zero invalid, answers varied (no weak-tier flag) |
| Medium tier | 20 of 20, zero invalid |
| Behavior | pass |
| Max-size timing | 0.193 s on a 4.4 MB `gen_max` input that the reference itself validated; the author's expected answer matched the candidate's |
| Cross-check vs run 20's adjudicated candidate | 0 mismatches on 1,650 inputs |

## 2. What changed since run 21

Batch 10: code block first in the solve prompt, Sonnet 5 at low effort as the oracle and stress author, cut-off
reporting, budget shares as fractions of the deadline. Plus the user's commit: solve effort lowered from medium
to low.

## 3. Mechanical facts

| Call | Model, effort | Latency | Output tokens (reasoning) | Cost |
|---|---|---|---|---|
| solve | claude-opus-5, low | 107.7 s | 8,805 (5,338) | $0.233 |
| oracle | claude-sonnet-5, low | 76.0 s | 8,562 (3,511) | $0.093 |
| stress | claude-sonnet-5, low | 29.8 s | 2,929 (1,835) | $0.038 |

Timeline: stress reply at 30 s, oracle at 76 s, preparation done by 88 s while the solver was still running,
solve reply at 108 s, example check (20 s, the reference ran on the examples) to 128 s, full gate in 3.5 s,
emit at 131 s. 154 s of the 285 s usable were left, enough for two repairs had one been needed.

Compared with the medium-effort runs: solve latency 108 s against 152–240 s, reasoning tokens 5.3k against
7–10k, and the same correctness on everything checked.

## 4. What the run says about the last open items

- **Solve effort (round 15, T3):** low effort produced a correct candidate 40 % faster than medium and well
  inside the cap. One observation; the prior medium-effort candidates were also correct, so on this statement
  the effort was cost, not capability.
- **Oracle and stress author (T2):** Sonnet 5 at low effort wrote a reference that accepted every one of its own
  inputs, every edge case and the max-size input, and a stress module whose expected answer matched. 76 s is
  inside the solver's latency, so preparation overlapped as designed.
- **Code first (T1):** the reply was complete at 108 s, so salvage was not needed; whether code-first hurts
  code quality did not show on this run.
- **Bounds check (T5):** not exercised; the reference finished on the max-size input, so no bounds verdict was
  needed.

## 5. What is not proven by one run

A single run at low effort. The medium-effort latency spread was 152–240 s over ten runs, so the low-effort
spread is unknown beyond this point. The oracle's quality is one sample. The worst-case timing shape (T4) is
still the stress author's choice: the max input ran in 0.19 s, and the adjudicated in-bounds worst case for a
similar candidate was 5–6 s against the 5 s limit.

## 6. State of the work

Implementation is complete for the scope agreed: one problem, one attempt, hosted models, every budget a
fraction of the problem's deadline. Remaining before a submission: commit the analysis documents and CLAUDE.md,
keep `runs/` out of git, rotate the OpenRouter key, and read the architecture document once against the code.
