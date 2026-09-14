import json, os, pathlib, re, pytest
from llm import FakeLLM
from sandbox import Problem
import solve as S

def prob(tmp_path, lang="python"):
    return Problem("pid", lang, "Return a+b for ints a,b. At most 10^18.", "add" if lang == "python" else "main", [], 300.0)

def cfg():
    return {"limits": {"safety_margin_s": 15.0, "cases_small": 5, "cases_medium": 2, "stress_limit_python_s": 5.0, "stress_limit_rust_s": 2.0, "max_repairs": 2, "max_syntax_repairs": 2, "mem_mb": 2048, "shrink_budget_s": 1.0, "max_cost_usd_per_problem": 0.10, "oracle_retry_afford_s": 130.0, "repair_afford_s": 60.0, "oracle_selfrepair_afford_s": 130.0, "validate_reject_frac_regen": 0.5},
            "phases": {"generate_until": 0.32, "gate_until": 0.39, "repair_until": 0.81, "settle_until": 0.93, "generate_call_share": 0.45, "repair_call_share": 0.25}, "profile": "fake"}

# A SOLVE reply with no ===EXAMPLES=== block: the diff_examples gate step is then skipped.
SOLVE_NO_EXAMPLES = "===RULES===\nr\n===END===\n===TRAPS===\nt\n===END===\n===ALGORITHM===\na\n===END===\n===CODE===\n```python\ndef add(a, b):\n    return a + b\n```\n===END===\n"
# Hand-traced examples, all with a < 15 so the planted a>=15 bug below is still caught by EDGES
# (15, 0) at diff_edge rather than short-circuiting the gate at diff_examples.
EXAMPLES_BLOCK = "===EXAMPLES===\n((0, 0), 0)\n((1, 2), 3)\n((3, 4), 7)\n===END===\n"
SOLVE_OK = SOLVE_NO_EXAMPLES + EXAMPLES_BLOCK
ORACLE_OK = "===ORACLE===\nimport random\ndef reference(a, b):\n    return a + b\ndef gen(seed, mode):\n    r = random.Random(seed)\n    return (r.randint(0, 20), r.randint(0, 20))\n===END===\n"
STRESS_OK = "===STRESS===\ndef gen_max(seed):\n    return (10**18, 10**18)\nEDGES = [(0, 0), (15, 0)]\n===END===\n"
# oracle variants for the max-size input: gen(seed, "large") is the fallback when gen_max is unusable
ORACLE_WITH_LARGE = ORACLE_OK.replace("def gen(seed, mode):\n", "def gen(seed, mode):\n    if mode == 'large':\n        return (10**18, 10**18)\n")
ORACLE_NO_LARGE = ORACLE_OK.replace("def gen(seed, mode):\n", "def gen(seed, mode):\n    if mode == 'large':\n        raise ValueError('no large mode')\n")   # (15, 0) deterministically exposes the planted a>=15 bug used in later tests

def test_single_shot_emits_solution_and_report(tmp_path):
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    out = tmp_path / "solution.py"; run_dir = tmp_path / "run"
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(out), run_dir=str(run_dir))
    assert out.read_text().startswith("def add")
    assert rep["status"] in ("passed_all_gates", "emitted_unverified") and rep["final_candidate"] == "c1"
    assert (run_dir / "report.json").exists() and (run_dir / "candidates" / "c1.py").exists()
    assert rep["deadline_scale"] == 1.0 and {c["tag"] for c in rep["calls"]} == {"solve", "oracle", "stress"}
    # regression: the emit log line must be recorded before the report is written, not after --
    # otherwise report.json on disk (as opposed to the returned dict) lacks it.
    on_disk = json.loads((run_dir / "report.json").read_text())
    assert any("emit candidate=c1" in line for line in on_disk["events"])

def test_solver_timeout_is_reported_as_inconclusive(tmp_path):
    llm = FakeLLM({"solve": ["", ""], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    llm.script["solve"] = ["", ""]
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["status"] == "no_candidate" and rep["solver_status"] == "malformed"

def test_broken_oracle_and_no_stress_emits_unverified_not_passed(tmp_path):
    # oracle whose reference() always raises, and no STRESS block at all -> the gate has zero real
    # cases and no stress input, so every gate step is "skipped" rather than genuinely exercised.
    ORACLE_BROKEN = "===ORACLE===\ndef reference(a, b):\n    raise RuntimeError('broken')\ndef gen(seed, mode):\n    return (1, 2)\n===END===\n"
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_BROKEN], "stress": [""]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["status"] == "emitted_unverified"
    evs = rep["evidence"]["c1"]
    assert all(e["passed"] for e in evs)
    assert any(e.get("skipped") for e in evs)
    # and log.txt must say so: a skipped step logged as passed=True reads as a clean sweep
    skipped_lines = [l for l in rep["events"] if "skipped=True reason=" in l]
    assert skipped_lines and not any("gate.diff_small passed=True cases=0" in l for l in rep["events"])

def test_degraded_stress_evidence_is_not_a_full_pass(tmp_path):
    # gen_max crashes -> stress falls back to the largest medium case and marks itself degraded.
    # Every gate step still "passes", but the max-size check never really happened.
    STRESS_NO_GENMAX = "===STRESS===\ndef gen_max(seed):\n    raise RuntimeError('boom')\nEDGES = [(0, 0)]\n===END===\n"
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_NO_LARGE], "stress": [STRESS_NO_GENMAX]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    ev = {e["kind"]: e for e in rep["evidence"]["c1"]}
    # A clean run on a not-max-size input is not evidence the candidate is fast enough, so the step
    # records itself as skipped rather than as a pass.
    assert ev["stress"]["skipped"] and ev["stress"]["detail"].get("degraded")
    assert rep["gate_inputs"]["stress_source"] == "medium_degraded"
    assert rep["status"] == "emitted_unverified"

