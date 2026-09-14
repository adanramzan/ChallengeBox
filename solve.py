"""CLI, orchestrator, budget controller, finalizer, report."""
from __future__ import annotations
import argparse, json, os, pathlib, re, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
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


from llm import LLM, load_config, parse_blocks, pick_profile, salvageable, terminated_blocks
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
    algorithm: str = ""                            # the reply's ===ALGORITHM=== (or ===DESIGN===) block
    replaces: str | None = None                    # set on a fresh solve: the candidate whose failure asked for this one

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
            "gen_returns": "a tuple of positional arguments" if py else "the stdin text as a str",
            # Empty for the concurrent SOLVE calls, which have no previous attempt; fresh_solve()
            # overrides it with the failed attempt and the failure that ended it.
            "previous_attempt": ""}


# Which model role each prompt goes to when config.toml carries no [roles] table. Not a fallback
# for a missing entry only -- it is the whole mapping for an older config, and it is the mapping the
# system was tuned on before the table existed.
PROMPT_ROLES = {"solve": "strong", "oracle": "fast", "stress": "fast", "repair": "strong"}


class Run:
    """Per-problem state: budget, log, candidates, artifacts."""
    def __init__(self, problem: Problem, llm, cfg: dict, run_dir: str, deadline_scale: float, clock):
        self.p, self.llm, self.cfg, self.dir = problem, llm, cfg, run_dir
        self.budget = Budget(problem.deadline_s * deadline_scale, margin_s=cfg["limits"]["safety_margin_s"], phases=cfg["phases"], clock=clock)
        self.scale = deadline_scale
        self.cands: list[Candidate] = []
        self.events: list[str] = []
        # Set by any path that built a candidate out of a reply cut off at its timeout, so the
        # report's solver_status says the run's answer came from a salvaged stream.
        self.salvaged_partial = False
        self._log_lock = threading.Lock()
        self._cost_logged = False
        self._reply_seq: dict[str, int] = {}
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

    def save_reply(self, tag: str, prompt: str, r) -> None:
        """Both sides of every model call, verbatim, as <run_dir>/replies/<tag>-<n>.prompt.txt and
        <tag>-<n>.txt (n counts that tag's calls in this run).

        Only the PARSED product of a reply survives a run today -- candidates/c1.py is the ===CODE===
        block and nothing else -- so anything the parser dropped is gone. Two adjudications could not
        say which example lines bench19's SOLVE reply actually carried, because the drop is logged as
        a count (`parsed=3 dropped=2`) and the text no longer existed anywhere; the fix for that drop
        had to be inferred rather than read. Salvaged partial replies are written too: those are
        precisely the ones whose parse is most in doubt. No secret can reach these files -- the API
        key is a request header in llm.chat, never part of a rendered prompt or of a completion."""
        with self._log_lock:
            n = self._reply_seq[tag] = self._reply_seq.get(tag, 0) + 1
        d = os.path.join(self.dir, "replies")
        os.makedirs(d, exist_ok=True)
        text = r.text or (f"(no reply: {r.error})" if r.error else "")
        for name, body in ((f"{tag}-{n}.prompt.txt", prompt), (f"{tag}-{n}.txt", text)):
            with open(os.path.join(d, name), "w", encoding="utf-8") as f: f.write(body)

    def role(self, prompt_name: str) -> str:
        """Which model role a prompt is sent to. Pure config: config.toml's [roles] table, with
        PROMPT_ROLES as the mapping a config that does not carry the table gets."""
        return (self.cfg.get("prompt_roles") or {}).get(prompt_name, PROMPT_ROLES.get(prompt_name, "strong"))

    def chat(self, role: str, prompt_name: str, cap_s: float, *, tag: str | None = None, **vars):
        # `tag` defaults to the prompt name and is overridden only where the same prompt is sent for
        # a different purpose -- a fresh solve, which is a post-gate call and takes its own entry in
        # the role's tag_extra. It is what the log line, report.json's `calls` and the FakeLLM script
        # are keyed on, so the two are distinguishable everywhere.
        tag = tag or prompt_name
        timeout = self.budget.step_timeout(min(cap_s, self.llm.roles[role].timeout_cap_s if hasattr(self.llm, "roles") else cap_s), reserve_s=10.0)
        self.log(f"{tag}.sent role={role} timeout={timeout:.0f}")
        prompt = render(prompt_name, **vars)
        r = self.llm.chat(role, "You are a precise competitive-programming engineer.", prompt, timeout_s=timeout, tag=tag)
        self.save_reply(tag, prompt, r)
        self.log(f"{tag}.done error={r.error} latency={r.latency_s:.1f} usage={r.usage}")
        if getattr(r, "cost_lookup", None):
            self.log(f"cost.lookup id={r.cost_lookup['id']} cost={r.cost_lookup.get('cost')}")
        return r


