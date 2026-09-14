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
    # Streaming exists for one reason: a connection that stalls mid-answer is otherwise
    # indistinguishable from a model that is still thinking, and the call burns its whole timeout
    # before the transport retry can fire. With the body streamed, "no bytes for stall_timeout_s"
    # is a transport error the existing retry already knows how to handle. A provider that cannot
    # stream sets stream = false in its role config; no branch on provider name goes in this module.
    stream: bool = True
    stall_timeout_s: float = 30.0
    # {tag: extra} merged over `extra` for that tag's request only. Two uses so far, both about
    # effort: the post-gate calls (`repair`, `solve_fresh`), where what is left of the deadline is
    # measured in tens of seconds rather than hundreds and a thinking model at its solve-time effort
    # cannot finish one (bench16 gave a repair 67 s against a role whose completed call that run took
    # 168 s); and `stress`, which is off the critical path and can take a slower, better author at a
    # cheaper effort. Which knob to turn is the provider's business and stays in config; llm.py only
    # knows that a tag may carry its own extra.
    tag_extra: dict = field(default_factory=dict)
    # Floor for the repair/fresh-solve call cap in seconds (see verify.repair_cap): the phase
    # fraction alone is tuned to the deadline, not to the model, and for a thinking model it is far
    # below the call's measured latency. 0 keeps the fraction as the only rule.
    repair_cap_s: float = 0.0
    # Where to ask the provider what a call actually cost, when the stream never delivered its usage
    # chunk. `{id}` is filled with the generation id the first streamed chunk carried. Empty (the
    # default) means there is nothing to ask and the buffer estimate stands. A provider difference,
    # so it lives here rather than in a branch in chat(); the response shape is read leniently.
    usage_lookup_url: str = ""


@dataclass
class Reply:
    content: str
    reasoning: str
    usage: dict
    latency_s: float
    model: str
    error: str | None = None
    # True when this is what a STREAMED call had accumulated when its own timeout_s elapsed: the
    # request thread was abandoned mid-answer and `text` is the prefix it had sent. Everything
    # downstream that reads such a reply has to ask which blocks actually closed (`salvageable`),
    # because the block the cut landed in is truncated, not finished.
    partial: bool = False
    # {id, cost} when the real cost of this call was fetched from the provider after the fact (see
    # _lookup_usage). None when it was not asked for, or the lookup failed and `usage` is an estimate.
    cost_lookup: dict | None = None

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
                        r.get("token_param", "max_tokens"), bool(r.get("omit_temperature", False)), float(r.get("price_in_per_m", 0.0)), float(r.get("price_out_per_m", 0.0)),
                        bool(r.get("stream", True)), float(r.get("stall_timeout_s", 30.0)),
                        dict(r.get("tag_extra", {})), float(r.get("repair_cap_s", 0.0)), r.get("usage_lookup_url", ""))
             for name, r in prof.items()}
    # `roles` is the per-role model config above; `prompt_roles` is the [roles] table -- which model
    # role each PROMPT is sent to (solve.PROMPT_ROLES holds the defaults when it is absent). Two
    # different things with one natural name; the return keys are what keeps them apart.
    return {"roles": roles, "prompt_roles": dict(cfg.get("roles", {})), "limits": cfg["limits"], "phases": cfg["phases"], "profile": profile}


def pick_profile(path: str) -> str:
    """The first profile whose every role has its API key set, so whichever supported key a user
    exports just works without --profile. With none set, the first profile -- whose key the CLI names."""
    with open(path, "rb") as f:
        profiles = tomllib.load(f)["profiles"]
    for name, prof in profiles.items():
        if all(not r.get("api_key_env") or r["api_key_env"] in os.environ for r in prof.values()):
            return name
    return next(iter(profiles))


def _new_acc() -> dict:
    """The accumulator a streaming read fills. It is created by the caller and handed to the request
    thread so that whatever arrived before the call's timeout is still readable after the thread is
    abandoned. `content`/`reasoning` are lists because list.append and the reader's slice copy are
    each atomic under the GIL -- that is the whole of the thread safety needed here, and one is
    created per attempt so an abandoned thread can never write into a later call's buffer."""
    return {"content": [], "reasoning": [], "usage": {}, "model": None, "id": None}


