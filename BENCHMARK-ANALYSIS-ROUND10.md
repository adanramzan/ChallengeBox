# Round 10: full run, hidden-test verdicts, and what the system is missing

Date: 2026-09-14. Branch `solver` at `659bd34` (31 commits after the external audit), 186 tests passing.
All ten samples ran to completion in one pass with zero network or provider errors. Total cost $0.246.
Each run was then adjudicated by an independent Opus agent that compiled and executed the emitted
solution, hand-traced the statement, wrote its own tiny reference, and diffed on 100–3,500 random
inputs. The ten full reports are in `.superpowers/sdd/2026-09-12-challengebox-solver/bench10-*.md`.

## 1. Headline

**Zero of ten emitted solutions would pass hidden tests.** Every adjudicator returned NO.

But the reason splits cleanly, and it matters for what to do next:

| Where the run was lost | Runs | Problems |
|---|---|---|
| Strong model never produced the intended algorithm (correctness or complexity) | 7 | 1ba0, 1c18, 2bef, 4e49, 5d02, 6e43, 6eca |
| A correct-on-small candidate existed and the architecture repaired it into a wrong one or deselected it | 4 | 1dea, 4e49, 5cb2, 5ce3 |
| Of those four, the correct candidate would still have timed out at max size | 3 | 1dea, 4e49, 5ce3 |
| A likely fully correct candidate was destroyed by the system | 1 | 5cb2 |
| Max-size timing was never measured on a real max-size input | 9 | all but none: gen_max failed or was rejected in 9 of 10 |

Read together: the model's capability sets the ceiling at roughly 1–3 of 10 on this sample set, and the
architecture is currently losing the points the model does earn. Both need work. The architecture work is
cheap and generic; the model work is a config line and a cost decision.

## 2. Per-problem verdicts

| Problem | Lang | What it asks | Failed at | Emitted | Hidden tests | Primary cause | Model or architecture | Key evidence |
|---|---|---|---|---|---|---|---|---|
| 1ba0d34fae43 | py | packet/attempt simulation with RLE outcome runs | diff_edge after 2 repairs | c4 | NO | MODEL-SOLVE | model, amplified by repair | one field wrong (`attempts` not counted on implicit error), 18% mismatch; walks RLE one attempt at a time, 10^18 never finishes; both repairs edited a correct part |
| 1c182498c9c7 | rs | wire-format chunk encoder, 7-field output | diff_small 35/200 after oracle regen | c2 | NO | MODEL-SOLVE | model | closes chunk after overflow instead of before; O(N·Q) is >600 s; all five model artifacts share the misreading; two oracles disagreed 200/200 |
| 1dea32802072 | py | list with insert/remove/reverse, indicator tracking | diff_small 4/189 after 2 repairs | c4 | NO | ARCH-REPAIR | architecture, then model | oracle prepends an entry the statement forbids; both candidates were right on all 11 edges; repair blamed the candidate and injected the oracle's bug; underlying algorithm cubic anyway |
| 2beff58fa923 | py | reference refresh over snapshots | diff_edge 6/7, 0 repairs, 235 s | c1 | NO | MODEL-SOLVE | model, amplified by budget and prompt | five semantic bugs and O(snapshots×entries); SOLVE read records as tuples, ORACLE as dicts; 201 s spent in two oracle calls, self-repair left 50 s, no repair possible |
| 4e49a099fd84 | rs | grapheme/UTF-16 boundary queries | diff_small 70/96 after 2 repairs | c2 | NO | MODEL-SOLVE | model, amplified by oracle and selection | both attempts Θ(Q·G) ≈ 920 s; oracle wrong on 59/96 of its own inputs (off-by-one); c1 had 0/1600 mismatches and was rejected; tiebreak emitted the 78%-wrong sibling |
| 5cb294c18288 | py | schema/container state machine | diff_edge 3/11 after 2 repairs | c4 | NO | MODEL-ORACLE | architecture | oracle never pushed the root container so it answered 1 for everything; validate rejected 100% of gen; 9 edges malformed by the stress author; **c1 was correct on 500/500** and was repaired into c4 (74/500 wrong, hangs on 10^18) |
| 5ce30ef9e5cf | rs | cell/activation VM with START/GET/STACK/STOP | diff_edge 5/8 after 2 repairs | c4 | NO | ARCH-REPAIR | architecture, then model | failing edge is out of spec; validate is a parser, not a simulator, so it accepted it; c2 was correct (0/300) and was rewritten into c4 (97/300); even c2 has O(Q²) STOP ≈ 185 s |
| 5d02bd0e16ab | rs | persistent branching versions | none; stress degraded | c4 | NO | MODEL-SOLVE | model, hidden by gate | semantically flawless (0/450) but clones full state per op: Θ(Q·N) time and memory, 36.9 GB at max; gen_max crashed; a 12-op medium case stood in for max size and "passed" |
| 6e43a08ec05a | py | row-run protection with edits | diff_edge 10/11 after 2 repairs | c4 | NO | MODEL-SOLVE | model, amplified by shrink and selection | both attempts wrong; shrinker emptied `edits` and the repair deleted edit handling; emitted c4 (0.9% correct) over c1 (32% correct) |
| 6eca8a9120e0 | py | capture-avoiding declarations | diff_small 4/200 after 2 repairs | c3 | NO | MODEL-SOLVE | model, hidden by gate | misread record format, recursion over 250k-deep paths, 46% wrong; all 220 gate cases had expected `[]`, so a constant function would have passed |

