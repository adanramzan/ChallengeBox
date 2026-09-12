import json, threading, time, pathlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    active = 0; max_active = 0; requests = 0; lock = threading.Lock(); delay = 0.3; served_model = None
    def do_POST(self):
        n = int(self.headers["Content-Length"]); body = json.loads(self.rfile.read(n))
        with _Handler.lock:
            _Handler.active += 1; _Handler.max_active = max(_Handler.max_active, _Handler.active)
            _Handler.requests += 1
        time.sleep(_Handler.delay)
        with _Handler.lock: _Handler.active -= 1
        resp = {"choices": [{"message": {"role": "assistant", "content": "", "reasoning_content": "===CODE===\nprint(1)\n===END==="}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "model": _Handler.served_model or body["model"]}
        data = json.dumps(resp).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, *a): pass

@pytest.fixture
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler); th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()   # threaded, so the semaphore is what serializes
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
        # Drain the abandoned worker before returning: with fix 1 the semaphore
        # slot stays held until it actually finishes, so a second call on the
        # same LLM blocks until then — avoiding a zombie thread that would
        # otherwise bleed into later tests via the shared _Handler class state.
        r2 = llm.chat("strong", "s", "u", timeout_s=10)
        assert r2.error is None
    finally:
        _Handler.delay = 0.3

def test_timeout_releases_slot_only_after_worker_finishes(server):
    # Fix 1: the semaphore slot must stay held until the abandoned worker's
    # request actually finishes, not just until join() times out.
    _Handler.delay = 2.0
    _Handler.max_active = 0
    try:
        role = Role("strong", server, "m", "", 100, 1, 30.0, {})
        llm = LLM({"strong": role})
        r1 = llm.chat("strong", "s", "u", timeout_s=0.3)
        assert r1.error == "timeout"
        t2 = threading.Thread(target=lambda: llm.chat("strong", "s", "u", timeout_s=10))
        t2.start(); t2.join()
        assert _Handler.max_active == 1
    finally:
        _Handler.delay = 0.3

def test_chat_zero_timeout_skips_request(server):
    # Fix 4: a non-positive timeout must not bill the endpoint at all.
    role = Role("strong", server, "m", "", 100, 1, 30.0, {})
    llm = LLM({"strong": role})
    before = _Handler.requests
    r = llm.chat("strong", "s", "u", timeout_s=0)
    assert r.error == "timeout" and _Handler.requests == before

def test_shared_endpoint_uses_min_max_concurrent(server):
    # Fix 5: two roles pointed at the same (base_url, model) must share a
    # semaphore sized to the smaller max_concurrent, not the first one seen.
    _Handler.max_active = 0
    role_a = Role("a", server, "m", "", 100, 5, 30.0, {})
    role_b = Role("b", server, "m", "", 100, 1, 30.0, {})
    llm = LLM({"a": role_a, "b": role_b})
    ts = [threading.Thread(target=lambda: llm.chat("a", "s", "u", timeout_s=10)) for _ in range(2)]
    ts.append(threading.Thread(target=lambda: llm.chat("b", "s", "u", timeout_s=10)))
    [t.start() for t in ts]; [t.join() for t in ts]
    assert _Handler.max_active == 1

def test_parse_blocks_last_occurrence_wins():
    # Fix 2: a repeated block name keeps the LAST occurrence (a thinking
    # model's draft block can precede its final one).
    t = "===CODE===\ndraft\n===END===\n===CODE===\nfinal\n===END===\n"
    assert parse_blocks(t)["CODE"] == "final"

def test_parse_blocks_fence_with_leading_prose():
    t = "===CODE===\nHere's the fix:\n```python\nx = 1\n```\n===END===\n"
    assert parse_blocks(t)["CODE"] == "x = 1"

def test_parse_blocks_fence_with_trailing_prose():
    t = "===CODE===\n```python\nx = 1\n```\nHope that helps!\n===END===\n"
    assert parse_blocks(t)["CODE"] == "x = 1"

def test_parse_blocks_fence_picks_longest():
    t = ("===CODE===\n```python\nshort\n```\nmiddle text\n"
         "```python\nlonger_body_here\n```\n===END===\n")
    assert parse_blocks(t)["CODE"] == "longer_body_here"

def test_parse_blocks_no_fence_unchanged():
    t = "===CODE===\nplain text no fence\n===END===\n"
    assert parse_blocks(t)["CODE"] == "plain text no fence"

def test_calls_record_the_served_model_not_the_requested_slug(server):
    # Minor fix: `calls` used to record the requested model slug (Role.model) unconditionally; it
    # should record what the provider actually served (Reply.model / response "model" field).
    role = Role("strong", server, "requested/slug", "", 100, 1, 30.0, {})
    llm = LLM({"strong": role})
    _Handler.served_model = "actually/served-model"
    try:
        r = llm.chat("strong", "s", "u", timeout_s=10)
    finally:
        _Handler.served_model = None
    assert r.model == "actually/served-model"
    assert llm.calls[-1]["model"] == "actually/served-model"

def test_fake_llm_scripts_by_tag_then_role():
    f = FakeLLM({"strong": ["one", "two"], "oracle": ["orc"]})
    assert f.chat("fast", "", "", timeout_s=1, tag="oracle").text == "orc"
    assert f.chat("strong", "", "", timeout_s=1, tag="solve").text == "one"   # no 'solve' key: falls back to role
    assert f.chat("strong", "", "", timeout_s=1).text == "two"
    with pytest.raises(AssertionError):
        f.chat("strong", "", "", timeout_s=1)
    assert f.calls[0]["tag"] == "oracle"
