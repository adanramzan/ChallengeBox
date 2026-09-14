import os, time, shutil, pytest
from sandbox import Problem
import verify as V

needs_rustc = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not installed")

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
    assert all(e.passed for e in ev) and [e.kind for e in ev][:5] == ["compile", "diff_examples", "diff_edge", "diff_small", "diff_medium"]
    ev = V.run_gate(PY, BUG, gi, workdir=str(tmp_path / "g2"), budget=FakeBudget(), limits=limits, log=lambda m: None)
    assert ev[0].kind == "compile" and ev[0].passed
    assert ev[1].kind == "diff_examples" and ev[1].skipped   # no candidate examples: skipped, never failed
    assert ev[2].kind == "diff_edge" and not ev[2].passed and ev[2].detail["input"] == (15, 0)

def test_a_failed_tier_no_longer_suppresses_the_later_tiers_or_the_timing(tmp_path):
    # round-10 L2: stress sat last in a short-circuiting chain, so a differential failure meant the
    # candidate was never timed -- seven of ten round-10 solutions were asymptotically wrong and the
    # system had timing data on none of them. Every step after compile now runs.
    limits = {"cases_small": 20, "cases_medium": 5, "stress_limit_python_s": 5.0, "stress_limit_rust_s": 2.0, "mem_mb": 2048, "shrink_budget_s": 1.0}
    stress = "def gen_max(seed):\n    return (10**18, 10**18)\nEDGES = [(0, 0), (15, 0)]\n"
    gi = V.prepare_gate_inputs(PY, ORACLE, stress, limits, workdir=str(tmp_path / "gi"), budget=FakeBudget(), log=lambda m: None)
    ev = V.run_gate(PY, BUG, gi, workdir=str(tmp_path / "g"), budget=FakeBudget(), limits=limits, log=lambda m: None)
    kinds = [e.kind for e in ev]
    assert kinds == ["compile", "diff_examples", "diff_edge", "diff_small", "diff_medium", "behavior", "stress", "overflow"]
    by = {e.kind: e for e in ev}
    assert not by["diff_edge"].passed and not by["diff_small"].passed   # both measured, not just the first
    assert by["stress"].cases == 1 and by["stress"].detail["duration_s"] >= 0   # timed despite the failures
    # and the first failure in canonical order is still what a repair would be given
    assert next(e for e in ev if not e.passed).kind == "diff_edge"

def test_thin_tier_passes_but_is_marked_degraded(tmp_path):
    limits = {"cases_small": 5, "cases_medium": 2, "min_cases_small": 30, "min_cases_medium": 5,
              "stress_limit_python_s": 5.0, "stress_limit_rust_s": 2.0, "mem_mb": 2048, "shrink_budget_s": 1.0}
    gi = V.GateInputs(oracle_src=ORACLE, cases_small=[V.Case((a, 1), a + 1, "small") for a in range(5)])
    ev = {e.kind: e for e in V.run_gate(PY, GOOD, gi, workdir=str(tmp_path / "a"), budget=FakeBudget(), limits=limits, log=lambda m: None)}
    assert ev["diff_small"].passed and "degraded" in ev["diff_small"].detail
    assert ev["diff_medium"].skipped and "degraded" not in ev["diff_medium"].detail   # empty tier is skipped, not degraded
    gi.cases_small = [V.Case((a, 1), a + 1, "small") for a in range(30)]
    ev = {e.kind: e for e in V.run_gate(PY, GOOD, gi, workdir=str(tmp_path / "b"), budget=FakeBudget(), limits=limits, log=lambda m: None)}
    assert ev["diff_small"].passed and "degraded" not in ev["diff_small"].detail

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
    assert [e.kind for e in ev] == ["compile", "diff_examples", "diff_edge", "diff_small", "diff_medium", "behavior", "stress", "overflow"] and all(e.passed for e in ev)
    assert ev[-1].skipped and "arbitrary precision" in ev[-1].detail["skipped"]

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
    assert ev[-2].kind == "stress" and ev[-2].skipped is True and ev[-2].passed is True
    assert ev[-1].kind == "overflow" and ev[-1].skipped is True and ev[-1].passed is True
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
    def fake_run_candidate(problem, source, inputs, *, workdir, timeout_s, binary=None, overflow_checks=True, mem_mb=4096, per_case_s=0.0, deadline_s=None, max_consec_timeouts=0):
        captured["timeout_s"] = timeout_s; captured["n"] = len(inputs); captured["deadline_s"] = deadline_s
        return [V.CaseResult(True, output=str(i)) for i in inputs]
    monkeypatch.setattr(V, "run_candidate", fake_run_candidate)
    cases = [V.Case(str(i), str(i), "small") for i in range(200)]
    ev = V.differential(rust, "src", cases, "diff_small", workdir=str(tmp_path), timeout_s=60.0, binary="bin")
    assert ev.passed and captured["n"] == 200
    assert captured["timeout_s"] == pytest.approx(60.0 / 200)
    assert captured["deadline_s"] is not None   # the divided per-case grant still needs a batch ceiling

