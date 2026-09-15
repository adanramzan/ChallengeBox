import json, threading, time, pathlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest
from llm import LLM, Role, FakeLLM, parse_blocks, terminated_blocks, salvageable, load_config, pick_profile

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
    assert isinstance(cfg["roles"]["strong"].extra, dict)
    with pytest.raises(KeyError):
        load_config(str(ROOT / "config.toml"), "nope")
    assert load_config(str(ROOT / "config.toml"), "openai")["roles"]["strong"].token_param == "max_completion_tokens"
    assert load_config(str(ROOT / "config.toml"), "anthropic")["roles"]["strong"].omit_temperature

def test_pick_profile_takes_first_profile_whose_keys_are_all_set(tmp_path, monkeypatch):
    c = tmp_path / "c.toml"
    c.write_text('[profiles.a.strong]\napi_key_env = "KEY_A"\n[profiles.b.strong]\napi_key_env = "KEY_B"\n[profiles.b.fast]\napi_key_env = "KEY_B"\n')
    monkeypatch.delenv("KEY_A", raising=False); monkeypatch.delenv("KEY_B", raising=False)
    assert pick_profile(str(c)) == "a"   # nothing set: the first, so the CLI names its key
    monkeypatch.setenv("KEY_B", "x")
    assert pick_profile(str(c)) == "b"
    monkeypatch.setenv("KEY_A", "x")
    assert pick_profile(str(c)) == "a"

