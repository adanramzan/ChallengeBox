# ChallengeBox AI Solver — Architecture

**See [`SOLVER.md`](SOLVER.md) for setup and usage.** This document is the design rationale.

**Status:** implemented and test-covered; live-model quality remains provider/model dependent
**Deliverable:** a runnable tool: `python solve.py <problem.json> -o <solution file>`
**Scope:** one week, one developer

---

## 1. What is being scored

Each problem is worth 0 or 1. A point requires all of the following at once:

- solution file emitted before `deadline_s`;
- passes every hidden test, including edge cases and maximum-size inputs;
- obeys the language contract (Python: named function, stdlib only, no I/O; Rust: `fn main()`, stdin/stdout, stdlib only).

Consequences that shape the design:

- A compiling but wrong solution is worth the same as no solution. "Best effort" fallback is a consolation, not a feature.
- A correct but slow solution is worth nothing. Performance must be tested, not estimated.
- No public examples are guaranteed. All ten samples have none. The system must manufacture its own ground truth.
- Cost matters. The README asks for cost optimization in the architecture, so the design states a per-problem budget.

---

## 2. What the samples say

The ten problems in `samples/` are the specification. Reading them yields a consistent profile.

| Property | Observed |
|---|---|
| `public_examples` | empty in all ten (see §5.4.1: when non-empty, this is the only ground truth in the system that isn't manufactured by a model, and is treated accordingly) |
| `deadline_s` | 300 in all ten |
| Languages | 6 Python, 4 Rust |
| Statement length | 2–3 KB, dense, formal |
| A parameter ≥ 10^18 that forbids direct simulation | all ten |

### 2.1 Trap classes found in the samples

| Trap | Where it appears | What kills a naive solution |
|---|---|---|
| Counts up to 10^18 | run-length outcome counts, `repeat` factors, `default k`, demand `k`, capacities | any loop over the count |
| Structures that cannot be built | flattened schema layouts "may be enormous"; DAG unfolding is exponential; "do not enumerate individual cells" | materializing the structure |
| Persistence and branching | versions created from any earlier version `b < i` | copying state per version |
| Unbounded range reversal | 200k `reverse(left, right)` ops with no bound on total length | list slicing, O(n²) |
| Deep recursion in Python | path depth "may be linear in the node count" (250k) | any recursive traversal |
| Big integers in Rust | sums and products of values up to 10^18 | `i64`/`u64` overflow |
| Custom Unicode rules | grapheme rules stated in the problem, deliberately not Unicode's | a model applying the "real" algorithm from memory |
| Wording that redefines behavior | "implicitly returns error after runs are exhausted", "restoration writes even if another activation changed the cell", "disregard every removed tab other than the selected tab" | reading the general idea instead of the sentence |

### 2.2 Contracts stated in the samples

Python problems say: deterministic, stdlib only, no I/O, no global state, do not mutate supplied arguments, return exact types.

Rust problems say: one program with `fn main()`, stdlib only, no `unsafe`, no filesystem, no network, no nondeterminism, stdin/stdout only, output compared as ASCII-whitespace-separated tokens.

Every one of these is mechanically checkable. Section 6 checks them.

### 2.3 The good news

Every sample is a state machine or simulation with a literal, small-scale interpretation. A brute-force reference that follows the statement sentence by sentence is always writable for tiny inputs. That reference is the ground truth the hidden tests withhold, so building it is mandatory, not optional.

---

## 3. Design principles

1. **The statement is the spec, not the model's memory.** Every prompt instructs the model to derive rules from the text and to flag where the text contradicts a well-known algorithm.
2. **Evidence beats confidence.** A model saying "correct" is not evidence. Compilation, contract checks, differential agreement with an independent oracle, and a passed max-size stress run are.
3. **Independence is the source of truth.** The oracle is generated in a separate context, from the statement alone, in Python regardless of target language, with instructions to be literal and slow. It never sees the candidate.
4. **Speed is a gate, not an audit.** Every candidate runs on a generated maximum-size input under a wall-clock limit before it can be finalized.
5. **The deadline is a clock, not a prompt.** A monotonic budget controller assigns every model call and every subprocess a timeout derived from time remaining.
6. **Few calls, run in parallel.** Roughly five serious model calls fit in 300 seconds. Independent calls are issued concurrently.
7. **Lazy plumbing.** One OpenAI-compatible client, one CLI, four Python modules, and one config file. Effort goes into verification, not scaffolding.

---

## 4. Pipeline

```mermaid
flowchart TD
    A["problem JSON"] --> B["intake + contract"]
    B --> C1["SOLVE call (strong model)<br/>analysis + traps + algorithm + code"]
    B --> C2["ORACLE call (fast role, low effort)<br/>literal Python reference + small-input generator"]
    B --> C3["STRESS call (fast role, low effort)<br/>max-size input generator + edge-case list"]
    C1 --> G["deterministic gate"]
    C2 --> G
    C3 --> G
    G --> H{"all gates pass?"}
    H -- yes --> K["finalize + emit"]
    H -- no --> S["shrink failing case"]
    S --> R["REPAIR call (strong model)"]
    R --> G
    G -.-> T["time controller: stop repairs, emit best"]
    T --> K
```

The three generation calls run concurrently, and preparation of the gate's inputs overlaps them: the orchestrator waits for the ORACLE reply first and starts building the small and medium differential tiers immediately, while the SOLVE and STRESS calls are still in flight (§5.3). Each SOLVE reply is then built into a candidate and **gated as it lands**, while any other attempt is still running (§5.3). The gate is deterministic and costs no tokens. The repair loop runs at most twice against a semantic (behavioral) failure and, independently, at most twice against a mechanical (static/compile) one — see §5.7. The time controller can interrupt at any point and finalize.

### 4.1 Time budget for a 300-second problem

Usable time is `deadline_s` minus a 15-second safety margin. Phases are expressed as percentages so other deadlines scale.

| Relative usable time | 300 s example | Phase | Actual rule |
|---:|---:|---|---|
| 0–0.32 | 0–91 s | generate | intake runs at the start; SOLVE, ORACLE, and STRESS are submitted concurrently. ORACLE/STRESS use `[phases].generate_call_share` (0.70) and SOLVE uses `1 − [limits].solve_reserve_frac` (0.84) of usable time. Role caps still apply: strong 240 s, fast 150 s. |
| 0.32–0.39 | 91–111 s | gate | compile/import, contract, differential, behavior, stress, and overflow steps run while their grants and remaining budget permit. |
| 0.39–0.81 | 111–231 s | repair | semantic and syntax repair loops may run; each repair/fresh solve must fit its call cap plus one gate-pass estimate. |
| 0.81–0.93 | 231–265 s | settle | no new repair or fresh-solve call; the last gate result stands and the report is assembled. |
| 0.93–1.00 | 265–285 s | emit | report is written before the selected solution is written atomically. The final 15 s is the safety margin outside usable time. |

bench17 is why the SOLVE call has its own rule: one Opus attempt was capped at 200 s (0.70 × 285 s usable), timed out at 199.5 s, and the run shipped `no_candidate` with 85 s of budget unused. A share of the phase is the right shape for the two measuring calls, which stopped being on the critical path once preparation began overlapping the solver (§5.3); it is the wrong shape for the one call the run cannot do without. The role's own `timeout_cap_s` still applies underneath — `Run.chat` takes the min() — which is where a model's measured ceiling belongs, and a call clipped at that cap now still yields its code when `===CODE===` was emitted (§8.1, salvaging a cut-off reply).

The phase boundaries above are advisory for gate work, not hard stops: the orchestrator explicitly
checks the repair loop's phase (it will not start another repair once `Budget.phase()` has left
`repair`). What guarantees the deadline is `step_timeout` — every model call and every subprocess is
capped at the remaining time minus a reserve — plus the `safety_margin_s` that is subtracted from
`deadline_s` before any of this arithmetic, not the phase table. That is deliberate: enforcing the
generate window as a hard cutoff would kill the ORACLE call at ~95 s, below its measured 38–128 s
latency, and a run with no oracle verifies nothing.

**Every per-step grant is a share, never a number of seconds.** The problem file supplies only
`deadline_s`, so a step budget written as fixed seconds is correct for exactly one deadline: a 60 s
differential-tier grant is more than half of a 120 s run and a twelfth of a 900 s one, and the 20 s
it leaves behind stops being a reserve in the first case and stops being worth holding in the second.
`config.toml`'s **`[grants]`** table (mirrored by `solve.DEFAULT_GRANTS` for a config without it) is
the single place those shares live — `batch` 0.21, `short` 0.105, `tiny` 0.07, `rust_compile` 0.32,
plus the reserves `batch_reserve`/`short_reserve` 0.07, `call_reserve` 0.035, `stress_reserve` 0.07
and `rust_reserve` 0.053 — and `Budget.grant(name, reserve)` resolves a pair of them against *this*
run's `usable_s` and hands the result to `step_timeout`. Every timed subprocess in `verify.py` and
every model call in `solve.py` goes through it, so no literal second count is left in the code. The
comment on each entry gives what it came out to at the 300 s deadline the values were calibrated
against (285 s usable), which is the behaviour these replaced unchanged. `[limits]` carries the same
conversion for the three larger shares: `solve_reserve_frac` 0.16, `gate_pass_frac` 0.21 (the
affordability price of one gate pass, §5.7) and `medium_tier_frac` 0.07 (§5.3).

What stays in **absolute seconds** is what does not scale with the deadline, each with a comment in
`config.toml` saying why:

- `safety_margin_s`, `per_case_limit_s`, `shrink_budget_s` — a process's own cost and a bound on one
  pathological case, not a share of anything.
- `stress_limit_python_s` / `stress_limit_rust_s` and `stress_min_plausible_s` — the statement gives
  no time limit at all, so these are *our judgment of a hidden judge*, and a judge's limit does not
  grow because our deadline did. They are what the candidate is measured against, not what we spend.
- `stall_timeout_s` and each role's `timeout_cap_s` — network and model latency. A model does not
  answer faster because the deadline is shorter; `Run.chat` takes the `min()` of the cap and the
  share, so the ceiling binds when it is the smaller of the two.
- `repair_cap_s` — the measured latency of a repair by *that* model, used as the floor half of
  `max(repair_call_share × usable, repair_cap_s)`. The fraction is the other half, which is what
  keeps a short deadline from promising a call it cannot fit (§5.7).
- `oracle_retry_afford_s`, `oracle_selfrepair_afford_s`, `repair_afford_s` — ceilings over rules that
  already read the mapped role's own cap (§5.3.1), so they re-tune themselves when the model changes
  and never need to follow the deadline.

### 4.2 Why this fits

Serious reasoning calls can consume most of the deadline. The implementation therefore submits the three initial roles concurrently, gates SOLVE replies as they arrive, and spends remaining time on deterministic checks and bounded optional work. The default is one SOLVE attempt, one ORACLE, one STRESS, up to two semantic repairs plus two syntax repairs, and at most one fresh solve; transport retries, refusal fallbacks, no-block retries, oracle self-repair, and oracle adjudication can add requests when their budget/cost checks allow them.

---

## 5. Stages

### 5.1 Intake

- Parse JSON; require `problem_id`, `language ∈ {python, rust}`, `statement`, `entrypoint`, `deadline_s > 0`.
- Compute `safe_deadline = monotonic() + deadline_s - margin`.
- Build the contract: Python → `def <entrypoint>(...)` must exist; Rust → `fn main()` must exist.
- Invalid input exits with code 2 before any model call.

### 5.2 SOLVE call (strong model, one call)

One structured response with five sections. Merging analysis and code into one call halves latency and keeps the analysis in the same context that writes the code, which is where it matters.

Required sections:

In the order the prompt asks for them:

1. **Code** — in a fenced block with a fixed marker. The orchestrator extracts by marker, never by trusting prose.
2. **Examples** — 3–5 hand-traced `(args, expected)` pairs, machine-readable (§5.2.1).
3. **Rules** — a numbered restatement of the behavioral sentences of the statement, quoting the text, at most 15 numbered lines with the ambiguities and their chosen readings among them.
4. **Design** — state representation, the invariant per operation, complexity at the stated maxima, overflow and recursion treatment, and one hand trace of the hardest boundary, in at most ~20 lines.
5. **Traps** — one line for each trap class from §2.1 that *actually applies*, saying what a naive approach would do and why it fails here. The classes that do not apply are skipped, not answered "n/a".

