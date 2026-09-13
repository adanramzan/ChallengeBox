# ChallengeBox Solver — Usage

Design rationale lives in `ChallengeBox-Agent-Architecture.md`. This file is the practical how-to.

## Setup

```bash
uv sync --group dev
curl https://sh.rustup.rs -sSf | sh   # if rustc is not already on PATH; then: source ~/.cargo/env
export OPENROUTER_API_KEY=sk-or-...
```

Always invoke Python through `uv run` (CPython 3.11). The system `python3` must never be used.

## The one command

```bash
uv run python solve.py samples/<id>.json -o runs/out.py    # or out.rs for a Rust problem
```

Exit codes: `0` passed all local gates, `1` emitted but with unverified or failing gates, `2` invalid
problem input, `3` no candidate at all. Useful flags: `--deadline-scale 0.25` shrinks the usable budget
for testing the emit-under-pressure path; `--profile <name>` and `--config <path>` select a different
`config.toml` profile; `--run-dir` overrides where artifacts land (default `runs/<problem_id[:12]>-<YYYYmmdd-HHMMSS>/`,
timestamped so re-running a problem never overwrites the previous run).

## Where reports land, and how to read `report.json`

Each run writes `<run_dir>/report.json`, `<run_dir>/log.txt`, and `<run_dir>/candidates/c*.{py,rs}`.
The solution file itself goes wherever `-o` points.

In `report.json`:

- `status` — one of `passed_all_gates`, `emitted_unverified`, `emitted_with_failures`, `no_candidate`.
- `final_candidate` — the id (`c1`, `c2`, ...) of the emitted candidate, or `null`.
- `evidence` — a dict of candidate id to a list of gate results (`kind`, `passed`, `cases`, `duration_s`,
  `detail`, `skipped`). Kinds, in gate order: `compile`, `diff_public` (only present if the problem
  shipped `public_examples` and they parsed — see below), `diff_edge`, `diff_small`, `diff_medium`,
  `behavior`, `stress`, `overflow` (Rust only — see below). `skipped: true` means the gate had
  nothing to run against (e.g. the oracle produced no cases), not that it passed for real — that's
  why `passed_all_gates` requires no evidence entry to be skipped, with one deliberate exception:
  a Python candidate's `overflow` entry is always `skipped: true` (Python integers are arbitrary
  precision, so the check is definitionally not applicable, not a coverage gap) and does not by
  itself block `passed_all_gates`. A Rust candidate's `overflow` entry being skipped (no stress
  input, or no budget left) is a real gap and still blocks it, same as any other skipped step. A
  `stress` entry whose `detail.degraded` is set means `gen_max` failed and the timing ran against
  the largest available `medium`-tier case instead — a weak, non-zero signal, not a real max-size
  check; don't read that timing as proof the solution is fast at true maximum scale.
- `diff_public` runs first among the gate's differential-style checks — right after compile, before
  `diff_edge`/`diff_small`/`diff_medium` — because `public_examples` (when the problem ships any) is
  the only evidence in the whole system that isn't manufactured by a model: real input/output pairs
  from whoever set the problem. A candidate that fails one fails fast, on the most trustworthy check
  available, before any model-generated case is even considered. These cases are never run through
  the oracle's `validate()` and never dropped if the oracle's `reference()` disagrees with one — a
  disagreement there means the *oracle* is wrong, and is recorded in `gate_inputs.notes` and in
  `diff_public`'s `detail.oracle_disagreements`, not treated as grounds to discard the example. With
  no public examples (every sample in `samples/` today), `diff_public` doesn't appear in `evidence`
  at all and the gate is exactly what it was before this existed.
- When every step past `compile` is skipped (typically: no oracle source at all, even after the
  retry below), `events` logs `gate.unverifiable <reason>` instead of `gate.passed` — the run
  genuinely checked nothing, and `status` still reads `emitted_unverified`, not `passed_all_gates`.
  This is not turned into a candidate repair: repairing the candidate can't produce an oracle, and
  it would spend both repair attempts on nothing.
