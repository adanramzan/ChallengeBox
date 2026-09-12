import os, sys, time, ast, pytest, shutil
import sandbox
from sandbox import run_cmd, run_python_cases, python_static, Problem, rust_static, compile_rust, run_rust_cases, tokens

ALLOWED_OS_INJECTED = {"__CF_USER_TEXT_ENCODING"}  # macOS injects this into every child; not inherited from our env dict

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

def test_run_cmd_caps_output_both_streams(tmp_path):
    code = "import sys; sys.stdout.write('o'*50_000_000); sys.stderr.write('e'*50_000_000)"
    r = run_cmd([sys.executable, "-c", code], timeout_s=10, cwd=str(tmp_path), max_output=10_000)
    assert len(r.stdout) <= 10_000 and len(r.stderr) <= 10_000

def test_run_cmd_times_out_even_when_child_ignores_large_stdin(tmp_path):
    # Child ignores stdin but sleeps 10s; timeout is 1.0s; must return in <4s with timed_out=True
    t0 = time.monotonic()
    r = run_cmd([sys.executable, "-c", "import time; time.sleep(10)"], stdin=b"x"*50_000_000, timeout_s=1.0, cwd=str(tmp_path))
    elapsed = time.monotonic() - t0
    assert r.timed_out and elapsed < 4

def test_run_cmd_delivers_stdin(tmp_path):
    # Child reads stdin and prints its length
    code = "import sys; print(len(sys.stdin.buffer.read()))"
    r = run_cmd([sys.executable, "-c", code], stdin=b"x"*5_000_000, timeout_s=10, cwd=str(tmp_path))
    assert r.stdout.strip() == b"5000000"

