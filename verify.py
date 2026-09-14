"""Verification: cases from the oracle, differential, behavior, stress, shrink, repair glue."""
from __future__ import annotations
import ast, collections, difflib, os, re, time
from dataclasses import dataclass, field
from sandbox import CaseResult, run_python_cases, compile_rust, compile_rust_with_imports, run_rust_cases, tokens
from llm import parse_blocks, salvageable

# Prepended to every generated oracle/stress source before it runs. The oracle prompt tells the
# model to *use* random.Random(seed) but never tells it to import anything, and one live run wrote
# a reference/gen module with no imports at all, dying with NameError on the first gen() call. A
# duplicate import is harmless if the model already wrote its own; the module list is exactly what
# the oracle/stress prompts assume is available. This adds exactly one line, and it is prepended to
# the same string that gets written to disk as cand.py (see sandbox.run_python_cases), so any
# traceback line number always matches that persisted file -- it never lies relative to the artifact
# a person would actually open, only relative to the raw text the model returned.
_STDLIB_PREAMBLE = "import random, math, itertools, collections, string, heapq, bisect\n"

# The oracle prompt now defines validate() as "reference() did not raise ValueError" and asks for this
# text verbatim, because two functions written from one precondition list is more than a fast model
# delivers: every model-written validate measured so far was a token/range parser, and every
# precondition that depends on evolving state ("must currently exist", "never declared") went
# unenforced. Appended only when the model forgot it; a validate the model did write is kept as-is
# rather than fought. Note the cost: validation now runs reference() once per input.
_VALIDATE_TEMPLATE = ("\n\ndef validate(*args):\n"
                      "    try:\n"
                      "        reference(*args)\n"
                      "    except ValueError:\n"
                      "        return False\n"
                      "    return True\n")

def _ensure_validate(src: str) -> str:
    if re.search(r"^\s*def\s+validate\s*\(", src, re.M) or not re.search(r"^\s*def\s+reference\s*\(", src, re.M):
        return src
    return src + _VALIDATE_TEMPLATE

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
    stress_source: str = ""   # gen_max | gen_large | medium_degraded -- which generator produced the timing input
    stress_validity: str = ""   # accepted | unjudged -- what the oracle's validate() said about the timing input
    validate_trusted: bool = False   # the oracle's validate() accepted at least one generated input and was not distrusted
    validate_reject_frac: float = 0.0   # rejected/checked over small+medium: how far the oracle's validate() and gen() disagree
    validate_rejects: dict = field(default_factory=dict)   # tier -> (checked, rejected, up to two rejected inputs), for the self-repair prompt
    diff_degraded: str = ""   # why every diff_* evidence against this oracle is weaker than it looks (set by regenerate_oracle)
    example_checks: dict = field(default_factory=dict)   # this oracle's reference() vs the SOLVE authors' hand-traced examples
    oracle_src: str = ""
    oracle_regens: int = 0        # adjudication: repair blamed the oracle for a wrong answer
    oracle_selfrepairs: int = 0   # this module noticed its own oracle crashed and produced zero usable cases
    notes: list = field(default_factory=list)
    regressions: list = field(default_factory=list)
    regen_failed: bool = False
    def summary(self) -> dict:
        return {"small": len(self.cases_small), "medium": len(self.cases_medium), "edge": len(self.cases_edge),
                "public": len(self.cases_public), "stress": self.stress_input is not None,
                "stress_source": self.stress_source, "stress_degraded": self.stress_degraded,
                "stress_validity": self.stress_validity,
                "notes": self.notes, "example_checks": self.example_checks}

@dataclass
class Case:
    input: object
    expected: object
    tag: str = ""

MAX_EXAMPLES = 8

# A hand-traced example that sits "at a stated numeric limit" is naturally spelled 10**18, which is
# an ast.BinOp and not a literal: ast.literal_eval raises on it and the line was silently dropped.
# On bench15 that cost the candidate both of its limit-sized traces -- the only cases covering the
# trap the statement is built around -- while the other candidate, which spelled the same constant
# as 1000000000000000000, lost nothing. This is the whole of the extension: literals as before, plus
# integer arithmetic over them. Names, calls, attributes, comprehensions and f-strings stay
# unreachable, which is what keeps this a parser for model output rather than an evaluator.
_EXAMPLE_BINOPS = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
                   ast.Pow: lambda a, b: a ** b, ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b}
_MAX_EXAMPLE_INT = 10 ** 40   # bigger than any stated limit; bounds what one line can make us compute
_MAX_EXAMPLE_EXP = 10 ** 4