def test_behavior_rust_divides_timeout_by_case_count(tmp_path, monkeypatch):
    # C4 completion: behavior() calls run_candidate for up to 10 inputs, twice (plus a third for the
    # Python-only mutation check), and run_rust_cases applies its timeout PER CASE -- so it needs the
    # same division differential() got, or 10 cases * 60s * 2 calls = 1200s against a 285s budget.
    rust = Problem("p", "rust", "s", "main", [], 300.0)
    captured = []
    def fake_run_candidate(problem, source, inputs, *, workdir, timeout_s, binary=None, overflow_checks=True, mem_mb=4096, per_case_s=0.0, deadline_s=None):
        captured.append((timeout_s, len(inputs), deadline_s))
        return [V.CaseResult(True, output=str(i)) for i in inputs]
    monkeypatch.setattr(V, "run_candidate", fake_run_candidate)
    cases = [V.Case(str(i), str(i), "small") for i in range(50)]
    ev = V.behavior(rust, "src", cases, workdir=str(tmp_path), timeout_s=60.0)
    assert ev.passed and captured
    for timeout_s, n, deadline_s in captured:
        assert n == 10   # behavior() only ever looks at cases[:10]
        assert timeout_s == pytest.approx(60.0 / 10)
        assert deadline_s is not None

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

# --- overflow gate: max-scale i64 wrap is invisible to timing-only stress, caught by a checked rerun ---

RUST = Problem("p", "rust", "s", "main", [], 300.0)

RUST_SUM_I64 = ('use std::io::*;\n'
                'fn main(){let mut s=String::new();stdin().read_to_string(&mut s).unwrap();'
                'let v:Vec<i64>=s.split_whitespace().skip(1).map(|x| x.parse().unwrap()).collect();'
                'let sum:i64=v.iter().sum();println!("{}",sum);}\n')

RUST_SUM_I128 = ('use std::io::*;\n'
                 'fn main(){let mut s=String::new();stdin().read_to_string(&mut s).unwrap();'
                 'let v:Vec<i128>=s.split_whitespace().skip(1).map(|x| x.parse().unwrap()).collect();'
                 'let sum:i128=v.iter().sum();println!("{}",sum);}\n')

BIG_STRESS_INPUT = "2\n9000000000000000000 9000000000000000000\n"   # two values that fit i64 alone; their sum (1.8e19) overflows i64::MAX (~9.22e18)

@needs_rustc
def test_overflow_step_catches_i64_wrap_invisible_to_timing_only_stress(tmp_path):
    # The gap: an i64 sum that wraps silently at max scale passes compile (small/medium inputs are too
    # small to overflow) and passes stress (overflow-checks=off, timing only) -- only the new dedicated
    # overflow step, rerunning the checked build against the max-size input, catches it.
    limits = {"stress_limit_rust_s": 5.0, "mem_mb": 2048}
    small = [V.Case("3\n1 2 3\n", "6")]
    medium = [V.Case("3\n100000 200000 300000\n", "600000")]
    gi = V.GateInputs(cases_small=small, cases_medium=medium, stress_input=BIG_STRESS_INPUT)
    ev = V.run_gate(RUST, RUST_SUM_I64, gi, workdir=str(tmp_path / "g"), budget=FakeBudget(), limits=limits, log=lambda m: None)
    by_kind = {e.kind: e for e in ev}
    assert by_kind["diff_small"].passed and by_kind["diff_medium"].passed   # same binary is fine at realistic scale
    assert by_kind["stress"].passed   # unchecked build wraps silently and looks fine on timing alone -- the gap
    assert not by_kind["overflow"].passed
    assert "overflow" in str(by_kind["overflow"].detail).lower()

@needs_rustc
def test_overflow_step_passes_when_candidate_uses_i128(tmp_path):
    binary, err = V.compile_rust(RUST_SUM_I128, overflow_checks=True, workdir=str(tmp_path))
    assert binary is not None, err
    ev = V.overflow_check(RUST, binary, BIG_STRESS_INPUT, limit_s=5.0, mem_mb=2048)
    assert ev.passed and not ev.skipped

def test_overflow_step_skips_python_with_arbitrary_precision_reason():
    ev = V.overflow_check(PY, None, (10**18, 10**18), limit_s=1.0, mem_mb=64)
    assert ev.skipped and "arbitrary precision" in ev.detail["skipped"]

# --- validate(): reject inputs the statement's preconditions rule out before differential ever runs ---

