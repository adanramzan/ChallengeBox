"""Untrusted-code execution: process isolation, harnesses, static contract checks."""
from __future__ import annotations
import ast, hashlib, json, os, re, resource, signal, subprocess, sys, threading, time
from dataclasses import dataclass

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

def _reader(stream, cap: int, sink: list):
    kept = 0
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        if kept < cap:
            take = chunk[: cap - kept]; sink.append(take); kept += len(take)
    stream.close()

def _writer(stream, data: bytes):
    try:
        if data:
            stream.write(data)
    except (BrokenPipeError, OSError):
        pass
    finally:
        try: stream.close()
        except OSError: pass

def run_cmd(argv: list[str], *, stdin: bytes = b"", timeout_s: float, cwd: str | None = None,
            mem_mb: int = 4096, max_output: int = 1_000_000) -> RunResult:
    # HOME must be the real home for rustup/rustc toolchain discovery, not cwd
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.environ.get("HOME", "/tmp"), "LANG": "C.UTF-8"}
    t0 = time.monotonic()
    p = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd=cwd, env=env, start_new_session=True, preexec_fn=_limits(mem_mb))
    out_parts, err_parts = [], []
    out_thread = threading.Thread(target=_reader, args=(p.stdout, max_output, out_parts), daemon=True)
    err_thread = threading.Thread(target=_reader, args=(p.stderr, max_output, err_parts), daemon=True)
    stdin_thread = threading.Thread(target=_writer, args=(p.stdin, stdin), daemon=True)
    out_thread.start(); err_thread.start(); stdin_thread.start()
    timed_out = False
    try:
        p.wait(timeout=max(0.05, timeout_s))
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        p.wait()
    out_thread.join(timeout=5); err_thread.join(timeout=5); stdin_thread.join(timeout=1)
    return RunResult(p.returncode, b"".join(out_parts), b"".join(err_parts), time.monotonic() - t0, timed_out)

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
import ast, copy, importlib.util, signal, sys, time, traceback
spec = importlib.util.spec_from_file_location("cand", sys.argv[1]); mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod); fn = getattr(mod, sys.argv[2])
except Exception:
    print(repr({"ok": False, "error": "IMPORT: " + traceback.format_exc()[-1500:]}), flush=True); sys.exit(2)

# Per-case wall limit. Without it one pathological case consumes the WHOLE batch's timeout and
# every later case is reported as a timeout it never got to run -- and the caller waits the full
# grant to learn a verdict the first case already settled. 0 disables.
PER_CASE = float(sys.argv[4]) if len(sys.argv) > 4 else 0.0
# Consecutive per-case timeouts after which the rest of the batch is skipped unrun. A function that
# times out on N small inputs in a row is dead, not slow, and every further case costs the full
# per-case limit for nothing. 0 disables.
MAX_CONSEC = int(sys.argv[5]) if len(sys.argv) > 5 else 0
consec = 0
abandoned = ""
class _CaseTimeout(BaseException): pass   # BaseException, not Exception: a candidate's `except Exception:` must not be able to swallow its own wall limit
def _on_alarm(sig, frame): raise _CaseTimeout()
if PER_CASE > 0: signal.signal(signal.SIGALRM, _on_alarm)

