# Benchmark Findings — live runs on `samples/`

Live OpenRouter runs of `solve.py` against all ten sample problems, one at a time, on 2026-09-12
between 20:51 and 21:03.

## Headline

| | |
|---|---|
| **Passed all gates** | **1 of 9** |
| Emitted unverified (no gate could run) | 6 of 9 |
| Emitted with known gate failures | 2 of 9 |
| **Deadline breaches** | **0** — slowest run 135.8 s of 285 s usable |
| Total cost, 9 runs | **$0.0882** (~$0.0098/problem, cap is $0.10) |

The deadline and cost machinery are not the problem. **Verification is.** Six of nine runs shipped
a solution with every differential, behavior and stress gate skipped, because the one call that
produces the ground truth timed out. The system reports this honestly as `emitted_unverified`
rather than claiming success, but the score is still at risk, and the run typically ended after
using only 21% of its time budget.

## Environment

| | |
|---|---|
| Models | `strong` = `qwen/qwen3-coder`, `fast` = `deepseek/deepseek-chat-v3-0324`, both non-reasoning (`extra = {}`) |
| Deadline | 300 s, `safety_margin_s = 15` → 285 s usable |
| Toolchain | CPython 3.11.13 via `uv`, rustc 1.98.1 |
| Command | `uv run python solve.py samples/<id>.json -o runs/out/<id>.<ext>` |

Connectivity was verified before the batch: both models answered a marker-block probe in under 3 s,
so no failure below is an auth or model-name problem.

### Two caveats on the data

1. **`config.toml` changed mid-batch.** At 21:00, `fast.timeout_cap_s` was raised 60 → 110 by
   concurrent work on the repo. Nine samples ran under 60, the tenth under 110. See F6 — the
   change made no difference, for a reason that matters.
2. **`runs/` was written by another process during the batch.** `1dea32802072` was re-run after my
   pass, so the aggregate table shows 111.6 s / 0 repairs while my diagnosis below records the
   65.7 s / 1 repair run I actually observed. Per-sample findings were captured at run time and
   describe what happened then.

## Results

| Problem | Lang | Status | Elapsed | Repairs | Cost | Cause |
|---|---|---|---:|---:|---:|---|
| `1ba0d34fae43` | python | **passed_all_gates** | 50.5 s | 0 | $0.0079 | — |
| `1c182498c9c7` | rust | emitted_with_failures | 85.9 s | 2 | $0.0176 | F1 |
| `1dea32802072` | python | emitted_unverified | 65.7 s | 1 | $0.0090 | F2, F3 |
| `2beff58fa923` | python | emitted_unverified | 60.1 s | 0 | $0.0080 | F4 |
| `4e49a099fd84` | rust | emitted_unverified | 60.4 s | 0 | $0.0074 | F4 |
| `5cb294c18288` | python | emitted_unverified | 60.1 s | 0 | $0.0054 | F4 (both fast calls) |
| `5ce30ef9e5cf` | rust | emitted_unverified | 60.5 s | 0 | $0.0092 | F4 |
| `5d02bd0e16ab` | rust | emitted_unverified | 60.4 s | 0 | $0.0073 | F4 |
| `6e43a08ec05a` | python | emitted_with_failures | 135.8 s | 2 | $0.0186 | F5 + a real bug |
| `6eca8a9120e0` | python | emitted_unverified | 85.6 s | 0 | $0.0077 | F4, F6 |

The only clean pass, `1ba0d34fae43`, was verified against 141 small + 11 medium + 9 edge cases and
a 7.74 s stress run — real evidence, not a vacuous pass.

## Measured call latency

Across every run, per role:

```
role     n   min   med   max   timeouts   observed
solve    6    10    16    18   0          10, 14, 16, 16, 18
oracle   6    37    56    60   2          37, 40, 56, 60✗, 60✗
stress   6    22    35    50   0          22, 28, 35, 43, 50
repair   4    11    18    20   0          11, 12, 18, 20
```

**The `fast` role is 3–4× slower than the `strong` role.** The role names are inverted in practice.
`strong` has a 120 s cap against an 18 s maximum — six times the headroom it needs — while `fast`
had a 60 s cap sitting exactly at the oracle's median.

---

## Findings

### F1 — Indented edge-case fixtures corrupt Rust stdin (Rust only)

**Seen in:** `1c182498c9c7` (rust) — `emitted_with_failures`, both repair calls consumed.

**Exact reason.** `prompts/stress.md` asks the `fast` model for a literal `EDGES` list. It writes
Python triple-quoted strings, indented to match the surrounding list:

```python
EDGES = [
    """1 1 1 4 0
    41
    1
    1 1 1""",
```