class _Handler(BaseHTTPRequestHandler):
    active = 0; max_active = 0; requests = 0; lock = threading.Lock(); delay = 0.3; served_model = None; last_body = None
    def do_POST(self):
        n = int(self.headers["Content-Length"]); body = json.loads(self.rfile.read(n)); _Handler.last_body = body
        with _Handler.lock:
            _Handler.active += 1; _Handler.max_active = max(_Handler.max_active, _Handler.active)
            _Handler.requests += 1
        time.sleep(_Handler.delay)
        with _Handler.lock: _Handler.active -= 1
        model = _Handler.served_model or body["model"]
        if body.get("stream"):
            # Same answer, delivered as SSE: content empty, the text in the reasoning field, usage
            # only on the final chunk (what stream_options.include_usage asks for).
            self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
            for piece in ("===CODE===\n", "print(1)\n", "===END==="):
                self._sse({"model": model, "choices": [{"delta": {"content": "", "reasoning": piece}}]})
            self._sse({"model": model, "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
            self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()
            return
        resp = {"choices": [{"message": {"role": "assistant", "content": "", "reasoning_content": "===CODE===\nprint(1)\n===END==="}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}, "model": model}
        data = json.dumps(resp).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def _sse(self, obj):
        self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n"); self.wfile.flush()
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

def test_chat_token_param_and_omit_temperature(server):
    role = Role("strong", server, "m", "", 100, 1, 30.0, {}, token_param="max_completion_tokens", omit_temperature=True)
    llm = LLM({"strong": role})
    llm.chat("strong", "s", "u", timeout_s=10, max_tokens=42)
    b = _Handler.last_body
    assert b["max_completion_tokens"] == 42 and "max_tokens" not in b and "temperature" not in b
    assert llm.calls[-1]["max_tokens"] == 42

def test_chat_computes_cost_from_prices_when_provider_omits_it(server):
    priced = Role("strong", server, "m", "", 100, 1, 30.0, {}, price_in_per_m=2.0, price_out_per_m=10.0)
    r = LLM({"strong": priced}).chat("strong", "s", "u", timeout_s=10)
    assert r.usage["cost"] == pytest.approx((10 * 2.0 + 5 * 10.0) / 1e6)
    # No prices: cost stays absent, so the report says "unknown" rather than a false $0.
    unpriced = Role("strong", server, "m", "", 100, 1, 30.0, {})
    assert "cost" not in LLM({"strong": unpriced}).chat("strong", "s", "u", timeout_s=10).usage

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

# An unclosed fence: the model opened ```python inside the block and closed it after ===END===, so
# the marker line landed in the candidate and every such candidate failed static (round 13, both runs).
def test_parse_blocks_strips_an_unclosed_leading_fence():
    t = "===CODE===\n```python\nx = 1\n===END===\n"
    assert parse_blocks(t)["CODE"] == "x = 1"

def test_parse_blocks_strips_an_unmatched_trailing_fence():
    t = "===CODE===\nx = 1\n```\n===END===\n"
    assert parse_blocks(t)["CODE"] == "x = 1"

def test_parse_blocks_strips_both_unmatched_fence_lines():
    # not a matched pair: the opener has a language tag and no closer of its own before the body ends
    assert parse_blocks("===CODE===\n```py\n```\n===END===\n")["CODE"] == ""

def test_parse_blocks_keeps_a_code_line_that_merely_starts_with_backticks():
    t = "===CODE===\ns = '```not a fence'\nx = 1\n===END===\n"
    assert parse_blocks(t)["CODE"] == "s = '```not a fence'\nx = 1"

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


class _FlakyHandler(_Handler):
    """Fails the first `fail_first` requests with `code`, then serves normally."""
    fail_first = 0; code = 503
    def do_POST(self):
        with _Handler.lock:
            _Handler.requests += 1; n = _Handler.requests
        if n <= _FlakyHandler.fail_first:
            self.send_response(_FlakyHandler.code); self.send_header("Content-Length", "0"); self.end_headers(); return
        _Handler.requests -= 1   # let the parent count the served request itself
        super().do_POST()

@pytest.fixture
def flaky():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FlakyHandler); th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
    yield f"http://127.0.0.1:{srv.server_port}/v1"; srv.shutdown()

def test_chat_retries_5xx_then_succeeds(flaky):
    _Handler.requests = 0; _FlakyHandler.fail_first = 2; _FlakyHandler.code = 503; _Handler.delay = 0.0
    role = Role("fast", flaky, "m", "", 100, 1, 30.0, {}, transport_retries=2, transport_backoff_s=0.01)
    reply = LLM({"fast": role}).chat("fast", "s", "u", timeout_s=30.0)
    assert reply.error is None and reply.text.startswith("===CODE===")
    assert _Handler.requests == 3

def test_chat_does_not_retry_4xx(flaky):
    _Handler.requests = 0; _FlakyHandler.fail_first = 5; _FlakyHandler.code = 403
    role = Role("fast", flaky, "m", "", 100, 1, 30.0, {}, transport_retries=2, transport_backoff_s=0.01)
    reply = LLM({"fast": role}).chat("fast", "s", "u", timeout_s=30.0)
    assert reply.error.startswith("http 403") and _Handler.requests == 1

def test_chat_retries_transport_error_then_gives_up():
    # Nothing listens on this port: every attempt is a refused connection.
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler); port = srv.server_port; srv.server_close()
    role = Role("fast", f"http://127.0.0.1:{port}/v1", "m", "", 100, 1, 30.0, {}, transport_retries=2, transport_backoff_s=0.01)
    llm = LLM({"fast": role}); t0 = time.monotonic()
    reply = llm.chat("fast", "s", "u", timeout_s=30.0)
    assert reply.error.startswith("transport:") and time.monotonic() - t0 < 5
    assert len(llm.calls) == 1   # one logical call, however many attempts it took

def test_chat_no_retry_when_timeout_has_no_room(flaky):
    _Handler.requests = 0; _FlakyHandler.fail_first = 5; _FlakyHandler.code = 503
    role = Role("fast", flaky, "m", "", 100, 1, 30.0, {}, transport_retries=2, transport_backoff_s=0.01)
    reply = LLM({"fast": role}).chat("fast", "s", "u", timeout_s=4.0)   # backoff + 5 s floor exceeds it
    assert reply.error.startswith("http 503") and _Handler.requests == 1


# --- round 14: streamed replies, and a connection that stalls mid-answer ---

class _StallHandler(_Handler):
    """Sends the SSE headers and one chunk, then holds the connection open saying nothing."""
    requests = 0
    def do_POST(self):
        json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with _Handler.lock:
            _StallHandler.requests += 1
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        self._sse({"choices": [{"delta": {"content": "partial"}}]})
        time.sleep(5)   # longer than any stall_timeout_s the tests use

@pytest.fixture
def staller():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _StallHandler); th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
    _StallHandler.requests = 0
    yield f"http://127.0.0.1:{srv.server_port}/v1"; srv.shutdown()

