# Round 14: one problem, four batches of fixes, first correct candidate, and what still blocks a pass

Date: 2026-09-14. Branch `solver` at `27e19a2` (19 commits after round 13's `659bd34`), 247 tests passing.
Focus problem `5cb294c18288` (python, schema/container state machine). Two runs of it are compared here,
plus two side runs from a four-problem batch that the key limit cut short. Every run was adjudicated by an
independent Opus agent that wrote its own literal reference, hand-traced the statement, and diffed
candidates and oracle on 1,000–4,000 inputs including the `repeat` family that random inputs miss.
Reports: `.superpowers/sdd/2026-09-12-challengebox-solver/bench14-*.md`, `bench15-*.md`, `bench16-*.md`.

## 1. Headline

| Run | Strong / fast model | Emitted | Correct per adjudicator | Fast enough on legal max input | Status the system gave | Hidden tests |
|---|---|---|---|---|---|---|
| bench14, 5cb2 | qwen / qwen | c4 after 2 repairs | yes, 0 % on every family | no: iterates `default k` to 10^18 | unverified (false example dispute) | UNLIKELY |
| bench15, 5cb2 | claude-opus-5 / qwen | c1 of 2, no repair fit | yes, 0 % on every family | yes: 0.74 s worst on in-spec maxima | with failures (stress on an illegal input) | **LIKELY** |
| bench16, 1c18 (side) | claude-opus-5 / qwen | c1 | 19.8 % wrong: one sentinel bug at R = N | yes, 0.87 s at max | with failures | NO |
| bench16, 1dea (side) | claude-opus-5 / qwen | c1 of 2 | yes, 0 % on 3,445 inputs; intended treap | 5.1 s vs our 5 s cap; no stated limit | with failures (oracle wrong) | LIKELY |

Read together: with a thinking model in the strong role the candidates are now right, and the system is
what stands between them and a clean pass. On the focus problem the model produced a correct, fast solution
and the gate failed it on an input the statement forbids. On the side problems, one run was lost to a wrong
oracle the system had two-versus-one evidence against and did not use, and one to a one-line boundary bug
that a repair could have fixed if the repair call had fit its cap.

## 2. What changed between round 13 and these runs

Nineteen commits in four batches, all generic. Cleanup: candidate ranking by mismatch evidence, repair sees
the failed child's diff, sequential generation reverted, sample-specific smoke check deleted, prompts
de-sampled. Batch 1: unclosed fence stripped, oracle authored by the strong-tier model, skipped steps logged
as skipped. Batch 2: SOLVE emits hand-traced examples that gate the candidate and check the oracle;
`validate()` is "reference did not raise"; generators must reach the deep state; RULES before CODE.
Batch 3: every gate tier runs, stress always runs, `gen(seed, "large")` as the max-size fallback, fresh solve
on timing failure / `approach` verdict / regression. Batch 4: oracle rejections are not disagreements,
max-size inputs must drive the huge-bounded quantities, oracle prep overlaps the solver, medium tier
time-boxed, solver ceiling 70 %, streaming with stall abort, `claude-opus-5` at medium effort.

## 3. Mechanical facts per run

Latencies in seconds per call. Reasoning tokens are part of completion tokens.

| Run | Status | Cand. | Elapsed | Cost | solve | oracle | stress | repair | reasoning tok | small / medium / edge | stress source | examples agree/disagree/rejected | Gate result |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bench14 5cb2 | unverified | 4 | 163.6 | 0.049 | 17 / 17 | 30, regen 21 | 51 | 13 / 11 | 0 | 200 / 0 / 2 | gen_max 476 KB (no `default` op, invalid op at index 1) | 3 / 7 (all validate rejections) / – | c4 all pass; diff evidence degraded |
| bench15 5cb2 | with failures | 2 | 239.1 | 0.763 | 152 / 196 | 29 | 12 | – | 7.4k / 9.7k | 200 / 20 / 9 | gen_max 8.7 MB, nesting 200,000 vs cap 60 | 6 / 0 / 2 | c1, c2: all correctness tiers pass, stress killed at cap |
| bench16 1c18 | with failures | 1 | 275.0 | 0.375 | 168 / timeout 200 | 12 | 9 | timeout 67 | 8.6k | 200 / 20 / 9 | medium, degraded | 3 / 1 / 0 | edge 1/9, small 35/200, medium 6/20; all phantoms |
| bench16 1dea | with failures | 2 | 192.8 | 0.754 | 167 / 173 | 13 | 11 | 403 | 7.9k / 9.6k | 158 / 13 / 9 | gen_max 4.6 MB | 8 / 1 / 0 | small 44/158 on the oracle's bug; stress 5.38 s vs 5 |

Opus at medium effort spends 7–10k reasoning tokens per solve, which is 150–200 s. Two attempts in parallel
cost no wall time but $0.75 per problem, and leave no room for a repair (cap 67–71 s) or a fresh solve.

## 4. What the system is lacking now, ranked by what it cost on the focus problem

### S1. A max-size input the validator cannot judge is used as the only timing evidence (bench15)
The stress author's `gen_max` defined `MAX_NESTING = 60` and never used it; the input nested 200,000 deep.
`validate()` is the literal reference, which cannot finish on a max-size input, so it returned no verdict,
and the harness used the input undegraded. Both candidates were killed at the cap on an input the statement
forbids; the correct one was emitted "with failures" and only by tie-break (the other is genuinely 10× slower
on legal maxima). **Change:** an input with no validate verdict gives *degraded* stress evidence, never a
failure; the stress prompt requires `gen_max` to assert every stated bound on the input it returns before
returning it (depth and nesting caps included); and stress ranks candidates by measured duration when both
pass, so the faster of two correct candidates wins.

### S2. `parse_examples` drops any example spelled with arithmetic (bench15)
`ast.literal_eval` rejects `10**18`, so the two examples that covered the 10^18 trap were dropped and c1
kept only three small ones. **Change:** evaluate example lines with a restricted AST that allows integer
arithmetic (`+ - * ** //` and unary minus over constants) inside literals.

### S3. Gating waits for the slowest solve attempt (bench15 43 s, bench16-1c18 31 s)
`solve()` joins on all attempts before gating any. With a thinking model the second attempt can be 40 s
behind the first or hit the cap. **Change:** gate each candidate as its reply arrives, while the other
attempt is still running; the second is gated when it lands.

### S4. The repair call cannot fit a thinking model (bench16-1c18: 67 s timeout on a 168 s role)
The repair cap is 25 % of usable, tuned to a 12 s non-reasoning model. **Change:** per-role
`repair_extra` (reasoning effort `low` for repairs and fresh solves) and a repair cap that follows the role's
measured latency; a repair that cannot fit is skipped, not started.

### S5. Two candidates against one oracle is still not a signal (bench16-1dea)
Both candidates agreed byte-for-byte against the oracle on the failing input, and a hand-traced example
contradicted the oracle on the same rule, but the dispute threshold is per example (1 of 9) and the repair
call errored, whose fallback verdict is `candidate`. **Change:** identical candidate outputs on the failing
case count as a disagreement toward the oracle dispute (two independent authors), and a repair call that
errors produces no verdict at all.

### S6. The examples author's own inputs can be out of contract (bench14: 3 of 10; bench15: 2 of 8)
Handled since batch 4 (rejections are not disagreements), but the prompt can ask for less waste: examples
must satisfy every stated precondition, and the author should spell containers exactly as the contract says.
**Change:** one sentence in the SOLVE prompt's examples block.

## 5. Model capability, stated plainly

- **`anthropic/claude-opus-5`, medium effort**: correct semantics and intended complexity on 5cb2 and 1dea,
  and the right reading plus one sentinel bug on 1c18, where every earlier model misread the statement.
  150–200 s per solve, $0.30–0.40 per attempt. This is the model to keep for the strong role.
- **`qwen/qwen3-coder` as the oracle author**: 12–30 s per call and usable on 5cb2, but wrong on 1c18 (two
  misreads, every counterexample a phantom) and 1dea (one wrong conjunct, 28–67 % wrong). The oracle is now
  the weaker author, which is what S5 is for.
- The OpenRouter key's total limit was hit at $1.13 into the four-problem batch; the last two rows of that
  batch are key failures, not solver results.

## 6. What to do next, in order

1. S1 and S2: they are what turned a correct, fast 5cb2 candidate into "with failures".
2. S3 and S4: they make a repair possible at all with the thinking model.
3. S5 and S6: cheap, and the side runs show they cost real points.
4. Re-run 5cb2 alone (per the standing rule, one problem until it passes), then scale out.
