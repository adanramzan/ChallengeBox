import os, time, pytest
from sandbox import Problem
import verify as V

PY = Problem("p", "python", "s", "add", [], 300.0)
ORACLE = "import random\ndef reference(a, b):\n    return a + b\ndef gen(seed, mode):\n    r = random.Random(seed)\n    hi = 20 if mode == 'small' else 10**5\n    return (r.randint(0, hi), r.randint(0, hi))\n"
GOOD = "def add(a, b):\n    return a + b\n"
BUG = "def add(a, b):\n    return a + b if a < 15 else a + b + 1\n"   # planted off-by-one for a >= 15

class FakeBudget:
    def step_timeout(self, cap, reserve_s=0.0): return cap
    def remaining(self): return 1e9
    def phase(self): return "gate"
    def can_afford(self, s): return True

def test_make_cases_from_oracle(tmp_path):
    from sandbox import run_python_cases
    inputs = [r.output for r in run_python_cases(ORACLE, "gen", [(s, "small") for s in range(10)], workdir=str(tmp_path / "g"), timeout_s=20)]
    cases, ev = V.make_cases(PY, ORACLE, inputs, "small", workdir=str(tmp_path / "o"), timeout_s=20)
    assert ev.passed and len(cases) == 10 and all(c.expected == c.input[0] + c.input[1] for c in cases)

def test_differential_passes_good_and_catches_bug(tmp_path):
    cases = [V.Case((a, 1), a + 1, "small") for a in range(30)]
    ok = V.differential(PY, GOOD, cases, "diff_small", workdir=str(tmp_path / "a"), timeout_s=20)
    bad = V.differential(PY, BUG, cases, "diff_small", workdir=str(tmp_path / "b"), timeout_s=20)
    assert ok.passed and ok.cases == 30
    assert not bad.passed and bad.detail["input"] == (15, 1) and bad.detail["expected"] == 16 and bad.detail["actual"] == 17

def test_shrink_reduces_failing_input(tmp_path):
    case = V.Case((1000, 999), 1999, "small")
    small = V.shrink(PY, BUG, ORACLE, case, workdir=str(tmp_path), budget_s=3.0)
    assert small.input[0] >= 15 and small.input[0] < 1000 and small.input[1] < 999
    assert small.expected == small.input[0] + small.input[1]

def test_prepare_gate_inputs_and_run_gate(tmp_path):
    limits = {"cases_small": 20, "cases_medium": 5, "stress_limit_python_s": 5.0, "stress_limit_rust_s": 2.0, "mem_mb": 2048, "shrink_budget_s": 1.0}
    stress = "def gen_max(seed):\n    return (10**18, 10**18)\nEDGES = [(0, 0), (15, 0)]\n"
    gi = V.prepare_gate_inputs(PY, ORACLE, stress, limits, workdir=str(tmp_path / "gi"), budget=FakeBudget(), log=lambda m: None)
    assert len(gi.cases_small) == 20 and len(gi.cases_medium) == 5 and len(gi.cases_edge) == 2 and gi.stress_input == (10**18, 10**18)
    ev = V.run_gate(PY, GOOD, gi, workdir=str(tmp_path / "g1"), budget=FakeBudget(), limits=limits, log=lambda m: None)
    assert all(e.passed for e in ev) and [e.kind for e in ev][:4] == ["compile", "diff_edge", "diff_small", "diff_medium"]
    ev = V.run_gate(PY, BUG, gi, workdir=str(tmp_path / "g2"), budget=FakeBudget(), limits=limits, log=lambda m: None)
    assert ev[0].kind == "compile" and ev[0].passed
    assert ev[1].kind == "diff_edge" and not ev[1].passed and ev[1].detail["input"] == (15, 0)

def test_prepare_gate_inputs_survives_broken_oracle(tmp_path):
    gi = V.prepare_gate_inputs(PY, "def reference(a, b): raise RuntimeError()\ndef gen(seed, mode): return (1, 2)\n", "", {"cases_small": 3, "cases_medium": 1, "mem_mb": 2048}, workdir=str(tmp_path), budget=FakeBudget(), log=lambda m: None)
    assert gi.cases_small == [] and any("reference" in n for n in gi.notes)

def test_stress_rejects_quadratic(tmp_path):
    p = Problem("p", "python", "s", "f", [], 300.0)
    slow = "def f(n):\n    s = 0\n    for i in range(n):\n        for j in range(i):\n            s += 1\n    return s\n"
    fast = "def f(n):\n    return n * (n - 1) // 2\n"
    assert not V.stress(p, slow, (20000,), workdir=str(tmp_path / "s"), limit_s=1.0, mem_mb=2048).passed
    assert V.stress(p, fast, (20000,), workdir=str(tmp_path / "f"), limit_s=1.0, mem_mb=2048).passed

