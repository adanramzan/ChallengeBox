# How to use this project

This repository contains an AI solver for the JSON challenge problems in `samples/`.
The solver generates a candidate, checks it locally, and writes the best available
solution before the problem deadline.

## Prerequisites and setup

- [uv](https://docs.astral.sh/uv/) and CPython 3.11 (the project pins this through
  `pyproject.toml`; use `uv run`, not the system `python3`).
- `rustc` on `PATH` for Rust problems and Rust-related tests. Check it with:

  ```bash
  rustc --version
  ```

  If it is missing, install it with rustup and load Cargo's environment:

  ```bash
  curl https://sh.rustup.rs -sSf | sh
  source ~/.cargo/env
  ```

From the repository root:

```bash
uv sync --group dev
```

## API keys and profiles

Choose one configured provider and export its key:

```bash
export OPENROUTER_API_KEY=...
# or:
export ANTHROPIC_API_KEY=...
# or:
export OPENAI_API_KEY=...
```

The available `config.toml` profiles are `openrouter`, `anthropic`, and `openai`.
Without `--profile`, the CLI selects the first profile whose required role keys are
set; with multiple keys set, the current config therefore selects `openrouter` first.
Select one explicitly when needed:

```bash
uv run python solve.py --profile openai samples/1ba0d34fae43f1d26c14f3091598291f0ca9d6ab3f88232d15a3322c017042c3.json -o runs/out.py
```

Use `--config path/to/config.toml` to load a different configuration file.

## Solve one problem

The exact sample below is a Python problem:

```bash
uv run python solve.py samples/1ba0d34fae43f1d26c14f3091598291f0ca9d6ab3f88232d15a3322c017042c3.json -o runs/out.py
```

For a Rust problem, use a `.rs` output path; this is an exact Rust sample:

```bash
uv run python solve.py samples/5d02bd0e16ab63c540232452b60e8e6cf36d01e0820cb6b422b63c1a31091718.json -o runs/out.rs
```

Python output is a source file defining the JSON problem's `entrypoint` function,
using only the standard library and no I/O. Rust output is one complete `fn main()`
program that reads stdin and writes stdout, also using only the standard library.

Useful options are `--deadline-scale 0.25` for a shortened test budget,
`--run-dir runs/my-run` for a fixed artifact directory, `--profile NAME`, and
`--config PATH`.

## Output and status

The solution is written exactly where `-o` points. By default, each run gets a new
directory named `runs/<problem_id[:12]>-<YYYYmmdd-HHMMSS>/`; `--run-dir` overrides it.
That directory contains:

- `report.json`: status, selected candidate, gate evidence, model calls, token usage,
  cost, and event history.
- `log.txt`: timestamped phase, model-call, gate, repair, and emit events.
- `candidates/c1.py` or `candidates/c1.rs` (and later candidates): every parsed
  candidate produced during the run.
- `replies/<tag>-<n>.prompt.txt` and `replies/<tag>-<n>.txt`: the raw prompt and raw
  model reply for every call, including salvaged partial replies.
- `oracle/`: generated oracle, case-generator, validation, and stress fixtures.
- `c1/`, `c2/`, and similar candidate work directories: sandbox inputs, harnesses,
  compilation products, and gate artifacts used to evaluate each candidate.

`report.json` uses these statuses: `passed_all_gates`, `emitted_unverified`,
`emitted_with_failures`, or `no_candidate`. The process exit codes are:

| Code | Meaning |
|---:|---|
| `0` | A candidate passed all applicable local gates. |
| `1` | A solution was emitted but it was unverified or failed one or more gates. |
| `2` | Invalid problem/sample-directory input or a required API key is missing. |
| `3` | No candidate was produced. |

An emitted solution is not proof that it passes hidden tests; inspect `report.json`.

## Benchmark all samples

```bash
uv run python solve.py --bench samples/ -o runs/bench
```

