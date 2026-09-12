"""Verification: cases from the oracle, differential, behavior, stress, shrink, repair glue."""
from __future__ import annotations
import os, re, time
from dataclasses import dataclass, field
from sandbox import CaseResult, run_python_cases, compile_rust, run_rust_cases, tokens
from llm import parse_blocks

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
    stress_input: object = None
    oracle_src: str = ""
    oracle_regens: int = 0
    notes: list = field(default_factory=list)
    regressions: list = field(default_factory=list)
    dispute_trace: str = ""
    regen_failed: bool = False
    def summary(self) -> dict:
        return {"small": len(self.cases_small), "medium": len(self.cases_medium), "edge": len(self.cases_edge), "stress": self.stress_input is not None, "notes": self.notes}

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
    res = run_python_cases(oracle_src, "reference", [_ref_args(problem, i) for i in inputs], workdir=workdir, timeout_s=timeout_s)
    cases = [Case(i, r.output, tag) for i, r in zip(inputs, res) if r.ok]
    dropped = [r.error[-200:] for r in res if not r.ok]
    return cases, Evidence(f"oracle_{tag}", bool(cases), len(cases), time.monotonic() - t0, {"dropped": len(dropped), "errors": dropped[:3]})

def run_candidate(problem, source: str, inputs: list, *, workdir: str, timeout_s: float, binary: str | None = None, overflow_checks: bool = True) -> list[CaseResult]:
    if problem.language == "python":
        return run_python_cases(source, problem.entrypoint, [tuple(i) if isinstance(i, (tuple, list)) else (i,) for i in inputs], timeout_s=timeout_s, workdir=workdir)
    if binary is None:
        binary, err = compile_rust(source, overflow_checks=overflow_checks, workdir=workdir)
        if binary is None:
            return [CaseResult(False, error="compile: " + err[-800:]) for _ in inputs]
    return run_rust_cases(binary, list(inputs), timeout_s=timeout_s)

def same(problem, actual: CaseResult, expected) -> bool:
    if not actual.ok: return False
    if problem.language == "rust": return tokens(actual.output) == tokens(expected if isinstance(expected, str) else str(expected))
    return actual.output == expected

def differential(problem, source: str, cases: list[Case], kind: str, *, workdir: str, timeout_s: float, binary: str | None = None) -> Evidence:
    t0 = time.monotonic()
    if not cases:
        return Evidence(kind, True, 0, 0.0, {"skipped": "no cases"}, skipped=True)
    if timeout_s <= 0:
        return Evidence(kind, True, 0, 0.0, {"skipped": "no budget"}, skipped=True)
    # Rust runs each case as a separate process with its own timeout, so a batch timeout must be
    # divided across cases or the ceiling is timeout_s * len(cases); Python runs the whole batch
    # inside one subprocess call, so the full timeout_s already bounds the batch.
    case_timeout = timeout_s if problem.language == "python" else max(0.25, timeout_s / max(1, len(cases)))
    res = run_candidate(problem, source, [c.input for c in cases], workdir=workdir, timeout_s=case_timeout, binary=binary)
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

def prepare_gate_inputs(problem, oracle_src: str, stress_src: str, limits: dict, *, workdir: str, budget, log) -> GateInputs:
    gi = GateInputs(oracle_src=oracle_src)
    if not oracle_src.strip():
        gi.notes.append("no oracle source"); return gi
    for mode, n in (("small", limits["cases_small"]), ("medium", limits["cases_medium"])):
        gen = run_python_cases(oracle_src, "gen", [(s, mode) for s in range(n)], workdir=os.path.join(workdir, f"gen_{mode}"), timeout_s=budget.step_timeout(60.0, reserve_s=20.0))
        inputs = [r.output for r in gen if r.ok]
        if not inputs:
            gi.notes.append(f"gen({mode}) produced nothing: {(gen[0].error if gen else '')[-200:]}"); continue
        cases, ev = make_cases(problem, oracle_src, inputs, mode, workdir=os.path.join(workdir, f"ref_{mode}"), timeout_s=budget.step_timeout(60.0, reserve_s=20.0))
        if not cases: gi.notes.append(f"reference failed on all {mode} inputs: {ev.detail['errors']}")
        setattr(gi, f"cases_{mode}", cases); log(f"oracle.{mode} cases={len(cases)} dropped={ev.detail['dropped']}")
    if stress_src.strip():
        edges = run_python_cases(stress_src + "\ndef _edges():\n    return list(EDGES)\n", "_edges", [()], workdir=os.path.join(workdir, "edges"), timeout_s=budget.step_timeout(60.0, reserve_s=20.0))
        if edges and edges[0].ok and isinstance(edges[0].output, list):
            gi.cases_edge, ev = make_cases(problem, oracle_src, edges[0].output, "edge", workdir=os.path.join(workdir, "ref_edge"), timeout_s=budget.step_timeout(60.0, reserve_s=20.0))
            log(f"oracle.edge cases={len(gi.cases_edge)} dropped={ev.detail['dropped']}")
        mx = run_python_cases(stress_src, "gen_max", [(1,)], workdir=os.path.join(workdir, "genmax"), timeout_s=budget.step_timeout(60.0, reserve_s=20.0))
        if mx and mx[0].ok: gi.stress_input = mx[0].output
        else: gi.notes.append("gen_max failed: " + (mx[0].error[-200:] if mx else ""))
    else:
        gi.notes.append("no stress source")
    return gi