- `repairs`, `syntax_repairs`, `oracle_regenerated`, `oracle_selfrepaired` — how many repair attempts
  and oracle regenerations were used. `repairs` counts semantic repairs (following a `diff_*`,
  `behavior`, `stress`, or `overflow` gate failure) against `[limits] max_repairs` (default 2);
  `syntax_repairs` counts repairs following a mechanical `static` or `compile` failure against
  `[limits] max_syntax_repairs` (default 2). `oracle_regenerated` and `oracle_selfrepaired` are two
  *independent* one-shot oracle-regeneration counters, both capped at 1 per run and both able to fire
  in the same run: `oracle_regenerated` counts adjudication (a repair call blamed the oracle for a
  wrong answer on a specific input); `oracle_selfrepaired` counts the orchestrator noticing, on its
  own, that the oracle's `reference()`/`gen()`/`validate()` crashed at runtime and yielded zero usable
  cases in every tier (small/medium/edge) even though the ORACLE call itself succeeded — a failure
  mode `gate_inputs.notes` already recorded but nothing used to act on before this fix. All four
  counters are mutually independent budgets, so a run can spend up to each without any one crowding
  out another — a missing import can't burn the attempts meant for an actual algorithmic bug, and a
  self-inflicted oracle crash can't spend the budget a later real candidate/oracle dispute needs.
- `calls` — every model call made (role, model, tag, latency, usage).
- `token_usage` — summed prompt/completion tokens across all calls.
- `cost_usd` — summed `usage.cost` as reported by OpenRouter, or `null` if the profile/provider
  doesn't report cost (no local price table is maintained).
- `events` — the same lines as `log.txt`, one per phase transition and gate step.

The report never claims a solution is "verified" — only what was checked and whether it passed.

## Models, prices, cost cap

Configured per role in `config.toml` under `[profiles.openrouter.<role>]`. As of 2026-09-12:

| Role | Used by | Model | Max tokens | Concurrency |
|---|---|---|---:|---:|
| `strong` | SOLVE, REPAIR | `qwen/qwen3-coder` | 12000 | 3 |
| `fast` | ORACLE, STRESS | `deepseek/deepseek-chat-v3-0324` | 8000 | 3 |

**Both roles must be non-reasoning models**, which is why `extra` is empty rather than carrying a
`reasoning` key. Reasoning models were tried first and failed completely: every call hit its token
ceiling with 100% reasoning tokens and zero content, so no block was ever emitted and the raw
reasoning prose was written as the solution. Neither `reasoning.effort` nor `reasoning.max_tokens`
changed that. The prompts already ask for the analysis as parseable blocks, so a separate hidden
reasoning channel buys nothing here and costs the entire output budget.

Despite the names, the `fast` role is the slower of the two in practice — it writes three functions
per call against the `strong` role's one, and its latency tracks output size at roughly 34 tokens
per second. Its measured oracle latency across the ten samples was 38 s minimum, 73 s median,
128 s maximum, which is what sets `timeout_cap_s` below.

Prices are whatever OpenRouter bills for these models at call time; the report reads them back from
`usage.cost` on each response rather than keeping a local price table. OpenRouter includes `usage.cost`
on every response by default now — the request body needs no extra parameter for this (the older
`usage: {include: true}` flag is deprecated). The per-problem cost cap is
`[limits] max_cost_usd_per_problem = 0.10` — the repair loop checks cumulative cost before each
repair attempt and stops (emitting the best candidate so far) once the cap is reached.

The three generate-phase calls (SOLVE, ORACLE, STRESS) each get `[phases] generate_call_share` (default
`0.45`) of usable time — a real config knob, not a literal in `solve.py` — capped underneath by each
role's own `timeout_cap_s` (`fast` is `150.0`, raised from a stale `110.0` so the measured 128 s oracle
worst case is no longer clipped by the role cap; the share is what actually binds in practice). The
ORACLE call writes the most output of the three (it defines `reference`, `gen`, *and* `validate`), so
it's reliably the slowest and the one most likely to time out. If it comes back with no `===ORACLE===`
block at all, the orchestrator retries it exactly once, only if `budget.can_afford(130)` — comfortably
above the measured 128 s worst case — still holds; a retry this close to the deadline would just cost
more time for nothing. That's separate machinery from the adjudication regeneration described below,
which has its own independent one-shot counter — but it gets the same `generate_call_share`-derived
timeout as the initial ORACLE call (not a smaller hardcoded fraction), because it writes the same three
functions and is just as likely to need the full span.

## How the three README questions are answered

- **How traps are found and handled:** see architecture §7.1. In short, the SOLVE prompt carries an
  explicit trap checklist against the stated bounds, and the stress gate turns "would this be too
  slow" into an actual timed measurement rather than an opinion.
