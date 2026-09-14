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
    # Retries for a call that failed before the model ever answered -- a DNS blip, a refused
    # connection, a 429/5xx from the gateway. Measured on the round-6 benchmark: one machine-side
    # DNS flap turned nine of fifteen runs into instant no_candidate. A model that answered wrongly
    # or timed out is NOT retried here; that is the orchestrator's decision.
    transport_retries: int = 2
    transport_backoff_s: float = 2.0
    # Where OpenAI-compatible providers still differ. OpenAI's reasoning models reject max_tokens for
    # max_completion_tokens. Only OpenRouter reports usage.cost; for any other provider the per-million
    # prices fill it in, otherwise cost reads as $0 and max_cost_usd_per_problem never fires.
    token_param: str = "max_tokens"
    omit_temperature: bool = False   # Claude 5 and OpenAI reasoning models reject a sampling temperature
    price_in_per_m: float = 0.0
    price_out_per_m: float = 0.0


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
                        int(r.get("max_concurrent", 1)), float(r.get("timeout_cap_s", 120.0)), dict(r.get("extra", {})),
                        int(r.get("transport_retries", 2)), float(r.get("transport_backoff_s", 2.0)),
                        r.get("token_param", "max_tokens"), bool(r.get("omit_temperature", False)), float(r.get("price_in_per_m", 0.0)), float(r.get("price_out_per_m", 0.0)))
             for name, r in prof.items()}
    return {"roles": roles, "limits": cfg["limits"], "phases": cfg["phases"], "profile": profile}


def pick_profile(path: str) -> str:
    """The first profile whose every role has its API key set, so whichever supported key a user
    exports just works without --profile. With none set, the first profile -- whose key the CLI names."""
    with open(path, "rb") as f:
        profiles = tomllib.load(f)["profiles"]
    for name, prof in profiles.items():
        if all(not r.get("api_key_env") or r["api_key_env"] in os.environ for r in prof.values()):
            return name
    return next(iter(profiles))


class LLM:
    def __init__(self, roles: dict[str, Role]):
        self.roles = roles
        self.calls: list[dict] = []
        self._sems: dict[tuple, threading.Semaphore] = {}
        limits: dict[tuple, int] = {}
        for r in roles.values():
            key = (r.base_url, r.model)
            limits[key] = min(limits[key], r.max_concurrent) if key in limits else r.max_concurrent
        for key, n in limits.items():
            self._sems[key] = threading.Semaphore(n)

    def chat(self, role: str, system: str, user: str, *, timeout_s: float, max_tokens: int | None = None, tag: str = "") -> Reply:
        r = self.roles[role]
        limit = max_tokens or r.max_tokens
        body = {"model": r.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                r.token_param: limit, "temperature": 0.2, "stream": False, **r.extra}
        if r.omit_temperature:
            body.pop("temperature")
        if timeout_s <= 0:
            reply = Reply("", "", {}, 0.0, r.model, "timeout")
            self.calls.append({"role": role, "tag": tag, "model": reply.model, "latency_s": 0.0, "usage": {},
                               "error": "timeout", "timed_out": True, "max_tokens": limit})
            return reply
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(r.api_key_env) if r.api_key_env else None
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(r.base_url + "/chat/completions", data=json.dumps(body).encode(), headers=headers)
        sem = self._sems[(r.base_url, r.model)]
        t0 = time.monotonic()
        for attempt in range(r.transport_retries + 1):
            result: dict = {}
            sem.acquire()

            def work(result=result):
                try:
                    with urllib.request.urlopen(req, timeout=timeout_s + 5) as resp:
                        result["data"] = json.load(resp)
                except urllib.error.HTTPError as e:
                    result["error"] = f"http {e.code}: {e.read().decode('utf-8', 'replace')[:300]}"
                    result["retryable"] = e.code == 429 or e.code >= 500
                except Exception as e:  # transport errors
                    result["error"] = f"transport: {e!r}"[:300]; result["retryable"] = True
                finally:
                    sem.release()

            remaining = timeout_s - (time.monotonic() - t0)
            th = threading.Thread(target=work, daemon=True); th.start(); th.join(timeout=max(0.0, remaining))
            timed_out = th.is_alive()
            # Retry only a failure that happened before the model answered, and only while the call's
            # own timeout still has room for the backoff plus a real attempt.
            backoff = r.transport_backoff_s * (attempt + 1)
            if timed_out or not result.get("retryable") or timeout_s - (time.monotonic() - t0) - backoff < 5.0:
                break
            time.sleep(backoff)
        latency = time.monotonic() - t0
        if timed_out:
            reply = Reply("", "", {}, latency, r.model, "timeout")
        elif "error" in result:
            reply = Reply("", "", {}, latency, r.model, result["error"])
        else:
            d = result["data"]; msg = d["choices"][0]["message"]; usage = d.get("usage") or {}
            if "cost" not in usage and (r.price_in_per_m or r.price_out_per_m):
                usage["cost"] = (usage.get("prompt_tokens", 0) * r.price_in_per_m + usage.get("completion_tokens", 0) * r.price_out_per_m) / 1e6
            reply = Reply(msg.get("content") or "", msg.get("reasoning_content") or msg.get("reasoning") or "",
                          usage, latency, d.get("model", r.model), None)
        self.calls.append({"role": role, "tag": tag, "model": reply.model, "latency_s": round(latency, 2), "usage": reply.usage,
                           "error": reply.error, "timed_out": timed_out, "max_tokens": limit})
        return reply


_BLOCK = re.compile(r"===([A-Z_]+)===\s*\n(.*?)(?:\n===END===|\Z)", re.S)
_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*[ \t]*\n(.*?)\n```", re.S)
_OPEN_FENCE = re.compile(r"^```[a-zA-Z0-9_+-]*[ \t]*$")
_CLOSE_FENCE = re.compile(r"^```[ \t]*$")


def _unfence(body: str) -> str:
    fences = _FENCE.findall(body)
    if fences:
        return max(fences, key=len).strip()
    # A fence opened inside the block and closed after ===END=== leaves an unmatched marker line in
    # the body; left in place it is a syntax error and the whole candidate scores zero. Only a line
    # that is nothing but the marker counts, so backticks inside a string literal are untouched.
    lines = body.split("\n")
    if lines and _OPEN_FENCE.match(lines[0]):
        lines = lines[1:]
    if lines and _CLOSE_FENCE.match(lines[-1]):
        lines = lines[:-1]
    return "\n".join(lines)


def parse_blocks(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, body in _BLOCK.findall(text):
        if name == "END":
            continue
        out[name] = _unfence(body.strip())  # last occurrence wins
    return out


class FakeLLM:
    def __init__(self, script: dict[str, list[str]], cost: float | None = None):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[dict] = []
        self.prompts: list[tuple[str, str, str]] = []
        self._lock = threading.Lock()
        self.usage = {"prompt_tokens": 1, "completion_tokens": 1}
        if cost is not None:
            self.usage["cost"] = cost   # lets a test drive Run.cost_usd() past the cap

    def chat(self, role: str, system: str, user: str, *, timeout_s: float, max_tokens: int | None = None, tag: str = "") -> Reply:
        with self._lock:
            key = tag if tag in self.script else role
            assert self.script.get(key), f"FakeLLM: no scripted reply left for {key!r} (tag={tag!r}, role={role!r})"
            text = self.script[key].pop(0)
            self.prompts.append((role, system, user))
            self.calls.append({"role": role, "tag": tag, "model": "fake", "latency_s": 0.0, "usage": dict(self.usage), "error": None, "timed_out": False})
        return Reply(text, "", dict(self.usage), 0.0, "fake", None)