def test_streamed_reply_accumulates_content_reasoning_and_usage(server):
    role = Role("strong", server, "m", "", 100, 1, 30.0, {})
    assert role.stream   # streaming is the default
    r = LLM({"strong": role}).chat("strong", "s", "u", timeout_s=10)
    b = _Handler.last_body
    assert b["stream"] is True and b["stream_options"] == {"include_usage": True}
    assert r.error is None and r.content == "" and r.text == "===CODE===\nprint(1)\n===END==="
    assert r.usage["prompt_tokens"] == 10 and r.usage["completion_tokens"] == 5

def test_a_role_can_opt_out_of_streaming(server):
    role = Role("strong", server, "m", "", 100, 1, 30.0, {}, stream=False)
    r = LLM({"strong": role}).chat("strong", "s", "u", timeout_s=10)
    b = _Handler.last_body
    assert b["stream"] is False and "stream_options" not in b
    assert r.error is None and "===CODE===" in r.text

def test_config_carries_stream_and_stall_timeout_per_role():
    cfg = load_config(str(ROOT / "config.toml"), "openrouter")
    assert cfg["roles"]["strong"].stream and cfg["roles"]["strong"].stall_timeout_s > 0

def test_a_stalled_stream_aborts_and_is_retried(staller):
    role = Role("fast", staller, "m", "", 100, 1, 30.0, {}, transport_retries=1, transport_backoff_s=0.01, stall_timeout_s=0.5)
    t0 = time.monotonic()
    reply = LLM({"fast": role}).chat("fast", "s", "u", timeout_s=20.0)
    # the stall is a transport error, so the existing retry fires; both attempts stall out
    assert reply.error.startswith("transport:") and _StallHandler.requests == 2
    assert time.monotonic() - t0 < 10   # aborted on the stall, not on the call's own timeout

def test_a_stall_that_exhausts_retries_returns_an_error_inside_the_call_timeout(staller):
    role = Role("fast", staller, "m", "", 100, 1, 30.0, {}, transport_retries=0, transport_backoff_s=0.01, stall_timeout_s=0.4)
    t0 = time.monotonic()
    reply = LLM({"fast": role}).chat("fast", "s", "u", timeout_s=20.0)
    assert reply.error.startswith("transport:") and _StallHandler.requests == 1
    assert time.monotonic() - t0 < 20.0 and not reply.text


def test_tag_extra_reaches_the_request_body_only_for_the_mapped_tag(server):
    # A thinking model cannot finish a repair at the effort its first solve used, and the stress call
    # can afford a cheaper one because it is off the critical path. Which knob that is stays in
    # config; llm.py only knows that a tag may carry its own extra.
    role = Role("strong", server, "m", "", 100, 1, 30.0, {"reasoning": {"effort": "medium"}},
                tag_extra={"repair": {"reasoning": {"effort": "low"}},
                           "solve_fresh": {"reasoning": {"effort": "low"}},
                           "stress": {"reasoning": {"effort": "minimal"}}})
    llm = LLM({"strong": role})
    llm.chat("strong", "s", "u", timeout_s=10, tag="solve")
    assert _Handler.last_body["reasoning"] == {"effort": "medium"}   # unmapped tag: the role's own extra
    llm.chat("strong", "s", "u", timeout_s=10, tag="repair")
    assert _Handler.last_body["reasoning"] == {"effort": "low"}
    llm.chat("strong", "s", "u", timeout_s=10, tag="solve_fresh")
    assert _Handler.last_body["reasoning"] == {"effort": "low"}
    llm.chat("strong", "s", "u", timeout_s=10, tag="stress")
    assert _Handler.last_body["reasoning"] == {"effort": "minimal"}
    # a role that sets no tag_extra at all sends exactly what it always sent
    plain = LLM({"strong": Role("strong", server, "m", "", 100, 1, 30.0, {"reasoning": {"effort": "medium"}})})
    plain.chat("strong", "s", "u", timeout_s=10, tag="repair")
    assert _Handler.last_body["reasoning"] == {"effort": "medium"}