ORACLE_REJECTS_2 = ("def reference(a, b):\n    return a + b\n"
                    "def gen(seed, mode):\n    return (seed, seed)\n"
                    "def validate(a, b):\n    return a != 2\n")

ORACLE_RAISES_ON_2 = ("def reference(a, b):\n    return a + b\n"
                      "def gen(seed, mode):\n    return (seed, seed)\n"
                      "def validate(a, b):\n    if a == 2: raise ValueError('malformed')\n    return True\n")

ORACLE_REJECTS_ALL = ("def reference(a, b):\n    return a + b\n"
                      "def gen(seed, mode):\n    return (seed, seed)\n"
                      "def validate(a, b):\n    return False\n")

def test_make_cases_validate_drops_only_the_input_it_rejects(tmp_path):
    inputs = [(1, 1), (2, 2), (3, 3)]
    cases, ev = V.make_cases(PY, ORACLE_REJECTS_2, inputs, "small", workdir=str(tmp_path), timeout_s=20)
    assert {c.input for c in cases} == {(1, 1), (3, 3)}
    assert ev.detail["checked"] == 3 and ev.detail["invalid_dropped"] == 1
    assert "validation_skipped" not in ev.detail

def test_make_cases_validate_raising_drops_only_that_input(tmp_path):
    inputs = [(1, 1), (2, 2), (3, 3)]
    cases, ev = V.make_cases(PY, ORACLE_RAISES_ON_2, inputs, "small", workdir=str(tmp_path), timeout_s=20)
    assert {c.input for c in cases} == {(1, 1), (3, 3)}
    assert ev.detail["checked"] == 3 and ev.detail["invalid_dropped"] == 1

def test_make_cases_distrusts_validator_that_rejects_everything(tmp_path):
    inputs = [(1, 1), (2, 2)]
    cases, ev = V.make_cases(PY, ORACLE_REJECTS_ALL, inputs, "small", workdir=str(tmp_path), timeout_s=20)
    assert {c.input for c in cases} == {(1, 1), (2, 2)}   # safety valve: nothing actually dropped
    assert ev.detail["invalid_dropped"] == 0
    assert "rejected every input" in ev.detail["validation_skipped"]

def test_make_cases_with_no_validate_behaves_as_before(tmp_path):
    inputs = [(1, 1), (2, 2)]
    cases, ev = V.make_cases(PY, ORACLE, inputs, "small", workdir=str(tmp_path), timeout_s=20)
    assert {c.input for c in cases} == {(1, 1), (2, 2)}
    assert ev.detail["checked"] == 0   # never counted: skipped before any validate() call could run
    assert ev.detail["invalid_dropped"] == 0
    assert ev.detail["validation_skipped"] == "no validate() defined in oracle"

RUST = Problem("p", "rust", "s", "main", [], 300.0)
RUST_ORACLE_VALIDATE = ("def reference(stdin):\n    return stdin.strip()\n"
                        "def gen(seed, mode):\n    return str(seed)\n"
                        "def validate(stdin):\n    assert isinstance(stdin, str), 'validate got a non-string arg'\n    return stdin != '2'\n")

def test_make_cases_validate_gets_raw_stdin_string_for_rust(tmp_path):
    # validate must be called via _ref_args like reference: for rust that's (stdin_text,), a single
    # string argument -- not a splatted tuple of characters or anything else.
    inputs = ["0", "1", "2"]
    cases, ev = V.make_cases(RUST, RUST_ORACLE_VALIDATE, inputs, "small", workdir=str(tmp_path), timeout_s=20)
    assert {c.input for c in cases} == {"0", "1"}
    assert ev.detail["invalid_dropped"] == 1

# --- benchfix: stdlib preamble, rust stdin dedent, degraded stress fallback ---

def test_oracle_with_no_imports_still_runs_gen_via_preamble(tmp_path):
    # F5: the oracle prompt tells the model to *use* random.Random(seed) but never tells it to
    # import anything -- a live run produced a module with no import statements at all, and gen()
    # died with NameError. The preamble prepended in prepare_gate_inputs must cover it.
    NO_IMPORT_ORACLE = ("def reference(a, b):\n    return a + b\n"
                         "def gen(seed, mode):\n    rng = random.Random(seed)\n"
                         "    hi = 20 if mode == 'small' else 10**5\n"
                         "    return (rng.randint(0, hi), rng.randint(0, hi))\n")
    limits = {"cases_small": 5, "cases_medium": 2, "mem_mb": 2048}
    gi = V.prepare_gate_inputs(PY, NO_IMPORT_ORACLE, "", limits, workdir=str(tmp_path), budget=FakeBudget(), log=lambda m: None)
    assert len(gi.cases_small) == 5 and len(gi.cases_medium) == 2
    assert not any("produced nothing" in n for n in gi.notes)