Every line after the first therefore carries four leading spaces. These strings are fed verbatim
to the Rust candidate as stdin. A line-oriented parser does `"    41".parse::<u64>()`, which
returns `Err`, and the program panics:

```
thread 'main' panicked at cand_4c719907b1942424.rs:122:31:
index out of bounds: the len is 2 but the index is 2
```

**Steps to reproduce.**
1. `uv run python solve.py samples/1c182498c9c7*.json -o runs/out/x.rs`
2. `cat runs/1c182498c9c7/oracle/edges/cand.py` — note the indentation inside each `"""` literal.
3. `runs/1c182498c9c7/report.json` → `gate_inputs.notes` reads
   `"edge: dropped 5 of 8 generated inputs as invalid (failed validate())"`.
4. `evidence.c1.diff_edge` fails with a panic on input `"1 1 1 4 0\n    41\n    1\n    1 1 1"`.

**Why it is expensive.** The failure is not a real defect — the judge never sends indented input.
But `diff_edge` runs first in `run_gate`, so the run burns both repair calls (`max_repairs = 2`)
hardening the candidate against whitespace, and never reaches the real algorithmic checks.
Two strong-model calls, ~$0.009, and the actual bug went unrepaired.

**Not applicable to Python.** Python edge cases are tuples parsed by `ast.literal_eval`
(`([1], [("ok", 1)], 0, 0, 1, 0, 0)`), so indentation is irrelevant. Confirmed on
`1ba0d34fae43`, where only 1 of 10 edges dropped versus 5 of 8 here.

**Fix.** Normalize per-line whitespace on Rust stdin fixtures where they enter the gate, in
`verify.prepare_gate_inputs`, and say so in `prompts/stress.md`:

```python
def _clean_stdin(problem, inputs):
    # Model-written EDGES are indented Python literals; their continuation lines inherit the
    # source indentation, which breaks line-oriented stdin parsers. The judge never does this.
    if problem.language != "rust":
        return inputs
    return ["\n".join(l.strip() for l in s.split("\n")) if isinstance(s, str) else s for s in inputs]
```

Apply to both `edges[0].output` and `gen_max`'s `stress_input`. Safe because the README
specifies Rust I/O as ASCII-whitespace-separated tokens.

---

### F2 — `gen_max` crashes on its own assertion, silently disabling the stress gate

**Seen in:** `1dea32802072` (python) — `emitted_unverified`.

**Exact reason.** The `fast` model's `gen_max` contains a self-check that its own generated
values violate:

```
File "runs/1dea32802072/oracle/genmax/cand.py", line 171, in <module>
    assert 0 <= left < right <= len(current_ids)
AssertionError
```

`prepare_gate_inputs` records the note and leaves `stress_input = None`, so `run_gate` emits
`stress: passed=True, skipped=True, {"skipped": "no stress input"}`.

**Steps to reproduce.**
1. `uv run python solve.py samples/1dea32802072*.json -o runs/out/x.py`
2. `report.json` → `gate_inputs.notes[0]` shows the AssertionError.
3. `evidence.c2.stress` → `skipped: "no stress input"`.

**Why it matters.** The README scores a correct-but-slow solution as 0. This run emitted a
solution whose speed was never measured. `status` correctly reads `emitted_unverified` rather
than `passed_all_gates`, so the system is honest about it — but the point is still at risk, and
nothing retries.

**Fix.** `gen_max` failure is the one generator failure worth a retry, because it is the only
source of the stress input and it is cheap to regenerate. Either:
- re-ask the `fast` model once with the traceback appended (mirroring `regenerate_oracle`), or
- fall back to the largest `medium` case as a stress input, marking the evidence
  `degraded` rather than `skipped`, so the timing signal is weak but non-zero.

The second is the lazier fix and needs no extra model call.

---

### F3 — The strong model deliberates inside the `===CODE===` block

**Seen in:** `1dea32802072` (python) — `c1` rejected by the static gate, costing one repair call.

**Exact reason.** `qwen/qwen3-coder` began an approach, changed its mind mid-function, and wrote
its reasoning as comments *inside the code*, leaving a `while` with no body:

```python
while old_pos < len(op[1]) + len(ids_list):  # This is wrong logic
# Actually, we should iterate through the old ids_list with the removal set
# Let's re-think:
...
# This is getting messy. Let's store the old list before removal for selection logic
```

`python_static` reports `syntax error: expected an indented block after 'while' statement on
line 68`.

**Not truncation.** `completion_tokens = 1592` against `max_tokens = 12000`, and the file ends
with a complete `return result`. The model had budget to spare; it simply thought out loud.

**Steps to reproduce.**
1. `uv run python solve.py samples/1dea32802072*.json -o runs/out/x.py`
2. `sed -n '64,80p' runs/1dea32802072/candidates/c1.py`
3. `report.json` → `evidence.c1.static.detail.problems[0]`.