def test_config_gives_the_thinking_role_per_tag_efforts_and_a_repair_cap():
    strong = load_config(str(ROOT / "config.toml"), "openrouter")["roles"]["strong"]
    assert strong.tag_extra == {"oracle": {"reasoning": {"effort": "low"}},
                                "stress": {"reasoning": {"effort": "low"}},
                                "repair": {"reasoning": {"effort": "low"}},
                                "solve_fresh": {"reasoning": {"effort": "low"}}}
    assert strong.repair_cap_s == 120.0
    assert load_config(str(ROOT / "config.toml"), "openai")["roles"]["strong"].tag_extra == {}

def test_config_carries_the_prompt_to_role_mapping():
    cfg = load_config(str(ROOT / "config.toml"), "openrouter")
    # the oracle moved to the strong role: in bench19 and bench20 the qwen-authored oracle was the
    # primary or the only remaining cause of the run shipping unverified.
    assert cfg["prompt_roles"] == {"solve": "strong", "oracle": "strong", "stress": "strong", "repair": "strong"}
    assert set(cfg["prompt_roles"].values()) <= set(cfg["roles"])   # every mapped role exists in the profile


# --- round 14 batch 6: a timed-out stream is salvaged when its CODE block closed ---

class _PartialHandler(_Handler):
    """Streams a ===CODE=== block and then goes quiet, never sending [DONE]. With close_code the
    block is terminated first and the stream stops inside ===TRAPS===; without it the cut lands
    inside the code itself."""
    requests = 0; close_code = True; fail_lookup = False; lookups = 0
    def do_POST(self):
        json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with _Handler.lock:
            _PartialHandler.requests += 1
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        pieces = ["===CODE===\n", "def add(a, b):\n    return a + b\n"]
        pieces += ["===END===\n", "===TRAPS===\nwatch out for over"] if _PartialHandler.close_code else ["    # more to come"]
        for piece in pieces:
            self._sse({"id": "gen-abc", "model": "m", "choices": [{"delta": {"content": piece}}]})
        time.sleep(4)   # longer than any timeout_s the tests below use
    def do_GET(self):
        # the provider's generation-metadata endpoint: what the cut-off call actually cost
        with _Handler.lock:
            _PartialHandler.lookups += 1
        if _PartialHandler.fail_lookup:
            self.send_response(500); self.send_header("Content-Length", "0"); self.end_headers(); return
        assert "id=gen-abc" in self.path and self.headers.get("Authorization") == "Bearer k"
        body = json.dumps({"data": {"id": "gen-abc", "total_cost": 0.42,
                                    "tokens_prompt": 100, "tokens_completion": 2000}}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

@pytest.fixture
def partial_server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _PartialHandler); th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
    _PartialHandler.requests = 0; _PartialHandler.close_code = True
    _PartialHandler.fail_lookup = False; _PartialHandler.lookups = 0
    yield f"http://127.0.0.1:{srv.server_port}/v1"
    srv.shutdown(); _PartialHandler.close_code = True; _PartialHandler.fail_lookup = False

def test_a_timed_out_stream_keeps_what_it_streamed(partial_server):
    role = Role("strong", partial_server, "m", "", 100, 3, 30.0, {}, transport_retries=0,
                price_out_per_m=10.0, stall_timeout_s=10.0)
    llm = LLM({"strong": role})
    before = set(threading.enumerate())
    r = llm.chat("strong", "s", "u", timeout_s=1.0)
    assert r.error == "timeout" and r.partial and salvageable(r)
    assert terminated_blocks(r.text) == {"CODE"} and parse_blocks(r.text)["CODE"] == "def add(a, b):\n    return a + b"
    rec = llm.calls[-1]
    assert rec["timed_out"] and rec["partial"] and rec["estimated"] is True
    # no usage chunk arrived, so the tokens are estimated from the buffer and priced from the role
    assert rec["usage"]["completion_tokens"] == len(r.text) // 4 and rec["usage"]["cost"] > 0
    # the abandoned request thread must not keep the process alive
    assert all(t.daemon for t in threading.enumerate() if t not in before)

def test_a_stream_cut_off_inside_the_code_block_is_not_salvageable(partial_server):
    _PartialHandler.close_code = False
    role = Role("strong", partial_server, "m", "", 100, 3, 30.0, {}, transport_retries=0, stall_timeout_s=10.0)
    r = LLM({"strong": role}).chat("strong", "s", "u", timeout_s=1.0)
    # the text arrived, but ===CODE=== never closed: what parse_blocks returns for it is truncated source
    assert r.partial and "def add" in r.text and not salvageable(r) and terminated_blocks(r.text) == set()