def test_rust_stdin_fixtures_are_dedented_before_reaching_the_gate(tmp_path):
    # F1: model-written EDGES are indented Python triple-quoted literals; every continuation line
    # inherits the surrounding indentation, which a line-oriented Rust stdin parser can't survive.
    rust_oracle = "def reference(stdin):\n    return stdin\ndef gen(seed, mode):\n    return str(seed)\n"
    stress = ('def gen_max(seed):\n    return "1\\n41\\n1"\n'
              'EDGES = [\n    """1\n    41\n    1""",\n]\n')
    limits = {"cases_small": 0, "cases_medium": 0, "mem_mb": 2048}
    gi = V.prepare_gate_inputs(RUST, rust_oracle, stress, limits, workdir=str(tmp_path), budget=FakeBudget(), log=lambda m: None)
    assert gi.cases_edge and gi.cases_edge[0].input == "1\n41\n1"   # not "1\n    41\n    1"
    assert gi.stress_input == "1\n41\n1"

def test_gen_max_failure_falls_back_to_largest_medium_case(tmp_path):
    # F2: gen_max failing must not silently disable the stress gate when a medium case exists --
    # fall back to the biggest one, degraded rather than skipped.
    stress_bad_genmax = "def gen_max(seed):\n    raise RuntimeError('boom')\nEDGES = [(0, 0)]\n"
    limits = {"cases_small": 3, "cases_medium": 3, "mem_mb": 2048}
    gi = V.prepare_gate_inputs(PY, ORACLE, stress_bad_genmax, limits, workdir=str(tmp_path), budget=FakeBudget(), log=lambda m: None)
    assert gi.cases_medium   # sanity: the fallback pool actually exists
    biggest = max(gi.cases_medium, key=lambda c: len(str(c.input)))
    assert gi.stress_input == biggest.input
    assert gi.stress_degraded is True
    assert any("degraded" in n for n in gi.notes)
    ev = V.stress(PY, GOOD, gi.stress_input, workdir=str(tmp_path / "s"), limit_s=5.0, mem_mb=2048, degraded=gi.stress_degraded)
    assert "degraded" in ev.detail

def test_gen_max_failure_with_no_medium_cases_still_skips(tmp_path):
    # The old behavior (skip, not degrade) must be preserved when there's no fallback pool at all.
    stress_bad_genmax = "def gen_max(seed):\n    raise RuntimeError('boom')\nEDGES = [(0, 0)]\n"
    limits = {"cases_small": 0, "cases_medium": 0, "mem_mb": 2048}
    gi = V.prepare_gate_inputs(PY, ORACLE, stress_bad_genmax, limits, workdir=str(tmp_path), budget=FakeBudget(), log=lambda m: None)
    assert gi.stress_input is None and gi.stress_degraded is False
    ev = V.stress(PY, GOOD, gi.stress_input, workdir=str(tmp_path / "s"), limit_s=5.0, mem_mb=2048, degraded=gi.stress_degraded)
    assert ev.skipped

# --- oracle self-repair: prepare_gate_inputs yielded zero usable cases in every tier ---

ORACLE_ALWAYS_RAISES = "def reference(a, b):\n    raise NameError('boom')\ndef gen(seed, mode):\n    return (1, 2)\n"

def test_oracle_unusable_true_when_every_tier_is_empty(tmp_path):
    gi = V.prepare_gate_inputs(PY, ORACLE_ALWAYS_RAISES, "", {"cases_small": 3, "cases_medium": 2, "mem_mb": 2048}, workdir=str(tmp_path), budget=FakeBudget(), log=lambda m: None)
    assert gi.cases_small == [] and gi.cases_medium == [] and gi.cases_edge == []
    assert V.oracle_unusable(gi)

def test_oracle_unusable_false_when_any_tier_has_cases(tmp_path):
    gi = V.prepare_gate_inputs(PY, ORACLE, "", {"cases_small": 3, "cases_medium": 2, "mem_mb": 2048}, workdir=str(tmp_path), budget=FakeBudget(), log=lambda m: None)
    assert gi.cases_small   # ORACLE is healthy: small tier is non-empty
    assert not V.oracle_unusable(gi)

# --- public examples: parsed and honest, ground truth, never validated or dropped ---

WRONG_ON_PUBLIC = "def add(a, b):\n    return a + b + 1\n"

