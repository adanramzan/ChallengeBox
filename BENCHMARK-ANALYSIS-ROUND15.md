# Round 15: five single-problem runs, one attempt each, and where the last gap is

Date: 2026-09-15. Branch `solver` from `5201aba` to `83f41e4` (batches 5–9 after round 14), 324 tests passing.
Focus problem `5cb294c18288` only, per the standing rule. Strong role `anthropic/claude-opus-5` at medium
effort, one attempt. Runs 18, 19 and 20 were adjudicated by an independent Opus agent (own reference, hand
traces, 3,200–12,632 diffed inputs); runs 17 and 21 produced no candidate and need no adjudication.
Reports: `.superpowers/sdd/2026-09-12-challengebox-solver/bench18-*.md`, `bench19-*.md`, `bench20-*.md`.

## 1. Headline

| Run | Oracle / stress author | Solve | Candidate | Correct (adjudicator) | Max-size timing | Status given | Why not passed_all_gates |
|---|---|---|---|---|---|---|---|
| 17 | qwen / qwen | timeout at 200 s cap | none | – | – | no_candidate | reply discarded on timeout |
| 18 | qwen / qwen | cut at 240 s, code salvaged | c1 | yes, 0 / 12,225 | skipped, 7-op input answered in 0 s | unverified | oracle rejected all examples and edges; tiny max input |
| 19 | qwen / opus-low | 180 s | c1 | yes, 0 / 3,200 | unjudged (5.8 MB, reference cannot finish) | unverified | oracle rejects 52 % of its own inputs; all 75 small answers identical; triggers evaluated too late |
| 20 | qwen / opus-low | 166 s | c1 | yes, 0 / 12,632 | **passed, 0.38 s, bounds-checked 9.8 MB legal input** | unverified | small tier weak (192 of 200 identical); regenerated oracle had a syntax error |
| 21 | opus-low / opus-low | cut at 240 s, code not closed | none | – | – | no_candidate | oracle took 155 s; code block after two prose blocks |

Read together: every candidate the strong model finished is correct, three for three, and run 20 passed every
check the harness can run including a real max-size timing. The status stayed `unverified` only because the
oracle's generator could not produce varied answers. The two runs with no candidate are a latency problem: a
single Opus attempt lands between 152 and 240+ seconds, and the current prompt puts two prose blocks before
the code, so a cut-off can land before the code closes.

## 2. What changed, batches 5–9 (26 commits)

One solve attempt. Unvalidated max-size inputs cannot fail a candidate; the faster of two correct candidates
wins. Examples may use integer arithmetic and span lines; examples and edges are respelled to the shape the
oracle's own generator produces; an oracle rejection of an example is not a disagreement. A candidate is gated
the moment its reply arrives. Repairs use low reasoning effort with a cap that fits, or are skipped. Agreeing
candidates dispute the oracle before any repair. A timed-out streamed reply is salvaged when its code block is
complete, and a lone attempt may use the budget up to a 45 s gate reserve (cap 240 s). Tiers whose answers
barely vary are degraded and regenerate the oracle, with every detected defect in the regeneration text.
Oracle health is judged right after prep, while the solver is still running. The oracle writes a linear
`bounds()` so a real max-size input can count; suspicious or unjudged inputs fall through to the next source.
Per-prompt role mapping and per-tag effort. The stress author states the answer it expects on its own input.
Real cost lookup for salvaged calls. Raw prompts and replies are kept in the run directory.

## 3. Mechanical facts per run

Latencies in seconds. The cost column is what the key was actually charged where known.