def test_a_non_streamed_timeout_has_nothing_to_salvage(server):
    _Handler.delay = 2.0
    try:
        llm = LLM({"strong": Role("strong", server, "m", "", 100, 1, 30.0, {}, stream=False)})
        r = llm.chat("strong", "s", "u", timeout_s=0.5)
        assert r.error == "timeout" and not r.partial and r.text == "" and llm.calls[-1]["partial"] is False
        assert "estimated" not in llm.calls[-1]
        assert llm.chat("strong", "s", "u", timeout_s=10).error is None   # drain the abandoned worker
    finally:
        _Handler.delay = 0.3

def test_an_abandoned_stream_does_not_write_into_a_later_calls_buffer(partial_server):
    role = Role("strong", partial_server, "m", "", 100, 3, 30.0, {}, transport_retries=0, stall_timeout_s=10.0)
    llm = LLM({"strong": role})
    r1 = llm.chat("strong", "s", "u", timeout_s=1.0)
    r2 = llm.chat("strong", "s", "u", timeout_s=1.0)   # r1's thread is still streaming into its own buffer
    assert r1.partial and r2.partial and r2.text == r1.text

def test_terminated_blocks_reads_the_last_occurrence_like_parse_blocks():
    t = "===CODE===\ndraft\n===END===\n===CODE===\nfinal but cut off"
    assert parse_blocks(t)["CODE"] == "final but cut off" and terminated_blocks(t) == set()
    t2 = "===VERDICT===\ncandidate\n===END===\n===CODE===\nx = 1\n===END===\n===NOTES===\nhalf"
    assert terminated_blocks(t2) == {"VERDICT", "CODE"}


# --- round 14 batch 7: a salvaged call's real cost ---

def _lookup_role(server):
    return Role("strong", server, "m", "KEY_LOOKUP", 100, 3, 30.0, {}, transport_retries=0,
                price_out_per_m=10.0, stall_timeout_s=10.0,
                usage_lookup_url=server + "/generation?id={id}")

def test_a_salvaged_calls_real_cost_is_looked_up(partial_server, monkeypatch):
    monkeypatch.setenv("KEY_LOOKUP", "k")
    llm = LLM({"strong": _lookup_role(partial_server)})
    r = llm.chat("strong", "s", "u", timeout_s=1.0)
    assert r.partial and salvageable(r)
    assert r.cost_lookup == {"id": "gen-abc", "cost": 0.42}
    rec = llm.calls[-1]
    # the measured numbers replace the buffer estimate entirely, and the record no longer claims one
    assert rec["usage"] == {"prompt_tokens": 100, "completion_tokens": 2000, "cost": 0.42}
    assert "estimated" not in rec and _PartialHandler.lookups == 1

def test_a_failed_lookup_leaves_the_estimate_flagged(partial_server, monkeypatch):
    monkeypatch.setenv("KEY_LOOKUP", "k")
    _PartialHandler.fail_lookup = True
    llm = LLM({"strong": _lookup_role(partial_server)})
    r = llm.chat("strong", "s", "u", timeout_s=1.0)
    assert r.partial and r.cost_lookup is None
    rec = llm.calls[-1]
    assert rec["estimated"] is True and rec["usage"]["completion_tokens"] == len(r.text) // 4
    assert rec["usage"]["cost"] > 0 and _PartialHandler.lookups == 1

def test_a_role_without_a_lookup_url_never_asks(partial_server):
    role = Role("strong", partial_server, "m", "", 100, 3, 30.0, {}, transport_retries=0,
                price_out_per_m=10.0, stall_timeout_s=10.0)
    r = LLM({"strong": role}).chat("strong", "s", "u", timeout_s=1.0)
    assert r.partial and r.cost_lookup is None and _PartialHandler.lookups == 0

def test_config_gives_the_openrouter_roles_a_usage_lookup_url():
    roles = load_config(str(ROOT / "config.toml"), "openrouter")["roles"]
    assert all(r.usage_lookup_url == "https://openrouter.ai/api/v1/generation?id={id}" for r in roles.values())
    assert load_config(str(ROOT / "config.toml"), "openai")["roles"]["strong"].usage_lookup_url == ""
