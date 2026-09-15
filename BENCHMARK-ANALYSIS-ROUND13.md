# Round 13: one problem, two runs, hidden-test verdicts, and what the system is missing

Date: 2026-09-14. Working tree = `659bd34` plus the uncommitted architecture changes under review
(candidate_score, sequential solve→oracle, sample-specific oracle smoke check, DESIGN block, prompt edits).
Models: the original pair, strong `qwen/qwen3-coder`, fast `deepseek/deepseek-chat-v3-0324`, via a scratch
copy of `config.toml` (`--config`), so your working-tree config was not touched. Problem: `5cb294c18288`
(python, schema/container state machine), the first of the four focus problems. The first run produced
nothing checkable, so it was run a second time. Both runs were adjudicated by an independent Opus agent
that wrote its own literal reference, hand-traced the statement, and diffed both candidates and the oracle
on 1,000–2,000 random small inputs. Reports: `.superpowers/sdd/2026-09-12-challengebox-solver/bench13-5cb294c18288.md`
and `bench13b-5cb294c18288.md`.

## 1. Headline

**Zero of two emitted solutions would pass hidden tests, and neither run checked anything.**

| Where the run was lost | Run A (bench13) | Run B (bench13b) |
|---|---|---|
| Oracle call timed out at the 120 s cap (a cap the scratch config lowered by mistake; the committed cap is 150 s, see G1) | twice (240 s burned) | once, retry returned in 106 s at 243 s of 285 |
| Oracle usable when it arrived | never arrived | no: 45 % wrong vs an independent reference, crashes on every array descriptor |
| Sample-specific smoke check | not reached | fired, and its own inputs are malformed (one tuple level missing), so it would reject a perfect oracle too |
| Differential evidence | none (no oracle) | none (smoke check disabled it, 41 s left) |
| First-attempt candidate c1 | destroyed at static: unclosed ```` ```python ```` fence not stripped | same |
| Emitted candidate | c2, 21.3 % wrong on random small inputs | c2, 5.0 % wrong on random, 9/12 wrong on the canonical `repeat` shape |
| Would the *other* candidate have passed | no: c1 6.9 % wrong | no: c1 1.3 % wrong on random, 12/12 wrong on the canonical shape |
| Complexity at stated limits | both candidates loop `default k` up to 10^18 and materialize the layout | same |

Read together: the strong model produced four candidates across two runs and every one is wrong on the
same clause (`repeat`) and non-terminating at the limits, so the model is the ceiling on this problem. The
fast model cannot write this oracle (three constant-answer or 45 %-wrong oracles in three rounds) and cannot
write it inside the cap. And two of the uncommitted architecture changes made the run worse, not better:
the sequential start cost 17–24 s of oracle time, and the hardcoded smoke check threw away the only ground
truth the run had.

## 2. Per-run verdicts

| Run | Failed at | Emitted | Hidden tests | Primary cause | Model or architecture | Key evidence |
|---|---|---|---|---|---|---|
| A (bench13) | oracle ×2 timeout; gate had nothing to check | c2 | NO | ARCH-BUDGET | architecture, then model | stress call on the same model returned in 56 s at ~33 tok/s, so an 8000-token oracle needs ~240 s and cannot fit 120 s; retry re-sent the identical prompt at the identical cap; c2 wrong on `open` cursor, array slot, nested `repeat`; `default 10^18` never returns |
| B (bench13b) | oracle timeout then smoke check | c2 | NO | MODEL-SOLVE | model, amplified by architecture | both attempts correct on containers, arrays, defaults, invalid-op result (0/750 when no `repeat` is reachable) and wrong only on `repeat`; c2 flattens `(repeat 0 3)` as `AAABBB…` instead of `ABABAB`; oracle 452/1000 wrong; smoke inputs malformed at `verify.py:64` |

Comparison with round 10 on the same problem: round 10's c1 was reported "correct on 500/500". That verdict
was on random small inputs, which this round shows understate badly (1.3 % random vs 12/12 on the shape
that fills a `repeat` completely). The round-10 c1 was probably wrong on `repeat` too.

## 3. Mechanical facts per run

Latencies are seconds per call, multiple calls separated by `/`. P pass, skip skipped.

| Run | Status | Cand. | Elapsed | Cost | Calls | Tokens in/out | solve | oracle | stress | repair | small | medium | edge | gate steps | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A | unverified | 2 | 264.4 | 0.0135 | 5 | 4653/7704 | 17/24 | 120 (timeout)/120 (timeout) | 56 | – | 0 | 0 | 0 | compile P, all else skip | oracle/stress started at 24.3 s (after solve); candidates added at 144 s; c1 syntax error from fence; "no oracle source" |
| B | unverified | 2 | 243.5 | 0.0160 | 5 | 6369/10339 | 17/17 | 120 (timeout)/106 | 52 | – | 0 | 0 | 0 | compile P, all else skip | oracle/stress started at 17.3 s; oracle arrived at 243 s; smoke 6/6 failed (validate False on all, reference 1 on five); no time to regenerate; c1 syntax error from fence |

Timing: the oracle call is 106–128 s on this statement against a 120 s cap. Correction: that cap came from
the scratch config used for this round, which lowered the fast role's `timeout_cap_s` from the committed
150 s by mistake (a blanket substitution meant only for the strong role). Run B's retry at 106 s would have
fit either cap; run A's two calls exceeded 120 s and may or may not have fit 150 s. Solve calls are 17–24 s with zero
reasoning tokens. Cost was $0.013–0.016 against a $0.50 cap.

## 4. What the AI system is lacking, ranked by what it cost here

Each item is a class of failure any hidden problem can trigger, with the smallest generic change.

### G1. Oracle latency is above the cap, and a timeout is retried identically (both runs, 3 of 4 oracle calls lost)
The fast model streams at ~33 tok/s; an oracle of 4–8k tokens needs 120–240 s. **Correction:** the
committed config already caps the fast role at 150 s; the 120 s cap this round ran under came from the
scratch config, which lowered it by mistake. So "cap below measured latency" is withdrawn. What stands: the
model's latency is at or above any cap the budget can afford, the timeout is logged as "no ===ORACLE===
block" and retried with the same prompt at the same cap, so the second call usually dies the same way, and
the sequential start delays the oracle by the solve latency on top. **Change:** retry only when the
remaining budget covers the cap, with a "shorter output" instruction; stream and abort on a no-bytes stall
so a hang is distinguishable from a slow model; restore the concurrent start; author the oracle with a
model that returns in tens of seconds. And ask for less: `validate()` as "reference did not raise" halves
the output.

### G2. An unclosed code fence destroys a candidate (both runs, c1 both times)
`llm._unfence` strips only a fence with a matching closer. The model opened ```` ```python ```` inside
`===CODE===` and closed it after `===END===`, so the leading fence line went into the file and the
candidate failed static. Half the candidate pool was lost in both runs, and c1 was the better candidate in
both. **Change:** also strip a leading fence line and a trailing bare ```` ``` ```` when unmatched. One
regex.

### G3. The sample-specific smoke check is wrong and disables everything (run B)
`architecture_smoke_cases` hardcodes six inputs for one entrypoint, and all six are malformed: the schema
list is missing one tuple level, so any oracle sees `schemas[0] == ("field","x","int")` and every case
fails. With the nesting restored the adjudicator's reference passes all six and `validate` accepts all six.
The check fired, disabled differential evidence, and left 41 s for nothing. It scores zero on every other
problem by construction. **Change:** delete it. The generic replacement is examples the SOLVE model writes
in its own block, run against both the candidate and the oracle before gating.

### G4. The complexity rule in the prompt does not reach this model (both runs, all four candidates)
The new DESIGN block says to reject any approach that expands a repetition up to 10^18 or scans an
enormous flattened layout. All four candidates do exactly that: `range(k)` for `default k` and a
materialized layout list. With a non-reasoning model a prose rule is not a check. **Change:** the
independent timing gate (round-10 L2) on a grown input, and a fresh-solve path that hands the model the
measured timeout, the input's sizes, and "do not use this approach".

### G5. Random small inputs do not reach the hard state (run B, and round 10's verdict on this problem)
Randomly built operation sequences go invalid before the `repeat` is filled, so a candidate that is wrong
on the whole `repeat` clause shows 1.3 % mismatch on random inputs and 100 % on the canonical shape. The
oracle's `gen()` and the shrinker have the same blind spot. **Change:** one rule in the oracle and stress
prompts: at least half of generated sequences must stay valid to the end and drive every bounded quantity
to its bound; plus the SOLVE-authored examples from G3, which name the shape the author considers hardest.

### G6. Skipped steps are logged as `passed=True` (both runs)
Six skipped gate steps print as passes in the log; only the status line says nothing was checked.
**Change:** log `skipped` explicitly. Cosmetic, but it misleads every reader of `log.txt`.

## 5. Model capability, stated plainly

- **Strong model (`qwen/qwen3-coder`)**: four independent attempts at this statement, all correct on
  containers, arrays, defaults and the invalid-operation result, all wrong on `repeat`, all non-terminating
  at the limits, zero reasoning tokens, 17–24 s per call. Two attempts per run do not reach the intended
  algorithm; a re-solve with concrete failure evidence is the only lever left at this model.
- **Fast model (`deepseek/deepseek-chat-v3-0324`)**: three oracles for this problem across rounds 10 and 13,
  all unusable (constant answer, constant answer, 45 % wrong and crashing on arrays), 106–128 s per call.
  Its self-consistency check does not catch it: `reference()` ran clean on 45/45 of its own `gen()` inputs.
- Cost is not the constraint: $0.013–0.016 per run against a $0.50 cap.

## 6. What to do next, in order

1. G2 and G1 (fence stripping, cap and retry, concurrent start). They are mechanical and they lost three of
   four oracle calls and both first candidates.
2. Remove G3 and replace it with SOLVE-authored examples; add the G5 prompt rule to oracle and stress.
3. G4: the timing gate and the fresh-solve path. That is the only path to a correct `repeat` with this model.
4. Re-run this problem, then the other three focus problems.