**Root cause.** `config.toml` now sets `strong = qwen/qwen3-coder` with `extra = {}` — a
**non-reasoning** model. With no separate reasoning channel, deliberation has nowhere to go but
the visible output, and the prompt's `===CODE===` block is where it lands. The previous config
used `deepseek/deepseek-v4.1-flash`, a reasoning model whose thinking is returned in
`message.reasoning` and never reaches `parse_blocks`.

**Fix, in order of effect.**
1. Use a reasoning model for the `strong` role so deliberation is routed to `message.reasoning`.
   `llm.Reply.text` already prefers `content` and falls back to `reasoning`, so no code change.
2. If the non-reasoning model is kept for cost, add to `prompts/solve.md`: *"The `===CODE===`
   block must contain only the final program. Do all deliberation in `===RULES===` and
   `===TRAPS===`. Never include commentary, alternatives, or abandoned code inside `===CODE===`."*
3. Keep the static gate as the net — it worked exactly as designed here, converting a fatal
   emission into a one-repair recovery.
---

### F4 — ORACLE timeout collapses all verification (dominant failure: 6 of 9 runs)

**Seen in:** `2beff58fa923`, `4e49a099fd84`, `5cb294c18288`, `5ce30ef9e5cf`, `5d02bd0e16ab`,
`6eca8a9120e0` — every one `emitted_unverified`.

**Exact reason.** The ORACLE call exceeds its timeout and returns `error=timeout` with no text.
`parse_blocks` finds no `===ORACLE===` block, so `oracle_src` is empty, and
`prepare_gate_inputs` returns immediately on `if not oracle_src.strip()` (`verify.py:133`) with the
note `"no oracle source"`. `run_gate` then finds no cases, and **every** gate reports
`passed=True, skipped=True`:

```
oracle   err=timeout timed_out=True latency=60.0
...
[00060.0] oracle.done error=timeout
[00060.1] gate.diff_edge   passed=True cases=0      skipped: "no cases"
[00060.1] gate.diff_small  passed=True cases=0      skipped: "no cases"
[00060.1] gate.diff_medium passed=True cases=0      skipped: "no cases"
[00060.1] gate.behavior    passed=True cases=0      skipped: "no cases"
[00060.1] gate.stress      passed=True cases=0      skipped: "no stress input"
[00060.1] gate.passed
[00060.1] emit candidate=c1 status=emitted_unverified
```

Because no gate *failed*, `cand.all_passed()` is true and the repair loop never engages. The run
exits at 60.1 s having spent **21% of a 285 s budget**, with 225 seconds unused.

**Steps to reproduce.**
1. `uv run python solve.py samples/2beff58fa923*.json -o runs/out/x.py`
2. `report.json` → `calls[]` shows the entry with `tag: "oracle"`, `error: "timeout"`.
3. `report.json` → `gate_inputs.notes == ["no oracle source"]`.
4. Every entry in `evidence.c1` after `compile` has `"skipped": true`.
5. `elapsed_s` ≈ 60, against `deadline_s` 300.

**Root cause.** The oracle prompt asks for the largest output of any fast call — `reference()`
*and* `gen()` *and* `validate()` — so it is reliably the slowest, while `fast.timeout_cap_s` was
60 s, the measured median. Roughly half of all oracle calls were therefore expected to clip; six
of nine did. In `5cb294c18288` both fast calls clipped at once.

**Why it is the worst failure.** It is silent. No gate fails, no repair triggers, nothing looks
wrong in the log except one `error=timeout` line. The status field is honest, but a run that
verified nothing costs exactly as much as one that verified everything, and the solution ships.

**Fix, in order of value.**
1. **Retry ORACLE on timeout while budget allows.** It is the single point of failure for all
   verification, and a failed run leaves ~225 s unused. Guard with `budget.can_afford(120)` and a
   one-retry cap, mirroring `regenerate_oracle`'s structure. This alone converts most of these six
   runs into verified ones.
2. **Raise the generate-phase ceiling** — see F6; raising `timeout_cap_s` alone does nothing.
3. **Shrink the oracle's output.** Drop `validate()` from `prompts/oracle.md` and derive validity
   by letting `reference()` raise. Less output, proportionally less latency.
4. **Treat "no oracle source" as a gate failure, not a skip.** A run that verified nothing should
   not reach `gate.passed`. Making it a failure lets the existing repair machinery react.

---

### F5 — The generated oracle omits `import random`, so `gen()` produces nothing

**Seen in:** `6e43a08ec05a` — 0 small cases, 0 medium cases, verification reduced to 10 edge cases.