def test_public_examples_run_as_own_gate_step_before_generated_tiers_and_fail_a_wrong_candidate(tmp_path):
    p = Problem("p", "python", "s", "add", [{"input": [2, 3], "output": 5}], 300.0)
    limits = {"cases_small": 3, "cases_medium": 2, "mem_mb": 2048, "stress_limit_python_s": 5.0, "stress_limit_rust_s": 2.0}
    gi = V.prepare_gate_inputs(p, ORACLE, "", limits, workdir=str(tmp_path / "gi"), budget=FakeBudget(), log=lambda m: None)
    assert len(gi.cases_public) == 1 and gi.cases_public[0].input == (2, 3) and gi.cases_public[0].expected == 5
    ev = V.run_gate(p, WRONG_ON_PUBLIC, gi, workdir=str(tmp_path / "g"), budget=FakeBudget(), limits=limits, log=lambda m: None)
    # diff_public is its own evidence kind and runs right after compile, before any generated tier --
    # ahead of the model-written oracle's tiers, which still run (the gate records every tier).
    assert [e.kind for e in ev][:3] == ["compile", "diff_examples", "diff_public"]
    assert not ev[2].passed
    assert ev[2].detail["input"] == (2, 3) and ev[2].detail["expected"] == 5 and ev[2].detail["actual"] == 6

def test_public_example_disagreeing_with_oracle_reference_is_kept_and_recorded(tmp_path):
    p = Problem("p", "python", "s", "add", [{"input": [2, 3], "output": 5}], 300.0)
    wrong_oracle = "def reference(a, b):\n    return a + b + 100\ndef gen(seed, mode):\n    return (seed, seed)\n"
    limits = {"cases_small": 2, "cases_medium": 1, "mem_mb": 2048, "stress_limit_python_s": 5.0, "stress_limit_rust_s": 2.0}
    gi = V.prepare_gate_inputs(p, wrong_oracle, "", limits, workdir=str(tmp_path / "gi"), budget=FakeBudget(), log=lambda m: None)
    # kept exactly as given -- not dropped, not overridden by the oracle's (wrong) answer
    assert len(gi.cases_public) == 1 and gi.cases_public[0].expected == 5
    assert gi.public_disagreements and gi.public_disagreements[0]["public_expected"] == 5
    assert any("disagrees with a public example" in n for n in gi.notes)
    ev = V.run_gate(p, GOOD, gi, workdir=str(tmp_path / "g"), budget=FakeBudget(), limits=limits, log=lambda m: None)
    diff_public = next(e for e in ev if e.kind == "diff_public")
    assert diff_public.passed   # candidate matches the PUBLIC example even though the oracle disagreed
    assert "oracle_disagreements" in diff_public.detail

def test_unparseable_public_example_entry_is_skipped_with_a_note(tmp_path):
    p = Problem("p", "python", "s", "add", [{"input": [2, 3], "output": 5}, "not a valid example", [1, 2, 3]], 300.0)
    limits = {"cases_small": 2, "cases_medium": 1, "mem_mb": 2048}
    gi = V.prepare_gate_inputs(p, ORACLE, "", limits, workdir=str(tmp_path), budget=FakeBudget(), log=lambda m: None)
    assert len(gi.cases_public) == 1   # only the recognizable entry kept; nothing crashed
    assert sum("unrecognized shape" in n for n in gi.notes) == 2

def test_no_public_examples_behaves_exactly_as_before(tmp_path):
    limits = {"cases_small": 5, "cases_medium": 2, "stress_limit_python_s": 5.0, "stress_limit_rust_s": 2.0, "mem_mb": 2048, "shrink_budget_s": 1.0}
    stress = "def gen_max(seed):\n    return (10**18, 10**18)\nEDGES = [(0, 0), (15, 0)]\n"
    gi = V.prepare_gate_inputs(PY, ORACLE, stress, limits, workdir=str(tmp_path / "gi"), budget=FakeBudget(), log=lambda m: None)
    assert gi.cases_public == [] and gi.public_disagreements == []
    ev = V.run_gate(PY, GOOD, gi, workdir=str(tmp_path / "g"), budget=FakeBudget(), limits=limits, log=lambda m: None)
    assert [e.kind for e in ev] == ["compile", "diff_examples", "diff_edge", "diff_small", "diff_medium", "behavior", "stress", "overflow"]


def test_oracle_unusable_ignores_edge_cases(tmp_path):
    # A hallucinated-stdlib oracle produces 0 small / 0 medium but the STRESS call's literal EDGES
    # still price fine. One surviving edge fixture must not mask a dead oracle (R2-1).
    edge_only = V.GateInputs(oracle_src="x", cases_edge=[V.Case((1, 2), 3, "edge")])
    assert V.oracle_unusable(edge_only)
    assert V.oracle_unusable(V.GateInputs(oracle_src="x"))
    assert not V.oracle_unusable(V.GateInputs(oracle_src="x", cases_small=[V.Case((1, 2), 3, "small")]))
    assert not V.oracle_unusable(V.GateInputs(oracle_src="x", cases_medium=[V.Case((1, 2), 3, "medium")]))


