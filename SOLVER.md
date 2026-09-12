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
`config.toml` profile; `--run-dir` overrides where artifacts land (default `runs/<problem_id[:12]>/`).

## Where reports land, and how to read `report.json`

Each run writes `<run_dir>/report.json`, `<run_dir>/log.txt`, and `<run_dir>/candidates/c*.{py,rs}`.
The solution file itself goes wherever `-o` points.

In `report.json`:

- `status` — one of `passed_all_gates`, `emitted_unverified`, `emitted_with_failures`, `no_candidate`.
- `final_candidate` — the id (`c1`, `c2`, ...) of the emitted candidate, or `null`.
- `evidence` — a dict of candidate id to a list of gate results (`kind`, `passed`, `cases`, `duration_s`,
  `detail`, `skipped`). Kinds, in gate order: `compile`, `diff_edge`, `diff_small`, `diff_medium`,
  `behavior`, `stress`, `overflow` (Rust only — see below). `skipped: true` means the gate had
  nothing to run against (e.g. the oracle produced no cases), not that it passed for real — that's
  why `passed_all_gates` requires no evidence entry to be skipped, with one deliberate exception:
  a Python candidate's `overflow` entry is always `skipped: true` (Python integers are arbitrary
  precision, so the check is definitionally not applicable, not a coverage gap) and does not by
  itself block `passed_all_gates`. A Rust candidate's `overflow` entry being skipped (no stress
  input, or no budget left) is a real gap and still blocks it, same as any other skipped step.
- `repairs`, `oracle_regenerated` — how many repair attempts and oracle regenerations were used.
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
| `strong` | SOLVE, REPAIR | `deepseek/deepseek-v4.1-flash` | 16000 | 3 |
| `fast` | ORACLE, STRESS | `qwen/qwen3.7-flash` | 6000 | 3 |

Prices are whatever OpenRouter bills for these models at call time; the report reads them back from
`usage.cost` on each response rather than keeping a local price table. OpenRouter includes `usage.cost`
on every response by default now — the request body needs no extra parameter for this (the older
`usage: {include: true}` flag is deprecated). The per-problem cost cap is
`[limits] max_cost_usd_per_problem = 0.10` — the repair loop checks cumulative cost before each
repair attempt and stops (emitting the best candidate so far) once the cap is reached.

## How the three README questions are answered

- **How traps are found and handled:** see architecture §7.1. In short, the SOLVE prompt carries an
  explicit trap checklist against the stated bounds, and the stress gate turns "would this be too
  slow" into an actual timed measurement rather than an opinion.
- **How a solution is verified without public examples:** see architecture §7.2. A literal Python
  oracle is generated independently of the candidate, then used for seeded differential testing at
  small and medium magnitude plus literal edge cases, behavioral contract checks, and a max-size
  stress run — with the oracle itself subject to at most one adjudicated regeneration if the
  candidate and oracle disagree and the model says the oracle is wrong. For Rust, right after the
  stress run, the same max-size input is rerun on the overflow-checked build (gate kind `overflow`)
  to catch a silent i64 wrap that the timing-only stress binary (`overflow-checks=off`) can't see —
  this only proves the arithmetic didn't overflow, not that the output is correct at that scale,
  since the oracle can't produce an expected answer for a maximum-size input. Skipped for Python
  (arbitrary-precision integers, nothing to overflow), when there's no stress input, or out of budget.
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
with gate results (including the Rust-only `Overflow` column — `skip` for Python), repair count, call
count, token usage, elapsed time, cost, final status, and a `Hidden tests` column that always reads
`unknown` (the grading harness is not available to this tool).

**The benchmark has not been run in this environment** — no `OPENROUTER_API_KEY` was configured, so no
real model calls were made and no results table can be reported here. Running the command above with a
valid key populates `runs/bench/benchmark.md` with real per-sample evidence and cost figures.

## Limitations

(See architecture §14 for the full discussion.)

- Shared misreading: an independent oracle reduces, but does not eliminate, the chance that both the
  candidate and the oracle misread the same sentence in the statement.
- Judge time limits are unknown; the stress thresholds in `config.toml` are guesses recorded in the
  report, and a solution passing locally at 4s may fail a 2s judge.
- Large-magnitude correctness is still not verified: it is inferred from agreement at the medium
  differential tier, plus (Rust only) an overflow-checked rerun of the max-size stress input, which
  proves only the absence of an i64 overflow at that scale, never that the output is correct there —
  the oracle can't produce an expected answer for a maximum-size input. A bug that appears only above
  the medium tier and doesn't manifest as an overflow is not caught.
- Process isolation only: generated code runs as the invoking user with resource limits, not inside a
  container.
- Model latency variance compresses the repair window on a slow strong-model response; the budget
  controller adapts but cannot create time.
- The benchmark in this repository has not been run against a real model (no API key in this
  environment), so `runs/bench/benchmark.md` does not yet exist here.
