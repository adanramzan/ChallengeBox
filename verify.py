"""Verification: cases from the oracle, differential, behavior, stress, shrink, repair glue."""
from __future__ import annotations
import difflib, os, re, time
from dataclasses import dataclass, field
from sandbox import CaseResult, run_python_cases, compile_rust, run_rust_cases, tokens
from llm import parse_blocks

# Prepended to every generated oracle/stress source before it runs. The oracle prompt tells the
# model to *use* random.Random(seed) but never tells it to import anything, and one live run wrote
# a reference/gen module with no imports at all, dying with NameError on the first gen() call. A
# duplicate import is harmless if the model already wrote its own; the module list is exactly what
# the oracle/stress prompts assume is available. This adds exactly one line, and it is prepended to
# the same string that gets written to disk as cand.py (see sandbox.run_python_cases), so any
# traceback line number always matches that persisted file -- it never lies relative to the artifact
# a person would actually open, only relative to the raw text the model returned.
_STDLIB_PREAMBLE = "import random, math, itertools, collections, string, heapq, bisect\n"

@dataclass
class Evidence:
    kind: str
    passed: bool
    cases: int = 0
    duration_s: float = 0.0
    detail: dict = field(default_factory=dict)
    skipped: bool = False

@dataclass
class GateInputs:
    cases_small: list = field(default_factory=list)
    cases_medium: list = field(default_factory=list)
    cases_edge: list = field(default_factory=list)
    cases_public: list = field(default_factory=list)   # from problem.public_examples -- ground truth, never validated/dropped
    public_disagreements: list = field(default_factory=list)   # reference() vs a public example: evidence the ORACLE is wrong
    stress_input: object = None
    stress_degraded: bool = False
    oracle_src: str = ""
    oracle_regens: int = 0        # adjudication: repair blamed the oracle for a wrong answer
    oracle_selfrepairs: int = 0   # this module noticed its own oracle crashed and produced zero usable cases
    notes: list = field(default_factory=list)
    regressions: list = field(default_factory=list)
    dispute_trace: str = ""
    regen_failed: bool = False
    def summary(self) -> dict:
        return {"small": len(self.cases_small), "medium": len(self.cases_medium), "edge": len(self.cases_edge),
                "public": len(self.cases_public), "stress": self.stress_input is not None, "notes": self.notes}

@dataclass
class Case:
    input: object
    expected: object
    tag: str = ""

def _ref_args(problem, inp) -> tuple:
    if problem.language != "python":
        return (inp,)
    return tuple(inp) if isinstance(inp, (tuple, list)) else (inp,)

def make_cases(problem, oracle_src: str, inputs: list, tag: str, *, workdir: str, timeout_s: float) -> tuple[list[Case], Evidence]:
    t0 = time.monotonic()
    valid_inputs = inputs
    checked = invalid_dropped = 0
    validation_skipped = ""
    if inputs:
        val_res = run_python_cases(oracle_src, "validate", [_ref_args(problem, i) for i in inputs],
                                    workdir=os.path.join(workdir, "validate"), timeout_s=timeout_s)
        if val_res[0].error.startswith("IMPORT:"):
            validation_skipped = "no validate() defined in oracle"
        else:
            checked = len(inputs)
            kept = [i for i, r in zip(inputs, val_res) if r.ok and r.output is True]
            if kept:
                valid_inputs, invalid_dropped = kept, checked - len(kept)
            else:
                validation_skipped = "validate() rejected every input; distrusted, kept all"
    res = run_python_cases(oracle_src, "reference", [_ref_args(problem, i) for i in valid_inputs], workdir=workdir, timeout_s=timeout_s)
    cases = [Case(i, r.output, tag) for i, r in zip(valid_inputs, res) if r.ok]
    dropped = [r.error[-200:] for r in res if not r.ok]
    detail = {"dropped": len(dropped), "errors": dropped[:3], "checked": checked, "invalid_dropped": invalid_dropped}
    if validation_skipped:
        detail["validation_skipped"] = validation_skipped
    return cases, Evidence(f"oracle_{tag}", bool(cases), len(cases), time.monotonic() - t0, detail)

def run_candidate(problem, source: str, inputs: list, *, workdir: str, timeout_s: float, binary: str | None = None, overflow_checks: bool = True, mem_mb: int = 4096) -> list[CaseResult]:
    if problem.language == "python":
        return run_python_cases(source, problem.entrypoint, [tuple(i) if isinstance(i, (tuple, list)) else (i,) for i in inputs], timeout_s=timeout_s, workdir=workdir, mem_mb=mem_mb)
    if binary is None:
        binary, err = compile_rust(source, overflow_checks=overflow_checks, workdir=workdir)
        if binary is None:
            return [CaseResult(False, error="compile: " + err[-800:]) for _ in inputs]
    return run_rust_cases(binary, list(inputs), timeout_s=timeout_s, mem_mb=mem_mb)