**Exact reason.** The oracle module the model wrote contains **no import statements at all**, yet
`gen()` calls `random.Random(seed)`:

```
File "runs/6e43a08ec05a/oracle/gen_small/cand.py", line 99, in gen
    rng = random.Random(seed)
          ^^^^^^
NameError: name 'random' is not defined
```

Both `gen(small)` and `gen(medium)` raise, so `inputs` is empty for both tiers and
`prepare_gate_inputs` records the note and moves on.

**Steps to reproduce.**
1. `uv run python solve.py samples/6e43a08ec05a*.json -o runs/out/x.py`
2. `grep -n "^import\|^from" runs/6e43a08ec05a/oracle/gen_small/cand.py` → no output.
3. `report.json` → `gate_inputs` shows `"small": 0, "medium": 0`, with the NameError in `notes`.

**Root cause — a missing instruction, not a contradictory one.** `prompts/oracle.md:5` says
*"define `gen(seed: int, mode: str)` using only `random.Random(seed)` for randomness"*, and line 22
warns *"never assign to an imported module name … bind the generator to a fresh name such as
`rng`."* The prompt refers to "an imported module name" but **never instructs the model to write
`import random`**. The model complied with every stated rule — used `random.Random(seed)`, bound it
to `rng` — and omitted an import nobody asked for.

**Fix.** A prompt sentence would help but is not reliable. Prepend a stdlib preamble to the oracle
source before executing it, in `verify.prepare_gate_inputs`:

```python
_ORACLE_PREAMBLE = "import random, math, itertools, collections, string, heapq, bisect\n"
```

A duplicate import costs nothing if the model already wrote one, and the prompt already forbids
rebinding those names. This eliminates the whole class rather than this one instance.

---

### F6 — Raising `fast.timeout_cap_s` cannot help: the phase fraction binds first

**Seen in:** `6eca8a9120e0`, the one sample that ran after `fast.timeout_cap_s` was raised 60 → 110.
Its oracle still timed out — at **85.5 s**, not 110 s.

**Exact reason.** `solve.py:144-146` passes a hardcoded phase cap into every generation call:

```python
f_solve  = ex.submit(run.chat, "strong", "solve",  0.30 * run.budget.usable_s, **pv)
f_oracle = ex.submit(run.chat, "fast",   "oracle", 0.30 * run.budget.usable_s, **pv)
f_stress = ex.submit(run.chat, "fast",   "stress", 0.30 * run.budget.usable_s, **pv)
```

and `Run.chat` (`solve.py:115`) takes the **minimum** of that and the role cap:

```python
timeout = self.budget.step_timeout(min(cap_s, self.llm.roles[role].timeout_cap_s), reserve_s=10.0)
```

With `usable_s = 285`, `0.30 × 285 = 85.5`. So `min(85.5, 110) = 85.5`. **Any `timeout_cap_s`
above 86 is dead configuration** — the phase fraction silently wins, and the config comment
("60 s was cutting it off") describes a fix that cannot take effect.

**Steps to reproduce.**
1. Set `fast.timeout_cap_s = 110` in `config.toml`.
2. `uv run python solve.py samples/6eca8a9120e0*.json -o runs/out/x.py`
3. `report.json` → the oracle call shows `latency_s: 85.5`, not 110.

**Fix.** Make the generation-phase share configurable and consistent with `[phases]`, instead of a
literal `0.30` in three places. `generate_until = 0.32` already exists in `config.toml` and is
ignored by these calls. Either derive the cap from it, or raise both together — and note that
the observed oracle needs more than 85.5 s, so the share itself must grow (0.32 → ~0.45 gives
~128 s) or the oracle's output must shrink (F4 fix 3).

---

## Recommended order of work

Ranked by points recovered per line of code.

1. **F4.1 — retry ORACLE on timeout.** Six of nine runs; ~225 s of unused budget each. Highest
   value change in the list.
2. **F6 — make the generate-phase share configurable and raise it.** Without this, F4's retry has
   nowhere to run and the existing config knob stays inert.
3. **F5 — oracle stdlib preamble.** One line, eliminates a whole failure class.
4. **F1 — dedent Rust stdin fixtures.** Four lines; recovers two wasted repair calls per affected
   Rust run.
5. **F4.4 — "no oracle source" becomes a gate failure, not a skip.** Makes silent
   non-verification loud, and lets existing machinery react.
6. **F3 — a reasoning model for `strong`, or an explicit "code block only" instruction.**
7. **F2 — fall back to the largest medium case as a stress input** when `gen_max` fails.

Nothing here touches the budget controller: **no run came close to the deadline.** The slowest was
135.8 s against 285 s usable. The deadline design is working; the verification chain is what
needs attention.