def _example_literal(node):
    """One node of an example line. Raises ValueError for anything outside the grammar above."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, int) and abs(node.value) > _MAX_EXAMPLE_INT:
            raise ValueError("integer too large")
        return node.value
    if isinstance(node, ast.Tuple):
        return tuple(_example_literal(e) for e in node.elts)
    if isinstance(node, ast.List):
        return [_example_literal(e) for e in node.elts]
    if isinstance(node, ast.Set):
        return {_example_literal(e) for e in node.elts}
    if isinstance(node, ast.Dict):
        if any(k is None for k in node.keys):   # {**other}
            raise ValueError("dict unpacking")
        return {_example_literal(k): _example_literal(v) for k, v in zip(node.keys, node.values)}
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        v = _example_literal(node.operand)
        if not isinstance(v, (int, float, complex)):
            raise ValueError("unary operand")
        return -v if isinstance(node.op, ast.USub) else +v
    if isinstance(node, ast.BinOp) and type(node.op) in _EXAMPLE_BINOPS:
        a, b = _example_literal(node.left), _example_literal(node.right)
        # Integers only, on both sides. That is what the arithmetic is for -- and it is also what
        # keeps [x]*3 and "ab"*10**6 out: a container or a string operand is rejected, not repeated.
        if not (isinstance(a, int) and isinstance(b, int)) or isinstance(a, bool) or isinstance(b, bool):
            raise ValueError("non-integer operand")
        if isinstance(node.op, ast.Pow) and (b > _MAX_EXAMPLE_EXP or b < 0):
            raise ValueError("exponent out of range")
        v = _EXAMPLE_BINOPS[type(node.op)](a, b)
        if abs(v) > _MAX_EXAMPLE_INT:
            raise ValueError("integer too large")
        return v
    raise ValueError(f"not allowed in an example: {type(node).__name__}")

def parse_examples(problem, block_text: str) -> list[Case]:
    """The ===EXAMPLES=== block of a SOLVE reply: input/expected pairs the solution's author
    hand-traced from the statement. This is the only ground truth in the run besides the oracle that
    was not produced by running code, and it is a SECOND reading of the statement -- so it checks the
    candidate against its own author's trace, and the oracle against a reading it did not write.

    One restricted evaluation per non-blank line (never exec: this is model output). The grammar is
    Python literals plus integer arithmetic -- `+ - * ** // %` and unary sign over int constants --
    and nothing else, so `10**18` parses where ast.literal_eval raised on it (see _example_literal).
    A line that does not evaluate to a 2-tuple is dropped; the caller counts the drops by comparing
    against the line count.
    Python args that are not a tuple are wrapped as a 1-tuple (a single-argument entrypoint); Rust args
    must be the stdin text, so anything but a str is dropped."""
    cases: list[Case] = []
    for line in [l for l in block_text.splitlines() if l.strip()][:MAX_EXAMPLES]:
        try:
            v = _example_literal(ast.parse(line.strip(), mode="eval").body)
        except (ValueError, TypeError, SyntaxError, ZeroDivisionError, OverflowError, MemoryError, RecursionError):
            continue
        if not isinstance(v, tuple) or len(v) != 2:
            continue
        args, expected = v
        if problem.language == "python":
            if not isinstance(args, tuple):
                args = (args,)
        elif not isinstance(args, str):
            continue
        cases.append(Case(args, expected, "example"))
    return cases

def example_line_count(block_text: str) -> int:
    """How many lines parse_examples actually looked at -- so `dropped` counts malformed lines, not
    the ones past the cap."""
    return len([l for l in block_text.splitlines() if l.strip()][:MAX_EXAMPLES])

def _ref_args(problem, inp) -> tuple:
    if problem.language != "python":
        return (inp,)
    return tuple(inp) if isinstance(inp, (tuple, list)) else (inp,)

def _validate_inputs(problem, oracle_src: str, inputs: list, *, workdir: str, timeout_s: float, per_case_s: float = 0.0, max_consec_timeouts: int = 0) -> tuple[list | None, str]:
    """Run the oracle's own validate() over `inputs` in the sandbox -- the single place any input the
    gate will use is checked against the statement's preconditions. Returns (verdicts, absent_reason):
    verdicts[i] is True (accepted), False (rejected) or None (validate crashed or timed out on that
    input, so there is no verdict -- which is not a rejection); verdicts is None, with a reason, when
    the oracle defines no validate() at all (detected via the harness's IMPORT: prefix).
    Policy is the caller's: make_cases drops a rejected input and distrusts a validate that rejected a
    whole tier, while the stress and shrink paths keep an input validate could not judge."""
    res = run_python_cases(oracle_src, "validate", [_ref_args(problem, i) for i in inputs],
                           workdir=workdir, timeout_s=timeout_s, per_case_s=per_case_s, max_consec_timeouts=max_consec_timeouts)
    if res and res[0].error.startswith("IMPORT:"):
        return None, "no validate() defined in oracle"
    return [(r.output is True) if r.ok else None for r in res], ""

def make_cases(problem, oracle_src: str, inputs: list, tag: str, *, workdir: str, timeout_s: float, per_case_s: float = 0.0, max_consec_timeouts: int = 0) -> tuple[list[Case], Evidence]:
    t0 = time.monotonic()
    valid_inputs = inputs
    checked = invalid_dropped = 0
    rejected_examples = []
    validation_skipped = ""
    if inputs:
        verdicts, validation_skipped = _validate_inputs(problem, oracle_src, inputs, workdir=os.path.join(workdir, "validate"),
                                                        timeout_s=timeout_s, per_case_s=per_case_s, max_consec_timeouts=max_consec_timeouts)
        if verdicts is not None:
            checked = len(inputs)
            kept = [i for i, v in zip(inputs, verdicts) if v is True]
            if kept:
                valid_inputs, invalid_dropped = kept, checked - len(kept)
                # Kept for the self-repair prompt: telling the model *which* of its own generated
                # inputs its own validate() threw out is the only concrete evidence of the
                # disagreement between the two (see prepare_gate_inputs / selfrepair_extra).
                rejected_examples = [i for i, v in zip(inputs, verdicts) if v is False][:2]
            else:
                validation_skipped = "validate() rejected every input; distrusted, kept all"
    res = run_python_cases(oracle_src, "reference", [_ref_args(problem, i) for i in valid_inputs], workdir=workdir, timeout_s=timeout_s, per_case_s=per_case_s, max_consec_timeouts=max_consec_timeouts)
    cases = [Case(i, r.output, tag) for i, r in zip(valid_inputs, res) if r.ok]
    dropped = [r.error[-200:] for r in res if not r.ok]
    # A reference() that raises anything but ValueError on an input its own gen() produced is a
    # broken reference, not an invalid input (the prompt says so, and validate() is now defined as
    # "reference did not raise ValueError"). Counted alongside validate's rejections so a crashing
    # reference can push the reject fraction over the regeneration threshold, which it could not do
    # while these were only "errors".
    crashes = sum(1 for r in res if not r.ok and "ValueError" not in r.error)
    detail = {"dropped": len(dropped), "errors": dropped[:3], "checked": checked, "invalid_dropped": invalid_dropped,
              "inputs": len(inputs), "reference_crashes": crashes}
    if rejected_examples:
        detail["rejected_examples"] = rejected_examples
    if validation_skipped:
        detail["validation_skipped"] = validation_skipped
    # Two independent signals that the oracle cannot parse this input format at all: its validate()
    # rejected every input, AND its reference() gave the same answer to inputs that differ. On
    # 4e49a099fd84 that answer was "" for all ten edges (the oracle's parser demanded two tokens on
    # the first line) and the gate blamed the candidate for printing the right answer. One signal
    # alone is weak -- a strict validator, or a problem whose answer really is constant -- but
    # together they mean this tier can only produce meaningless comparisons.
    if validation_skipped.startswith("validate() rejected every input") and len(cases) > 1 \
            and len({repr(c.expected) for c in cases}) == 1 and len({repr(c.input) for c in cases}) > 1:
        detail["unusable"] = (f"{tag}: validate rejected every input and reference gave one answer for all "
                              "-- oracle cannot parse this format")
        cases = []
    return cases, Evidence(f"oracle_{tag}", bool(cases), len(cases), time.monotonic() - t0, detail)

def run_candidate(problem, source: str, inputs: list, *, workdir: str, timeout_s: float, binary: str | None = None, overflow_checks: bool = True, mem_mb: int = 4096, per_case_s: float = 0.0, deadline_s: float | None = None, max_consec_timeouts: int = 0) -> list[CaseResult]:
    if problem.language == "python":
        return run_python_cases(source, problem.entrypoint, [tuple(i) if isinstance(i, (tuple, list)) else (i,) for i in inputs], timeout_s=timeout_s, workdir=workdir, mem_mb=mem_mb, per_case_s=per_case_s, max_consec_timeouts=max_consec_timeouts)
    if binary is None:
        binary, err = compile_rust(source, overflow_checks=overflow_checks, workdir=workdir)
        if binary is None:
            return [CaseResult(False, error="compile: " + err[-800:]) for _ in inputs]
    return run_rust_cases(binary, list(inputs), timeout_s=timeout_s, mem_mb=mem_mb, deadline_s=deadline_s)

def same(problem, actual: CaseResult, expected) -> bool:
    if not actual.ok: return False
    if problem.language == "rust": return tokens(actual.output) == tokens(expected if isinstance(expected, str) else str(expected))
    return actual.output == expected

def _actual_for_report(r: CaseResult):
    """What to show as the candidate's answer in failure evidence -- and, via repair(), in the
    repair prompt. A run that crashed or timed out has no answer: whatever reached stdout before
    the panic is a fragment, and showing it invites the repair model to chase a formatting diff
    that does not exist (a Rust panic after one correct line reads as a stray trailing newline).
    Say plainly that there was no answer, and let `error` carry the cause."""
    if r.ok:
        return r.output
    if r.timed_out:
        return "(no answer: timed out)"
    last = [l for l in (r.error or "").strip().splitlines() if l.strip()][-2:]
    return "(no answer: crashed before completing) " + " | ".join(last) if last else "(no answer: crashed before completing)"

def differential(problem, source: str, cases: list[Case], kind: str, *, workdir: str, timeout_s: float, binary: str | None = None, mem_mb: int = 4096, per_case_s: float = 0.0, max_consec_timeouts: int = 0) -> Evidence:
    t0 = time.monotonic()
    if not cases:
        return Evidence(kind, True, 0, 0.0, {"skipped": "no cases"}, skipped=True)
    if timeout_s <= 0:
        return Evidence(kind, True, 0, 0.0, {"skipped": "no budget"}, skipped=True)
    # Rust runs each case as a separate process with its own timeout, so a batch timeout must be
    # divided across cases or the ceiling is timeout_s * len(cases); Python runs the whole batch
    # inside one subprocess call, so the full timeout_s already bounds the batch.
    # The per-case division spreads the grant fairly; deadline_s is the hard ceiling on top of it,
    # so a hung candidate cannot turn timeout_s into timeout_s * len(cases) of wall clock.
    case_timeout = timeout_s if problem.language == "python" else max(0.25, timeout_s / max(1, len(cases)))
    dl = None if problem.language == "python" else time.monotonic() + timeout_s
    res = run_candidate(problem, source, [c.input for c in cases], workdir=workdir, timeout_s=case_timeout, binary=binary, mem_mb=mem_mb, per_case_s=per_case_s, deadline_s=dl, max_consec_timeouts=max_consec_timeouts)
    # The batch already ran, so counting every mismatch is free -- and the count is what tells the
    # repair model whether it is looking at a boundary bug (a few cases) or two different readings of
    # the statement (most of them). Reporting only the first mismatch discarded that for nothing.
    bad = [(i, c, r) for i, (c, r) in enumerate(zip(cases, res)) if not same(problem, r, c.expected)]
    if bad:
        i, c, r = bad[0]
        return Evidence(kind, False, len(cases), time.monotonic() - t0,
                        {"index": i, "input": c.input, "expected": c.expected, "actual": _actual_for_report(r),
                         "error": r.error[-600:], "timed_out": r.timed_out, "mismatches": len(bad), "cases": len(cases)})
    return Evidence(kind, True, len(cases), time.monotonic() - t0, {"mismatches": 0, "cases": len(cases)})

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
        # Never empty a sequence that started non-empty (round-10 L6): on 6e43a08ec05a the shrinker
        # reduced `edits` to [] and the repair model, shown a counterexample where nothing was ever
        # edited, deleted edit handling outright. A one-element witness still reproduces the failure
        # and still shows the model what the operation is; ordering (halves first, then single
        # deletions) keeps this the SMALLEST non-empty witness the greedy loop can reach.
        half = v[: n // 2], v[n // 2 :]
        for c in half:
            if 0 < len(c) < n: yield type(v)(c)
        if n > 1:
            for i in range(n):
                yield type(v)(list(v[:i]) + list(v[i + 1 :]))
        for i, e in enumerate(v):
            for se in _shrink_value(e):
                yield type(v)(list(v[:i]) + [se] + list(v[i + 1 :]))
    elif isinstance(v, dict):
        for k in list(v):
            d = dict(v); del d[k]; yield d

def shrink(problem, source: str, oracle_src: str, case: Case, *, workdir: str, budget_s: float, binary: str | None = None, validate_trusted: bool = False) -> Case:
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
                # _shrink_value knows nothing about the statement's preconditions -- driving ids to 0
                # produces duplicates, emptying a list produces a "must currently exist" violation. A
                # counterexample that the statement forbids is a phantom: it drives repair and is kept
                # as a regression that fails every later candidate forever. Reject it like a
                # non-reproducing step. Bounded by the same budget_s as everything else in this loop.
                if validate_trusted:
                    v, _ = _validate_inputs(problem, oracle_src, [cand], workdir=os.path.join(workdir, "v"), timeout_s=call_timeout)
                    if v is not None and v[0] is not True: continue
                still, e = _fails(problem, source, oracle_src, cand, workdir, binary, timeout_s=call_timeout)
                if still:
                    cur, exp, improved = cand, e, True
                    break
            if improved: break
    return Case(cur, exp, case.tag + "+shrunk")

def _log_and_note_validation(gi: GateInputs, ev: Evidence, mode: str, log) -> None:
    invalid = ev.detail.get("invalid_dropped", 0)
    crashes = ev.detail.get("reference_crashes", 0)
    skip = ev.detail.get("validation_skipped", "")
    log(f"oracle.{mode} cases={ev.cases} dropped={ev.detail['dropped']} invalid={invalid}" + (f" crashes={crashes}" if crashes else "") + (f" skip={skip}" if skip else ""))
    # Only a validate() that actually ran and was not distrusted has a verdict worth counting; a tier
    # where it was absent or rejected everything says nothing about gen/validate *disagreeing*. A
    # reference() that crashed on its own gen() input is counted either way: that is the oracle being
    # unusable, which is what the regeneration threshold exists to catch.
    checked = ev.detail.get("checked") or ev.detail.get("inputs", 0)
    if checked and (not skip or crashes):
        gi.validate_rejects[mode] = (checked, invalid + crashes, ev.detail.get("rejected_examples", []))
    if invalid:
        gi.notes.append(f"{mode}: dropped {invalid} of {ev.detail.get('checked', 0)} generated inputs as invalid (failed validate())")
    if ev.detail.get("unusable"):
        gi.notes.append(ev.detail["unusable"])

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

def _as_tuples(v):
    """Every list in a nested structure respelled as a tuple. Strings, ints and dicts are left
    exactly as they are -- a dict is never re-containered, and its keys must not be either."""
    if isinstance(v, (list, tuple)):
        return tuple(_as_tuples(x) for x in v)
    return v

def _as_lists(v):
    if isinstance(v, (list, tuple)):
        return [_as_lists(x) for x in v]
    return v

def _respellings(problem, inp) -> list:
    """Canonical container spellings of one example input, other than the one it was written in.
    The statement names the containers ("the stated tuple forms"); a hand trace can spell the same
    input with lists, and the oracle's input check then rejects an input nobody disagrees about --
    four of seven bench14 "disagreements" were exactly this. Python only: a Rust example is stdin
    text, which has no container spelling."""
    if problem.language != "python":
        return []
    out = []
    for f in (_as_tuples, _as_lists):
        try:
            v = f(inp)
        except RecursionError:
            continue
        if repr(v) != repr(inp) and all(repr(v) != repr(o) for o in out):
            out.append(v)
    return out

def _check_examples_against_oracle(problem, gi: GateInputs, oracle_src: str, examples: dict, limits: dict, *, workdir: str, budget, log) -> None:
    """Run the oracle's validate() and reference() over every distinct input the SOLVE authors
    hand-traced, and record where the reference disagrees with them. Neither side is ground truth --
    both are readings of the same prose by a model -- so a value mismatch drops nothing; it only
    measures how far the two readings are apart, which is what decides whether the oracle is disputed
    (see examples_disputed).

    A validate() REJECTION is not a disagreement about a value. Two things can be behind it, and
    neither is the oracle contradicting the trace (bench14: all 7 "disagreements" were rejections,
    0 were value mismatches, and the false dispute cost the run its oracle and its medium tier):

    * the same input spelled with different containers -- retried once, canonically respelled, and
      the accepted spelling then replaces the original *in the candidates' own example lists*, so the
      candidate is gated on the spelling the only precondition check in the system accepts;
    * an input that really does violate the stated preconditions -- the examples author was wrong.
      It is recorded as `rejected`, counts toward no dispute, and is dropped from every authoring
      candidate's examples, because failing a candidate on an input the precondition check calls
      invalid spends repairs guarding inputs a hidden test cannot contain.

    Both effects reach the candidates by mutating the example lists this function was handed -- they
    are the candidates' own lists, and `diff_examples` runs off them.
    """
    by_key: dict[str, list[tuple[list, Case]]] = {}   # input -> every (candidate's list, its Case)
    authors: dict[str, list[str]] = {}
    for cid, cases in (examples or {}).items():
        for c in cases:
            k = repr(c.input)
            by_key.setdefault(k, []).append((cases, c))
            authors.setdefault(k, []).append(cid)
    keys = list(by_key)
    cases = [by_key[k][0][1] for k in keys]
    if not cases:
        return
    per_case = limits.get("per_case_limit_s", 0.0)
    consec = limits.get("max_consecutive_case_timeouts", 0)
    step = lambda: budget.step_timeout(30.0, reserve_s=20.0)
    verdicts, _ = _validate_inputs(problem, oracle_src, [c.input for c in cases], workdir=os.path.join(workdir, "validate_examples"),
                                   timeout_s=step(), per_case_s=per_case, max_consec_timeouts=consec)
    # One respelling attempt, for the rejected inputs only, in one batch.
    normalized = 0
    trials = [(i, v) for i, c in enumerate(cases)
              if verdicts is not None and verdicts[i] is False
              for v in _respellings(problem, c.input)]
    if trials:
        tv, _ = _validate_inputs(problem, oracle_src, [v for _, v in trials], workdir=os.path.join(workdir, "validate_respelled"),
                                 timeout_s=step(), per_case_s=per_case, max_consec_timeouts=consec)
        taken: dict[int, object] = {}
        for (i, v), ok in zip(trials, tv or []):
            if ok is True and i not in taken:
                taken[i] = v
        for i, v in taken.items():
            for _, c in by_key[keys[i]]:
                c.input = v
            verdicts[i] = True
            normalized += 1
        if normalized:
            log(f"oracle.examples normalized={normalized}")
    res = run_python_cases(oracle_src, "reference", [_ref_args(problem, c.input) for c in cases], workdir=os.path.join(workdir, "ref_examples"),
                           timeout_s=step(), per_case_s=per_case, max_consec_timeouts=consec)
    disagreements, rejected, agree, errors = [], [], 0, 0
    for i, c in enumerate(cases):
        who = authors[keys[i]]
        if verdicts is not None and verdicts[i] is False:
            rejected.append({"input": c.input, "expected": c.expected, "authors": who})
            for lst, case in by_key[keys[i]]:
                lst[:] = [x for x in lst if x is not case]
            continue
        r = res[i]
        if not r.ok:
            errors += 1
            continue
        if same(problem, r, c.expected):
            agree += 1
            continue
        disagreements.append({"input": c.input, "expected": c.expected, "reference": r.output, "authors": who})
    gi.example_checks = {"cases": len(cases), "agreements": agree, "disagreements": disagreements,
                         "rejected": rejected, "errors": errors, "normalized": normalized}
    log(f"oracle.examples cases={len(cases)} agree={agree} disagree={len(disagreements)} rejected={len(rejected)} errors={errors}")

def examples_nonrejected(ec: dict) -> int:
    """Hand-traced inputs the oracle's precondition check accepted: the only ones whose answer the
    reference and the trace can actually disagree about."""
    ec = ec or {}
    return ec.get("cases", 0) - len(ec.get("rejected") or [])

def examples_disputed(gi: GateInputs) -> bool:
    """The oracle contradicts at least half of the hand-traced examples it accepted as valid. Two
    independent readings of the statement that far apart cannot both be right, and the oracle is the
    one this system can rewrite -- so this is a regeneration trigger, not a candidate failure.
    Rejected examples are excluded from both sides of the ratio: a rejection is a claim about the
    input, not about the answer."""
    ec = gi.example_checks or {}
    n = examples_nonrejected(ec)
    return n >= 2 and 2 * len(ec.get("disagreements") or []) >= n

def oracle_unusable(gi: GateInputs) -> bool:
    """True when the oracle's own gen()/reference() produced nothing across BOTH generated tiers,
    so there is no real differential coverage (its code crashed, or it produced no ===ORACLE===
    block at all). Edge cases are deliberately excluded: they are literals authored by the STRESS
    call and merely priced by reference(), so one surviving edge fixture says nothing about whether
    the oracle works -- counting it here let a hallucinated-stdlib oracle with 0 small / 0 medium
    cases pass as "usable" and skip self-repair. Public examples are excluded for the same reason:
    they are ground truth from the problem, not something the oracle produced."""
    return not gi.cases_small and not gi.cases_medium

def selfrepair_extra(gi: GateInputs) -> str:
    """Extra context for regenerate_oracle when the oracle is being reissued because of its own code
    rather than because a repair disputed one of its answers. Two shapes, matching the two triggers in
    solve(): the code crashed (the failure notes prepare_gate_inputs collected -- tracebacks from
    reference()/gen() -- are the only evidence available), or validate() rejected most of what gen()
    produced (the counts and a couple of the rejected inputs are the evidence). Never any candidate
    material: the oracle is generated in its own context and never sees the candidate."""
    if gi.validate_reject_frac and not oracle_unusable(gi):
        gen_tiers = [v for t, v in gi.validate_rejects.items() if t in ("small", "medium")]
        checked = sum(c for c, _, _ in gen_tiers)
        rejected = sum(r for _, r, _ in gen_tiers)
        examples = [e for _, _, ex in gen_tiers for e in ex][:2]
        edge = gi.validate_rejects.get("edge")
        independent = f" (and {edge[1]} of {edge[0]} inputs from an independent generator)" if edge else ""
        shown = "\n".join(f"- {_fmt(e)}" for e in examples) or "(none captured)"
        return (f"\n\nYour reference() rejected as invalid, or crashed on, {rejected} of {checked} inputs that your own gen() produced"
                f"{independent}. Both were written from the statement's preconditions, so at least one of them "
                "misreads a precondition (or the reference has a bug that is not a precondition check at all). "
                "Here are inputs it rejected:\n"
                f"{shown}\n"
                "Re-list the preconditions from the statement, then write reference() and gen() from that one "
                "list, and trace one gen() output through reference() before answering.\n")
    notes = "\n".join(f"- {n}" for n in gi.notes) or "(no details captured)"
    return ("\n\nYour previous reference()/gen()/validate() code crashed instead of running -- these "
            "are the actual errors your code produced when executed on real inputs. Fix these specific "
            "problems (undefined names, unpacking errors, exceptions) and follow the statement "
            f"literally:\n{notes}\n")

def _examples_paragraph(gi: GateInputs) -> str:
    """The hand-traced examples this oracle's reference() contradicts, as inputs and expected values
    only. Never any candidate code: the oracle is generated in its own context and never sees it."""
    ec = gi.example_checks or {}
    bad = [d for d in (ec.get("disagreements") or []) if not d.get("from_candidates")]
    if not bad:
        return ""
    shown = "\n".join(f"- Input: {_fmt(d['input'], 800)}\n  Hand-traced expected: {_fmt(d['expected'], 800)}\n"
                      f"  Your reference returned: {_fmt(d['reference'], 800)}" for d in bad[:3])
    return (f"\n\nAn independent reader hand-traced {examples_nonrejected(ec)} inputs from this statement. Your "
            f"reference disagrees with {len(bad)} of them. The first three:\n{shown}\n"
            "Re-derive each of these from the statement's own words before writing the new reference.\n")

def examples_dispute_extra(gi: GateInputs) -> str:
    """Extra context for regenerate_oracle when the oracle is being reissued because it contradicts
    most of the hand-traced examples (examples_disputed). Inputs and expected values only."""
    return _examples_paragraph(gi) or (
        "\n\nYour reference disagreed with inputs hand-traced from this statement. Re-derive the "
        "expected output from the statement alone, sentence by sentence, before writing the new reference.\n")

def _no_answer(v) -> bool:
    """_actual_for_report renders a crash or a timeout as a "(no answer: ...)" sentence rather than
    a value. Two candidates that both crashed are not two authors agreeing on anything."""
    return isinstance(v, str) and v.startswith("(no answer")

def _same_answer(problem, a, b) -> bool:
    if b is None or _no_answer(a) or _no_answer(b):
        return False
    if problem.language == "rust":
        return tokens(a if isinstance(a, str) else str(a)) == tokens(b if isinstance(b, str) else str(b))
    return repr(a) == repr(b)

def _lineage_root(cands, c):
    """The root candidate `c` descends from: a repaired child inherits its parent's reading of the
    statement, so it is not a second author."""
    by_id = {x.id: x for x in cands}
    seen = set()
    while c.parent and c.parent in by_id and c.parent not in seen:
        seen.add(c.parent)
        c = by_id[c.parent]
    return c

def candidates_agree(problem, cands, cand, failed: Evidence) -> dict | None:
    """Another candidate that failed the SAME tier at the SAME input index with the SAME answer.

    Two solutions sampled independently, written in separate contexts, agreeing on a value the
    oracle contradicts is two readings of the statement against one -- the same shape of evidence as
    a hand-traced example and the strongest the system has that the ORACLE is the wrong side. On
    bench16-1dea both candidates were right on the failing input, the oracle had one wrong conjunct,
    and the run spent its repair on the candidates. Returns the agreeing pair, or None."""
    idx = failed.detail.get("index")
    mine = failed.detail.get("actual")
    if idx is None or not failed.kind.startswith("diff_") or _no_answer(mine):
        return None
    root = _lineage_root(cands, cand)
    for other in cands:
        if other is cand or _lineage_root(cands, other) is root:
            continue
        for e in other.evidence:
            if e.kind == failed.kind and not e.passed and e.detail.get("index") == idx \
                    and _same_answer(problem, mine, e.detail.get("actual")):
                return {"kind": failed.kind, "index": idx, "authors": sorted({cand.id, other.id}),
                        "input": failed.detail.get("input"), "actual": mine, "reference": failed.detail.get("expected")}
    return None

def add_candidate_agreement(gi: GateInputs, agreement: dict) -> None:
    """Record one candidate agreement as a value disagreement with the oracle, in the same place and
    the same shape as the hand-traced ones -- so the existing >= half rule (examples_disputed), and
    the resolution rule a regeneration's cross-check applies, both see it. Flagged so the prompt
    paragraph that calls these "hand-traced" does not claim this one was."""
    ec = gi.example_checks or {"cases": 0, "agreements": 0, "disagreements": [], "rejected": [], "errors": 0, "normalized": 0}
    ec["cases"] = ec.get("cases", 0) + 1
    ec.setdefault("disagreements", []).append(
        {"input": agreement["input"], "expected": agreement["actual"], "reference": agreement["reference"],
         "authors": agreement["authors"], "from_candidates": True})
    gi.example_checks = ec

def dispute_extra(gi: GateInputs, failed: Evidence, agreement: dict | None = None) -> str:
    """Extra context for regenerate_oracle when a repair call adjudicated candidate vs. oracle and
    blamed the oracle for a wrong answer on a specific input.

    It carries the disputed input and what the current reference answered on it -- and nothing else.
    It used to append the repair model's own prose about the candidate, which is the candidate
    leaking into the oracle's context: on runs/bench6/1dea32802072 the regenerated oracle inherited
    the candidate's exact bug and agreed with it on 4000/4000 inputs, while the original oracle had
    been right. The oracle is generated in its own context and never sees the candidate."""
    if agreement:
        return ("\n\nTwo solutions to this statement, written independently and without seeing each other, "
                "produced the SAME output on this input, and it is not what the previous reference produced:\n"
                f"Input: {_fmt(agreement['input'])}\n"
                f"Both solutions returned: {_fmt(agreement['actual'])}\n"
                f"Previous reference returned: {_fmt(agreement['reference'])}\n"
                "Re-derive the expected output from the statement alone, sentence by sentence, before writing the "
                "new reference.\n"
                + _examples_paragraph(gi))
    return ("\n\nA previous reference produced this output on this input:\n"
            f"Input: {_fmt(failed.detail.get('input'))}\nPrevious reference output: {_fmt(failed.detail.get('expected'))}\n"
            "An independent review believes the reference's output on this input does not follow the statement. "
            "Re-derive the expected output from the statement alone, sentence by sentence, before writing the new reference.\n"
            + _examples_paragraph(gi))

def _input_size(v) -> int:
    """Bytes of one gate input, for the log line -- generic over a Python argument tuple and a Rust
    stdin string without knowing the problem's shape."""
    return len((v if isinstance(v, str) else repr(v)).encode("utf-8", "replace"))

def _accept_stress_input(problem, gi: GateInputs, oracle_src: str, value, source: str, *, workdir: str, budget) -> bool:
    """Take `value` as the max-size stress input unless the oracle's own validate() rejects it, and
    record which of the three verdicts it got: accepted, rejected, or unjudged.

    The stress input is the one gate input no tier validated: an invalid max-size input makes the
    stress step meaningless -- the candidate crashes or answers nonsense on something the statement
    forbids, and a repair attempt gets spent chasing it.

    `unjudged` is the third outcome and it used to be silently treated as `accepted`: validate()
    timed out or crashed on the input, or there is no trusted validate() in this run at all, so
    nothing in the system can say whether the input is legal. On bench15 gen_max returned an input
    nested 200,000 deep against a stated cap of 60; the literal reference could not finish on it, so
    there was no verdict, and both candidates -- one of them correct and fast on every legal maximum
    -- were killed at the timing cap by an input the statement forbids. The input is still USED (a
    timing measurement on a doubtful input is better than none), but everything it produces is
    degraded: see stress(). Generic over every problem whose constraints include a bound the literal
    oracle cannot evaluate, which is precisely the class where timing matters most."""
    value = _clean_stdin(problem, value)
    verdicts = None
    if gi.validate_trusted:
        verdicts, _ = _validate_inputs(problem, oracle_src, [value], workdir=os.path.join(workdir, f"{source}_validate"),
                                       timeout_s=budget.step_timeout(20.0, reserve_s=20.0))
    verdict = ("accepted" if verdicts and verdicts[0] is True else
               "rejected" if verdicts and verdicts[0] is False else "unjudged")
    if verdict == "rejected":
        gi.notes.append(f"{source} output rejected by validate(); not used")
        return False
    gi.stress_input, gi.stress_source, gi.stress_validity = value, source, verdict
    if verdict == "unjudged":
        gi.notes.append(f"{source} output could not be validated (the reference gave no verdict on it); "
                        "its timing is indicative only")
    return True

def prepare_oracle_tiers(problem, oracle_src: str, limits: dict, *, workdir: str, budget, log) -> GateInputs:
    """Everything the gate needs that depends on the ORACLE reply alone: the public examples, the
    small and medium differential tiers, and how far the oracle's own validate() and gen() disagree.

    Split out of prepare_gate_inputs so solve() can run it the moment the oracle returns, while the
    SOLVE and STRESS calls are still in flight -- on bench14 the oracle arrived at 30 s, the stress
    reply at 51 s, and prep did not start until 51 s even though every second of it was
    subprocess-bound work that needed nothing from either. prepare_stress_inputs fills in the rest.
    """
    gi = GateInputs(oracle_src=oracle_src)
    gi.cases_public, parse_notes = _parse_public_examples(problem)
    gi.notes.extend(parse_notes)
    if gi.cases_public:
        log(f"public_examples parsed={len(gi.cases_public)} skipped={len(parse_notes)}")
    if not oracle_src.strip():
        gi.notes.append("no oracle source"); return gi
    oracle_src = _ensure_validate(_STDLIB_PREAMBLE + oracle_src)
    gi.oracle_src = oracle_src
    if gi.cases_public:
        _check_public_against_oracle(problem, gi, oracle_src, workdir=workdir, budget=budget, log=log)
    for mode, n in (("small", limits["cases_small"]), ("medium", limits["cases_medium"])):
        # The medium tier is the most expensive evidence per second (literal loops over 10^4-10^5
        # multipliers) and the least decisive: small already catches most logic bugs. It is therefore
        # TIME-BOXED rather than skipped by phase: on bench14 gen(seed, "medium") took 60.3 s to make
        # 6 cases and the run never recovered that time, and then the re-prep after an oracle
        # regeneration dropped medium entirely because the budget had moved on -- so the run's only
        # mid-size tier was paid for twice and delivered nothing. Both batches are capped at
        # [limits] medium_tier_budget_s; if the generator alone spends it, medium ships 0 cases and
        # says so, rather than eating the gate's and the repair loop's time as well.
        box = float(limits.get("medium_tier_budget_s", 20.0)) if mode == "medium" else 60.0
        t_mode = time.monotonic()
        gen = run_python_cases(oracle_src, "gen", [(s, mode) for s in range(n)], workdir=os.path.join(workdir, f"gen_{mode}"), timeout_s=budget.step_timeout(min(60.0, box), reserve_s=20.0))
        inputs = [r.output for r in gen if r.ok]
        if not inputs:
            gi.notes.append(f"gen({mode}) produced nothing: {(gen[0].error if gen else '')[-200:]}"); continue
        if mode == "medium" and time.monotonic() - t_mode >= box:
            gi.notes.append("medium tier: time box exhausted"); continue
        cases, ev = make_cases(problem, oracle_src, inputs, mode, workdir=os.path.join(workdir, f"ref_{mode}"), timeout_s=budget.step_timeout(min(60.0, box), reserve_s=20.0),
                               per_case_s=limits.get("per_case_limit_s", 0.0), max_consec_timeouts=limits.get("max_consecutive_case_timeouts", 0))
        if not cases: gi.notes.append(f"reference failed on all {mode} inputs: {ev.detail['errors']}")
        # "Trusted" = this validate() actually ran and accepted something in at least one tier, so a
        # False from it elsewhere (the stress input, a shrunk input) is a real precondition violation
        # rather than a validate that rejects everything or does not exist.
        if ev.detail.get("checked") and not ev.detail.get("validation_skipped"):
            gi.validate_trusted = True
        setattr(gi, f"cases_{mode}", cases); _log_and_note_validation(gi, ev, mode, log)
    # How far the oracle's own validate() and gen() disagree about the statement's preconditions.
    # The edge tier's counts are recorded by prepare_stress_inputs but deliberately excluded here:
    # those inputs come from the independent STRESS author, so they are corroboration, not evidence
    # about this oracle's gen().
    gen_tiers = [v for t, v in gi.validate_rejects.items() if t in ("small", "medium")]
    checked = sum(c for c, _, _ in gen_tiers)
    rejected = sum(r for _, r, _ in gen_tiers)
    if checked and rejected:
        gi.validate_reject_frac = rejected / checked
        gi.notes.append(f"validate() rejected, or reference() crashed on, {rejected} of {checked} inputs its own gen() produced (small+medium)")
    return gi

def check_oracle_examples(problem, gi: GateInputs, limits: dict, *, workdir: str, budget, log, examples: dict) -> None:
    """The hand-traced examples against the prepared oracle. Separate from prepare_oracle_tiers
    because the examples arrive with the SOLVE replies, which land after the oracle does."""
    if gi.oracle_src.strip():
        _check_examples_against_oracle(problem, gi, gi.oracle_src, examples or {}, limits, workdir=workdir, budget=budget, log=log)

def prepare_stress_inputs(problem, gi: GateInputs, stress_src: str, limits: dict, *, workdir: str, budget, log) -> None:
    """The parts that need the STRESS reply: the edge fixtures and the max-size timing input. Fills
    `gi` in place -- GateInputs stays the single result of preparation, whichever order it was
    assembled in."""
    oracle_src = gi.oracle_src
    if not oracle_src.strip():
        return
    if stress_src.strip():
        stress_src = _STDLIB_PREAMBLE + stress_src
        edges = run_python_cases(stress_src + "\ndef _edges():\n    return list(EDGES)\n", "_edges", [()], workdir=os.path.join(workdir, "edges"), timeout_s=budget.step_timeout(60.0, reserve_s=20.0))
        if edges and edges[0].ok and isinstance(edges[0].output, list):
            cleaned_edges = [_clean_stdin(problem, s) for s in edges[0].output]
            gi.cases_edge, ev = make_cases(problem, oracle_src, cleaned_edges, "edge", workdir=os.path.join(workdir, "ref_edge"), timeout_s=budget.step_timeout(60.0, reserve_s=20.0),
                                           per_case_s=limits.get("per_case_limit_s", 0.0), max_consec_timeouts=limits.get("max_consecutive_case_timeouts", 0))
            _log_and_note_validation(gi, ev, "edge", log)
        mx = run_python_cases(stress_src, "gen_max", [(1,)], workdir=os.path.join(workdir, "genmax"), timeout_s=budget.step_timeout(60.0, reserve_s=20.0))
        if mx and mx[0].ok:
            _accept_stress_input(problem, gi, oracle_src, mx[0].output, "gen_max", workdir=workdir, budget=budget)
        else:
            gi.notes.append("gen_max failed: " + (mx[0].error[-200:] if mx else ""))
    else:
        gi.notes.append("no stress source")
    # gen_max failed or was rejected in nine of ten round-10 runs, and it is the STRESS author's only
    # product: with it gone the timing check had nothing to run on. The oracle writes a generator from
    # the same statement, in its own context, so its "large" mode is a second, independent source of a
    # real maximum-size input -- tried before falling back to a medium case, which is not one.
    if gi.stress_input is None:
        lg = run_python_cases(oracle_src, "gen", [(1, "large")], workdir=os.path.join(workdir, "gen_large"), timeout_s=budget.step_timeout(60.0, reserve_s=20.0))
        if not (lg and lg[0].ok):
            gi.notes.append("gen(large) failed: " + ((lg[0].error[-200:] if lg else "") or "no output"))
        elif _accept_stress_input(problem, gi, oracle_src, lg[0].output, "gen_large", workdir=workdir, budget=budget):
            # An oracle whose gen() ignores its mode argument answers "large" with a small input, and
            # timing a candidate on a small input is not a timing check. To count as max-size it has
            # to be bigger than the tiers it is standing in for.
            biggest = max((len(str(c.input)) for c in gi.cases_medium + gi.cases_small), default=0)
            if not biggest or len(str(gi.stress_input)) <= biggest:   # no tier to compare against = no way to confirm it is large
                gi.stress_degraded = True
                gi.notes.append("stress input degraded: gen(large) returned an input no larger than the generated tiers (not a true max-size input)")
    if gi.stress_input is None and gi.cases_medium:
        # Neither generator produced a max-size input; rather than ship a correct-but-slow solution
        # with the timing check silently skipped, fall back to the largest medium case. "Largest" by
        # len(str(...)): generic across a Python argument tuple (str() of the tuple scales with its
        # total content) and a Rust stdin string (str() is the string itself), so the same one-line
        # measure works for both without knowing the problem's shape. This is NOT a true maximum-size
        # input -- medium is capped far below gen_max's target scale -- so it's marked degraded and
        # stress()'s evidence records that, so the timing it produces is never mistaken for a real
        # max-size check.
        biggest = max(gi.cases_medium, key=lambda c: len(str(c.input)))
        gi.stress_input = _clean_stdin(problem, biggest.input)
        gi.stress_degraded = True
        gi.stress_source = "medium_degraded"
        gi.notes.append("stress input degraded: no usable gen_max or gen(large) output, using largest medium case instead (not a true max-size input)")
    if gi.stress_input is not None:
        log(f"stress.input source={gi.stress_source} validity={gi.stress_validity or 'unvalidated'} size={_input_size(gi.stress_input)}")

def prepare_gate_inputs(problem, oracle_src: str, stress_src: str, limits: dict, *, workdir: str, budget, log, examples: dict | None = None) -> GateInputs:
    """All three phases in one call, for every caller that has the oracle, the stress source and the
    examples in hand at once (regenerate_oracle, and the tests)."""
    gi = prepare_oracle_tiers(problem, oracle_src, limits, workdir=workdir, budget=budget, log=log)
    check_oracle_examples(problem, gi, limits, workdir=workdir, budget=budget, log=log, examples=examples or {})
    prepare_stress_inputs(problem, gi, stress_src, limits, workdir=workdir, budget=budget, log=log)
    return gi

UNJUDGED_STRESS_REASON = ("max-size input could not be validated (reference did not finish); "
                          "timing is indicative only")

def stress(problem, source: str, stress_input, *, workdir: str, limit_s: float, mem_mb: int, degraded: bool = False, min_plausible_s: float = 0.0, unjudged: bool = False) -> Evidence:
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
    # "Too slow" and "crashed" are different failures and want different responses: a timing failure
    # cannot be patched (solve() sends it to a fresh solve), a crash can. `dur` is limit_s + 1 for a
    # Python run that died, so duration alone does not separate them -- record the distinction here,
    # where r.ok is still in hand.
    detail = {"duration_s": round(dur, 3), "limit_s": limit_s, "timed_out": r.timed_out,
              "too_slow": bool(r.timed_out or (r.ok and dur > limit_s)), "error": r.error[-400:]}
    if unjudged:
        # Nothing in the run could say whether this input is legal (see _accept_stress_input), so
        # neither direction of the measurement is evidence: a pass is not proof the candidate is fast
        # enough on a legal maximum, and a failure is not proof it is too slow on one. It is recorded
        # as a SKIPPED PASS either way -- that is what keeps it out of passed_all_gates while making
        # it impossible for it to fail a candidate, spend a repair, or trigger a fresh solve on an
        # input nothing can confirm. The measured numbers stay in the detail, as a note, not a verdict.
        detail["degraded"] = UNJUDGED_STRESS_REASON
    elif degraded:
        detail["degraded"] = "gen_max failed; this is the largest medium case, not a true max-size input -- this timing is not a real max-size stress check"
    # An answer that arrives faster than any real work could is not a timing measurement, whatever
    # the input weighed. On bench14 gen_max's first operation named an identifier the input never
    # created, so the candidate rejected the whole 476 KB input at operation 1 in 0.000 s and the
    # gate recorded that as evidence of speed -- for a solution that needs ~10^11 s at the stated
    # limits. Generic over every early-exit shape (first-invalid-index, validators, short-circuiting
    # searches): the step cannot be a pass, because nothing was actually exercised.
    suspicious = passed and not detail.get("degraded") and min_plausible_s > 0 and dur < min_plausible_s
    if suspicious:
        detail["suspicious"] = (f"finished in {dur:.3f} s on a {_input_size(stress_input)}-byte input; "
                                "the input may not exercise the candidate")
        detail["degraded"] = detail["suspicious"]
    # A degraded input is not a max-size input, so a clean run on it is not evidence that the
    # candidate is fast enough: record it as a SKIPPED step, which is what the finalizer and the
    # benchmark already treat as "not checked". A degraded run that was still too slow is kept as a
    # real failure -- too slow on a smaller-than-max input is only more damning.
    return Evidence("stress", passed or unjudged, 1, time.monotonic() - t0, detail,
                    skipped=unjudged or ((degraded or suspicious) and passed))

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

def behavior(problem, source: str, cases: list[Case], *, workdir: str, timeout_s: float, binary: str | None = None, mem_mb: int = 4096, per_case_s: float = 0.0) -> Evidence:
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
        aba = run_candidate(problem, source, [inputs[0], inputs[-1], inputs[0]], workdir=os.path.join(workdir, "aba"), timeout_s=case_timeout, mem_mb=mem_mb, per_case_s=per_case_s)
        if any(r.mutated for r in aba):
            return Evidence("behavior", False, 3, time.monotonic() - t0, {"check": "mutation", "input": inputs[0]})
        if aba[0].ok and aba[2].ok and aba[0].output != aba[2].output:
            return Evidence("behavior", False, 3, time.monotonic() - t0, {"check": "global_state", "input": inputs[0], "first": aba[0].output, "again": aba[2].output})
    rust_deadline = lambda: None if problem.language == "python" else time.monotonic() + timeout_s   # see differential()
    r1 = run_candidate(problem, source, inputs, workdir=os.path.join(workdir, "p1"), timeout_s=case_timeout, binary=binary, mem_mb=mem_mb, per_case_s=per_case_s, deadline_s=rust_deadline())
    r2 = run_candidate(problem, source, inputs, workdir=os.path.join(workdir, "p2"), timeout_s=case_timeout, binary=binary, mem_mb=mem_mb, per_case_s=per_case_s, deadline_s=rust_deadline())
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

def _gate_line(e: Evidence, extra: str = "") -> str:
    """One log line per gate step. A skipped step carries passed=True (nothing failed) and zero
    cases, which read in log.txt as a step that ran and passed -- so a run that checked nothing
    looked like a clean sweep. Say skipped, with the reason the Evidence already recorded."""
    if e.skipped:
        return f"gate.{e.kind} skipped=True reason={e.detail.get('skipped') or e.detail.get('degraded', 'unspecified')}"
    return f"gate.{e.kind} passed={e.passed}" + (f" {extra}" if extra else "")

def run_gate(problem, source: str, gi: GateInputs, *, workdir: str, budget, limits: dict, log, examples: list | None = None) -> list[Evidence]:
    ev: list[Evidence] = []
    binary = None
    if problem.language == "rust":
        t0 = time.monotonic(); binary, err, fixed = compile_rust_with_imports(source, overflow_checks=True, workdir=workdir, timeout_s=budget.step_timeout(90.0))
        detail = {"stderr": err[-1500:]}
        if fixed != source:
            # rustc named the missing `use` and the retry compiled: every later step -- stress's
            # unchecked rebuild included -- and the emitted solution must be this patched source, so
            # it travels back to solve() in the evidence.
            detail["patched_source"] = fixed
            source = fixed
        ev.append(Evidence("compile", binary is not None, 0, time.monotonic() - t0, detail))
        if binary is None: return ev
    else:
        e = python_import_check(source, problem.entrypoint, gi, workdir=os.path.join(workdir, "compile"), timeout_s=budget.step_timeout(30.0, reserve_s=20.0), mem_mb=limits["mem_mb"])
        ev.append(e); log(_gate_line(e))
        if not e.passed: return ev
    # Past compile, every step the inputs allow runs, and the list is complete rather than truncated
    # at the first failure. Three reasons (round-10 L2): the timing steps need no oracle, so a
    # differential failure must not suppress them -- seven of ten round-10 solutions were
    # asymptotically wrong and none was ever timed; a full mismatch count across tiers is what
    # candidate_score ranks on; and solve() still repairs the FIRST failed step in this canonical
    # order, so a correctness failure is still repaired before a timing one. Cost is bounded: each
    # differential tier is seconds, the expensive tier (medium) keeps its own budget rule, and every
    # step turns itself into `skipped: no budget` once step_timeout returns 0.
    #
    # The candidate against its own author's hand trace: no oracle involved, so it is the one
    # differential step that cannot be poisoned by a wrong reference. Skipped when this candidate
    # has no examples (an older candidate, a reply with no ===EXAMPLES=== block).
    e = differential(problem, source, examples or [], "diff_examples", workdir=os.path.join(workdir, "diff_examples"),
                     timeout_s=budget.step_timeout(60.0, reserve_s=20.0), binary=binary, mem_mb=limits["mem_mb"],
                     per_case_s=limits.get("per_case_limit_s", 0.0), max_consec_timeouts=limits.get("max_consecutive_case_timeouts", 0))
    ev.append(e); log(_gate_line(e, f"cases={e.cases}"))
    # Public examples (ground truth from the problem itself, not the model-written oracle) run first,
    # before any generated tier -- a candidate that fails real ground truth fails fast on the most
    # trustworthy evidence available. Absent entirely (the common case), this step is not added at
    # all, so behavior is exactly what it was before this existed.
    if gi.cases_public:
        e = differential(problem, source, gi.cases_public, "diff_public", workdir=os.path.join(workdir, "diff_public"), timeout_s=budget.step_timeout(60.0, reserve_s=20.0), binary=binary, mem_mb=limits["mem_mb"],
                         per_case_s=limits.get("per_case_limit_s", 0.0), max_consec_timeouts=limits.get("max_consecutive_case_timeouts", 0))
        if gi.public_disagreements:
            e.detail = {**e.detail, "oracle_disagreements": gi.public_disagreements}
        if gi.diff_degraded:
            e.detail["degraded"] = gi.diff_degraded
        ev.append(e); log(_gate_line(e, f"cases={e.cases}"))
    for kind, cases in (("diff_edge", gi.regressions + gi.cases_edge), ("diff_small", gi.cases_small), ("diff_medium", gi.cases_medium)):
        e = differential(problem, source, cases, kind, workdir=os.path.join(workdir, kind), timeout_s=budget.step_timeout(60.0, reserve_s=20.0), binary=binary, mem_mb=limits["mem_mb"],
                         per_case_s=limits.get("per_case_limit_s", 0.0), max_consec_timeouts=limits.get("max_consecutive_case_timeouts", 0))
        # A generated tier that agreed on a handful of cases is thin evidence, not coverage: the
        # oracle's gen() mostly crashed or its validate() rejected most of what it produced. Mark it
        # degraded (solve() then demotes the status); an empty tier is already `skipped`.
        m = 0 if e.skipped else limits.get(f"min_cases_{kind[5:]}", 0)
        if e.passed and e.cases < m:
            e.detail["degraded"] = f"only {e.cases} {kind[5:]} cases (min {m})"
        if gi.diff_degraded:
            e.detail["degraded"] = gi.diff_degraded
        ev.append(e); log(_gate_line(e, f"cases={e.cases}"))
    e = behavior(problem, source, gi.cases_small or gi.cases_edge or gi.cases_public or gi.cases_medium,
                 workdir=os.path.join(workdir, "behavior"), timeout_s=budget.step_timeout(60.0, reserve_s=20.0), binary=binary, mem_mb=limits["mem_mb"], per_case_s=limits.get("per_case_limit_s", 0.0))
    ev.append(e); log(_gate_line(e, f"detail={e.detail.get('check','')}"))
    limit = limits["stress_limit_python_s"] if problem.language == "python" else limits["stress_limit_rust_s"]
    # A hung Python stress call can itself run up to limit_s + 15s (see stress() above), so reserve
    # enough margin that it can't eat the whole safety margin; Rust's call is bounded by limit_s exactly.
    reserve = limit + 20.0 if problem.language == "python" else 15.0
    avail = budget.step_timeout(limit, reserve_s=reserve)
    if avail <= 0:
        e = Evidence("stress", True, 0, 0.0, {"skipped": "no budget"}, skipped=True)
    else:
        e = stress(problem, source, gi.stress_input, workdir=os.path.join(workdir, "stress"), limit_s=min(limit, avail), mem_mb=limits["mem_mb"], degraded=gi.stress_degraded,
                   min_plausible_s=limits.get("stress_min_plausible_s", 0.0), unjudged=gi.stress_validity == "unjudged")
    ev.append(e); log(_gate_line(e, f"duration={e.detail.get('duration_s')}"))
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
    ev.append(eo); log(_gate_line(eo))
    return ev

def _fmt(v, limit: int = 4000) -> str:
    s = repr(v) if not isinstance(v, str) else v
    return s if len(s) <= limit else s[:limit] + " ...[truncated]"

def _fmt_typed(v, limit: int = 4000) -> str:
    # repr() alone can look identical for two differently-shaped failures once truncated or eyeballed
    # (an empty tuple candidate vs an empty list oracle both read short); naming the type explicitly
    # next to the value removes any doubt for the repair model. Display only -- same() is untouched.
    return f"{_fmt(v, limit)}  (type: {type(v).__name__})"

_SHAPE_NODE_CAP = 200_000   # ponytail: bounded scan, not a sample -- raise it if a real input is ever bigger

def _shape_stats(v) -> tuple[int, int | None]:
    """(nesting depth, largest integer magnitude) of one value, by explicit stack: a max-size input
    can be deeper than Python's recursion limit. Bounded by _SHAPE_NODE_CAP nodes."""
    depth, biggest, seen = 0, None, 0
    stack = [(v, 1)]
    while stack and seen < _SHAPE_NODE_CAP:
        cur, d = stack.pop(); seen += 1
        depth = max(depth, d)
        if isinstance(cur, bool):
            continue
        if isinstance(cur, int):
            biggest = abs(cur) if biggest is None else max(biggest, abs(cur))
        elif isinstance(cur, dict):
            for k, val in cur.items(): stack.append((k, d + 1)); stack.append((val, d + 1))
        elif isinstance(cur, (list, tuple, set, frozenset)):
            for e in cur: stack.append((e, d + 1))
    return depth, biggest

def _op_histogram(v) -> list:
    """How often each operation kind appears, when the argument is a list of records whose first
    element names the operation -- the shape every operation-sequence statement in the samples uses.
    Empty for anything else."""
    if not isinstance(v, (list, tuple)) or not v:
        return []
    if not all(isinstance(e, (tuple, list)) and e and isinstance(e[0], str) for e in v[:50]):
        return []
    c = collections.Counter(e[0] for e in v if isinstance(e, (tuple, list)) and e and isinstance(e[0], str))
    return c.most_common(10)

def describe_input(value, limit: int = 1500) -> str:
    """A description of an input's SHAPE, for a prompt that must not carry the input itself -- the
    max-size stress input is megabytes, and pasting it would crowd out the statement. Generic over
    nested tuples/lists/dicts/strings/ints: per argument, its type, its length, how deeply it nests,
    the largest integer magnitude anywhere inside it, and the operation mix when it is a list of
    records. That is what tells a model which quantity its next algorithm has to be cheap in."""
    args = value if isinstance(value, tuple) else (value,)
    lines = []
    for i, a in enumerate(args):
        parts = [f"type {type(a).__name__}"]
        if isinstance(a, (str, bytes, list, tuple, dict, set, frozenset)):
            parts.append(f"length {len(a)}")
        if isinstance(a, str):
            parts.append(f"{len(a.split())} whitespace-separated tokens")
        d, biggest = _shape_stats(a)
        if d > 1:
            parts.append(f"nesting depth {d}")
        if biggest is not None:
            parts.append(f"largest integer magnitude {biggest}")
        hist = _op_histogram(a)
        if hist:
            parts.append("operation kinds " + ", ".join(f"{k}x{n}" for k, n in hist))
        lines.append(f"argument {i + 1}: " + "; ".join(parts))
    return _fmt("\n".join(lines), limit)

def expected_source(kind: str) -> str:
    """Where the `expected` value of a differential failure came from. Named in every prompt that
    shows one, because how much the model should trust it differs by source."""
    if kind == "diff_examples":
        return "from the solution author's own hand trace of the statement, which may itself be wrong"
    if kind == "diff_public":
        return "from a public example shipped with the problem statement: this one is ground truth"
    return "from an independent literal reference, which may itself be wrong"

def previous_attempt_section(problem, cand, failed: Evidence, gi: GateInputs) -> str:
    """The `{{previous_attempt}}` section of a fresh SOLVE prompt: what the last attempt did, and
    the concrete failure that ended it. A timing failure deliberately carries the input's SHAPE
    rather than the input (megabytes of it), plus the measured duration against the limit."""
    algorithm = (getattr(cand, "algorithm", "") or "").strip() or "(the previous reply recorded no algorithm block)"
    head = ("A previous solution to this statement failed. Its algorithm and code follow, then the failure.\n"
            "Choose a DIFFERENT algorithm or data structure that removes the cause named below; do not "
            "resubmit a variant of this approach.\n\n"
            f"Previous algorithm:\n{_fmt(algorithm, 3000)}\n\n"
            f"Previous code:\n{_fmt(cand.source, 6000)}\n\n")
    if failed.kind == "stress":
        d = failed.detail
        return (head + "Failure: it was too slow on a maximum-size input. The input itself is too large to show; "
                "this is its shape:\n"
                f"{describe_input(gi.stress_input)}\n"
                f"measured duration {d.get('duration_s')} s vs limit {d.get('limit_s')} s\n"
                f"timed out: {'yes' if d.get('timed_out') else 'no'}\n"
                "A smaller edit cannot fix this: the complexity of the approach is what has to change.\n")
    return (head + f"Failure: it disagreed with a checked expected value ({failed.kind}).\n"
            f"Input:\n{_fmt_typed(failed.detail.get('input'))}\n"
            f"Expected ({expected_source(failed.kind)}):\n{_fmt_typed(failed.detail.get('expected'))}\n"
            f"Actual:\n{_fmt_typed(failed.detail.get('actual'))}\n")

def _previous_attempt_note(run, cand, failed: Evidence) -> str:
    """If `cand` is itself the result of an earlier repair, describe that attempt: the input it was
    given, what it changed, and that the candidate above still fails -- so the model doesn't repeat a
    change that already didn't work. Empty string when there is no earlier repair (first attempt)."""
    if not cand.parent:
        child = next((c for c in run.cands if c.parent == cand.id), None)
        if child is None:
            return ""
        diff = "\n".join(difflib.unified_diff(cand.source.splitlines(), child.source.splitlines(), lineterm="", n=1))
        return ("\nA previous repair already ran from this candidate and made this change:\n"
                f"{_fmt(diff, 1500)}\nThat change did NOT fix the case. Do not repeat it.\n")
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

def _agreement_note(run, cand, failed: Evidence, gi: GateInputs, detail: dict) -> str:
    """How wholesale the disagreement is, in one phrase for the repair prompt. One failing input
    reads the same whether the candidate is off by one at a boundary or has read the whole statement
    differently -- and those want opposite responses from the repair model. The failing tier's own
    numbers answer it when the tier is large enough; an edge/public failure (often a single case) is
    measured against cases_small instead, reusing the compiled binary in the candidate's gate
    workdir so a Rust candidate is not recompiled."""
    if detail.get("cases") and failed.kind in ("diff_small", "diff_medium"):
        return f"disagrees with the reference on {detail.get('mismatches', 0)} of {detail['cases']} {failed.kind[5:]} inputs"
    if failed.kind in ("diff_edge", "diff_public", "diff_examples") and gi.cases_small:
        e = differential(run.p, cand.source, gi.cases_small, "agreement", workdir=os.path.join(run.dir, cand.id),
                         timeout_s=run.budget.step_timeout(30.0, reserve_s=20.0), mem_mb=run.cfg["limits"]["mem_mb"],
                         per_case_s=run.cfg["limits"].get("per_case_limit_s", 0.0))
        if e.cases:
            return f"disagrees with the reference on {e.detail.get('mismatches', 0)} of {e.cases} small inputs"
    return "was not measured against a whole tier of inputs"

def repair_cap(run) -> float:
    """The wall cap for one repair or fresh-solve call: the larger of the phase fraction
    ([phases] repair_call_share, which is tuned to the DEADLINE) and the strong role's own
    repair_cap_s (which is tuned to the MODEL). Both are needed: the fraction alone gave bench16 a
    67 s cap against a role whose completed solve that run took 168 s, so the repair could only time
    out; a fixed floor alone would ignore a shorter deadline. solve() uses the same number to decide
    whether a repair can be afforded at all, so the call is never started against a cap the budget
    cannot cover."""
    roles = getattr(run.llm, "roles", None) or {}
    floor = float(getattr(roles.get("strong"), "repair_cap_s", 0.0) or 0.0)
    return max(run.cfg["phases"]["repair_call_share"] * run.budget.usable_s, floor)

def repair(run, cand, failed: Evidence, gi: GateInputs, pv: dict) -> tuple[str, str]:
    problem = run.p
    previous_attempt = _previous_attempt_note(run, cand, failed)
    detail = dict(failed.detail)
    # diff_examples is excluded: its expected value is the author's hand trace, and shrink re-derives
    # the expectation from the ORACLE, which would silently swap one ground truth for the other.
    if failed.kind.startswith("diff_") and failed.kind != "diff_examples" and "input" in detail:
        case = Case(detail["input"], detail["expected"], failed.kind)
        if problem.language == "python" and gi.oracle_src:
            case = shrink(problem, cand.source, gi.oracle_src, case, workdir=os.path.join(run.dir, cand.id, "shrink"),
                          budget_s=run.cfg["limits"]["shrink_budget_s"], validate_trusted=gi.validate_trusted)
            run.log(f"shrink input={_fmt(case.input)[:120]}")
        detail["input"], detail["expected"] = case.input, case.expected
        gi.regressions.append(case)
    if failed.kind == "stress" and detail.get("error") and not detail.get("timed_out") and gi.stress_input is not None:
        # A stress crash is about the input's format and scale, and the model sees neither: the
        # stress input is never in the evidence (it is enormous). Show the head of it so the crash
        # has a shape to be explained by. Timeouts are left alone -- they are about cost, not shape.
        si = gi.stress_input if isinstance(gi.stress_input, str) else repr(gi.stress_input)
        detail["input"] = si[:1500] + (f"\n(max-size input, {len(si)} characters total; truncated)" if len(si) > 1500 else "")
    agreement = _agreement_note(run, cand, failed, gi, detail)
    pv_repair = {**pv, "statement": _fmt(pv["statement"], 12000), "previous_attempt": previous_attempt}
    # Derived from config.toml's [phases].repair_call_share, not a literal fraction -- see
    # regenerate_oracle's identical use of generate_call_share, and BENCHMARK-FINDINGS.md F6 for
    # what goes wrong when a phase cap is hardcoded instead (raising the config knob then does
    # nothing because the hardcoded fraction still wins the min() in Run.chat/step_timeout).
    cap = repair_cap(run)
    r = run.chat("strong", "repair", cap, kind=failed.kind, expected_source=expected_source(failed.kind),
                 input=_fmt_typed(detail.get("input")), expected=_fmt_typed(detail.get("expected")),
                 actual=_fmt_typed(detail.get("actual")), details=_fmt({k: v for k, v in detail.items() if k not in ("input", "expected", "actual")}),
                 code=_fmt(cand.source), agreement=agreement,
                 reference=_fmt(gi.oracle_src, 8000) if gi.oracle_src.strip() else "(no reference available)", **pv_repair)
    blocks = parse_blocks(r.text)
    # A streamed reply the cap cut off still holds the patch when its ===CODE=== block closed --
    # repair.md puts ===VERDICT=== before it, so both blocks survive the cut. Only the timeout is
    # salvaged this way; a transport error or a 4xx has no text at all.
    salvaged = salvageable(r)
    if salvaged:
        run.salvaged_partial = True
        run.log("repair.partial the reply was cut off after ===CODE=== closed; using it")
    # A call that never produced an answer -- a transport error, a 4xx/5xx, a timeout with nothing
    # complete in it, a reply with no marker block -- adjudicated nothing, and must not be read as
    # one. It used to fall through to the default verdict `candidate`, which says the reference is
    # right and the candidate is wrong: on bench16 a 403 was recorded that way on a run whose oracle
    # was in fact the wrong side. There is no verdict here, and no child to mint.
    if (r.error and not salvaged) or not blocks:
        run.log(f"repair.error {r.error or 'no marker block in the reply'}")
        return "", "error"
    # "approach": the model says no patch of this code can meet the stated limits. Its CODE block is
    # then the current code unchanged (the prompt asks for that), so solve() starts a fresh solution
    # from a different algorithm instead of minting a child that changes nothing.
    said = blocks.get("VERDICT", "").strip().lower()
    verdict = next((v for v in ("oracle", "approach") if said.startswith(v)), "candidate")
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
    if run.over_cost():
        # This is an optional extra model call like any other; regen_failed is what makes the
        # repair loop stop cleanly instead of re-gating against an oracle that was never rewritten.
        run.log(f"oracle.{label} skipped: cost cap reached")
        gi.notes.append(f"oracle {label} skipped: cost cap reached")
        gi.regen_failed = True
        return gi
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
    examples = {c.id: getattr(c, "examples", []) for c in run.cands if getattr(c, "examples", None)}
    new = prepare_gate_inputs(run.p, src, "", run.cfg["limits"], workdir=os.path.join(run.dir, workdir_tag), budget=run.budget, log=run.log, examples=examples)
    new.cases_edge, new.regressions = gi.cases_edge, []
    # Keep the stress input the run already had -- it came from the independent STRESS call and the
    # statement has not changed. The exception is a degraded one: the replacement oracle's own
    # gen(large) is a real max-size input and beats the largest medium case. `stress_degraded` used
    # to be left behind here, which silently turned a degraded timing run back into a full pass.
    if gi.stress_input is not None and (not gi.stress_degraded or new.stress_input is None):
        new.stress_input, new.stress_degraded, new.stress_source = gi.stress_input, gi.stress_degraded, gi.stress_source
        new.stress_validity = gi.stress_validity
    new.oracle_regens, new.oracle_selfrepairs = gi.oracle_regens, gi.oracle_selfrepairs
    setattr(new, counter, getattr(new, counter) + 1)
    new.regen_failed = False
    # re-derive edge expectations with the new reference
    if gi.cases_edge:
        new.cases_edge, _ = make_cases(run.p, new.oracle_src, [c.input for c in gi.cases_edge], "edge", workdir=os.path.join(run.dir, workdir_tag, "edge"), timeout_s=run.budget.step_timeout(30.0))
    # Cross-check the new reference against the one it replaces on the OLD small inputs. Two
    # references written by the same model from the same prose that disagree on most tiny inputs
    # cannot both be near-correct, so the run has no ground truth left: the differential evidence the
    # new oracle produces is marked degraded (solve() then demotes the status out of
    # passed_all_gates). Control flow is otherwise unchanged -- the new oracle is still used.
    if gi.cases_small:
        res = run_python_cases(new.oracle_src, "reference", [_ref_args(run.p, c.input) for c in gi.cases_small],
                               workdir=os.path.join(run.dir, workdir_tag, "crosscheck"), timeout_s=run.budget.step_timeout(30.0, reserve_s=20.0))
        n = len(gi.cases_small)
        k = sum(1 for c, r in zip(gi.cases_small, res) if not same(run.p, r, c.expected))
        run.log(f"oracle.{label} disagreement={k}/{n}")
        new.notes.append(f"regenerated oracle disagrees with the one it replaces on {k}/{n} small inputs")
        if k / n > run.cfg["limits"].get("oracle_regen_max_disagreement", 0.5):
            # "Neither reference can be trusted" holds only while nothing outside the two can say
            # which is right. Hand-traced examples can: if the old reference contradicted half of
            # them and the new one contradicts none, the wholesale disagreement is the old one's
            # error being corrected, not two references of unknown quality.
            if examples_disputed(gi) and new.example_checks and not (new.example_checks.get("disagreements") or []):
                run.log(f"oracle.{label} disagreement resolved by the hand-traced examples")
                new.notes.append("the replacement agrees with every hand-traced example and the one it replaces did not, "
                                 "so their disagreement is not evidence that both are untrustworthy")
            else:
                new.diff_degraded = (f"the oracle was regenerated and disagrees with the one it replaces on {k}/{n} small inputs; "
                                     "neither reference can be trusted as ground truth")
    run.log(f"oracle.{label} small={len(new.cases_small)} medium={len(new.cases_medium)} edge={len(new.cases_edge)}")
    return new