def _acc_text(acc: dict) -> tuple[str, str]:
    return "".join(acc["content"][:]), "".join(acc["reasoning"][:])


def _read_sse(resp, acc: dict) -> dict:
    """An OpenAI-compatible SSE body, folded back into the same shape a non-streamed reply has, so
    nothing downstream knows the difference. Accumulates delta.content and delta.reasoning (OpenRouter's
    field name; reasoning_content is accepted too) into `acc` and keeps whatever usage the last chunk
    carries -- OpenAI puts it in a final chunk requested with stream_options.include_usage, OpenRouter
    sends it top-level on the last chunk. Reading line by line is what makes the socket timeout a stall
    detector: each readline is bounded by it."""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except ValueError:   # a keep-alive or a malformed frame is not the answer; skip it
            continue
        acc["model"] = chunk.get("model") or acc["model"]
        acc["id"] = acc["id"] or chunk.get("id")   # what _lookup_usage asks the provider about
        if chunk.get("usage"):
            acc["usage"] = chunk["usage"]
        for ch in chunk.get("choices") or []:
            d = ch.get("delta") or {}
            if d.get("content"):
                acc["content"].append(d["content"])
            if d.get("reasoning") or d.get("reasoning_content"):
                acc["reasoning"].append(d.get("reasoning") or d.get("reasoning_content"))
    content, reasoning = _acc_text(acc)
    out = {"choices": [{"message": {"content": content, "reasoning": reasoning}}], "usage": acc["usage"]}
    if acc["model"]:
        out["model"] = acc["model"]
    return out


def _lookup_usage(r: Role, gen_id: str, headers: dict) -> dict | None:
    """What the call really cost, asked of the provider after the fact.

    A stream cut off at its timeout never reaches its usage chunk, so the only figure available is
    len(buffer) // 4 -- which ignores reasoning tokens entirely and is therefore wrong by an order of
    magnitude for a thinking model. bench17 and bench18 reported $0.12 between them while the key's
    usage rose by $0.32. One GET, the call's own auth header, 10 s, never retried: any failure returns
    None and the estimate stands, because a cost figure is a report line and never a control decision
    inside the call. The response is read leniently (OpenRouter wraps it in `data` and spells the
    counts `tokens_prompt` / `tokens_completion` / `total_cost`) so no provider name appears here."""
    try:
        with urllib.request.urlopen(urllib.request.Request(r.usage_lookup_url.format(id=gen_id), headers=headers), timeout=10.0) as resp:
            d = json.load(resp)
    except Exception:
        return None
    if isinstance(d, dict) and isinstance(d.get("data"), dict):
        d = d["data"]
    if not isinstance(d, dict):
        return None
    usage = {}
    for key, names in (("prompt_tokens", ("tokens_prompt", "native_tokens_prompt", "prompt_tokens")),
                       ("completion_tokens", ("tokens_completion", "native_tokens_completion", "completion_tokens")),
                       ("cost", ("total_cost", "cost"))):
        v = next((d[n] for n in names if isinstance(d.get(n), (int, float)) and not isinstance(d.get(n), bool)), None)
        if v is not None:
            usage[key] = v
    return usage or None


