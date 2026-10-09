# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A tool that turns an algorithmic-challenge JSON file into a verified solution file before the problem's deadline:
`uv run python solve.py <problem.json> -o <solution.py|.rs>`. Each problem scores 0 or 1 — late, uncompilable,
wrong, or too slow all score 0 — so the whole design is about *manufacturing evidence* of correctness when no
public examples exist.

Two documents are the spec and must be kept in sync with the code:

- `ChallengeBox-Agent-Architecture.md` — the design (pipeline, gates, budget, cost, limitations). Authoritative.
- `docs/superpowers/plans/2026-09-12-challengebox-solver.md` — the task-by-task implementation plan with exact
  interfaces per module. Read the relevant task's **Produces/Consumes** block before writing a new module.
- `.superpowers/sdd/2026-09-12-challengebox-solver/progress.md` — execution ledger: which tasks are done, and the
  *rulings* on every review finding (why a deviation from the plan was accepted). Gitignored but load-bearing.

`README.md` is the assessment brief (it says `problems/`; the actual sample dir is `samples/`).

## Commands

```bash
uv run --group dev pytest -q                       # full suite
uv run --group dev pytest tests/test_sandbox.py -q -k rust   # one file / one pattern
source ~/.cargo/env                                # needed before any rustc test in a fresh shell
uv run python solve.py samples/<id>.json -o runs/out.py      # single problem (once solve.py exists)
uv run python solve.py --bench samples/ -o runs/bench        # all ten, writes benchmark.md
```

Always `uv run` (CPython 3.11.13). System `python3` is 3.9 and must never be used — for the orchestrator, the
sandbox harnesses, or generated candidates.

## Architecture

One strong-model SOLVE call (analysis + traps + algorithm + code) runs **concurrently** with two fast-model calls:
ORACLE (a deliberately literal Python `reference()` + seeded `gen()` for small/medium inputs) and STRESS
(`gen_max()` + literal edge cases). A deterministic, token-free **gate** then decides pass/fail: static contract →
compile → edge cases → differential small → differential medium → behavior (mutation/global state/nondeterminism) →
max-size stress under a wall clock. Failures are shrunk to a minimal counterexample and fed to a bounded REPAIR loop
(≤2), which may also blame the *oracle*, triggering at most one oracle regeneration. A monotonic `Budget` maps
`deadline_s` onto phase fractions (`config.toml [phases]`) and hands every model call and subprocess a
`step_timeout()`; emission always happens before `deadline − safety_margin`.

Invariants that hold across modules:

- **The oracle is the ground truth**, and it is always Python — even for Rust problems — so oracle and candidate
  cannot share an overflow bug or a language idiom. It is generated in its own context and never sees the candidate.
- **A model saying "correct" is not evidence.** Only compilation, contract checks, differential agreement, and a
  timed stress run count. Reports say what was checked, never "verified".
- **Model output is never trusted as code** until it passes `python_static` / `rust_static`, and never runs in the
  orchestrator process — every execution goes through `sandbox.run_cmd` (`start_new_session=True`, clean env,
  process-group SIGKILL on timeout, capped output).
- **Runtime dependencies: none.** Stdlib only (`urllib`, `tomllib`, `subprocess`, `ast`, `resource`, `threading`).
  `pytest` is the sole dev dependency. Adding a runtime dep needs a very good reason.

Module layout is fixed at five source files: `solve.py` (CLI, orchestrator, budget, finalizer), `llm.py` (one
OpenAI-compatible client, marker parsing, `FakeLLM`), `sandbox.py` (runners + static checks), `verify.py`
(differential, shrink, behavior, stress), `prompts/{solve,oracle,stress,repair}.md`. Don't add files or layers
beyond these without cause.

### Model calls

Models are pure config: `[profiles.<name>.<role>]` in `config.toml`, roles `strong` and `fast`, over an
OpenAI-compatible HTTP API. **Hosted providers only — do not add local-model support** (LM Studio /
Ollama / llama.cpp). Local models were measured and rejected: 7–16 tok/s with ~20 s prefill and no concurrency,
which fits about two calls in a 300 s deadline and breaks the three-concurrent-calls design. `config.toml` ships
`openrouter`, `anthropic` and `openai` profiles; `solve.py` uses the first whose API key is set (`--profile`
forces one). Provider differences live in role config (`token_param`, `omit_temperature`, `price_in_per_m` /
`price_out_per_m`), never in branches in `llm.py`. (The `127.0.0.1` `ThreadingHTTPServer` in `tests/test_llm.py` is a stub endpoint for offline
tests, not local inference.) JSON-schema
`response_format` was measured unreliable with thinking models — prompts use `===NAME===` … `===END===` marker
blocks parsed by `llm.parse_blocks`, which falls back to the reasoning field when `content` is empty. Tests use
`FakeLLM` and never touch the network; `FakeLLM` scripts are keyed by `tag` (prompt name) first because the three
generation calls complete in nondeterministic order.

### Sandbox details

`run_python_cases` writes `repr(args_tuple)` lines into `cases.txt` and a trusted harness reads them with
`ast.literal_eval`, printing one `repr(dict)` result per line — so every fixture in a run dir is readable and
re-runnable by hand. Keep both sides literal-eval-safe; a result that can't round-trip becomes an error record
rather than silently shifting subsequent results. Rust: compile once per candidate hash,
`-O -C overflow-checks=on` for the gate, off for stress timing; output compared as `str.split()` token lists.

## Working rules

- **All coding is done by a subagent running the Opus model** — implementation, tests, prompt edits, config changes.
  The main session writes the brief, reviews the diff, runs benchmarks and adjudicates; it does not edit source files
  itself. Dispatch with `model: "opus"` explicitly. Batch small same-shape fixes into one brief rather than one
  subagent per line.
- **Commit after every task**, one conventional-commit message per task. This is what gives the task reviewer its
  `BASE..HEAD` diff and the ledger its recovery map — don't batch several tasks into one commit.
- **Never `git push`.** Commits stay local; the user decides what leaves the machine. History is rewritable until
  then, so tasks can be squashed later if wanted.
- **No attribution trailers, ever:** no `Co-Authored-By`, no `Claude-Session`, no "Generated with Claude Code".
  Message body only.
- Never commit `runs/` or API keys.
- When a review finding is resolved by a judgment call rather than a code change, record the ruling (and the cost if
  it's wrong) in the progress ledger, as the existing entries do.