def test_crashed_candidate_is_not_reported_as_a_near_miss_diff(tmp_path):
    # A candidate that raises must not have its partial output presented as the answer: the repair
    # prompt reads `actual`, and a fragment that looks almost right sends it after a phantom
    # formatting bug instead of the crash (R2-2).
    crashes = "def add(a, b):\n    raise ValueError('boom')\n"
    ev = V.differential(PY, crashes, [V.Case((1, 2), 3, "small")], "diff_small",
                        workdir=str(tmp_path / "d"), timeout_s=10)
    assert not ev.passed
    assert "no answer" in str(ev.detail["actual"])
    assert "ValueError" in ev.detail["error"]


def test_passing_candidate_still_reports_its_real_output(tmp_path):
    ev = V.differential(PY, BUG, [V.Case((20, 1), 21, "small")], "diff_small",
                        workdir=str(tmp_path / "d"), timeout_s=10)
    assert not ev.passed and ev.detail["actual"] == 22   # wrong, but it ran: show the real answer


# --- round 6: every input the gate uses must pass the oracle's validate() ---

ORACLE_VALIDATE_ONLY_ORIGINAL = ("def reference(a, b):\n    return a + b\n"
                                 "def gen(seed, mode):\n    return (seed, seed)\n"
                                 "def validate(a, b):\n    return (a, b) == (1000, 999) or a < 5\n")

def test_shrink_keeps_the_original_when_every_smaller_input_is_invalid(tmp_path):
    # _shrink_value knows nothing about preconditions; a shrunk input validate() rejects is a phantom
    # counterexample (it later fails every candidate as a regression), so the step must be rejected.
    case = V.Case((1000, 999), 1999, "small")
    out = V.shrink(PY, BUG, ORACLE_VALIDATE_ONLY_ORIGINAL, case, workdir=str(tmp_path), budget_s=3.0, validate_trusted=True)
    assert out.input == (1000, 999) and out.expected == 1999

def test_differential_failure_counts_every_mismatch_not_just_the_first(tmp_path):
    # How wholesale the disagreement is decides whether the repair model should hunt a boundary bug
    # or re-read the statement; the batch already ran, so counting the rest is free.
    cases = [V.Case((a, 1), a + 1, "small") for a in range(30)]   # BUG is wrong for a >= 15
    ev = V.differential(PY, BUG, cases, "diff_small", workdir=str(tmp_path), timeout_s=20)
    assert not ev.passed and ev.detail["mismatches"] == 15 and ev.detail["cases"] == 30

def test_tier_is_dropped_when_validate_rejects_all_and_reference_answers_the_same(tmp_path):
    # 4e49a099fd84: the oracle's parser demanded two tokens on the first line, so validate() rejected
    # all ten edges and reference() returned "" for every one of them -- and the gate then failed the
    # candidate for printing the right answer. Two signals together mean the tier is unusable.
    blind = ("def reference(a, b):\n    return 0\n"
             "def gen(seed, mode):\n    return (seed + 1, seed + 2)\n"
             "def validate(a, b):\n    return False\n")
    limits = {"cases_small": 4, "cases_medium": 0, "mem_mb": 2048}
    gi = V.prepare_gate_inputs(PY, blind, "", limits, workdir=str(tmp_path), budget=FakeBudget(), log=lambda m: None)
    assert gi.cases_small == []
    assert any("oracle cannot parse this format" in n for n in gi.notes)

def test_a_genuinely_constant_answer_survives_when_validate_accepts(tmp_path):
    # Only the two signals together are fatal: a constant reference with a working validate is fine.
    const = ("def reference(a, b):\n    return 0\n"
             "def gen(seed, mode):\n    return (seed + 1, seed + 2)\n"
             "def validate(a, b):\n    return True\n")
    limits = {"cases_small": 4, "cases_medium": 0, "mem_mb": 2048}
    gi = V.prepare_gate_inputs(PY, const, "", limits, workdir=str(tmp_path), budget=FakeBudget(), log=lambda m: None)
    assert len(gi.cases_small) == 4 and not any("cannot parse" in n for n in gi.notes)


def test_medium_tier_yields_to_repair_when_the_budget_is_already_late(tmp_path):
    class LateBudget(FakeBudget):
        def phase(self): return "repair"
    limits = {"cases_small": 5, "cases_medium": 3, "mem_mb": 2048}
    gi = V.prepare_gate_inputs(PY, ORACLE, "", limits, workdir=str(tmp_path / "gi"), budget=LateBudget(), log=lambda m: None)
    assert len(gi.cases_small) == 5 and gi.cases_medium == []
    assert any("medium tier skipped" in n for n in gi.notes)
    gi2 = V.prepare_gate_inputs(PY, ORACLE, "", limits, workdir=str(tmp_path / "gi2"), budget=FakeBudget(), log=lambda m: None)
    assert len(gi2.cases_medium) == 3


