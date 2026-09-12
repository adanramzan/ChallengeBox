# ChallengeBox Solver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A runnable tool, `python solve.py <problem.json> -o <solution>`, that generates, verifies, repairs, and emits a Python or Rust solution before the problem's deadline, using any OpenAI-compatible model endpoint chosen purely by config.

**Architecture:** One SOLVE call produces analysis and code; an independent ORACLE call produces a literal Python reference plus input generator; a STRESS call produces a max-size input generator. A deterministic, token-free gate (static contract, compile, differential vs oracle, behavior checks, max-size timing) decides pass/fail. A bounded repair loop fixes demonstrated failures. A monotonic budget controller turns the deadline into per-step timeouts and forces emission before time runs out.

**Tech Stack:** Python 3.11 via `uv` (stdlib only at runtime: `urllib`, `tomllib`, `subprocess`, `ast`, `resource`, `threading`), `pytest` as the only dev dependency, `rustc` for Rust candidates, OpenRouter as the model endpoint (OpenAI-compatible; any other OpenAI-compatible endpoint can be added as a config profile later without code changes).

**Spec:** `/Users/adanramzan/Documents/Projects/ChallengeBox/ChallengeBox-Agent-Architecture.md` (the architecture) and `/Users/adanramzan/Documents/Projects/ChallengeBox/README.md` (the assessment). Sample problems: `samples/*.json`.

## Context

The README scores each problem 0/1: on time, all hidden tests pass, contract obeyed. All ten samples have no public examples and a 300 s deadline. The architecture doc (already rewritten) specifies the pipeline; this plan turns it into code.

Measured environment facts that shaped this plan (do not re-measure):

- macOS on Apple M1 Max, 64 GB. `uv` is installed with CPython 3.11.13 at `~/.local/bin/python3.11`. System `python3` is 3.9 and must not be used. `rustc` is **not installed** (Task 1 installs rustup).
- Local LM Studio models were measured and rejected: 7–16 tok/s, ~20 s prefill per 2k-token prompt, concurrent requests refused, and the 27B thinking model consumed its whole token budget on reasoning. At that speed a 300 s deadline fits two calls. The user decided: **OpenRouter only, cheap models per role.**
- OpenRouter (from live docs and the public `/api/v1/models` list on 2026-09-12): endpoint `https://openrouter.ai/api/v1/chat/completions`, header `Authorization: Bearer $OPENROUTER_API_KEY`; every response carries `usage.cost` (USD) and `usage.completion_tokens_details.reasoning_tokens` with no extra request flag; a unified request field `"reasoning": {"effort": "low|medium|high"}` caps thinking across providers; reasoning text comes back in `message.reasoning` (some providers use `reasoning_content`, the client reads both).
- JSON-schema `response_format` is not used (measured as unreliable with thinking models: constrained output landed in the reasoning field). Prompts use `===NAME===` … `===END===` marker blocks and a lenient parser that also scans reasoning text when content is empty.

Model choice (config only, swap any time). Prices are USD per million tokens from the live list:

| Role | Model | In / Out | Why |
|---|---|---|---|
| `strong` (SOLVE, REPAIR) | `deepseek/deepseek-v4.1-flash` | 0.15 / 0.60 | reasoning model, strong at code, 1M context, cheapest tier that still reasons well |
| `fast` (ORACLE, STRESS) | `qwen/qwen3.7-flash` | 0.03 / 0.13 | different model family from the candidate (independent misreadings), reasoning-capable, near-free |

Worst-case per problem at the architecture's token budget (about 20k in / 40k out on `strong` including reasoning, 6k in / 8k out on `fast`) is roughly $0.03; ten problems under $0.40. A hard cap `max_cost_usd_per_problem = 0.10` stops repairs when exceeded and is recorded in the report.

User decisions already made: OpenRouter only; pure-config roles (`strong`, `fast`) under `[profiles.openrouter]`; the deadline is always enforced (`--deadline-scale` remains as a development convenience, default 1.0, recorded in the report); rustup install is the first plan step.

## Global Constraints

- Orchestrator runtime dependencies: **none** beyond the Python 3.11 standard library. `pytest` is the only dev dependency.
- Python interpreter for orchestrator, harnesses, and generated Python candidates: the `uv`-managed 3.11 (`uv run`), never `/usr/bin/python3`.
- Layout is fixed: `solve.py`, `llm.py`, `sandbox.py`, `verify.py`, `prompts/{solve,oracle,stress,repair}.md`, `config.toml`, `pyproject.toml`, `tests/`, `samples/` (given), `runs/` (gitignored).
- Generated code never runs in the orchestrator process. Every execution goes through `sandbox.run_cmd` with `start_new_session=True`, an empty environment plus `PATH`, a wall-clock timeout, process-group kill, and capped output.
- Model output is never trusted as code without passing `sandbox.python_static` / `sandbox.rust_static`.
- Deadline: `Budget` uses `time.monotonic()`; every model call and subprocess gets `budget.step_timeout(cap)`. Emission happens before `deadline_s * scale - safety_margin_s`.
- Rust compile flags: gate build `rustc --edition 2021 -O -C overflow-checks=on`; stress build `-C overflow-checks=off`. Output compared as `str.split()` token lists (Python's `split()` with no argument splits on exactly the judge's ASCII whitespace set).
- Commit after every task with a conventional message. Never commit `runs/` or API keys.

---

### Task 1: Toolchain, project skeleton, config

**Files:**
- Create: `pyproject.toml`, `config.toml`, `.gitignore`, `prompts/.gitkeep` (removed in Task 6), `tests/__init__.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `config.toml` schema consumed by `llm.load_config` (Task 5): `[limits]`, `[profiles.<name>.<role>]` with keys `base_url`, `model`, `api_key_env`, `max_tokens`, `max_concurrent`, `timeout_cap_s`, `extra` (table).

- [ ] **Step 1: Install rustup (network install, approve when prompted)**

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal
source "$HOME/.cargo/env"
rustc --version
```
Expected: `rustc 1.x.y (...)`. If `rustc` is still not found in later shells, add `source "$HOME/.cargo/env"` to `~/.zshrc`.

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "challengebox"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = []

[dependency-groups]
dev = ["pytest>=8"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 3: Write `.gitignore`**

```
runs/
.venv/
__pycache__/
*.pyc
.pytest_cache/
```

- [ ] **Step 4: Write `config.toml`**

```toml
[limits]
safety_margin_s = 15.0
cases_small = 200
cases_medium = 20
stress_limit_python_s = 5.0
stress_limit_rust_s = 2.0
max_repairs = 2
mem_mb = 4096
shrink_budget_s = 3.0
max_cost_usd_per_problem = 0.10

# Phase boundaries as fractions of usable time (deadline*scale - margin)
[phases]
generate_until = 0.32
gate_until = 0.39
repair_until = 0.81
settle_until = 0.93

# Any OpenAI-compatible endpoint can be added as another [profiles.<name>.<role>] table.
[profiles.openrouter.strong]
base_url = "https://openrouter.ai/api/v1"
model = "deepseek/deepseek-v4.1-flash"
api_key_env = "OPENROUTER_API_KEY"
max_tokens = 16000
max_concurrent = 3
timeout_cap_s = 120.0
extra = { reasoning = { effort = "medium" } }

[profiles.openrouter.fast]
base_url = "https://openrouter.ai/api/v1"
model = "qwen/qwen3.7-flash"
api_key_env = "OPENROUTER_API_KEY"
max_tokens = 6000
max_concurrent = 3
timeout_cap_s = 60.0
extra = { reasoning = { effort = "low" } }
```
`max_tokens` includes reasoning tokens on OpenRouter, which is why `strong` gets 16k. The `extra` table is merged verbatim into the request body, so per-model knobs never need code changes.

- [ ] **Step 5: Write the failing config test**

`tests/test_config.py`:
```python
import tomllib, pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

def test_config_has_openrouter_roles_and_cost_cap():
    cfg = tomllib.loads((ROOT / "config.toml").read_text())
    for role in ("strong", "fast"):
        r = cfg["profiles"]["openrouter"][role]
        assert r["base_url"] == "https://openrouter.ai/api/v1"
        assert r["api_key_env"] == "OPENROUTER_API_KEY"
        assert r["max_concurrent"] >= 1 and r["max_tokens"] > 0
        assert "reasoning" in r["extra"]
    assert cfg["profiles"]["openrouter"]["strong"]["model"] != cfg["profiles"]["openrouter"]["fast"]["model"]
    assert 0 < cfg["limits"]["max_cost_usd_per_problem"] <= 1.0
    assert cfg["limits"]["safety_margin_s"] > 0
    assert cfg["phases"]["generate_until"] < cfg["phases"]["repair_until"] < cfg["phases"]["settle_until"] < 1
```

- [ ] **Step 6: Run tests**

Run: `uv run --group dev pytest -q`
Expected: `1 passed`

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml config.toml .gitignore tests/
git commit -m "chore: project skeleton, config profiles, rust toolchain notes"
```

---

### Task 2: Sandbox core — `run_cmd`, Python harness, Python static checks

**Files:**
- Create: `sandbox.py`
- Test: `tests/test_sandbox.py`

**Interfaces:**
- Produces:
  - `Problem` dataclass: `problem_id: str, language: str, statement: str, entrypoint: str, public_examples: list, deadline_s: float`; `Problem.load(path: str) -> Problem` raises `ValueError` on bad input.
  - `RunResult` dataclass: `returncode: int, stdout: bytes, stderr: bytes, duration_s: float, timed_out: bool`.
  - `run_cmd(argv: list[str], *, stdin: bytes = b"", timeout_s: float, cwd: str | None = None, mem_mb: int = 4096, max_output: int = 1_000_000) -> RunResult`.
  - `CaseResult` dataclass: `ok: bool, output: object, error: str, duration_s: float, mutated: bool, timed_out: bool`.
  - `run_python_cases(source: str, entrypoint: str, args_list: list[tuple], *, timeout_s: float, workdir: str) -> list[CaseResult]` — always returns exactly `len(args_list)` results.
  - `python_static(source: str, entrypoint: str) -> list[str]` — empty list means OK.
  - `PYTHON` constant: `sys.executable` (the uv-managed 3.11).

- [ ] **Step 1: Write failing tests**

`tests/test_sandbox.py`:
```python
import os, sys, time, textwrap, pytest
import sandbox
from sandbox import run_cmd, run_python_cases, python_static, Problem

def test_run_cmd_captures_output_and_exit(tmp_path):
    r = run_cmd([sys.executable, "-c", "import sys; print('hi'); sys.stderr.write('e'); sys.exit(3)"], timeout_s=5, cwd=str(tmp_path))
    assert r.returncode == 3 and r.stdout == b"hi\n" and r.stderr == b"e" and not r.timed_out

def test_run_cmd_kills_process_group_on_timeout(tmp_path):
    code = "import subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); print(p.pid, flush=True); time.sleep(30)"
    t0 = time.monotonic()
    r = run_cmd([sys.executable, "-c", code], timeout_s=1.0, cwd=str(tmp_path))
    assert r.timed_out and time.monotonic() - t0 < 5
    child_pid = int(r.stdout.split()[0])
    time.sleep(0.3)
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)  # grandchild must be dead too