- **How a solution is verified without public examples:** see architecture §7.2. A literal Python
  oracle is generated independently of the candidate, and it also emits its own `validate` predicate
  that checks every generated or literal input against the statement's stated preconditions (distinct
  identifiers, must-currently-exist, ordering, and so on) before that input is used — an input
  `validate` rejects or raises on is dropped, so the differential never burns a case, or a repair
  attempt, chasing a candidate/oracle disagreement on input the statement itself forbids. A validator
  that rejects every input is distrusted (all inputs are kept, and the report says so), and an oracle
  with no `validate` at all behaves exactly as before this existed. This only discards inputs the
  statement calls invalid — it does not make the oracle's `reference` itself correct. That oracle is
  then used for seeded differential testing at small and medium magnitude plus literal edge cases,
  behavioral contract checks, and a max-size stress run — with the oracle itself subject to at most one
  adjudicated regeneration if the candidate and oracle disagree and the model says the oracle is wrong.
  For Rust, right after the
  stress run, the same max-size input is rerun on the overflow-checked build (gate kind `overflow`)
  to catch a silent i64 wrap that the timing-only stress binary (`overflow-checks=off`) can't see —
  this only proves the arithmetic didn't overflow, not that the output is correct at that scale,
  since the oracle can't produce an expected answer for a maximum-size input. Skipped for Python
  (arbitrary-precision integers, nothing to overflow), when there's no stress input, or out of budget.
  Before any of this runs, a fixed `import random, math, itertools, collections, string, heapq,
  bisect` line is prepended to the oracle/stress source, because the prompt tells the model to *use*
  `random.Random(seed)` but never explicitly says to import anything, and one live oracle shipped with
  no imports at all. For Rust, both `EDGES` entries and `gen_max`'s stdin are dedented per line before
  they reach the gate — the model writes them as indented Python triple-quoted literals, and that
  indentation is not part of the input a real judge would ever send.
- **What happens when time runs out:** see architecture §7.3. A monotonic budget divides the deadline
  into generate/gate/repair/settle/emit phases; whatever candidate ranks best is written before the
  deadline even if it never passed every gate, because a missing file scores zero and an unverified
  answer does not.

## Swapping models or adding another endpoint

Everything is config, not code. To change a model, edit its `model` key under
`[profiles.openrouter.strong]` or `[profiles.openrouter.fast]` in `config.toml`. To point at a
different OpenAI-compatible provider, add a new `[profiles.<name>.strong]` / `[profiles.<name>.fast]`
pair with that provider's `base_url` and `api_key_env`, then pass `--profile <name>` on the command
line. No source changes are needed for either case — `llm.py`'s client is provider-agnostic.

## Benchmark mode

```bash
uv run python solve.py --bench samples/ -o runs/bench
```

Runs every `*.json` in the sample directory and writes `runs/bench/benchmark.md`: one row per problem
with gate results (including the Rust-only `Overflow` column — `skip` for Python), the `Repairs` and
`Syntax Repairs` counts (semantic vs. mechanical, see above), call count, token usage, elapsed time,
cost, final status, and a `Hidden tests` column that always reads `unknown` (the grading harness is not
available to this tool).

**The benchmark has not been run in this environment** — no `OPENROUTER_API_KEY` was configured, so no
real model calls were made and no results table can be reported here. Running the command above with a
valid key populates `runs/bench/benchmark.md` with real per-sample evidence and cost figures.

## Limitations

(See architecture §14 for the full discussion.)

- Shared misreading: an independent oracle reduces, but does not eliminate, the chance that both the
  candidate and the oracle misread the same sentence in the statement. `validate` filters inputs the
  statement calls invalid; it cannot detect a `reference` that misreads the statement while staying
  internally consistent with its own `validate`.
- Judge time limits are unknown; the stress thresholds in `config.toml` are guesses recorded in the
  report, and a solution passing locally at 4s may fail a 2s judge.
- Large-magnitude correctness is still not verified: it is inferred from agreement at the medium
  differential tier, plus (Rust only) an overflow-checked rerun of the max-size stress input, which
  proves only the absence of an i64 overflow at that scale, never that the output is correct there —
  the oracle can't produce an expected answer for a maximum-size input. A bug that appears only above
  the medium tier and doesn't manifest as an overflow is not caught.
- When `gen_max` fails and the stress input degrades to the largest `medium` case (`detail.degraded`
  in the report), the timing check runs at medium scale, not true maximum scale — it can show a
  quadratic solution is already too slow, but it proves nothing about performance at the sizes
  `gen_max` was supposed to reach, and a solution that's fast at medium scale but not at true maximum
  scale will pass this check anyway.
- Process isolation only: generated code runs as the invoking user with resource limits, not inside a
  container.
- Model latency variance compresses the repair window on a slow strong-model response; the budget
  controller adapts but cannot create time.
- The benchmark in this repository has not been run against a real model (no API key in this
  environment), so `runs/bench/benchmark.md` does not yet exist here.