def stress(problem, source: str, stress_input, *, workdir: str, limit_s: float, mem_mb: int) -> Evidence:
    if stress_input is None:
        return Evidence("stress", True, 0, 0.0, {"skipped": "no stress input"}, skipped=True)
    t0 = time.monotonic()
    if problem.language == "python":
        args = tuple(stress_input) if isinstance(stress_input, (tuple, list)) else (stress_input,)
        r = run_python_cases(source, problem.entrypoint, [args], timeout_s=limit_s + 15.0, workdir=workdir)[0]  # +15 s covers import + arg parsing; function time is measured inside the harness
        dur = r.duration_s if r.ok else (limit_s + 1)
    else:
        binary, err = compile_rust(source, overflow_checks=False, workdir=workdir)
        if binary is None:
            return Evidence("stress", False, 1, time.monotonic() - t0, {"error": "compile: " + err[-500:]})
        r = run_rust_cases(binary, [stress_input], timeout_s=limit_s, mem_mb=mem_mb)[0]
        dur = r.duration_s
    passed = r.ok and not r.timed_out and dur <= limit_s
    return Evidence("stress", passed, 1, time.monotonic() - t0, {"duration_s": round(dur, 3), "limit_s": limit_s, "timed_out": r.timed_out, "error": r.error[-400:]})

def behavior(problem, source: str, cases: list[Case], *, workdir: str, timeout_s: float, binary: str | None = None) -> Evidence:
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
        aba = run_candidate(problem, source, [inputs[0], inputs[-1], inputs[0]], workdir=os.path.join(workdir, "aba"), timeout_s=case_timeout)
        if any(r.mutated for r in aba):
            return Evidence("behavior", False, 3, time.monotonic() - t0, {"check": "mutation", "input": inputs[0]})
        if aba[0].ok and aba[2].ok and aba[0].output != aba[2].output:
            return Evidence("behavior", False, 3, time.monotonic() - t0, {"check": "global_state", "input": inputs[0], "first": aba[0].output, "again": aba[2].output})
    r1 = run_candidate(problem, source, inputs, workdir=os.path.join(workdir, "p1"), timeout_s=case_timeout, binary=binary)
    r2 = run_candidate(problem, source, inputs, workdir=os.path.join(workdir, "p2"), timeout_s=case_timeout, binary=binary)
    for i, (a, b) in enumerate(zip(r1, r2)):
        if a.ok and b.ok and (a.output if problem.language == "python" else tokens(a.output)) != (b.output if problem.language == "python" else tokens(b.output)):
            return Evidence("behavior", False, len(inputs), time.monotonic() - t0, {"check": "nondeterminism", "input": inputs[i], "run1": a.output, "run2": b.output})
    return Evidence("behavior", True, len(inputs), time.monotonic() - t0)

def _sample_args(gi: GateInputs):
    for c in (gi.regressions + gi.cases_edge) or gi.cases_small or gi.cases_medium:
        return c.input
    return None

