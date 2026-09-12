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