def test_static_failure_repair_reaches_cap_and_emits_best(tmp_path):
    # each scripted repair reply is genuinely repair-shaped (VERDICT + CODE) and produces DIFFERENT
    # source each time, so the loop keeps making progress and burns both repair attempts (max_repairs=2)
    # instead of breaking early on "no progress" -- this is what the old, coincidentally-passing
    # version of this test failed to actually exercise.
    bad = SOLVE_OK.replace("def add(a, b):", "def wrong(a, b):")
    repair1 = "===VERDICT===\ncandidate\n===END===\n===CODE===\ndef wrong1(a, b):\n    return a + b\n===END===\n"
    repair2 = "===VERDICT===\ncandidate\n===END===\n===CODE===\ndef wrong2(a, b):\n    return a + b\n===END===\n"
    llm = FakeLLM({"solve": [bad], "repair": [repair1, repair2], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["status"] == "emitted_with_failures"
    # a "static" failure (wrong entrypoint name) is mechanical, not semantic -- it must spend the
    # separate syntax-repair budget, leaving the semantic `repairs` counter untouched.
    assert rep["syntax_repairs"] == 2 and rep["repairs"] == 0
    assert (tmp_path / "s.py").exists()
    assert any(e["kind"] == "static" and not e["passed"] for e in rep["evidence"]["c1"])

def test_repair_no_progress_breaks_early(tmp_path):
    # the repair reply parses to code identical to the current candidate -> the loop detects no
    # progress and breaks instead of consuming further scripted repair attempts.
    bad = SOLVE_OK.replace("def add(a, b):", "def wrong(a, b):")
    llm = FakeLLM({"solve": [bad], "repair": [bad, bad], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["status"] == "emitted_with_failures"
    assert rep["syntax_repairs"] == 1 and rep["repairs"] == 0   # "static" failure -> syntax budget
    assert len(llm.script["repair"]) == 1

def test_invalid_problem_exit_code(tmp_path):
    p = tmp_path / "bad.json"; p.write_text("{}")
    assert S.main([str(p), "-o", str(tmp_path / "x.py")]) == 2   # exits before any config or network use

def test_render_does_not_rescan_substituted_text(tmp_path):
    p = Problem("pid", "python", "This engine uses {{language}} and {{entrypoint}} as template tokens.", "add", [], 300.0)
    rendered = S.render("solve", **S.prompt_vars(p))
    assert "{{language}} and {{entrypoint}}" in rendered
    assert "Target language: python." in rendered

def test_render_leaves_unknown_placeholders(tmp_path):
    vars = S.prompt_vars(prob(tmp_path))
    del vars["contract"]
    rendered = S.render("solve", **vars)
    assert "{{contract}}" in rendered

BUGGY = SOLVE_OK.replace("return a + b", "return a + b if a < 15 else a + b + 1")
REPAIR_FIX = "===VERDICT===\ncandidate\n===END===\n===CODE===\ndef add(a, b):\n    return a + b\n===END===\n"
REPAIR_BLAME_ORACLE = "===VERDICT===\noracle\n===END===\n===CODE===\ndef add(a, b):\n    return a + b if a < 15 else a + b + 1\n===END===\n"
ORACLE_WRONG = ORACLE_OK.replace("return a + b\n", "return a + b if a < 15 else a + b + 1\n", 1)

def test_repair_flow_fixes_planted_bug(tmp_path):
    llm = FakeLLM({"solve": [BUGGY], "repair": [REPAIR_FIX], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["status"] == "passed_all_gates" and rep["final_candidate"] == "c2" and rep["repairs"] == 1
    repair_prompt = next(u for r, s, u in llm.prompts if "Failure kind" in u)
    assert "(15, 0)" in repair_prompt or "15" in repair_prompt   # shrunk/edge counterexample is in the prompt

def test_repair_can_blame_oracle_and_regenerate_once(tmp_path):
    llm = FakeLLM({"solve": [SOLVE_OK], "repair": [REPAIR_BLAME_ORACLE], "oracle": [ORACLE_WRONG, ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["oracle_regenerated"] == 1 and rep["status"] == "passed_all_gates" and rep["final_candidate"] == "c1"

def test_repair_stops_at_max_repairs_and_emits_best(tmp_path):
    still_buggy = "===VERDICT===\ncandidate\n===END===\n===CODE===\ndef add(a, b):\n    return a + b if a < 15 else a + b + 2\n===END===\n"
    llm = FakeLLM({"solve": [BUGGY], "repair": [still_buggy, still_buggy, still_buggy], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["status"] == "emitted_with_failures" and rep["repairs"] == 2 and len(llm.script["repair"]) == 1

def test_cost_cap_blocks_the_repair_call(tmp_path):
    # 3 initial calls * 0.05 = 0.15 against a 0.10 cap -> every optional call after them is off.
    llm = FakeLLM({"solve": [BUGGY], "repair": [REPAIR_FIX], "oracle": [ORACLE_OK], "stress": [STRESS_OK]}, cost=0.05)
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert [c["tag"] for c in llm.calls].count("repair") == 0
    assert rep["repairs"] == 0 and rep["status"] == "emitted_with_failures"
    assert sum("cost.cap reached" in e for e in rep["events"]) == 1   # logged once, not per check

def test_cost_cap_blocks_the_oracle_retry_and_selfrepair(tmp_path):
    # empty ORACLE reply -> a retry AND, since no tier has cases, a self-repair would both fire.
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": ["", ORACLE_OK], "stress": [STRESS_OK]}, cost=0.05)
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert [c["tag"] for c in llm.calls].count("oracle") == 1
    assert rep["oracle_selfrepaired"] == 0 and any("cost.cap reached" in e for e in rep["events"])

def test_deadline_forces_emit_without_repair(tmp_path):
    class Clock:
        t = 0.0
        def __call__(self):
            Clock.t += 120.0   # every clock read advances 2 minutes: budget is exhausted after intake
            return Clock.t
    llm = FakeLLM({"solve": [BUGGY], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"), clock=Clock())
    assert rep["repairs"] == 0 and (tmp_path / "s.py").exists() and rep["status"] != "passed_all_gates"

REPAIR_BLAME_ORACLE_WITH_TRACE = (
    "I traced through the statement by hand: when a=15, the spec says simple addition, so the reference must be wrong here.\n"
    "===VERDICT===\noracle\n===END===\n===CODE===\ndef add(a, b):\n    return a + b if a < 15 else a + b + 1\n===END===\n")

def test_oracle_regen_prompt_never_shows_the_candidate_or_the_repair_trace(tmp_path):
    # The oracle is generated in its own context and must never see the candidate. The regeneration
    # prompt used to carry the repair model's prose about the candidate, and the new oracle then
    # inherited the candidate's exact bug (1dea32802072: 4000/4000 agreement with a wrong candidate).
    llm = FakeLLM({"solve": [SOLVE_OK], "repair": [REPAIR_BLAME_ORACLE_WITH_TRACE], "oracle": [ORACLE_WRONG, ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["oracle_regenerated"] == 1
    oracle_prompts = [u for c, (r, s, u) in zip(llm.calls, llm.prompts) if c["tag"] == "oracle"]
    assert len(oracle_prompts) == 2
    assert "the spec says simple addition" not in oracle_prompts[1]   # the repair model's trace
    assert "def add(" not in oracle_prompts[1]                        # the candidate's source
    assert "An independent review believes" in oracle_prompts[1]      # the neutral sentence that replaces it

def test_repair_prompt_truncates_large_statement_and_code(tmp_path):
    long_statement = "Return a+b for ints a,b. At most 10**18. " + ("x" * 50000)
    p = Problem("pid", "python", long_statement, "add", [], 300.0)
    padded_buggy = BUGGY.replace(
        "return a + b if a < 15 else a + b + 1",
        "return a + b if a < 15 else a + b + 1  # " + ("y" * 50000))
    llm = FakeLLM({"solve": [padded_buggy], "repair": [REPAIR_FIX], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(p, llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["repairs"] == 1
    repair_prompt = next(u for r, s, u in llm.prompts if "Failure kind" in u)
    assert len(repair_prompt) < 40000
    assert "...[truncated]" in repair_prompt

def test_oracle_regen_failure_stops_repair_loop(tmp_path):
    llm = FakeLLM({"solve": [SOLVE_OK], "repair": [REPAIR_BLAME_ORACLE], "oracle": [ORACLE_WRONG, ""], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["oracle_regenerated"] == 1
    assert rep["repairs"] == 1   # regen failed -> loop stopped before a second repair attempt
    assert rep["status"] == "emitted_with_failures"

def test_bench_writes_table(tmp_path):
    sd = tmp_path / "samples"; sd.mkdir()
    (sd / "a.json").write_text(json.dumps({"problem_id": "aaaa", "language": "python", "statement": "s", "entrypoint": "add", "public_examples": [], "deadline_s": 300.0}))
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rows = S.bench(str(sd), str(tmp_path / "bench"), cfg(), llm, 1.0)
    md = (tmp_path / "bench" / "benchmark.md").read_text()
    assert len(rows) == 1 and "| aaaa" in md and "unknown" in md   # hidden-test column is always unknown

def test_bench_scopes_usage_per_problem(tmp_path):
    # two problems that both take the identical single-shot path (3 calls each): if bench() failed to
    # clear llm.calls between problems, row 2's calls/tokens would be a cumulative total of both
    # problems (6 calls, 6/6 tokens) instead of just its own (3 calls, 3/3 tokens).
    sd = tmp_path / "samples"; sd.mkdir()
    for pid in ("aaaa", "bbbb"):
        (sd / f"{pid}.json").write_text(json.dumps({"problem_id": pid, "language": "python", "statement": "s", "entrypoint": "add", "public_examples": [], "deadline_s": 300.0}))
    llm = FakeLLM({"solve": [SOLVE_OK, SOLVE_OK], "oracle": [ORACLE_OK, ORACLE_OK], "stress": [STRESS_OK, STRESS_OK]})
    rows = S.bench(str(sd), str(tmp_path / "bench"), cfg(), llm, 1.0)
    assert len(rows) == 2
    assert rows[0]["calls"] == rows[1]["calls"] == 3   # not a running total (would be 3 then 6)
    for r in rows:
        prompt_toks, completion_toks = (int(x) for x in r["tokens"].split("/"))
        assert prompt_toks <= 3 and completion_toks <= 3   # per-problem, not cumulative across rows

def test_bench_isolates_bad_sample(tmp_path):
    sd = tmp_path / "samples"; sd.mkdir()
    (sd / "a.json").write_text(json.dumps({"problem_id": "aaaa", "language": "python", "statement": "s", "entrypoint": "add", "public_examples": [], "deadline_s": 300.0}))
    (sd / "bad.json").write_text("{not valid json")
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rows = S.bench(str(sd), str(tmp_path / "bench"), cfg(), llm, 1.0)
    assert len(rows) == 2
    good = next(r for r in rows if r["id"] == "aaaa")
    bad = next(r for r in rows if r["id"] != "aaaa")
    assert not good["status"].startswith("error")
    assert bad["status"].startswith("error:")
    md = (tmp_path / "bench" / "benchmark.md").read_text()
    assert "| aaaa" in md and "error" in md

def test_bench_missing_dir_exit_code(tmp_path):
    assert S.main([str(tmp_path / "does-not-exist"), "-o", str(tmp_path / "out"), "--bench"]) == 2

# --- fix round 2 (final review wave) ---

def test_oracle_gen_bare_scalar_does_not_crash_the_run(tmp_path):
    # C1(b): _ref_args used to do tuple(inp), which raises TypeError on a non-iterable like a bare
    # int. That must not crash the run -- it should just fail to produce cases, not lose the emit.
    ORACLE_BARE = "===ORACLE===\ndef reference(a, b):\n    return a + b\ndef gen(seed, mode):\n    return seed\n===END===\n"
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_BARE], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert (tmp_path / "s.py").exists()
    assert (tmp_path / "run" / "report.json").exists()

def test_oracle_gen_returning_a_list_still_unpacks_correctly(tmp_path):
    # Regression: the "non-tuple becomes a 1-tuple" fix for the bare-scalar case above must not also
    # wrap a LIST of positional args (a plausible model output) as a single one-element tuple -- that
    # would call reference([a, b]) instead of reference(a, b) and fail every differential on arity.
    ORACLE_LIST = "===ORACLE===\nimport random\ndef reference(a, b):\n    return a + b\ndef gen(seed, mode):\n    r = random.Random(seed)\n    return [r.randint(0, 20), r.randint(0, 20)]\n===END===\n"
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_LIST], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    # gate_inputs.small/medium come straight from make_cases()'s output for the list-returning gen: if
    # a list got wrapped as a 1-tuple instead of splatted, reference(a, b) sees an arity error on every
    # call and every one of these cases is dropped, leaving small/medium at 0.
    assert rep["gate_inputs"]["small"] == 5 and rep["gate_inputs"]["medium"] == 2
    assert rep["status"] in ("passed_all_gates", "emitted_unverified")
    assert all(e["passed"] for e in rep["evidence"]["c1"] if e["kind"] in ("diff_small", "diff_medium"))

def test_run_gate_crash_still_emits_solution_and_report(tmp_path, monkeypatch):
    # C1(a): any exception inside the gate/repair loop must not lose the run -- it must be recorded
    # as evidence and the finalizer must still run.
    import verify
    def boom(*a, **k): raise RuntimeError("boom")
    monkeypatch.setattr(verify, "run_gate", boom)
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert (tmp_path / "s.py").exists()
    assert (tmp_path / "run" / "report.json").exists()
    assert any(e["kind"] == "gate_error" for e in rep["evidence"]["c1"])
    assert any("gate.crashed RuntimeError" in line for line in rep["events"])

def test_write_solution_creates_missing_directories(tmp_path):
    # C2: write_solution must create the output directory if it doesn't exist, and the report must
    # not be lost even if that write happens after it.
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    out = tmp_path / "deep" / "nested" / "dir" / "solution.py"
    run_dir = tmp_path / "run"
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(out), run_dir=str(run_dir))
    assert out.exists()
    assert (run_dir / "report.json").exists()

def test_solve_falls_back_to_raw_reply_when_no_code_block_parsed(tmp_path):
    # A SOLVE reply with no ===CODE=== block at all must still produce a candidate (and a file) instead
    # of leaving the run with zero candidates.
    NO_CODE = "===RULES===\nr\n===END===\n===TRAPS===\nt\n===END===\n===ALGORITHM===\na\n===END===\n"
    llm = FakeLLM({"solve": [NO_CODE], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["final_candidate"] == "c1"
    assert (tmp_path / "s.py").exists()

def test_main_missing_api_key_returns_2_and_names_the_var(tmp_path, monkeypatch, capsys):
    for var in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):   # any one set would be auto-picked and hit the network
        monkeypatch.delenv(var, raising=False)
    p = tmp_path / "p.json"
    p.write_text(json.dumps({"problem_id": "pid", "language": "python", "statement": "s", "entrypoint": "add", "public_examples": [], "deadline_s": 300.0}))
    rc = S.main([str(p), "-o", str(tmp_path / "out.py")])
    assert rc == 2
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err

def test_bench_marks_skipped_gates_as_skip_not_pass(tmp_path):
    # C3: bench() used to build {kind: passed} and never read `skipped`, so a fully-skipped gate
    # (nothing to run against) read as "pass" in the benchmark table.
    sd = tmp_path / "samples"; sd.mkdir()
    (sd / "a.json").write_text(json.dumps({"problem_id": "aaaa", "language": "python", "statement": "s", "entrypoint": "add", "public_examples": [], "deadline_s": 300.0}))
    ORACLE_BROKEN = "===ORACLE===\ndef reference(a, b):\n    raise RuntimeError('broken')\ndef gen(seed, mode):\n    return (1, 2)\n===END===\n"
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_BROKEN], "stress": [""]})
    rows = S.bench(str(sd), str(tmp_path / "bench"), cfg(), llm, 1.0)
    row = rows[0]
    assert row["compiles"] == "pass"
    assert row["edge"] == "skip" and row["small"] == "skip" and row["medium"] == "skip"
    # The stress input now falls back to the oracle's gen(seed, "large"), which answers here even
    # though its reference() is dead -- but with no tier to compare its size against, nothing
    # confirms it is max-size, so the cell says "degraded". Either way, never "pass".
    assert row["behavior"] == "skip" and row["stress"] == "degraded"

def test_bench_shows_unknown_total_cost_when_no_cost_data(tmp_path):
    sd = tmp_path / "samples"; sd.mkdir()
    (sd / "a.json").write_text(json.dumps({"problem_id": "aaaa", "language": "python", "statement": "s", "entrypoint": "add", "public_examples": [], "deadline_s": 300.0}))
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    S.bench(str(sd), str(tmp_path / "bench"), cfg(), llm, 1.0)
    md = (tmp_path / "bench" / "benchmark.md").read_text()
    assert "total_cost_usd=unknown" in md   # FakeLLM never reports usage.cost

def test_report_records_candidate_parent(tmp_path):
    llm = FakeLLM({"solve": [BUGGY], "repair": [REPAIR_FIX], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["parents"]["c1"] is None and rep["parents"]["c2"] == "c1"

# --- benchfix: config-driven generate share, oracle retry, honest "nothing verified" log ---

def _sent_timeout(events, tag):
    line = next(l for l in events if l.split("] ", 1)[1].startswith(f"{tag}.sent"))
    return float(re.search(r"timeout=(\d+)", line).group(1))

def test_generate_call_share_is_read_from_config_not_hardcoded(tmp_path):
    # F6: solve() used to hardcode 0.45 * usable_s for the concurrent generate-phase cap. It must
    # come from config.toml's [phases].generate_call_share, so changing the config changes the cap.
    llm1 = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    c1 = cfg(); c1["phases"]["generate_call_share"] = 0.10
    rep1 = S.solve(prob(tmp_path), llm1, c1, out_path=str(tmp_path / "s1.py"), run_dir=str(tmp_path / "run1"))
    llm2 = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    c2 = cfg(); c2["phases"]["generate_call_share"] = 0.40
    rep2 = S.solve(prob(tmp_path), llm2, c2, out_path=str(tmp_path / "s2.py"), run_dir=str(tmp_path / "run2"))
    t1, t2 = _sent_timeout(rep1["events"], "oracle"), _sent_timeout(rep2["events"], "oracle")
    assert t2 > t1
    assert t1 == pytest.approx(0.10 * 285.0, abs=1.5)   # usable_s = 300 - 15 margin; well under the remaining-10 reserve, so the cap itself binds

def test_oracle_timeout_is_retried_once_and_retry_result_is_used(tmp_path):
    # F4.1: an empty first oracle reply (parses to no ===ORACLE=== block, as a timeout would produce)
    # must trigger exactly one retry, and the retry's content -- not the empty first reply -- must be
    # what actually drives gate_inputs.
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": ["", ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["gate_inputs"]["small"] == 5   # only possible if the retry's ORACLE_OK was used
    assert any("oracle.retry" in l for l in rep["events"])
    assert len([c for c in rep["calls"] if c["tag"] == "oracle"]) == 2

def test_oracle_retry_does_not_fire_when_budget_cannot_afford_it(tmp_path):
    class TightClock:
        t = 0.0
        def __call__(self):
            TightClock.t += 200.0   # burns past the 285s usable budget almost immediately
            return TightClock.t
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [""], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"), clock=TightClock())
    assert not any("oracle.retry" in l for l in rep["events"])
    assert len([c for c in rep["calls"] if c["tag"] == "oracle"]) == 1
    assert "no oracle source" in rep["gate_inputs"]["notes"]

class _TimedOutOracle(FakeLLM):
    """FakeLLM whose empty oracle replies carry error='timeout', as a capped call does."""
    def chat(self, role, system, user, *, timeout_s, max_tokens=None, tag=""):
        r = super().chat(role, system, user, timeout_s=timeout_s, max_tokens=max_tokens, tag=tag)
        if tag == "oracle" and not r.content.strip():
            return type(r)("", "", r.usage, r.latency_s, r.model, "timeout")
        return r

def _with_caps(llm, cap_s=150.0):
    llm.roles = {r: type("Role", (), {"timeout_cap_s": cap_s})() for r in ("strong", "fast")}
    return llm

def test_oracle_retry_names_timeout_as_its_reason(tmp_path):
    llm = _with_caps(_TimedOutOracle({"solve": [SOLVE_OK], "oracle": ["", ORACLE_OK], "stress": [STRESS_OK]}))
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert any("oracle.retry reason=timeout" in l for l in rep["events"])
    assert len([c for c in rep["calls"] if c["tag"] == "oracle"]) == 2

def test_a_timed_out_oracle_is_not_retried_without_room_for_the_cap_and_a_repair(tmp_path):
    # usable = 185 s: enough for the no_block rule (oracle_retry_afford_s = 130) but not for a second
    # call that will run to the fast role's 150 s cap and still leave a repair's worth of time.
    p = Problem("pid", "python", "Return a+b for ints a,b. At most 10^18.", "add", [], 200.0)
    timed_out = _with_caps(_TimedOutOracle({"solve": [SOLVE_OK], "oracle": [""], "stress": [STRESS_OK]}))
    rep = S.solve(p, timed_out, cfg(), out_path=str(tmp_path / "s1.py"), run_dir=str(tmp_path / "run1"))
    assert not any("oracle.retry" in l for l in rep["events"])
    assert len([c for c in rep["calls"] if c["tag"] == "oracle"]) == 1
    # same budget, but the first call answered without an ===ORACLE=== block: that retry is cheap
    # enough to be worth making, and it must still fire.
    no_block = _with_caps(FakeLLM({"solve": [SOLVE_OK], "oracle": ["", ORACLE_OK], "stress": [STRESS_OK]}))
    rep2 = S.solve(p, no_block, cfg(), out_path=str(tmp_path / "s2.py"), run_dir=str(tmp_path / "run2"))
    assert any("oracle.retry reason=no_block" in l for l in rep2["events"])

def test_all_skipped_gate_does_not_log_gate_passed(tmp_path):
    # F4.4: a run that verified nothing (broken oracle, no stress source) must not log "gate.passed" --
    # that reads as success in the log even though every real check was skipped.
    ORACLE_BROKEN = "===ORACLE===\ndef reference(a, b):\n    raise RuntimeError('broken')\ndef gen(seed, mode):\n    return (1, 2)\n===END===\n"
    # SOLVE_NO_EXAMPLES: with hand-traced examples the candidate WOULD have been checked against
    # them, and "gate.passed" would then be accurate even with a dead oracle.
    llm = FakeLLM({"solve": [SOLVE_NO_EXAMPLES], "oracle": [ORACLE_BROKEN], "stress": [""]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["status"] == "emitted_unverified"
    assert not any(l.endswith("gate.passed") for l in rep["events"])
    assert any("gate.unverifiable" in l for l in rep["events"])

def test_gate_passed_still_logged_when_something_was_actually_checked(tmp_path):
    # Regression guard for the fix above: a real, fully-passing run must still log "gate.passed".
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert any(l.endswith("gate.passed") for l in rep["events"])

# --- repairfix: oracle-regen timeout, syntax/semantic repair budgets, typed repair prompts ---

def _sent_timeouts(events, tag):
    return [float(re.search(r"timeout=(\d+)", l).group(1)) for l in events if l.split("] ", 1)[1].startswith(f"{tag}.sent")]

def test_oracle_regen_timeout_derived_from_generate_call_share(tmp_path):
    # regenerate_oracle used to hardcode 0.2 * usable_s (57s against a 285s usable budget) regardless
    # of config -- well below the oracle's measured worst-case latency (128s), so a correct "blame the
    # oracle" verdict could still lose its regeneration to a timeout. It must scale with
    # [phases].generate_call_share exactly like the initial ORACLE call does.
    llm1 = FakeLLM({"solve": [SOLVE_OK], "repair": [REPAIR_BLAME_ORACLE], "oracle": [ORACLE_WRONG, ORACLE_OK], "stress": [STRESS_OK]})
    c1 = cfg(); c1["phases"]["generate_call_share"] = 0.10
    rep1 = S.solve(prob(tmp_path), llm1, c1, out_path=str(tmp_path / "s1.py"), run_dir=str(tmp_path / "run1"))
    llm2 = FakeLLM({"solve": [SOLVE_OK], "repair": [REPAIR_BLAME_ORACLE], "oracle": [ORACLE_WRONG, ORACLE_OK], "stress": [STRESS_OK]})
    c2 = cfg(); c2["phases"]["generate_call_share"] = 0.50
    rep2 = S.solve(prob(tmp_path), llm2, c2, out_path=str(tmp_path / "s2.py"), run_dir=str(tmp_path / "run2"))
    usable = 285.0
    t1 = _sent_timeouts(rep1["events"], "oracle")[1]   # index 1: the regeneration call, not the initial one
    t2 = _sent_timeouts(rep2["events"], "oracle")[1]
    assert t1 == pytest.approx(0.10 * usable, abs=1.5)
    assert t2 == pytest.approx(0.50 * usable, abs=1.5)
    assert t2 > t1
    assert "repairs" in rep1 and "syntax_repairs" in rep1   # both repair counters must appear in the report

def test_repair_call_share_is_read_from_config_not_hardcoded(tmp_path):
    # verify.repair() used to hardcode 0.25 * usable_s for its own call cap, the same class of bug
    # F6 found in the generate phase: raising a config knob would do nothing because a literal
    # fraction still won the min() in Run.chat/step_timeout. It must come from
    # config.toml's [phases].repair_call_share.
    still_buggy = "===VERDICT===\ncandidate\n===END===\n===CODE===\ndef add(a, b):\n    return a + b if a < 15 else a + b + 2\n===END===\n"
    llm1 = FakeLLM({"solve": [BUGGY], "repair": [still_buggy], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    c1 = cfg(); c1["phases"]["repair_call_share"] = 0.05
    c1["limits"]["max_repairs"] = 1
    rep1 = S.solve(prob(tmp_path), llm1, c1, out_path=str(tmp_path / "s1.py"), run_dir=str(tmp_path / "run1"))
    llm2 = FakeLLM({"solve": [BUGGY], "repair": [still_buggy], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    c2 = cfg(); c2["phases"]["repair_call_share"] = 0.40
    c2["limits"]["max_repairs"] = 1
    rep2 = S.solve(prob(tmp_path), llm2, c2, out_path=str(tmp_path / "s2.py"), run_dir=str(tmp_path / "run2"))
    t1, t2 = _sent_timeout(rep1["events"], "repair"), _sent_timeout(rep2["events"], "repair")
    assert t2 > t1
    assert t1 == pytest.approx(0.05 * 285.0, abs=1.5)

def test_oracle_retry_afford_threshold_is_read_from_config(tmp_path):
    # limits.oracle_retry_afford_s used to be a bare 130.0 literal tuned to one specific model's
    # measured worst-case latency. Raising it past the usable budget must suppress the retry that
    # otherwise fires by default (see test_oracle_timeout_is_retried_once_and_retry_result_is_used).
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [""], "stress": [STRESS_OK]})
    c = cfg(); c["limits"]["oracle_retry_afford_s"] = 10_000.0
    rep = S.solve(prob(tmp_path), llm, c, out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert not any("oracle.retry" in l for l in rep["events"])
    assert len([call for call in rep["calls"] if call["tag"] == "oracle"]) == 1

def test_a_repair_that_cannot_fit_its_own_call_cap_is_never_started(tmp_path):
    # bench16: the repair cap was 25 % of usable (67 s) against a strong role whose completed call
    # that run took 168 s, so the call could only time out -- and the 67 s were spent anyway. A
    # repair now needs its own cap (verify.repair_cap) plus a gate pass to still fit the budget.
    llm = FakeLLM({"solve": [BUGGY], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    c = cfg(); c["phases"]["repair_call_share"] = 10.0   # a call this size can never fit the budget
    rep = S.solve(prob(tmp_path), llm, c, out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["repairs"] == 0 and rep["syntax_repairs"] == 0
    assert not any(call["tag"] == "repair" for call in rep["calls"])
    assert any("repair.skipped reason=budget" in e for e in rep["events"])
    assert rep["status"] == "emitted_with_failures"

def test_static_failure_consumes_syntax_budget_not_semantic(tmp_path):
    # A wrong entrypoint name is a mechanical "static" failure. It must spend the small, separate
    # max_syntax_repairs budget and never touch the semantic max_repairs counter.
    bad = SOLVE_OK.replace("def add(a, b):", "def wrong(a, b):")
    c = cfg(); c["limits"]["max_syntax_repairs"] = 1
    repair1 = "===VERDICT===\ncandidate\n===END===\n===CODE===\ndef wrong1(a, b):\n    return a + b\n===END===\n"
    llm = FakeLLM({"solve": [bad], "repair": [repair1], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, c, out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["syntax_repairs"] == 1   # stopped by max_syntax_repairs=1, not max_repairs=2
    assert rep["repairs"] == 0

def test_behavioral_failure_consumes_semantic_budget_not_syntax(tmp_path):
    # A genuine algorithmic bug (diff_edge) must spend max_repairs, and never the syntax budget.
    still_buggy = "===VERDICT===\ncandidate\n===END===\n===CODE===\ndef add(a, b):\n    return a + b if a < 15 else a + b + 2\n===END===\n"
    llm = FakeLLM({"solve": [BUGGY], "repair": [still_buggy, still_buggy, still_buggy], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["repairs"] == 2
    assert rep["syntax_repairs"] == 0

def test_repair_prompt_distinguishes_tuple_from_list_via_repr_and_type(tmp_path):
    # A candidate returning a tuple where the oracle returns a list renders identically once eyeballed
    # ("both look like []" at the empty case); the repair prompt must make the type explicit so the
    # model can actually see the difference instead of rewriting unrelated code.
    ORACLE_LIST = "===ORACLE===\nimport random\ndef reference(a, b):\n    return [a, b]\ndef gen(seed, mode):\n    r = random.Random(seed)\n    return (r.randint(0, 20), r.randint(0, 20))\n===END===\n"
    SOLVE_TUPLE = SOLVE_NO_EXAMPLES.replace("return a + b", "return (a, b)")
    fix = "===VERDICT===\ncandidate\n===END===\n===CODE===\ndef add(a, b):\n    return [a, b]\n===END===\n"
    llm = FakeLLM({"solve": [SOLVE_TUPLE], "repair": [fix], "oracle": [ORACLE_LIST], "stress": [STRESS_OK]})
    S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    repair_prompt = next(u for r, s, u in llm.prompts if "Failure kind" in u)
    assert "(type: list)" in repair_prompt and "(type: tuple)" in repair_prompt

def test_second_repair_prompt_notes_previous_change_did_not_fix_the_case(tmp_path):
    still_buggy = "===VERDICT===\ncandidate\n===END===\n===CODE===\ndef add(a, b):\n    return a + b if a < 15 else a + b + 2\n===END===\n"
    llm = FakeLLM({"solve": [BUGGY], "repair": [still_buggy, still_buggy], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    repair_prompts = [u for r, s, u in llm.prompts if "Failure kind" in u]
    assert len(repair_prompts) == 2
    assert "did not fix" not in repair_prompts[0].lower()   # first attempt: no history to report yet
    assert "did not fix" in repair_prompts[1].lower()       # second attempt: must say the prior change failed

# --- oracle self-repair: the oracle CALL succeeds but its own reference()/gen() crashes at runtime,
# leaving zero usable cases in every tier; nothing failed, so nothing would normally react ---

ORACLE_ALWAYS_RAISES = "===ORACLE===\ndef reference(a, b):\n    raise NameError('boom')\ndef gen(seed, mode):\n    return (1, 2)\n===END===\n"
ORACLE_WRONG_DETERMINISTIC = "===ORACLE===\ndef reference(a, b):\n    return a + b if a < 15 else a + b + 1\ndef gen(seed, mode):\n    return (15, 0)\n===END===\n"

def test_oracle_selfrepair_triggers_once_and_recovers_with_good_oracle(tmp_path):
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_ALWAYS_RAISES, ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["oracle_selfrepaired"] == 1
    assert rep["oracle_regenerated"] == 0   # this is not the adjudication path
    assert rep["gate_inputs"]["small"] == 5 and rep["gate_inputs"]["medium"] == 2   # recovered via the good second oracle
    assert any("oracle.selfrepair" in l for l in rep["events"])
    assert len([c for c in rep["calls"] if c["tag"] == "oracle"]) == 2
    # the recovered oracle never regenerated edge cases (the original edges never survived the broken
    # reference() in the first place), so diff_edge stays skipped -- that alone keeps status off
    # passed_all_gates, but every case that WAS checked (small/medium) must have genuinely passed.
    assert rep["status"] in ("passed_all_gates", "emitted_unverified")
    evs = {e["kind"]: e for e in rep["evidence"][rep["final_candidate"]]}
    assert evs["diff_small"]["passed"] and not evs["diff_small"]["skipped"]
    assert evs["diff_medium"]["passed"] and not evs["diff_medium"]["skipped"]

# Round 8: the oracle whose own validate() rejects most of its own gen() output. gen(seed) is (seed, 1)
# and validate accepts only seed 0, so 4 of 5 small and 1 of 2 medium inputs are thrown away -- far
# above validate_reject_frac_regen, and neither tier is "distrusted" (each keeps one input).
ORACLE_REJECTS_ITS_OWN_GEN = ("===ORACLE===\ndef reference(a, b):\n    return a + b\n"
                              "def gen(seed, mode):\n    return (seed, 1)\n"
                              "def validate(a, b):\n    return a == 0\n===END===\n")

def test_oracle_is_regenerated_when_validate_rejects_most_of_its_own_gen(tmp_path):
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_REJECTS_ITS_OWN_GEN, ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["oracle_selfrepaired"] == 1 and rep["oracle_regenerated"] == 0
    assert len([c for c in rep["calls"] if c["tag"] == "oracle"]) == 2   # exactly one extra call
    oracle_prompts = [u for r, s_, u in llm.prompts if "===ORACLE===" in u]
    assert "rejected" in oracle_prompts[1] and "(1, 1)" in oracle_prompts[1]   # counts and a rejected input
    # the gate ran on the second oracle's cases (5 small / 2 medium), not the first's single survivor
    assert rep["gate_inputs"]["small"] == 5 and rep["gate_inputs"]["medium"] == 2


def test_oracle_selfrepair_and_adjudication_regen_counters_are_independent(tmp_path):
    # A broken oracle triggers self-repair first (recovering a usable-but-WRONG oracle), and that
    # wrong oracle then disagrees with the (correct) candidate -- a genuine repair-loop disagreement
    # that gets adjudicated and blamed on the oracle. Both recovery paths fire, once each, in one run:
    # oracle_selfrepaired and oracle_regenerated must both read 1, proving the two counters don't share
    # a single one-shot budget.
    llm = FakeLLM({"solve": [SOLVE_OK], "repair": [REPAIR_BLAME_ORACLE],
                   "oracle": [ORACLE_ALWAYS_RAISES, ORACLE_WRONG_DETERMINISTIC, ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["oracle_selfrepaired"] == 1
    assert rep["oracle_regenerated"] == 1
    assert len([c for c in rep["calls"] if c["tag"] == "oracle"]) == 3
    assert rep["status"] in ("passed_all_gates", "emitted_unverified")
    evs = {e["kind"]: e for e in rep["evidence"][rep["final_candidate"]]}
    assert evs["diff_small"]["passed"] and not evs["diff_small"]["skipped"]


def cfg_n(n):
    c = cfg(); c["limits"]["solve_attempts"] = n; return c

SOLVE_BUGGY = SOLVE_OK.replace("return a + b", "return a + b if a < 15 else a + b + 1")
ORACLE_MED = "===ORACLE===\nimport random\ndef reference(a, b):\n    return a + b\ndef gen(seed, mode):\n    r = random.Random(seed)\n    hi = 20 if mode == 'small' else 10**5\n    return (r.randint(hi // 2, hi), r.randint(0, 20))\n===END===\n"


def test_best_of_n_prefers_a_passing_attempt_over_repairing_a_failing_one(tmp_path):
    # Attempt 1 carries the planted a>=15 bug that EDGES (15, 0) exposes; attempt 2 is correct.
    # The correct attempt must win without any repair call being spent.
    llm = FakeLLM({"solve": [SOLVE_BUGGY, SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg_n(2), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["repairs"] == 0, "a second attempt is cheaper than a repair and must be tried first"
    assert not any(c["tag"] == "repair" for c in rep["calls"])
    # candidates are numbered in the order their replies land, not in submission order (each is
    # gated as it arrives), so the correct attempt is identified by what was emitted, not by its id.
    assert rep["status"] == "passed_all_gates"
    assert (tmp_path / "s.py").read_text().strip() == "def add(a, b):\n    return a + b"


def test_best_of_n_issues_one_solve_call_per_attempt(tmp_path):
    llm = FakeLLM({"solve": [SOLVE_OK, SOLVE_BUGGY, SOLVE_BUGGY], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg_n(3), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert sum(1 for c in rep["calls"] if c["tag"] == "solve") == 3
    assert rep["status"] == "passed_all_gates"   # the good one wins wherever it lands


def test_identical_attempts_are_not_gated_twice(tmp_path):
    # Duplicate source buys nothing and a gate pass is the scarce resource, so it must be dropped.
    llm = FakeLLM({"solve": [SOLVE_OK, SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg_n(2), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert list(rep["evidence"]) == ["c1"]
    assert any("solve.duplicate" in e for e in rep["events"])


def test_single_attempt_remains_the_default_behaviour(tmp_path):
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert sum(1 for c in rep["calls"] if c["tag"] == "solve") == 1 and list(rep["evidence"]) == ["c1"]


def test_repair_targets_the_best_attempt_not_the_last_gated(tmp_path):
    # One attempt fails only the LAST differential tier; the other fails an earlier one, so the
    # first got further. Once both are gated the repair must be spent on that one, not on whichever
    # happened to be gated last. Which id it carries depends on which reply landed first.
    # the two bugs must not overlap on any tier: two candidates failing the SAME tier at the same
    # index with the same answer is a dispute about the oracle, not a repair (see below).
    ok_small_bad_medium = SOLVE_OK.replace("return a + b", "return a + b if a < 10**4 else a + b + 1")
    bad_small = SOLVE_OK.replace("return a + b", "return a + b + 1 if a < 15 else a + b")
    llm = FakeLLM({"solve": [ok_small_bad_medium, bad_small], "oracle": [ORACLE_MED], "stress": [STRESS_OK],
                   "repair": ["===VERDICT===\ncandidate\n===END===\n===CODE===\ndef add(a, b):\n    return a + b\n===END===\n"]})
    rep = S.solve(prob(tmp_path), llm, cfg_n(2), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    best = next(cid for cid, ev in rep["evidence"].items()
                if all(e["passed"] for e in ev if e["kind"] == "diff_small"))   # the one that got past diff_small
    assert rep["parents"].get("c3") == best, f"repair should build on the better attempt, got {rep['parents']}"
    assert any(f"repair.target {best}" in e for e in rep["events"])


# --- round 6: the max-size stress input must pass the oracle's validate() too ---

ORACLE_VALIDATES = ("===ORACLE===\nimport random\ndef reference(a, b):\n    return a + b\n"
                    "def gen(seed, mode):\n    r = random.Random(seed)\n    return (r.randint(0, 20), r.randint(0, 20))\n"
                    "def validate(a, b):\n    return a <= 100 and b <= 100\n===END===\n")

def test_gen_max_output_rejected_by_validate_is_not_stressed(tmp_path):
    # STRESS's gen_max invents an input the statement forbids: running it proves nothing and its
    # crash burns repair attempts. It must be dropped (degraded fallback), never gated as a FAIL.
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_VALIDATES], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert any("gen_max output rejected by validate(); not used" in n for n in rep["gate_inputs"]["notes"])
    stress = next(e for e in rep["evidence"][rep["final_candidate"]] if e["kind"] == "stress")
    assert stress["passed"] and (stress["skipped"] or stress["detail"].get("degraded"))


# --- round 6: a regenerated oracle is cross-checked against the one it replaces ---

ORACLE_OFF_BY_1000 = "===ORACLE===\nimport random\ndef reference(a, b):\n    return a + b + 1000\ndef gen(seed, mode):\n    r = random.Random(seed)\n    return (r.randint(0, 20), r.randint(0, 20))\n===END===\n"

def test_regenerated_oracle_disagreeing_everywhere_degrades_the_diff_evidence(tmp_path):
    # Two references written from the same prose that disagree on every tiny input cannot both be
    # near-correct: the run has no ground truth, so its differential evidence is not a full pass.
    still_buggy = "===VERDICT===\ncandidate\n===END===\n===CODE===\ndef add(a, b):\n    return a + b + 7\n===END===\n"
    llm = FakeLLM({"solve": [SOLVE_OK], "repair": [REPAIR_BLAME_ORACLE, still_buggy],
                   "oracle": [ORACLE_WRONG, ORACLE_OFF_BY_1000], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["oracle_regenerated"] == 1
    assert any("disagrees with the one it replaces" in n for n in rep["gate_inputs"]["notes"])
    assert any("oracle.regen disagreement=" in e for e in rep["events"])
    # diff_examples is excluded: it is the candidate against its author's own trace, with no oracle
    # in it at all, so an untrustworthy reference says nothing about that step.
    diffs = [e for evs in rep["evidence"].values() for e in evs if e["kind"].startswith("diff_") and e["kind"] != "diff_examples"]
    assert diffs and all(e["detail"].get("degraded") for e in diffs)
    assert rep["status"] != "passed_all_gates"


def test_second_oracle_verdict_with_no_regeneration_left_does_not_mint_a_candidate(tmp_path):
    # repair.md asks for the current code repeated unchanged behind an `oracle` verdict, so that CODE
    # block is not a fix. With the one regeneration already spent there is nothing to act on: the
    # loop must stop rather than promote it to a candidate (1c182498c9c7 emitted such a c3).
    llm = FakeLLM({"solve": [SOLVE_OK], "repair": [REPAIR_BLAME_ORACLE, REPAIR_BLAME_ORACLE],
                   "oracle": [ORACLE_WRONG, ORACLE_OFF_BY_1000], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["oracle_regenerated"] == 1
    assert list(rep["evidence"]) == ["c1"]   # no c2 minted from the second `oracle` verdict
    assert any("repair.oracle_verdict_unactionable" in e for e in rep["events"])

def test_repair_prompt_states_how_wholesale_the_disagreement_is(tmp_path):
    llm = FakeLLM({"solve": [BUGGY], "repair": [REPAIR_FIX], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    repair_prompt = next(u for r, s, u in llm.prompts if "Failure kind" in u)
    assert re.search(r"disagrees with the reference on \d+ of \d+ (small|edge) inputs", repair_prompt)
    assert "{{agreement}}" not in repair_prompt

def test_repair_prompt_shows_the_reference_it_is_asked_to_judge(tmp_path):
    # repair.md asks the model to trace what the reference would produce, so it has to see it.
    llm = FakeLLM({"solve": [BUGGY], "repair": [REPAIR_FIX], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    repair_prompt = next(u for r, s, u in llm.prompts if "Failure kind" in u)
    assert "def reference(a, b):" in repair_prompt and "Reference implementation" in repair_prompt
    assert "{{reference}}" not in repair_prompt


# --- round 6: one shared stdin rule for Rust across SOLVE, ORACLE and STRESS ---

def test_rust_stdin_token_rule_reaches_all_three_prompts(tmp_path):
    # SOLVE, ORACLE and STRESS each used to invent their own line layout for the same stdin, which
    # cost one run 7 of 10 edge cases and another every edge answer.
    pv = S.prompt_vars(prob(tmp_path, "rust"))
    assert "whitespace-separated token stream" in pv["io_rules"]
    assert "whitespace-separated token stream" in pv["language_rules"]   # solve.md/repair.md see it here
    for name in ("oracle", "stress"):
        assert "whitespace-separated token stream" in S.render(name, **pv)

def test_python_prompts_carry_no_stdin_rule_and_no_leftover_placeholder(tmp_path):
    pv = S.prompt_vars(prob(tmp_path))
    assert "token stream" not in pv["io_rules"] and "token stream" not in pv["language_rules"]
    for name in ("solve", "oracle", "stress"):
        assert "{{io_rules}}" not in S.render(name, **pv)


# --- round 6: small fixes ---

def test_stress_crash_repair_prompt_shows_the_head_of_the_max_size_input(tmp_path):
    # A stress crash is about the format and scale of an input the model has never seen; the repair
    # prompt showed neither. (11b) It also covers 11a: the gate.failed log line must use repr.
    CRASHES_ON_BIG = SOLVE_OK.replace("return a + b", "return a + b if a < 10**9 else 1 // 0")
    STRESS_BIG = "===STRESS===\ndef gen_max(seed):\n    return (10**18, 10**18)\nEDGES = [(0, 0)]\n===END===\n"
    llm = FakeLLM({"solve": [CRASHES_ON_BIG], "repair": [REPAIR_FIX], "oracle": [ORACLE_OK], "stress": [STRESS_BIG]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    repair_prompt = next(u for r, s, u in llm.prompts if "Failure kind: stress" in u)
    assert "1000000000000000000" in repair_prompt          # the stress input itself, not just its size
    failed_line = next(e for e in rep["events"] if "gate.failed" in e)
    assert "{'" in failed_line and '{"' not in failed_line   # repr(detail): json renders () and [] alike


def test_an_ungated_second_attempt_is_never_a_full_pass(tmp_path):
    # No oracle and no stress at all: the first attempt is gated with every step skipped and the
    # loop stops there, leaving the second attempt with only its static check. The finalizer's
    # tie-break then prefers the later candidate -- which has nothing skipped because nothing was
    # ever checked. That must read as unverified, not passed_all_gates (seen live on bench7).
    SOLVE_2 = SOLVE_OK.replace("return a + b", "return b + a")
    c = cfg(); c["limits"]["solve_attempts"] = 2
    llm = FakeLLM({"solve": [SOLVE_OK, SOLVE_2], "oracle": ["", ""], "stress": [""]})
    rep = S.solve(prob(tmp_path), llm, c, out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert len(rep["evidence"]) == 2
    assert rep["status"] == "emitted_unverified"


# --- round 8: prompt rules ---

def test_oracle_prompt_ties_the_reference_and_gen_to_one_precondition_list(tmp_path):
    # bench8: oracles whose validate() re-modelled the state more loosely than gen(), rejecting most
    # of what they generated. There is now one function, so gen() is tied to reference() instead.
    rendered = S.render("oracle", **S.prompt_vars(prob(tmp_path)))
    assert "`reference()` and `gen()` must agree" in rendered
    assert "raise on every entry of it in `reference()`" in rendered

def test_stress_prompt_forbids_work_at_import_time(tmp_path):
    # bench8/1ba0d34fae43: the STRESS module asserted its own EDGES at module level against rules the
    # statement never stated; the import raised and both EDGES and gen_max were lost with it.
    rendered = S.render("stress", **S.prompt_vars(prob(tmp_path)))
    assert "import time" in rendered and "No module-level loops, asserts, or calls" in rendered

def test_python_container_rule_reaches_solve_and_oracle(tmp_path):
    # bench8/2beff58fa923, three rounds running: the candidate returned () where the oracle returned []
    # on the empty input and a repair was spent on it. The statement names no outer container, so both
    # sides need the same convention, not each their own.
    pv = S.prompt_vars(prob(tmp_path))
    sentence = "use a `list` for the outer/returned sequence"
    assert sentence in pv["io_rules"] and sentence in pv["language_rules"]   # solve.md/repair.md see it here
    for name in ("solve", "oracle"):
        assert sentence in S.render(name, **pv)
    rust = S.prompt_vars(prob(tmp_path, "rust"))
    assert sentence not in rust["io_rules"] and "whitespace-separated token stream" in rust["io_rules"]


# --- round 13: the SOLVE author's hand-traced examples ---

SOLVE_CONTRADICTS_ITSELF = SOLVE_NO_EXAMPLES + "===EXAMPLES===\n((1, 2), 4)\n===END===\n"   # code returns 3
REPAIR_NO_CHANGE = "===VERDICT===\ncandidate\n===END===\n===CODE===\ndef add(a, b):\n    return a + b\n===END===\n"

def test_examples_are_parsed_counted_and_reported_per_candidate(tmp_path):
    malformed = SOLVE_NO_EXAMPLES + "===EXAMPLES===\n((0, 0), 0)\n((1, 2), 3)\nnot a pair\n===END===\n"
    llm = FakeLLM({"solve": [malformed], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["examples"]["c1"] == {"count": 2, "dropped": 1}
    assert any("solve.examples id=c1 parsed=2 dropped=1" in e for e in rep["events"])

def test_a_candidate_with_no_examples_skips_the_step(tmp_path):
    llm = FakeLLM({"solve": [SOLVE_NO_EXAMPLES], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    ev = {e["kind"]: e for e in rep["evidence"]["c1"]}
    assert ev["diff_examples"]["skipped"] and ev["diff_examples"]["passed"]
    assert rep["examples"]["c1"] == {"count": 0, "dropped": 0}
    # ...and a missing block never demotes the status: that would report on the reply's formatting
    # rather than on what was verified. Everything the oracle could check here did run.
    assert rep["status"] == "passed_all_gates"

def test_diff_examples_failure_reaches_repair_with_the_author_trace_wording(tmp_path):
    llm = FakeLLM({"solve": [SOLVE_CONTRADICTS_ITSELF], "repair": [REPAIR_NO_CHANGE], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    ev = {e["kind"]: e for e in rep["evidence"]["c1"]}
    assert not ev["diff_examples"]["passed"] and ev["diff_examples"]["detail"]["input"] == (1, 2)
    prompt = next(u for r, s_, u in llm.prompts if "Failure kind" in u)
    assert "Failure kind: diff_examples" in prompt
    assert "solution author's own hand trace of the statement" in prompt
    assert "independent literal reference" not in prompt.split("Actual:")[0]   # the Expected line, not the trailing reference section

def test_oracle_disagreeing_with_half_the_examples_is_regenerated_once(tmp_path):
    # ORACLE_OFF_BY_1000 contradicts all three hand-traced examples: one regeneration, and the
    # replacement (which agrees) is what the gate then runs against.
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OFF_BY_1000, ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["oracle_selfrepaired"] == 1 and rep["oracle_regenerated"] == 0
    assert len([c for c in rep["calls"] if c["tag"] == "oracle"]) == 2
    assert any("oracle.disputed reference disagrees with hand-traced examples on 3 of 3" in e for e in rep["events"])
    oracle_prompts = [u for c, (r, s_, u) in zip(llm.calls, llm.prompts) if c["tag"] == "oracle"]
    assert "Hand-traced expected" in oracle_prompts[1] and "def add(" not in oracle_prompts[1]
    diffs = [e for e in rep["evidence"]["c1"] if e["kind"].startswith("diff_")]
    assert not any(e["detail"].get("degraded") for e in diffs)

def test_a_replacement_oracle_that_still_disagrees_degrades_the_diff_evidence(tmp_path):
    llm = FakeLLM({"solve": [SOLVE_OK], "repair": [REPAIR_NO_CHANGE],
                   "oracle": [ORACLE_OFF_BY_1000, ORACLE_OFF_BY_1000], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["oracle_selfrepaired"] == 1
    assert any("oracle.disputed after regeneration" in e for e in rep["events"])
    diffs = [e for evs in rep["evidence"].values() for e in evs if e["kind"].startswith("diff_") and not e["skipped"]]
    assert diffs and all("hand-traced examples" in (e["detail"].get("degraded") or "") for e in diffs if e["kind"] != "diff_examples")
    assert rep["status"] != "passed_all_gates"

def test_an_agreeing_oracle_triggers_no_regeneration(tmp_path):
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["oracle_selfrepaired"] == 0 and len([c for c in rep["calls"] if c["tag"] == "oracle"]) == 1
    assert rep["gate_inputs"]["example_checks"]["disagreements"] == []
    assert rep["status"] == "passed_all_gates"

def test_rust_examples_are_stdin_stdout_pairs(tmp_path):
    p = Problem("pid", "rust", "read n then n ints, print the sum", "main", [], 300.0)
    assert V_parse_rust_examples(p) == [("3\n1 2 3\n", "6")]

def V_parse_rust_examples(p):
    import verify as V
    block = '("3\\n1 2 3\\n", "6")\n(123, "6")\n'
    return [(c.input, c.expected) for c in V.parse_examples(p, block)]

def test_examples_alone_are_real_evidence_when_the_oracle_is_dead(tmp_path):
    # The generic replacement for the deleted sample-specific smoke check: a dead oracle no longer
    # means nothing was checked -- the candidate still faces its author's own hand trace.
    ORACLE_BROKEN = "===ORACLE===\ndef reference(a, b):\n    raise RuntimeError('broken')\ndef gen(seed, mode):\n    return (1, 2)\n===END===\n"
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_BROKEN], "stress": [""]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    ev = {e["kind"]: e for e in rep["evidence"]["c1"]}
    assert ev["diff_examples"]["passed"] and not ev["diff_examples"]["skipped"] and ev["diff_examples"]["cases"] == 3
    assert not any("gate.unverifiable" in l for l in rep["events"])


# --- round 13: the gate records every tier and always times the candidate ---

def test_a_failing_candidate_is_still_timed_and_repaired_from_the_first_failure(tmp_path):
    # The candidate contradicts its own hand trace AND the edge cases. Both are recorded, the later
    # tiers and the timing run anyway, and the repair call is given the FIRST failure in canonical
    # order (a correctness failure is repaired before a timing one).
    both_wrong = BUGGY.replace(EXAMPLES_BLOCK, "===EXAMPLES===\n((1, 2), 4)\n===END===\n")
    llm = FakeLLM({"solve": [both_wrong], "repair": [REPAIR_FIX], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    ev = {e["kind"]: e for e in rep["evidence"]["c1"]}
    assert not ev["diff_examples"]["passed"] and not ev["diff_edge"]["passed"]
    assert ev["diff_small"]["cases"] and ev["stress"]["cases"] == 1   # neither suppressed by the failures above
    prompt = next(u for r, s_, u in llm.prompts if "Failure kind" in u)
    assert "Failure kind: diff_examples" in prompt


def test_a_broken_gen_max_is_replaced_by_the_oracles_large_mode(tmp_path):
    # gen_max failed or was rejected in nine of ten round-10 runs; the oracle's own "large" mode is
    # a second, independent max-size input, so the timing check still happens for real.
    STRESS_NO_GENMAX = "===STRESS===\ndef gen_max(seed):\n    raise RuntimeError('boom')\nEDGES = [(0, 0)]\n===END===\n"
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_WITH_LARGE], "stress": [STRESS_NO_GENMAX]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    ev = {e["kind"]: e for e in rep["evidence"]["c1"]}
    assert rep["gate_inputs"]["stress_source"] == "gen_large" and not rep["gate_inputs"]["stress_degraded"]
    assert ev["stress"]["passed"] and not ev["stress"]["skipped"] and "degraded" not in ev["stress"]["detail"]
    assert rep["status"] == "passed_all_gates"


# --- round 13: validate() is "reference() did not raise ValueError" ---

ORACLE_CRASHES_ON_MOST = ("===ORACLE===\ndef reference(a, b):\n    if a: raise KeyError('missing')\n    return a + b\n"
                          "def gen(seed, mode):\n    return (seed, 1)\n===END===\n")

def test_a_crashing_reference_triggers_the_oracle_regeneration(tmp_path):
    # Before this, a reference that raised on most of its own gen() inputs was recorded as "errors"
    # and nothing reacted: the reject fraction only counted validate()'s verdicts.
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_CRASHES_ON_MOST, ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["oracle_selfrepaired"] == 1
    assert len([c for c in rep["calls"] if c["tag"] == "oracle"]) == 2
    assert rep["gate_inputs"]["small"] == 5   # recovered on the replacement oracle

def test_oracle_prompt_defines_validate_as_the_reference_not_raising(tmp_path):
    rendered = S.render("oracle", **S.prompt_vars(prob(tmp_path)))
    assert "raise ValueError" in rendered
    assert "def validate(*args):" in rendered and "except ValueError:" in rendered
    assert "is a bug in your reference, not an invalid input" in rendered

def test_generator_prompts_require_reaching_the_bound(tmp_path):
    # G5: randomly built sequences go invalid before the deep state is reached, so a candidate wrong
    # about a whole clause shows a 1% mismatch. Both generators must be told to reach the bound.
    pv = S.prompt_vars(prob(tmp_path))
    oracle, stress = S.render("oracle", **pv), S.render("stress", **pv)
    assert "to its bound at least once" in oracle and "to its bound at least once" in stress
    assert "at the maximum that mode allows" in oracle

def test_solve_prompt_restates_the_rules_before_the_code(tmp_path):
    # RULES is what catches a misreading, and it used to be written after the code -- so the model
    # coded before it had read carefully. FakeLLM scripts parse by marker, so order is free to change.
    rendered = S.render("solve", **S.prompt_vars(prob(tmp_path)))
    order = [m.group(1) for m in re.finditer(r"^===([A-Z]+)===$", rendered, re.M) if m.group(1) != "END"]
    assert order == ["RULES", "DESIGN", "CODE", "EXAMPLES", "TRAPS", "ALGORITHM"]
    assert "at most about 25 lines" in rendered


# --- round 13: the fresh-solve path ---

def sumprob(tmp_path):
    return Problem("pid", "python", "Given a list xs of ints, return the sum of all its prefix sums.", "f", [], 300.0)

SOLVE_QUADRATIC = ("===CODE===\ndef f(xs):\n    t = 0\n    for i in range(len(xs)):\n        t += sum(xs[:i + 1])\n    return t\n===END===\n"
                   "===ALGORITHM===\nRe-sum the whole prefix at every index: quadratic in the length.\n===END===\n")
SOLVE_LINEAR = ("===CODE===\ndef f(xs):\n    t = 0\n    run = 0\n    for x in xs:\n        run += x\n        t += run\n    return t\n===END===\n"
                "===ALGORITHM===\nOne running prefix sum, linear.\n===END===\n")
ORACLE_SUM = ("===ORACLE===\nimport random\ndef reference(xs):\n    return sum(sum(xs[:i + 1]) for i in range(len(xs)))\n"
              "def gen(seed, mode):\n    r = random.Random(seed)\n    return ([r.randint(0, 20) for _ in range(r.randint(0, 6))],)\n===END===\n")
STRESS_BIG_LIST = "===STRESS===\ndef gen_max(seed):\n    return (list(range(20000)),)\nEDGES = [([],), ([5],)]\n===END===\n"

def slow_cfg():
    c = cfg(); c["limits"]["stress_limit_python_s"] = 0.2; c["limits"]["max_fresh_solves"] = 1
    return c

def _prompts(llm, tag):
    return [u for c, (r, s_, u) in zip(llm.calls, llm.prompts) if c["tag"] == tag]

def test_a_timing_failure_starts_a_fresh_solve_that_can_win_emission(tmp_path):
    # round-13 G4: a patch cannot change the complexity of an approach, so a candidate that is only
    # too slow gets a second solution written from a different algorithm, carrying the failure.
    llm = FakeLLM({"solve": [SOLVE_QUADRATIC], "solve_fresh": [SOLVE_LINEAR], "oracle": [ORACLE_SUM], "stress": [STRESS_BIG_LIST]})
    rep = S.solve(sumprob(tmp_path), llm, slow_cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    ev = {e["kind"]: e for e in rep["evidence"]["c1"]}
    assert not ev["stress"]["passed"] and ev["stress"]["detail"]["too_slow"]
    assert rep["fresh_solves"] == 1 and rep["repairs"] == 0   # never sent to the patch-style repair
    p2 = _prompts(llm, "solve_fresh")[0]   # a fresh solve is its own tag: it is a post-gate call
    assert "A previous solution to this statement failed" in p2
    assert "Re-sum the whole prefix at every index" in p2       # the previous ALGORITHM block
    assert "argument 1: type list; length 20000" in p2           # the input's shape...
    assert "19997, 19998, 19999" not in p2                         # ...never the input itself
    assert "measured duration" in p2 and "vs limit 0.2 s" in p2
    # the fresh candidate is a root that replaces c1, and wins emission on its own evidence
    assert rep["parents"]["c2"] is None and rep["replaces"]["c2"] == "c1"
    assert rep["final_candidate"] == "c2" and rep["status"] == "passed_all_gates"
    assert (tmp_path / "s.py").read_text().startswith("def f(xs):\n    t = 0\n    run = 0")

def test_no_fresh_solve_left_falls_through_without_a_patch(tmp_path):
    c = slow_cfg(); c["limits"]["max_fresh_solves"] = 0
    llm = FakeLLM({"solve": [SOLVE_QUADRATIC], "solve_fresh": [SOLVE_LINEAR], "oracle": [ORACLE_SUM], "stress": [STRESS_BIG_LIST]})
    rep = S.solve(sumprob(tmp_path), llm, c, out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["fresh_solves"] == 0 and rep["repairs"] == 0 and not _prompts(llm, "solve_fresh")
    assert rep["final_candidate"] == "c1" and rep["status"] == "emitted_with_failures"

def test_no_budget_means_no_fresh_solve(tmp_path):
    # A fresh solve needs its own call cap (the generate share) plus a gate pass to still fit.
    c = slow_cfg(); c["phases"]["generate_call_share"] = 10.0   # a call this size can never fit the budget
    llm = FakeLLM({"solve": [SOLVE_QUADRATIC], "solve_fresh": [SOLVE_LINEAR], "oracle": [ORACLE_SUM], "stress": [STRESS_BIG_LIST]})
    rep = S.solve(sumprob(tmp_path), llm, c, out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["fresh_solves"] == 0 and not _prompts(llm, "solve_fresh")
    assert any("solve.fresh skipped reason=budget" in e for e in rep["events"])
    assert any("solve.fresh unavailable" in e for e in rep["events"])

REPAIR_APPROACH = "===VERDICT===\napproach\n===END===\n===CODE===\ndef add(a, b):\n    return a + b if a < 15 else a + b + 1\n===END===\n"

def test_an_approach_verdict_mints_no_child_and_starts_a_fresh_solve(tmp_path):
    c = cfg(); c["limits"]["max_fresh_solves"] = 1
    llm = FakeLLM({"solve": [BUGGY], "solve_fresh": [SOLVE_OK], "repair": [REPAIR_APPROACH], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, c, out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert any("repair.verdict=approach" in e for e in rep["events"])
    assert rep["fresh_solves"] == 1 and rep["parents"] == {"c1": None, "c2": None}   # no repaired child
    assert rep["replaces"]["c2"] == "c1" and rep["final_candidate"] == "c2"

REPAIR_WORSE = "===VERDICT===\ncandidate\n===END===\n===CODE===\ndef add(a, b):\n    return 0\n===END===\n"

def test_a_repair_that_regresses_stops_the_lineage_and_re_solves(tmp_path):
    # round-10 L1: a child that fails more gate steps than its parent is a losing bet to patch again.
    c = cfg(); c["limits"]["max_fresh_solves"] = 1
    llm = FakeLLM({"solve": [BUGGY], "solve_fresh": [SOLVE_OK], "repair": [REPAIR_WORSE], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, c, out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert any("repair.regressed child=c2 parent=c1" in e for e in rep["events"])
    assert rep["repairs"] == 1 and rep["fresh_solves"] == 1   # the second repair attempt is never spent
    assert rep["replaces"]["c3"] == "c1" and rep["final_candidate"] == "c3"

def test_the_solve_prompt_has_no_previous_attempt_section_by_default(tmp_path):
    rendered = S.render("solve", **S.prompt_vars(prob(tmp_path)))
    assert "{{previous_attempt}}" not in rendered and "A previous solution" not in rendered

def test_repair_prompt_offers_the_approach_verdict(tmp_path):
    rendered = S.render("repair", **{**S.prompt_vars(prob(tmp_path)), "kind": "stress", "input": "i", "expected": "e",
                                     "actual": "a", "details": "d", "code": "c", "agreement": "x", "reference": "r",
                                     "expected_source": "s"})
    assert "the verdict is `approach`" in rendered and "if no patch can meet the stated limits" in rendered


def test_stress_prompt_demands_huge_bounded_quantities_and_an_input_valid_to_the_end(tmp_path):
    # bench14: gen_max capped repetitions at 10 000 "to avoid blowup" on a statement bounding them
    # by 10^18, and its first operation was invalid, so the candidate discarded the whole input.
    rendered = S.render("stress", **S.prompt_vars(prob(tmp_path)))
    assert "is a NUMBER, not work" in rendered and "at its stated maximum at least once" in rendered
    assert "the blowup is exactly what the check exists to find" in rendered
    assert "must stay valid, in the statement's own sense, all the way to its end" in rendered
    assert "Put any deliberately invalid operation last" in rendered


# --- round 14: oracle preparation overlaps the solver ---

class _LatchLLM(FakeLLM):
    """FakeLLM whose reply for one tag is held until an Event is set, so a test can control which
    of the three concurrent generation calls lands first."""
    def __init__(self, script, gates):
        super().__init__(script)
        self.gates = gates
    def chat(self, role, system, user, *, timeout_s, max_tokens=None, tag=""):
        ev = self.gates.get(tag)
        if ev is not None:
            ev.wait(timeout=20)
        return super().chat(role, system, user, timeout_s=timeout_s, max_tokens=max_tokens, tag=tag)

def _spy_on(monkeypatch, name, before=None, after=None):
    real = getattr(S.V, name)
    def wrapper(*a, **k):
        if before: before()
        out = real(*a, **k)
        if after: after()
        return out
    monkeypatch.setattr(S.V, name, wrapper)

def test_oracle_prep_starts_before_the_solve_reply_arrives(tmp_path, monkeypatch):
    # bench14: solves 17 s, oracle 30 s, stress 51 s, prep 62 s. Prep is subprocess-bound and needs
    # nothing from the solver, so it must not wait for it -- with a slow strong model that ordering
    # wastes the entire prep.
    import threading
    solve_gate = threading.Event()
    _spy_on(monkeypatch, "prepare_oracle_tiers", after=solve_gate.set)   # the SOLVE reply is released only once the tiers are built
    llm = _LatchLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]}, {"solve": solve_gate})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert any("prep.overlap solve_pending=1" in l for l in rep["events"])
    small = next(i for i, l in enumerate(rep["events"]) if "oracle.small cases=" in l)
    done = next(i for i, l in enumerate(rep["events"]) if "solve.done" in l)
    assert small < done   # the tier was built while the solver was still running
    # and the candidate is still gated against fully prepared inputs
    assert rep["gate_inputs"]["small"] > 0 and rep["gate_inputs"]["edge"] > 0 and rep["gate_inputs"]["stress_source"] == "gen_max"
    ev = {e["kind"]: e for e in rep["evidence"]["c1"]}
    assert ev["diff_small"].get("cases") and ev["diff_edge"].get("cases") and ev["diff_examples"].get("cases")
    assert rep["status"] == "passed_all_gates"

def test_a_slow_stress_reply_does_not_delay_the_small_and_medium_tiers(tmp_path, monkeypatch):
    import threading
    stress_gate = threading.Event()
    _spy_on(monkeypatch, "prepare_oracle_tiers", after=stress_gate.set)   # STRESS lands only after the oracle tiers are done
    llm = _LatchLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]}, {"stress": stress_gate})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    small = next(i for i, l in enumerate(rep["events"]) if "oracle.small cases=" in l)
    stress_done = next(i for i, l in enumerate(rep["events"]) if "stress.done" in l)
    assert small < stress_done
    # the stress-dependent parts are still filled in afterwards, on the same GateInputs
    assert rep["gate_inputs"]["edge"] > 0 and rep["gate_inputs"]["stress"] and rep["gate_inputs"]["stress_source"] == "gen_max"
    assert rep["status"] == "passed_all_gates"

def test_the_example_check_runs_against_the_prepared_oracle_after_the_solves(tmp_path):
    # the examples only exist once the SOLVE replies are in, so the check is the last prep step --
    # it must still see the same oracle the tiers were built from.
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    ec = rep["gate_inputs"]["example_checks"]
    assert ec["cases"] == 3 and ec["agreements"] == 3 and ec["disagreements"] == [] and ec["rejected"] == []
    small = next(i for i, l in enumerate(rep["events"]) if "oracle.small cases=" in l)
    ex = next(i for i, l in enumerate(rep["events"]) if "oracle.examples cases=" in l)
    assert small < ex


# --- round 14: an unvalidated max-size input cannot fail a candidate ---

# The literal reference cannot finish on a max-size input, so validate() gives no verdict on it
# (bench15). Raising here is the same "no verdict" outcome as the timeout was, at no cost in test time.
ORACLE_SUM_NO_VERDICT = ORACLE_SUM.replace(
    "===END===\n", "def validate(xs):\n    if len(xs) > 100:\n        raise RuntimeError('cannot judge an input this big')\n    return True\n===END===\n")
STRESS_MID_LIST = "===STRESS===\ndef gen_max(seed):\n    return (list(range(5000)),)\nEDGES = [([],), ([5],)]\n===END===\n"

def test_a_slow_run_on_an_unjudged_max_size_input_is_not_a_failure(tmp_path):
    # bench15: gen_max returned a structure nested 200 000 deep against a stated cap of 60, the
    # reference could not judge it, and the timing gate failed a candidate that is fast on every
    # legal maximum. An input nothing could validate must never fail one, nor spend a fresh solve.
    c = slow_cfg(); c["limits"]["stress_limit_python_s"] = 0.05
    llm = FakeLLM({"solve": [SOLVE_QUADRATIC, SOLVE_LINEAR], "oracle": [ORACLE_SUM_NO_VERDICT], "stress": [STRESS_MID_LIST]})
    rep = S.solve(sumprob(tmp_path), llm, c, out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["gate_inputs"]["stress_validity"] == "unjudged" and rep["gate_inputs"]["stress_source"] == "gen_max"
    ev = {e["kind"]: e for e in rep["evidence"]["c1"]}
    assert ev["stress"]["detail"]["too_slow"]                       # measured honestly
    assert ev["stress"]["passed"] and ev["stress"]["skipped"]       # and still not a failure
    assert rep["fresh_solves"] == 0 and rep["repairs"] == 0 and len(_prompts(llm, "solve")) == 1
    assert rep["status"] == "emitted_unverified"                    # never passed_all_gates either
    assert any("validity=unjudged" in e for e in rep["events"])
    # an unjudged input falls through to a JUDGED one -- but this oracle's gen() ignores its mode,
    # so gen(large) is no bigger than the tiers: judged and degraded is not better evidence than an
    # honest measurement on a big input, so the source is marked tried and the input is kept.
    assert rep["gate_inputs"]["stress_tried"] == ["gen_max", "gen_large"]

def _cand_with_stress(cid, duration):
    c = S.Candidate(cid, f"# {cid}", None)
    c.evidence = [S.V.Evidence("static", True), S.V.Evidence("diff_small", True, 5, detail={"mismatches": 0}),
                  S.V.Evidence("stress", True, 1, detail={"duration_s": duration})]
    return c

def test_two_all_passing_candidates_are_ranked_by_measured_stress_time(tmp_path):
    # bench15 shipped the first of two identically-scored candidates by attempt order; the other is
    # 10x slower on legal maximum-size inputs.
    slow, fast = _cand_with_stress("c1", 4.2), _cand_with_stress("c2", 0.3)
    assert S.best_candidate([slow, fast]) is fast
    assert S.best_candidate([fast, slow]) is fast
    # a candidate whose timing is not a real measurement is not compared on it: the tie falls back
    # to candidate_score's own last tie-break, the older attempt.
    slow, degraded = _cand_with_stress("c1", 4.2), _cand_with_stress("c2", 0.3)
    degraded.evidence[-1].detail["degraded"] = "not a true max-size input"
    assert S.measured_stress_s(degraded) is None
    assert S.best_candidate([slow, degraded]) is slow


# --- round 14: each candidate is gated as its own reply arrives ---

class _SecondSolveLatch(FakeLLM):
    """FakeLLM that holds the SECOND solve reply until an Event is set, so the test can assert the
    first candidate was gated while the other attempt was still outstanding."""
    def __init__(self, script, gate):
        super().__init__(script)
        self.gate, self.n = gate, 0
    def chat(self, role, system, user, *, timeout_s, max_tokens=None, tag=""):
        if tag == "solve":
            with self._lock:
                self.n += 1
                mine = self.n
            if mine == 2:
                self.gate.wait(timeout=20)
        return super().chat(role, system, user, timeout_s=timeout_s, max_tokens=max_tokens, tag=tag)

def test_the_first_candidate_is_gated_while_the_other_attempt_is_still_running(tmp_path, monkeypatch):
    # bench15 idled 43 s and bench16 31 s waiting for the slowest solve attempt before gating any
    # candidate. With a thinking strong model that wait is the whole repair budget.
    import threading
    gate_done = threading.Event()
    _spy_on(monkeypatch, "run_gate", after=gate_done.set)   # the second reply is released only once a gate pass has run
    llm = _SecondSolveLatch({"solve": [SOLVE_OK, SOLVE_BUGGY], "oracle": [ORACLE_OK], "stress": [STRESS_OK]}, gate_done)
    rep = S.solve(prob(tmp_path), llm, cfg_n(2), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    early = [l for l in rep["events"] if "gate.early" in l]
    assert len(early) == 2 and "cand=c1 solve_pending=1" in early[0] and "cand=c2 solve_pending=0" in early[1]
    first_gate = rep["events"].index(early[0])
    second_reply = [i for i, l in enumerate(rep["events"]) if "solve.done" in l][1]
    assert first_gate < second_reply, "the first candidate must be gated before the second reply lands"
    # both attempts are still gated against the same fully prepared inputs
    assert len(rep["evidence"]) == 2
    assert all(any(e["kind"] == "diff_small" and e["cases"] for e in ev) for ev in rep["evidence"].values())
    assert rep["status"] == "passed_all_gates" and rep["repairs"] == 0

def test_the_example_check_accumulates_and_the_dispute_is_decided_once(tmp_path):
    # The oracle is wrong about a >= 15, which both attempts' hand traces contradict. The dispute
    # must fire once, on the first candidate that supplies the evidence -- not once per candidate.
    ex15 = "===EXAMPLES===\n((15, 0), 15)\n((16, 0), 16)\n((17, 0), 17)\n===END===\n"
    solve_a = SOLVE_NO_EXAMPLES + ex15
    solve_b = SOLVE_NO_EXAMPLES.replace("return a + b", "return b + a") + ex15
    llm = FakeLLM({"solve": [solve_a, solve_b], "oracle": [ORACLE_WRONG, ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg_n(2), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["oracle_selfrepaired"] == 1
    assert sum("oracle.selfrepair triggered" in e for e in rep["events"]) == 1
    assert len([c for c in rep["calls"] if c["tag"] == "oracle"]) == 2   # the one regeneration, not one per candidate
    assert sum("oracle.examples cases=" in e for e in rep["events"]) >= 2   # the check itself does accumulate


# --- round 14: a repair has to fit the model it is sent to ---

class _ErroringRepair(FakeLLM):
    """Every call scripted as usual, except the repair, which fails the way a 403 or a dropped
    connection does: no content, no blocks, an error on the Reply."""
    def chat(self, role, system, user, *, timeout_s, max_tokens=None, tag=""):
        r = super().chat(role, system, user, timeout_s=timeout_s, max_tokens=max_tokens, tag=tag)
        if tag == "repair":
            from llm import Reply
            return Reply("", "", dict(self.usage), 0.0, "fake", "http 403: key limit reached")
        return r

def test_an_errored_repair_call_mints_no_child_and_reaches_no_verdict(tmp_path):
    # bench16-1dea: the repair call 403'd and the empty reply fell through to the default verdict
    # `candidate` -- "the reference is right" -- on a run whose oracle was in fact the wrong side.
    llm = _ErroringRepair({"solve": [BUGGY], "repair": [""], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert any("repair.error http 403" in e for e in rep["events"])
    assert not any("repair.verdict" in e for e in rep["events"])
    assert list(rep["evidence"]) == ["c1"] and rep["status"] == "emitted_with_failures"
    assert (tmp_path / "s.py").exists()

def test_repair_cap_takes_the_larger_of_the_phase_share_and_the_role_floor(tmp_path):
    class _Role: repair_cap_s = 120.0
    class _LLM: roles = {"strong": _Role()}
    class _Run:
        llm = _LLM()
        cfg = {"phases": {"repair_call_share": 0.25}}
        budget = S.Budget(300.0, margin_s=15.0, phases={})
        role = S.Run.role
    assert S.V.repair_cap(_Run()) == 120.0            # 0.25 * 285 = 71 s, far below the role's own latency
    _Run.cfg = {"phases": {"repair_call_share": 0.6}}
    assert S.V.repair_cap(_Run()) == pytest.approx(171.0)
    _Role.repair_cap_s = 0.0                           # unset: the phase share is the only rule, as before
    assert S.V.repair_cap(_Run()) == pytest.approx(171.0)


# --- round 14: two candidates agreeing against the oracle is a dispute ---

SOLVE_OK_VARIANT = SOLVE_NO_EXAMPLES.replace("return a + b", "return b + a") + EXAMPLES_BLOCK

def test_two_candidates_agreeing_on_the_failing_case_dispute_the_oracle_before_any_repair(tmp_path):
    # bench16-1dea: both candidates produced the same output on the failing input, the oracle had
    # one wrong conjunct, and the repair was aimed at the candidates. Two independent authors
    # against one reference is a dispute, and the reference is the side this system can rewrite.
    llm = FakeLLM({"solve": [SOLVE_OK, SOLVE_OK_VARIANT], "oracle": [ORACLE_WRONG, ORACLE_OK], "stress": [STRESS_OK],
                   "repair": [REPAIR_FIX]})
    rep = S.solve(prob(tmp_path), llm, cfg_n(2), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert any("oracle.dispute two_candidates_agree kind=diff_edge" in e for e in rep["events"])
    assert rep["oracle_regenerated"] == 1
    assert not any(c["tag"] == "repair" for c in rep["calls"]), "the regeneration must come before any repair"
    assert rep["repairs"] == 0 and rep["status"] == "passed_all_gates"
    # and the agreement is recorded as a value disagreement with the oracle, like a hand trace
    agreed = [d for d in rep["gate_inputs"]["example_checks"]["disagreements"] if d.get("from_candidates")]
    assert len(agreed) <= 1   # the regenerated oracle's own check replaces the dict; at most one is carried

def test_a_single_candidate_failing_alone_still_goes_to_repair(tmp_path):
    # One author against the oracle is not a dispute: today's path, unchanged.
    llm = FakeLLM({"solve": [BUGGY], "oracle": [ORACLE_OK], "stress": [STRESS_OK], "repair": [REPAIR_FIX]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert not any("oracle.dispute" in e for e in rep["events"])
    assert rep["repairs"] == 1 and rep["oracle_regenerated"] == 0 and rep["status"] == "passed_all_gates"

def test_two_candidates_that_merely_both_crash_are_not_an_agreement(tmp_path):
    # "(no answer: crashed ...)" is not a value two authors agreed on.
    p = prob(tmp_path)
    crash = S.Candidate("c1", "x", None)
    crash.evidence = [S.V.Evidence("static", True),
                      S.V.Evidence("diff_edge", False, 2, detail={"index": 1, "actual": "(no answer: crashed before completing)", "expected": 3})]
    other = S.Candidate("c2", "y", None)
    other.evidence = list(crash.evidence)
    assert S.V.candidates_agree(p, [crash, other], crash, crash.evidence[1]) is None
    # a repaired child agreeing with its own parent is one author, not two
    parent = S.Candidate("c1", "x", None)
    parent.evidence = [S.V.Evidence("static", True), S.V.Evidence("diff_edge", False, 2, detail={"index": 1, "actual": 15, "expected": 16})]
    child = S.Candidate("c2", "y", "c1")
    child.evidence = list(parent.evidence)
    assert S.V.candidates_agree(p, [parent, child], child, child.evidence[1]) is None
    root2 = S.Candidate("c3", "z", None)
    root2.evidence = list(parent.evidence)
    got = S.V.candidates_agree(p, [parent, child, root2], child, child.evidence[1])
    assert got and got["authors"] == ["c2", "c3"] and got["index"] == 1


def test_the_examples_block_demands_in_contract_inputs_and_the_io_convention(tmp_path):
    # bench14 lost 3 of 10 hand-traced examples and bench15 2 of 8 to inputs the oracle's
    # precondition check rejected, or to a container spelled the other way round.
    py = S.render("solve", **S.prompt_vars(prob(tmp_path)))
    assert "must satisfy every precondition the statement states" in py
    assert "thrown away and its check is lost" in py
    assert "`()` is not" in py and "{{io_rules}}" not in py      # the python container rule, rendered
    rust = S.render("solve", **S.prompt_vars(prob(tmp_path, "rust")))
    assert "whitespace-separated token stream" in rust.split("===EXAMPLES===")[1]


# --- round 14 batch 6: a reply cut off at its timeout is salvaged when its CODE block closed ---

class _PartialReplies(FakeLLM):
    """Delivers the scripted text the way a streamed call abandoned at its own timeout does:
    partial=True and error='timeout', for the tags named in `partial_tags`."""
    def __init__(self, script, partial_tags=("solve",), cost=None):
        super().__init__(script, cost)
        self.partial_tags = set(partial_tags)

    def chat(self, role, system, user, *, timeout_s, max_tokens=None, tag=""):
        r = super().chat(role, system, user, timeout_s=timeout_s, max_tokens=max_tokens, tag=tag)
        if tag in self.partial_tags:
            self.calls[-1].update(error="timeout", timed_out=True, partial=True)
            from llm import Reply
            return Reply(r.content, "", r.usage, r.latency_s, r.model, "timeout", True)
        return r

# The solve prompt's order: what a cut-off reply keeps is RULES/DESIGN/CODE, what it loses is
# EXAMPLES/TRAPS/ALGORITHM -- so diff_examples is skipped and every oracle-derived step still runs.
SOLVE_CUT_IN_TRAPS = ("===RULES===\nr\n===END===\n===DESIGN===\nd\n===END===\n"
                      "===CODE===\ndef add(a, b):\n    return a + b\n===END===\n"
                      "===TRAPS===\nwatch out for over")
SOLVE_CUT_IN_CODE = "===RULES===\nr\n===END===\n===DESIGN===\nd\n===END===\n===CODE===\ndef add(a, b):\n    ret"

def test_a_timed_out_solve_with_a_complete_code_block_is_gated(tmp_path):
    llm = _PartialReplies({"solve": [SOLVE_CUT_IN_TRAPS], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    out = tmp_path / "s.py"
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(out), run_dir=str(tmp_path / "run"))
    assert rep["solver_status"] == "partial" and rep["final_candidate"] == "c1"
    assert out.read_text().startswith("def add")
    line = next(e for e in rep["events"] if "solve.partial cand=c1" in e)
    assert "'CODE'" in line.split("salvaged_blocks=")[1].split("missing=")[0]
    assert "'TRAPS'" in line.split("missing=")[1] and "'EXAMPLES'" in line.split("missing=")[1]
    # the run continued normally: the oracle-derived steps really ran, only the hand trace is missing
    ev = {e["kind"]: e for e in rep["evidence"]["c1"]}
    assert ev["diff_small"]["detail"]["cases"] > 0 and ev["diff_examples"]["skipped"]
    assert any("emit candidate=c1" in e and "solver=partial" in e for e in rep["events"])

def test_a_timed_out_solve_cut_off_inside_the_code_block_yields_no_candidate(tmp_path):
    llm = _PartialReplies({"solve": [SOLVE_CUT_IN_CODE], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    out = tmp_path / "s.py"
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(out), run_dir=str(tmp_path / "run"))
    assert rep["status"] == "no_candidate" and rep["solver_status"] == "timeout" and not out.exists()
    assert any("cut off before ===CODE=== closed" in e for e in rep["events"])
    # the raw-reply fallback must not fire for a truncated stream
    assert not any("solve.no_code_block" in e for e in rep["events"])

def test_a_timed_out_repair_with_a_complete_code_block_is_used(tmp_path):
    fix = "===VERDICT===\ncandidate\n===END===\n===CODE===\ndef add(a, b):\n    return a + b\n===END===\n===NOTES===\nand th"
    llm = _PartialReplies({"solve": [BUGGY], "repair": [fix], "oracle": [ORACLE_OK], "stress": [STRESS_OK]},
                          partial_tags=("repair",))
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert any("repair.partial" in e for e in rep["events"]) and "c2" in rep["evidence"]
    assert rep["repairs"] == 1 and rep["solver_status"] == "partial"


# --- round 14 batch 6: the lone solve attempt's ceiling follows the budget ---

def _sent_timeout(events, tag):
    return float(next(e for e in events if f"{tag}.sent " in e).split("timeout=")[1])

def test_the_solve_call_may_use_the_budget_up_to_the_gate_reserve(tmp_path):
    from llm import Role
    c = cfg(); c["limits"]["solve_reserve_s"] = 45.0
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    # a role cap well above the budget, so the reserve is what binds
    llm.roles = {"strong": Role("strong", "", "m", "", 100, 3, 240.0, {}),
                 "fast": Role("fast", "", "m", "", 100, 3, 150.0, {})}
    rep = S.solve(prob(tmp_path), llm, c, out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    usable = 300.0 - c["limits"]["safety_margin_s"]
    assert S.solve_call_cap(c, S.Budget(300.0, margin_s=15.0, phases=c["phases"])) == usable - 45.0
    assert _sent_timeout(rep["events"], "solve") == round(usable - 45.0)
    # the measuring calls keep the generate share, and a repair keeps its own cap
    assert _sent_timeout(rep["events"], "oracle") == round(c["phases"]["generate_call_share"] * usable)

def test_the_role_cap_still_binds_the_solve_call_when_it_is_the_smaller_of_the_two(tmp_path):
    from llm import Role
    c = cfg(); c["limits"]["solve_reserve_s"] = 45.0
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    llm.roles = {"strong": Role("strong", "", "m", "", 100, 3, 90.0, {}),
                 "fast": Role("fast", "", "m", "", 100, 3, 150.0, {})}
    rep = S.solve(prob(tmp_path), llm, c, out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert _sent_timeout(rep["events"], "solve") == 90.0

def test_config_ships_a_gate_reserve_for_the_solve_call():
    import tomllib
    cfg_toml = tomllib.loads((pathlib.Path(S.__file__).parent / "config.toml").read_text())
    reserve = cfg_toml["limits"]["solve_reserve_s"]
    assert 0 < reserve < cfg_toml["limits"]["safety_margin_s"] * 10
    # the role cap must sit above the measured solve latency, not below it
    assert cfg_toml["profiles"]["openrouter"]["strong"]["timeout_cap_s"] == 240.0


# --- round 14 batch 7: a small tier whose answers barely vary regenerates the oracle ---

def cfg_weak():
    c = cfg(); c["limits"]["cases_small"] = 20; return c

# reference() is correct, but its gen() puts 19 of 20 inputs on the same answer: 20 agreements that
# assert one fact (bench18: 195 of 200 small answers were "operation 1 is invalid").
ORACLE_WEAK_TIER = ("===ORACLE===\ndef reference(a, b):\n    return a + b\n"
                    "def gen(seed, mode):\n    return (0, 0) if seed < 19 else (1, 0)\n===END===\n")

def test_a_weak_small_tier_degrades_the_evidence_and_regenerates_the_oracle_once(tmp_path):
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_WEAK_TIER, ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg_weak(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["oracle_selfrepaired"] == 1 and rep["oracle_regenerated"] == 0
    assert len([c for c in rep["calls"] if c["tag"] == "oracle"]) == 2
    assert any("oracle.selfrepair triggered: small tier weak: 19 of 20" in e for e in rep["events"])
    # the regeneration prompt says what varies and how, and never shows the candidate
    oracle_prompts = [u for c, (r, s_, u) in zip(llm.calls, llm.prompts) if c["tag"] == "oracle"]
    assert "answers VARY" in oracle_prompts[1] and "def add(" not in oracle_prompts[1]
    # the replacement's tier is varied, so the evidence it produces is not degraded
    ev = {e["kind"]: e for e in rep["evidence"]["c1"]}
    assert ev["diff_small"]["passed"] and not (ev["diff_small"]["detail"].get("degraded") or "")
    assert rep["gate_inputs"]["weak_tiers"] == {}

def test_a_regenerated_tier_that_is_still_weak_stays_degraded_and_is_not_regenerated_again(tmp_path):
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_WEAK_TIER, ORACLE_WEAK_TIER], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg_weak(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["oracle_selfrepaired"] == 1
    assert len([c for c in rep["calls"] if c["tag"] == "oracle"]) == 2   # one regeneration, never two
    assert sum("oracle.selfrepair triggered" in e for e in rep["events"]) == 1
    assert any("regenerated small tier is no more varied" in e for e in rep["events"])
    ev = {e["kind"]: e for e in rep["evidence"]["c1"]}
    assert ev["diff_small"]["passed"] and "weak" in ev["diff_small"]["detail"]["degraded"]
    assert rep["status"] != "passed_all_gates"

def test_a_varied_tier_triggers_no_weak_regeneration(tmp_path):
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg_weak(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["oracle_selfrepaired"] == 0 and rep["gate_inputs"]["weak_tiers"] == {}
    assert rep["status"] == "passed_all_gates"


# --- round 14 batch 7: which model role each prompt goes to is config ---

def test_the_prompt_to_role_mapping_is_honoured_per_tag(tmp_path):
    c = cfg(); c["prompt_roles"] = {"solve": "strong", "oracle": "fast", "stress": "strong", "repair": "strong"}
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, c, out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    by_tag = {call["tag"]: call["role"] for call in rep["calls"]}
    assert by_tag == {"solve": "strong", "oracle": "fast", "stress": "strong"}
    assert any("stress.sent role=strong" in e for e in rep["events"])

def test_without_the_mapping_every_prompt_goes_where_it_always_did(tmp_path):
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert {call["tag"]: call["role"] for call in rep["calls"]} == {"solve": "strong", "oracle": "fast", "stress": "fast"}
    assert S.PROMPT_ROLES == {"solve": "strong", "oracle": "fast", "stress": "fast", "repair": "strong"}


# --- round 14 batch 8: oracle health is judged during the overlap, not after the solve reply ---

class _BlockedSolve(FakeLLM):
    """A solve reply that does not arrive until another call releases it. Lets a test assert that
    something happened WHILE the solver was still thinking rather than after it answered."""
    def __init__(self, script, release_on=("oracle", 2), wait_s=8.0):
        super().__init__(script)
        import threading
        self._release, self._wait_s = threading.Event(), wait_s
        self._release_tag, self._release_nth = release_on
        self._seen = {}
        self.solve_released = None   # True: released by the other call; False: the wait timed out

    def chat(self, role, system, user, *, timeout_s, max_tokens=None, tag=""):
        if tag == "solve":
            self.solve_released = self._release.wait(self._wait_s)
            return super().chat(role, system, user, timeout_s=timeout_s, tag=tag)
        r = super().chat(role, system, user, timeout_s=timeout_s, tag=tag)
        self._seen[tag] = self._seen.get(tag, 0) + 1
        if tag == self._release_tag and self._seen[tag] >= self._release_nth:
            self._release.set()
        return r

def test_a_weak_small_tier_regenerates_before_the_solve_reply_lands(tmp_path):
    llm = _BlockedSolve({"solve": [SOLVE_OK], "oracle": [ORACLE_WEAK_TIER, ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg_weak(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert llm.solve_released is True, "the regeneration waited for the solve reply instead of overlapping it"
    assert rep["oracle_selfrepaired"] == 1
    assert any("oracle.selfrepair triggered: small tier weak" in e and "solve_pending=1" in e for e in rep["events"])
    assert rep["gate_inputs"]["weak_tiers"] == {}

def test_a_high_reject_fraction_regenerates_before_the_solve_reply_lands(tmp_path):
    llm = _BlockedSolve({"solve": [SOLVE_OK], "oracle": [ORACLE_REJECTS_ITS_OWN_GEN, ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert llm.solve_released is True
    assert rep["oracle_selfrepaired"] == 1
    assert any("oracle.selfrepair triggered: validate() rejected" in e and "solve_pending=1" in e for e in rep["events"])
    assert rep["gate_inputs"]["small"] == 5   # the gate ran on the replacement's cases

def test_the_example_dispute_still_waits_for_a_candidate(tmp_path):
    # The oracle is healthy on its own terms -- it generates and validates fine -- and is only
    # contradicted by the hand trace a candidate carries, so nothing can fire until one arrives.
    llm = _BlockedSolve({"solve": [SOLVE_OK], "oracle": [ORACLE_OFF_BY_1000, ORACLE_OK], "stress": [STRESS_OK]},
                        release_on=("stress", 1))
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["oracle_selfrepaired"] == 1
    trig = next(e for e in rep["events"] if "oracle.selfrepair triggered" in e)
    assert "hand-traced examples" in trig and "solve_pending=0" in trig
    # and it happened after the candidate was built, not during the overlap
    assert rep["events"].index(trig) > rep["events"].index(next(e for e in rep["events"] if "candidate.added" in e))

def test_a_health_regeneration_spends_the_one_shot_the_dispute_would_have_used(tmp_path):
    # This oracle is BOTH self-rejecting (health, fires at prep) and off by 1000 against every
    # hand-traced example (dispute, would fire later). One budget: exactly one regeneration.
    bad = ("===ORACLE===\ndef reference(a, b):\n    return a + b + 1000\n"
           "def gen(seed, mode):\n    return (seed, 1)\n"
           "def validate(a, b):\n    return a == 0\n===END===\n")
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [bad, bad], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["oracle_selfrepaired"] == 1
    assert len([c for c in rep["calls"] if c["tag"] == "oracle"]) == 2   # never a third
    assert sum("oracle.selfrepair triggered" in e for e in rep["events"]) == 1
    assert any("validate() rejected" in e for e in rep["events"] if "oracle.selfrepair triggered" in e)

def test_the_oracle_afford_threshold_follows_the_fast_role_cap(tmp_path):
    from llm import Role
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    c = cfg()
    run = S.Run(prob(tmp_path), llm, c, str(tmp_path / "run"), 1.0, lambda: 0.0)
    # no role table (FakeLLM): the configured number stands
    assert S.oracle_afford_s(run, "oracle_selfrepair_afford_s") == 130.0
    # a fast role whose cap is low needs less, and the config value is only a ceiling
    llm.roles = {"fast": Role("fast", "", "m", "", 100, 3, 40.0, {})}
    assert S.oracle_afford_s(run, "oracle_selfrepair_afford_s") == 50.0
    llm.roles = {"fast": Role("fast", "", "m", "", 100, 3, 400.0, {})}
    assert S.oracle_afford_s(run, "oracle_selfrepair_afford_s") == 130.0

def test_config_ships_afford_thresholds_a_fast_oracle_can_meet():
    import tomllib
    lim = tomllib.loads((pathlib.Path(S.__file__).parent / "config.toml").read_text())["limits"]
    assert lim["oracle_retry_afford_s"] == 60.0 and lim["oracle_selfrepair_afford_s"] == 60.0