# The SOLVE prompt's blocks, in the order it asks for them. Used only to name what a reply cut off
# at its timeout kept and what it lost; CODE is the only one the gate cannot do without.
SOLVE_BLOCKS = ("RULES", "DESIGN", "CODE", "EXAMPLES", "TRAPS", "ALGORITHM")


def log_salvage(run, cand_id: str, text: str) -> None:
    term = terminated_blocks(text)
    run.salvaged_partial = True
    run.log(f"solve.partial cand={cand_id} salvaged_blocks={[b for b in SOLVE_BLOCKS if b in term]} "
            f"missing={[b for b in SOLVE_BLOCKS if b not in term]}")


def static_evidence(p: Problem, source: str) -> "V.Evidence":
    probs = python_static(source, p.entrypoint) if p.language == "python" else rust_static(source)
    return V.Evidence("static", not probs, detail={"problems": probs})


def candidate_score(c: Candidate) -> tuple:
    mismatches = sum(int(e.detail.get("mismatches", 0)) for e in c.evidence)
    failures = sum(1 for e in c.evidence if not e.passed and not e.skipped)
    return (c.passed_count(), -mismatches, -failures, -int(c.id[1:]))


def regression_score(c: Candidate) -> tuple:
    """How a repaired child is compared against the parent it came from. Deliberately NOT
    candidate_score's mismatch totals: the child is gated against its parent's case set *plus* the
    regression case the parent's failure produced, so the two mismatch counts are over different
    inputs and are not comparable. Steps passed and steps failed are."""
    return (c.passed_count(), -sum(1 for e in c.evidence if not e.passed and not e.skipped))


def measured_stress_s(c: Candidate) -> float | None:
    """The candidate's max-size stress time, when it is a real measurement: the step ran, passed, and
    its input was a true, validated maximum. A degraded/skipped step (an unjudged or below-maximum
    input, an implausibly fast answer) is not a measurement, and a failed Python run reports a
    `limit_s + 1` sentinel rather than a duration -- neither is comparable across candidates."""
    for e in c.evidence:
        if e.kind == "stress" and e.passed and not e.skipped and not e.detail.get("degraded") and "duration_s" in e.detail:
            return float(e.detail["duration_s"])
    return None


def best_candidate(cands: list[Candidate]) -> Candidate | None:
    """Best by candidate_score, with one extra tie-break: two candidates whose evidence is otherwise
    identical (same steps passed, same mismatch and failure totals -- the common case, since most
    gate steps are pass/fail) are separated by their measured max-size stress time.

    bench15 emitted the first of two byte-identically-scored candidates purely by attempt order; the
    other one takes 8-12 s on legal maximum-size inputs where the emitted one takes 0.74 s, so the
    run shipped the fast solution by luck. The comparison is only made when EVERY candidate in the
    tied group has a real measurement (see measured_stress_s), otherwise a candidate whose timing
    step was skipped would win or lose on the absence of evidence; the fall-back is candidate_score's
    own last tie-break, the older attempt."""
    if not cands: return None
    best = max(cands, key=candidate_score)
    tied = [c for c in cands if candidate_score(c)[:3] == candidate_score(best)[:3]]
    if len(tied) > 1 and all(measured_stress_s(c) is not None for c in tied):
        return min(tied, key=lambda c: (measured_stress_s(c), int(c.id[1:])))
    return best


# One gate pass, for the affordability check before a fresh solve. Taken from the measured numbers
# in config.toml's comments -- a differential tier is bounded at 60 s and the expensive ones (an 82 s
# medium pass, a 60.9 s edge pass) are what dominate -- not from a knob, because a fresh solve's real
# cost is one strong call (repair_afford_s covers that shape) plus re-gating the result.
GATE_PASS_ESTIMATE_S = 60.0


def oracle_afford_s(run, key: str) -> float:
    """How much budget an optional ORACLE-shaped call -- the retry, a self-repair, a dispute
    regeneration -- must still have before it is worth starting.

    The [limits] values were tuned to a fast model measured at up to 128 s per call. qwen3-coder
    returns in 12-45 s, and a 130 s threshold then means a run more than half way through its
    deadline can never regenerate an oracle it already knows is broken: bench19 kept a weak,
    self-rejecting oracle for the whole run because the check first ran at 180 s with 105 s left.
    So the requirement follows the role's OWN measured ceiling -- half its cap (a call that runs to
    the cap is the pathological case, not the median) plus the time to use what comes back -- and
    the config value is a ceiling on that, never a floor. A client with no role table (FakeLLM)
    keeps the configured number."""
    role = (getattr(run.llm, "roles", None) or {}).get(run.role("oracle"))
    configured = float(run.cfg["limits"][key])
    return min(configured, role.timeout_cap_s * 0.5 + 30.0) if role is not None else configured