for line in open(sys.argv[3], encoding="utf-8"):
    line = line.rstrip("\n")
    if not line: continue
    if abandoned:
        print(repr({"ok": False, "output": None, "error": abandoned, "duration_s": 0.0, "mutated": False}), flush=True)
        continue
    t0 = time.perf_counter()
    try:
        args = ast.literal_eval(line); before = copy.deepcopy(args)
        if PER_CASE > 0: signal.setitimer(signal.ITIMER_REAL, PER_CASE)
        # duration_s is the CANDIDATE's time, not the harness's. literal_eval plus deepcopy of a
        # max-size input costs 0.10 s on 50k elements and is the same for every candidate, so
        # leaving it inside made duration_s a measure of input size: a candidate doing no work at
        # all and one doing all of it read as 0.103 vs 0.105 s. The judge hands the function real
        # objects, so this setup is not part of what is being timed either.
        t0 = time.perf_counter()
        out = fn(*args); ok = True; err = ""
    except _CaseTimeout:
        args = before = None; out = None; ok = False; err = "case exceeded per-case limit of %gs" % PER_CASE
        consec += 1
        if MAX_CONSEC > 0 and consec >= MAX_CONSEC:
            abandoned = "skipped: batch abandoned after %d consecutive per-case timeouts" % MAX_CONSEC
    except BaseException:
        args = before = None; out = None; ok = False; err = traceback.format_exc()[-1500:]
        consec = 0
    else:
        consec = 0
    finally:
        if PER_CASE > 0: signal.setitimer(signal.ITIMER_REAL, 0)
    dt = time.perf_counter() - t0
    try:
        mutated = args != before
    except Exception:
        mutated = True
    try:
        line = repr({"ok": ok, "output": out, "error": err, "duration_s": dt, "mutated": mutated})
        ast.literal_eval(line)  # Verify it's a valid literal
    except Exception:
        try:
            line = repr({"ok": False, "output": None, "error": "unrepresentable result: " + type(out).__name__, "duration_s": dt, "mutated": mutated})
        except Exception:
            line = repr({"ok": False, "output": None, "error": "unrepresentable result: unknown", "duration_s": dt, "mutated": mutated})
    print(line, flush=True)