def test_run_cmd_caps_output(tmp_path):
    r = run_cmd([sys.executable, "-c", "print('x'*5_000_000)"], timeout_s=10, cwd=str(tmp_path), max_output=1000)
    assert len(r.stdout) <= 1000

def test_run_cmd_env_is_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "leak")
    r = run_cmd([sys.executable, "-c", "import os; print(os.environ.get('SECRET_KEY','none'))"], timeout_s=5, cwd=str(tmp_path))
    assert r.stdout.strip() == b"none"

def test_run_python_cases_basic(tmp_path):
    src = "def add(a, b):\n    return a + b\n"
    res = run_python_cases(src, "add", [(1, 2), (3, 4)], timeout_s=10, workdir=str(tmp_path))
    assert [r.output for r in res] == [3, 7] and all(r.ok and not r.mutated for r in res)

def test_run_python_cases_detects_mutation_and_exception(tmp_path):
    src = "def f(xs):\n    xs.append(1)\n    return len(xs)\ndef g(x):\n    raise ValueError('boom')\n"
    res = run_python_cases(src, "f", [([1],)], timeout_s=10, workdir=str(tmp_path))
    assert res[0].ok and res[0].mutated
    res = run_python_cases(src, "g", [(1,)], timeout_s=10, workdir=str(tmp_path))
    assert not res[0].ok and "ValueError" in res[0].error

def test_run_python_cases_timeout_marks_remaining(tmp_path):
    src = "import time\ndef f(x):\n    if x: time.sleep(30)\n    return x\n"
    res = run_python_cases(src, "f", [(0,), (1,), (0,)], timeout_s=1.0, workdir=str(tmp_path))
    assert res[0].ok and res[0].output == 0
    assert res[1].timed_out and res[2].timed_out and len(res) == 3

def test_run_python_cases_preserves_tuples_and_none(tmp_path):
    src = "def f(t):\n    return (t, None, {'k': (1, 2)})\n"
    res = run_python_cases(src, "f", [(("a", 1),)], timeout_s=10, workdir=str(tmp_path))
    assert res[0].output == (("a", 1), None, {"k": (1, 2)})

def test_python_static_rules():
    assert python_static("def solve(x):\n    return x\n", "solve") == []
    assert any("entrypoint" in v for v in python_static("def other(x):\n    return x\n", "solve"))
    assert any("import" in v for v in python_static("import numpy\ndef solve(x):\n    return x\n", "solve"))
    assert any("open" in v for v in python_static("def solve(x):\n    return open('f')\n", "solve"))
    assert any("print" in v for v in python_static("def solve(x):\n    print(x)\n", "solve"))
    assert any("syntax" in v for v in python_static("def solve(x:\n", "solve"))

def test_problem_load_validates(tmp_path):
    p = tmp_path / "p.json"
    p.write_text('{"problem_id":"a","language":"python","statement":"s","entrypoint":"f","public_examples":[],"deadline_s":300.0}')
    assert Problem.load(str(p)).entrypoint == "f"
    p.write_text('{"problem_id":"a","language":"go","statement":"s","entrypoint":"f","public_examples":[],"deadline_s":300.0}')
    with pytest.raises(ValueError):
        Problem.load(str(p))
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --group dev pytest tests/test_sandbox.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'sandbox'`

- [ ] **Step 3: Implement `sandbox.py` (Python half)**

```python
"""Untrusted-code execution: process isolation, harnesses, static contract checks."""
from __future__ import annotations
import ast, json, os, resource, signal, subprocess, sys, time
from dataclasses import dataclass, field

PYTHON = sys.executable

@dataclass
class Problem:
    problem_id: str
    language: str
    statement: str
    entrypoint: str
    public_examples: list
    deadline_s: float

    @staticmethod
    def load(path: str) -> "Problem":
        with open(path, "rb") as f:
            d = json.load(f)
        for k in ("problem_id", "language", "statement", "entrypoint", "deadline_s"):
            if k not in d:
                raise ValueError(f"missing field {k}")
        if d["language"] not in ("python", "rust"):
            raise ValueError(f"unsupported language {d['language']}")
        if not isinstance(d["deadline_s"], (int, float)) or d["deadline_s"] <= 0:
            raise ValueError("deadline_s must be positive")
        return Problem(d["problem_id"], d["language"], d["statement"], d["entrypoint"],
                       list(d.get("public_examples") or []), float(d["deadline_s"]))

@dataclass
class RunResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    duration_s: float
    timed_out: bool

def _limits(mem_mb: int):
    def fn():
        try:
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
            # RLIMIT_AS is unreliable on macOS; set RLIMIT_DATA where possible and ignore failure.
            resource.setrlimit(resource.RLIMIT_DATA, (mem_mb << 20, mem_mb << 20))
        except (ValueError, OSError):
            pass
    return fn

def run_cmd(argv: list[str], *, stdin: bytes = b"", timeout_s: float, cwd: str | None = None,
            mem_mb: int = 4096, max_output: int = 1_000_000) -> RunResult:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": cwd or "/tmp", "LANG": "C.UTF-8"}
    t0 = time.monotonic()
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd=cwd, env=env, start_new_session=True, preexec_fn=_limits(mem_mb))
    timed_out = False
    try:
        out, err = p.communicate(stdin, timeout=max(0.05, timeout_s))
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = p.communicate()
    return RunResult(p.returncode, out[:max_output], err[:max_output], time.monotonic() - t0, timed_out)

@dataclass
class CaseResult:
    ok: bool
    output: object = None
    error: str = ""
    duration_s: float = 0.0
    mutated: bool = False
    timed_out: bool = False

# Trusted harness. Reads one repr(args_tuple) per line from cases.txt, prints one repr(dict) per line.
_PY_HARNESS = r'''
import ast, copy, importlib.util, sys, time, traceback
spec = importlib.util.spec_from_file_location("cand", sys.argv[1]); mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod); fn = getattr(mod, sys.argv[2])
except Exception:
    print(repr({"ok": False, "error": "IMPORT: " + traceback.format_exc()[-1500:]}), flush=True); sys.exit(2)
for line in open(sys.argv[3], encoding="utf-8"):
    line = line.rstrip("\n")
    if not line: continue
    args = ast.literal_eval(line); before = copy.deepcopy(args)
    t0 = time.perf_counter()
    try:
        out = fn(*args); ok = True; err = ""
    except BaseException:
        out = None; ok = False; err = traceback.format_exc()[-1500:]
    dt = time.perf_counter() - t0
    try:
        mutated = args != before
    except Exception:
        mutated = True
    print(repr({"ok": ok, "output": out, "error": err, "duration_s": dt, "mutated": mutated}), flush=True)
'''

def run_python_cases(source: str, entrypoint: str, args_list: list[tuple], *, timeout_s: float, workdir: str) -> list[CaseResult]:
    os.makedirs(workdir, exist_ok=True)
    cand = os.path.join(workdir, "cand.py"); harness = os.path.join(workdir, "harness.py"); cases = os.path.join(workdir, "cases.txt")
    with open(cand, "w", encoding="utf-8") as f: f.write(source)
    with open(harness, "w", encoding="utf-8") as f: f.write(_PY_HARNESS)
    with open(cases, "w", encoding="utf-8") as f:
        for a in args_list: f.write(repr(tuple(a)) + "\n")
    r = run_cmd([PYTHON, harness, cand, entrypoint, cases], timeout_s=timeout_s, cwd=workdir, max_output=50_000_000)
    results: list[CaseResult] = []
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        try:
            d = ast.literal_eval(line)
        except Exception:
            continue
        if d.get("error", "").startswith("IMPORT:"):
            return [CaseResult(False, error=d["error"]) for _ in args_list]
        results.append(CaseResult(d["ok"], d.get("output"), d.get("error", ""), d.get("duration_s", 0.0), d.get("mutated", False)))
    while len(results) < len(args_list):
        results.append(CaseResult(False, error="timeout" if r.timed_out else "harness died: " + r.stderr.decode("utf-8", "replace")[-500:], timed_out=r.timed_out))
    return results[:len(args_list)]

_PY_FORBIDDEN_CALLS = {"open", "input", "print", "exec", "eval", "compile", "__import__", "breakpoint"}
_PY_FORBIDDEN_MODULES = {"os", "subprocess", "socket", "sys", "shutil", "pathlib", "threading", "multiprocessing", "ctypes", "signal", "random"}

