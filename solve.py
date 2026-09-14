"""CLI, orchestrator, budget controller, finalizer, report."""
from __future__ import annotations
import argparse, json, os, pathlib, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict


class Budget:
    def __init__(self, deadline_s: float, *, margin_s: float, phases: dict, clock=time.monotonic):
        self._clock = clock
        self.start = clock()
        self.usable_s = max(0.0, deadline_s - margin_s)
        self.phases = phases

    def elapsed(self) -> float:
        return self._clock() - self.start

    def remaining(self) -> float:
        return max(0.0, self.usable_s - self.elapsed())

    def elapsed_frac(self) -> float:
        return 1.0 if self.usable_s == 0 else self.elapsed() / self.usable_s

    def phase(self) -> str:
        f = self.elapsed_frac()
        if f >= 1.0: return "expired"
        if f < self.phases["generate_until"]: return "generate"
        if f < self.phases["gate_until"]: return "gate"
        if f < self.phases["repair_until"]: return "repair"
        if f < self.phases["settle_until"]: return "settle"
        return "emit"

    def step_timeout(self, cap_s: float, reserve_s: float = 0.0) -> float:
        return max(0.0, min(cap_s, self.remaining() - reserve_s))

    def can_afford(self, seconds: float) -> bool:
        return self.remaining() >= seconds


from llm import LLM, load_config, parse_blocks, pick_profile
from sandbox import Problem, python_static, rust_static
import verify as V

ROOT = pathlib.Path(__file__).resolve().parent


@dataclass
class Candidate:
    id: str
    source: str
    parent: str | None
    evidence: list = field(default_factory=list)
    examples: list = field(default_factory=list)   # the author's hand-traced (input, expected) cases
    examples_dropped: int = 0                      # lines of the ===EXAMPLES=== block that did not parse

    def passed_count(self) -> int:
        return sum(1 for e in self.evidence if e.passed and not e.skipped)

    def all_passed(self) -> bool:
        return bool(self.evidence) and all(e.passed for e in self.evidence)


_LANG_RULES = {
    "python": "Define def {ep}(...) at top level. Standard library only. No I/O, no print, no global mutable state, do not mutate arguments, no recursion over input-sized structures (use explicit stacks), no random, do not let set or dict iteration order reach the return value (sort, or use a list).",
    "rust": "One complete program with fn main(). Read all of stdin into one String, write via a BufWriter, std only, no unsafe, no HashMap iteration order reaching output (use BTreeMap or sort), use i128/u128 where sums can exceed i64.",
}


# One I/O contract per language, given verbatim to SOLVE, ORACLE and STRESS, because they otherwise
# each invent their own reading of the same shape.
#
# Python: the outer container. Three rounds running, both of 2beff58fa923's attempts returned () where
# the oracle returned [] on the empty input, and a repair was spent on a difference the statement is
# silent about. The judge compares with ==, so the two sides have to pick the same convention rather
# than each pick a defensible one.
_IO_RULES_PY = ("Return exactly the container types the statement names — the judge compares with `==`, so `()` is not "
                "`[]` and `(1,)` is not `[1]`. When the statement names no type for a container, use a `list` for the "
                "outer/returned sequence and a `tuple` only where the statement says tuple or pair.")

# Rust: the stdin layout. In one bench6 run 7 of 10 edge cases were rejected as invalid and in another
# the oracle answered "" to every edge, purely because its parser demanded two values on the first line.
_IO_RULES_RUST = ("Stdin is a whitespace-separated token stream. Read all of stdin, split on ASCII whitespace, and consume "
                  "tokens in exactly the order the statement lists them; never assume how tokens are split across lines, and "
                  "never require a value to be on its own line unless the statement explicitly says a value occupies a whole "
                  "line. Generated inputs must follow the same rule: emit tokens in statement order, one or many per line, and "
                  "the parser must accept both.")