CODE comes *first*, and the four blocks after it are a **record** of thinking already done, not a plan
for it — the prompt's opening instruction is to think the statement, its rules and the cost of the
approach through before writing anything, and it carries the complexity-rejection rule (reject an
approach that iterates a huge bounded count, materializes an enormous structure, or recurses linearly
in the input) ahead of the CODE block rather than inside a DESIGN block that now comes later.

RULES-before-CODE was the earlier order, chosen for a non-reasoning model so it would read before it
coded. A reasoning model does that reading in its reasoning tokens, so the visible RULES and DESIGN
blocks became a second pass costing 1–2k output tokens — 30–60 s at the measured rates — before the
first line of code: in bench21 the CODE block had not closed at the 240 s cap and salvage (§8.2)
recovered nothing. Putting CODE first also makes salvage strictly more likely to pay, since the block
the gate cannot do without is the one written earliest. ALGORITHM, which overlapped DESIGN almost
entirely, is folded into it: one block, not two. The orchestrator parses by marker (`parse_blocks` is
order-independent), so the order is a prompt decision only.

The prompt carries one optional section on top of those five, `{{previous_attempt}}`, which is empty
for the concurrent generation calls and filled in only for a **fresh solve** (§5.7.1): the failed
attempt's algorithm and code, and the concrete failure that ended it.

Language-specific instructions baked into the prompt:

- Python: iterative traversals only; no globals; no mutation of arguments; no recursion on data whose depth can be large.
- Rust: read all stdin into one buffer, `BufWriter` for output, `i128`/`u128` for sums of 10^18 quantities, `BTreeMap` or sorted output where iteration order reaches stdout, no `unsafe`.