'''

def run_python_cases(source: str, entrypoint: str, args_list: list[tuple], *, timeout_s: float, workdir: str, mem_mb: int = 4096, per_case_s: float = 0.0, max_consec_timeouts: int = 0, max_output: int = 50_000_000) -> list[CaseResult]:
    # run_cmd sets cwd=workdir, so every path handed to the child must be absolute: a relative
    # workdir would otherwise be resolved a second time against itself and double the path.
    workdir = os.path.abspath(workdir)
    os.makedirs(workdir, exist_ok=True)
    cand = os.path.join(workdir, "cand.py"); harness = os.path.join(workdir, "harness.py"); cases = os.path.join(workdir, "cases.txt")
    with open(cand, "w", encoding="utf-8") as f: f.write(source)
    with open(harness, "w", encoding="utf-8") as f: f.write(_PY_HARNESS)
    with open(cases, "w", encoding="utf-8") as f:
        for a in args_list: f.write(repr(tuple(a)) + "\n")
    r = run_cmd([PYTHON, harness, cand, entrypoint, cases, repr(float(per_case_s)), str(int(max_consec_timeouts))], timeout_s=timeout_s, cwd=workdir, max_output=max_output, mem_mb=mem_mb)
    results: list[CaseResult] = []
    for line in r.stdout.decode("utf-8", "replace").splitlines():
        try:
            d = ast.literal_eval(line)
        except Exception:
            # A result whose repr overran the output cap is cut mid-literal; say so, rather than
            # "unparseable", so a gen_max that returned a 70 MB input is diagnosable from the note.
            results.append(CaseResult(False, error=f"harness output exceeded the {max_output >> 20} MB cap (result too large)" if len(r.stdout) >= max_output else "unparseable harness line"))
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
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            root = module.split(".")[0] if module else ""
            # from sys import ... is always forbidden
            if root == "sys":
                problems.append(f"forbidden import sys")
            elif root and root not in sys.stdlib_module_names:
                problems.append(f"non-stdlib import {module}")
            elif root in _PY_FORBIDDEN_MODULES:
                problems.append(f"forbidden import {module}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                # import sys as name is always forbidden
                if alias.asname is not None and alias.name == "sys":
                    problems.append(f"forbidden import sys as {alias.asname}")
                elif root not in sys.stdlib_module_names:
                    problems.append(f"non-stdlib import {alias.name}")
                elif root == "sys":
                    # import sys allowed only if used for setrecursionlimit/getrecursionlimit/maxsize
                    if not _only_setrecursionlimit(tree):
                        problems.append(f"forbidden import {alias.name}")
                elif root in _PY_FORBIDDEN_MODULES:
                    problems.append(f"forbidden import {alias.name}")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in _PY_FORBIDDEN_CALLS:
            problems.append(f"forbidden call {node.func.id}")
    return problems

def _only_setrecursionlimit(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "sys":
            if node.attr not in ("setrecursionlimit", "getrecursionlimit", "maxsize"):
                return False
    return True

def _normalize_rust(source: str) -> str:
    """Strip comments and literals, collapse whitespace around ::"""
    result = []
    i = 0
    while i < len(source):
        # Line comment
        if i + 1 < len(source) and source[i:i+2] == '//':
            while i < len(source) and source[i] != '\n':
                i += 1
            if i < len(source):
                result.append('\n')
                i += 1
            continue
        # Block comment
        if i + 1 < len(source) and source[i:i+2] == '/*':
            i += 2
            while i + 1 < len(source) and source[i:i+2] != '*/':
                if source[i] == '\n':
                    result.append('\n')
                i += 1
            if i + 1 < len(source) and source[i:i+2] == '*/':
                i += 2
            continue
        # String literal
        if source[i] == '"':
            result.append('"')
            i += 1
            while i < len(source) and source[i] != '"':
                if source[i] == '\\' and i + 1 < len(source):
                    i += 2
                else:
                    i += 1
            if i < len(source):
                result.append('"')
                i += 1
            continue
        # Char literal vs lifetime: only treat ' as char literal if followed by valid char pattern
        if source[i] == "'":
            # Check if this looks like a char literal: '\...' or 'X' where X is not quote/backslash
            is_char_lit = False
            if i + 1 < len(source):
                if source[i+1] == '\\':
                    # Escape form: '\x' where x is any char
                    if i + 3 < len(source) and source[i+3] == "'":
                        is_char_lit = True
                elif source[i+1] != "'" and source[i+1] != '\\':
                    # Single char form: 'X'
                    if i + 2 < len(source) and source[i+2] == "'":
                        is_char_lit = True

            if is_char_lit:
                # This is a char literal, strip its contents
                result.append("'")
                i += 1
                while i < len(source) and source[i] != "'":
                    if source[i] == '\\' and i + 1 < len(source):
                        i += 2
                    else:
                        i += 1
                if i < len(source):
                    result.append("'")
                    i += 1
            else:
                # This is a lifetime, just emit it
                result.append(source[i])
                i += 1
            continue
        result.append(source[i])
        i += 1
    normalized = ''.join(result)
    normalized = re.sub(r'\s*::\s*', '::', normalized)
    return normalized

def rust_static(source: str) -> list[str]:
    problems = []
    normalized = _normalize_rust(source)
    # Check for fn main() on normalized text so hidden main() in comments is caught
    if not re.search(r"\bfn\s+main\s*\(", normalized):
        problems.append("no fn main()")
    # Check for fully-qualified forbidden paths
    if re.search(r"\bunsafe\b", normalized):
        problems.append("forbidden: unsafe block")
    if re.search(r"\bextern\s+crate\b", normalized):
        problems.append("forbidden: extern crate")
    if re.search(r"std::fs\b", normalized):
        problems.append("forbidden: std::fs")
    if re.search(r"std::net\b", normalized):
        problems.append("forbidden: std::net")
    if re.search(r"std::process\b", normalized):
        problems.append("forbidden: std::process")
    if re.search(r"std::env\b", normalized):
        problems.append("forbidden: std::env")
    if re.search(r"use\s+std\s+as\b", normalized):
        problems.append("forbidden: use std as")

    # Check use statements: for each "use std...;" extract tokens and check if any is fs/net/process/env
    for use_match in re.finditer(r"use\s+std[^;]*;", normalized):
        use_stmt = use_match.group(0)
        # Split on non-identifier characters to get tokens
        tokens = re.split(r'[^a-zA-Z0-9_]+', use_stmt)
        for token in tokens:
            if token in ('fs', 'net', 'process', 'env'):
                if token == 'fs':
                    problems.append("forbidden: std::fs")
                elif token == 'net':
                    problems.append("forbidden: std::net")
                elif token == 'process':
                    problems.append("forbidden: std::process")
                elif token == 'env':
                    problems.append("forbidden: std::env")
                break  # Report only once per use statement

    return problems

def compile_rust(source: str, *, overflow_checks: bool, workdir: str, timeout_s: float = 90.0) -> tuple[str | None, str]:
    workdir = os.path.abspath(workdir)   # see run_python_cases: cwd=workdir makes relative paths double
    os.makedirs(workdir, exist_ok=True)
    tag = "gate" if overflow_checks else "stress"
    # Build argv first to include in cache key
    overflow_flag = f"overflow-checks={'on' if overflow_checks else 'off'}"
    argv = ["rustc", "--edition", "2021", "-O", "-C", overflow_flag, "-A", "warnings"]
    # Hash includes full argv (minus output file) plus source
    argv_key = "\x00".join(argv) + "\x00" + overflow_flag
    h = hashlib.sha256((argv_key + source).encode()).hexdigest()[:16]
    binp = os.path.join(workdir, f"bin_{tag}_{h}")
    if os.path.exists(binp):
        return binp, ""
    src = os.path.join(workdir, f"cand_{h}.rs")
    with open(src, "w", encoding="utf-8") as f: f.write(source)
    argv_full = argv + ["-o", binp, src]
    try:
        r = run_cmd(argv_full, timeout_s=timeout_s, cwd=workdir)
    except FileNotFoundError:
        return None, "rustc not found on PATH"
    err = r.stderr.decode("utf-8", "replace")
    if r.returncode != 0 or not os.path.exists(binp):
        return None, err if not r.timed_out else "rustc timed out"
    return binp, err

# rustc prints a suggested import either as a numbered insertion inside a help block
# ("1 + use std::io::Read;") or inline in backticks ("= help: ... `use std::io::Read;`").
_RUST_USE_SUGGESTION = re.compile(r"^\s*\d+\s*\+\s*(use\s+[^;\n]+;)|`(use\s+[^;\n]+;)`", re.M)

def rust_missing_imports(stderr: str, source: str) -> list[str]:
    """The `use ...;` lines rustc itself suggested, minus any the source already has."""
    src = re.sub(r"\s+", " ", source)
    out: list[str] = []
    for m in _RUST_USE_SUGGESTION.finditer(stderr):
        u = re.sub(r"\s+", " ", m.group(1) or m.group(2)).strip()
        if u not in src and u not in out:
            out.append(u)
    return out[:5]

def compile_rust_with_imports(source: str, *, overflow_checks: bool, workdir: str, timeout_s: float = 90.0) -> tuple[str | None, str, str]:
    """compile_rust, plus one retry with the imports rustc asked for. Two benchmark runs in a row
    lost a Rust candidate to nothing but a missing `use std::io::Read;`, which rustc names in its own
    help block -- harvesting it costs zero model tokens and one extra compile.

    Returns (binary, stderr, source_actually_compiled). The caller MUST keep the third value as the
    candidate's source: it is what the returned binary was built from, and what every later step
    (stress's unchecked rebuild, the emitted solution) has to use."""
    binary, err = compile_rust(source, overflow_checks=overflow_checks, workdir=workdir, timeout_s=timeout_s)
    if binary is not None:
        return binary, err, source
    uses = rust_missing_imports(err, source)
    if not uses:
        return None, err, source
    patched = "".join(u + "\n" for u in uses) + source
    binary2, err2 = compile_rust(patched, overflow_checks=overflow_checks, workdir=workdir, timeout_s=timeout_s)
    if binary2 is None:
        return None, err, source
    return binary2, "added missing imports: " + " ".join(uses) + "\n" + err2, patched

def run_rust_cases(binary: str, stdins: list[str], *, timeout_s: float, mem_mb: int = 4096, deadline_s: float | None = None) -> list[CaseResult]:
    binary = os.path.abspath(binary)   # cwd is derived from it below; a relative path would double
    out = []
    for s in stdins:
        # One process per case means the real ceiling is timeout_s * len(stdins): a hung candidate
        # on 200 cases burns 50s even when 10s remain. deadline_s (an absolute time.monotonic()) is
        # the hard ceiling on the batch -- cases past it are reported timed out without running.
        case_s = timeout_s if deadline_s is None else min(timeout_s, deadline_s - time.monotonic())
        if case_s <= 0:
            out.append(CaseResult(False, error="timeout", timed_out=True)); continue
        r = run_cmd([binary], stdin=s.encode("utf-8"), timeout_s=case_s, cwd=os.path.dirname(binary), mem_mb=mem_mb, max_output=50_000_000)
        ok = r.returncode == 0 and not r.timed_out
        out.append(CaseResult(ok, r.stdout.decode("utf-8", "replace"), "" if ok else r.stderr.decode("utf-8", "replace")[-1500:], r.duration_s, False, r.timed_out))
    return out

def tokens(s: str) -> list[str]:
    return s.split()