def test_repairing_a_root_again_shows_the_failed_childs_diff():
    # Repairing c1 a second time (its first repair produced c2, which did not fix the case) must not
    # look like a first attempt: the model has to see the change that already failed.
    import solve as S
    root = S.Candidate("c1", GOOD, None, [V.Evidence("diff_small", False, detail={"input": (1, 2)})])
    child = S.Candidate("c2", BUG, "c1", [])
    run = type("R", (), {"cands": [root, child]})()
    note = V._previous_attempt_note(run, root, root.evidence[0])
    assert "a + b + 1" in note and "did NOT fix" in note
    # a root with no child at all is still a first attempt
    assert V._previous_attempt_note(type("R", (), {"cands": [root]})(), root, root.evidence[0]) == ""


# --- round 13: the SOLVE author's hand-traced examples, checked against candidate and oracle ---

def test_parse_examples_keeps_well_formed_pairs_and_drops_the_rest():
    block = ("((0, 0), 0)\n"
             "((1, 2), 3)\n"
             "not a tuple at all\n"
             "(1, 2, 3)\n"          # not a 2-tuple
             "\n"
             "(5, 5)\n")            # args 5 is not a tuple -> wrapped as (5,)
    cases = V.parse_examples(PY, block)
    assert [(c.input, c.expected) for c in cases] == [((0, 0), 0), ((1, 2), 3), ((5,), 5)]
    assert V.example_line_count(block) - len(cases) == 2   # the two malformed lines
    assert all(c.tag == "example" for c in cases)

def test_parse_examples_caps_at_eight_and_takes_rust_stdin_strings():
    many = "\n".join(f"(({i}, 0), {i})" for i in range(20))
    assert len(V.parse_examples(PY, many)) == 8
    rust = V.parse_examples(RUST, '("3\\n1 2 3\\n", "6")\n((1, 2), 3)\n')
    assert [(c.input, c.expected) for c in rust] == [("3\n1 2 3\n", "6")]   # non-str args dropped for rust

def test_diff_examples_fails_a_candidate_that_contradicts_its_own_trace(tmp_path):
    limits = {"cases_small": 3, "cases_medium": 1, "mem_mb": 2048, "stress_limit_python_s": 5.0, "stress_limit_rust_s": 2.0}
    gi = V.prepare_gate_inputs(PY, ORACLE, "", limits, workdir=str(tmp_path / "gi"), budget=FakeBudget(), log=lambda m: None)
    examples = [V.Case((15, 0), 15, "example")]   # BUG returns 16 here
    ev = V.run_gate(PY, BUG, gi, workdir=str(tmp_path / "g"), budget=FakeBudget(), limits=limits, log=lambda m: None, examples=examples)
    assert [e.kind for e in ev][:2] == ["compile", "diff_examples"]
    assert not ev[1].passed and ev[1].detail["input"] == (15, 0) and ev[1].detail["actual"] == 16

def test_rust_examples_run_as_stdin_and_stdout(tmp_path):
    # Rust path: args is the whole stdin, expected the whole stdout, compared as token lists.
    captured = {}
    def fake_run_candidate(problem, source, inputs, *, workdir, timeout_s, binary=None, overflow_checks=True, mem_mb=4096, per_case_s=0.0, deadline_s=None, max_consec_timeouts=0):
        captured["inputs"] = list(inputs)
        return [V.CaseResult(True, output="6\n") for _ in inputs]
    import verify
    old = verify.run_candidate
    verify.run_candidate = fake_run_candidate
    try:
        cases = V.parse_examples(RUST, '("3\\n1 2 3\\n", "6")\n')
        ev = V.differential(RUST, "src", cases, "diff_examples", workdir=str(tmp_path), timeout_s=10.0, binary="bin")
    finally:
        verify.run_candidate = old
    assert ev.passed and captured["inputs"] == ["3\n1 2 3\n"]