| Run | Status | Elapsed | Cost | solve | oracle | stress | examples agree/disagree/rejected | small / medium / edge | stress source, validity | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| 17 | no_candidate | 199.5 | ~0.16 | 199.5 timeout | 20 | 10 | – | 200 / 20 / 1 | gen_large accepted | partial reply discarded |
| 18 | unverified | 241.0 | ~0.16 | 240 cut, salvaged | 45 | 37 | 0 / 0 / 4 | 200 / 6 / 0 | gen_max 3.4 KB, suspicious 0.000 s | 195 of 200 small answers identical (not yet detected) |
| 19 | unverified | 183.2 | 0.57 | 180 | 17 | 79 | 3 / 0 / 0 (2 lines dropped) | 75 / 7 / 8 | gen_max 5.8 MB, unjudged, 2.4 s | 89 of 171 gen inputs rejected; weak tier; health check ran at 180 s with 105 s left, under the 130 s threshold |
| 20 | unverified | 173.8 | 0.57 | 166 | 18, regen 21 | 71 | 5 / 0 / 0 | 200 / 0 / 9 | gen_max 9.8 MB, bounds_checked, **0.382 s pass** | health trigger at 41 s during overlap; replacement oracle unusable (syntax error), original kept |
| 21 | no_candidate | 241.1 | 0.57 | 240 cut, no code | 155 | 70 | – | – | gen_max 6.8 MB, bounds_checked | three Opus calls filled the concurrency cap |

## 4. What the system is lacking now

### T1. The code block sits behind two prose blocks (runs 17, 21)
RULES and DESIGN before CODE was the right order for a non-reasoning model. A thinking model reads in its
reasoning tokens; the visible prose is a second pass of 1–2k tokens before the first line of code, and a
cut-off at the cap then salvages nothing. **Change:** CODE first, EXAMPLES second, then short RULES, DESIGN and
TRAPS; drop ALGORITHM into DESIGN. In batch 10.

### T2. Oracle and stress authors need a model that returns in under a minute (run 21)
qwen was dropped from the fast role for being wrong, not slow; Opus at low effort was right but took 155 s,
which starts preparation late and fills the strong role's concurrency cap. **Change:** `claude-sonnet-5` at low
effort in the fast role for both authors. In batch 10.

### T3. Solve latency variance against a fixed cap (runs 17, 21)
Ten Opus solves at medium effort: 152–240+ s; three of ten did not finish by their cap. Salvage recovers the
code when it is emitted early (T1). If the cut rate stays high after T1, the next lever is lower effort for
the solve, measured against correctness on this problem.

### T4. Worst-case timing coverage is the stress author's guess (run 20)
The legal 9.8 MB input ran in 0.38 s; the adjudicator's in-bounds worst case for the same candidate ran in
5.2–6.3 s against our 5 s Python limit. `EXPECTED_MAX` (batch 9) catches inputs that stop early; nothing
makes the author pick the shape that is worst for this candidate. Not addressed; the judge's limit is unknown.

### T5. A `bounds()` written by a weak model is unsound (run 20)
The qwen `bounds()` omitted descriptor nodes and nesting; the input happened to be legal. **Change:** with
T2 the author is stronger; a cross-check of `bounds()` against `validate()` on the small tier, where both can
answer, is the cheap guard if spurious verdicts appear.

## 5. Model capability, stated plainly

- **`claude-opus-5`, medium effort, as the solver**: three candidates, all correct on 3,200–12,632 inputs
  including counts at 10^18 and the repeat family; asymptotically right each time. Latency 152–240+ s, about
  $0.40 per attempt. The model is no longer the problem on this statement.
- **`claude-opus-5`, low effort, as the stress author**: valid edges and legal, large, 10^18-bearing max
  inputs in three of three runs, one with an off-by-one that ended processing early (now detected by
  `EXPECTED_MAX`). 70–79 s. As the oracle author: 155 s, too slow for the overlap.
- **`qwen3-coder` as the oracle author**: constant answers, precondition checks off by one, no array path,
  a regenerated replacement with a syntax error. Retired from the role.

## 6. What to do next, in order

1. Batch 10 (T1, T2), then one run of 5cb2. If the candidate lands and the oracle's tiers are varied, the run
   should reach `passed_all_gates` for the first time.
2. If the solve is still cut at the cap in more than one run of three, lower the solve effort and measure.
3. Only then the other three focus problems, then all ten.
4. The OpenRouter key has about $0.60 left; a run now costs $0.55–0.70.
