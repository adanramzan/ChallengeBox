# Round 17: five problems, one run each, every emitted solution judged correct

Date: 2026-09-15. Branch `solver` at `60696ce` (six commits after round 16: refusal handling with a fallback
role, empty-reply retry, no-code retry, medium effort restored, thinking-budget mechanism kept for providers
that honour it), 350 tests passing. One attempt per problem, `claude-opus-5` at medium effort as the solver,
`claude-sonnet-5` at low effort as oracle and stress author, Sonnet as the refusal fallback. Each run with a
candidate was adjudicated by an independent Opus agent with its own reference, hand traces and 1,000–19,500
diffed inputs. Reports: `.superpowers/sdd/2026-09-12-challengebox-solver/bench22-*.md` (5cb2, round 16) and
`bench23-*.md` (the other four). The driver stopped when the key balance fell under $0.40.

## 1. Headline

| Problem | Lang | Asks | System status | Adjudicator | Mismatches vs independent reference | Primary cause of any gap |
|---|---|---|---|---|---|---|
| 5cb294c18288 | py | schema/container state machine | **passed_all_gates** | correct | 0 / 1,650 (vs the run-20 adjudicated candidate) | none |
| 5d02bd0e16ab | rs | persistent branching versions | **passed_all_gates** | YES | 0 / 3,560; path-copying segment trees, 30× time headroom | none on the solution; stress input never branches (MODEL-STRESS) |
| 1dea32802072 | py | list insert/remove/reverse, indicator | unverified | YES | 0 / 19,500; implicit treap, identical to the round-14 candidate | stress module lost to a bare `python` line (ARCH-PROMPT) |
| 1c182498c9c7 | rs | wire-format chunk encoder, 7 fields | with failures | LIKELY | 0 / 1,008; oracle 100 % wrong (6 fields, no chunk cap) | oracle wrong, dispute seen with 55 s left (MODEL-ORACLE, ARCH-BUDGET) |
| 1ba0d34fae43 | py | packet/attempt simulation, RLE runs | with failures | LIKELY | 0 / 8,003; solved by the Sonnet fallback after an Opus refusal | author's own example off by one and unpriceable by the oracle (ARCH-GATE) |

**Five of five emitted solutions would pass hidden tests in the adjudicators' judgment.** Two carry the
system's own full pass. Three are right but the system could not certify them, each for a different generic
reason, all on the verification side. Round 10 on these same five problems scored zero of five.

Cost this round: about $3.10 of key credit in total, of which $1.86 on the five counted runs ($0.13–0.67
each) and the rest on four runs lost to the refusal defect before it was fixed, and on probes.

## 2. Mechanical facts per run

| Problem | Elapsed | Cost | solve | oracle | stress | Examples agree / disagree / rejected / unpriced | small / medium / edge | Stress source, validity, time | Notes |
|---|---|---|---|---|---|---|---|---|---|
| 5cb2 | 131 s | $0.36 | 108 (Opus) | 76 | 30 | 4 / 0 / 0 / 1 | 200 / 20 / 8 | gen_max accepted, 0.19 s | first full pass, round 16 |
| 1dea | 242 s | $0.13 + lookup | 239 cut, code salvaged | 26 | 40 | 4 / 0 / 0 / 0 | 200 / 20 / 0 | gen_large accepted, 0.17 s, 1 % of the op budget | stress module import failed |
| 5d02 | 221 s | $0.67 | 216 | 99 | 66 | 4 / 0 / 0 / 0 | 200 / 20 / 8 | gen_max bounds-checked, 0.50 s, zero branching | bounds() accepted 8 of 9 invalid probes |
| 1c18 | 230 s | $0.67 | 225 | 43 | 127 | 0 / 4 / 0 / 0 | 200 / 20 / 8 (all phantoms) | gen_max accepted 17.5 MB, 0.71 s | repair skipped: need 180, left 55 |
| 1ba0 | 194 s | $0.23 | 48 refused → 116 (Sonnet) | 26 | 58 | 3 / 0 / 0 / 1 | 200 / 20 / 8 | gen_max bounds-checked, 0.22 s | repair skipped: need 180, left 91 |

Opus solve latency at medium effort: 108, 216, 225 s and one cut at 239 s. Sonnet as solver: 116 s.

## 3. Failure reasons and how to resolve each

Every item is generic and ordered by what it cost this round.

### F1. A bare language-tag line after the marker kills the whole module (1dea)
The stress reply opened its code with the line `python`, a fence with the backticks dropped. `_unfence`
strips only real fences, so the token became the first statement, the import raised `NameError`, and both
`EDGES` and `gen_max` were lost. The run fell back to a 1 %-size input and the edge tier was empty, which
alone demoted the status. **Resolve:** in `_unfence`, also drop a first line that is exactly a language
name. Three lines. Would have made 1dea a full pass.

