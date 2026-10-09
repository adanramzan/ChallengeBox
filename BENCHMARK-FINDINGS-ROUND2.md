# Benchmark Findings — Round 2

Second live pass over all ten samples, 2026-09-13 01:34–01:58, after the round-1 fixes landed
(commits `81d333f`, `8bbcd9b`, `be6fbf8`). Companion to `BENCHMARK-FINDINGS.md`.

## Headline: verification now actually runs

| | Round 1 | Round 2 |
|---|---:|---:|
| passed_all_gates | 1 | **2** |
| emitted_with_failures | 2 | 8 |
| **emitted_unverified (nothing checked)** | **6** | **0** |
| Deadline breaches | 0 | 0 |
| Max elapsed | 135.8 s | 238.6 s (of 285 s) |
| Total cost | $0.0882 | $0.1649 |

**The round-1 fixes worked.** Zero runs now ship unverified — every run produced real cases and
real evidence. The jump in `emitted_with_failures` is not a regression: those runs were previously
*unverified*, so their bugs were invisible. Round 2 is the first honest measurement of solution
quality, and it says **8 of 10 candidate solutions are wrong**.

Cost and time roughly doubled, which is the price of actually doing the verification work. Both
remain inside budget.

## Results (from each run's own stdout — see Data integrity)

| Problem | Lang | Round 1 | Round 2 | Elapsed | Repairs | Cost |
|---|---|---|---|---:|---:|---:|
| `1ba0d34fae43` | python | passed_all_gates | **passed_all_gates** | 70.8 s | 1 | $0.0088 |
| `1c182498c9c7` | rust | with_failures | with_failures | 80.0 s | 2 | $0.0154 |
| `1dea32802072` | python | unverified | **passed_all_gates** | 47.5 s | 1 | $0.0104 |
| `2beff58fa923` | python | unverified | with_failures | 176.2 s | 2 | $0.0207 |
| `4e49a099fd84` | rust | unverified | with_failures | 139.2 s | 2 | $0.0250 |
| `5cb294c18288` | python | unverified | with_failures | 238.6 s | 0 | $0.0125 |
| `5ce30ef9e5cf` | rust | unverified | with_failures | 165.5 s | 2 | $0.0162 |
| `5d02bd0e16ab` | rust | unverified | with_failures | 155.3 s | 2 | $0.0178 |
| `6e43a08ec05a` | python | with_failures | with_failures | 84.1 s | 2 | $0.0189 |
| `6eca8a9120e0` | python | unverified | with_failures | 235.6 s | 1 | $0.0192 |

## Round-1 findings: status

| | Finding | Status | Evidence |
|---|---|---|---|
| F1 | Indented Rust stdin fixtures | **FIXED** | fixture is now `"1 1 1 4 0\n41\n1\n1 1 1"`, previously `"\n    41"` |
| F2 | `gen_max` crash disables stress | **FIXED** | stress falls back with an explicit `degraded` marker |
| F3 | Model deliberates inside `===CODE===` | **not observed** | no syntax-error candidate in round 2 |
| F4 | Oracle timeout kills all verification | **FIXED** | 0 unverified runs; oracle now succeeds at 95–145 s |
| F5 | Oracle omits `import random` | **FIXED** | `6e43a08ec05a` went 0→109 small cases, notes empty |
| F6 | Phase fraction overrides role cap | **FIXED** | `generate_call_share = 0.55` is now config-driven |

### Correction to round 1's latency analysis

Round 1 reported oracle latency as "max 60 s". That data was **censored** — the 60 s cap was
truncating the distribution, not measuring it. The true spread, now observable:

```
oracle latencies (s): 31, 43, 44, 53, 58, 66, 72, 78, 95, 116, 135, 145
```

Median ~72 s, max 145 s. Round 1's recommendation to raise the cap to ~86 s would have been
**insufficient**. `generate_call_share = 0.55` (≈157 s) was the correct fix.

---

## New findings

### R2-1 — `oracle_unusable` misses the case that matters most

**Seen in:** `4e49a099fd84` (rust) — 0 small, 0 medium, 1 edge. `oracle_selfrepaired: 0`.

**Exact reason.** The oracle hallucinated a stdlib API that does not exist:

```
File ".../oracle/gen_small/cand.py", line 259, in gen
    G = len(list(unicodedata.grapheme_clusters(S))) if S else 0
AttributeError: module 'unicodedata' has no attribute 'grapheme_clusters'
```

Both generated tiers produced nothing. But the self-repair guard did not fire:

```python
verify.py:233   return not gi.cases_small and not gi.cases_medium and not gi.cases_edge
```

A single surviving edge case makes this `False`, so the oracle is judged "usable" on the strength
of one literal fixture, while both *generated* tiers are empty. The run proceeded with essentially
no differential coverage and spent four repair calls ($0.0250, the most expensive run of either
round) producing only regressions.

**Steps to reproduce.**
1. `uv run python solve.py samples/4e49a099fd84*.json -o runs/out/x.rs`
2. `report.json` → `gate_inputs` shows `"small": 0, "medium": 0, "edge": 1`.
3. `report.json` → `oracle_selfrepaired: 0` despite the AttributeError in `notes`.

**Fix.** The threshold should measure *generated* coverage, not any case at all:

```python
def oracle_unusable(gi) -> bool:
    # Edge cases are literals from STRESS; they say nothing about whether the oracle's own
    # gen()/reference() work. Judge usability on the generated tiers alone.
    return not gi.cases_small and not gi.cases_medium
```

Optionally also treat "usable but far below `cases_small`" as degraded.

---

### R2-2 — A crashed candidate is reported as a near-miss diff, misleading the repair call

**Seen in:** `5ce30ef9e5cf` (rust), and consistent with `1c182498c9c7`'s three no-progress repairs.

**Exact reason.** When a Rust candidate panics, `run_rust_cases` sets `ok=False` but still stores
whatever reached stdout. `same()` correctly returns False on `not actual.ok` (`verify.py:93`), but
the evidence detail then reads:

```
expected: 'I 42'
actual  : 'I 42\n'
error   : thread 'main' panicked at cand_1483d38f6f10d223.rs:105:33:
          called `Option::unwrap()` on a `None` value
```

The headline reads as a trailing-newline mismatch. It is not — under the README's
ASCII-whitespace token comparison those two strings are identical. The real cause is the panic,
buried in a truncated `error` field.

**Why it costs points.** `repair()` passes `_fmt(detail.get("actual"))` into the prompt. A repair
model shown `expected 'I 42'` / `actual 'I 42\n'` will hunt for a formatting bug that does not
exist. `5ce30ef9e5cf` burned both repairs without changing the outcome; `1c182498c9c7` produced
byte-identical output three times running.

**Steps to reproduce.**
1. `uv run python solve.py samples/5ce30ef9e5cf*.json -o runs/out/x.rs`
2. `report.json` → `evidence.c3.diff_edge.detail` — compare `expected`/`actual` against `error`.

**Fix.** When the process did not exit cleanly, do not present partial stdout as the answer:

```python
actual = r.output if r.ok else f"(process crashed, exit != 0) {r.error.strip().splitlines()[-2:]}"
```

so the repair prompt starts from the crash rather than a phantom formatting diff.

---

### R2-3 — `degraded` evidence still reports `passed_all_gates`

**Seen in:** `1dea32802072` — stress ran against "the largest medium case, not a true max-size
input", and the run still reported `passed_all_gates` and exit code 0.

**Exact reason.** The F2 fallback correctly records degradation in the evidence detail and in
`gi.notes`, but the status computation never consults it:

```python
solve.py:248   skipped = any(e.skipped for e in best.evidence if not (e.kind == "overflow" and ...))
solve.py:249   status = ("passed_all_gates" if best.all_passed() and not skipped ...
```

`degraded` appears nowhere in that expression.

**Why it matters.** Architecture §7.2 states the report "never says 'verified'; it says what was
checked." A run whose max-size timing was never measured claiming the strongest available status —
and exit code 0 — breaks that contract. Correct-but-slow scores zero, so this is exactly the
property that must not be overstated.

**Steps to reproduce.**
1. `uv run python solve.py samples/1dea32802072*.json -o runs/out/x.py`
2. `report.json` → `evidence.c2.stress.detail.degraded` is present, `status` is `passed_all_gates`.

**Fix.** One clause — fold degradation into the demotion test:

```python
weak = any(e.skipped or e.detail.get("degraded") for e in best.evidence if not (...))
```

so the run reports `emitted_unverified` (exit 1) instead.

---

### R2-4 — `prepare_gate_inputs` can consume the entire repair window

**Seen in:** `5cb294c18288` — 238.6 s elapsed (84% of budget), `repairs: 0`, despite `diff_small`
failing with a real defect.

**Exact reason.** The oracle took 95 s, then the literal `reference()` timed out on *every* medium
input:

```
"reference failed on all medium inputs: ['timeout', 'timeout', 'timeout']"
```

Each timeout burned its full grant and produced nothing. By the time `diff_small` failed, the
repair guard (`repairs >= max_repairs or phase() not in (...) or not can_afford(60)`) refused to
start a repair. The run detected a genuine bug and had no budget left to fix it.

This is the phase-starvation problem predicted in the Codex audit, now confirmed live. Round 1
never exposed it because oracle timeouts ended those runs at 60 s.

**Steps to reproduce.**
1. `uv run python solve.py samples/5cb294c18288*.json -o runs/out/x.py`
2. `report.json` → `gate_inputs.notes` shows the three medium timeouts; `elapsed_s` ≈ 238;
   `repairs` is 0 while `evidence.c1.diff_small.passed` is false.

**Fix.**
1. **Abandon a tier after its first `reference()` timeout.** A literal oracle that cannot finish
   one medium input will not finish the next twenty; three timeouts is three wasted grants.
2. **Give `prepare_gate_inputs` a hard phase ceiling** derived from `[phases].gate_until`, so gate
   preparation cannot cross into the repair window regardless of how slow the oracle is.

---

### R2-5 — Adjudication fires unreliably because it depends solely on the model's verdict

**Seen in:** `5d02bd0e16ab` fired it (`oracle_regenerated: 1`); `1c182498c9c7` did not, despite
three candidates producing byte-identical output against one oracle answer.

**Exact reason.** `regenerate_oracle` runs only when `repair()` returns `verdict == "oracle"`.
Commit `be6fbf8` addressed the bias by rewording `prompts/repair.md`, but the trigger is still a
single model judgement with no structural backstop.

**The unused signal.** When repair *N* produces output byte-identical to repair *N−1* on the same
failing case, the candidate is stable under repair pressure — evidence that the oracle is the
wrong side. The system has this information and discards it.

**Steps to reproduce.**
1. `uv run python solve.py samples/1c182498c9c7*.json -o runs/out/x.rs`
2. `report.json` → `evidence.c1/c2/c3.diff_edge.detail.actual` are identical
   (`"1 1 1 1 1 1 END\n"`), `oracle_regenerated` is 0.

**Fix.** Escalate structurally: if a repair produces no change in the failing case's output, stop
trusting the verdict and regenerate the oracle once, independent of what the model said.

---

### R2-6 — Reported `repairs` undercounts actual model spend

**Seen in:** `4e49a099fd84` — `repairs: 2` in the report, but **four** `repair` calls in `calls[]`
and five candidates (c1–c5).

**Exact reason.** `max_syntax_repairs = 2` is a separate budget from `max_repairs = 2`
(`config.toml`), and only the latter increments the reported counter. The report's `repairs` field
therefore understates model usage by up to 2 calls, which matters against
`max_cost_usd_per_problem`.

**Fix.** Report both counters, or have `repairs` reflect total repair calls with a breakdown in
the detail.

---

## Data integrity

`runs/` is being written by concurrent work on this repo. `runs/1dea32802072/report.json` was
overwritten seconds after my batch's run by a different execution (mine: 47.5 s, c2,
`passed_all_gates`; the file on disk: 76.2 s, c4, `emitted_with_failures`).

**The results table above is taken from each run's own stdout, captured in the batch log**, which
cannot be clobbered by another process. Per-sample diagnoses quote `report.json` as read at the
time of each run. If this benchmark is repeated, give it a private output directory
(`--run-dir`) so nothing else can write into it.

Unlike round 1, no source file changed during the batch (latest source edit 00:06, batch started
01:34), so round 2 is otherwise a clean controlled measurement.

---

## Recommended order of work

1. **R2-1 — fix the `oracle_unusable` threshold.** One line; the self-repair machinery already
   exists and simply is not reached in the case it was built for.
2. **R2-2 — stop presenting crashed output as a near-miss diff.** Cheap, and it is plausibly the
   reason several repair sequences made no progress at all.
3. **R2-4 — phase ceiling on gate preparation + abandon a tier after the first timeout.**
   Recovers repair capacity on slow-oracle runs.
4. **R2-3 — `degraded` must demote the status.** Correctness-of-reporting, one clause.
5. **R2-5 — structural adjudication escalation on no-progress repairs.**
6. **R2-6 — report total repair calls.**

The deadline controller remains untested against its limit: the slowest run was 238.6 s of 285 s.
That is now close enough that R2-4 matters for safety, not just for quality.