def python_static(source: str, entrypoint: str) -> list[str]:
    problems: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"syntax error: {e}"]
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    if entrypoint not in names:
        problems.append(f"entrypoint {entrypoint} not defined at top level")
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            for m in mods:
                root = m.split(".")[0]
                if root not in sys.stdlib_module_names:
                    problems.append(f"non-stdlib import {m}")
                elif root in _PY_FORBIDDEN_MODULES and not (root == "sys" and _only_setrecursionlimit(tree)):
                    problems.append(f"forbidden import {m}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _PY_FORBIDDEN_CALLS:
            problems.append(f"forbidden call {node.func.id}")
    return problems

def _only_setrecursionlimit(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "sys":
            if node.attr not in ("setrecursionlimit", "getrecursionlimit", "maxsize"):
                return False
    return True
```
Note: `random` is forbidden because the statements require determinism; `threading` is forbidden except the prompt tells the model to use iterative code instead of the big-stack-thread trick.

- [ ] **Step 4: Run tests**

Run: `uv run --group dev pytest tests/test_sandbox.py -q`
Expected: all PASS. If `test_run_cmd_kills_process_group_on_timeout` fails, check that `start_new_session=True` and `os.killpg` are both present.

- [ ] **Step 5: Commit**

```bash
git add sandbox.py tests/test_sandbox.py
git commit -m "feat: sandbox run_cmd with process-group kill, python harness, static checks"
```

---

### Task 3: Sandbox — Rust compile, run, static checks

**Files:**
- Modify: `sandbox.py` (append)
- Test: `tests/test_sandbox.py` (append)

**Interfaces:**
- Produces:
  - `rust_static(source: str) -> list[str]`.
  - `compile_rust(source: str, *, overflow_checks: bool, workdir: str, timeout_s: float = 90.0) -> tuple[str | None, str]` returns `(binary_path, compiler_stderr)`; caches by sha256 of source + flags in `workdir`.
  - `run_rust_cases(binary: str, stdins: list[str], *, timeout_s: float, mem_mb: int = 4096) -> list[CaseResult]` where `output` is stdout `str`.
  - `tokens(s: str) -> list[str]` = `s.split()`.

- [ ] **Step 1: Write failing tests (append to `tests/test_sandbox.py`)**

```python
import shutil
from sandbox import rust_static, compile_rust, run_rust_cases, tokens
needs_rustc = pytest.mark.skipif(shutil.which("rustc") is None, reason="rustc not installed")

RS_OK = 'use std::io::*;\nfn main(){let mut s=String::new();stdin().read_to_string(&mut s).unwrap();let n:i64=s.trim().parse().unwrap();println!("{}",n*2);}\n'

@needs_rustc
def test_compile_and_run_rust(tmp_path):
    binp, err = compile_rust(RS_OK, overflow_checks=True, workdir=str(tmp_path))
    assert binp and os.path.exists(binp), err
    res = run_rust_cases(binp, ["21\n", "5\n"], timeout_s=5)
    assert [tokens(r.output) for r in res] == [["42"], ["10"]]
    binp2, _ = compile_rust(RS_OK, overflow_checks=True, workdir=str(tmp_path))
    assert binp2 == binp  # cached

@needs_rustc
def test_compile_error_reported(tmp_path):
    binp, err = compile_rust("fn main(){ let x: i32 = \"s\"; }", overflow_checks=True, workdir=str(tmp_path))
    assert binp is None and "mismatched types" in err

@needs_rustc
def test_overflow_checks_panic_only_in_gate_build(tmp_path):
    src = 'fn main(){let mut x: i64 = i64::MAX; let y = std::hint::black_box(1i64); x += y; println!("{}", x);}'
    gate, _ = compile_rust(src, overflow_checks=True, workdir=str(tmp_path))
    stress, _ = compile_rust(src, overflow_checks=False, workdir=str(tmp_path))
    assert not run_rust_cases(gate, [""], timeout_s=5)[0].ok
    assert run_rust_cases(stress, [""], timeout_s=5)[0].ok

@needs_rustc
def test_rust_timeout(tmp_path):
    binp, _ = compile_rust("fn main(){loop{}}", overflow_checks=False, workdir=str(tmp_path))
    r = run_rust_cases(binp, [""], timeout_s=0.5)[0]
    assert r.timed_out and not r.ok

def test_rust_static_rules():
    assert rust_static("fn main(){}") == []
    assert any("main" in v for v in rust_static("fn helper(){}"))
    assert any("unsafe" in v for v in rust_static("fn main(){ unsafe { } }"))
    assert any("fs" in v for v in rust_static("use std::fs; fn main(){}"))
    assert any("extern" in v for v in rust_static("extern crate rand; fn main(){}"))
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --group dev pytest tests/test_sandbox.py -q -k rust`
Expected: FAIL with `ImportError: cannot import name 'rust_static'`

- [ ] **Step 3: Implement (append to `sandbox.py`)**

```python
import hashlib, re

_RS_FORBIDDEN = [(r"\bunsafe\b", "unsafe block"), (r"\bextern\s+crate\b", "extern crate"),
                 (r"std::fs\b", "std::fs"), (r"std::net\b", "std::net"), (r"std::process::Command", "process::Command"),
                 (r"std::env::args", "env::args")]

def rust_static(source: str) -> list[str]:
    problems = []
    if not re.search(r"\bfn\s+main\s*\(", source):
        problems.append("no fn main()")
    for pat, label in _RS_FORBIDDEN:
        if re.search(pat, source):
            problems.append(f"forbidden: {label}")
    return problems

def compile_rust(source: str, *, overflow_checks: bool, workdir: str, timeout_s: float = 90.0) -> tuple[str | None, str]:
    os.makedirs(workdir, exist_ok=True)
    tag = "gate" if overflow_checks else "stress"
    h = hashlib.sha256((tag + source).encode()).hexdigest()[:16]
    binp = os.path.join(workdir, f"bin_{tag}_{h}")
    if os.path.exists(binp):
        return binp, ""
    src = os.path.join(workdir, f"cand_{h}.rs")
    with open(src, "w", encoding="utf-8") as f: f.write(source)
    argv = ["rustc", "--edition", "2021", "-O", "-C", f"overflow-checks={'on' if overflow_checks else 'off'}",
            "-A", "warnings", "-o", binp, src]
    r = run_cmd(argv, timeout_s=timeout_s, cwd=workdir)
    err = r.stderr.decode("utf-8", "replace")
    if r.returncode != 0 or not os.path.exists(binp):
        return None, err if not r.timed_out else "rustc timed out"
    return binp, err

def run_rust_cases(binary: str, stdins: list[str], *, timeout_s: float, mem_mb: int = 4096) -> list[CaseResult]:
    out = []
    for s in stdins:
        r = run_cmd([binary], stdin=s.encode("utf-8"), timeout_s=timeout_s, cwd=os.path.dirname(binary), mem_mb=mem_mb, max_output=50_000_000)
        ok = r.returncode == 0 and not r.timed_out
        out.append(CaseResult(ok, r.stdout.decode("utf-8", "replace"), "" if ok else r.stderr.decode("utf-8", "replace")[-1500:], r.duration_s, False, r.timed_out))
    return out

def tokens(s: str) -> list[str]:
    return s.split()
```

- [ ] **Step 4: Run tests**

Run: `source ~/.cargo/env && uv run --group dev pytest tests/test_sandbox.py -q`
Expected: all PASS (Rust tests must not be skipped; if they are, `rustc` is not on PATH).

- [ ] **Step 5: Commit**

```bash
git add sandbox.py tests/test_sandbox.py
git commit -m "feat: rust compile with overflow-check builds, binary cache, token compare"
```

---

### Task 4: Budget controller

**Files:**
- Create: `solve.py` (Budget only for now; orchestrator added in Task 6)
- Test: `tests/test_budget.py`

**Interfaces:**
- Produces: `class Budget(deadline_s: float, *, margin_s: float, phases: dict, clock=time.monotonic)` with `remaining() -> float`, `elapsed_frac() -> float`, `phase() -> str` in `{"generate","gate","repair","settle","emit","expired"}`, `step_timeout(cap_s: float, reserve_s: float = 0.0) -> float`, `can_afford(seconds: float) -> bool`, `usable_s: float`.

- [ ] **Step 1: Write failing tests**

`tests/test_budget.py`:
```python
from solve import Budget
PH = {"generate_until": 0.32, "gate_until": 0.39, "repair_until": 0.81, "settle_until": 0.93}

class Clock:
    def __init__(self): self.t = 100.0
    def __call__(self): return self.t

def mk():
    c = Clock(); return c, Budget(300.0, margin_s=15.0, phases=PH, clock=c)

def test_usable_and_remaining():
    c, b = mk()
    assert b.usable_s == 285.0 and b.remaining() == 285.0
    c.t += 100; assert b.remaining() == 185.0

def test_phase_transitions():
    c, b = mk()
    assert b.phase() == "generate"
    c.t = 100 + 0.33 * 285; assert b.phase() == "gate"
    c.t = 100 + 0.40 * 285; assert b.phase() == "repair"
    c.t = 100 + 0.82 * 285; assert b.phase() == "settle"
    c.t = 100 + 0.94 * 285; assert b.phase() == "emit"
    c.t = 100 + 286; assert b.phase() == "expired" and b.remaining() == 0.0

def test_step_timeout_is_min_of_cap_and_remaining_minus_reserve():
    c, b = mk()
    assert b.step_timeout(90.0) == 90.0
    c.t = 100 + 285 - 50
    assert b.step_timeout(90.0, reserve_s=20.0) == 30.0
    c.t = 100 + 285 - 5
    assert b.step_timeout(90.0, reserve_s=20.0) == 0.0

def test_can_afford():
    c, b = mk()
    c.t = 100 + 285 - 61
    assert b.can_afford(60) and not b.can_afford(62)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --group dev pytest tests/test_budget.py -q`
Expected: FAIL `ModuleNotFoundError: No module named 'solve'`

- [ ] **Step 3: Implement `solve.py` (Budget section)**

```python
"""CLI, orchestrator, budget controller, finalizer, report."""
from __future__ import annotations
import time

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
```

- [ ] **Step 4: Run tests**

Run: `uv run --group dev pytest tests/test_budget.py -q`
Expected: `4 passed`

- [ ] **Step 5: Commit**

```bash
git add solve.py tests/test_budget.py
git commit -m "feat: monotonic budget controller with phase transitions"
```

---

### Task 5: LLM client — profiles, serialized calls, timeouts, marker parser, fake

**Files:**
- Create: `llm.py`
- Test: `tests/test_llm.py`

**Interfaces:**
- Produces:
  - `Role` dataclass: `name, base_url, model, api_key_env, max_tokens, max_concurrent, timeout_cap_s, extra: dict`.
  - `Reply` dataclass: `content: str, reasoning: str, usage: dict, latency_s: float, model: str, error: str | None`; `Reply.text` property returns `content` if non-empty else `reasoning`.
  - `load_config(path: str, profile: str) -> dict` with keys `roles: dict[str, Role]`, `limits: dict`, `phases: dict`.
  - `class LLM(roles: dict[str, Role])` with `chat(role: str, system: str, user: str, *, timeout_s: float, max_tokens: int | None = None, tag: str = "") -> Reply` and `calls: list[dict]` (one record per call: role, tag, model, latency_s, usage, error, timed_out). `tag` is the prompt name (`solve`, `oracle`, `stress`, `repair`) and is only for records and the fake.
  - `parse_blocks(text: str) -> dict[str, str]` for `===NAME===` … `===END===` blocks; code fences inside a block are stripped.
  - `class FakeLLM(script: dict[str, list[str]])` — same `chat` signature. Script keys are matched against `tag` first, then `role`; replies are returned in order per key; records `calls` and `prompts` (list of `(role, system, user)`); raises `AssertionError` if the matched script is exhausted. Keying by tag is required because the orchestrator issues the three generation calls from a thread pool, so role-only keys would be consumed in a nondeterministic order.

- [ ] **Step 1: Write failing tests**

`tests/test_llm.py`:
```python
import json, threading, time, pathlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import pytest
from llm import LLM, Role, FakeLLM, parse_blocks, load_config

ROOT = pathlib.Path(__file__).resolve().parent.parent

def test_parse_blocks_basic_and_fences():
    t = "junk\n===RULES===\n1. a\n===END===\n===CODE===\n```python\ndef f(): pass\n```\n===END===\n"
    b = parse_blocks(t)
    assert b["RULES"] == "1. a" and b["CODE"] == "def f(): pass"

def test_parse_blocks_unterminated_takes_rest():
    assert parse_blocks("===CODE===\nx = 1\n")["CODE"] == "x = 1"

def test_load_config_profiles():
    cfg = load_config(str(ROOT / "config.toml"), "openrouter")
    assert set(cfg["roles"]) == {"strong", "fast"} and cfg["roles"]["strong"].max_concurrent == 3
    assert cfg["roles"]["strong"].extra["reasoning"]["effort"] == "medium"
    with pytest.raises(KeyError):
        load_config(str(ROOT / "config.toml"), "nope")

class _Handler(BaseHTTPRequestHandler):
    active = 0; max_active = 0; lock = threading.Lock(); delay = 0.3
    def do_POST(self):
        n = int(self.headers["Content-Length"]); body = json.loads(self.rfile.read(n))
        with _Handler.lock:
            _Handler.active += 1; _Handler.max_active = max(_Handler.max_active, _Handler.active)
        time.sleep(_Handler.delay)
        with _Handler.lock: _Handler.active -= 1
        resp = {"choices": [{"message": {"role": "assistant", "content": "", "reasoning_content": "===CODE===\nprint(1)\n===END==="}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "model": body["model"]}
        data = json.dumps(resp).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, *a): pass

@pytest.fixture
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler); th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"; srv.shutdown()

def test_chat_serializes_when_max_concurrent_1(server):
    _Handler.max_active = 0
    role = Role("strong", server, "m", "", 100, 1, 30.0, {})
    llm = LLM({"strong": role})
    ts = [threading.Thread(target=lambda: llm.chat("strong", "s", "u", timeout_s=10)) for _ in range(3)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert _Handler.max_active == 1 and len(llm.calls) == 3

def test_chat_reply_text_falls_back_to_reasoning(server):
    llm = LLM({"strong": Role("strong", server, "m", "", 100, 1, 30.0, {})})
    r = llm.chat("strong", "s", "u", timeout_s=10)
    assert r.content == "" and "===CODE===" in r.text and r.usage["completion_tokens"] == 5 and r.error is None

def test_chat_times_out(server):
    _Handler.delay = 2.0
    try:
        llm = LLM({"strong": Role("strong", server, "m", "", 100, 1, 30.0, {})})
        t0 = time.monotonic(); r = llm.chat("strong", "s", "u", timeout_s=0.5)
        assert r.error == "timeout" and time.monotonic() - t0 < 1.5 and llm.calls[-1]["timed_out"]
    finally:
        _Handler.delay = 0.3

def test_fake_llm_scripts_by_tag_then_role():
    f = FakeLLM({"strong": ["one", "two"], "oracle": ["orc"]})
    assert f.chat("fast", "", "", timeout_s=1, tag="oracle").text == "orc"
    assert f.chat("strong", "", "", timeout_s=1, tag="solve").text == "one"   # no 'solve' key: falls back to role
    assert f.chat("strong", "", "", timeout_s=1).text == "two"
    with pytest.raises(AssertionError):
        f.chat("strong", "", "", timeout_s=1)
    assert f.calls[0]["tag"] == "oracle"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --group dev pytest tests/test_llm.py -q`
Expected: FAIL `ModuleNotFoundError: No module named 'llm'`

- [ ] **Step 3: Implement `llm.py`**

```python
"""One OpenAI-compatible chat client. Provider is chosen purely by config."""
from __future__ import annotations
import json, os, re, threading, time, tomllib, urllib.request, urllib.error
from dataclasses import dataclass, field

@dataclass
class Role:
    name: str
    base_url: str
    model: str
    api_key_env: str
    max_tokens: int
    max_concurrent: int
    timeout_cap_s: float
    extra: dict = field(default_factory=dict)

@dataclass
class Reply:
    content: str
    reasoning: str
    usage: dict
    latency_s: float
    model: str
    error: str | None = None

    @property
    def text(self) -> str:
        return self.content if self.content.strip() else self.reasoning

def load_config(path: str, profile: str) -> dict:
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    prof = cfg["profiles"][profile]  # KeyError on unknown profile is intended
    roles = {name: Role(name, r["base_url"].rstrip("/"), r["model"], r.get("api_key_env", ""), int(r["max_tokens"]),
                        int(r.get("max_concurrent", 1)), float(r.get("timeout_cap_s", 120.0)), dict(r.get("extra", {})))
             for name, r in prof.items()}
    return {"roles": roles, "limits": cfg["limits"], "phases": cfg["phases"], "profile": profile}

class LLM:
    def __init__(self, roles: dict[str, Role]):
        self.roles = roles
        self.calls: list[dict] = []
        self._sems: dict[tuple, threading.Semaphore] = {}
        for r in roles.values():
            self._sems.setdefault((r.base_url, r.model), threading.Semaphore(r.max_concurrent))

    def chat(self, role: str, system: str, user: str, *, timeout_s: float, max_tokens: int | None = None, tag: str = "") -> Reply:
        r = self.roles[role]
        body = {"model": r.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "max_tokens": max_tokens or r.max_tokens, "temperature": 0.2, "stream": False, **r.extra}
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(r.api_key_env) if r.api_key_env else None
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(r.base_url + "/chat/completions", data=json.dumps(body).encode(), headers=headers)
        result: dict = {}
        def work():
            try:
                with urllib.request.urlopen(req, timeout=timeout_s + 5) as resp:
                    result["data"] = json.load(resp)
            except urllib.error.HTTPError as e:
                result["error"] = f"http {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
            except Exception as e:  # transport errors
                result["error"] = f"transport: {e!r}"[:300]
        t0 = time.monotonic()
        with self._sems[(r.base_url, r.model)]:
            th = threading.Thread(target=work, daemon=True); th.start(); th.join(timeout=timeout_s)
            timed_out = th.is_alive()
        latency = time.monotonic() - t0
        if timed_out:
            reply = Reply("", "", {}, latency, r.model, "timeout")
        elif "error" in result:
            reply = Reply("", "", {}, latency, r.model, result["error"])
        else:
            d = result["data"]; msg = d["choices"][0]["message"]
            reply = Reply(msg.get("content") or "", msg.get("reasoning_content") or msg.get("reasoning") or "",
                          d.get("usage") or {}, latency, d.get("model", r.model), None)
        self.calls.append({"role": role, "tag": tag, "model": r.model, "latency_s": round(latency, 2), "usage": reply.usage,
                           "error": reply.error, "timed_out": timed_out, "max_tokens": body["max_tokens"]})
        return reply

_BLOCK = re.compile(r"===([A-Z_]+)===\s*\n(.*?)(?:\n===END===|\Z)", re.S)
_FENCE = re.compile(r"^```[a-zA-Z0-9_+-]*\s*\n(.*?)\n```\s*$", re.S)

def parse_blocks(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, body in _BLOCK.findall(text):
        if name == "END":
            continue
        body = body.strip()
        m = _FENCE.match(body)
        if m:
            body = m.group(1).strip()
        out.setdefault(name, body)
    return out

class FakeLLM:
    def __init__(self, script: dict[str, list[str]]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[dict] = []
        self.prompts: list[tuple[str, str, str]] = []
        self._lock = threading.Lock()
    def chat(self, role: str, system: str, user: str, *, timeout_s: float, max_tokens: int | None = None, tag: str = "") -> Reply:
        with self._lock:
            key = tag if tag in self.script else role
            assert self.script.get(key), f"FakeLLM: no scripted reply left for {key!r} (tag={tag!r}, role={role!r})"
            text = self.script[key].pop(0)
            self.prompts.append((role, system, user))
            self.calls.append({"role": role, "tag": tag, "model": "fake", "latency_s": 0.0, "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "error": None, "timed_out": False})
        return Reply(text, "", {"prompt_tokens": 1, "completion_tokens": 1}, 0.0, "fake", None)
```

- [ ] **Step 4: Run tests**

Run: `uv run --group dev pytest tests/test_llm.py -q`
Expected: `7 passed` (one test uses a live in-process HTTP server; no network needed)

- [ ] **Step 5: Smoke against OpenRouter (manual; needs `export OPENROUTER_API_KEY=...`)**

Run: `uv run python -c "from llm import *; c=load_config('config.toml','openrouter'); l=LLM(c['roles']); r=l.chat('fast','You answer tersely.','Reply with ===OK===\nyes\n===END===', timeout_s=60, max_tokens=400); print(r.error, parse_blocks(r.text), l.calls[-1])"`
Expected: `None {'OK': 'yes'} {...}` and the call record's `usage` contains a `cost` key with a value well under $0.001. Run it once for `strong` too so both model ids are confirmed live.

- [ ] **Step 6: Commit**

```bash
git add llm.py tests/test_llm.py
git commit -m "feat: config-driven OpenAI-compatible client with serialization, timeouts, marker parser"
```

---

### Task 6: Prompts and single-shot orchestrator (SOLVE → static → compile → emit + report)

**Files:**
- Create: `prompts/solve.md`, `prompts/oracle.md`, `prompts/stress.md`, `prompts/repair.md`
- Modify: `solve.py` (add `Candidate`, `Run`, `solve()`, `main()`)
- Test: `tests/test_solve.py`

**Interfaces:**
- Produces:
  - `Candidate` dataclass: `id: str, source: str, parent: str | None, evidence: list = []`, plus `passed_count()`.
  - `render(name: str, **vars) -> str` loads `prompts/<name>.md` and substitutes `{{statement}}`, `{{language}}`, `{{entrypoint}}`, `{{contract}}`, etc. with simple `str.replace`.
  - `solve(problem: Problem, llm, cfg: dict, *, out_path: str, run_dir: str, deadline_scale: float = 1.0, clock=time.monotonic) -> dict` (the report). Writes `out_path` and `run_dir/report.json`, `run_dir/log.txt`, `run_dir/candidates/<id>.<ext>`.
  - `main(argv=None) -> int` exit codes: 0 emitted and passed all gates, 1 emitted but unverified or with failures, 2 invalid input, 3 nothing compiled.
  - Report `status` values: `passed_all_gates` (every gate ran and passed), `emitted_unverified` (no gate failed but at least one was skipped, e.g. the oracle produced no cases), `emitted_with_failures`, `no_candidate`.
- Consumes: `Budget` (Task 4), `LLM/FakeLLM/parse_blocks/load_config` (Task 5), `Problem/python_static/rust_static/compile_rust` (Tasks 2–3).

- [ ] **Step 1: Write `prompts/solve.md`**

````markdown
You are solving a hard algorithmic problem. The statement below is the only specification. Where your memory of a standard algorithm disagrees with the statement, the statement wins.

Target language: {{language}}. Contract: {{contract}}

Respond with exactly these blocks, in order, each terminated by a line `===END===`. Keep reasoning brief; the budget is small.

===RULES===
Numbered restatement of every behavioral sentence of the statement, quoting the text. List ambiguities separately with the reading you chose.
===END===
===TRAPS===
For EACH item below, one line: what a naive solution would do and why it fails here, or "n/a".
- counts/repetitions/capacities up to 10^18 that must not be iterated
- structures that must not be materialized (enormous layouts, exponential unfoldings, cell enumeration)
- persistence/branching across versions
- unbounded reversals or splices on long sequences
- recursion depth linear in input size (Python recursion limit)
- integer overflow (Rust i64) — use i128/u128 for sums of 10^18 quantities
- custom Unicode/grapheme/byte rules that differ from the standard library
- wording that redefines behavior after exhaustion, reset, or removal
===END===
===ALGORITHM===
Data structures, per-operation complexity against the stated maximum sizes, overflow and recursion treatment.
===END===
===CODE===
The complete solution. {{language_rules}}
===END===

Problem statement:

{{statement}}
````

`{{language_rules}}` for python: `Define def {{entrypoint}}(...) at top level. Standard library only. No I/O, no print, no global mutable state, do not mutate arguments, no recursion over input-sized structures (use explicit stacks), no random.` For rust: `One complete program with fn main(). Read all of stdin into one String, write via a BufWriter, std only, no unsafe, no HashMap iteration order reaching output (use BTreeMap or sort), use i128/u128 where sums can exceed i64.`

- [ ] **Step 2: Write `prompts/oracle.md`**

````markdown
You write a SLOW, LITERAL reference implementation and a small random input generator for testing. You never see any other solution. Follow the statement sentence by sentence; loop where it loops; build what it describes. Inputs will be tiny. Efficiency is irrelevant. Python 3.11, standard library only.

{{oracle_signature}}

Also define `gen(seed: int, mode: str)` using `random.Random(seed)` only. `mode == "small"`: sizes at most 6, values at most 20, every validity constraint of the statement honored, include boundary shapes (empty where allowed, single element, equal values, adjacent positions). `mode == "medium"`: sizes at most 12 but numeric counts/repetitions/demands around 10^4 to 10^5 so that closed-form arithmetic in a fast solution is exercised while your literal loops still finish in a few seconds. {{gen_returns}}

Respond with exactly one block:

===ORACLE===
python source defining reference and gen
===END===

Problem statement:

{{statement}}
````

`{{oracle_signature}}` python: `Define reference(*args) with the same parameters as {{entrypoint}} and the same return value.` rust: `Define reference(stdin_text: str) -> str that returns the exact expected stdout for that stdin.` `{{gen_returns}}` python: `gen returns a tuple of positional arguments.` rust: `gen returns the stdin text as a str.`

- [ ] **Step 3: Write `prompts/stress.md`**

````markdown
You write a generator for one MAXIMUM-SIZE input and a list of small hand-picked edge-case inputs for testing. Python 3.11 standard library only, `random.Random(seed)` only.

Define `gen_max(seed: int)` returning {{gen_returns}} at every stated maximum simultaneously (max counts, max values, max nesting, deepest paths, worst-case operation mix). It must run in under 10 seconds itself.
Define `EDGES = [...]`, 5 to 10 literal inputs of the same shape: empty/minimum, single element, duplicates, boundary indices, overflow-adjacent values, and one case that distinguishes the literal statement from a plausible misreading.

Respond with exactly one block:

===STRESS===
python source defining gen_max and EDGES
===END===

Problem statement:

{{statement}}
````

- [ ] **Step 4: Write `prompts/repair.md`**

````markdown
A solution failed a concrete check. Make the smallest change that fixes the demonstrated failure. Keep the public contract. Do not switch algorithms unless the failure proves the algorithm wrong. {{language_rules}}

Failure kind: {{kind}}
Input:
{{input}}
Expected (from an independent literal reference, which may itself be wrong):
{{expected}}
Actual:
{{actual}}
Details:
{{details}}

First hand-trace the statement on this input. Then respond with:

===VERDICT===
candidate   (if the solution is wrong)  or  oracle   (if the reference is wrong and the solution is right)
===END===
===CODE===
the full corrected solution (repeat the current one unchanged if VERDICT is oracle)
===END===

Statement:

{{statement}}

Current solution:

{{code}}
````

- [ ] **Step 5: Write failing orchestrator tests**

`tests/test_solve.py`:
```python
import json, os, pathlib, pytest
from llm import FakeLLM
from sandbox import Problem
import solve as S

def prob(tmp_path, lang="python"):
    return Problem("pid", lang, "Return a+b for ints a,b. At most 10^18.", "add" if lang == "python" else "main", [], 300.0)

def cfg():
    return {"limits": {"safety_margin_s": 15.0, "cases_small": 5, "cases_medium": 2, "stress_limit_python_s": 5.0, "stress_limit_rust_s": 2.0, "max_repairs": 2, "mem_mb": 2048, "shrink_budget_s": 1.0, "max_cost_usd_per_problem": 0.10},
            "phases": {"generate_until": 0.32, "gate_until": 0.39, "repair_until": 0.81, "settle_until": 0.93}, "profile": "fake"}

SOLVE_OK = "===RULES===\nr\n===END===\n===TRAPS===\nt\n===END===\n===ALGORITHM===\na\n===END===\n===CODE===\n```python\ndef add(a, b):\n    return a + b\n```\n===END===\n"
ORACLE_OK = "===ORACLE===\nimport random\ndef reference(a, b):\n    return a + b\ndef gen(seed, mode):\n    r = random.Random(seed)\n    return (r.randint(0, 20), r.randint(0, 20))\n===END===\n"
STRESS_OK = "===STRESS===\ndef gen_max(seed):\n    return (10**18, 10**18)\nEDGES = [(0, 0), (1, 0)]\n===END===\n"

def test_single_shot_emits_solution_and_report(tmp_path):
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    out = tmp_path / "solution.py"; run_dir = tmp_path / "run"
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(out), run_dir=str(run_dir))
    assert out.read_text().startswith("def add")
    assert rep["status"] in ("passed_all_gates", "emitted_unverified") and rep["final_candidate"] == "c1"
    assert (run_dir / "report.json").exists() and (run_dir / "candidates" / "c1.py").exists()
    assert rep["deadline_scale"] == 1.0 and {c["tag"] for c in rep["calls"]} == {"solve", "oracle", "stress"}

def test_static_failure_without_repair_budget_still_emits(tmp_path):
    bad = SOLVE_OK.replace("def add(a, b):", "def wrong(a, b):")
    llm = FakeLLM({"solve": [bad], "repair": [bad, bad], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rep = S.solve(prob(tmp_path), llm, cfg(), out_path=str(tmp_path / "s.py"), run_dir=str(tmp_path / "run"))
    assert rep["status"] == "emitted_with_failures"
    assert any(e["kind"] == "static" and not e["passed"] for e in rep["evidence"]["c1"])

def test_invalid_problem_exit_code(tmp_path):
    p = tmp_path / "bad.json"; p.write_text("{}")
    assert S.main([str(p), "-o", str(tmp_path / "x.py")]) == 2   # exits before any config or network use
```

- [ ] **Step 6: Run to verify failure**

Run: `uv run --group dev pytest tests/test_solve.py -q`
Expected: FAIL `AttributeError: module 'solve' has no attribute 'solve'`

- [ ] **Step 7: Implement the orchestrator (append to `solve.py`)**

```python
import argparse, json, os, pathlib, sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from llm import LLM, load_config, parse_blocks
from sandbox import Problem, python_static, rust_static, compile_rust
import verify as V   # created in Task 7; for Task 6 create verify.py containing only `Evidence` (see below)

ROOT = pathlib.Path(__file__).resolve().parent

@dataclass
class Candidate:
    id: str
    source: str
    parent: str | None
    evidence: list = field(default_factory=list)
    def passed_count(self) -> int:
        return sum(1 for e in self.evidence if e.passed)
    def all_passed(self) -> bool:
        return bool(self.evidence) and all(e.passed for e in self.evidence)

_LANG_RULES = {
    "python": "Define def {ep}(...) at top level. Standard library only. No I/O, no print, no global mutable state, do not mutate arguments, no recursion over input-sized structures (use explicit stacks), no random.",
    "rust": "One complete program with fn main(). Read all of stdin into one String, write via a BufWriter, std only, no unsafe, no HashMap iteration order reaching output (use BTreeMap or sort), use i128/u128 where sums can exceed i64.",
}

def render(name: str, **vars) -> str:
    text = (ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8")
    for k, v in vars.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text

def prompt_vars(p: Problem) -> dict:
    py = p.language == "python"
    return {"statement": p.statement, "language": p.language, "entrypoint": p.entrypoint,
            "contract": f"Python function `{p.entrypoint}`" if py else "Rust program reading stdin, writing stdout",
            "language_rules": _LANG_RULES[p.language].format(ep=p.entrypoint),
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
        os.makedirs(os.path.join(run_dir, "candidates"), exist_ok=True)
    def log(self, msg: str):
        line = f"[{self.budget.elapsed():07.1f}] {msg}"; self.events.append(line)
        with open(os.path.join(self.dir, "log.txt"), "a", encoding="utf-8") as f: f.write(line + "\n")
    def ext(self) -> str:
        return "py" if self.p.language == "python" else "rs"
    def cost_usd(self) -> float:
        return sum(float((c.get("usage") or {}).get("cost") or 0.0) for c in self.llm.calls)
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

def best_candidate(cands: list[Candidate]) -> Candidate | None:
    if not cands: return None
    return max(cands, key=lambda c: (c.passed_count(), int(c.id[1:])))

def write_solution(out_path: str, source: str):
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: f.write(source.rstrip() + "\n")
    os.replace(tmp, out_path)

def solve(problem: Problem, llm, cfg: dict, *, out_path: str, run_dir: str, deadline_scale: float = 1.0, clock=time.monotonic) -> dict:
    run = Run(problem, llm, cfg, run_dir, deadline_scale, clock)
    run.log(f"intake lang={problem.language} deadline={problem.deadline_s} scale={deadline_scale} usable={run.budget.usable_s:.0f}")
    pv = prompt_vars(problem)
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_solve = ex.submit(run.chat, "strong", "solve", 0.30 * run.budget.usable_s, **pv)
        f_oracle = ex.submit(run.chat, "fast", "oracle", 0.30 * run.budget.usable_s, **pv)
        f_stress = ex.submit(run.chat, "fast", "stress", 0.30 * run.budget.usable_s, **pv)
        r_solve, r_oracle, r_stress = f_solve.result(), f_oracle.result(), f_stress.result()
    code = parse_blocks(r_solve.text).get("CODE", "")
    if code.strip():
        cand = run.add_candidate(code, None); cand.evidence.append(static_evidence(problem, code))
    oracle_src = parse_blocks(r_oracle.text).get("ORACLE", "")
    stress_src = parse_blocks(r_stress.text).get("STRESS", "")
    gi = V.prepare_gate_inputs(problem, oracle_src, stress_src, cfg["limits"], workdir=os.path.join(run_dir, "oracle"), budget=run.budget, log=run.log)
    repairs = 0
    while run.cands:
        cand = run.cands[-1]
        if cand.evidence and cand.evidence[0].passed:
            cand.evidence = [cand.evidence[0]] + V.run_gate(problem, cand.source, gi, workdir=os.path.join(run_dir, cand.id), budget=run.budget, limits=cfg["limits"], log=run.log)
        if cand.all_passed():
            run.log("gate.passed"); break
        failed = next(e for e in cand.evidence if not e.passed)
        run.log(f"gate.failed kind={failed.kind} detail={json.dumps(failed.detail)[:300]}")
        if repairs >= cfg["limits"]["max_repairs"] or run.budget.phase() not in ("generate", "gate", "repair") or not run.budget.can_afford(60):
            break
        if run.cost_usd() >= cfg["limits"]["max_cost_usd_per_problem"]:
            run.log(f"cost.cap reached usd={run.cost_usd():.4f}"); break
        repairs += 1
        new_source, verdict = V.repair(run, cand, failed, gi, pv)
        if verdict == "oracle" and gi.oracle_regens == 0:
            gi = V.regenerate_oracle(run, gi, failed, pv)
            cand.evidence = cand.evidence[:1]   # re-gate the same candidate against the new oracle
            continue
        if new_source.strip() and new_source.strip() != cand.source.strip():
            nc = run.add_candidate(new_source, cand.id); nc.evidence.append(static_evidence(problem, new_source))
        else:
            break
    # settle/emit
    best = best_candidate(run.cands)
    status = "no_candidate"
    if best is not None:
        write_solution(out_path, best.source)
        skipped = any(e.detail.get("skipped") for e in best.evidence)
        status = ("passed_all_gates" if best.all_passed() and not skipped
                  else "emitted_unverified" if best.all_passed() else "emitted_with_failures")
        run.log(f"emit candidate={best.id} status={status}")
    report = {"problem_id": problem.problem_id, "language": problem.language, "profile": cfg.get("profile"), "deadline_s": problem.deadline_s,
              "deadline_scale": deadline_scale, "elapsed_s": round(run.budget.elapsed(), 1), "status": status,
              "final_candidate": best.id if best else None, "repairs": repairs, "oracle_regenerated": gi.oracle_regens,
              "evidence": {c.id: [asdict(e) for e in c.evidence] for c in run.cands}, "calls": list(llm.calls),
              "token_usage": {"prompt": sum((c["usage"] or {}).get("prompt_tokens", 0) for c in llm.calls),
                              "completion": sum((c["usage"] or {}).get("completion_tokens", 0) for c in llm.calls)},
              "cost_usd": round(run.cost_usd(), 5) if any((c.get("usage") or {}).get("cost") is not None for c in llm.calls) else None,
              "cost_cap_usd": cfg["limits"].get("max_cost_usd_per_problem"),
              "gate_inputs": gi.summary(), "events": run.events}
    with open(os.path.join(run_dir, "report.json"), "w", encoding="utf-8") as f: json.dump(report, f, indent=2, default=str)
    return report

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ChallengeBox AI solver")
    ap.add_argument("problem"); ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--profile", default="openrouter"); ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--deadline-scale", type=float, default=1.0); ap.add_argument("--run-dir", default=None)
    a = ap.parse_args(argv)
    try:
        problem = Problem.load(a.problem)
    except (ValueError, OSError, json.JSONDecodeError) as e:
        print(f"invalid problem: {e}", file=sys.stderr); return 2
    cfg = load_config(a.config, a.profile)
    run_dir = a.run_dir or str(ROOT / "runs" / problem.problem_id[:12])
    rep = solve(problem, LLM(cfg["roles"]), cfg, out_path=a.output, run_dir=run_dir, deadline_scale=a.deadline_scale)
    print(json.dumps({k: rep[k] for k in ("status", "final_candidate", "elapsed_s", "repairs", "token_usage", "cost_usd")}))
    return {"passed_all_gates": 0, "emitted_unverified": 1, "emitted_with_failures": 1}.get(rep["status"], 3)

if __name__ == "__main__":
    sys.exit(main())
```

For Task 6 only, create a minimal `verify.py` so the import works; Task 7 replaces its body:
```python
"""Verification: cases from the oracle, differential, behavior, stress, shrink, repair glue."""
from __future__ import annotations
from dataclasses import dataclass, field

@dataclass
class Evidence:
    kind: str
    passed: bool
    cases: int = 0
    duration_s: float = 0.0
    detail: dict = field(default_factory=dict)

@dataclass
class GateInputs:
    cases_small: list = field(default_factory=list)
    cases_medium: list = field(default_factory=list)
    cases_edge: list = field(default_factory=list)
    stress_input: object = None
    oracle_src: str = ""
    oracle_regens: int = 0
    notes: list = field(default_factory=list)
    def summary(self) -> dict:
        return {"small": len(self.cases_small), "medium": len(self.cases_medium), "edge": len(self.cases_edge), "stress": self.stress_input is not None, "notes": self.notes}

def prepare_gate_inputs(problem, oracle_src, stress_src, limits, *, workdir, budget, log) -> GateInputs:
    return GateInputs(oracle_src=oracle_src)

def run_gate(problem, source, gi, *, workdir, budget, limits, log) -> list[Evidence]:
    return []

def repair(run, cand, failed, gi, pv):
    return "", "candidate"

def regenerate_oracle(run, gi, failed, pv):
    return gi
```

- [ ] **Step 8: Run tests**

Run: `uv run --group dev pytest -q`
Expected: all PASS (`test_static_failure_without_repair_budget_still_emits` passes because the stub `repair` returns empty source, so the loop breaks and c1 is emitted with a failed static evidence).

- [ ] **Step 9: First real run on OpenRouter (needs `OPENROUTER_API_KEY`)**

Run: `uv run python solve.py samples/1dea328020722d9c3737e123e64dafc670e9bcbc8a9e0d3163eb6352fa2d9710.json -o runs/out.py`
Expected: exits 0 or 1 within 300 s, `runs/1dea32802072/log.txt` shows `solve.sent`, `solve.done`, `candidate.added`, `emit`, and `report.json` has a `cost_usd` under 0.05. Record the observed latency and cost in the commit message body.

- [ ] **Step 10: Commit**

```bash
git add prompts/ solve.py verify.py tests/test_solve.py
git commit -m "feat: prompts and single-shot orchestrator with report and CLI"
```

---

### Task 7: Verification — cases from the oracle, differential, shrink, gate assembly

**Files:**
- Modify: `verify.py` (replace stub body; keep `Evidence`, `GateInputs`)
- Test: `tests/test_verify.py`

**Interfaces:**
- Produces:
  - `Case` dataclass: `input: object` (args tuple for python, stdin str for rust), `expected: object`, `tag: str`.
  - `module_calls(source: str, fn: str, calls: list[tuple], *, workdir: str, timeout_s: float) -> list[CaseResult]` — runs a model-written Python module function in the sandbox (thin wrapper over `sandbox.run_python_cases`).
  - `make_cases(problem, oracle_src, inputs: list, tag: str, *, workdir, timeout_s) -> tuple[list[Case], Evidence]` — expected values from `reference`; inputs whose reference call fails are dropped and counted in evidence detail.
  - `run_candidate(problem, source, inputs: list, *, workdir, timeout_s, binary: str | None = None, overflow_checks=True) -> list[CaseResult]`.
  - `same(problem, actual: CaseResult, expected) -> bool`.
  - `differential(problem, source, cases: list[Case], kind: str, *, workdir, timeout_s, binary=None) -> Evidence` with `detail={"input", "expected", "actual", "error", "index"}` for the first failure.
  - `shrink(problem, source, oracle_src, case: Case, *, workdir, budget_s: float, binary=None) -> Case` (python-arg shrinking; rust returns the case unchanged).
  - `prepare_gate_inputs(...)` and `run_gate(...)` real implementations (stress and behavior steps added in Task 8, repair glue in Task 9).

- [ ] **Step 1: Write failing tests**

`tests/test_verify.py`:
```python
import os, pytest
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
    inputs = [r.output for r in V.module_calls(ORACLE, "gen", [(s, "small") for s in range(10)], workdir=str(tmp_path / "g"), timeout_s=20)]
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
    assert all(e.passed for e in ev) and [e.kind for e in ev][:3] == ["diff_edge", "diff_small", "diff_medium"]
    ev = V.run_gate(PY, BUG, gi, workdir=str(tmp_path / "g2"), budget=FakeBudget(), limits=limits, log=lambda m: None)
    assert ev[0].kind == "diff_edge" and not ev[0].passed and ev[0].detail["input"] == (15, 0)

def test_prepare_gate_inputs_survives_broken_oracle(tmp_path):
    gi = V.prepare_gate_inputs(PY, "def reference(a, b): raise RuntimeError()\ndef gen(seed, mode): return (1, 2)\n", "", {"cases_small": 3, "cases_medium": 1, "mem_mb": 2048}, workdir=str(tmp_path), budget=FakeBudget(), log=lambda m: None)
    assert gi.cases_small == [] and any("reference" in n for n in gi.notes)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --group dev pytest tests/test_verify.py -q`
Expected: FAIL `AttributeError: module 'verify' has no attribute 'Case'` (or similar)

- [ ] **Step 3: Implement `verify.py` (replace stub functions; keep Evidence and GateInputs)**

```python
import os, time
from dataclasses import dataclass, field
from sandbox import CaseResult, run_python_cases, compile_rust, run_rust_cases, tokens

@dataclass
class Case:
    input: object
    expected: object
    tag: str = ""

def module_calls(source: str, fn: str, calls: list[tuple], *, workdir: str, timeout_s: float) -> list[CaseResult]:
    return run_python_cases(source, fn, calls, timeout_s=timeout_s, workdir=workdir)

def _ref_args(problem, inp) -> tuple:
    return tuple(inp) if problem.language == "python" else (inp,)

def make_cases(problem, oracle_src: str, inputs: list, tag: str, *, workdir: str, timeout_s: float) -> tuple[list[Case], Evidence]:
    t0 = time.monotonic()
    res = module_calls(oracle_src, "reference", [_ref_args(problem, i) for i in inputs], workdir=workdir, timeout_s=timeout_s)
    cases = [Case(i, r.output, tag) for i, r in zip(inputs, res) if r.ok]
    dropped = [r.error[-200:] for r in res if not r.ok]
    return cases, Evidence(f"oracle_{tag}", bool(cases), len(cases), time.monotonic() - t0, {"dropped": len(dropped), "errors": dropped[:3]})

def run_candidate(problem, source: str, inputs: list, *, workdir: str, timeout_s: float, binary: str | None = None, overflow_checks: bool = True) -> list[CaseResult]:
    if problem.language == "python":
        return run_python_cases(source, problem.entrypoint, [tuple(i) for i in inputs], timeout_s=timeout_s, workdir=workdir)
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
        return Evidence(kind, True, 0, 0.0, {"skipped": "no cases"})
    res = run_candidate(problem, source, [c.input for c in cases], workdir=workdir, timeout_s=timeout_s, binary=binary)
    for i, (c, r) in enumerate(zip(cases, res)):
        if not same(problem, r, c.expected):
            return Evidence(kind, False, len(cases), time.monotonic() - t0,
                            {"index": i, "input": c.input, "expected": c.expected, "actual": r.output, "error": r.error[-600:], "timed_out": r.timed_out})
    return Evidence(kind, True, len(cases), time.monotonic() - t0)

def _fails(problem, source, oracle_src, inp, workdir, binary) -> tuple[bool, object]:
    exp = module_calls(oracle_src, "reference", [_ref_args(problem, inp)], workdir=os.path.join(workdir, "o"), timeout_s=5)[0]
    if not exp.ok: return False, None
    act = run_candidate(problem, source, [inp], workdir=os.path.join(workdir, "c"), timeout_s=5, binary=binary)[0]
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
                if time.monotonic() - t0 >= budget_s: break
                cand = cur[:i] + (alt,) + cur[i + 1 :]
                still, e = _fails(problem, source, oracle_src, cand, workdir, binary)
                if still:
                    cur, exp, improved = cand, e, True
                    break
            if improved: break
    return Case(cur, exp, case.tag + "+shrunk")

def prepare_gate_inputs(problem, oracle_src: str, stress_src: str, limits: dict, *, workdir: str, budget, log) -> GateInputs:
    gi = GateInputs(oracle_src=oracle_src)
    if not oracle_src.strip():
        gi.notes.append("no oracle source"); return gi
    t = budget.step_timeout(60.0, reserve_s=20.0)
    for mode, n in (("small", limits["cases_small"]), ("medium", limits["cases_medium"])):
        gen = module_calls(oracle_src, "gen", [(s, mode) for s in range(n)], workdir=os.path.join(workdir, f"gen_{mode}"), timeout_s=t)
        inputs = [r.output for r in gen if r.ok]
        if not inputs:
            gi.notes.append(f"gen({mode}) produced nothing: {(gen[0].error if gen else '')[-200:]}"); continue
        cases, ev = make_cases(problem, oracle_src, inputs, mode, workdir=os.path.join(workdir, f"ref_{mode}"), timeout_s=t)
        if not cases: gi.notes.append(f"reference failed on all {mode} inputs: {ev.detail['errors']}")
        setattr(gi, f"cases_{mode}", cases); log(f"oracle.{mode} cases={len(cases)} dropped={ev.detail['dropped']}")
    if stress_src.strip():
        edges = module_calls(stress_src + "\ndef _edges():\n    return list(EDGES)\n", "_edges", [()], workdir=os.path.join(workdir, "edges"), timeout_s=t)
        if edges and edges[0].ok and isinstance(edges[0].output, list):
            gi.cases_edge, ev = make_cases(problem, oracle_src, edges[0].output, "edge", workdir=os.path.join(workdir, "ref_edge"), timeout_s=t)
            log(f"oracle.edge cases={len(gi.cases_edge)} dropped={ev.detail['dropped']}")
        mx = module_calls(stress_src, "gen_max", [(1,)], workdir=os.path.join(workdir, "genmax"), timeout_s=t)
        if mx and mx[0].ok: gi.stress_input = mx[0].output
        else: gi.notes.append("gen_max failed: " + (mx[0].error[-200:] if mx else ""))
    else:
        gi.notes.append("no stress source")
    return gi

def run_gate(problem, source: str, gi: GateInputs, *, workdir: str, budget, limits: dict, log) -> list[Evidence]:
    ev: list[Evidence] = []
    binary = None
    if problem.language == "rust":
        t0 = time.monotonic(); binary, err = compile_rust(source, overflow_checks=True, workdir=workdir, timeout_s=budget.step_timeout(90.0))
        ev.append(Evidence("compile", binary is not None, 0, time.monotonic() - t0, {"stderr": err[-1500:]}))
        if binary is None: return ev
    t = budget.step_timeout(60.0, reserve_s=20.0)
    for kind, cases in (("diff_edge", gi.cases_edge), ("diff_small", gi.cases_small), ("diff_medium", gi.cases_medium)):
        e = differential(problem, source, cases, kind, workdir=os.path.join(workdir, kind), timeout_s=t, binary=binary)
        ev.append(e); log(f"gate.{kind} passed={e.passed} cases={e.cases}")
        if not e.passed: return ev
    return ev
```

- [ ] **Step 4: Run tests**

Run: `uv run --group dev pytest -q`
Expected: all PASS, including the Task 6 orchestrator tests (they now exercise the real gate with the scripted oracle).

- [ ] **Step 5: Commit**

```bash
git add verify.py tests/test_verify.py
git commit -m "feat: oracle-driven cases, differential testing, shrinking, gate assembly"
```

---

### Task 8: Stress gate and behavior checks

**Files:**
- Modify: `verify.py` (add `stress`, `behavior`, extend `run_gate`)
- Test: `tests/test_verify.py` (append)

**Interfaces:**
- Produces:
  - `stress(problem, source, stress_input, *, workdir, limit_s: float, mem_mb: int) -> Evidence` — Rust uses the `overflow_checks=False` build; detail has `duration_s`, `limit_s`, `timed_out`.
  - `behavior(problem, source, cases: list[Case], *, workdir, timeout_s, binary=None) -> Evidence` — checks mutation (python, from `CaseResult.mutated`), global state (python: run `[A, B, A]`, compare outputs 0 and 2), nondeterminism (both languages: run the first 10 cases in two separate processes, compare).
  - `run_gate` order becomes: `compile?`, `diff_edge`, `diff_small`, `diff_medium`, `behavior`, `stress`.

- [ ] **Step 1: Write failing tests (append)**

```python
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
    assert [e.kind for e in ev] == ["diff_edge", "diff_small", "diff_medium", "behavior", "stress"] and all(e.passed for e in ev)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --group dev pytest tests/test_verify.py -q -k "stress or behavior"`
Expected: FAIL `AttributeError: module 'verify' has no attribute 'stress'`

- [ ] **Step 3: Implement (append to `verify.py`, and extend `run_gate`)**

```python
def stress(problem, source: str, stress_input, *, workdir: str, limit_s: float, mem_mb: int) -> Evidence:
    if stress_input is None:
        return Evidence("stress", True, 0, 0.0, {"skipped": "no stress input"})
    t0 = time.monotonic()
    if problem.language == "python":
        r = run_python_cases(source, problem.entrypoint, [tuple(stress_input)], timeout_s=limit_s + 15.0, workdir=workdir)[0]  # +15 s covers import + arg parsing; function time is measured inside the harness
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
        return Evidence("behavior", True, 0, 0.0, {"skipped": "no cases"})
    inputs = [c.input for c in cases[:10]]
    if problem.language == "python":
        aba = run_candidate(problem, source, [inputs[0], inputs[-1], inputs[0]], workdir=os.path.join(workdir, "aba"), timeout_s=timeout_s)
        if any(r.mutated for r in aba):
            return Evidence("behavior", False, 3, time.monotonic() - t0, {"check": "mutation", "input": inputs[0]})
        if aba[0].ok and aba[2].ok and aba[0].output != aba[2].output:
            return Evidence("behavior", False, 3, time.monotonic() - t0, {"check": "global_state", "input": inputs[0], "first": aba[0].output, "again": aba[2].output})
    r1 = run_candidate(problem, source, inputs, workdir=os.path.join(workdir, "p1"), timeout_s=timeout_s, binary=binary)
    r2 = run_candidate(problem, source, inputs, workdir=os.path.join(workdir, "p2"), timeout_s=timeout_s, binary=binary)
    for i, (a, b) in enumerate(zip(r1, r2)):
        if a.ok and b.ok and (a.output if problem.language == "python" else tokens(a.output)) != (b.output if problem.language == "python" else tokens(b.output)):
            return Evidence("behavior", False, len(inputs), time.monotonic() - t0, {"check": "nondeterminism", "input": inputs[i], "run1": a.output, "run2": b.output})
    return Evidence("behavior", True, len(inputs), time.monotonic() - t0)
```
Extend `run_gate` after the differential loop:
```python
    e = behavior(problem, source, gi.cases_small or gi.cases_edge, workdir=os.path.join(workdir, "behavior"), timeout_s=t, binary=binary)
    ev.append(e); log(f"gate.behavior passed={e.passed} detail={e.detail.get('check','')}")
    if not e.passed: return ev
    limit = limits["stress_limit_python_s"] if problem.language == "python" else limits["stress_limit_rust_s"]
    e = stress(problem, source, gi.stress_input, workdir=os.path.join(workdir, "stress"), limit_s=min(limit, budget.step_timeout(limit, reserve_s=15.0)) or limit, mem_mb=limits["mem_mb"])
    ev.append(e); log(f"gate.stress passed={e.passed} duration={e.detail.get('duration_s')}")
    return ev
```

- [ ] **Step 4: Run tests**

Run: `uv run --group dev pytest -q`
Expected: all PASS. Update the Task 7 `test_prepare_gate_inputs_and_run_gate` assertion on kinds if it compares the full list (it uses `[:3]`, so it still passes).

- [ ] **Step 5: Commit**

```bash
git add verify.py tests/test_verify.py
git commit -m "feat: stress timing gate and behavior checks (mutation, global state, nondeterminism)"
```

---

### Task 9: Repair loop with shrink, adjudication, and oracle regeneration

**Files:**
- Modify: `verify.py` (real `repair`, `regenerate_oracle`)
- Test: `tests/test_solve.py` (append)

**Interfaces:**
- Produces:
  - `repair(run, cand, failed: Evidence, gi: GateInputs, pv: dict) -> tuple[str, str]` returns `(new_source, verdict)` with verdict in `{"candidate","oracle"}`. Shrinks the failing case first when `failed.kind.startswith("diff_")`, stores the shrunk case in `gi.regressions` (a list that `run_gate` prepends to `cases_edge`).
  - `regenerate_oracle(run, gi, failed, pv) -> GateInputs` — one FAST call to `oracle` prompt with an extra paragraph containing the disputed input and the model's trace; rebuilds all cases; increments `gi.oracle_regens`.
- Consumes: `Run.chat`, `Run.log`, `Candidate` (Task 6), `shrink`, `prepare_gate_inputs` (Task 7).

- [ ] **Step 1: Write failing tests (append to `tests/test_solve.py`)**

```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --group dev pytest tests/test_solve.py -q -k repair`
Expected: FAIL (stub `repair` returns empty source, so `final_candidate == "c1"` and status is not `passed_all_gates`).

- [ ] **Step 3: Implement (replace the stubs in `verify.py`)**

Add `regressions: list = field(default_factory=list)` to `GateInputs`, and in `run_gate` use `gi.regressions + gi.cases_edge` for `diff_edge`.

```python
from llm import parse_blocks

def _fmt(v) -> str:
    s = repr(v) if not isinstance(v, str) else v
    return s if len(s) <= 4000 else s[:4000] + " ...[truncated]"

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
    r = run.chat("strong", "repair", 0.25 * run.budget.usable_s, kind=failed.kind, input=_fmt(detail.get("input")), expected=_fmt(detail.get("expected")),
                 actual=_fmt(detail.get("actual")), details=_fmt({k: v for k, v in detail.items() if k not in ("input", "expected", "actual")}),
                 code=cand.source, **pv)
    blocks = parse_blocks(r.text)
    verdict = "oracle" if blocks.get("VERDICT", "").strip().lower().startswith("oracle") else "candidate"
    run.log(f"repair.verdict={verdict}")
    return blocks.get("CODE", ""), verdict

def regenerate_oracle(run, gi: GateInputs, failed: Evidence, pv: dict) -> GateInputs:
    extra = ("\n\nA previous reference was judged WRONG on this input; re-read the statement and follow it literally here:\n"
             f"Input: {_fmt(failed.detail.get('input'))}\nPrevious (wrong) expected: {_fmt(failed.detail.get('expected'))}\n")
    r = run.chat("fast", "oracle", 0.2 * run.budget.usable_s, **{**pv, "statement": pv["statement"] + extra})
    src = parse_blocks(r.text).get("ORACLE", "")
    if not src.strip():
        run.log("oracle.regen failed: empty"); gi.oracle_regens += 1; return gi
    new = prepare_gate_inputs(run.p, src, "", run.cfg["limits"], workdir=os.path.join(run.dir, "oracle2"), budget=run.budget, log=run.log)
    new.stress_input, new.cases_edge, new.oracle_regens, new.regressions = gi.stress_input, gi.cases_edge, gi.oracle_regens + 1, []
    # re-derive edge expectations with the new reference
    if gi.cases_edge:
        new.cases_edge, _ = make_cases(run.p, src, [c.input for c in gi.cases_edge], "edge", workdir=os.path.join(run.dir, "oracle2", "edge"), timeout_s=run.budget.step_timeout(30.0))
    run.log(f"oracle.regen small={len(new.cases_small)} medium={len(new.cases_medium)}")
    return new
```

- [ ] **Step 4: Run tests**

Run: `uv run --group dev pytest -q`
Expected: all PASS. If `test_repair_flow_fixes_planted_bug` fails on the prompt assertion, print `llm.prompts` and confirm the shrunk input is rendered into `{{input}}`.

- [ ] **Step 5: Real run on one Python and one Rust sample (OpenRouter, real deadline)**

```bash
uv run python solve.py samples/1dea328020722d9c3737e123e64dafc670e9bcbc8a9e0d3163eb6352fa2d9710.json -o runs/1dea.py
uv run python solve.py samples/5d02bd0e16ab63c540232452b60e8e6cf36d01e0820cb6b422b63c1a31091718.json -o runs/5d02.rs
```
Expected: both complete under 300 s, `runs/<id>/report.json` lists every evidence step, every model call with latency, and `cost_usd`. Read the log and note which gate step fails most; that is Task 10's prompt-tuning input.

- [ ] **Step 6: Commit**

```bash
git add verify.py tests/test_solve.py
git commit -m "feat: repair loop with shrinking, adjudication, and one-shot oracle regeneration"
```

---

### Task 10: Benchmark mode, README, first benchmark table

**Files:**
- Modify: `solve.py` (add `--bench`)
- Create: `README.md` → do **not** overwrite the assessment README; create `SOLVER.md` and link it from the top of `ChallengeBox-Agent-Architecture.md`.
- Test: `tests/test_solve.py` (append one test)

**Interfaces:**
- Produces: `bench(sample_dir: str, out_dir: str, cfg: dict, llm, deadline_scale: float) -> list[dict]` writes `out_dir/benchmark.md` with the §13 table from the architecture doc plus a cost column and a total-cost line, and returns the rows. CLI: `python solve.py --bench samples/ -o runs/bench`.

- [ ] **Step 1: Write failing test (append)**

```python
def test_bench_writes_table(tmp_path):
    sd = tmp_path / "samples"; sd.mkdir()
    (sd / "a.json").write_text(json.dumps({"problem_id": "aaaa", "language": "python", "statement": "s", "entrypoint": "add", "public_examples": [], "deadline_s": 300.0}))
    llm = FakeLLM({"solve": [SOLVE_OK], "oracle": [ORACLE_OK], "stress": [STRESS_OK]})
    rows = S.bench(str(sd), str(tmp_path / "bench"), cfg(), llm, 1.0)
    md = (tmp_path / "bench" / "benchmark.md").read_text()
    assert len(rows) == 1 and "| aaaa" in md and "unknown" in md   # hidden-test column is always unknown
```

- [ ] **Step 2: Implement `bench` and CLI flag in `solve.py`**

```python
def bench(sample_dir: str, out_dir: str, cfg: dict, llm, deadline_scale: float) -> list[dict]:
    os.makedirs(out_dir, exist_ok=True); rows = []
    for name in sorted(os.listdir(sample_dir)):
        if not name.endswith(".json"): continue
        p = Problem.load(os.path.join(sample_dir, name)); pid = p.problem_id[:12]
        rep = solve(p, llm, cfg, out_path=os.path.join(out_dir, f"{pid}.{'py' if p.language == 'python' else 'rs'}"), run_dir=os.path.join(out_dir, pid), deadline_scale=deadline_scale)
        ev = {e["kind"]: e["passed"] for e in rep["evidence"].get(rep["final_candidate"] or "", [])}
        mark = lambda k: {True: "pass", False: "FAIL"}.get(ev.get(k), "n/a")
        rows.append({"id": pid, "lang": p.language, "compiles": mark("compile") if p.language == "rust" else mark("static"), "edge": mark("diff_edge"), "small": mark("diff_small"),
                     "medium": mark("diff_medium"), "behavior": mark("behavior"), "stress": mark("stress"), "repairs": rep["repairs"], "calls": len(rep["calls"]),
                     "tokens": f"{rep['token_usage']['prompt']}/{rep['token_usage']['completion']}", "elapsed": rep["elapsed_s"], "cost": rep["cost_usd"], "status": rep["status"]})
    hdr = "| Problem | Lang | Compiles | Edge | Small | Medium | Behavior | Stress | Repairs | Calls | Tokens in/out | Elapsed s | Cost USD | Status | Hidden tests |\n|---|---|---|---|---|---|---|---|---:|---:|---|---:|---:|---|---|\n"
    body = "".join(f"| {r['id']} | {r['lang']} | {r['compiles']} | {r['edge']} | {r['small']} | {r['medium']} | {r['behavior']} | {r['stress']} | {r['repairs']} | {r['calls']} | {r['tokens']} | {r['elapsed']} | {r['cost'] if r['cost'] is not None else 'n/a'} | {r['status']} | unknown |\n" for r in rows)
    total = sum(r["cost"] or 0.0 for r in rows)
    note = f"\nprofile={cfg.get('profile')} deadline_scale={deadline_scale} total_cost_usd={total:.4f}. 'pass' = passed local gates; hidden-test status is unknown.\n"
    with open(os.path.join(out_dir, "benchmark.md"), "w", encoding="utf-8") as f: f.write(hdr + body + note)
    return rows
```
In `main`: add `ap.add_argument("--bench", action="store_true")`; when set, treat `problem` as a directory and `-o` as the output directory, call `bench`, print the table path, return 0.

- [ ] **Step 3: Run tests**

Run: `uv run --group dev pytest -q`
Expected: all PASS.

- [ ] **Step 4: Write `SOLVER.md`**

Contents (prose, keep under 120 lines): setup (`uv sync --group dev`, rustup, `export OPENROUTER_API_KEY=...`), the one command, where reports land, how to read `report.json`, the model table from the Context section with prices and the per-problem cost cap, the three README answers in three short paragraphs pointing to the architecture doc sections 7.1–7.3, how to swap models or add another OpenAI-compatible endpoint by editing `config.toml` only, and a "Limitations" list copied from architecture §14.

- [ ] **Step 5: Run the benchmark (needs `OPENROUTER_API_KEY`; expected total under $0.50)**

```bash
uv run python solve.py --bench samples/ -o runs/bench
```
Expected: `runs/bench/benchmark.md` exists with ten rows, every row has a cost, and the total-cost line is well under $1. Copy the table into `SOLVER.md`. Do not claim hidden-test results.

- [ ] **Step 6: Tune prompts from evidence (one pass)**

Read every `runs/bench/*/log.txt`. For the most common failing gate kind, adjust the corresponding prompt (`solve.md` trap checklist wording, `oracle.md` literalness instructions, or `stress.md` sizes). Re-run only the affected samples with `python solve.py samples/<id>.json ...`. Commit each prompt change separately with the observed before/after and cost in the message.

- [ ] **Step 7: Commit**

```bash
git add solve.py SOLVER.md tests/test_solve.py prompts/
git commit -m "feat: benchmark mode, solver README, first benchmark tables"
```

---

## Verification (end to end)

1. `uv run --group dev pytest -q` — all unit and integration tests pass (Rust tests must not be skipped). No test touches the network.
2. `uv run python solve.py samples/1dea3280…json -o runs/x.py` — exits 0 or 1, writes the solution and `runs/1dea32802072/report.json`, log shows every phase and the emit line before 285 s of wall time; `cost_usd` is present.
3. Deadline proof: `uv run python solve.py samples/1dea3280…json -o runs/y.py --deadline-scale 0.25` — with a 75 s budget the tool must still emit *something* or report `no_candidate` before ~60 s wall time; check `elapsed_s` in the report.
4. Planted-fault proof is covered by `test_repair_flow_fixes_planted_bug` and `test_stress_rejects_quadratic`.
5. `uv run python solve.py --bench samples/ -o runs/bench` — ten rows, every evidence column filled, cost column filled, total cost under $1, hidden-test column reads "unknown".

## Known deviations from the architecture doc

- Repository root is used instead of a `challengebox/` package directory; `tests/test_llm.py`, `tests/test_config.py`, and `tests/test_solve.py` exist in addition to the three named test files.
- Shrinking is implemented for Python arguments only; Rust failures use the smallest failing generated case as-is.
- The three generation calls run concurrently (`max_concurrent = 3` on OpenRouter); a per-endpoint semaphore still exists so a future profile with `max_concurrent = 1` serializes them in submission order (SOLVE first).
- The "settle" phase re-gate with a shorter batch is not implemented; the last full gate result stands. Cost is taken from `usage.cost` as returned by OpenRouter; there is no local price table, so a non-OpenRouter profile reports `cost_usd: null` and the cost cap does not bind.
- The Python recursion "smell" warning is not implemented; the max-size stress input is what catches recursion-limit crashes.
- Model ids were chosen from OpenRouter's live list on 2026-09-12; if either id is retired, change only `config.toml`.