def test_behavior_catches_mutation_global_state_and_nondeterminism(tmp_path):
    p = Problem("p", "python", "s", "f", [], 300.0)
    cases = [V.Case(([3, 1, 2],), None, "b"), V.Case(([9, 8],), None, "b")]
    mut = "def f(xs):\n    xs.sort()\n    return xs[0]\n"
    ev = V.behavior(p, mut, cases, workdir=str(tmp_path / "m"), timeout_s=10)
    assert not ev.passed and ev.detail["check"] == "mutation"
    glob = "_calls = []\ndef f(xs):\n    _calls.append(1)\n    return len(_calls)\n"
    ev = V.behavior(p, glob, cases, workdir=str(tmp_path / "g"), timeout_s=10)
    assert not ev.passed and ev.detail["check"] == "global_state"
    nondet = "import os\ndef f(xs):\n    return os.getpid()\n"
    ev = V.behavior(p, nondet, cases, workdir=str(tmp_path / "n"), timeout_s=10)
    assert not ev.passed and ev.detail["check"] == "nondeterminism"
    good = "def f(xs):\n    return sorted(xs)[0]\n"
    assert V.behavior(p, good, cases, workdir=str(tmp_path / "ok"), timeout_s=10).passed

def test_run_gate_includes_behavior_and_stress(tmp_path):
    limits = {"cases_small": 5, "cases_medium": 2, "stress_limit_python_s": 5.0, "stress_limit_rust_s": 2.0, "mem_mb": 2048, "shrink_budget_s": 1.0}
    stress = "def gen_max(seed):\n    return (10**18, 10**18)\nEDGES = [(0, 0)]\n"
    gi = V.prepare_gate_inputs(PY, ORACLE, stress, limits, workdir=str(tmp_path / "gi"), budget=FakeBudget(), log=lambda m: None)
    ev = V.run_gate(PY, GOOD, gi, workdir=str(tmp_path / "g"), budget=FakeBudget(), limits=limits, log=lambda m: None)
    assert [e.kind for e in ev] == ["compile", "diff_edge", "diff_small", "diff_medium", "behavior", "stress"] and all(e.passed for e in ev)

# --- fix round 1 ---

class NoStressBudget(FakeBudget):
    """step_timeout returns 0.0 only for the stress call (identified by its cap == stress_limit_python_s,
    which is unique to that call site), so every other gate step still gets a normal timeout."""
    def step_timeout(self, cap, reserve_s=0.0):
        return 0.0 if cap == 5.0 else cap

def test_run_gate_skips_stress_when_budget_exhausted(tmp_path):
    limits = {"cases_small": 5, "cases_medium": 2, "stress_limit_python_s": 5.0, "stress_limit_rust_s": 2.0, "mem_mb": 2048, "shrink_budget_s": 1.0}
    stress = "def gen_max(seed):\n    return (10**18, 10**18)\nEDGES = [(0, 0)]\n"
    gi = V.prepare_gate_inputs(PY, ORACLE, stress, limits, workdir=str(tmp_path / "gi"), budget=FakeBudget(), log=lambda m: None)
    t0 = time.monotonic()
    ev = V.run_gate(PY, GOOD, gi, workdir=str(tmp_path / "g"), budget=NoStressBudget(), limits=limits, log=lambda m: None)
    dt = time.monotonic() - t0
    assert ev[-1].kind == "stress" and ev[-1].skipped is True and ev[-1].passed is True
    assert dt < 5.0

def test_run_gate_all_skipped_reports_passed_but_flags_skipped(tmp_path):
    limits = {"cases_small": 5, "cases_medium": 2, "stress_limit_python_s": 5.0, "stress_limit_rust_s": 2.0, "mem_mb": 2048, "shrink_budget_s": 1.0}
    gi = V.GateInputs()  # broken oracle / no stress source: no cases at all, no stress input
    ev = V.run_gate(PY, GOOD, gi, workdir=str(tmp_path / "g"), budget=FakeBudget(), limits=limits, log=lambda m: None)
    assert all(e.passed for e in ev)
    assert any(e.skipped for e in ev)

def test_shrink_respects_budget_when_candidate_hangs(tmp_path):
    slow_bug = "import time\ndef add(a, b):\n    time.sleep(10)\n    return a + b\n"
    case = V.Case((1000, 999), 1999, "small")
    t0 = time.monotonic()
    V.shrink(PY, slow_bug, ORACLE, case, workdir=str(tmp_path), budget_s=1.0)
    assert time.monotonic() - t0 < 4.0

# --- fix round 2 (final review wave) ---

def test_run_gate_python_compile_step_catches_module_level_nameerror(tmp_path):
    # C3: Python had no compile/import pre-flight at all, so a module-level NameError was only ever
    # discovered mid-differential (burning cases on it). It must now fail at a dedicated "compile" step.
    limits = {"cases_small": 5, "cases_medium": 2, "stress_limit_python_s": 5.0, "stress_limit_rust_s": 2.0, "mem_mb": 2048, "shrink_budget_s": 1.0}
    stress = "def gen_max(seed):\n    return (10**18, 10**18)\nEDGES = [(0, 0)]\n"
    gi = V.prepare_gate_inputs(PY, ORACLE, stress, limits, workdir=str(tmp_path / "gi"), budget=FakeBudget(), log=lambda m: None)
    broken = "BOOM = undefined_name\ndef add(a, b):\n    return a + b\n"
    ev = V.run_gate(PY, broken, gi, workdir=str(tmp_path / "g"), budget=FakeBudget(), limits=limits, log=lambda m: None)
    assert len(ev) == 1   # the gate stops at the first failure
    assert ev[0].kind == "compile" and not ev[0].passed and "undefined_name" in ev[0].detail["error"]