def same(problem, actual: CaseResult, expected) -> bool:
    if not actual.ok: return False
    if problem.language == "rust": return tokens(actual.output) == tokens(expected if isinstance(expected, str) else str(expected))
    return actual.output == expected

def differential(problem, source: str, cases: list[Case], kind: str, *, workdir: str, timeout_s: float, binary: str | None = None, mem_mb: int = 4096) -> Evidence:
    t0 = time.monotonic()
    if not cases:
        return Evidence(kind, True, 0, 0.0, {"skipped": "no cases"}, skipped=True)
    if timeout_s <= 0:
        return Evidence(kind, True, 0, 0.0, {"skipped": "no budget"}, skipped=True)
    # Rust runs each case as a separate process with its own timeout, so a batch timeout must be
    # divided across cases or the ceiling is timeout_s * len(cases); Python runs the whole batch
    # inside one subprocess call, so the full timeout_s already bounds the batch.
    case_timeout = timeout_s if problem.language == "python" else max(0.25, timeout_s / max(1, len(cases)))
    res = run_candidate(problem, source, [c.input for c in cases], workdir=workdir, timeout_s=case_timeout, binary=binary, mem_mb=mem_mb)
    for i, (c, r) in enumerate(zip(cases, res)):
        if not same(problem, r, c.expected):
            return Evidence(kind, False, len(cases), time.monotonic() - t0,
                            {"index": i, "input": c.input, "expected": c.expected, "actual": r.output, "error": r.error[-600:], "timed_out": r.timed_out})
    return Evidence(kind, True, len(cases), time.monotonic() - t0)

def _fails(problem, source, oracle_src, inp, workdir, binary, timeout_s: float = 5.0) -> tuple[bool, object]:
    exp = run_python_cases(oracle_src, "reference", [_ref_args(problem, inp)], workdir=os.path.join(workdir, "o"), timeout_s=timeout_s)[0]
    if not exp.ok: return False, None
    act = run_candidate(problem, source, [inp], workdir=os.path.join(workdir, "c"), timeout_s=timeout_s, binary=binary)[0]
    return (not same(problem, act, exp.output)), exp.output