def solve_call_cap(cfg: dict, budget) -> float:
    """The ceiling for the initial SOLVE call: everything except what a gate pass and emission need.

    At solve_attempts = 1 this call is the only thing in the run that can produce an answer, so a
    fixed share of the generate phase is the wrong shape for it -- bench17 capped one Opus attempt at
    200 s ([phases] generate_call_share 0.70 of 285 s usable), it timed out at 199.5 s, and 85 s of
    the budget were never spent. [limits] solve_reserve_s is what the call must leave behind. The
    ORACLE and STRESS calls keep generate_call_share: once preparation overlaps the solve call they
    are no longer on the critical path. Run.chat still takes the min() with the role's own
    timeout_cap_s, which is where the model's measured ceiling lives."""
    return max(0.0, budget.usable_s - cfg["limits"].get("solve_reserve_s", 45.0))


def fresh_solve_cap(cfg: dict, budget) -> float:
    """A fresh solve is a whole new SOLVE call, but it is a POST-gate one: it has to fit alongside
    the gate pass its own result needs, so it keeps the generate share rather than solve_call_cap."""
    return cfg["phases"]["generate_call_share"] * budget.usable_s


def fresh_solve(run, prev_cand, failed, gi, pv) -> "Candidate | None":
    """One more SOLVE call that starts over from a different algorithm, carrying the previous
    attempt and the concrete failure that ended it.

    This exists because with a fixed model a patch cannot reach a second algorithm: round 13's four
    candidates for one statement were all asymptotically wrong in the same way, and every repair was
    a variant of the same approach. The new candidate is a ROOT (parent None) -- it is not a repair
    of anything -- and competes for emission through candidate_score like any other."""
    gen_cap = fresh_solve_cap(run.cfg, run.budget)
    section = V.previous_attempt_section(run.p, prev_cand, failed, gi)
    r = run.chat(run.role("solve"), "solve", gen_cap, tag="solve_fresh", **{**pv, "previous_attempt": section})
    if r.partial and not salvageable(r):
        run.log("solve.fresh the reply was cut off before ===CODE=== closed"); return None
    blocks = parse_blocks(r.text)
    code = blocks.get("CODE", "")
    if not code.strip() or any(c.source.strip() == code.strip() for c in run.cands):
        run.log("solve.fresh no new code in the reply"); return None
    nc = run.add_candidate(code, None)
    nc.replaces = prev_cand.id
    nc.evidence.append(static_evidence(run.p, code))
    ex_block = blocks.get("EXAMPLES", "")
    # The examples describe the statement, not the approach: keep the previous attempt's when this
    # reply carried none, so the new candidate is held to the same hand trace.
    nc.examples = V.parse_examples(run.p, ex_block) or prev_cand.examples
    nc.examples_dropped = max(0, V.example_line_count(ex_block) - len(nc.examples))
    nc.algorithm = blocks.get("ALGORITHM") or blocks.get("DESIGN", "")
    if r.partial:
        log_salvage(run, nc.id, r.text)
    run.log(f"solve.fresh candidate={nc.id} replaces={prev_cand.id} examples={len(nc.examples)}")
    return nc


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
    limits = cfg["limits"]
    oracle_dir = os.path.join(run_dir, "oracle")
    gi = V.GateInputs()
    prep_error = None
    repairs = 0
    syntax_repairs = 0
    fresh_solves = 0
    gate_crashed = False
    selfrepaired = False          # the one oracle self-repair this run is allowed has been spent
    oracle_checked = False        # the accumulating example check has run for at least one candidate
    settled: set[str] = set()     # example disagreements the dispute decision has already seen
    no_repair: set[str] = set()   # lineages a regression closed: another patch there is the same losing bet
    agreed_cases: set = set()     # candidate agreements already counted as a disagreement with the oracle

    f_solves: list = []

    def solve_pending() -> int:
        return sum(1 for f in f_solves if not f.done())

    def build_candidate(r_solve, attempt: int) -> "Candidate | None":
        blocks = parse_blocks(r_solve.text)
        code = blocks.get("CODE", "")
        # A reply the call's timeout cut off mid-stream is usable only if ===CODE=== closed with its
        # own ===END===; otherwise what parse_blocks returns for it is truncated source, and the
        # raw-reply fallback below would write that verbatim as the solution.
        partial = bool(r_solve.partial)
        if partial and not salvageable(r_solve):
            run.log(f"solve.partial attempt={attempt} discarded: the reply was cut off before ===CODE=== closed")
            return None
        if not code.strip() and r_solve.text.strip() and not partial:
            code = r_solve.text.strip()   # no ===CODE=== block parsed at all: fall back to the raw reply so a file is still emitted
            run.log(f"solve.no_code_block attempt={attempt} using raw reply text")
        if not code.strip():
            return None
        # Identical attempts are one candidate: gating a duplicate costs a full gate pass and can
        # only reach the same verdict. Sampling at temperature > 0 usually differs, but not always.
        if any(c.source.strip() == code.strip() for c in run.cands):
            run.log(f"solve.duplicate attempt={attempt} identical to an earlier attempt, dropped")
            return None
        cand = run.add_candidate(code, None); cand.evidence.append(static_evidence(problem, code))
        # The author's own hand trace of the statement: checked against this candidate (gate step
        # diff_examples) and against the oracle (check_oracle_examples), which never saw it.
        ex_block = blocks.get("EXAMPLES", "")
        cand.examples = V.parse_examples(problem, ex_block)
        cand.examples_dropped = max(0, V.example_line_count(ex_block) - len(cand.examples))
        cand.algorithm = blocks.get("ALGORITHM") or blocks.get("DESIGN", "")
        if partial:
            log_salvage(run, cand.id, r_solve.text)
        run.log(f"solve.examples id={cand.id} parsed={len(cand.examples)} dropped={cand.examples_dropped}")
        return cand

    def gate_candidate(cand) -> None:
        """One full gate pass over a candidate that has only its static evidence, plus the two things
        that have to happen immediately after one: adopting the source the Rust compile step patched,
        and noticing that a repaired child came out worse than its parent."""
        cand.evidence = [cand.evidence[0]] + V.run_gate(problem, cand.source, gi, workdir=os.path.join(run_dir, cand.id), budget=run.budget, limits=cfg["limits"], log=run.log, examples=cand.examples)
        # The Rust compile step may have added the imports rustc asked for; the gate ran that
        # patched source, so it is the one that must be emitted and re-gated from here on.
        patched = next((e.detail["patched_source"] for e in cand.evidence if "patched_source" in e.detail), None)
        if patched:
            cand.source = patched
            with open(os.path.join(run.dir, "candidates", f"{cand.id}.{run.ext()}"), "w", encoding="utf-8") as f: f.write(patched)
            run.log(f"candidate.imports_added id={cand.id}")
        # A repaired child that now scores below the parent it came from is a regression: the
        # repair made the candidate worse, and another patch down the same lineage is the
        # same losing bet (round-10 L1). The parent already wins emission on score; what has
        # to stop is repairing this lineage.
        parent = next((c for c in run.cands if c.id == cand.parent), None)
        if parent is not None and regression_score(cand) < regression_score(parent):
            run.log(f"repair.regressed child={cand.id} parent={parent.id}")
            no_repair.update({cand.id, parent.id})

    def check_oracle_against_examples() -> None:
        """Every candidate's hand-traced examples against the oracle, then the dispute decision.

        Candidates are gated as they arrive, so this runs once per arriving candidate over the
        examples of ALL of them -- the check accumulates, and re-running it over the earlier
        candidate's handful of tiny inputs costs one subprocess batch. The dispute decision, however,
        is made once: a verdict reached on the first candidate is not re-opened by the second unless
        that second candidate contributed a value disagreement nobody had seen."""
        nonlocal oracle_checked
        V.check_oracle_examples(problem, gi, limits, workdir=oracle_dir, budget=run.budget, log=run.log,
                                examples={c.id: c.examples for c in run.cands if c.examples})
        bad = [repr(d["input"]) for d in ((gi.example_checks or {}).get("disagreements") or [])]
        if oracle_checked and not [k for k in bad if k not in settled]:
            return
        settled.update(bad)
        oracle_checked = True
        adjudicate_oracle()

    def adjudicate_oracle(health_only: bool = False) -> None:
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
        nonlocal gi, selfrepaired
        if selfrepaired:
            return
        selfrepair_why = ""
        disputed = False
        weak = False
        if V.oracle_unusable(gi):
            selfrepair_why = "no usable case in any tier"
        elif gi.validate_reject_frac >= cfg["limits"]["validate_reject_frac_regen"]:
            selfrepair_why = f"validate() rejected {gi.validate_reject_frac:.0%} of its own gen() inputs"
        elif not health_only and V.examples_disputed(gi):
            ec = gi.example_checks
            disputed = True
            selfrepair_why = f"reference disagrees with hand-traced examples on {len(ec['disagreements'])} of {V.examples_nonrejected(ec)}"
            run.log(f"oracle.disputed {selfrepair_why}")
        # Fourth trigger, same one-shot path: the oracle ran and its small tier agreed on 200 cases, but
        # almost every one of them has the same expected answer, because gen() never reaches the state the
        # statement is about (bench18: 195 of 200 small answers were "1", "operation 1 is invalid"). The
        # tier is degraded either way (run_gate); regenerating is the only way to get real coverage back,
        # and the extra tells the model exactly what varies and how to make it vary.
        elif gi.weak_tiers.get("small"):
            weak = True
            selfrepair_why = gi.weak_tiers["small"]["note"]
        if selfrepair_why and run.budget.can_afford(oracle_afford_s(run, "oracle_selfrepair_afford_s")) and not run.over_cost():
            run.log(f"oracle.selfrepair triggered: {selfrepair_why}; solve_pending={solve_pending()}; "
                    f"notes={'; '.join(gi.notes)[:300]}")
            prev = gi
            selfrepaired = True
            extra = (V.examples_dispute_extra(gi) if disputed else
                     V.weak_tier_extra(gi) if weak else V.selfrepair_extra(gi))
            try:
                gi = V.regenerate_oracle(run, gi, extra, pv, counter="oracle_selfrepairs", workdir_tag="oracle_selfrepair")
            except Exception as e:
                run.log(f"oracle.selfrepair crashed {type(e).__name__}: {e}")
            # The replacement was asked specifically about these inputs; if it still contradicts half of
            # them, no reference in this run is ground truth and no diff_* step against it is full evidence.
            if disputed and V.examples_disputed(gi):
                ec = gi.example_checks
                gi.diff_degraded = f"reference disagrees with hand-traced examples on {len(ec['disagreements'])} of {V.examples_nonrejected(ec)}"
                run.log(f"oracle.disputed after regeneration: {gi.diff_degraded}")
            # Coming from the weak-tier trigger there is a working oracle to lose too: keep whichever of
            # the two has the LOWER share of identical answers. Either way the tier stays degraded -- a
            # regeneration that is still weak has not restored the evidence, only maybe improved it.
            if weak and gi is not prev and not gi.regen_failed:
                new_share = (gi.weak_tiers.get("small") or {}).get("share")
                old_share = (prev.weak_tiers.get("small") or {}).get("share", 1.0)
                if V.oracle_unusable(gi) or (new_share is not None and new_share >= old_share):
                    run.log(f"oracle.selfrepair kept the original oracle: regenerated small tier is no more varied "
                            f"({new_share if new_share is not None else 'unusable'} vs {old_share})")
                    prev.notes.append("oracle self-repair discarded: the regenerated oracle's small tier is no more varied than the original's")
                    prev.oracle_selfrepairs = gi.oracle_selfrepairs
                    gi = prev
            # Coming from the rejection trigger there was a working-but-inconsistent oracle to lose: keep it
            # unless the replacement is actually better. (The crash trigger has nothing to fall back to.)
            if prev.validate_reject_frac and gi is not prev and not gi.regen_failed \
                    and (V.oracle_unusable(gi) or gi.validate_reject_frac > prev.validate_reject_frac):
                run.log(f"oracle.selfrepair kept the original oracle: regenerated reject_frac={gi.validate_reject_frac:.2f} small={len(gi.cases_small)}")
                prev.notes.append("oracle self-repair discarded: the regenerated oracle rejected at least as much of its own gen() output")
                prev.oracle_selfrepairs = gi.oracle_selfrepairs
                gi = prev

    with ThreadPoolExecutor(max_workers=attempts + 2) as ex:
        # All of these run concurrently, so the generate phase costs max(), not sum() —
        # each call can therefore have the whole generate budget rather than a share of it. The
        # SOLVE call gets more than a share: it is the only one that can produce an answer, so its
        # ceiling is the budget minus what a gate pass and emission need (solve_call_cap), while the
        # measuring calls keep generate_call_share.
        gen_cap = cfg["phases"]["generate_call_share"] * run.budget.usable_s
        f_solves.extend(ex.submit(run.chat, run.role("solve"), "solve", solve_call_cap(cfg, run.budget), **pv) for _ in range(attempts))
        f_oracle = ex.submit(run.chat, run.role("oracle"), "oracle", gen_cap, **pv)
        f_stress = ex.submit(run.chat, run.role("stress"), "stress", gen_cap, **pv)
        # The ORACLE reply is waited for FIRST and its preparation starts immediately, while the
        # solve and stress calls are still in flight. Preparation is subprocess-bound work that needs
        # nothing from either of them: on bench14 the oracle landed at 30 s, stress at 51 s, and prep
        # did not start until 51 s and then ran to 62 s -- eleven seconds that were free. With a slow
        # strong model in the strong role (100-200 s) the whole prep is free. It runs in this thread,
        # not in the pool: it spawns subprocesses, and the pool's workers are for the model calls.
        r_oracle = f_oracle.result()
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
            fast_cap = run.llm.roles[run.role("oracle")].timeout_cap_s if hasattr(run.llm, "roles") else gen_cap
            afford = run.budget.can_afford(fast_cap + limits["repair_afford_s"])
        else:
            afford = run.budget.can_afford(oracle_afford_s(run, "oracle_retry_afford_s"))
        if not oracle_src.strip() and afford and not run.over_cost():
            run.log(f"oracle.retry reason={'timeout' if oracle_timed_out else 'no_block'}, retrying once")
            r_oracle = run.chat(run.role("oracle"), "oracle", gen_cap, **pv)
            oracle_src = parse_blocks(r_oracle.text).get("ORACLE", "")
        run.log(f"prep.overlap solve_pending={solve_pending()}")
        try:
            gi = V.prepare_oracle_tiers(problem, oracle_src, limits, workdir=oracle_dir, budget=run.budget, log=run.log)
        except Exception as e:
            prep_error = e
        # Oracle health is judged HERE, not after the solve reply lands. The three triggers that
        # need no candidate -- an unusable oracle, one whose validate() rejects its own gen()
        # output, a small tier whose answers do not vary -- are facts about the oracle alone, and
        # every second they wait is a second of the solve call's own latency. On bench19 prep
        # finished at 18 s knowing both that the small tier was weak and that validate() had
        # rejected 89 of 171 inputs, the solve reply landed at 180 s, and by then the afford check
        # refused the regeneration: the run shipped unverified with 100 s unused. The example
        # dispute needs a candidate's hand trace and stays where it was; it shares the same
        # one-shot budget, so a health regeneration here spends it.
        if prep_error is None:
            before_gi = gi
            adjudicate_oracle(health_only=True)
            if gi is not before_gi:
                # The regeneration re-prepped through prepare_gate_inputs with no STRESS source, so
                # it has already walked part of the max-size chain on the oracle's own gen(large).
                # prepare_stress_inputs is still to come with the real STRESS reply: give it the
                # whole chain back rather than a position advanced by a prep that had nothing to
                # prefer gen_max over.
                gi.stress_input, gi.stress_source, gi.stress_validity = None, "", ""
                gi.stress_degraded, gi.stress_tried = False, []
        r_stress = f_stress.result()
        stress_src = parse_blocks(r_stress.text).get("STRESS", "")
        if prep_error is None:
            try:
                V.prepare_stress_inputs(problem, gi, stress_src, limits, workdir=oracle_dir, budget=run.budget, log=run.log)
            except Exception as e:
                prep_error = e
        # Each SOLVE reply is built into a candidate and gated the moment it lands, while the other
        # attempt is still running. Joining on every attempt first cost bench15 43 s and bench16 31 s
        # of dead time: with a thinking model in the strong role the second reply can be 40 s behind
        # the first or run all the way into its cap and return nothing. The gate inputs are ready by
        # now (the oracle was waited for first and its preparation has already run), so nothing here
        # is waiting on anything but the candidate itself.
        attempt_no = {f: i + 1 for i, f in enumerate(f_solves)}
        r_solves = []
        for f in as_completed(f_solves):
            r_solves.append(f.result())
            cand = build_candidate(r_solves[-1], attempt_no[f])
            if cand is None or prep_error is not None or gate_crashed:
                continue   # a timed-out or empty reply produces no candidate; a crashed prep gates nothing
            try:
                check_oracle_against_examples()
            except Exception as e:
                prep_error = e; continue
            run.log(f"gate.early cand={cand.id} solve_pending={solve_pending()}")
            try:
                gate_candidate(cand)
            except Exception as e:
                run.log(f"gate.crashed {type(e).__name__}: {e}")
                cand.evidence.append(V.Evidence("gate_error", False, detail={"error": f"{type(e).__name__}: {e}"}))
                gate_crashed = True
    # No candidate carried examples (or none was built at all): the oracle's own triggers still apply.
    if prep_error is None and not oracle_checked:
        try:
            check_oracle_against_examples()
        except Exception as e:
            prep_error = e
    if prep_error is not None:
        e = prep_error
        run.log(f"gate.crashed {type(e).__name__}: {e}")
        if run.cands:
            run.cands[-1].evidence.append(V.Evidence("gate_error", False, detail={"error": f"{type(e).__name__}: {e}"}))
        gi = V.GateInputs(oracle_src=oracle_src, notes=[f"prepare_gate_inputs crashed: {type(e).__name__}: {e}"])
        # A crashed preparation leaves an oracle with no usable case in any tier, which is the
        # self-repair path's own first trigger: give the run its one recovery attempt here too.
        adjudicate_oracle()
    def repair_afford() -> float:
        """What starting a repair costs: the call at its own cap (V.repair_cap, which follows the
        configured model, not just the deadline) plus the gate pass its result needs. A call that
        cannot fit its cap can only time out, and bench16 spent 67 s of a 285 s run proving it."""
        return V.repair_cap(run) + GATE_PASS_ESTIMATE_S

    def can_fresh_solve() -> bool:
        """A fresh solve costs one strong call at the full generate cap plus a gate pass on its
        result, so it is only started while the budget still covers both (and the cost cap and the
        per-run count allow it)."""
        if fresh_solves >= cfg["limits"].get("max_fresh_solves", 1) or run.over_cost():
            return False
        need = fresh_solve_cap(cfg, run.budget) + GATE_PASS_ESTIMATE_S
        if not run.budget.can_afford(need):
            run.log(f"solve.fresh skipped reason=budget need={need:.0f} remaining={run.budget.remaining():.0f}")
            return False
        return True

    while run.cands and not gate_crashed:
        # Every attempt that arrived was already gated as it landed; what is left here are the
        # children a repair or a fresh solve mints, which carry only their static evidence. Once
        # nothing is ungated, work on whichever passed the most gates (best_candidate's ranking),
        # which is also the one that gets emitted.
        ungated = [c for c in run.cands if len(c.evidence) == 1 and c.evidence[0].passed]
        cand = ungated[0] if ungated else best_candidate(run.cands)
        try:
            if len(cand.evidence) == 1 and cand.evidence[0].passed:
                gate_candidate(cand)
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
            if len(ungated) > 1 and run.budget.can_afford(repair_afford()):
                run.log(f"gate.next_attempt {len(ungated) - 1} attempt(s) still ungated, trying before repair")
                continue
            # Every attempt is now gated, and `cand` is merely the one this iteration picked up --
            # repair the one that got furthest instead, which is also the candidate that would be
            # emitted.
            cand = best_candidate(run.cands)
            failed = next((e for e in cand.evidence if not e.passed), None)
            if failed is None:
                run.log("gate.passed"); break
            if len(run.cands) > 1:
                run.log(f"repair.target {cand.id} (best of {len(run.cands)} candidates)")
            # Two independently written candidates that produce the SAME answer on the failing input
            # are two readings of the statement against the oracle's one, and the oracle is the side
            # this system can rewrite. It is the same evidence a hand-traced example gives, so it is
            # recorded in the same place (it can then tip examples_disputed, and it tells a
            # regeneration's cross-check which of the two references was the wrong one), and it takes
            # the regeneration BEFORE any candidate repair: patching two agreeing candidates into
            # agreement with a wrong reference is how bench16-1dea lost a run that was already
            # correct. Only before a regeneration has happened -- afterwards the other candidate's
            # evidence refers to a different oracle's case list, so its index means something else.
            agreement = None if gi.oracle_regens else V.candidates_agree(problem, run.cands, cand, failed)
            if agreement:
                key = (agreement["kind"], agreement["index"], repr(agreement["actual"]))
                if key not in agreed_cases:
                    agreed_cases.add(key)
                    V.add_candidate_agreement(gi, agreement)
                if not gi.regen_failed and run.budget.can_afford(oracle_afford_s(run, "oracle_selfrepair_afford_s")) and not run.over_cost():
                    run.log(f"oracle.dispute two_candidates_agree kind={agreement['kind']} index={agreement['index']} "
                            f"authors={','.join(agreement['authors'])}")
                    gi = V.regenerate_oracle(run, gi, V.dispute_extra(gi, failed, agreement), pv, counter="oracle_regens")
                    if gi.regen_failed:
                        run.log("oracle.regen failed: stopping repair loop"); break
                    cand.evidence = cand.evidence[:1]   # re-gate against the new oracle; if it still disagrees, the next pass repairs
                    continue
            # Two failures a patch cannot fix, both routed to a fresh solution from a different
            # algorithm instead of to the patch-style repair: a timing failure on a real max-size
            # input (a smaller edit does not change an approach's complexity -- round-10 L2, round-13
            # G4, where all four candidates for one statement were asymptotically wrong in the same
            # way), and a lineage a repair already made worse (round-10 L1). A degraded stress input
            # is excluded: its timing is not a max-size measurement and must not drive a re-solve.
            timing = failed.kind == "stress" and failed.detail.get("too_slow") and not failed.detail.get("degraded")
            fresh_why = "stress timing" if timing else "repair regression" if cand.id in no_repair else ""
            if fresh_why:
                if can_fresh_solve():
                    fresh_solves += 1
                    run.log(f"solve.fresh reason={fresh_why} previous={cand.id}")
                    if fresh_solve(run, cand, failed, gi, pv) is not None:
                        continue
                # No budget, no cost headroom, no attempts left, or no usable reply: there is nothing
                # else to try on this failure -- the best candidate so far is what gets emitted.
                run.log(f"solve.fresh unavailable reason={fresh_why}")
                break
            # A static/compile failure is mechanical (missing import, missing mut), not a semantic
            # defect -- it gets its own small budget so it can't eat the attempts meant for an actual
            # behavioral bug (diff_*, behavior, stress, overflow).
            is_syntax = failed.kind in ("static", "compile")
            cap = cfg["limits"]["max_syntax_repairs"] if is_syntax else cfg["limits"]["max_repairs"]
            count = syntax_repairs if is_syntax else repairs
            if count >= cap or run.budget.phase() not in ("generate", "gate", "repair"):
                break
            if not run.budget.can_afford(repair_afford()):
                run.log(f"repair.skipped reason=budget need={repair_afford():.0f} remaining={run.budget.remaining():.0f}")
                break
            if run.over_cost():
                break
            if is_syntax:
                syntax_repairs += 1
            else:
                repairs += 1
            new_source, verdict = V.repair(run, cand, failed, gi, pv)
            if verdict == "error":
                break   # the call produced no answer at all: no verdict, no child, nothing to act on
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
            if verdict == "approach":
                # The model says no patch of this code can meet the stated limits, and repair.md asks
                # it to return the current code unchanged with that verdict -- so there is no child to
                # mint. Start over from a different algorithm instead.
                if can_fresh_solve():
                    fresh_solves += 1
                    run.log(f"solve.fresh reason=approach_verdict previous={cand.id}")
                    if fresh_solve(run, cand, failed, gi, pv) is not None:
                        continue
                run.log("solve.fresh unavailable reason=approach_verdict")
                break
            if new_source.strip() and new_source.strip() != cand.source.strip():
                nc = run.add_candidate(new_source, cand.id); nc.evidence.append(static_evidence(problem, new_source))
                # The examples describe the problem, not the code: a repaired child inherits them and
                # is held to the same hand trace its parent was.
                nc.examples, nc.examples_dropped, nc.algorithm = cand.examples, cand.examples_dropped, cand.algorithm
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
    # What the SOLVE call itself produced, independent of how the gate then judged it: "partial" says
    # the candidate came out of a reply the call's timeout cut off (see log_salvage).
    solver_status = ("partial" if run.salvaged_partial else
                     "candidate" if run.cands else
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
        # A skipped "diff_examples" is exempt for a different reason: it means the SOLVE reply
        # carried no parseable ===EXAMPLES=== block, which is prompt compliance, not a gap in what
        # the oracle-derived steps checked. Tying the emitted status to a block's presence would
        # make the status report on formatting; `checked` below is what protects a run where
        # nothing real ran at all.
        exempt = lambda e: (e.kind == "overflow" and problem.language == "python") or (e.kind == "diff_examples" and e.skipped)
        skipped = any(e.skipped or e.detail.get("degraded") for e in best.evidence if not exempt(e))
        # "static"/"compile" are pre-flight, not verification. A candidate that was never gated (a
        # second attempt left ungated when the budget ran out) has only its static check -- nothing
        # skipped, nothing failed, and nothing checked either. Measured on bench7/2beff58fa923: no
        # oracle at all, and the finalizer's tie-break picked the ungated attempt and called it a
        # full pass. A full pass needs at least one real check to have actually run.
        checked = any(not e.skipped for e in best.evidence if e.kind not in ("static", "compile"))
        status = ("passed_all_gates" if best.all_passed() and not skipped and checked
                  else "emitted_unverified" if best.all_passed() else "emitted_with_failures")
        run.log(f"emit candidate={best.id} status={status} solver={solver_status}")
    report = {"problem_id": problem.problem_id, "language": problem.language, "profile": cfg.get("profile"), "deadline_s": problem.deadline_s,
              "deadline_scale": deadline_scale, "elapsed_s": round(run.budget.elapsed(), 1), "status": status,
              "solver_status": solver_status,
              "final_candidate": best.id if best else None, "repairs": repairs, "syntax_repairs": syntax_repairs,
              "fresh_solves": fresh_solves, "replaces": {c.id: c.replaces for c in run.cands}, "oracle_regenerated": gi.oracle_regens,
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
                # "degraded" is checked before "skip" so a degraded stress input (which now records
                # itself as skipped, since its timing is not a max-size check) still says *why* in
                # the table instead of reading as a step that never ran.
                mark = lambda k: "n/a" if k not in ev else "FAIL" if not ev[k]["passed"] else "degraded" if (ev[k].get("detail") or {}).get("degraded") else "skip" if ev[k].get("skipped") else "pass"
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