## 3. Mechanical facts per run

Latencies are seconds per call (multiple calls separated by `/`). Steps: P pass, F fail, deg degraded, skip skipped.

| Problem | Status | Cand. | Elapsed | Cost | Calls | Tokens in/out | solve | oracle | stress | repair | small | medium | edge | gate steps | Verdicts | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1ba0d34fae43 | with_failures | 4 | 78.0 | 0.0166 | 6 | 13563/8512 | 14/15 | 40 | 27 | 18/11 | 200 | 20 | 9 | compile P, edge F (5/11) | cand/cand | gen_max rejected by validate |
| 1c182498c9c7 | with_failures | 3 | 161.2 | 0.0219 | 7 | 16248/12158 | 13/14 | 55/62 | 19 | 14/12 | 200 | 0 | 10 | compile P, edge deg, small F (35/200) | oracle/cand | regen disagreed 200/200; medium skipped (late); no stress source |
| 1dea32802072 | with_failures | 4 | 90.5 | 0.0205 | 6 | 13790/11206 | 22/23 | 63 | 41 | 8/10 | 189 | 17 | 11 | compile P, edge P, small F (4/189) | cand/cand | gen_max rejected by validate |
| 2beff58fa923 | with_failures | 2 | 234.7 | 0.0208 | 5 | 8762/14569 | 24/27 | 117/84 | 71 | – | 45 | 0 | 7 | compile P, edge deg F (6/7) | – | selfrepair; validate rejected 155/200; medium skipped (settle); gen_max 47 MB |
| 4e49a099fd84 | with_failures | 4 | 122.3 | 0.0302 | 6 | 16843/16433 | 18/20 | 82 | 29 | 17/16 | 96 | 2 | 5 | compile P, edge P, small F (70/96) | cand/cand | imports added by rustc; gen_max rejected |
| 5cb294c18288 | with_failures | 4 | 205.2 | 0.0267 | 6 | 14527/15482 | 14/16 | 110 | 52 | 19/10 | 0 | 0 | 9 | compile P, edge F (3/11) | cand/cand | small tier unusable (constant answer); gen(medium) timeout; gen_max failed |
| 5ce30ef9e5cf | with_failures | 4 | 177.6 | 0.0325 | 6 | 15641/18306 | 15/17 | 118 | 28 | 42/10 | 31 | 0 | 6 | compile P, edge F (5/8) | cand/cand | medium skipped (late); gen_max failed |
| 5d02bd0e16ab | unverified | 4 | 128.6 | 0.0204 | 6 | 15214/11320 | 12/13 | 97 | 37 | 10/11 | 200 | 20 | 10 | all P, stress deg | cand/cand | gen_max crashed |
| 6e43a08ec05a | with_failures | 4 | 114.5 | 0.0215 | 6 | 14157/11495 | 24/35 | 60 | 35 | 23/9 | 200 | 20 | 9 | compile P, edge F (10/11) | cand/cand | gen_max rejected by validate |
| 6eca8a9120e0 | with_failures | 4 | 126.9 | 0.0350 | 6 | 15211/20749 | 17/37 | 86 | 79 | 14/13 | 200 | 20 | 0 | compile P, edge P, small F (4/200) | cand/cand | edge tier unusable; gen_max failed |

Timing: no run approached the deadline; the slowest used 235 s of 285 usable. Oracle latency ranged 40–118 s
and is the largest single time cost in every run. Repair calls are 8–42 s.

## 4. What the AI system is lacking, ranked by how many runs it cost

Each item is a class of failure that any hidden problem can trigger, with the smallest generic change.

### L1. Candidate selection ignores evidence it already has (5 runs: 1ba0, 4e49, 5cb2, 5ce3, 6e43)
`best_candidate` ranks on gates passed, then newest id. When a repair fails at the same step as its parent,
the newer, worse child wins. The mismatch fraction per step is already recorded and would have flipped
the emission in at least three runs. **Change:** rank by (gates passed, fewest mismatches at the failing
step, then *older*); abort the repair loop when a child is worse than its parent and revert to the parent.