def test_differential_rust_divides_timeout_by_case_count(tmp_path, monkeypatch):
    # C4(b): run_rust_cases applies its timeout PER CASE, so a batch timeout must be divided across
    # cases before reaching it, or a 200-case differential with a 60s cap has a 3-hour ceiling.
    rust = Problem("p", "rust", "s", "main", [], 300.0)
    captured = {}
    def fake_run_candidate(problem, source, inputs, *, workdir, timeout_s, binary=None, overflow_checks=True):
        captured["timeout_s"] = timeout_s; captured["n"] = len(inputs)
        return [V.CaseResult(True, output=str(i)) for i in inputs]
    monkeypatch.setattr(V, "run_candidate", fake_run_candidate)
    cases = [V.Case(str(i), str(i), "small") for i in range(200)]
    ev = V.differential(rust, "src", cases, "diff_small", workdir=str(tmp_path), timeout_s=60.0, binary="bin")
    assert ev.passed and captured["n"] == 200
    assert captured["timeout_s"] == pytest.approx(60.0 / 200)

def test_behavior_rust_divides_timeout_by_case_count(tmp_path, monkeypatch):
    # C4 completion: behavior() calls run_candidate for up to 10 inputs, twice (plus a third for the
    # Python-only mutation check), and run_rust_cases applies its timeout PER CASE -- so it needs the
    # same division differential() got, or 10 cases * 60s * 2 calls = 1200s against a 285s budget.
    rust = Problem("p", "rust", "s", "main", [], 300.0)
    captured = []
    def fake_run_candidate(problem, source, inputs, *, workdir, timeout_s, binary=None, overflow_checks=True):
        captured.append((timeout_s, len(inputs)))
        return [V.CaseResult(True, output=str(i)) for i in inputs]
    monkeypatch.setattr(V, "run_candidate", fake_run_candidate)
    cases = [V.Case(str(i), str(i), "small") for i in range(50)]
    ev = V.behavior(rust, "src", cases, workdir=str(tmp_path), timeout_s=60.0)
    assert ev.passed and captured
    for timeout_s, n in captured:
        assert n == 10   # behavior() only ever looks at cases[:10]
        assert timeout_s == pytest.approx(60.0 / 10)

def test_behavior_skips_on_zero_budget_instead_of_failing(tmp_path):
    cases = [V.Case((a, 1), a + 1, "small") for a in range(3)]
    ev = V.behavior(PY, GOOD, cases, workdir=str(tmp_path), timeout_s=0.0)
    assert ev.passed and ev.skipped and ev.detail.get("skipped") == "no budget"

def test_differential_skips_on_zero_budget_instead_of_failing(tmp_path):
    # C4(d): running out of time must be reported as skipped, not as a candidate failure.
    cases = [V.Case((a, 1), a + 1, "small") for a in range(5)]
    ev = V.differential(PY, GOOD, cases, "diff_small", workdir=str(tmp_path), timeout_s=0.0)
    assert ev.passed and ev.skipped and ev.detail.get("skipped") == "no budget"

class CountingBudget(FakeBudget):
    def __init__(self): self.n = 0
    def step_timeout(self, cap, reserve_s=0.0):
        self.n += 1
        return cap

def test_prepare_gate_inputs_reads_step_timeout_fresh_per_site(tmp_path):
    # C4(a): step_timeout must be re-invoked at every subprocess site (so each leaves reserve_s behind
    # and reflects the current clock), not computed once and reused across all six call sites.
    limits = {"cases_small": 3, "cases_medium": 2, "mem_mb": 2048}
    stress = "def gen_max(seed):\n    return (1, 2)\nEDGES = [(0, 0)]\n"
    b = CountingBudget()
    V.prepare_gate_inputs(PY, ORACLE, stress, limits, workdir=str(tmp_path), budget=b, log=lambda m: None)
    assert b.n >= 6

def test_run_gate_reserves_stress_limit_plus_20_for_python(tmp_path):
    # C4(c): stress() can itself run up to limit_s + 15s, so the reserve at the call site must be more
    # than that (limit_s + 20), not the flat 15s reserved by every other gate step.
    limits = {"cases_small": 0, "cases_medium": 0, "stress_limit_python_s": 5.0, "stress_limit_rust_s": 2.0, "mem_mb": 2048, "shrink_budget_s": 1.0}
    seen = {}
    class SpyBudget(FakeBudget):
        def step_timeout(self, cap, reserve_s=0.0):
            if cap == 5.0: seen["reserve"] = reserve_s
            return 5.0
    gi = V.GateInputs(oracle_src="x")
    V.run_gate(PY, GOOD, gi, workdir=str(tmp_path), budget=SpyBudget(), limits=limits, log=lambda m: None)
    assert seen.get("reserve") == 25.0