def _fill_cost(usage: dict, r: Role) -> dict:
    """Only OpenRouter reports usage.cost; for any other provider the per-million prices fill it in."""
    if usage and "cost" not in usage and (r.price_in_per_m or r.price_out_per_m):
        usage["cost"] = (usage.get("prompt_tokens", 0) * r.price_in_per_m
                         + usage.get("completion_tokens", 0) * r.price_out_per_m) / 1e6
    return usage


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
        extra = {**r.extra, **r.tag_extra[tag]} if tag in r.tag_extra else r.extra
        body = {"model": r.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                r.token_param: limit, "temperature": 0.2, "stream": r.stream,
                **({"stream_options": {"include_usage": True}} if r.stream else {}), **extra}
        if r.omit_temperature:
            body.pop("temperature")
        if timeout_s <= 0:
            reply = Reply("", "", {}, 0.0, r.model, "timeout")
            self.calls.append({"role": role, "tag": tag, "model": reply.model, "latency_s": 0.0, "usage": {},
                               "error": "timeout", "timed_out": True, "partial": False, "max_tokens": limit})
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
            acc = _new_acc()   # per attempt: an abandoned thread keeps writing into its own buffer
            sem.acquire()

            def work(result=result, acc=acc):
                try:
                    # Streaming: the socket timeout applies per read, so it IS the stall detector --
                    # a gap longer than stall_timeout_s between chunks raises, and the retry below
                    # treats it as any other transport error. The call's own timeout_s still bounds
                    # the whole attempt, through the join() beneath. The `with` closes the socket
                    # whenever this thread finally leaves, abandoned or not.
                    with urllib.request.urlopen(req, timeout=r.stall_timeout_s if r.stream else timeout_s + 5) as resp:
                        result["data"] = _read_sse(resp, acc) if r.stream else json.load(resp)
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
        estimated = False
        if timed_out:
            # The request thread is abandoned, but what it streamed before the cap is still in `acc`.
            # A reply cut off in its last blocks can still hold a complete ===CODE=== block (see
            # `salvageable`), and dropping it threw away runs that had already written the answer:
            # bench17 spent 199.5 s of a 200 s cap on one Opus attempt and shipped no_candidate.
            content, reasoning = _acc_text(acc)
            partial = bool(content.strip() or reasoning.strip())
            usage = dict(acc["usage"])
            if partial and not usage:
                # The stream never reached the usage chunk. Four characters per token is the usual
                # rough ratio; the call record says `estimated` so no cost total reads it as measured.
                usage, estimated = {"completion_tokens": len(content or reasoning) // 4}, True
            reply = Reply(content, reasoning, _fill_cost(usage, r), latency, acc["model"] or r.model, "timeout", partial)
        elif "error" in result:
            reply = Reply("", "", {}, latency, r.model, result["error"])
        else:
            d = result["data"]; msg = d["choices"][0]["message"]
            reply = Reply(msg.get("content") or "", msg.get("reasoning_content") or msg.get("reasoning") or "",
                          _fill_cost(d.get("usage") or {}, r), latency, d.get("model", r.model), None)
        # A reply with no usage of its own -- cut off before the usage chunk, or a stream that never
        # sent one -- is the only case worth a round trip; everything else already has the numbers.
        if r.usage_lookup_url and acc.get("id") and (reply.partial or not reply.usage):
            found = _lookup_usage(r, acc["id"], headers)
            if found:
                reply.usage, estimated = found, False
                reply.cost_lookup = {"id": acc["id"], "cost": found.get("cost")}
        rec = {"role": role, "tag": tag, "model": reply.model, "latency_s": round(latency, 2), "usage": reply.usage,
               "error": reply.error, "timed_out": timed_out, "partial": reply.partial, "max_tokens": limit}
        if estimated:
            rec["estimated"] = True
        self.calls.append(rec)
        return reply


_BLOCK = re.compile(r"===([A-Z_]+)===\s*\n(.*?)(\n===END===|\Z)", re.S)
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
    for name, body, _end in _BLOCK.findall(text):
        if name == "END":
            continue
        out[name] = _unfence(body.strip())  # last occurrence wins
    return out


def terminated_blocks(text: str) -> set[str]:
    """The names whose block was closed by its own `===END===` line.

    parse_blocks deliberately lets an unterminated block run to the end of the text, which is what
    makes a reply that stopped mid-answer parse at all. For a reply cut off by its timeout that
    distinction is the whole question: the block the cut landed in is truncated, not finished. Kept
    as a companion function so parse_blocks' return type stays a plain {name: body} for its existing
    callers, and reading last-occurrence-wins exactly as parse_blocks does, so the two always agree
    about which body a name refers to."""
    closed: dict[str, bool] = {}
    for name, _body, end in _BLOCK.findall(text):
        if name != "END":
            closed[name] = bool(end)
    return {n for n, ok in closed.items() if ok}


def salvageable(reply) -> bool:
    """Whether a reply abandoned at its timeout still carries a usable solution: it is `partial` and
    its ===CODE=== block closed. The SOLVE prompt puts RULES and DESIGN before CODE and
    EXAMPLES/TRAPS/ALGORITHM after it, and the REPAIR prompt puts VERDICT before CODE, so what a
    cut-off reply loses is commentary the gate does not need. A cut that landed inside CODE leaves
    truncated source, which is not a candidate."""
    return bool(getattr(reply, "partial", False)) and "CODE" in terminated_blocks(reply.text)


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