_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def render(name: str, **vars) -> str:
    text = (ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    return _PLACEHOLDER.sub(lambda m: str(vars[m.group(1)]) if m.group(1) in vars else m.group(0), text)


def prompt_vars(p: Problem) -> dict:
    py = p.language == "python"
    io_rules = _IO_RULES_PY if py else _IO_RULES_RUST
    return {"statement": p.statement, "language": p.language, "entrypoint": p.entrypoint, "io_rules": io_rules,
            "contract": f"Python function `{p.entrypoint}`" if py else "Rust program reading stdin, writing stdout",
            "language_rules": " ".join(filter(None, (_LANG_RULES[p.language].format(ep=p.entrypoint), io_rules))),
            "oracle_signature": (f"Define reference(*args) with the same parameters as {p.entrypoint} and the same return value." if py
                                 else "Define reference(stdin_text: str) -> str that returns the exact expected stdout for that stdin."),
            "gen_returns": "a tuple of positional arguments" if py else "the stdin text as a str"}


class Run:
    """Per-problem state: budget, log, candidates, artifacts."""
    def __init__(self, problem: Problem, llm, cfg: dict, run_dir: str, deadline_scale: float, clock):
        self.p, self.llm, self.cfg, self.dir = problem, llm, cfg, run_dir
        self.budget = Budget(problem.deadline_s * deadline_scale, margin_s=cfg["limits"]["safety_margin_s"], phases=cfg["phases"], clock=clock)
        self.scale = deadline_scale
        self.cands: list[Candidate] = []
        self.events: list[str] = []
        self._log_lock = threading.Lock()
        self._cost_logged = False
        os.makedirs(os.path.join(run_dir, "candidates"), exist_ok=True)

    def log(self, msg: str):
        line = f"[{self.budget.elapsed():07.1f}] {msg}"
        with self._log_lock:
            self.events.append(line)
            with open(os.path.join(self.dir, "log.txt"), "a", encoding="utf-8") as f: f.write(line + "\n")

    def ext(self) -> str:
        return "py" if self.p.language == "python" else "rs"

    def cost_usd(self) -> float:
        return sum(float((c.get("usage") or {}).get("cost") or 0.0) for c in self.llm.calls)

    def over_cost(self) -> bool:
        """True once this problem has spent its cap. Every OPTIONAL model call -- the oracle retry,
        the oracle self-repair, a repair, an adjudication regeneration -- must consult this; only
        the initial concurrent calls can't be pre-checked, since nothing has been spent yet."""
        usd = self.cost_usd()
        if usd < self.cfg["limits"]["max_cost_usd_per_problem"]:
            return False
        if not self._cost_logged:
            self._cost_logged = True
            self.log(f"cost.cap reached usd={usd:.4f}")
        return True

    def add_candidate(self, source: str, parent: str | None) -> Candidate:
        c = Candidate(f"c{len(self.cands) + 1}", source, parent); self.cands.append(c)
        with open(os.path.join(self.dir, "candidates", f"{c.id}.{self.ext()}"), "w", encoding="utf-8") as f: f.write(source)
        self.log(f"candidate.added id={c.id} parent={parent}")
        return c

    def chat(self, role: str, prompt_name: str, cap_s: float, **vars):
        timeout = self.budget.step_timeout(min(cap_s, self.llm.roles[role].timeout_cap_s if hasattr(self.llm, "roles") else cap_s), reserve_s=10.0)
        self.log(f"{prompt_name}.sent role={role} timeout={timeout:.0f}")
        r = self.llm.chat(role, "You are a precise competitive-programming engineer.", render(prompt_name, **vars), timeout_s=timeout, tag=prompt_name)
        self.log(f"{prompt_name}.done error={r.error} latency={r.latency_s:.1f} usage={r.usage}")
        return r


def static_evidence(p: Problem, source: str) -> "V.Evidence":
    probs = python_static(source, p.entrypoint) if p.language == "python" else rust_static(source)
    return V.Evidence("static", not probs, detail={"problems": probs})


def candidate_score(c: Candidate) -> tuple:
    mismatches = sum(int(e.detail.get("mismatches", 0)) for e in c.evidence)
    failures = sum(1 for e in c.evidence if not e.passed and not e.skipped)
    return (c.passed_count(), -mismatches, -failures, -int(c.id[1:]))


def best_candidate(cands: list[Candidate]) -> Candidate | None:
    if not cands: return None
    return max(cands, key=candidate_score)


def write_solution(out_path: str, source: str):
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: f.write(source.rstrip() + "\n")
    os.replace(tmp, out_path)


def solve(problem: Problem, llm, cfg: dict, *, out_path: str, run_dir: str, deadline_scale: float = 1.0, clock=time.monotonic) -> dict:
    run = Run(problem, llm, cfg, run_dir, deadline_scale, clock)
    run.log(f"intake lang={problem.language} deadline={problem.deadline_s} scale={deadline_scale} usable={run.budget.usable_s:.0f}")
    pv = prompt_vars(problem)
    attempts = max(1, int(cfg["limits"].get("solve_attempts", 1)))
    with ThreadPoolExecutor(max_workers=attempts + 2) as ex:
        # All of these run concurrently, so the generate phase costs max(), not sum() —
        # each call can therefore have the whole generate budget rather than a share of it.
        gen_cap = cfg["phases"]["generate_call_share"] * run.budget.usable_s
        f_solves = [ex.submit(run.chat, "strong", "solve", gen_cap, **pv) for _ in range(attempts)]
        f_oracle = ex.submit(run.chat, "fast", "oracle", gen_cap, **pv)
        f_stress = ex.submit(run.chat, "fast", "stress", gen_cap, **pv)
        r_solves = [f.result() for f in f_solves]
        r_oracle, r_stress = f_oracle.result(), f_stress.result()
    for i, r_solve in enumerate(r_solves):
        blocks = parse_blocks(r_solve.text)
        code = blocks.get("CODE", "")
        if not code.strip() and r_solve.text.strip():
            code = r_solve.text.strip()   # no ===CODE=== block parsed at all: fall back to the raw reply so a file is still emitted
            run.log(f"solve.no_code_block attempt={i + 1} using raw reply text")
        if not code.strip():
            continue
        # Identical attempts are one candidate: gating a duplicate costs a full gate pass and can
        # only reach the same verdict. Sampling at temperature > 0 usually differs, but not always.
        if any(c.source.strip() == code.strip() for c in run.cands):
            run.log(f"solve.duplicate attempt={i + 1} identical to an earlier attempt, dropped")
            continue
        cand = run.add_candidate(code, None); cand.evidence.append(static_evidence(problem, code))
        # The author's own hand trace of the statement: checked against this candidate (gate step
        # diff_examples) and against the oracle (prepare_gate_inputs), which never saw it.
        ex_block = blocks.get("EXAMPLES", "")
        cand.examples = V.parse_examples(problem, ex_block)
        cand.examples_dropped = max(0, V.example_line_count(ex_block) - len(cand.examples))
        run.log(f"solve.examples id={cand.id} parsed={len(cand.examples)} dropped={cand.examples_dropped}")
    oracle_src = parse_blocks(r_oracle.text).get("ORACLE", "")
    # The oracle is the single source of ground truth for every gate step, so a timeout here
    # (roughly half of them clip at a 60s median-tuned cap; see BENCHMARK-FINDINGS.md F4) costs
    # the entire run its verification, not just one call. One retry, only when the budget can
    # still afford a call as slow as the measured worst case -- config.toml's
    # limits.oracle_retry_afford_s, not a literal, since that worst case is a function of whatever
    # model is configured for the "fast" role.
    #
    # A first call that TIMED OUT is a different bet from one that answered without the block: the
    # retry will run to the same cap, so it is only worth making when the budget covers that cap AND
    # still leaves a repair's worth of time to use the oracle for something. Round 13 spent 240 s of
    # a 285 s run on two capped oracle calls and gated nothing.
    oracle_timed_out = r_oracle.error == "timeout"
    if oracle_timed_out:
        fast_cap = run.llm.roles["fast"].timeout_cap_s if hasattr(run.llm, "roles") else gen_cap
        afford = run.budget.can_afford(fast_cap + cfg["limits"]["repair_afford_s"])
    else:
        afford = run.budget.can_afford(cfg["limits"]["oracle_retry_afford_s"])
    if not oracle_src.strip() and afford and not run.over_cost():
        run.log(f"oracle.retry reason={'timeout' if oracle_timed_out else 'no_block'}, retrying once")
        r_oracle = run.chat("fast", "oracle", gen_cap, **pv)
        oracle_src = parse_blocks(r_oracle.text).get("ORACLE", "")
    stress_src = parse_blocks(r_stress.text).get("STRESS", "")
    try:
        gi = V.prepare_gate_inputs(problem, oracle_src, stress_src, cfg["limits"], workdir=os.path.join(run_dir, "oracle"), budget=run.budget, log=run.log,
                                   examples={c.id: c.examples for c in run.cands if c.examples})
    except Exception as e:
        run.log(f"gate.crashed {type(e).__name__}: {e}")
        if run.cands:
            run.cands[-1].evidence.append(V.Evidence("gate_error", False, detail={"error": f"{type(e).__name__}: {e}"}))
        gi = V.GateInputs(oracle_src=oracle_src, notes=[f"prepare_gate_inputs crashed: {type(e).__name__}: {e}"])
    # The oracle CALL can succeed while the CODE it wrote crashes at runtime -- reference() raising on
    # every input, gen() raising while unpacking its own tuple -- leaving zero usable cases in every
    # tier even though prepare_gate_inputs ran to completion. That's silent: no gate step fails (there's
    # nothing to check), so the run would ship unverified. Recover once, the same way adjudication
    # does (regenerate_oracle, reusing the fast model with the collected failure notes as context), but
    # through its OWN one-shot counter (gi.oracle_selfrepairs) so this can fire and the unrelated
    # adjudication regeneration (gi.oracle_regens, in the repair loop below) can still fire later in
    # the same run -- one broken-oracle recovery must never spend the other's budget.
    # Second trigger, same one-shot path: the oracle ran, but its own validate() threw out most of what
    # its own gen() produced (30 of 32 small inputs on bench8/2beff58fa923). gen and validate are then
    # two readings of the same preconditions and one of them is wrong, so the survivors are either too
    # few to be coverage or inputs one half of the oracle calls illegal -- either way, rewriting both
    # from one precondition list beats gating on them.
    # Third trigger, same one-shot path: the oracle contradicts most of the inputs the SOLVE authors
    # hand-traced from the statement. Two independent readings that far apart cannot both be right,
    # and the oracle is the side this system can rewrite -- blaming the candidate here would spend a
    # repair attempt making correct code agree with a wrong reference.
    selfrepair_why = ""
    disputed = False
    if V.oracle_unusable(gi):
        selfrepair_why = "no usable case in any tier"
    elif gi.validate_reject_frac >= cfg["limits"]["validate_reject_frac_regen"]:
        selfrepair_why = f"validate() rejected {gi.validate_reject_frac:.0%} of its own gen() inputs"
    elif V.examples_disputed(gi):
        ec = gi.example_checks
        disputed = True
        selfrepair_why = f"reference disagrees with hand-traced examples on {len(ec['disagreements'])} of {ec['cases']}"
        run.log(f"oracle.disputed {selfrepair_why}")
    if selfrepair_why and run.budget.can_afford(cfg["limits"]["oracle_selfrepair_afford_s"]) and not run.over_cost():
        run.log(f"oracle.selfrepair triggered: {selfrepair_why}; notes={'; '.join(gi.notes)[:300]}")
        prev = gi
        extra = V.examples_dispute_extra(gi) if disputed else V.selfrepair_extra(gi)
        try:
            gi = V.regenerate_oracle(run, gi, extra, pv, counter="oracle_selfrepairs", workdir_tag="oracle_selfrepair")
        except Exception as e:
            run.log(f"oracle.selfrepair crashed {type(e).__name__}: {e}")
        # The replacement was asked specifically about these inputs; if it still contradicts half of
        # them, no reference in this run is ground truth and no diff_* step against it is full evidence.
        if disputed and V.examples_disputed(gi):
            ec = gi.example_checks
            gi.diff_degraded = f"reference disagrees with hand-traced examples on {len(ec['disagreements'])} of {ec['cases']}"
            run.log(f"oracle.disputed after regeneration: {gi.diff_degraded}")
        # Coming from the rejection trigger there was a working-but-inconsistent oracle to lose: keep it
        # unless the replacement is actually better. (The crash trigger has nothing to fall back to.)
        if prev.validate_reject_frac and gi is not prev and not gi.regen_failed \
                and (V.oracle_unusable(gi) or gi.validate_reject_frac > prev.validate_reject_frac):
            run.log(f"oracle.selfrepair kept the original oracle: regenerated reject_frac={gi.validate_reject_frac:.2f} small={len(gi.cases_small)}")
            prev.notes.append("oracle self-repair discarded: the regenerated oracle rejected at least as much of its own gen() output")
            prev.oracle_selfrepairs = gi.oracle_selfrepairs
            gi = prev
    repairs = 0
    syntax_repairs = 0
    while run.cands:
        # Gate every attempt that still has only its static evidence before spending a repair: a
        # second independent attempt is cheaper than a repair and often already correct. Once all
        # are gated, work on whichever passed the most gates (best_candidate's ranking), which is
        # also the one that gets emitted.
        ungated = [c for c in run.cands if len(c.evidence) == 1 and c.evidence[0].passed]
        cand = ungated[0] if ungated else best_candidate(run.cands)
        try:
            if len(cand.evidence) == 1 and cand.evidence[0].passed:
                cand.evidence = [cand.evidence[0]] + V.run_gate(problem, cand.source, gi, workdir=os.path.join(run_dir, cand.id), budget=run.budget, limits=cfg["limits"], log=run.log, examples=cand.examples)
                # The Rust compile step may have added the imports rustc asked for; the gate ran that
                # patched source, so it is the one that must be emitted and re-gated from here on.
                patched = next((e.detail["patched_source"] for e in cand.evidence if "patched_source" in e.detail), None)
                if patched:
                    cand.source = patched
                    with open(os.path.join(run.dir, "candidates", f"{cand.id}.{run.ext()}"), "w", encoding="utf-8") as f: f.write(patched)
                    run.log(f"candidate.imports_added id={cand.id}")
            if cand.all_passed():
                # "static"/"compile" are pre-flight checks, not verification against the oracle; if
                # everything past them was skipped, nothing was actually checked -- don't log this as
                # a pass, or the log reads as success for a run that verified nothing (see gi.notes
                # for why: usually "no oracle source" or "no stress source").
                if all(e.skipped for e in cand.evidence if e.kind not in ("static", "compile")):
                    run.log(f"gate.unverifiable nothing could be checked: {'; '.join(gi.notes) or 'no cases available'}")
                else:
                    run.log("gate.passed")
                break
            failed = next(e for e in cand.evidence if not e.passed)
            # repr, not json.dumps: json renders a tuple and a list identically as [...], and the
            # tuple/list distinction is exactly what several of these failures are about.
            run.log(f"gate.failed cand={cand.id} kind={failed.kind} detail={repr(failed.detail)[:300]}")
            # Another independent attempt is still ungated: gate it before spending a repair call.
            # It costs no model call, and an attempt that already passes beats a repaired one.
            if len(ungated) > 1 and run.budget.can_afford(cfg["limits"]["repair_afford_s"]):
                run.log(f"gate.next_attempt {len(ungated) - 1} attempt(s) still ungated, trying before repair")
                continue
            # Every attempt is now gated, and `cand` is merely the one gated last -- repair the one
            # that got furthest instead, which is also the candidate that would be emitted.
            cand = best_candidate(run.cands)
            failed = next((e for e in cand.evidence if not e.passed), None)
            if failed is None:
                run.log("gate.passed"); break
            if ungated and cand.id != ungated[0].id:
                run.log(f"repair.target {cand.id} (best of {len(run.cands)} candidates), not the last gated")
            # A static/compile failure is mechanical (missing import, missing mut), not a semantic
            # defect -- it gets its own small budget so it can't eat the attempts meant for an actual
            # behavioral bug (diff_*, behavior, stress, overflow).
            is_syntax = failed.kind in ("static", "compile")
            cap = cfg["limits"]["max_syntax_repairs"] if is_syntax else cfg["limits"]["max_repairs"]
            count = syntax_repairs if is_syntax else repairs
            if count >= cap or run.budget.phase() not in ("generate", "gate", "repair") or not run.budget.can_afford(cfg["limits"]["repair_afford_s"]):
                break
            if run.over_cost():
                break
            if is_syntax:
                syntax_repairs += 1
            else:
                repairs += 1
            new_source, verdict = V.repair(run, cand, failed, gi, pv)
            if verdict == "oracle":
                # repair.md tells the model to repeat the current code unchanged when it blames the
                # oracle, so its CODE block is not a fix. With no regeneration left there is nothing
                # to act on: adding that block as a new candidate is how a worse candidate became the
                # emitted one on 1c182498c9c7.
                if gi.oracle_regens or gi.regen_failed:
                    run.log("repair.oracle_verdict_unactionable no oracle regeneration left"); break
                gi = V.regenerate_oracle(run, gi, V.dispute_extra(gi, failed), pv, counter="oracle_regens")
                if gi.regen_failed:
                    run.log("oracle.regen failed: stopping repair loop"); break
                cand.evidence = cand.evidence[:1]   # re-gate the same candidate against the new oracle
                continue
            if new_source.strip() and new_source.strip() != cand.source.strip():
                nc = run.add_candidate(new_source, cand.id); nc.evidence.append(static_evidence(problem, new_source))
                # The examples describe the problem, not the code: a repaired child inherits them and
                # is held to the same hand trace its parent was.
                nc.examples, nc.examples_dropped = cand.examples, cand.examples_dropped
            else:
                break
        except Exception as e:
            run.log(f"gate.crashed {type(e).__name__}: {e}")
            cand.evidence.append(V.Evidence("gate_error", False, detail={"error": f"{type(e).__name__}: {e}"}))
            break
    # settle/emit: the report is written before the solution file, so a failure writing one never loses the other.
    # The emit log line is recorded before the report is assembled, so it's present in the persisted
    # report.json's `events` too, not just the in-memory list mutated after the file was written.
    best = best_candidate(run.cands)
    status = "no_candidate"
    solver_status = ("candidate" if run.cands else
                     "timeout" if any(r.error == "timeout" for r in r_solves) else
                     "malformed")
    if best is not None:
        # Python's "overflow" evidence is always skipped -- Python ints are arbitrary precision, so
        # the check is definitionally not applicable, not a coverage gap -- and must not by itself
        # demote a fully-passing Python run out of "passed_all_gates". A skipped overflow check on a
        # Rust candidate (no stress input / no budget) is a real gap and still counts.
        # Evidence carrying detail["degraded"] (a stress input that is not really max-size, a tier
        # with far too few cases) passed against weaker input than the gate claims to check, so it
        # is not full evidence either -- count it exactly like a skip.
        skipped = any(e.skipped or e.detail.get("degraded") for e in best.evidence if not (e.kind == "overflow" and problem.language == "python"))
        # "static"/"compile" are pre-flight, not verification. A candidate that was never gated (a
        # second attempt left ungated when the budget ran out) has only its static check -- nothing
        # skipped, nothing failed, and nothing checked either. Measured on bench7/2beff58fa923: no
        # oracle at all, and the finalizer's tie-break picked the ungated attempt and called it a
        # full pass. A full pass needs at least one real check to have actually run.
        checked = any(not e.skipped for e in best.evidence if e.kind not in ("static", "compile"))
        status = ("passed_all_gates" if best.all_passed() and not skipped and checked
                  else "emitted_unverified" if best.all_passed() else "emitted_with_failures")
        run.log(f"emit candidate={best.id} status={status}")
    report = {"problem_id": problem.problem_id, "language": problem.language, "profile": cfg.get("profile"), "deadline_s": problem.deadline_s,
              "deadline_scale": deadline_scale, "elapsed_s": round(run.budget.elapsed(), 1), "status": status,
              "solver_status": solver_status,
              "final_candidate": best.id if best else None, "repairs": repairs, "syntax_repairs": syntax_repairs, "oracle_regenerated": gi.oracle_regens,
              "oracle_selfrepaired": gi.oracle_selfrepairs,
              "evidence": {c.id: [asdict(e) for e in c.evidence] for c in run.cands}, "parents": {c.id: c.parent for c in run.cands},
              "examples": {c.id: {"count": len(c.examples), "dropped": c.examples_dropped} for c in run.cands},
              "calls": list(llm.calls),
              "token_usage": {"prompt": sum((c["usage"] or {}).get("prompt_tokens", 0) for c in llm.calls),
                              "completion": sum((c["usage"] or {}).get("completion_tokens", 0) for c in llm.calls)},
              "cost_usd": round(run.cost_usd(), 5) if any((c.get("usage") or {}).get("cost") is not None for c in llm.calls) else None,
              "cost_cap_usd": cfg["limits"].get("max_cost_usd_per_problem"),
              "gate_inputs": gi.summary(), "events": run.events}
    with open(os.path.join(run_dir, "report.json"), "w", encoding="utf-8") as f: json.dump(report, f, indent=2, default=str)
    if best is not None:
        write_solution(out_path, best.source)
    return report


def bench(sample_dir: str, out_dir: str, cfg: dict, llm, deadline_scale: float) -> list[dict]:
    os.makedirs(out_dir, exist_ok=True); rows = []
    try:
        for name in sorted(os.listdir(sample_dir)):
            if not name.endswith(".json"): continue
            pid = name[:-5][:12]   # fallback id from the filename if Problem.load never succeeds
            try:
                llm.calls.clear()   # each row's calls/tokens/cost must reflect only this problem
                p = Problem.load(os.path.join(sample_dir, name)); pid = p.problem_id[:12]
                rep = solve(p, llm, cfg, out_path=os.path.join(out_dir, f"{pid}.{'py' if p.language == 'python' else 'rs'}"), run_dir=os.path.join(out_dir, pid), deadline_scale=deadline_scale)
                ev = {e["kind"]: e for e in rep["evidence"].get(rep["final_candidate"] or "", [])}
                mark = lambda k: "n/a" if k not in ev else "skip" if ev[k].get("skipped") else "FAIL" if not ev[k]["passed"] else ("degraded" if (ev[k].get("detail") or {}).get("degraded") else "pass")
                rows.append({"id": pid, "lang": p.language, "compiles": mark("compile"), "examples": mark("diff_examples"), "public": mark("diff_public"), "edge": mark("diff_edge"), "small": mark("diff_small"),
                             "medium": mark("diff_medium"), "behavior": mark("behavior"), "stress": mark("stress"), "overflow": mark("overflow"), "repairs": rep["repairs"], "syntax_repairs": rep["syntax_repairs"], "calls": len(rep["calls"]),
                             "tokens": f"{rep['token_usage']['prompt']}/{rep['token_usage']['completion']}", "elapsed": rep["elapsed_s"], "cost": rep["cost_usd"], "status": rep["status"]})
            except Exception as e:
                rows.append({"id": pid, "lang": "?", "compiles": "n/a", "examples": "n/a", "public": "n/a", "edge": "n/a", "small": "n/a", "medium": "n/a", "behavior": "n/a", "stress": "n/a", "overflow": "n/a",
                             "repairs": 0, "syntax_repairs": 0, "calls": 0, "tokens": "0/0", "elapsed": 0.0, "cost": None, "status": f"error: {type(e).__name__}: {str(e)[:200]}"})
    finally:
        hdr = "| Problem | Lang | Compiles | Examples | Public | Edge | Small | Medium | Behavior | Stress | Overflow | Repairs | Syntax Repairs | Calls | Tokens in/out | Elapsed s | Cost USD | Status | Hidden tests |\n|---|---|---|---|---|---|---|---|---|---|---|---:|---:|---:|---|---:|---:|---|---|\n"
        body = "".join(f"| {r['id']} | {r['lang']} | {r['compiles']} | {r['examples']} | {r['public']} | {r['edge']} | {r['small']} | {r['medium']} | {r['behavior']} | {r['stress']} | {r['overflow']} | {r['repairs']} | {r['syntax_repairs']} | {r['calls']} | {r['tokens']} | {r['elapsed']} | {r['cost'] if r['cost'] is not None else 'n/a'} | {r['status']} | unknown |\n" for r in rows)
        costs = [r["cost"] for r in rows if r["cost"] is not None]
        total_cost = f"{sum(costs):.4f}" if costs else "unknown"
        note = f"\nprofile={cfg.get('profile')} deadline_scale={deadline_scale} total_cost_usd={total_cost}. 'pass' = passed local gates; hidden-test status is unknown.\n"
        with open(os.path.join(out_dir, "benchmark.md"), "w", encoding="utf-8") as f: f.write(hdr + body + note)
    return rows


def missing_api_key(cfg: dict) -> str | None:
    for role in cfg["roles"].values():
        if role.api_key_env and role.api_key_env not in os.environ:
            return role.api_key_env
    return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ChallengeBox AI solver")
    ap.add_argument("problem"); ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--profile", default=None, help="config.toml profile; default: the first whose API key is set"); ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--deadline-scale", type=float, default=1.0); ap.add_argument("--run-dir", default=None)
    ap.add_argument("--bench", action="store_true", help="treat `problem` as a directory of *.json samples and `-o` as an output directory; writes out_dir/benchmark.md")
    a = ap.parse_args(argv)
    a.profile = a.profile or pick_profile(a.config)
    if a.bench:
        if not os.path.isdir(a.problem):
            print(f"invalid sample directory: {a.problem}", file=sys.stderr); return 2
        cfg = load_config(a.config, a.profile)
        missing = missing_api_key(cfg)
        if missing:
            print(f"missing API key: set {missing}, or the key of another profile in {a.config}", file=sys.stderr); return 2
        rows = bench(a.problem, a.output, cfg, LLM(cfg["roles"]), a.deadline_scale)
        print(f"wrote {len(rows)} rows to {os.path.join(a.output, 'benchmark.md')}")
        return 0
    try:
        problem = Problem.load(a.problem)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f"invalid problem: {e}", file=sys.stderr); return 2
    cfg = load_config(a.config, a.profile)
    missing = missing_api_key(cfg)
    if missing:
        print(f"missing API key: set {missing}, or the key of another profile in {a.config}", file=sys.stderr); return 2
    # Timestamped so re-running the same problem never appends to the previous run's log.txt or
    # overwrites its report.json; an explicit --run-dir is used exactly as given.
    run_dir = a.run_dir or str(ROOT / "runs" / f"{problem.problem_id[:12]}-{time.strftime('%Y%m%d-%H%M%S')}")
    rep = solve(problem, LLM(cfg["roles"]), cfg, out_path=a.output, run_dir=run_dir, deadline_scale=a.deadline_scale)
    print(json.dumps({k: rep[k] for k in ("status", "final_candidate", "elapsed_s", "repairs", "token_usage", "cost_usd")}))
    return {"passed_all_gates": 0, "emitted_unverified": 1, "emitted_with_failures": 1}.get(rep["status"], 3)


if __name__ == "__main__":
    sys.exit(main())