### F2. A unanimous example dispute with no time left is recorded as hard failure (1c18)
All four hand-traced examples contradicted the oracle, the system logged the dispute, but the diff tiers
were marked degraded only inside the regeneration branch, which was unaffordable. The report therefore says
"failed edge, small and medium" about a correct candidate. **Resolve:** set the degraded flag at detection,
not after regeneration; status becomes `unverified` and the log says the oracle is disputed. One line.

### F3. An example the oracle cannot price is trusted fully (1ba0)
The author's fourth example needed 10^18 literal iterations; the reference timed out, so there was no
second opinion, and the example was used as ground truth. The trace was off by one; the candidate was
right. **Resolve:** an example with no oracle verdict is `unpriced`: excluded from `diff_examples` and
reported, exactly as a rejected example is. A lone author's trace with no independent check is not evidence.

### F4. The oracle can still misread a statement (1c18)
Sonnet's reference dropped one of seven output fields and never enforced the chunk cap; every gate
counterexample was a phantom. The system detected it (4 of 4 examples disagreed) but had 55 seconds.
**Resolve:** F2 for the reporting; for the outcome, the hand-traced examples already carry the decision and
the emitted candidate was the right one anyway. A regeneration that fits needs solve latency headroom (F5).

### F5. Medium effort leaves no time to act (1c18, 1dea, 5d02)
Three of four Opus solves landed at 216–239 s of 285 usable, so no repair, regeneration or fresh solve was
affordable in any run this round. That was the deliberate trade: low effort is refused by the provider's
classifier on some prompts and medium is not, and the first candidate was right five times out of five.
**Resolve:** nothing to change until a run shows a wrong first candidate; if one does, the lever is the
Sonnet fallback as a second, cheaper solver started in parallel when the Opus reply is late (say past 60 %
of its cap), so a repair-capable candidate exists earlier.

### F6. Provider refusals on the solve prompt (1ba0, and four lost runs before the fix)
Two different classifier messages, "reverse engineering or duplicating model outputs" and "violative cyber
content", both deterministic on their prompts, one tied to the low-effort setting. The client had been
swallowing them as empty replies. **Resolved this round:** refusals are detected, retried once with a
bounded budget where configured, then on the fallback role; 1ba0 was solved by that fallback.

### F7. Max-size inputs are legal but not adversarial (5d02, 1dea, 1ba0)
5d02's input never branched, so a non-persistent solution would have passed; 1dea's used 1 % of the
operation budget; 1ba0's was 24× easier than the adversarial climb the adjudicator built (5.4 s against our
5 s limit). **Resolve:** ask the stress author for two or three max-size inputs of different shapes, one
per structure the intended solution must exploit (deep chains, wide fan-out, the bounded quantity at its
bound), and time all of them; each costs under a second.

### F8. The Sonnet-written `bounds()` is permissive (5d02)
It accepted eight of nine deliberately invalid inputs. It was right about the one input that mattered.
**Resolve:** cross-check `bounds()` against `validate()` on the small tier, where both can answer, and
distrust a `bounds()` that accepts what `validate()` rejects.

### F9. Our Python time limit is stricter than the statements (1dea)
No statement gives a time limit. The correct treap takes 11 s on an operation-maximal legal input; had the
stress module imported, the gate would have failed a correct candidate and sent it to a fresh solve. The
1ba0 adversarial climb sits at 5 s. **Resolve:** raise the Python limit to 10 s and keep timing failures on
the fresh-solve path only, never on the patch repair.

## 4. Model capability, stated plainly

- **`claude-opus-5`, medium effort, as the solver**: four candidates this round, all correct on 1,000–19,500
  inputs, all with the intended complexity: implicit treap, persistent path-copying trees, binary lifting
  with u128 arithmetic, closed-form RLE batching. Latency 108–239 s. Refused by the provider's classifier on
  two prompts.
- **`claude-sonnet-5`, low effort, as the fallback solver**: one candidate, correct on 8,003 inputs, 116 s.
- **`claude-sonnet-5`, low effort, as oracle and stress author**: oracles right on four of five problems and
  100 % wrong on one (dropped field, unenforced cap); stress modules legal on all but never adversarial, one
  lost to a fence typo; `bounds()` weak.

## 5. What to do next

1. F1, F2, F3: three small generic fixes; together they would have made this round three full passes and
   two honest "unverified, oracle disputed" instead of two "with failures".
2. F9 and F7: the timing gate's limit and its inputs.
3. F8: the bounds cross-check.
4. Then the five remaining problems, one run each, when credit allows.