def test_oracle_checked_against_every_candidates_examples(tmp_path):
    limits = {"cases_small": 3, "cases_medium": 1, "mem_mb": 2048}
    examples = {"c1": [V.Case((1, 2), 3, "example"), V.Case((5, 5), 10, "example")],
                "c2": [V.Case((1, 2), 3, "example")]}   # deduped by repr(args)
    wrong = "def reference(a, b):\n    return a + b + 100\ndef gen(seed, mode):\n    return (seed, seed)\n"
    lines = []
    gi = V.prepare_gate_inputs(PY, wrong, "", limits, workdir=str(tmp_path / "a"), budget=FakeBudget(), log=lines.append, examples=examples)
    ec = gi.example_checks
    assert ec["cases"] == 2 and len(ec["disagreements"]) == 2 and ec["errors"] == 0
    assert sorted(ec["disagreements"][0]["authors"]) == ["c1", "c2"]
    assert V.examples_disputed(gi)
    assert any("oracle.examples cases=2 disagreements=2" in l for l in lines)
    # the dispute context carries inputs and expected values, never candidate code
    extra = V.examples_dispute_extra(gi)
    assert "Hand-traced expected: 3" in extra and "Your reference returned: 103" in extra
    # an oracle that agrees disputes nothing
    ok = V.prepare_gate_inputs(PY, ORACLE, "", limits, workdir=str(tmp_path / "b"), budget=FakeBudget(), log=lambda m: None, examples=examples)
    assert ok.example_checks["disagreements"] == [] and not V.examples_disputed(ok)
    assert not V.examples_disputed(V.GateInputs())   # no examples at all: never disputed


# --- round 13: validate() is "reference() did not raise ValueError" ---

ORACLE_NO_VALIDATE_RAISES = ("def reference(a, b):\n"
                             "    if a > 10: raise ValueError('a out of range')\n"
                             "    return a + b\n"
                             "def gen(seed, mode):\n    return (seed * 5, 1)\n")

def test_validate_template_is_appended_when_the_oracle_defines_none(tmp_path):
    src = "def reference(a, b):\n    return a + b\n"
    out = V._ensure_validate(src)
    assert "def validate(*args):" in out and "except ValueError:" in out
    # already has one -> kept verbatim, we do not fight the model
    own = src + "def validate(a, b):\n    return a < 3\n"
    assert V._ensure_validate(own) == own
    # nothing to wrap
    assert V._ensure_validate("def gen(seed, mode):\n    return (1, 2)\n") == "def gen(seed, mode):\n    return (1, 2)\n"

def test_a_reference_raising_valueerror_makes_validate_false_and_drops_the_input(tmp_path):
    limits = {"cases_small": 4, "cases_medium": 0, "mem_mb": 2048}
    gi = V.prepare_gate_inputs(PY, ORACLE_NO_VALIDATE_RAISES, "", limits, workdir=str(tmp_path), budget=FakeBudget(), log=lambda m: None)
    # gen gives a = 0, 5, 10, 15; only a > 10 raises, so exactly one input is invalid
    assert [c.input for c in gi.cases_small] == [(0, 1), (5, 1), (10, 1)]
    assert gi.validate_trusted and gi.validate_rejects["small"][1] == 1

ORACLE_CRASHES_ON_MOST = ("def reference(a, b):\n"
                          "    if a: raise KeyError('missing')\n"
                          "    return a + b\n"
                          "def gen(seed, mode):\n    return (seed, 1)\n")

def test_a_reference_crashing_on_its_own_gen_inputs_counts_toward_the_reject_fraction(tmp_path):
    limits = {"cases_small": 5, "cases_medium": 2, "mem_mb": 2048}
    gi = V.prepare_gate_inputs(PY, ORACLE_CRASHES_ON_MOST, "", limits, workdir=str(tmp_path), budget=FakeBudget(), log=lambda m: None)
    assert gi.cases_small and not V.oracle_unusable(gi)   # seed 0 survives: not the "no usable case" trigger
    assert gi.validate_reject_frac > 0.5                  # a KeyError is a broken reference, not an invalid input
    assert any("crashed on" in n for n in gi.notes)


# --- round 13: the shrinker must not empty a list that started non-empty ---

def test_shrink_keeps_a_non_empty_witness_for_a_list_dimension(tmp_path):
    # round-10 L6: the failure reproduces even with edits=[], and the shrinker used to hand the repair
    # model exactly that -- which then deleted the edit handling. A one-element witness is required.
    p = Problem("p", "python", "s", "f", [], 300.0)
    oracle = "def reference(xs, edits):\n    return len(xs) + len(edits)\ndef gen(seed, mode):\n    return ([1], [1])\n"
    bug = "def f(xs, edits):\n    return len(xs)\n"   # wrong for every input, including edits == []
    out = V.shrink(p, bug, oracle, V.Case(([1, 2, 3], [4, 5, 6]), 6, "small"), workdir=str(tmp_path), budget_s=5.0)
    assert out.input[1] != [] and out.input[0] != []
    assert len(out.input[1]) < 3   # still shrunk, just never to empty

def test_shrink_value_never_yields_an_empty_sequence():
    assert all(len(c) > 0 for c in V._shrink_value([1, 2, 3, 4]) if isinstance(c, list))
    assert all(c != [] for c in V._shrink_value([7]))   # a one-element list shrinks its element, never to []
    assert all(len(c) > 0 for c in V._shrink_value((1, 2)) if isinstance(c, tuple))