def _shrink_value(v):
    """Yield smaller variants of one argument value, most aggressive first."""
    if isinstance(v, bool) or v is None: return
    if isinstance(v, int):
        for c in (0, v // 2, v - 1):
            if c != v and abs(c) < abs(v): yield c
    elif isinstance(v, str):
        for c in (v[: len(v) // 2], v[1:], v[:-1]):
            if c != v: yield c
    elif isinstance(v, (list, tuple)):
        n = len(v)
        if n == 0: return
        half = v[: n // 2], v[n // 2 :]
        for c in half:
            if len(c) < n: yield type(v)(c)
        for i in range(n):
            yield type(v)(list(v[:i]) + list(v[i + 1 :]))
        for i, e in enumerate(v):
            for se in _shrink_value(e):
                yield type(v)(list(v[:i]) + [se] + list(v[i + 1 :]))
    elif isinstance(v, dict):
        for k in list(v):
            d = dict(v); del d[k]; yield d

def shrink(problem, source: str, oracle_src: str, case: Case, *, workdir: str, budget_s: float, binary: str | None = None) -> Case:
    if problem.language != "python":
        return case
    t0 = time.monotonic(); cur = tuple(case.input); exp = case.expected
    improved = True
    while improved and time.monotonic() - t0 < budget_s:
        improved = False
        for i in range(len(cur)):
            for alt in _shrink_value(cur[i]):
                now = time.monotonic()
                if now - t0 >= budget_s: break
                call_timeout = max(0.5, min(5.0, budget_s - (now - t0)))
                cand = cur[:i] + (alt,) + cur[i + 1 :]
                still, e = _fails(problem, source, oracle_src, cand, workdir, binary, timeout_s=call_timeout)
                if still:
                    cur, exp, improved = cand, e, True
                    break
            if improved: break
    return Case(cur, exp, case.tag + "+shrunk")

def _log_and_note_validation(gi: GateInputs, ev: Evidence, mode: str, log) -> None:
    invalid = ev.detail.get("invalid_dropped", 0)
    skip = ev.detail.get("validation_skipped", "")
    log(f"oracle.{mode} cases={ev.cases} dropped={ev.detail['dropped']} invalid={invalid}" + (f" skip={skip}" if skip else ""))
    if invalid:
        gi.notes.append(f"{mode}: dropped {invalid} of {ev.detail.get('checked', 0)} generated inputs as invalid (failed validate())")

def _clean_stdin(problem, s):
    # Model-written Rust stdin fixtures (EDGES entries, gen_max's output) are Python triple-quoted
    # literals indented to match the surrounding source, so every line after the first inherits that
    # indentation. Fed verbatim, a line-oriented Rust parser chokes on "    41".parse::<u64>(). The
    # judge never sends indented input -- the README defines Rust I/O as whitespace-separated tokens
    # -- so strip each line before it reaches the candidate. No-op for Python (args, not stdin text).
    if problem.language != "rust" or not isinstance(s, str):
        return s
    return "\n".join(line.strip() for line in s.split("\n"))

_PUBLIC_INPUT_KEYS = ("input", "args", "in", "params")
_PUBLIC_OUTPUT_KEYS = ("output", "expected", "result", "out", "answer")

def _extract_public_pair(entry):
    """Return (input, output) from one public_examples entry, or None if the shape isn't recognized."""
    if isinstance(entry, dict):
        inp = next((entry[k] for k in _PUBLIC_INPUT_KEYS if k in entry), None)
        out = next((entry[k] for k in _PUBLIC_OUTPUT_KEYS if k in entry), None)
        return (inp, out) if inp is not None and out is not None else None
    if isinstance(entry, (list, tuple)) and len(entry) == 2:
        return entry[0], entry[1]
    return None

def _parse_public_examples(problem) -> tuple[list[Case], list[str]]:
    """problem.public_examples is untrusted problem data of unknown shape. Recognize a dict with
    input/output under a few plausible spellings, or a bare [input, output] pair; skip -- with a
    note, never a crash -- anything else. These become ground-truth Cases: unlike oracle-derived
    cases they are never run through validate() and never dropped on a reference() disagreement
    (see _check_public_against_oracle: a disagreement there indicts the oracle, not the example)."""
    cases, notes = [], []
    for i, entry in enumerate(problem.public_examples or []):
        pair = _extract_public_pair(entry)
        if pair is None:
            notes.append(f"public_examples[{i}]: unrecognized shape, skipped: {_fmt(entry, 200)}")
            continue
        inp, out = pair
        norm_input = _ref_args(problem, inp) if problem.language == "python" else _clean_stdin(problem, inp)
        cases.append(Case(norm_input, out, "public"))
    return cases, notes

def _check_public_against_oracle(problem, gi: GateInputs, oracle_src: str, *, workdir: str, budget, log) -> None:
    """Cross-check every public example against the oracle's reference() purely to surface a
    disagreement -- never to validate or drop the public example, which is ground truth and the
    oracle is not. A disagreement means the ORACLE is wrong: it's recorded as a note and attached to
    the diff_public evidence (see run_gate), and the public example is kept exactly as given."""
    ref_res = run_python_cases(oracle_src, "reference", [_ref_args(problem, c.input) for c in gi.cases_public],
                                workdir=os.path.join(workdir, "ref_public"), timeout_s=budget.step_timeout(30.0, reserve_s=10.0))
    for c, r in zip(gi.cases_public, ref_res):
        if same(problem, r, c.expected):
            continue
        oracle_output = r.output if r.ok else f"<reference() raised: {r.error[-200:]}>"
        gi.public_disagreements.append({"input": c.input, "public_expected": c.expected, "oracle_reference_returned": oracle_output})
        gi.notes.append(f"oracle disagrees with a public example (keeping the public example as ground truth): "
                         f"input={_fmt(c.input, 200)} public_expected={_fmt(c.expected, 200)} oracle_returned={_fmt(oracle_output, 200)}")
        log(f"oracle.disagrees_with_public input={_fmt(c.input, 120)}")

def oracle_unusable(gi: GateInputs) -> bool:
    """True when the oracle yielded no usable case at all across every generated tier -- small,
    medium, and edge -- so nothing in the gate could actually be checked against it (the oracle's own
    reference()/gen()/validate() crashed, or it produced no ===ORACLE=== block at all). Public
    examples don't count either way here: they're ground truth from the problem, not something the
    oracle produced, so they say nothing about whether the oracle itself is usable."""
    return not gi.cases_small and not gi.cases_medium and not gi.cases_edge

def selfrepair_extra(gi: GateInputs) -> str:
    """Extra context for regenerate_oracle when the oracle is being reissued because its own code
    crashed rather than because a repair disputed one of its answers -- the failure notes collected
    by prepare_gate_inputs (tracebacks from reference()/gen()) are the only evidence available."""
    notes = "\n".join(f"- {n}" for n in gi.notes) or "(no details captured)"
    return ("\n\nYour previous reference()/gen()/validate() code crashed instead of running -- these "
            "are the actual errors your code produced when executed on real inputs. Fix these specific "
            "problems (undefined names, unpacking errors, exceptions) and follow the statement "
            f"literally:\n{notes}\n")

def dispute_extra(gi: GateInputs, failed: Evidence) -> str:
    """Extra context for regenerate_oracle when a repair call adjudicated candidate vs. oracle and
    blamed the oracle for a wrong answer on a specific input."""
    extra = ("\n\nA previous reference was judged WRONG on this input; re-read the statement and follow it literally here:\n"
             f"Input: {_fmt(failed.detail.get('input'))}\nPrevious (wrong) expected: {_fmt(failed.detail.get('expected'))}\n")
    if gi.dispute_trace:
        extra += f"A solver hand-traced the statement on this input and concluded the previous reference was wrong. Its trace:\n{gi.dispute_trace}\n"
    return extra

def prepare_gate_inputs(problem, oracle_src: str, stress_src: str, limits: dict, *, workdir: str, budget, log) -> GateInputs:
    gi = GateInputs(oracle_src=oracle_src)
    gi.cases_public, parse_notes = _parse_public_examples(problem)
    gi.notes.extend(parse_notes)
    if gi.cases_public:
        log(f"public_examples parsed={len(gi.cases_public)} skipped={len(parse_notes)}")
    if not oracle_src.strip():
        gi.notes.append("no oracle source"); return gi
    oracle_src = _STDLIB_PREAMBLE + oracle_src
    gi.oracle_src = oracle_src
    if gi.cases_public:
        _check_public_against_oracle(problem, gi, oracle_src, workdir=workdir, budget=budget, log=log)
    for mode, n in (("small", limits["cases_small"]), ("medium", limits["cases_medium"])):
        gen = run_python_cases(oracle_src, "gen", [(s, mode) for s in range(n)], workdir=os.path.join(workdir, f"gen_{mode}"), timeout_s=budget.step_timeout(60.0, reserve_s=20.0))
        inputs = [r.output for r in gen if r.ok]
        if not inputs:
            gi.notes.append(f"gen({mode}) produced nothing: {(gen[0].error if gen else '')[-200:]}"); continue
        cases, ev = make_cases(problem, oracle_src, inputs, mode, workdir=os.path.join(workdir, f"ref_{mode}"), timeout_s=budget.step_timeout(60.0, reserve_s=20.0))
        if not cases: gi.notes.append(f"reference failed on all {mode} inputs: {ev.detail['errors']}")
        setattr(gi, f"cases_{mode}", cases); _log_and_note_validation(gi, ev, mode, log)
    if stress_src.strip():
        stress_src = _STDLIB_PREAMBLE + stress_src
        edges = run_python_cases(stress_src + "\ndef _edges():\n    return list(EDGES)\n", "_edges", [()], workdir=os.path.join(workdir, "edges"), timeout_s=budget.step_timeout(60.0, reserve_s=20.0))
        if edges and edges[0].ok and isinstance(edges[0].output, list):
            cleaned_edges = [_clean_stdin(problem, s) for s in edges[0].output]
            gi.cases_edge, ev = make_cases(problem, oracle_src, cleaned_edges, "edge", workdir=os.path.join(workdir, "ref_edge"), timeout_s=budget.step_timeout(60.0, reserve_s=20.0))
            _log_and_note_validation(gi, ev, "edge", log)
        mx = run_python_cases(stress_src, "gen_max", [(1,)], workdir=os.path.join(workdir, "genmax"), timeout_s=budget.step_timeout(60.0, reserve_s=20.0))
        if mx and mx[0].ok:
            gi.stress_input = _clean_stdin(problem, mx[0].output)
        else:
            gi.notes.append("gen_max failed: " + (mx[0].error[-200:] if mx else ""))
            # gen_max is the only source of a stress input; rather than ship a correct-but-slow
            # solution with the timing check silently skipped, fall back to the largest medium case.
            # "Largest" by len(str(...)): generic across a Python argument tuple (str() of the tuple
            # scales with its total content) and a Rust stdin string (str() is the string itself), so
            # the same one-line measure works for both without knowing the problem's shape. This is
            # NOT a true maximum-size input -- medium is capped far below gen_max's target scale -- so
            # it's marked degraded and stress()'s evidence records that, so the timing it produces is
            # never mistaken for a real max-size check.
            if gi.cases_medium:
                biggest = max(gi.cases_medium, key=lambda c: len(str(c.input)))
                gi.stress_input = _clean_stdin(problem, biggest.input)
                gi.stress_degraded = True
                gi.notes.append("stress input degraded: gen_max failed, using largest medium case instead (not a true max-size input)")
    else:
        gi.notes.append("no stress source")
    return gi

def stress(problem, source: str, stress_input, *, workdir: str, limit_s: float, mem_mb: int, degraded: bool = False) -> Evidence:
    if stress_input is None:
        return Evidence("stress", True, 0, 0.0, {"skipped": "no stress input"}, skipped=True)
    t0 = time.monotonic()
    if problem.language == "python":
        args = tuple(stress_input) if isinstance(stress_input, (tuple, list)) else (stress_input,)
        r = run_python_cases(source, problem.entrypoint, [args], timeout_s=limit_s + 15.0, workdir=workdir, mem_mb=mem_mb)[0]  # +15 s covers import + arg parsing; function time is measured inside the harness
        dur = r.duration_s if r.ok else (limit_s + 1)
    else:
        binary, err = compile_rust(source, overflow_checks=False, workdir=workdir)
        if binary is None:
            return Evidence("stress", False, 1, time.monotonic() - t0, {"error": "compile: " + err[-500:]})
        r = run_rust_cases(binary, [stress_input], timeout_s=limit_s, mem_mb=mem_mb)[0]
        dur = r.duration_s
    passed = r.ok and not r.timed_out and dur <= limit_s
    detail = {"duration_s": round(dur, 3), "limit_s": limit_s, "timed_out": r.timed_out, "error": r.error[-400:]}
    if degraded:
        detail["degraded"] = "gen_max failed; this is the largest medium case, not a true max-size input -- this timing is not a real max-size stress check"
    return Evidence("stress", passed, 1, time.monotonic() - t0, detail)

def overflow_check(problem, binary: str | None, stress_input, *, limit_s: float, mem_mb: int) -> Evidence:
    """Reruns the max-size stress input on the OVERFLOW-CHECKED build (the one already compiled and
    cached by run_gate's compile step -- reused here, not recompiled) purely to catch a silent i64
    wrap at maximum scale that stress() cannot see (stress deliberately compiles with
    overflow-checks=off, for a realistic timing measurement).

    This does NOT verify the output is correct at this scale: the oracle is a literal, slow Python
    reference and cannot produce an expected answer for a maximum-size input in any reasonable time.
    All this step proves is the absence of an overflow panic; it says nothing about whether the
    arithmetic result itself is right.
    """
    if problem.language != "rust":
        return Evidence("overflow", True, 0, 0.0, {"skipped": "python integers are arbitrary precision; nothing to overflow"}, skipped=True)
    if stress_input is None:
        return Evidence("overflow", True, 0, 0.0, {"skipped": "no stress input"}, skipped=True)
    if binary is None:
        return Evidence("overflow", True, 0, 0.0, {"skipped": "no overflow-checked binary"}, skipped=True)
    t0 = time.monotonic()
    r = run_rust_cases(binary, [stress_input], timeout_s=limit_s, mem_mb=mem_mb)[0]
    dur = time.monotonic() - t0
    if r.ok:
        return Evidence("overflow", True, 1, dur, {"duration_s": round(r.duration_s, 3)})
    # Rust panics on overflow print things like "attempt to add with overflow" to stderr and exit
    # non-zero, but so does any other panic or crash -- a non-zero exit is not proof of overflow.
    # Only label it an overflow finding when stderr actually says so; otherwise report the failure
    # honestly as unexplained rather than overclaiming what was detected.
    stderr = r.error[-800:]
    is_overflow = "overflow" in stderr.lower()
    detail = {"stderr": stderr, "timed_out": r.timed_out}
    if not is_overflow:
        detail["note"] = "non-zero exit without a recognizable overflow-panic message; failure cause not confirmed as overflow"
    return Evidence("overflow", False, 1, dur, detail)

def behavior(problem, source: str, cases: list[Case], *, workdir: str, timeout_s: float, binary: str | None = None, mem_mb: int = 4096) -> Evidence:
    t0 = time.monotonic()
    if not cases:
        return Evidence("behavior", True, 0, 0.0, {"skipped": "no cases"}, skipped=True)
    if timeout_s <= 0:
        return Evidence("behavior", True, 0, 0.0, {"skipped": "no budget"}, skipped=True)
    inputs = [c.input for c in cases[:10]]
    # Rust runs each case as a separate process with its own timeout (see differential()'s comment);
    # Python handles a whole batch inside one subprocess call, so it keeps the full timeout_s.
    case_timeout = timeout_s if problem.language == "python" else max(0.25, timeout_s / max(1, len(inputs)))
    if problem.language == "python":
        aba = run_candidate(problem, source, [inputs[0], inputs[-1], inputs[0]], workdir=os.path.join(workdir, "aba"), timeout_s=case_timeout, mem_mb=mem_mb)
        if any(r.mutated for r in aba):
            return Evidence("behavior", False, 3, time.monotonic() - t0, {"check": "mutation", "input": inputs[0]})
        if aba[0].ok and aba[2].ok and aba[0].output != aba[2].output:
            return Evidence("behavior", False, 3, time.monotonic() - t0, {"check": "global_state", "input": inputs[0], "first": aba[0].output, "again": aba[2].output})
    r1 = run_candidate(problem, source, inputs, workdir=os.path.join(workdir, "p1"), timeout_s=case_timeout, binary=binary, mem_mb=mem_mb)
    r2 = run_candidate(problem, source, inputs, workdir=os.path.join(workdir, "p2"), timeout_s=case_timeout, binary=binary, mem_mb=mem_mb)
    for i, (a, b) in enumerate(zip(r1, r2)):
        if a.ok and b.ok and (a.output if problem.language == "python" else tokens(a.output)) != (b.output if problem.language == "python" else tokens(b.output)):
            return Evidence("behavior", False, len(inputs), time.monotonic() - t0, {"check": "nondeterminism", "input": inputs[i], "run1": a.output, "run2": b.output})
    return Evidence("behavior", True, len(inputs), time.monotonic() - t0)

def _sample_args(gi: GateInputs):
    for c in (gi.regressions + gi.cases_edge) or gi.cases_small or gi.cases_medium:
        return c.input
    return None

def python_import_check(source: str, entrypoint: str, gi: GateInputs, *, workdir: str, timeout_s: float, mem_mb: int = 4096) -> Evidence:
    """Python's only pre-flight: import the module (and attempt one call) in the sandbox, mirroring
    Rust's compile step, so a module-level error is caught before any gate step burns cases on it."""
    if timeout_s <= 0:
        return Evidence("compile", True, 0, 0.0, {"skipped": "no budget"}, skipped=True)
    t0 = time.monotonic()
    sample = _sample_args(gi)
    args = sample if isinstance(sample, tuple) else (sample,) if sample is not None else ()
    res = run_python_cases(source, entrypoint, [args], timeout_s=timeout_s, workdir=workdir, mem_mb=mem_mb)
    err = res[0].error if res else ""
    failed = err.startswith("IMPORT:")
    return Evidence("compile", not failed, 0, time.monotonic() - t0, {"error": err[-1500:]} if failed else {})

def run_gate(problem, source: str, gi: GateInputs, *, workdir: str, budget, limits: dict, log) -> list[Evidence]:
    ev: list[Evidence] = []
    binary = None
    if problem.language == "rust":
        t0 = time.monotonic(); binary, err = compile_rust(source, overflow_checks=True, workdir=workdir, timeout_s=budget.step_timeout(90.0))
        ev.append(Evidence("compile", binary is not None, 0, time.monotonic() - t0, {"stderr": err[-1500:]}))
        if binary is None: return ev
    else:
        e = python_import_check(source, problem.entrypoint, gi, workdir=os.path.join(workdir, "compile"), timeout_s=budget.step_timeout(30.0, reserve_s=20.0), mem_mb=limits["mem_mb"])
        ev.append(e); log(f"gate.compile passed={e.passed}")
        if not e.passed: return ev
    # Public examples (ground truth from the problem itself, not the model-written oracle) run first,
    # before any generated tier -- a candidate that fails real ground truth fails fast on the most
    # trustworthy evidence available. Absent entirely (the common case), this step is not added at
    # all, so behavior is exactly what it was before this existed.
    if gi.cases_public:
        e = differential(problem, source, gi.cases_public, "diff_public", workdir=os.path.join(workdir, "diff_public"), timeout_s=budget.step_timeout(60.0, reserve_s=20.0), binary=binary, mem_mb=limits["mem_mb"])
        if gi.public_disagreements:
            e.detail = {**e.detail, "oracle_disagreements": gi.public_disagreements}
        ev.append(e); log(f"gate.diff_public passed={e.passed} cases={e.cases}")
        if not e.passed: return ev
    for kind, cases in (("diff_edge", gi.regressions + gi.cases_edge), ("diff_small", gi.cases_small), ("diff_medium", gi.cases_medium)):
        e = differential(problem, source, cases, kind, workdir=os.path.join(workdir, kind), timeout_s=budget.step_timeout(60.0, reserve_s=20.0), binary=binary, mem_mb=limits["mem_mb"])
        ev.append(e); log(f"gate.{kind} passed={e.passed} cases={e.cases}")
        if not e.passed: return ev
    e = behavior(problem, source, gi.cases_small or gi.cases_edge, workdir=os.path.join(workdir, "behavior"), timeout_s=budget.step_timeout(60.0, reserve_s=20.0), binary=binary, mem_mb=limits["mem_mb"])
    ev.append(e); log(f"gate.behavior passed={e.passed} detail={e.detail.get('check','')}")
    if not e.passed: return ev
    limit = limits["stress_limit_python_s"] if problem.language == "python" else limits["stress_limit_rust_s"]
    # A hung Python stress call can itself run up to limit_s + 15s (see stress() above), so reserve
    # enough margin that it can't eat the whole safety margin; Rust's call is bounded by limit_s exactly.
    reserve = limit + 20.0 if problem.language == "python" else 15.0
    avail = budget.step_timeout(limit, reserve_s=reserve)
    if avail <= 0:
        e = Evidence("stress", True, 0, 0.0, {"skipped": "no budget"}, skipped=True)
    else:
        e = stress(problem, source, gi.stress_input, workdir=os.path.join(workdir, "stress"), limit_s=min(limit, avail), mem_mb=limits["mem_mb"], degraded=gi.stress_degraded)
    ev.append(e); log(f"gate.stress passed={e.passed} duration={e.detail.get('duration_s')}")
    # Placed after stress (not before) so stress's timing measurement runs first, on a warm cache,
    # unaffected by this step; and so this step's own subprocess never masks a stress timeout.
    if problem.language == "rust":
        ov_avail = budget.step_timeout(limits["stress_limit_rust_s"], reserve_s=15.0)
        if ov_avail <= 0:
            eo = Evidence("overflow", True, 0, 0.0, {"skipped": "no budget"}, skipped=True)
        else:
            eo = overflow_check(problem, binary, gi.stress_input, limit_s=min(limits["stress_limit_rust_s"], ov_avail), mem_mb=limits["mem_mb"])
    else:
        eo = overflow_check(problem, binary, gi.stress_input, limit_s=0.0, mem_mb=limits["mem_mb"])
    ev.append(eo); log(f"gate.overflow passed={eo.passed} skipped={eo.skipped}")
    return ev

_BLOCKS_RE = re.compile(r"===[A-Z_]+===.*?(===END===|\Z)", re.S)

def _fmt(v, limit: int = 4000) -> str:
    s = repr(v) if not isinstance(v, str) else v
    return s if len(s) <= limit else s[:limit] + " ...[truncated]"

def _fmt_typed(v, limit: int = 4000) -> str:
    # repr() alone can look identical for two differently-shaped failures once truncated or eyeballed
    # (an empty tuple candidate vs an empty list oracle both read short); naming the type explicitly
    # next to the value removes any doubt for the repair model. Display only -- same() is untouched.
    return f"{_fmt(v, limit)}  (type: {type(v).__name__})"

def _previous_attempt_note(run, cand, failed: Evidence) -> str:
    """If `cand` is itself the result of an earlier repair, describe that attempt: the input it was
    given, what it changed, and that the candidate above still fails -- so the model doesn't repeat a
    change that already didn't work. Empty string when there is no earlier repair (first attempt)."""
    if not cand.parent:
        return ""
    parent = next((c for c in run.cands if c.id == cand.parent), None)
    if parent is None:
        return ""
    prev_failed = next((e for e in parent.evidence if not e.passed), None)
    if prev_failed is None:
        return ""
    diff = "\n".join(difflib.unified_diff(parent.source.splitlines(), cand.source.splitlines(), lineterm="", n=1))
    return ("\nA previous repair already ran on this candidate. It was given failure kind="
            f"{prev_failed.kind} on input {_fmt_typed(prev_failed.detail.get('input'))}, and made this change:\n"
            f"{_fmt(diff, 1500)}\n"
            "That change did NOT fix the case: the candidate shown below still fails. Do not repeat it.\n")

def repair(run, cand, failed: Evidence, gi: GateInputs, pv: dict) -> tuple[str, str]:
    problem = run.p
    previous_attempt = _previous_attempt_note(run, cand, failed)
    detail = dict(failed.detail)
    if failed.kind.startswith("diff_") and "input" in detail:
        case = Case(detail["input"], detail["expected"], failed.kind)
        if problem.language == "python" and gi.oracle_src:
            case = shrink(problem, cand.source, gi.oracle_src, case, workdir=os.path.join(run.dir, cand.id, "shrink"), budget_s=run.cfg["limits"]["shrink_budget_s"])
            run.log(f"shrink input={_fmt(case.input)[:120]}")
        detail["input"], detail["expected"] = case.input, case.expected
        gi.regressions.append(case)
    pv_repair = {**pv, "statement": _fmt(pv["statement"], 12000)}
    # Derived from config.toml's [phases].repair_call_share, not a literal fraction -- see
    # regenerate_oracle's identical use of generate_call_share, and BENCHMARK-FINDINGS.md F6 for
    # what goes wrong when a phase cap is hardcoded instead (raising the config knob then does
    # nothing because the hardcoded fraction still wins the min() in Run.chat/step_timeout).
    repair_cap = run.cfg["phases"]["repair_call_share"] * run.budget.usable_s
    r = run.chat("strong", "repair", repair_cap, kind=failed.kind, input=_fmt_typed(detail.get("input")), expected=_fmt_typed(detail.get("expected")),
                 actual=_fmt_typed(detail.get("actual")), details=_fmt({k: v for k, v in detail.items() if k not in ("input", "expected", "actual")}),
                 code=_fmt(cand.source), previous_attempt=previous_attempt, **pv_repair)
    blocks = parse_blocks(r.text)
    trace = _BLOCKS_RE.sub("", r.text).strip()
    gi.dispute_trace = _fmt(trace)
    verdict = "oracle" if blocks.get("VERDICT", "").strip().lower().startswith("oracle") else "candidate"
    run.log(f"repair.verdict={verdict}")
    return blocks.get("CODE", ""), verdict

def regenerate_oracle(run, gi: GateInputs, extra: str, pv: dict, *, counter: str, workdir_tag: str = "oracle2") -> GateInputs:
    """Re-ask the fast model for a new oracle, with `extra` appended to the statement it sees, then
    re-run prepare_gate_inputs against whatever it returns.

    `counter` names which GateInputs field this call increments -- "oracle_regens" for adjudication
    (a repair blamed the oracle for a wrong answer on a specific input) or "oracle_selfrepairs" for
    this module noticing its own oracle crashed and produced zero usable cases. Keeping them as two
    distinct fields (rather than one shared counter) is what lets both fire at most once each in the
    same run -- a run can recover from a broken oracle early on and still adjudicate a later dispute.
    """
    label = "selfrepair" if counter == "oracle_selfrepairs" else "regen"
    # Regeneration writes the same reference+gen+validate functions as the original ORACLE call, so it
    # needs the same generous cap -- derived from the same config knob solve() uses -- not a separate,
    # smaller hardcoded fraction that clips before the oracle's measured latency (min 38s / median 73s
    # / max 128s). step_timeout (inside run.chat) still caps this to whatever budget remains.
    gen_cap = run.cfg["phases"]["generate_call_share"] * run.budget.usable_s
    r = run.chat("fast", "oracle", gen_cap, **{**pv, "statement": pv["statement"] + extra})
    src = parse_blocks(r.text).get("ORACLE", "")
    if not src.strip():
        run.log(f"oracle.{label} failed: empty")
        gi.notes.append(f"oracle {label} failed: empty reply")
        setattr(gi, counter, getattr(gi, counter) + 1)
        gi.regen_failed = True
        return gi
    new = prepare_gate_inputs(run.p, src, "", run.cfg["limits"], workdir=os.path.join(run.dir, workdir_tag), budget=run.budget, log=run.log)
    new.stress_input, new.cases_edge, new.regressions = gi.stress_input, gi.cases_edge, []
    new.oracle_regens, new.oracle_selfrepairs = gi.oracle_regens, gi.oracle_selfrepairs
    setattr(new, counter, getattr(new, counter) + 1)
    new.dispute_trace, new.regen_failed = "", False
    # re-derive edge expectations with the new reference
    if gi.cases_edge:
        new.cases_edge, _ = make_cases(run.p, new.oracle_src, [c.input for c in gi.cases_edge], "edge", workdir=os.path.join(run.dir, workdir_tag, "edge"), timeout_s=run.budget.step_timeout(30.0))
    run.log(f"oracle.{label} small={len(new.cases_small)} medium={len(new.cases_medium)} edge={len(new.cases_edge)}")
    return new