This requires a configured API key, processes every `*.json` in `samples/`, and
writes `runs/bench/benchmark.md`. It contains one row per problem with language,
compile/example/public/edge/small/medium/behavior/stress/overflow gate results,
repair counts, calls, token usage, elapsed time, cost, final status, and
`Hidden tests` (always `unknown`). Per-problem solution files are written beside
the report as `runs/bench/<problem_id[:12]>.py` or `.rs`; detailed run artifacts are
under `runs/bench/<problem_id[:12]>/`.

## Tests

Run the full local test suite with:

```bash
uv run --group dev pytest -q
```

## Common failures

- `missing API key`: export the key matching the selected profile, or pass the
  matching `--profile` explicitly.
- `invalid problem`: check that the path is a readable JSON file with the required
  problem fields (`problem_id`, `language`, `statement`, `entrypoint`, and
  `deadline_s`).
- `rustc` errors: install rustup, run `source ~/.cargo/env`, and retry; Rust
  candidates must compile before behavioral gates can run.
- `no_candidate` or a model timeout/refusal: inspect `log.txt` and `replies/`, then
  retry with a fresh `--run-dir` or another configured profile. A timed-out run may
  still have a salvaged candidate, so check `report.json` before discarding it.
- `emitted_unverified`: the solver emitted code but could not complete a real
  verification path, commonly because the oracle produced no usable cases or the
  budget expired. Treat it as unverified, not as a pass.

For the detailed operational behavior, see [`SOLVER.md`](SOLVER.md). For the design
and verification rationale, see
[`ChallengeBox-Agent-Architecture.md`](ChallengeBox-Agent-Architecture.md). The
latest benchmark findings are in
[`docs/reports/round17-analysis.html`](docs/reports/round17-analysis.html). The
original Claude development plan is in
[`docs/superpowers/plans/2026-09-12-challengebox-solver.md`](docs/superpowers/plans/2026-09-12-challengebox-solver.md).
The LLM prompt templates are in [`prompts/`](prompts/).

---

# AI Challenge Problem Solver

## Summary

This project tests your ability to build an AI system that solves algorithmic challenge problems.

Each problem comes as a JSON file with a statement, a target language, and an entrypoint.
Your system must read the problem, produce a working solution, and return it before the deadline.

The problems are hard on purpose.
Most of them hide traps, such as huge numeric bounds, structures that cannot be fully built in memory, or small wording details that change the answer.
A single "write the code" prompt will usually fail.

You should consider cost optimization in architecture.

## Input Format

Each problem is a JSON file like this:

```json
{
  "problem_id": "1ba0d34f...",
  "language": "python",
  "statement": "Simulate a congestion-aware ...",
  "entrypoint": "simulate_writes",
  "public_examples": [],
  "deadline_s": 300.0
}
```

| Field | Meaning |
|-------|---------|
| `problem_id` | Unique ID for the problem |
| `language` | `python` or `rust` |
| `statement` | The full problem description |
| `entrypoint` | The function name for Python, or `main` for Rust |
| `public_examples` | Sample test cases (may be empty) |
| `deadline_s` | Time limit in seconds for producing a solution |

Solution requirements by language:

* **Python:** Define the function named in `entrypoint`. Use only the standard library. No I/O.
* **Rust:** Write one complete program with `fn main()`. Read from stdin and write to stdout. Use only the standard library.

Sample problems are included in the `problems/` folder of this repo.

## Rules

Each problem is scored as 0 or 1.

* **1 point:** The solution is returned within the deadline and passes all hidden test cases.
* **0 points:** The solution is late, fails to run, or gets any hidden test case wrong.

Hidden test cases include edge cases and maximum-size inputs, so the solution must be both correct and fast.

## What We Want From You

Build a system that uses AI to solve these problems reliably and avoids their traps.

We accept either of the following:

1. **Running code (preferred):** A working tool that takes a problem JSON file and outputs a solution file within the deadline.
2. **AI architecture:** A clear design document explaining how your system works, how it checks correctness without public examples, and how it stays within the time limit.

Whichever you choose, please explain:

* How your system finds and handles the traps in each problem.
* How it verifies a solution before submitting it.
* What it does when it is running out of time.
