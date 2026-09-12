import json, os, pathlib, re, pytest
from llm import FakeLLM
from sandbox import Problem
import solve as S

def prob(tmp_path, lang="python"):
    return Problem("pid", lang, "Return a+b for ints a,b. At most 10^18.", "add" if lang == "python" else "main", [], 300.0)

def cfg():
    return {"limits": {"safety_margin_s": 15.0, "cases_small": 5, "cases_medium": 2, "stress_limit_python_s": 5.0, "stress_limit_rust_s": 2.0, "max_repairs": 2, "max_syntax_repairs": 2, "mem_mb": 2048, "shrink_budget_s": 1.0, "max_cost_usd_per_problem": 0.10, "oracle_retry_afford_s": 130.0, "repair_afford_s": 60.0, "oracle_selfrepair_afford_s": 130.0},
            "phases": {"generate_until": 0.32, "gate_until": 0.39, "repair_until": 0.81, "settle_until": 0.93, "generate_call_share": 0.45, "repair_call_share": 0.25}, "profile": "fake"}

SOLVE_OK = "===RULES===\nr\n===END===\n===TRAPS===\nt\n===END===\n===ALGORITHM===\na\n===END===\n===CODE===\n```python\ndef add(a, b):\n    return a + b\n```\n===END===\n"
ORACLE_OK = "===ORACLE===\nimport random\ndef reference(a, b):\n    return a + b\ndef gen(seed, mode):\n    r = random.Random(seed)\n    return (r.randint(0, 20), r.randint(0, 20))\n===END===\n"
STRESS_OK = "===STRESS===\ndef gen_max(seed):\n    return (10**18, 10**18)\nEDGES = [(0, 0), (15, 0)]\n===END===\n"   # (15, 0) deterministically exposes the planted a>=15 bug used in later tests

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

def test_oracle_regen_prompt_includes_repair_trace(tmp_path):
    llm = FakeLLM({"solve": [SOLVE_OK], "repair": [REPAIR_BLAME_ORACLE_WITH_TRACE], "oracle": [ORACLE_WRONG, ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["oracle_regenerated"] == 1
    oracle_prompts = [u for c, (r, s, u) in zip(llm.calls, llm.prompts) if c["tag"] == "oracle"]
    assert len(oracle_prompts) == 2
    assert "the spec says simple addition" in oracle_prompts[1]

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
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
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
    assert row["behavior"] == "skip" and row["stress"] == "skip"

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

def test_all_skipped_gate_does_not_log_gate_passed(tmp_path):
    # F4.4: a run that verified nothing (broken oracle, no stress source) must not log "gate.passed" --
    # that reads as success in the log even though every real check was skipped.
    ORACLE_BROKEN = "===ORACLE===\ndef reference(a, b):\n    raise RuntimeError('broken')\ndef gen(seed, mode):\n    return (1, 2)\n===END===\n"
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_BROKEN], "stress": [""]})
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

def test_repair_afford_threshold_is_read_from_config(tmp_path):
    # limits.repair_afford_s used to be a bare `60` literal gating whether the repair loop may start
    # another iteration. Raising it past the usable budget must suppress every repair attempt even
    # though max_repairs would otherwise allow one.
    llm = FakeLLM({"solve": [BUGGY], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    c = cfg(); c["limits"]["repair_afford_s"] = 10_000.0
    rep = S.solve(prob(tmp_path), llm, c, out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["repairs"] == 0 and rep["syntax_repairs"] == 0
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
    SOLVE_TUPLE = SOLVE_OK.replace("return a + b", "return (a, b)")
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