On top of those, one **I/O contract per language** (`solve.py`'s `io_rules`) is rendered verbatim into SOLVE, REPAIR, ORACLE and STRESS, so the candidate and the oracle cannot each pick a different, individually defensible reading of the same shape. For Rust it is the stdin token-stream rule (§5.4). For Python it is the outer container: return exactly the types the statement names — the judge compares with `==`, so `()` is not `[]` — and where the statement names none, a `list` for the returned sequence and a `tuple` only where it says tuple or pair. Three rounds running, both of 2beff58fa923's attempts returned `()` where the oracle returned `[]` on the empty input, and a repair attempt was spent on a difference the statement is silent about.

#### 5.2.1 Hand-traced examples: a second reading of the statement

The SOLVE reply carries one more block, `===EXAMPLES===`: three to five lines, each a Python literal
`(args, expected)` pair (for Rust, the complete stdin and the complete stdout as strings), traced from the
statement by hand and explicitly not obtained by running the code. The prompt asks for the smallest legal
input, one input at a stated numeric limit, and the input the author thinks is most likely to be misread — and it
asks for all of them to be *in contract*: every example must satisfy every precondition the statement states, and
every container in it must be spelled the way the same `{{io_rules}}` the oracle and stress authors receive says it
is. Both halves are paid for in lost checks: an out-of-contract input is rejected by the oracle's `validate()` and
dropped (3 of 10 examples on bench14, 2 of 8 on bench15), and a container spelled the other way round is rejected
until the respelling retry rescues it.

The respelling retry offers three spellings of a rejected input, and the first of them is derived from the run
itself: after the small tier is built, `verify.input_skeleton` reads the **container skeleton** off one input the
oracle's own `gen()` produced and its own `validate()` accepted — per argument position, then per sequence depth,
`("list"|"tuple", shape-of-the-first-element)`, with leaves untouched. Every hand-written input (a SOLVE author's
`===EXAMPLES===` line, a STRESS author's `EDGES` entry) is respelled into that shape before `validate()` judges it,
and only then are the two uniform spellings (all-tuples, all-lists) tried. The uniform pair is not enough: bench18's
reference opened with `if not isinstance(schemas, list): raise` and then `if not isinstance(schema, tuple): raise`,
a MIXED shape that neither uniform form reaches, and the run lost all four hand traces and all eight edges to it.
The skeleton is authoritative rather than a guess — by the shared I/O contract the candidate must accept whatever
`gen()` produces — so an input accepted in the respelled form is used in that form for both candidate and oracle.
A shape that is genuinely wrong (bench18's edges passed a flat list of layout terms where a list of schemas was
meant) is not reachable by any respelling and is still dropped. Counted as `oracle.examples respelled=N` /
`oracle.edge respelled=N`, and the skeleton itself reaches `report.json` under `gate_inputs.input_skeleton`. The
ORACLE prompt carries the other half of the fix: never raise on the container *type* of an argument, since the
harness may hand the reference either spelling.

`verify.parse_examples` reads them with one restricted evaluation per top-level expression — never `exec`, this is
model output. `verify.split_examples` cuts the block into those expressions by **bracket balance** rather than by
newline: a newline or a comma separates only at depth 0, string literals (triple-quoted ones included, which is what
a Rust stdin fixture is) are consumed whole, and a `#` outside a string runs to end of line. bench19 asked for five
examples and gated on three, because the two longest — the ones the prompt asks for by name, the limit-sized trace
and the deepest state — were pretty-printed across two physical lines and the line-oriented scan handed `ast.parse`
two incomplete fragments. The grammar is Python literals plus integer arithmetic (`+ - * ** // %` and unary sign over
`int` constants, with magnitudes capped at 10^40 and exponents at 10^4); names, calls, attributes and comprehensions
are rejected, and a
`*` whose operand is a container or a string is rejected rather than repeated. `ast.literal_eval` alone refused
`10**18`, so on bench15 the candidate lost both of the traces covering the very limit the statement is built around
while the other candidate, which spelled the same constant as `1000000000000000000`, lost nothing. It then
drops any expression that is not a 2-tuple (the count is reported per candidate as `examples: {count, dropped}`),
wraps non-tuple Python args as a 1-tuple, requires a `str` for Rust, and caps the list at 8. They are used twice:

- **Against the candidate** — gate step `diff_examples` (§5.5), immediately after compile and before every
  other differential step. A failure means the code contradicts its own author's reading of the statement, and
  it drives the normal repair loop with `kind=diff_examples`; the repair prompt's *Expected* line then says the
  value came from the solution author's own hand trace rather than from the reference. That failing case is
  **not** shrunk and **not** kept as a regression: shrink re-derives the expectation from the oracle, which
  would silently swap one ground truth for the other. A repaired child inherits its parent's examples.
- **Against the oracle** — `prepare_gate_inputs` runs the oracle's `validate()` and `reference()` over every
  distinct example input (the union across all candidates, deduped by `repr(args)`) and records
  `example_checks = {cases, disagreements, errors}`, which reaches `report.json` under `gate_inputs`. The
  oracle never sees the examples in its own prompt; this is a reading it did not write.

When the oracle contradicts at least half of at least two hand-traced inputs (`verify.examples_disputed`), it
is **disputed**: the same one-shot self-repair path §5.3.1 uses regenerates it, with the disputed inputs and
their hand-traced expected values as the extra context — inputs and expected values only, never any candidate
code. If the replacement still contradicts half of them, every `diff_*` step of the run is marked degraded
(`reference disagrees with hand-traced examples on X of N`) and the status can no longer be
`passed_all_gates`. If instead the replacement agrees with all of them, the §5.7 cross-check's "neither
reference can be trusted" degradation is *not* applied to their wholesale disagreement: the examples are
outside evidence saying which of the two references was wrong.

This is the generic replacement for a sample-specific smoke check that briefly existed and was deleted: it
scores on every problem rather than one, and it costs no extra model call.

`llm.parse_blocks` is deliberately forgiving at the boundary: it takes the last occurrence of a marker,
extracts the longest fenced body, removes an unmatched fence and a bare leading `python`/`rust` line, and
lets an unterminated block run to end-of-reply. `terminated_blocks` separately records only blocks with their
own `===END===`; `salvageable` accepts a timed-out reply only when its `===CODE===` block is closed.

### 5.3 ORACLE call (fast role at low effort, concurrent)

Produces three Python functions in one response:

- `reference(args)` — the literal simulation. Told: "Follow the statement sentence by sentence. Loop where the statement loops. Build what the statement describes. Ignore efficiency. Inputs will be tiny." For Rust problems it takes the parsed stdin text and returns the expected stdout tokens.
- `gen(seed, size)` — a seeded generator of small valid inputs. Told to honor every validity constraint in the statement, to include boundary values (empty, single element, equal values, adjacent positions), and to include a `medium` mode with counts around 10^4 to 10^5 so closed-form arithmetic in the candidate is exercised where the literal reference still finishes in seconds. A third mode, `large`, builds one input at the statement's stated maximum (or as large as three seconds of bulk construction reaches) purely so the timing gate has an input when STRESS's `gen_max` is unusable — §5.4. Nothing ever runs `reference()` on it.
- `validate(args)` — **defined as "`reference()` did not raise `ValueError`"**, and nothing else. The prompt gives the four-line template verbatim, and `verify._ensure_validate` appends it when the model forgot it (a `validate` the model *did* write is kept — the harness does not fight it). The reason is measured: asked for a separate predicate, every model wrote a token-shape and range parser, so every precondition that depends on evolving state ("must currently exist", "never declared") went unenforced and out-of-spec inputs became ground truth (round-10 L4, five of ten runs). Two consistent functions written from one precondition list is more than a fast model delivers; one function is. So `reference()` is told to `raise ValueError` at the point any stated precondition is violated — and told just as firmly that an operation the statement *itself* calls invalid and specifies a result for is not a precondition violation but an ordinary case with a specified answer. Any other exception out of `reference()` (`IndexError`, `KeyError`, …) is a bug in the reference, not an invalid input, and is counted as one (§5.3.1). Every input the gate uses goes through `validate`: both `gen` tiers, the STRESS edge list, every max-size candidate input (`gen_max`'s, then `gen(seed, "large")`'s — a rejection moves on to the next source rather than gating on an input the statement forbids) and every input the shrinker invents (§5.6). `verify._validate_inputs` is the single place that call is made; a validate that crashed or timed out on an input gives no verdict, which is not a rejection, and a validate that rejected a whole tier is distrusted exactly as before. When a tier is distrusted AND `reference()` returned the same answer for inputs that differ, those are two independent signals that the oracle cannot parse this input format at all: the tier is emptied with a note rather than used to fail a candidate for printing the right answer. The cost of the definition is one extra `reference()` call per input, which is why the per-case and batch limits apply to the validation pass exactly as to the reference pass.

The oracle is Python for both target languages. For Rust problems this gives a second language and free big integers, which removes the risk that oracle and candidate share an overflow.

The oracle writes the largest output of the three concurrent calls (three functions, not one), so it is reliably the slowest and the one most likely to hit its timeout. If the first attempt returns no `===ORACLE===` block at all (a timeout or empty reply), the orchestrator retries it at most once. A timeout retry requires the configured oracle role's timeout cap plus the configured repair affordance; a non-timeout missing-block retry uses `oracle_afford_s()` and `oracle_retry_afford_s`, both bounded by remaining budget and the cost cap. This is independent of `regenerate_oracle` (§5.7), which reissues the oracle for self-repair and adjudication with separate one-shot counters (§5.3.1 and §5.7). **The SOLVE call has the same one-shot no-block retry**, while its budget covers a fresh-solve-sized call plus one gate pass; it does not retry a timeout (salvage owns that case) or a refusal (the refusal ladder handles it).

Before any generated oracle or stress source is executed, a fixed preamble — `import random, math, itertools, collections, string, heapq, bisect` — is prepended to it. The prompt tells the model to *use* `random.Random(seed)` but never explicitly tells it to import anything, and one live run produced a module with no imports at all, whose `gen()` died with `NameError`. A duplicate import is harmless if the model already wrote one. The preamble is prepended to the exact string that gets written to disk as that run's `cand.py`, so any traceback line number always matches what's actually sitting in the run directory — it only differs from the raw LLM completion's own line count.

**Preparation overlaps the other two calls.** `prepare_gate_inputs` is split into three phases over one `GateInputs`: `prepare_oracle_tiers` (public examples, the `small` and `medium` differential tiers, the validate/gen reject fraction) needs only the ORACLE reply; `prepare_stress_inputs` (the `EDGES` fixtures and the max-size timing input) needs the STRESS reply; `check_oracle_examples` needs the SOLVE replies, since the hand-traced examples arrive with them. The orchestrator therefore waits for the oracle first, runs its retry/self-repair decision, and starts the tiers while the other calls are still outstanding — logged as `prep.overlap solve_pending=<n>`. On bench14 the oracle landed at 30 s, stress at 51 s and prep did not begin until 51 s, finishing at 62 s: eleven seconds that were free, and with a slow strong model the whole preparation is. The work is subprocess-bound, so it runs in the orchestrator's own thread rather than in the pool, whose workers belong to the model calls. `prepare_gate_inputs` remains as the all-at-once wrapper for `regenerate_oracle`, which has every input in hand.

**One attempt by default.** `[limits] solve_attempts` is 1. Two concurrent attempts were tuned to a 17 s non-reasoning coder model, where the second cost nothing but tokens — N attempts cost `max()` latency, not `sum()`. A thinking model spends 150–200 s on a solve, which is most of the deadline, so the second attempt stops being free: it leaves no room for the repair loop, and bench16 lost a run to a one-line boundary bug a repair could have fixed. One attempt plus a repair at low effort (§5.7) is the better trade at this latency, and it halves the per-problem cost. Raise it back to 2 when the *oracle* is the suspect rather than the solution: the two-candidates-agree dispute (§5.7) is the strongest evidence the system has that the reference is the wrong side, and it needs two candidates to exist. Everything below holds for any N.

**Each candidate is gated as its own reply arrives.** The orchestrator used to join on every SOLVE attempt before gating any of them, which idled 43 s on bench15 and 31 s on bench16 — with a thinking model in the strong role one attempt can be 40 s behind the other or run all the way into its cap and return nothing. The solve futures are now consumed with `as_completed`: each reply is turned into a candidate (examples parsed, `check_oracle_examples` run over the examples of every candidate so far) and put through a full gate pass immediately, logged as `gate.early cand=<id> solve_pending=<n>`; a reply that timed out or carried no code simply produces no candidate. Two consequences are worth naming. Candidate ids follow the order replies *land*, not the order attempts were submitted. And the example check accumulates while the dispute decision does not: a verdict reached on the first candidate is not re-opened by the second unless that second candidate contributed a value disagreement nobody had seen, and the one oracle self-repair a run is allowed stays one.

**The `medium` tier is time-boxed, not phase-gated.** It is the most expensive evidence per second and the least decisive, so it gets at most `[limits] medium_tier_frac` of usable time (0.07, ≈20 s at a 300 s deadline) for the generator batch and the reference batch together; if the generator alone spends it, medium ships 0 cases with the note `medium tier: time box exhausted`. It used to be *dropped* once the budget left the `gate` phase, which cost bench14 its only mid-size tier twice over: 60.3 s to produce 6 cases, which the oracle regeneration then discarded, and the re-prep skipped the tier entirely with time still on the clock. A box bounds the cost without ever silently dropping the tier, and the same rule applies to a regenerated oracle's re-prep.

#### 5.3.1 Oracle self-repair: the CALL can succeed while the CODE is unusable

The retry above reacts to the ORACLE call itself failing (timeout, empty reply). A different, later-discovered failure mode is silent by comparison: the call succeeds, `oracle_src` parses, but `reference()`, `gen()`, or `validate()` then raise at runtime on every input they're given — an f-string referencing an undefined name, a `gen()` unpacking its own tuple wrong, a missing import. `prepare_gate_inputs` already detects and records every one of these (`"reference failed on all small inputs: <traceback>"`, `"gen(medium) produced nothing: <traceback>"`), but until this fix nothing acted on the detection: no gate step fails (there is nothing to check against), so the run proceeds with zero verification and ships silently as `emitted_unverified`.

After `prepare_gate_inputs` returns, `verify.oracle_unusable(gi)` asks the one generic question that covers every variant of this failure without hardcoding which function or which tier broke: did the oracle yield **no usable case at all, in every generated tier** (`cases_small`, `cases_medium`, `cases_edge` all empty)? Public examples don't factor into this check either way — they're ground truth from the problem, not something the oracle produced, so they say nothing about whether the oracle itself is usable.

When the oracle is unusable and the budget can still afford one more oracle-shaped call (`[limits].oracle_selfrepair_afford_s`, same cost profile as the initial call and its retry), the orchestrator calls `regenerate_oracle` — the same function §5.7's adjudication path uses — passing it the collected failure notes (the actual tracebacks the broken code produced) as the extra context appended to the statement, so the model can see exactly what its own code did wrong. `prepare_gate_inputs` then reruns against whatever new source comes back.

**Second trigger: the oracle that rejects its own inputs.** The same one-shot path fires on a second, equally silent failure — the oracle runs fine, but its `validate()` throws out most of what its own `gen()` produced. Round 8 ended four of ten runs as `emitted_unverified` on exactly this: 30 of 32 small inputs rejected on 2beff58fa923, 163 of 200 small and 17 of 20 medium on 5d02bd0e16ab, 46 of 55 on 6eca8a9120e0, and the independent STRESS author's edges and `gen_max` rejected along with them. On 5d02bd0e16ab the cause was visible in the source: `validate` re-modelled the state machine more loosely than `gen`, never recording the versions `R` created, so every later reference to a version ≥ 1 was "invalid". `gen` and `validate` are two readings of the same precondition list, so a disagreement that wholesale means one of them misreads the statement — and gating on the handful of survivors, or on inputs one half of the oracle calls illegal, is worth less than one more oracle call. `prepare_gate_inputs` therefore records rejected/checked per tier (`make_cases` keeps up to two rejected inputs as evidence) and stores the small+medium ratio as `GateInputs.validate_reject_frac`; at or above `[limits] validate_reject_frac_regen` (default 0.5) the self-repair fires. An input whose `reference()` raised something other than `ValueError` counts toward that same ratio: with `validate` defined as "the reference did not raise `ValueError`", a reference that crashes is an unusable oracle, and while those were recorded only as "errors" they could never fire the regeneration. Tiers where `validate` was distrusted (it rejected everything, or there is none) are excluded — they say nothing about a *disagreement* — and the edge tier is recorded but excluded from the ratio, appearing in the prompt as independent corroboration since those inputs came from the STRESS author, not from this oracle's `gen()`. The extra context quotes the counts and the rejected inputs and asks for the preconditions to be re-listed and both functions written from that one list, with one `gen()` output traced through `validate()` — and, like every oracle call, contains no candidate material. The prompt itself was tightened to match (§5.3). Unlike the crash trigger, this one has something to lose: if the replacement rejects at least as much of its own output, or comes back with no cases at all, it is discarded and the original oracle is kept.

**Every reason that holds is collected, and the regeneration carries every matching paragraph.** The four triggers used to be an `elif` chain: one reason was picked and one paragraph sent. An oracle is rarely broken in exactly one way. bench19's was self-rejecting (52 %), produced 75 of 75 identical answers, *and* had no code path at all for a whole family of the statement's operations; the rejection paragraph won, the paragraph that addresses answer variety — the one that would have fixed the defect the run actually died of — never went out. The health check now evaluates all of them and `extra` concatenates `selfrepair_extra` (for the unusable/self-rejecting reasons it was written for, whose two shapes it still switches between), `crash_extra`, `weak_tier_extra` and `examples_dispute_extra`, logged as `oracle.selfrepair reasons=[…]`. **`crash_extra` is new evidence, not a rewording:** an input whose `reference()` raised something other than `ValueError` gets verdict `None`, which counts toward `validate_reject_frac` but was excluded from `rejected_examples` (those are the `False` ones), so the prompt could state the fraction with no exception to show. `make_cases` now keeps the first three exception lines per tier and the paragraph names them — "your reference() raised an exception other than `ValueError` on N of its own gen() inputs … `KeyError: 'flattened'`" — with the rule that any raise but `ValueError` is a bug in the reference, not an invalid input. And because one regeneration now answers several complaints at once it can fix one and break another, so the **keep-the-better guard is evaluated over all of them**: unusable, reject fraction and weak share are each checked wherever the original had something to lose, and the replacement is kept only if it is no worse on every one of them.

**The health triggers are judged during the generate-phase overlap.** Three of the four triggers — an unusable oracle, one whose `validate()` rejects its own `gen()` output, and a small tier whose answers do not vary — are facts about the oracle *alone*; they need no candidate, and `prepare_oracle_tiers` knows all three the moment it returns. They are therefore evaluated right there, while the SOLVE call is still in flight (logged `oracle.selfrepair triggered: …; solve_pending=<n>`), and the regeneration plus its re-prep run against the same dead time the tier build already uses. On bench19 prep finished at 18 s reporting *both* "small tier weak: 75 of 75 expected values are identical" and "validate() rejected … 89 of 171 inputs its own gen() produced"; the check only ran when the solve reply landed at 180 s, the afford threshold then refused the call, and the run shipped `unverified` at 183 s with 100 s unused. The fourth trigger — the example dispute — needs a candidate's hand trace and still runs after one arrives. All four share the one-shot budget: a health regeneration spends it, and the dispute then does not fire. A regeneration during the overlap resets the max-size chain (`stress_tried`) it walked on its own, because `prepare_stress_inputs` has not run yet and must still be free to prefer `gen_max`.

**The afford thresholds follow the configured model, not a literal.** `[limits] oracle_retry_afford_s` and `oracle_selfrepair_afford_s` were tuned to a model measured at up to 128 s per call and sat at 130 s, which refuses every optional oracle call through the whole second half of a deadline. `solve.oracle_afford_s` takes the smaller of the config value and the measured ceiling of whichever role `[roles]` sends the ORACLE prompt to (`timeout_cap_s × 0.5 + 30`, the call at half its cap plus the time to use what comes back), so swapping the model re-tunes the threshold by itself; the config values are now 60.0 and act as a ceiling, never a floor. During the overlap the remaining budget is nearly the whole deadline, so this number only ever binds a late trigger. The cost cap applies to every trigger unchanged.

This has its **own one-shot counter**, `GateInputs.oracle_selfrepairs`, entirely independent of `oracle_regens` (§5.7's adjudication counter) — `regenerate_oracle` takes a `counter` argument naming which field to increment, so the two recovery paths never share a budget. A run can recover from a broken oracle early on via self-repair and still adjudicate a genuine candidate/oracle disagreement later via the repair loop; both can fire, once each, in the same run. Both counts are reported (`oracle_selfrepaired`, `oracle_regenerated`), and the trigger is logged distinctly (`oracle.selfrepair ...`) so it's visible in `report.json`'s `events` separately from an adjudication regeneration (`oracle.regen ...`).

### 5.4 STRESS call (fast role at low effort, concurrent)

Produces `gen_max(seed)` — one input at every stated maximum simultaneously (max N, max Q, max values, max nesting, deepest recursion path, worst-case operation mix), plus a short list of hand-picked edge cases as literal inputs.

Two rules in that prompt come from bench14, where `gen_max` returned three operations, no `default` at all, and a repetition count its own author capped at 10 000 with the comment `# keep rep smallish to avoid blowup` — against a statement bounding repetitions, capacities and default counts by 10^18. First: a count, repetition, capacity or multiplier that appears *inside* the input is a number, not work — writing `10**18` costs the generator nothing whatever the structure it describes would cost to build — so every quantity the statement bounds by a huge number must appear at its stated maximum at least once, in an operation the candidate actually has to process. Second: the input must stay valid in the statement's own sense all the way to its end, because a statement that stops at the first invalid operation discards everything after it — bench14's first operation named an identifier the input never created, so 476 KB were answered at operation 1. Deliberately invalid operations go last, or into `EDGES`.

The whole module is imported to read either of them, so the prompt forbids anything running at import time except `import` statements, `def`s and the `EDGES = [...]` literal: on 1ba0d34fae43 the STRESS source asserted its own `EDGES` at module level against rules the statement never stated, the import raised, and the edges and `gen_max` were lost together — the call bought nothing.

For Rust, both `EDGES` entries and `gen_max`'s output are stdin text, and each is dedented per line (leading/trailing whitespace stripped on every line) before it reaches the gate. Model-written `EDGES` are Python triple-quoted string literals indented to match the surrounding list, so every continuation line inherits that indentation; fed verbatim, a line-oriented Rust parser sees `"    41".parse::<u64>()` and fails. The judge never sends indented input — the README defines Rust I/O as ASCII-whitespace-separated tokens — so the indentation is purely source formatting and is stripped, not preserved.

**A verdict the validator could not give is not an acceptance.** `_accept_stress_input` records one of three verdicts for the max-size input: `accepted`, `rejected` (move on to the next source, as before) and `unjudged` — validate() crashed or timed out on it, or this run has no trusted validate() at all. `unjudged` used to be silently treated as `accepted`. On bench15 `gen_max` returned a structure nested 200 000 deep against a stated cap of 60; the literal reference could not finish on it, so there was no verdict, and both candidates — one of them correct and ≤ 0.74 s on every legal maximum — were killed at the timing cap by an input the statement forbids, and the run shipped `emitted_with_failures`. The input is still *used* (an indicative timing beats none), but the resulting `stress` evidence carries `detail.degraded = "max-size input could not be validated (reference did not finish); timing is indicative only"` and is recorded as **skipped whichever way it went**: a pass is a skipped pass (status `emitted_unverified` at best) and a failure is a skipped pass too — it never fails the candidate, never routes to repair, and never spends a fresh solve on evidence nothing can confirm. This is generic over every problem whose constraints include a bound the literal oracle cannot evaluate — huge counts, enormous structures, exponential unfoldings — which is exactly the class where timing matters most. The other half of the fix is in `prompts/stress.md` (§5.4): `gen_max` must assert every stated bound — counts, lengths, values, and the structural ones (nesting or reference depth, chain length, tree height) — against the input it is actually about to return, and raise rather than return an input that breaks one.

**`bounds()`: the half of the validator a max-size input can actually be run through.** `validate()` *is* the literal reference (§5.3), so on a genuine maximum-size input it cannot finish — which makes every real `gen_max` output `unjudged` and throws away its timing on exactly the problems where timing decides the score. On bench19 the 5.8 MB input was `validity=unjudged`, the 2.4 s measurement was "indicative only", and the run could not reach `passed_all_gates`. State preconditions ("must currently exist", "never declared before") genuinely need the simulation. The **stated bounds** do not: counts, lengths, value ranges, nesting or reference depth are what a linear parser reads off any input, whatever its size. The ORACLE prompt therefore asks for one more function, `bounds(...)`, taking the same arguments as `reference` — `None` when every stated numeric and structural bound holds, a short string naming the first violated one otherwise, linear in the input, never simulating the operations, never raising.

`verify.bounds_verdicts` runs it in the sandbox, time-boxed to 20 s, wherever the reference gave no verdict. In the max-size chain (`_accept_stress_input`, so this covers `gen_max` and the oracle's own `gen(seed, "large")` alike): a named violation **rejects** the input and the chain moves to the next source — a stated bound is a fact about the input, not about the candidate; a clean answer records `stress_validity = "bounds_checked"`; a missing, crashed or timed-out `bounds()` leaves `unjudged` exactly as before. A `bounds_checked` input's stress evidence **counts as a real check** — a pass is a pass, a failure is a failure that routes to the fresh-solve path — and carries `detail.validity = "bounds_checked"` so the report never claims the state preconditions were simulated. The same check is applied to the STRESS author's `EDGES` as a cheap extra guard; there it buys no coverage (an out-of-bounds edge is already rejected by the reference) but names the bound instead of reporting a bare drop.

**Where the max-size input comes from, in order.** `gen_max(seed)` is tried first and validated; it failed or was rejected in nine of ten round-10 runs, and it was the only source there was, so the timing check had nothing to run on. Second is the ORACLE call's own `gen(seed, "large")` — the same state model as its `small`/`medium` modes, driven to the statement's stated maximum or to whatever it can build in about three seconds, written in the oracle's own context from the same statement, and run under the same sandbox timeout and memory limits as `gen_max`. It is validated the same way, and one extra check applies: an oracle whose `gen()` ignores its `mode` argument answers `"large"` with a small input, so an input no larger than the biggest case in the generated tiers (or one with no tier to compare against at all) is marked degraded rather than trusted as max-size. Whichever source is used is logged as `stress.input source=gen_max|gen_large|medium_degraded validity=accepted|unjudged|unvalidated size=<bytes>` and recorded in `gate_inputs.stress_source` / `gate_inputs.stress_validity` in the report. The 50 MB harness output cap and the statement's own stated limits apply to every source.

If both generators fail and at least one `medium`-tier case exists, the stress input falls back to the largest of those medium cases — "largest" measured as `len(str(case.input))`, which is generic across a Python argument tuple (its `str()` scales with total content) and a Rust stdin string (its `str()` is the string itself) without knowing anything about the problem's shape. This gives the timing check *something* to run rather than skipping it outright, but a medium case is capped far below `gen_max`'s target scale — it is not a maximum-size input. The resulting `stress` evidence is marked `detail.degraded`, and a *passing* degraded run is additionally recorded as `skipped`: finishing inside the limit on a smaller-than-maximum input is not evidence the candidate is fast enough, so it must not count as a passed gate (the status is `emitted_unverified` at best). A degraded run that was still **too slow** is kept as a real failure — too slow on a below-maximum input is only more damning. With no medium case either, the stress step is skipped exactly as before.

The same `detail.degraded` marker covers thin coverage. A `diff_small` or `diff_medium` step that passed on fewer cases than `[limits] min_cases_small` / `min_cases_medium` (config.toml; defaults 30 and 5 against the 200/20 targets) is marked degraded by `run_gate`: agreement on a handful of inputs means the oracle's `gen()` mostly crashed or its `validate()` rejected most of what it produced, and one run was observed shipping as a full pass on 48 small and 4 medium cases. An empty tier is `skipped`, not degraded. In the finalizer, any evidence carrying `detail.degraded` counts exactly like a skipped step: the status becomes `emitted_unverified` rather than `passed_all_gates`, and `benchmark.md` shows the cell as `degraded`. The gate order and the repair loop are unaffected — degraded evidence still *passed*; it is only weaker than the name of the step claims. A third source of `detail.degraded` is an oracle regeneration that disagrees with the oracle it replaced (§5.7): that marks every `diff_*` step of the run.

The same marker covers thin *discrimination*, which the case count hides completely. A tier of at least 20 cases
in which one expected value (compared by `repr`) covers 90% or more of them is marked `weak` by `verify.make_cases`
(`small tier weak: 195 of 200 expected values are identical`), recorded in `gate_inputs.weak_tiers`, and degrades
its `diff_*` step. On bench18 the small tier's 200 answers were 195 × `1` and 5 × `2` — every case asserting only
"operation 1 is invalid", because the oracle's `gen()` invented identifiers instead of reading them out of the
state it had just built — and the gate reported `diff_small passed=True cases=200`, the same headline a genuinely
discriminating 200-case tier gets. The pre-existing "validate rejected everything AND reference gave one answer"
signal is the stronger form of the same defect and still empties the tier outright; this one degrades rather than
deletes. It is generic over every problem with non-trivial preconditions, where a naive generator lands on
"invalid" / "no solution" / "empty output" almost every time.

A **weak small tier is also a regeneration trigger** — the fourth on the one-shot self-repair path of §5.3.1, with
the same budget and cost rules as the others. The extra context names the value almost every input produced and
gives the rule that fixes it: every identifier, name, key, index or position an operation refers to must be drawn
from what the generator itself created earlier in that same input. If the replacement's tier is still weak, the run
keeps whichever of the two has the lower share of identical answers and stays degraded either way. `prompts/oracle.md`
carries the same rule for the first attempt.

A fourth source is a stress run that finished faster than any real work could. The timing gate times the candidate but never checked that the candidate actually *consumed* the input: on bench14 `gen_max`'s first operation named an identifier its own input never created, so the candidate discarded a 476 KB input at operation 1 and the gate recorded a pass for a solution needing ~10^11 s at the stated limits. A *passing* `stress` whose measured duration is below `[limits] stress_min_plausible_s` (default 0.01 s) now records `detail.suspicious` (`finished in <d> s on a <n>-byte input; the input may not exercise the candidate`), carries it as `detail.degraded` and is recorded as `skipped` — never as a pass. It is generic over every early-exit shape (first-invalid-index answers, validators, short-circuiting searches) and costs no tokens; its one known false positive is a genuinely O(1) closed-form answer, which loses "pass" for "degraded" and nothing else.

A fifth source is the STRESS author's own stated answer. The duration heuristic above is only a duration heuristic, and an input can be legal, maximal and still measure nothing: bench19's `gen_max` was inside every stated bound and carried 70 231 operations at 10^18, but one off-by-one emitted a duplicate operation at index 60 123, the statement's "stop at the first invalid operation" rule ended processing there, and the 139 877 operations that followed — the whole large section, the only part that exercises the interesting code — were never executed. The candidate answered in 0.012 s and nothing noticed, because 5.8 MB passes any size check. The author knows what it built, so the prompt asks it: `prompts/stress.md` requests an optional `EXPECTED_MAX`, the exact answer the statement gives for `gen_max(1)` (seed 1 is the seed the harness passes), derived by reasoning about the input rather than by running any solution. `verify.expected_max` reads it **statically**, out of the module's `EXPECTED_MAX = <expr>` assignment, with the hand-traced examples' restricted evaluator — literals plus integer arithmetic, so `200000 * 10**18 + 1` is in and a name, a call or a comprehension is refused. That refusal is the enforcement, not a precaution: a module that *computes* its expectation is read as having stated nothing. A mismatch against the candidate's stress output (compared exactly as `differential` compares, so Rust is token-wise stdout) records `expected_max_agree: false` and marks the evidence `suspicious` with the reason `candidate's answer on the max-size input differs from the author's expected answer`, which takes the same fall-through as any other suspicion; it never fails the candidate on its own, because the author may be the one who is wrong. A match logs `stress.expected_max agree=True` and changes nothing. The claim is about `gen_max(1)` alone, so it is never compared against a fall-through source's input, and a module that defines no `EXPECTED_MAX` gets exactly the behaviour it had before.

**A suspicious *or unjudged* measurement falls through to the next source.** The suspect in that case is the *input*, not the
candidate, and the chain above has more of them — but until now a suspicious first source ended the timing check
for the whole run: on bench18 `gen_max` returned 7 operations with an invalid one first, the candidate answered in
0.000 s, the gate correctly refused the pass, and `gen(seed, "large")` sat unused. No candidate-independent
pre-check can catch this (whether an input exercises a solution is a fact about that solution), so it is done at
gate time: when the `stress` step comes back `suspicious` and a later source in the chain has not been tried,
`verify.next_stress_source` advances to it (logged `stress.fallthrough from=gen_max to=gen_large`) and the step is
re-run on the new input. The better evidence is kept — a real measurement or a degraded pass both beat a suspicious
one, and two suspicious runs leave the run degraded exactly as one did. Bounded at **two stress runs per candidate**
by construction: only the first result can trigger the fallthrough. `gate_inputs.stress_tried` records which
sources have been consumed, so the next candidate resumes the chain rather than repeating it.

An `unjudged` first source falls through the same way and for the same reason — nothing could say the input was
legal, so the step is a skipped pass whichever way the clock went — with one difference in what counts as better.
A second stress run is only spent when the next source comes back `accepted` or `bounds_checked`: a second
unjudged input buys the same skipped pass for a whole timing grant, so the source is marked tried and the run
keeps the input it already had. With `bounds()` (above) this is the path that turns bench19's `unjudged` 5.8 MB
input into a timing measurement that counts, from whichever source the oracle can actually judge.

For this to mean anything, `duration_s` had to become the candidate's own time. The Python harness started its clock before `ast.literal_eval` and `copy.deepcopy` of the arguments, which cost ~0.10 s on a 50 000-element input and are identical for every candidate — so a candidate doing no work at all and one doing all of it read as 0.103 s and 0.105 s, and `duration_s` measured input size rather than work. The clock now starts immediately before the call. The judge hands the function real objects, so that setup was never part of what is being timed.

### 5.5 Deterministic gate

Runs in order, cheapest first. Only a failed compile/import stops it: past that, **every step the inputs allow runs**, and the evidence list is complete rather than truncated at the first failure. Three reasons (round-10 L2): the timing steps need no oracle, so a differential failure must not suppress them — seven of ten round-10 solutions were asymptotically wrong and the system had timing data on none of them, because `stress` sat last in a short-circuiting chain; the per-tier mismatch counts are what `candidate_score` ranks candidates on, and half of them used to be missing; and a timing failure and a correctness failure want different responses (§5.7), which cannot be chosen if only one of the two was ever measured. The repair prompt stays focused anyway: `solve()` repairs the **first** failed step in the canonical order below, so a correctness failure is still repaired before a timing one. The cost is bounded — each differential tier is seconds, the expensive tier (medium) keeps its own budget rule, and every step turns itself into `skipped: no budget` once `step_timeout` returns 0.

1. **Static contract** — §6.
2. **Compile / import** — Rust: `rustc -O -C overflow-checks=on --edition 2021`. On a compile failure the `use ...;` lines rustc itself suggested are prepended and the compile is retried once (`sandbox.compile_rust_with_imports`, zero model tokens — two benchmark runs in a row lost a candidate to a missing `use std::io::Read;`); the patched source becomes the candidate's source, so it is what the rest of the gate, a later repair, and the emitted file all use. Python: `compile()` then import in a subprocess.
3. **Hand-traced examples** — the `===EXAMPLES===` block of the SOLVE reply: 3–5 `(args, expected)` pairs the solution's author traced from the statement by hand, run against that same candidate. No oracle is involved, so this is the one differential step a wrong reference cannot poison. Skipped (never failed) when the reply carried no parseable examples (§5.2.1) — and, alone among skipped steps, that skip does not demote the run's status: the absence of the block is prompt compliance, not evidence the gate failed to collect, and the "at least one real check ran" rule (§5.8) is what catches a run that verified nothing.
4. **Public examples** — `problem.public_examples`, compared directly, only present if the problem shipped any (§5.4.1).
5. **Edge cases** — the literal cases from STRESS, compared against `reference`.
6. **Differential, small** — 200 seeded cases from `gen(seed, "small")`, candidate vs `reference`.
7. **Differential, medium** — 20 seeded cases from `gen(seed, "medium")`.
8. **Behavioral contract** — §6.3 checks (mutation, global state, nondeterminism), run on the same cases.
9. **Stress** — `gen_max` input, release build (`overflow-checks=off`, for a realistic timing measurement), wall-clock limit (default Python 5 s, Rust 2 s, configurable; the real judge limits are unknown and this is recorded as an assumption).
10. **Overflow (Rust only)** — immediately after stress, so the timing run above happens first on a warm cache and this step can't mask a stress timeout. Reruns the same `gen_max` input on the overflow-checked binary already compiled by step 2 (reused, not recompiled). This is the only place a genuinely maximum-size input meets an overflow-checked build: step 2's checked build is only ever exercised by the small/medium differential inputs, which are far too small to overflow i64. A panic here (Rust prints `attempt to add with overflow` and similar) fails the step. **This does not check the output is correct** — the oracle is a literal Python reference and cannot produce an expected answer at maximum scale in reasonable time — it only proves no arithmetic operation overflowed. Skipped (not failed) when the problem is Python (arbitrary-precision integers, nothing to overflow), when there is no stress input, or when the budget is exhausted.

#### 5.4.1 Public examples: the one non-model-generated evidence source

`Problem.load` has always parsed `public_examples`, but until this fix nothing downstream consumed it — every sample in `samples/` ships an empty list, which is exactly why the gap went unnoticed. When a hidden problem does ship examples, they are the single most trustworthy evidence available: real input/output pairs from whoever set the problem, not a model's guess at either the algorithm or the ground truth.

Parsing is defensive by necessity — the shape is whatever the problem author chose, sight unseen. `verify._parse_public_examples` recognizes a dict with an input key (`input`/`args`/`in`/`params`) and an output key (`output`/`expected`/`result`/`out`/`answer`), or a bare two-element `[input, output]` pair; anything else is skipped with a note (`public_examples[i]: unrecognized shape, skipped`) rather than guessed at or allowed to crash the run.

These become `GateInputs.cases_public` — and they are treated differently from every other case tier in three ways:

- **Never validated.** They don't pass through the oracle's `validate()` (§5.5's input-validation paragraph): they're already known-good, and running an unrelated model-written predicate over them could only incorrectly discard real ground truth.
- **Never dropped on a `reference()` disagreement.** Every other tier throws away an input `reference()` can't produce an answer for. A public example is never thrown away — if the oracle's `reference()` disagrees with (or crashes on) a public example, `_check_public_against_oracle` reads that as evidence **the oracle is wrong**, not the example: it's recorded in `gate_inputs.notes` and attached to the `diff_public` evidence's `oracle_disagreements` detail, and the public example is still checked against the candidate exactly as given.
- **Run first.** `diff_public` is its own evidence kind, run in `run_gate` immediately after compile/import and before any generated tier, so a candidate that fails real ground truth fails fast on the most trustworthy evidence in the system, before spending time on model-manufactured cases.

With no public examples — the overwhelming common case — `cases_public` is empty and the `diff_public` step is not added to the evidence list at all; the gate is byte-for-byte what it was before this existed.

**Input validation, before steps 5–7.** Every input feeding those three steps (STRESS edge cases and both `gen` tiers) is first checked against the oracle's own `validate`, batched the same way as the `reference` pass. An input `validate` rejects, or raises on, is dropped as invalid before `reference` ever computes an expected output for it, so the differential never burns a case — or a repair attempt — chasing a candidate/oracle disagreement on input the statement itself forbids. Two safety rules keep this from doing more harm than good: if `validate` would reject *every* input in a tier, it is distrusted instead — all inputs for that tier are kept exactly as before, and the report records that the validator was skipped; and an oracle with no `validate` function at all behaves exactly as it did before this feature existed, with its own distinct skip reason in the report. This buys confidence that inputs reaching the gate satisfy the preconditions the statement states; **it does not buy correctness of `reference` itself** — an oracle whose `reference` misreads the statement can still be internally consistent with its own `validate` and pass every check.

Each step produces an `Evidence` record: kind, passed, case count, duration, and the failing input if any.

When there is no oracle source at all (both the first attempt and its one retry produced no `===ORACLE===` block), every differential/behavior/stress step is `skipped` rather than run, which makes `all_passed()` true even though nothing was actually checked. This is deliberately *not* turned into a candidate-repair failure — repairing the candidate cannot conjure an oracle, and it would burn both repair attempts for nothing — so the run's status is still the accurate `emitted_unverified`. What changes is the log: this case is logged as `gate.unverifiable`, naming why (from `gate_inputs.notes`), instead of `gate.passed`, so the log doesn't read as a success for a run that verified nothing.

### 5.6 Shrink

On a differential failure, a bounded greedy shrink (max 3 seconds): drop list elements, halve integers, shorten strings, drop trailing operations, keep the change only if the failure persists. A four-line counterexample makes the repair call both cheaper and more accurate than a 300-line one. Shrinking only applies to Python's structured arguments; for Rust, where the failing input is stdin text, `shrink` returns the case unshrunk and the repair prompt gets the original counterexample. A sequence that started non-empty is never shrunk to empty: the failure usually still reproduces with the list emptied, and the repair model, shown a counterexample in which the operation never happens, deletes the code that handles it (6e43a08ec05a). The greedy order — halves first, then single deletions — makes the kept witness the smallest non-empty one the loop can reach. A shrunk input is also checked against the oracle's `validate()` (when that validate is trusted, §5.3) before it is accepted: the shrinker knows nothing about the statement's preconditions, and driving an id to 0 or emptying a list manufactures an input the statement forbids. Such a step is rejected like one that stops reproducing the failure — otherwise the phantom drives the repair call and is kept as a regression case that fails every later candidate forever (1dea32802072).

### 5.7 REPAIR call (strong model, two independent budgets)

Input: statement, the current code, the gate step that failed, the shrunk input, expected vs actual (each shown as `repr()` *and* its Python type, so an empty tuple and an empty list — indistinguishable by eye, `()` vs `[]`, but not by type — can't be mistaken for the same value), and the time remaining. Instruction: smallest change that fixes the demonstrated failure; keep the public contract; do not switch algorithms unless the failure proves the algorithm wrong. The model is first required to name, in one sentence, the specific expression or line producing the wrong value, then change only what that sentence names — a whole-approach rewrite is almost never the right response to one failing input, and a model that believes the approach itself is wrong is told to say so rather than rewrite silently. When the candidate being repaired is itself the output of an earlier repair, the prompt says so explicitly: the earlier failing input, a diff of what that repair changed, and a flat statement that the change did not fix the case — so the model doesn't re-propose the same edit.

**Two counters, not one.** A gate failure of kind `static` or `compile` is mechanical (a missing import, a missing `mut`) rather than a semantic defect, and is charged against `[limits].max_syntax_repairs` (default 2). Every other failure kind (`diff_*`, `behavior`, `stress`, `overflow`) is charged against `[limits].max_repairs` (default 2). The two budgets are independent — a run can spend up to `max_syntax_repairs` fixing compile errors *and* up to `max_repairs` fixing an algorithmic bug, but a `static` failure can never crowd out a semantic repair attempt or vice versa. Both counts (`repairs`, `syntax_repairs`) are in the report. The overall repair loop is still bounded by the same phase and cost checks (`run.budget.phase()`, `max_cost_usd_per_problem`), and by one budget rule that now follows the configured model rather than a literal. **A repair call is capped at `verify.repair_cap` — the larger of `[phases] repair_call_share * usable` and the strong role's own `repair_cap_s` (a measured model latency, the one absolute second-count in this path, §4.1) — and is started only while `budget.remaining()` covers that cap plus one gate pass (`[limits] gate_pass_frac × usable`); otherwise the loop logs `repair.skipped reason=budget` and the call is never made.** The fraction alone gave bench16 a 67 s repair against a role whose completed solve that run took 168 s: the call could only time out, and the 67 s were spent proving it. The same rule governs a fresh solve, with the full generate cap as its price. **A repair call that errors — transport failure, 4xx/5xx, timeout, or a reply with no marker block — yields no verdict at all**: it is logged as `repair.error`, mints no child, and the loop stops as if the repair had been skipped. It used to fall through to the default verdict `candidate` ("the reference is right"), which on bench16-1dea was recorded for a 403 on a run whose oracle was the wrong side.

**The thinking budget follows the call's timeout — on a provider that honours it.** The optional per-role `reasoning_budget = { tokens_per_s, answer_reserve_s, min_tokens, max_tokens }` turns a call's own `timeout_s` — already the deadline's share for this phase — into an amount of thinking: `llm.chat` computes `N = clamp(tokens_per_s × (timeout_s − answer_reserve_s), min_tokens, max_tokens)` for that one call, sends `reasoning = { max_tokens = N }` overriding any `effort` the role's `extra` carries, and records `reasoning_max_tokens` in the call record beside the tokens actually spent. It is the right shape of knob — a fixed effort is a statement about the *model* while the thing that binds is the *deadline* — but it only works where the provider treats the number as a ceiling. **It does not bind on `anthropic/claude-opus-5` through OpenRouter.** A probe asking for 3 000 reasoning tokens inside a 3 600-token call returned `finish_reason = length` with `reasoning_tokens = 3600` and zero content, and a live run with a computed budget of 11 363 thought for the entire 239 s cap and emitted no block: the number is advisory there, and the model stops thinking when it is done or when `max_tokens` runs out — and if it is the latter, the answer never gets written. Only the *effort* levels bind on Claude 5, and of the three the provider's classifier refuses two (`low` 4 of 4 on one problem's prompt, `minimal` likewise; `medium` passed 1 of 1), so the OpenRouter strong role carries `extra = { reasoning = { effort = "medium" } }` and no budget. Medium fits: 152–196 s on the focus problem and 167–173 s on two others in round 14 against a 240 s `timeout_cap_s`, cut at that cap in 3 of 10 runs — the reason the effort had been lowered to `low` on 2026-09-15, before `low` was measured as refused. What makes medium survivable is CODE-first plus salvage (§5.2, §5.8): a reply cut at the cap still carries the code if its `===CODE===` block closed, so a cut costs the tail of the reasoning rather than the run. At Opus's measured 71 tokens/s, 240 s of output fits inside the role's 24 000-token cap — the clock binds, not the cap. The budget mechanism stays in `llm.py` for a role whose provider does honour a token ceiling, and `Role.tag_extra` (a TOML table per role, keyed by prompt tag, merged over `Role.extra` for that tag's request only) remains for a tag that must pin a *shape* of its own — an explicit `reasoning` there wins over a budget — and is what a fresh solve's tag (`solve_fresh`) rides on through `Run.chat`, the log, `report.json`'s `calls` and the `FakeLLM` script. The strong role sets no per-tag entries: the lower effort a post-gate call would drop to is exactly the one that is refused, so what bounds a repair is its own timeout (`repair_cap_s`), not a smaller effort. Which knob a provider takes stays in role config, never in a branch in `llm.py`: the fast role keeps `effort = "low"` by simply not setting a budget.

**Two candidates agreeing is a dispute, and it comes before any repair.** When the candidate that would go to repair failed a differential tier at the same input index, with the same answer, as another candidate from a different lineage (`verify.candidates_agree`: roots and fresh solves are different authors, a repaired child is not a second author of its parent's reading, and a crash or timeout — which the evidence renders as `(no answer: …)` — is never an agreement), that is two independently sampled readings of the statement against the reference's one. It is recorded as one value disagreement in `example_checks`, in the same shape as a hand-traced example and flagged `from_candidates` so no prompt calls it a hand trace — so it can tip `examples_disputed` and tell a regeneration's cross-check which of the two references was the wrong one. And it takes the **oracle regeneration path before any candidate repair**, subject to the same one-regeneration budget, carrying only the case as evidence (the input, the value both solutions produced, the value the reference produced — never any code). The candidate is then re-gated against the replacement: if it agrees with them the failure is gone, and if it still disagrees the next pass repairs the candidate exactly as before. The lookup is disabled once a regeneration has happened, because the other candidate's evidence then refers to a different oracle's case list and its index means something else. On bench16-1dea both candidates produced identical output on the failing input, a hand-traced example contradicted the oracle on the same rule, the oracle had one wrong conjunct — and the repair was aimed at the candidates. Logged as `oracle.dispute two_candidates_agree kind=… index=… authors=…`.

**Adjudication.** When candidate and oracle disagree, the oracle may be the wrong one. The repair prompt is told this and asked to hand-trace the shrunk case against the statement and say which side is wrong. If it names the oracle, `verify.regenerate_oracle` is called with the disputed input and what the current reference answered on it — **and nothing else**. The repair model's own prose about the candidate used to be appended here; on 1dea32802072 the regenerated oracle then inherited the candidate's exact bug and agreed with the wrong candidate on 4000/4000 inputs. The oracle is generated in its own context and never sees the candidate, so the extra context is the input, the previous answer, and one neutral sentence asking for the expected output to be re-derived from the statement alone. The call increments `GateInputs.oracle_regens` and fires at most once; when the verdict is `oracle` and no regeneration is left (already used, or it failed), the loop logs `repair.oracle_verdict_unactionable` and stops instead of adding the repair's deliberately-unchanged CODE block as a new candidate (1c182498c9c7 emitted such a candidate). Regeneration is given the same generous timeout as the original ORACLE call — derived from `[phases].generate_call_share`, not a separate smaller fraction — because it writes the same `reference`/`gen`/`validate` functions and is just as likely to need the full span; `step_timeout` still caps it to whatever budget remains. This is the same `regenerate_oracle` function §5.3.1 uses for self-repair, parameterized by which counter to increment (`counter="oracle_regens"` here vs. `"oracle_selfrepairs"` there) so the two one-shot budgets never collide — both can fire, once each, in the same run.

A regenerated oracle is then cross-checked against the one it replaces: the new `reference()` reruns the old `cases_small` inputs and the disagreement is logged as `oracle.regen disagreement=k/n` and noted. Above `[limits] oracle_regen_max_disagreement` (default 0.5) the run has no ground truth left — two references written from the same prose cannot both be near-correct while disagreeing on most tiny inputs — so every `diff_*` evidence produced against the new oracle is marked degraded and the status can no longer be `passed_all_gates`.

The repair prompt also carries two things it lacked: how wholesale the disagreement is (`mismatches` of `cases`, measured on the failing tier, or against `cases_small` when the failure was a single edge/public case) — a disagreement on most inputs is a different reading of the statement, not a boundary bug — and the reference source itself, which the prompt asks it to trace, labelled as possibly-wrong and never to be copied. A `stress` failure that is a crash also carries the head of the max-size input, so the panic has a format and a scale attached to it.

After every repair the full gate reruns, and every previously failing case is kept as a regression case.

#### 5.7.1 Fresh solve: the failures a patch cannot fix

Three failures are routed away from the patch-style repair and into one more SOLVE call that starts
over from a **different algorithm**, carrying the previous attempt and the concrete failure that
ended it (`solve.fresh_solve`, `[limits] max_fresh_solves`, default 1):

1. **A timing failure** — the `stress` step measured the candidate too slow (or timed out) on a real
   max-size input. A smaller edit cannot change an approach's complexity, so this never goes to
   repair. A *degraded* stress input is excluded: its timing is not a max-size measurement.
2. **`===VERDICT=== approach`** — the repair model itself says no patch of this code can meet the
   stated limits, or the algorithm is wrong at its core. `repair.md` asks it to return the current
   code unchanged with that verdict, so there is nothing to mint as a child.
3. **A repair that regressed** — a repaired child that passes fewer gate steps, or fails more, than
   the parent it came from (round-10 L1). The lineage is closed (`repair.regressed child=cX
   parent=cY`); the parent already wins emission on score. The comparison deliberately ignores
   `candidate_score`'s mismatch totals, because the child was gated against its parent's case set
   *plus* the regression case the parent's failure produced, so the two totals are over different
   inputs.

The prompt is `solve.md` with its optional `{{previous_attempt}}` section filled in (it is empty
everywhere else): the previous attempt's `===DESIGN===` block and its code, then the failure —
input/expected/actual with the source of *expected* named for a differential failure, and for a
timing failure the input's **shape** rather than the input (`verify.describe_input`: per argument,
type, length, nesting depth, largest integer magnitude, and the operation-kind histogram when it is
a list of records) plus the measured duration against the limit. The max-size input is megabytes;
pasting it would crowd out the statement, and the shape is what says which quantity the next
algorithm has to be cheap in.

The result is a **root** candidate (`parent` is `None`, `replaces` names the attempt it answers, both
in the report): it is not a repair of anything, it is gated like any other attempt, and it competes
for emission through `candidate_score` on its own evidence. A fresh solve costs one strong call plus
a gate pass on its result, so it starts only while `budget.can_afford(generate cap + gate_pass_frac × usable)`, the
cost cap allows it, and `max_fresh_solves` is not yet spent; otherwise the loop stops and the best
candidate so far is emitted.

Why it exists: with a fixed model, a re-solve carrying the concrete failure is the only path to a
second algorithm. Round 13 produced four independent candidates for one statement, all asymptotically
wrong in the same way, and every repair was a variant of the same approach (G4).

### 5.8 Finalize

The finalizer picks the candidate that passed the most gate steps, then the fewest mismatches and failures, then — among candidates whose evidence is otherwise identical, which is the common case since most gate steps are pass/fail — the one with the **smaller measured max-size stress time**, and only then the earlier attempt. The timing tie-break is applied only when every tied candidate has a real measurement (the step ran, passed, and its input was a validated maximum; a degraded, skipped or sentinel duration is not one), so a candidate never wins or loses on the absence of evidence. bench15 emitted the first of two byte-identically-scored candidates purely by attempt order; the other takes 8–12 s on legal maximum-size inputs where the emitted one takes 0.74 s. It writes `report.json` before the solution file, so a failure writing one never loses the other. A candidate that fails to compile (Rust) or fails a gate step is still written — that scores 0 either way, and a missing file is worse than a wrong one — and the run exits 1. A run exits 3 with `status: "no_candidate"` whenever no usable candidate exists, including empty/malformed/refused replies or a timeout whose CODE block did not close. A non-empty non-partial reply without a CODE marker is still used verbatim as a last-resort candidate, so that case can emit a file and be reported as a gate failure.

---

## 6. Contract and behavior checks

### 6.1 Python static

- `ast.parse` succeeds; the entrypoint is a top-level `def`.
- Every import is in `sys.stdlib_module_names`.
- No `open`, `input`, `print`, `exec`, `eval`, `compile`, `sys.stdin`, `sys.stdout`, `os`, `subprocess`, `socket`, `random`, or other forbidden I/O/process imports.
- Recursion smell: a function that calls itself over the input structure triggers a warning that feeds the stress step; `sys.setrecursionlimit` is allowed but the stress input must still pass.

### 6.2 Rust static

- `fn main()` present. No `extern crate`, no `unsafe`, no `std::fs`, `std::net`, `std::process::Command`, `std::env::args`.
- The static pass does not try to prove collection iteration order; the two-process nondeterminism check in §6.3 catches output that actually varies.

### 6.3 Behavioral (both languages, run inside the gate)

- **Mutation:** deep-copy the arguments, call, compare. Failure is a hard fail for Python problems.
- **Global state:** in one process call A, then B, then A again; the two A results must match.
- **Nondeterminism:** run the differential batch in two separate processes; results must match. Rust `HashMap` seeds differ per process, so this catches order-dependent output.
- **Overflow:** Rust builds run with `overflow-checks=on` for compile and the small/medium/edge differential tiers, and separately, right after stress, the same checked binary is rerun against the max-size `gen_max` input (§5.5 step 8) — the only overflow check that runs at a scale large enough to actually trigger a silent i64 wrap. A panic is an overflow finding with the input attached; the arithmetic result itself is still not checked for correctness at that scale.

---

## 7. How the three README questions are answered

### 7.1 How traps are found and handled

- The SOLVE prompt carries the §2.1 trap checklist and must address each class explicitly against the stated bounds before writing code.
- The stress gate turns "would this be too slow" from an opinion into a measurement.
- The medium-magnitude differential tier exercises the closed-form and structural shortcuts the traps force, at sizes where the literal oracle can still confirm them.
- Overflow checks, mutation checks, and the deep-recursion stress input catch the trap classes that are invisible to small tests.

### 7.2 How a solution is verified without public examples

- An independent literal oracle written from the statement alone, in Python, in a separate context.
- Every generated and literal input is checked against the oracle's own `validate` predicate before it is used, so an input the statement itself would call invalid never reaches a differential comparison (a validator that rejects everything is distrusted and ignored; an oracle with no validator behaves exactly as before). This discards inputs the statement rules out — it does not make `reference` itself correct.
- Seeded differential testing at small and medium magnitude, plus literal edge cases.
- Behavioral contract checks for mutation, state, and determinism.
- A max-size stress run under a wall-clock limit.
- For Rust, the same max-size input rerun on an overflow-checked build, to catch a silent i64 wrap that timing-only stress can't see — this proves the absence of overflow, not the correctness of the result (the oracle can't produce an expected answer at that scale).
- Adjudication when candidate and oracle disagree, with the oracle regenerated at most once.
- The report lists which layers passed, which failed, and which could not run. It never says "verified"; it says what was checked.

### 7.3 What happens when time runs out

The controller is a monotonic clock, and every step is given `min(step_cap, remaining - reserve)`.

1. **Entering the repair window with a failing candidate:** a repair continues only while its configured cap (`max(repair_call_share × usable, repair_cap_s)`) plus one gate-pass estimate fits; syntax and semantic repair counters are independent.
2. **Entering settle:** no new model calls are made and no candidate is re-gated; the last gate result for each candidate stands as-is, and the finalizer picks the best of what already exists.
3. **Entering emit:** whatever candidate ranks best is written, even if it failed differential testing, because an unverified answer has nonzero expected value and a missing file has none. The report records exactly which gates it failed.
4. **A model call overruns:** the call stops being waited on at its timeout. A streamed prefix is retained; a SOLVE/REPAIR reply is usable only when its CODE block closed, otherwise it is discarded. The orchestrator proceeds with whatever candidates exist. The strong SOLVE call is the only initial role that can create a candidate, so it gets the largest share and is gated as it lands.
5. **A subprocess hangs:** killed by process group at its timeout; treated as a stress failure.

Candidate files are retained for inspection. A repaired child that fails to compile remains in the report, but
the finalizer normally selects the best-scoring parent; an emitted candidate can still have gate failures.

---

## 8. Cost

### 8.1 Model roles

| Call | Current role/profile | Default and optional requests | Token accounting |
|---|---|---|---|
| SOLVE | `roles.solve` → `strong` | 1 by default; at most one no-block retry; one fresh solve may be added after a timing/approach/regression failure | actual prompt/completion usage in `report.json`; reasoning tokens count when the provider reports them |
| ORACLE | `roles.oracle` → `fast` | 1; at most one missing-block retry, one self-repair, and one adjudication regeneration | same; each optional request is cost/remaining-budget checked |
| STRESS | `roles.stress` → `fast` | 1 | same |
| REPAIR | `roles.repair` → `strong` | 0–2 semantic repairs plus 0–2 syntax/compile repairs | same; transport/refusal retries can add attempts |

There is no fixed six-call/60k-token ceiling in the implementation. Per-role `max_tokens`, transport retry settings,
the optional-call counters, and `[limits].max_cost_usd_per_problem` are the practical bounds. Monetary cost is
computed from provider `usage.cost` when available, otherwise from configured per-million input/output prices;
if no prices are configured the report says `cost unavailable` and the cost cap cannot protect the run.

**Which models.** Both roles are now *thinking* models, at different sizes: the strong role is `anthropic/claude-opus-5` through OpenRouter (`max_tokens = 24000`, thinking at `effort = "medium"` — the only level the provider's classifier accepts, and a token budget does not bind on this model; see §5.7), the fast role `anthropic/claude-sonnet-5` at `effort = "low"` (`max_tokens = 12000`). The initial solve ran at `medium` until it was measured cutting off near the 240 s cap with no complete `===CODE===` block — 239.4 s on 5cb294c18288 — then at `low`, then at a computed token budget, and is back at `medium`: `low` turned out to be refused by the classifier and the token budget turned out not to bind, while CODE-first salvage made a cut reply survivable (§5.7). Rounds 10–15 settled why. On 5cb294c18288 the coder model in the strong role read the *semantics* correctly — the emitted candidate matched an independent reference on 8 366 differential cases and six hand traces — and never once read the *complexity*: four candidates for that statement (two independent solves, two repairs) all materialized a layout the statement calls "enormous" and all iterated a count it bounds by 10^18, so the emitted solution needs ~10^11 s at the stated limits. No prompt or gate change reaches that; connecting "enormous" to "do not build the list" is the reasoning the strong role exists to buy. The earlier finding that no reasoning model returns inside a 300 s deadline was measured with `max_tokens = 12000`: reasoning tokens count against `max_tokens`, so the thinking spent the whole budget before any `===CODE===` block was written and the call returned nothing. The budget now covers thinking *and* answer. Oracle/candidate independence — the reason the two roles were put on different models after a round where both ran the same one — is carried by generation context rather than by model identity: the oracle is written in its own call, from the statement alone, and never sees the candidate. `[limits] max_cost_usd_per_problem` is 1.50 accordingly: at $5/$25 per 1M the cap must bound a runaway, not stop the second repair of an ordinary run.

**Which prompt goes to which role is config too.** `config.toml`'s top-level `[roles]` table maps prompt name → role, read once through `Run.role()`; a config without the table gets `solve.PROMPT_ROLES`, and the shipped table is now that same mapping: SOLVE and REPAIR to the strong role, ORACLE and STRESS to the fast one.

Extension points are configuration-only: add another `[profiles.<name>.<role>]` with an OpenAI-compatible
endpoint/model/key, token parameter, temperature/streaming behavior, concurrency, timeout and pricing; map
`solve`, `oracle`, `stress`, `repair`, and `fallback` in `[roles]`; and tune `[limits]`, `[grants]`, and
`[phases]`. Per-role `tag_extra`, `refusal_extra`, `reasoning_budget`, `repair_cap_s`, and
`usage_lookup_url` cover provider-specific request shapes without branches in `llm.py`. The shipped
`anthropic` and `openai` profiles are configured but their latency is not benchmarked here.

The two measuring calls have two requirements at once, and round 15 measured a model failing each of them. They must **read the statement** — the oracle is the ground truth of the entire system, every differential tier is worth exactly what the reference is worth, and no prompt or gate change reaches a reference with no code path for half the statement. And they must **return fast**, because `prepare_gate_inputs` cannot start before the ORACLE reply lands (§5.3), so the oracle's latency is dead time at the front of every run unless it finishes well inside the solve call's own 152–240 s.

`qwen/qwen3-coder` returned in 17–24 s and failed the first requirement: in bench19 and bench20 its oracle was the primary or the only remaining cause of an unverified run — a `reference()` that rejected half of what its own `gen()` produced, 192 of 200 small answers identical because `gen()` invented identifiers instead of drawing them from the state it had just built (so a 200-case tier asserted one fact), and a regenerated replacement that came back unusable. Both prompts were therefore moved to the strong role, which fixed the reading and broke the overlap: in bench21 the Opus oracle at *low* effort took 155 s, so preparation began at 155 s instead of ~18 s, and the three concurrent calls exactly filled the strong role's `max_concurrent = 3`.

So the prompts are back on the fast role, and the fast role is now a model that does both: `anthropic/claude-sonnet-5` at low effort — the same family's reading of the statement at roughly a third of Opus's latency and a fifth of its price, with `timeout_cap_s = 150 s` as a ceiling rather than an expectation. The strong role's `tag_extra` lost its `oracle` and `stress` entries with them, and later the post-gate ones too, once the thinking budget made a per-tag effort redundant (§5.7). Everything the move to the strong role bought is kept in the prompts themselves, which are unchanged: the STRESS author still states its expected answer and checks its own bounds in code, and the ORACLE author still writes `reference`/`gen`/`validate` from the statement alone.

**Transport.** Every call goes over one OpenAI-compatible HTTP client and every provider difference lives in the role's config, never in a branch in `llm.py`: `token_param`, `omit_temperature`, `price_in_per_m` / `price_out_per_m`, and now `stream` (default true) and `stall_timeout_s` (default 30 s). Replies are streamed as SSE — `delta.content` and `delta.reasoning` (OpenRouter's spelling; `reasoning_content` is accepted too) accumulated into exactly the shape a non-streamed reply has, with `usage` taken from the final chunk (`stream_options.include_usage`, or OpenRouter's top-level `usage` on the last chunk). The reason is not latency but detection: a connection that stalls mid-answer is otherwise indistinguishable from a model that is still thinking, and the call burns its whole timeout before the existing transport retry can fire. With the body read line by line, the socket timeout *is* the stall detector — no bytes for `stall_timeout_s` raises, the retry treats it as any other transport error, and the call's own `timeout_s` still bounds the whole attempt. A provider that cannot stream sets `stream = false` for that role.

**A stream that answers nothing.** Two failures arrive as a perfectly healthy 200 stream and used to be indistinguishable from a model that simply had nothing to say. The first is an **empty stream** — no content, no reasoning, no usage, `error=None` in 2.49 s — which is a connection that died quietly; it is treated exactly like any other transport failure, retried inside the same call with the same backoff and the same bound by the call's own `timeout_s`. The second is a **provider error delivered mid-stream** (`{"error": {...}}`, which OpenRouter sends when the upstream it routed to fails): the message reaches the call record truncated to 200 characters, and it follows the same rule the HTTP status branch does — retried on 429, on 5xx and on a code that cannot be read, never on any other 4xx. Neither is a branch on provider name: both are OpenAI-compatible SSE fields.

**A refusal is not a failure to retry.** The third shape is a **refusal**: `delta.refusal` text with empty content, then `finish_reason = "content_filter"` (`native_finish_reason = "refusal"`), then `[DONE]`. Measured live, anthropic/claude-opus-5 refused this tool's own SOLVE scaffold — "blocked as it seems to violate Anthropic's Terms of Service restrictions on reverse engineering or duplicating model outputs" — with the only difference from a prompt that passed being the entrypoint *name*: a classifier false positive on the scaffold, not a judgment about the problem. It is deterministic (3 of 3 with `reasoning: {effort: "low"}`), so retrying the same request verbatim only burns the deadline; `llm.chat` returns `refused = True` with `error = "refusal: …"` and does **not** retry. What changes the outcome is the request's shape, and `solve.Run.chat` climbs exactly two rungs, both bounded by what is left of the call's budget and by the cost cap. The first is the same call sent with the role's new optional `refusal_extra` table, merged *shallowly* over `extra` so its `reasoning` key replaces the role's whole reasoning table rather than merging into it, logged `call.refused tag=… retry=refusal_extra`. What goes in that table was measured on the refused prompt and is narrower than it looks: `effort: "low"` refused 4 of 4 and `"minimal"` refused too, so lowering the effort is not a rung at all; `{max_tokens: 4000}` passed, `{effort: "medium"}` passed, and omitting the `reasoning` key passes the classifier but switches the model to *adaptive* thinking — one live run at that rung thought for its entire 239 s budget (4,449 tokens of reasoning summary, no content) and shipped nothing. Since a token ceiling does not actually bind this model (§5.7), that leaves no retry shape worth sending, and the openrouter strong role sets no `refusal_extra` at all. The second rung is the role named by `[roles] fallback` (`"fast"`, and no fallback at all if the key is absent), logged `retry=fallback_role` — neither claude-sonnet-5 nor gpt-5.6-terra reproduced the refusal. A role with no `refusal_extra` has no first rung and goes straight to the fallback; a refusal after the last rung returns the error reply. Every call goes through this path, so ORACLE and STRESS get the same ladder as SOLVE.

**What a cut-off call really cost.** A stream abandoned at its timeout never reaches its usage chunk, and the only figure left is `len(buffer) // 4` — which ignores reasoning tokens entirely and is therefore wrong by an order of magnitude for a thinking model: bench17 and bench18 reported $0.12 between them while the key's usage rose by $0.32. An optional per-role `usage_lookup_url` (`https://openrouter.ai/api/v1/generation?id={id}` for OpenRouter; empty for every provider that has no such endpoint) closes it. When a call ends `partial`, or its stream carried no usage at all, and the first streamed chunk carried a generation `id`, `llm._lookup_usage` issues **one** GET with the call's own auth header, 10 s, never retried; its `total_cost` and token counts replace the estimate in the call record and the `estimated` flag comes off, and the run logs `cost.lookup id=… cost=…`. Any failure is ignored and the flagged estimate stands — a cost figure is a report line, never a control decision inside the call. The response is read leniently (`data` wrapper, `tokens_prompt` / `tokens_completion` / `total_cost`), so this stays a config field and not a branch on provider name.

**Salvaging a cut-off reply.** Because the body is streamed, a call abandoned at its own `timeout_s` is not empty: whatever the model had already sent is in the caller's accumulator (one per attempt, so an abandoned thread can never write into a later call's buffer), and the reply comes back with `partial = True`, `error = "timeout"` and that text. Whether it is *usable* is a structural question, not a judgment: `llm.terminated_blocks` reports which blocks closed with their own `===END===`, and `llm.salvageable(reply)` accepts a partial reply only when `===CODE===` is one of them. The prompts are ordered for this — SOLVE asks for CODE first and EXAMPLES/RULES/DESIGN/TRAPS after it (§5.2), REPAIR puts VERDICT before CODE — so a cut in the later blocks costs only the record of the reasoning, while a cut inside CODE leaves truncated source and is discarded (the raw-reply fallback is suppressed for a partial reply, or it would write that truncation out as the solution). A salvaged SOLVE reply is built into a candidate and gated normally, logs `solve.partial cand=… salvaged_blocks=[…] missing=[…]`, and sets the report's `solver_status` to `"partial"`; its missing EXAMPLES block simply skips the `diff_examples` step. Every partial call record also carries `partial_blocks = {complete: [...], cut_in: "<block or none>", chars: N}` — which blocks closed, the one the cut landed in, and how much text arrived — so `report.json`'s `calls` says how far a cut reply got rather than only that it was cut, and a benchmark table can tell "20 s short of finishing" from "still writing rules at the cap". It is derived from the same block regex as `terminated_blocks`, knows nothing about any prompt's order, and nothing reads it to make a decision — `salvageable` is what decides. Usage on such a call is whatever the stream reported before the cut; when no usage chunk arrived the tokens are estimated from the buffer length (≈4 characters per token) and the call record is flagged `estimated`, so no cost total reads it as measured. bench17 is what this is for: one Opus attempt spent 199.5 s of a 200 s cap, and the run shipped `no_candidate` with a complete solution in the discarded buffer.

### 8.2 Levers

- Bounded optional work: 2 semantic repairs + 2 syntax repairs, 1 fresh solve, 1 oracle self-repair, and 1 oracle adjudication regeneration; initial/no-block/retry/fallback requests remain subject to budget and cost checks.
- Default cost cap: `$1.50` per problem for the shipped OpenRouter profile; per-role token caps are `24,000` strong and `12,000` fast, and actual provider usage is recorded per request.
- The statement and trap checklist are the shared prompt prefix on every call, so provider prompt caching applies where available.
- The gate is free. Every token-spending step is preceded by a free step that can end the run early.
- Fast-model calls never touch the algorithm. Strong-model calls never generate test scaffolding.

---

## 9. Sandbox

The Python harness enforces a per-case wall limit (`[limits] per_case_limit_s`) and abandons the rest of a batch after `[limits] max_consecutive_case_timeouts` consecutive per-case timeouts, reporting those cases as skipped without running them: a reference or candidate that times out on five small inputs in a row is dead, not slow, and every further case costs the full per-case limit for nothing (on 6eca8a9120e0 a reference timing out on 40 of 92 small inputs burned the batch's whole 60 s grant and left the gate starting at 244 s of 285).

Generated code is untrusted. Every execution:

- runs in a fresh temporary directory in a separate process group;
- receives `PATH`, `HOME` (the real user home, needed by the rustc proxy) and `LANG`, nothing else;
- gets a wall-clock timeout, capped stdout/stderr, and a memory cap (`RLIMIT_DATA`, `RLIMIT_AS` where the platform supports it) from `config.toml`'s `[limits] mem_mb`, threaded through to both the Rust binary and the Python harness process — `sandbox.run_python_cases` takes `mem_mb` and forwards it to `run_cmd` exactly as the Rust path already did, so a changed `mem_mb` actually changes Python candidate behavior instead of silently running at `run_cmd`'s hardcoded default (invisible before this fix only because the two numbers happened to coincide);
- receives arguments as an array, never through a shell;
- is killed by process group on timeout and cleaned up.

**Python harness.** A trusted wrapper imports the candidate module by path and calls the entrypoint with arguments deserialized via `ast.literal_eval` from `repr` text. Results are written back the same way, so every fixture in the run directory is readable and re-runnable by hand.

**Rust harness.** Compile once per candidate hash (release with overflow checks for the gate; release without for the stress timing), then run each case with exact stdin bytes and compare token lists.

This is process isolation, not a hardened sandbox. Containers are a documented upgrade, not a dependency.

---

## 10. Repository layout

```text
challengebox/
├── README.md              # setup, one command, assumptions, limitations
├── ChallengeBox-Agent-Architecture.md # this document
├── solve.py               # CLI, orchestrator, budget controller, finalizer
├── llm.py                 # one OpenAI-compatible client, any provider by config: structured output, timeout, usage capture
├── sandbox.py             # python/rust runners, static contract checks
├── verify.py              # differential, medium tier, behavioral checks, stress, shrink
├── prompts/
│   ├── solve.md
│   ├── oracle.md
│   ├── stress.md
│   └── repair.md
├── config.toml            # models, prices, limits, safety margin
├── tests/
│   ├── test_budget.py     # fake clock, grants, phase transitions
│   ├── test_config.py     # profile and fallback configuration
│   ├── test_llm.py        # HTTP/SSE, retries, roles, parsing, salvage and cost lookup
│   ├── test_sandbox.py    # contract checks, timeouts, cleanup, both languages
│   ├── test_solve.py      # orchestration, retries, status, repair and report behavior
│   └── test_verify.py     # differential, shrink, oracle/stress trust, behavior and gates
├── samples/               # provided problems, used as the benchmark suite
└── runs/                  # gitignored artifacts
```

Module map: `solve.py` owns CLI dispatch, `Budget`, `Run`, concurrency, candidate lineage,
finalization and benchmark output; `llm.py` owns profile loading, OpenAI-compatible transport,
stream/retry/refusal handling, marker parsing/salvage and `FakeLLM`; `sandbox.py` owns problem loading,
static contract checks, isolated Python/Rust execution, compilation and resource limits; `verify.py` owns
`GateInputs`/`Evidence`, oracle/stress preparation, input validation and respelling, differential/behavior/
stress/overflow gates, shrinking, repair prompts and oracle regeneration. `prompts/` supplies the four model
contracts; `config.toml` supplies profiles, prompt-role routing, limits, phase shares and grants.

Four Python modules plus configuration. A plugin system, dashboards, and a separate provider SDK are deliberately absent; add them when a reviewer asks.

The concrete ownership and artifact flow is:

```mermaid
flowchart LR
    I["problem JSON"] --> O["solve.py<br/>orchestrator + Budget"]
    C["config.toml"] --> O
    P["prompts/*.md"] --> O
    O --> L["llm.py<br/>model calls"]
    L -->|candidate code| V["verify.py<br/>verify gate"]
    L -->|oracle reference + fixtures| V
    L -->|stress generator + cases| V
    V -->|candidate / oracle / stress cases| S["sandbox.py<br/>compile + isolated execution"]
    S -->|execution results| V
    V -->|evidence + selected candidate| O
    O --> R["runs/&lt;run&gt;/<br/>report.json + artifacts"]
    O --> E["emitted solution"]
```

### 10.1 CLI

```bash
python solve.py samples/<id>.json -o out/solution.py
python solve.py samples/<id>.json -o out/main.rs --deadline-scale 0.25 --run-dir runs/debug
python solve.py --bench samples/ -o out/          # runs all, writes a summary table
```

Other flags: `--profile <name>` forces a `config.toml` profile (default: the first whose API key is set) and `--config <path>` a different config file;
`--run-dir` overrides where run artifacts land. There is no `--seed` flag — cases are generated with
`range(n)`, not a configurable seed.

Exit codes: 0 solution emitted and passed all gates; 1 solution emitted but is unverified or has gate failures (details in report, including a non-compiling Rust candidate); 2 invalid input or a missing API key; 3 no candidate could be emitted (for example, empty/malformed/refused replies or an unsalvageable timeout).

---

## 11. Run report

The default run directory is `runs/<problem_id[:12]>-<YYYYmmdd-HHMMSS>/`; `--run-dir` uses the exact directory
supplied. `report.json` contains problem id/language/profile/deadline, candidate ids and parent/replacement
links, every model call's role/model/latency/usage/error flags, gate evidence, token totals, cost/cap,
gate-input summary, events, and final status. Candidate sources, raw prompts/replies, oracle/stress fixtures,
and parsed products live beside it rather than being embedded wholesale in the report. No API keys or
environment contents are persisted.

`runs/<problem_id>/replies/` holds both sides of every model call verbatim — `<tag>-<n>.prompt.txt` and `<tag>-<n>.txt`, `n` counting that tag's calls — salvaged partial replies included. Everything else in a run directory is the *parsed* product of a reply (`candidates/c1.py` is the `===CODE===` block and nothing else), so whatever a parser dropped used to be unrecoverable: bench19 logged `parsed=3 dropped=2` example lines and two adjudications could only infer which two and why. The API key is a request header in `llm.chat` and is never part of a rendered prompt or a completion, so nothing secret reaches these files.

A run also emits a one-line-per-event log:

```text
[00:00.2] intake        lang=rust deadline=300 safe=285
[00:04.9] solve.sent    model=strong
[00:05.0] oracle.sent   model=fast
[00:05.0] stress.sent   model=fast
[00:41.3] oracle.done   tokens=1.9k/2.7k
[01:18.7] solve.done    tokens=3.1k/7.4k
[01:21.0] gate.compile  ok  overflow_checks=on
[01:29.4] gate.diff     FAIL seed=1842 case=73
[01:31.1] shrink        ops=4
[02:04.0] repair.done   c1->c2
[02:19.8] gate.diff     ok cases=200
[02:23.1] gate.medium   ok cases=20
[02:24.5] gate.behavior ok
[02:27.9] gate.stress   ok 0.81s
[02:28.3] emit          candidate=c2 status=passed_all_gates
```

---

## 12. Build order

Ordered by value, not by layer. Each step ends with a runnable check.

1. **Sandbox + contract checks** (`sandbox.py`): both languages compile and run a hand-written solution with timeouts and cleanup. Test: a hanging program is killed; a mutating function is caught.
2. **Budget controller** (`solve.py`): fake-clock tests prove emit happens before the deadline under every phase.
3. **One provider + SOLVE prompt** (`llm.py`, `prompts/solve.md`): one sample produces a compiling candidate.
4. **Oracle + differential + shrink** (`verify.py`, `prompts/oracle.md`): a planted off-by-one is caught and shrunk to a tiny case.
5. **Stress gate** (`prompts/stress.md`): a planted O(n²) solution is rejected.
6. **Repair loop + adjudication**: the planted bug from step 4 flows through shrink → repair → regression.
7. **Behavioral checks**: mutation, global state, nondeterminism.
8. **Benchmark all ten samples**; tune prompts from failures; write the README with the results table.

Steps 1–5 are the MVP. If time is short, step 7 is the first to cut, then step 6's adjudication.

The current runnable test suite is `uv run --group dev pytest -q` and covers all five modules above;
the latest HEAD benchmark notes report 350 tests passing.

---

## 13. Benchmark reporting

| Problem | Lang | Compile | Examples | Public | Edge | Small | Medium | Behavior | Stress | Overflow | Repairs | Syntax repairs | Calls | Tokens in/out | Elapsed s | Cost USD | Status | Hidden tests |
|---|---|---|---|---|---|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---|---|
| `<id>` | py/rs | pass/fail/n.a. | pass/fail/skip | pass/fail/n.a. | pass/fail/skip | pass/fail/skip | pass/fail/skip | pass/fail/skip | pass/fail/degraded/skip | pass/fail/skip | 0–2 | 0–2 | n | in/out | s | n/a | status | unknown |

"Pass" means passed the local gates. The hidden-test column is always "unknown" because the evaluator is not available. The README states this plainly.

The latest recorded live benchmark at HEAD (`60696ce`, round 17, 2026-09-15) covered five samples with
one run each under the current Opus-medium/Sonnet-low configuration. Independent adjudication judged all
five emitted solutions correct: two runs reported `passed_all_gates`, one was `emitted_unverified` because
the stress module failed to import, and two were `emitted_with_failures` because the generated oracle or
hand-traced examples were wrong. Counted runs took 131–242 s and about $0.13–$0.67 each; the Opus solve
latencies were 108, 216, 225, and one 239 s cut salvaged at CODE, while the Sonnet fallback solved the
refused case in 116 s. This demonstrates correctness potential, not a five-of-five certification of the
architecture: the remaining gaps are verification-source quality and late-budget affordability.

---

## 14. Known limitations

- **Shared misreading.** Oracle independence reduces, but does not eliminate, the chance that both the candidate and the oracle misread the same sentence. Adjudication and the literal-trace instruction are the mitigations; a wrong shared reading still produces a confident wrong answer.
- **Judge time limits are unknown.** The stress thresholds are guesses recorded in the report. A solution that passes locally at 4 s may fail a 2 s judge.
- **Large-magnitude correctness is still not verified.** Behavior at 10^18 is inferred from agreement at the medium differential tier (10^5); the max-scale stress input is only checked for absence of Rust integer overflow (§5.5 step 8) and wall-clock timing, never for a correct output — the oracle cannot produce an expected answer at that scale. A bug that only appears above the medium tier and does not manifest as an overflow (e.g. a wrong-but-non-overflowing formula) is not caught.
- **Process isolation only.** Generated code runs as the invoking user with resource limits, not in a container.
- **Model latency variance.** A slow strong-model response compresses the repair window; the controller adapts but cannot create time.
- **Rust stress timing includes process spawn overhead.** The measured duration covers process start, not just the candidate's compute; on this machine that overhead is roughly 0.5 s, which makes the effective Rust stress limit tighter than the configured `stress_limit_rust_s`.
- **The benchmark is not a full hidden-test result.** The latest live round covered five samples once and used independent adjudication; it did not run the evaluator's hidden tests, and three of five runs were not locally certified despite the adjudicators judging their emitted solutions correct. Earlier all-sample runs used older model/config combinations, so their pass rates are not directly comparable.

---

## 15. Open parameters

The README fixes the interface: a problem JSON in, a solution file out. The remaining unknowns are held in `config.toml` with defaults and are not blockers:

- judge CPU time limits (default: Python 5 s, Rust 2 s);
- Python and Rust versions (default: whatever is on `PATH`, recorded in the report);
- whether concurrent model calls are permitted (default: yes; a flag serializes them);
- model names and prices (default: one strong tier, one fast tier, prices unset → cost reported as unavailable).