### L2. No oracle-independent performance check (7 runs would TLE; measured on none)
Stress is last in a short-circuiting chain, so any correctness failure suppresses it; it depends on a
fast-model `gen_max` that failed or was rejected in 9 of 10 runs; and a degraded stress "passes". Seven
emitted solutions are asymptotically wrong and the system has no timing data on any of them. **Change:**
run the timing step before and independently of the differential steps (it needs no oracle); give STRESS
the same one-shot regeneration the oracle has; when `gen_max` is unusable, synthesise a max-size input by
scaling the largest validating small input to the stated bound; make degraded stress a failure, not a
pass; add one SOLVE-prompt rule to multiply the constraint bounds before choosing an algorithm.

### L3. No token-free majority signal when the oracle is wrong (4 runs: 1dea, 4e49, 5cb2, 5ce3)
Two independent candidates and one oracle exist at gate time. When both candidates agree with each other
and disagree with the oracle on every case, that is two-versus-one evidence against the oracle, and the
system instead spends a strong-model call that blames the candidate. **Change:** when two or more
candidates fail the same tier with identical output and mismatches equal cases, force the oracle verdict
and regenerate the oracle first; route whole-tier disagreement to regeneration and only repair the
candidate if the fresh oracle still disagrees; when two oracles disagree above threshold, spend one
strong-model call on a single concrete input to pick a side.

### L4. `validate()` is a parser, not a simulator (5 runs: 1ba0, 4e49, 5cb2, 5ce3, 6eca)
Models write token-shape and range checks and stop; every precondition that depends on evolving state
("must currently exist", "never declared") is unenforced, so out-of-spec edges become ground truth. Also:
gen() crashes are not counted toward oracle health (52% of small seeds died uncounted in 4e49), and total
rejection is recorded as zero rejection so the regeneration trigger cannot fire. **Change:** require
`reference()` to raise on any violated precondition and define `validate()` as "reference did not raise";
count gen crashes in the health fraction; record all-rejected as 1.0; make oracle distrust global rather
than per tier.

### L5. Non-discriminating tiers (1 run: 6eca, and a latent class)
All 220 gate cases had the same expected value, so a constant function would have passed. **Change:**
after building a tier, if every expected value is identical over distinct inputs, mark it non-discriminating
and use the oracle regeneration with a note that gen() must construct positive cases.

### L6. Repair prompt and shrinker quality (3 runs: 1ba0, 6e43, 2bef)
No field-level diff of expected vs actual (a single wrong integer in a nine-field dict was invisible);
a repair that produces behaviourally identical output is not detected; the shrinker empties list-valued
dimensions and the model then deletes the corresponding logic; `max_repairs` is a count, not a budget,
and stopped one run at 78 s of 285. **Change:** recursive field diff in the prompt; compare child and
parent output on the failing input and re-issue with a note when identical; prefer the smallest non-empty
witness when shrinking lists; allow further repairs while time and cost remain.

### L7. Budget interactions (2 runs: 2bef, 1c18)
Oracle self-repair may start when its typical completion leaves less than one repair's worth of time.
**Change:** gate self-repair on the sum of its own and one repair's afford thresholds.

### L8. Input-side container convention (1 run: 2bef)
The Python convention covers the returned container but not inputs, so SOLVE read records as tuples and
ORACLE emitted dicts. **Change:** one sentence extending the shared rule to records described by field name.

## 5. Model capability, stated plainly

- **Strong model (`qwen/qwen3-coder`)** did not find the intended algorithm on seven problems: implicit
  treap, suffix-array/LCP over graphemes, path-copying persistence, chunk-split-before-overflow, and
  capture-avoiding substitution among them. It also never multiplies constraint bounds before choosing an
  approach. Two attempts plus two repairs do not reach these.
- **Fast model (`deepseek/deepseek-chat-v3-0324`)** misreads output arity and global conventions when writing
  the oracle (200/200 disagreement between two of its own oracles on 1c18; constant output on 5cb2; an
  off-by-one on 4e49 that rejected a correct candidate), writes validators that are not simulators, and
  takes 40–118 s per call, which is the largest time cost in every run.
- Cost was $0.02–0.035 per problem against a $0.10 cap, so there is room to buy stronger models. Both are
  one line each in `config.toml`.

## 6. What to do next, in order

1. L1 and L3 together: they are the cheapest and they stop the system from destroying correct work.
2. L2: an independent timing gate is the only way the seven asymptotically-wrong solutions become visible.
3. L4 and L5: they make the oracle's own evidence trustworthy.
4. Then re-run with a stronger strong model, because after 1–3 the architecture will no longer be the
   thing losing the points, and the model will be.