def test_run_cmd_env_is_clean(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_KEY", "leak")
    r = run_cmd([sys.executable, "-c", "import os; print(sorted(os.environ))"], timeout_s=5, cwd=str(tmp_path))
    env_list = ast.literal_eval(r.stdout.decode())
    assert sorted(set(env_list) - ALLOWED_OS_INJECTED) == ["HOME", "LANG", "PATH"]

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

def test_run_python_cases_unrepresentable_result(tmp_path):
    src = "def f(x):\n    return (i for i in range(x))\n"
    res = run_python_cases(src, "f", [(2,), (3,)], timeout_s=10, workdir=str(tmp_path))
    # Both results should have ok=False due to unrepresentable output (generator)
    assert not res[0].ok and "unrepresentable" in res[0].error
    assert not res[1].ok and ("unrepresentable" in res[1].error or "unparseable" in res[1].error)
    assert len(res) == 2

def test_python_static_rules():
    assert python_static("def solve(x):\n    return x\n", "solve") == []
    assert any("entrypoint" in v for v in python_static("def other(x):\n    return x\n", "solve"))
    assert any("import" in v for v in python_static("import numpy\ndef solve(x):\n    return x\n", "solve"))
    assert any("open" in v for v in python_static("def solve(x):\n    return open('f')\n", "solve"))
    assert any("print" in v for v in python_static("def solve(x):\n    print(x)\n", "solve"))
    assert any("syntax" in v for v in python_static("def solve(x:\n", "solve"))

def test_python_static_sys_bypass():
    assert any("sys" in v for v in python_static("from sys import exit\ndef solve(x):\n    exit(1)\n", "solve"))
    assert any("sys" in v for v in python_static("import sys as s\ndef solve(x):\n    return s.maxsize\n", "solve"))
    assert python_static("import sys\nsys.setrecursionlimit(10**6)\ndef solve(x):\n    return x\n", "solve") == []
    assert any("forbidden import" in v for v in python_static("import os\ndef solve(x):\n    return x\n", "solve"))

def test_python_static_from_forbidden_imports():
    # from os import path should be forbidden (os is in forbidden modules)
    assert any("forbidden" in v for v in python_static("from os import path\ndef solve(x):\n    return x\n", "solve"))
    # from subprocess import Popen should be forbidden
    assert any("forbidden" in v for v in python_static("from subprocess import Popen\ndef solve(x):\n    return x\n", "solve"))
    # from os.path import join should be forbidden (root is os)
    assert any("forbidden" in v for v in python_static("from os.path import join\ndef solve(x):\n    return x\n", "solve"))
    # from collections.abc import Mapping should be allowed (collections not forbidden)
    assert python_static("from collections.abc import Mapping\ndef solve(x):\n    return x\n", "solve") == []

def test_problem_load_validates(tmp_path):
    p = tmp_path / "p.json"
    p.write_text('{"problem_id":"a","language":"python","statement":"s","entrypoint":"f","public_examples":[],"deadline_s":300.0}')
    assert Problem.load(str(p)).entrypoint == "f"
    p.write_text('{"problem_id":"a","language":"go","statement":"s","entrypoint":"f","public_examples":[],"deadline_s":300.0}')
    with pytest.raises(ValueError):
        Problem.load(str(p))

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
    # Verify they have different binary paths (different hashes due to overflow flag)
    assert gate != stress

@needs_rustc
def test_compile_cache_key_includes_flags(tmp_path):
    # Compile same source with different flags, verify different cache entries
    src = 'fn main(){println!("ok");}'
    binp1, _ = compile_rust(src, overflow_checks=True, workdir=str(tmp_path))
    binp2, _ = compile_rust(src, overflow_checks=False, workdir=str(tmp_path))
    # Different flags should produce different paths (because hash includes flags)
    assert binp1 != binp2
    # Both should exist
    assert os.path.exists(binp1) and os.path.exists(binp2)

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

def test_rust_static_defeats_import_grouping():
    # use std::{fs} should be flagged
    assert any("fs" in v for v in rust_static("use std::{fs}; fn main(){}"))
    # use std::env should be flagged
    assert any("env" in v for v in rust_static("use std::env; fn main(){}"))
    # use std::process should be flagged
    assert any("process" in v for v in rust_static("use std::process; fn main(){}"))

def test_rust_static_defeats_spaced_paths():
    # std :: fs with spaces should be flagged
    assert any("fs" in v for v in rust_static("fn main(){ std :: fs :: write(\"x\",\"y\"); }"))

def test_rust_static_ignores_comments():
    # Comment containing "unsafe" should not be flagged
    assert rust_static("// this is unsafe? no.\nfn main(){}") == []

def test_rust_static_ignores_string_contents():
    # String containing "unsafe" should not be flagged
    assert rust_static('fn main(){ println!("unsafe"); }') == []

def test_rust_static_flags_std_alias():
    # use std as should be flagged
    assert any("std as" in v for v in rust_static("use std as s; fn main(){}"))

def test_rust_static_allows_standard_io():
    # Standard I/O for stdin/stdout should be allowed
    result = rust_static("use std::io::{self, Read, Write};\nfn main(){}")
    assert result == []

def test_rust_static_lifetimes_with_forbidden_paths():
    # Lifetime scanner bug: this should flag std::fs::write, not eat it
    src = "fn f<'a>(x: &'a str) -> &'a str { x } fn main(){ std::fs::write(\"x\",\"y\"); }"
    result = rust_static(src)
    assert any("fs" in v for v in result), f"Expected fs forbidden but got {result}"

def test_rust_static_lifetimes_clean():
    # Multiple lifetimes should not cause issues
    src = "fn f<'a, 'b>(x: &'a str, y: &'b str) -> &'a str { x } fn main(){}"
    result = rust_static(src)
    assert result == [], f"Expected clean but got {result}"

def test_rust_static_char_literal_escaped():
    # Escaped quotes in char literals should not break
    src = "fn main(){ let c = '\\''; let _ = c; }"
    result = rust_static(src)
    assert result == [], f"Expected clean but got {result}"

def test_rust_static_char_literal_simple():
    # Simple char literals should be handled
    src = "fn main(){ let c = 'x'; let _ = c; }"
    result = rust_static(src)
    assert result == [], f"Expected clean but got {result}"

def test_rust_static_nested_use_groups_with_fs():
    # Nested use groups: fs should be flagged even though it's nested
    src = "use std::{io::{self, Read}, fs}; fn main(){}"
    result = rust_static(src)
    assert any("fs" in v for v in result), f"Expected fs forbidden but got {result}"

def test_rust_static_nested_use_groups_flat():
    # Nested use groups without forbidden modules should be clean
    src = "use std::{io::{self, Read}, collections::BTreeMap}; fn main(){}"
    result = rust_static(src)
    assert result == [], f"Expected clean but got {result}"

def test_rust_static_multiple_use_statements():
    # Multiple use statements, should detect forbidden in any of them
    src = "use std::collections::{BTreeMap, HashMap};\nuse std::io::Read;\nfn main(){}"
    result = rust_static(src)
    assert result == [], f"Expected clean but got {result}"

def test_rust_static_use_with_similar_name():
    # Token "environment" contains "env" but is not the token "env"
    src = "use std::io::Read; fn main(){ let environment = 1; let _ = environment; }"
    result = rust_static(src)
    assert result == [], f"Expected clean but got {result}"

def test_rust_static_fn_main_in_comment_only():
    # fn main() hidden in comment should be flagged
    src = "// fn main(){}\nfn helper(){}"
    result = rust_static(src)
    assert any("main" in v for v in result), f"Expected no fn main but got {result}"