def python_import_check(source: str, entrypoint: str, gi: GateInputs, *, workdir: str, timeout_s: float) -> Evidence:
    """Python's only pre-flight: import the module (and attempt one call) in the sandbox, mirroring
    Rust's compile step, so a module-level error is caught before any gate step burns cases on it."""
    if timeout_s <= 0:
        return Evidence("compile", True, 0, 0.0, {"skipped": "no budget"}, skipped=True)
    t0 = time.monotonic()
    sample = _sample_args(gi)
    args = sample if isinstance(sample, tuple) else (sample,) if sample is not None else ()
    res = run_python_cases(source, entrypoint, [args], timeout_s=timeout_s, workdir=workdir)
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
        e = python_import_check(source, problem.entrypoint, gi, workdir=os.path.join(workdir, "compile"), timeout_s=budget.step_timeout(30.0, reserve_s=20.0))
        ev.append(e); log(f"gate.compile passed={e.passed}")
        if not e.passed: return ev
    for kind, cases in (("diff_edge", gi.regressions + gi.cases_edge), ("diff_small", gi.cases_small), ("diff_medium", gi.cases_medium)):
        e = differential(problem, source, cases, kind, workdir=os.path.join(workdir, kind), timeout_s=budget.step_timeout(60.0, reserve_s=20.0), binary=binary)
        ev.append(e); log(f"gate.{kind} passed={e.passed} cases={e.cases}")
        if not e.passed: return ev
    e = behavior(problem, source, gi.cases_small or gi.cases_edge, workdir=os.path.join(workdir, "behavior"), timeout_s=budget.step_timeout(60.0, reserve_s=20.0), binary=binary)
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
        e = stress(problem, source, gi.stress_input, workdir=os.path.join(workdir, "stress"), limit_s=min(limit, avail), mem_mb=limits["mem_mb"])
    ev.append(e); log(f"gate.stress passed={e.passed} duration={e.detail.get('duration_s')}")
    return ev

_BLOCKS_RE = re.compile(r"===[A-Z_]+===.*?(===END===|\Z)", re.S)

def _fmt(v, limit: int = 4000) -> str:
    s = repr(v) if not isinstance(v, str) else v
    return s if len(s) <= limit else s[:limit] + " ...[truncated]"

def repair(run, cand, failed: Evidence, gi: GateInputs, pv: dict) -> tuple[str, str]:
    problem = run.p
    detail = dict(failed.detail)
    if failed.kind.startswith("diff_") and "input" in detail:
        case = Case(detail["input"], detail["expected"], failed.kind)
        if problem.language == "python" and gi.oracle_src:
            case = shrink(problem, cand.source, gi.oracle_src, case, workdir=os.path.join(run.dir, cand.id, "shrink"), budget_s=run.cfg["limits"]["shrink_budget_s"])
            run.log(f"shrink input={_fmt(case.input)[:120]}")
        detail["input"], detail["expected"] = case.input, case.expected
        gi.regressions.append(case)
    pv_repair = {**pv, "statement": _fmt(pv["statement"], 12000)}
    r = run.chat("strong", "repair", 0.25 * run.budget.usable_s, kind=failed.kind, input=_fmt(detail.get("input")), expected=_fmt(detail.get("expected")),
                 actual=_fmt(detail.get("actual")), details=_fmt({k: v for k, v in detail.items() if k not in ("input", "expected", "actual")}),
                 code=_fmt(cand.source), **pv_repair)
    blocks = parse_blocks(r.text)
    trace = _BLOCKS_RE.sub("", r.text).strip()
    gi.dispute_trace = _fmt(trace)
    verdict = "oracle" if blocks.get("VERDICT", "").strip().lower().startswith("oracle") else "candidate"
    run.log(f"repair.verdict={verdict}")
    return blocks.get("CODE", ""), verdict

def regenerate_oracle(run, gi: GateInputs, failed: Evidence, pv: dict) -> GateInputs:
    extra = ("\n\nA previous reference was judged WRONG on this input; re-read the statement and follow it literally here:\n"
             f"Input: {_fmt(failed.detail.get('input'))}\nPrevious (wrong) expected: {_fmt(failed.detail.get('expected'))}\n")
    if gi.dispute_trace:
        extra += f"A solver hand-traced the statement on this input and concluded the previous reference was wrong. Its trace:\n{gi.dispute_trace}\n"
    r = run.chat("fast", "oracle", 0.2 * run.budget.usable_s, **{**pv, "statement": pv["statement"] + extra})
    src = parse_blocks(r.text).get("ORACLE", "")
    if not src.strip():
        run.log("oracle.regen failed: empty")
        gi.notes.append("oracle regeneration failed")
        gi.oracle_regens += 1
        gi.regen_failed = True
        return gi
    new = prepare_gate_inputs(run.p, src, "", run.cfg["limits"], workdir=os.path.join(run.dir, "oracle2"), budget=run.budget, log=run.log)
    new.stress_input, new.cases_edge, new.oracle_regens, new.regressions = gi.stress_input, gi.cases_edge, gi.oracle_regens + 1, []
    new.dispute_trace, new.regen_failed = "", False
    # re-derive edge expectations with the new reference
    if gi.cases_edge:
        new.cases_edge, _ = make_cases(run.p, src, [c.input for c in gi.cases_edge], "edge", workdir=os.path.join(run.dir, "oracle2", "edge"), timeout_s=run.budget.step_timeout(30.0))
    run.log(f"oracle.regen small={len(new.cases_small)} medium={len(new.cases_medium)}")
    return new
