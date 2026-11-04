"""Deterministic diagnostics run on every request.

Each rule looks at the normalized request (and the upstream response when
available) and emits Findings with a concrete suggestion. Rules are cheap:
no network calls, only token counting, regexes and a few SQLite lookups.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass

from . import tokens
from .config import Settings
from .models import Analysis, Finding, NormalizedRequest, ResponseInfo
from .store import Store

PENALTY = {"critical": 25, "warning": 10, "info": 2}


@dataclass
class RuleContext:
    settings: Settings
    store: Store | None
    app: str


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# ------------------------------------------------------------- breakdown ---

def token_breakdown(req: NormalizedRequest) -> dict[str, int]:
    b = Counter()
    b["instructions"] = tokens.count(req.instructions) + tokens.count(req.additional_instructions)
    b["tool_schemas"] = tokens.count_json(req.tools)
    last_user_idx = max((i for i, m in enumerate(req.messages) if m.role == "user"), default=-1)
    for i, m in enumerate(req.messages):
        t = tokens.count(m.text) + tokens.MESSAGE_OVERHEAD
        if m.role in ("system", "developer"):
            b["system"] += t
        elif m.kind == "tool_output":
            b["tool_outputs"] += t
        elif m.kind == "tool_call":
            b["tool_calls"] += t
        elif i == last_user_idx:
            b["latest_user"] += t
        else:
            b["history"] += t
    return {k: int(v) for k, v in b.items() if v}


# ----------------------------------------------------------------- rules ---

def r_context_pressure(req, resp, ctx, est, cw, out):
    used = resp.prompt_tokens if resp and resp.prompt_tokens else est
    if not used:
        return
    ratio = used / cw
    s = ctx.settings
    src = "actual (Azure usage)" if resp and resp.prompt_tokens else "estimated"
    if ratio >= s.context_crit_ratio:
        sev = "critical"
    elif ratio >= s.context_warn_ratio:
        sev = "warning"
    else:
        return
    out.append(Finding(
        "R001", sev, "Context window is nearly full",
        f"Input uses {used:,} of {cw:,} tokens ({ratio:.0%}, {src}). Quality drops and cost/latency grow as the "
        "window fills; the next turns may fail with context_length_exceeded.",
        "Trim what the model doesn't need for this step: summarize older turns, keep only the top-k RAG chunks, "
        "shorten tool outputs, and set a hard budget (max_prompt_tokens / truncation_strategy for Agents, "
        "truncation='auto' for Responses).",
        {"input_tokens": used, "context_window": cw, "ratio": round(ratio, 3), "source": src},
    ))


def r_output_budget(req, resp, ctx, est, cw, out):
    mo = req.max_output_tokens
    if mo and est and est + mo > cw:
        out.append(Finding(
            "R002", "warning", "Input + max output tokens exceed the context window",
            f"Estimated input {est:,} + max output {mo:,} = {est + mo:,} > window {cw:,}.",
            "Lower max_tokens/max_output_tokens or cut input; otherwise the response is clipped or the call fails.",
            {"est_input": est, "max_output_tokens": mo, "context_window": cw},
        ))


def r_large_system_prompt(req, resp, ctx, est, cw, out, bd):
    sys_tokens = bd.get("system", 0) + bd.get("instructions", 0)
    s = ctx.settings
    if sys_tokens < s.system_prompt_warn_tokens:
        return
    sev = "critical" if sys_tokens >= s.system_prompt_crit_tokens else "warning"
    out.append(Finding(
        "R003", sev, "System prompt / instructions are very large",
        f"Instructions are ~{sys_tokens:,} tokens and are re-sent on every call. Long prompts dilute key rules "
        "(models follow the start and end best) and often hide contradictions.",
        "Move reference material (policies, catalogs, examples) into retrieval or a file-search tool; keep the "
        "prompt to role, hard rules, output format and 1-2 examples. Put static content first so it can be cached.",
        {"instruction_tokens": sys_tokens},
    ))


def r_multiple_system(req, resp, ctx, est, cw, out):
    sys_idx = [i for i, m in enumerate(req.messages) if m.role in ("system", "developer")]
    n = len(sys_idx) + (1 if req.instructions else 0)
    first_non_sys = next((i for i, m in enumerate(req.messages) if m.role not in ("system", "developer")), None)
    mid = [i for i in sys_idx if first_non_sys is not None and i > first_non_sys]
    if n > 1:
        where = []
        if req.instructions:
            where.append("top-level `instructions`")
        if sys_idx:
            where.append(f"{len(sys_idx)} system/developer message(s)")
        out.append(Finding(
            "R004", "warning", "Multiple system prompts in one request (possible overwrite)",
            f"Found {n} instruction sources: {', '.join(where)}. Later or more specific instructions usually win, "
            "so an earlier prompt can be silently overridden.",
            "Merge into a single system/developer message (or `instructions`) built in one place in your code. "
            "If frameworks (LangChain, Semantic Kernel, AutoGen) inject their own system message, disable or "
            "merge it.",
            {"count": n, "mid_conversation_positions": mid},
        ))
    elif mid:
        out.append(Finding(
            "R004", "warning", "System message inserted mid-conversation",
            f"A system/developer message appears at position {mid} after user/assistant turns.",
            "Keep a single system message at the start; put dynamic per-turn context in the user message instead.",
            {"positions": mid},
        ))


_INJECTION = [
    r"ignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|prompts?|rules|directions)",
    r"disregard\s+(the\s+|all\s+|any\s+)?(previous|prior|above|system)\b",
    r"forget\s+(everything|all|your)\s+(you|instructions|rules|previous)",
    r"\byou\s+are\s+now\s+(?!going|ready|able|connected|logged)",
    r"\bnew\s+(system\s+)?instructions?\s*:",
    r"override\s+(the\s+|your\s+)?(system\s+)?(prompt|instructions)",
    r"<\|im_start\|>\s*system",
    r"\[/?INST\]",
    r"\bdo\s+anything\s+now\b|\bDAN\s+mode\b|\bjailbreak",
    r"^\s*system\s*:\s",
    r"reveal\s+(your\s+)?(system\s+prompt|hidden\s+instructions)",
]
_INJECTION_RE = re.compile("|".join(_INJECTION), re.IGNORECASE | re.MULTILINE)


def r_injection(req, resp, ctx, est, cw, out):
    hits = []
    for i, m in enumerate(req.messages):
        if m.role in ("system", "developer") or m.kind == "tool_call":
            continue
        mt = _INJECTION_RE.search(m.text)
        if mt:
            hits.append({"position": i, "role": m.role, "kind": m.kind, "match": mt.group(0)[:80]})
    if not hits:
        return
    from_tool = any(h["kind"] == "tool_output" for h in hits)
    out.append(Finding(
        "R005", "critical" if from_tool else "warning",
        "Instruction-override text found in " + ("tool/RAG output (indirect prompt injection)" if from_tool
                                                 else "user content"),
        "Content outside the system prompt tries to replace the agent's instructions: "
        + "; ".join(f"[{h['role']}#{h['position']}] \"{h['match']}\"" for h in hits[:5]),
        "Wrap untrusted content in clear delimiters and tell the model it is data, not instructions; enable Azure "
        "AI Content Safety Prompt Shields on the deployment; strip or quarantine such text from retrieved docs.",
        {"hits": hits[:10]},
    ))


def r_agent_instruction_override(req, resp, ctx, est, cw, out):
    if req.api in ("agents.create_run", "agents.create_thread_and_run") and req.instructions:
        stored = ctx.store.get_agent(req.agent_id) if (ctx.store and req.agent_id) else None
        detail = ("This run passes `instructions`, which REPLACES the agent's saved instructions for this run "
                  "(it does not append).")
        ev = {"agent_id": req.agent_id, "run_instruction_tokens": tokens.count(req.instructions)}
        sev = "warning"
        if stored and stored.get("instructions"):
            sim = difflib.SequenceMatcher(None, stored["instructions"][:5000], req.instructions[:5000]).ratio()
            lost = tokens.count(stored["instructions"])
            detail += f" The agent's own instructions (~{lost:,} tokens) are dropped; similarity {sim:.0%}."
            ev |= {"agent_instruction_tokens": lost, "similarity": round(sim, 3)}
            if sim < 0.5:
                sev = "critical"
        out.append(Finding(
            "R006", sev, "Run-level instructions overwrite the agent's instructions", detail,
            "Use `additional_instructions` to add per-run context, or update the agent itself if the base "
            "prompt should change.",
            ev,
        ))
    if req.api == "agents.update_assistant" and req.instructions is not None and ctx.store and req.agent_id:
        stored = ctx.store.get_agent(req.agent_id)
        if stored and stored.get("instructions") and stored["hash"] != _h(req.instructions):
            sim = difflib.SequenceMatcher(None, stored["instructions"][:5000], req.instructions[:5000]).ratio()
            out.append(Finding(
                "R006", "warning" if sim < 0.8 else "info", "Agent instructions were overwritten",
                f"Agent {req.agent_id} instructions changed (similarity {sim:.0%}, "
                f"{tokens.count(stored['instructions']):,} -> {tokens.count(req.instructions):,} tokens).",
                "Version agent prompts in source control and deploy them through one pipeline so different "
                "services don't overwrite each other's agent definition.",
                {"agent_id": req.agent_id, "similarity": round(sim, 3)},
            ))


def r_prompt_drift(req, resp, ctx, est, cw, out):
    if not ctx.store or req.api.startswith("agents.") and req.api not in ("agents.create_run",):
        return
    texts = req.system_texts()
    if not texts:
        return
    joined = "\n---\n".join(texts)
    key = f"{ctx.app}|{req.agent_id or req.model or '?'}"
    info = ctx.store.record_prompt(key, _h(joined), joined[:20000])
    total, distinct = info["total_requests"] or 0, info["distinct_versions"] or 0
    if total >= 5 and distinct / total > 0.5:
        out.append(Finding(
            "R007", "warning", "System prompt changes on almost every request",
            f"{distinct} distinct system prompts across {total} requests for `{key}`. The prompt likely embeds "
            "per-request data (time, user, retrieved docs), which defeats prompt caching and makes behaviour "
            "hard to reproduce.",
            "Keep the system prompt static; move per-request values (dates, user profile, RAG context) to the end "
            "of the conversation, e.g. into the latest user message.",
            {"key": key, "distinct_versions": distinct, "requests": total},
        ))
    elif info["prev_hash"] and info["prev_sample"]:
        sim = difflib.SequenceMatcher(None, info["prev_sample"][:5000], joined[:5000]).ratio()
        out.append(Finding(
            "R007", "warning" if sim < 0.6 else "info", "System prompt changed since the previous request",
            f"The system prompt for `{key}` differs from the last one seen (similarity {sim:.0%}). "
            + ("This looks like a different prompt entirely: something may be overwriting it."
               if sim < 0.6 else "Small edit or dynamic value."),
            "Confirm the change was intended. Load prompts from one versioned source and log the prompt version "
            "with each call.",
            {"key": key, "similarity": round(sim, 3)},
        ))


_CONFLICTS = [
    ("brevity vs detail",
     r"\b(be|keep|answer|respond|reply)\b[^.\n]{0,30}\b(concise|brief|short|succinct)\b",
     r"\b(be|give|provide|answer|respond|reply)\b[^.\n]{0,30}\b(detailed|comprehensive|thorough|exhaustive|in-depth|elaborate)\b"),
    ("JSON vs prose output",
     r"\b(respond|output|answer|reply|return)\b[^.\n]{0,40}\bjson\b",
     r"\b(respond|output|answer|reply|format)\b[^.\n]{0,40}\b(markdown|plain text|prose|bullet points)\b"),
    ("ask vs don't ask",
     r"\balways\s+ask\b|\bask\s+(a\s+)?clarifying\s+questions?\b",
     r"\bnever\s+ask\b|\bdo\s+not\s+ask\b|\bdon't\s+ask\b"),
]
_LANG_RE = re.compile(r"\b(?:respond|answer|reply|write|speak)\b[^.\n]{0,20}\bin\s+(english|hindi|french|spanish|german|"
                      r"japanese|chinese|arabic|portuguese|italian|tamil|telugu|marathi|bengali|kannada)\b", re.I)
_PERSONA_RE = re.compile(r"\byou\s+are\s+(?:an?|the)\s+([a-z][a-z\- ]{2,40}?)(?=[.,;:\n]|\s+(?:who|that|and|for|with)\b)", re.I)
_ALWAYS_RE = re.compile(r"\balways\s+([a-z]+\s+[a-z]+)", re.I)
_NEVER_RE = re.compile(r"\b(?:never|do\s+not|don't)\s+([a-z]+\s+[a-z]+)", re.I)


def r_conflicting_directives(req, resp, ctx, est, cw, out):
    texts = req.system_texts()
    if not texts:
        return
    joined = "\n".join(texts)
    conflicts = []
    for name, a, b in _CONFLICTS:
        ma, mb = re.search(a, joined, re.I), re.search(b, joined, re.I)
        if ma and mb:
            conflicts.append({"type": name, "a": ma.group(0)[:80], "b": mb.group(0)[:80]})
    langs = {m.group(1).lower() for m in _LANG_RE.finditer(joined)}
    if len(langs) > 1:
        conflicts.append({"type": "output language", "a": ", ".join(sorted(langs)), "b": ""})
    always = {m.group(1).lower() for m in _ALWAYS_RE.finditer(joined)}
    never = {m.group(1).lower() for m in _NEVER_RE.finditer(joined)}
    for phrase in sorted(always & never):
        conflicts.append({"type": "always vs never", "a": f"always {phrase}", "b": f"never {phrase}"})
    if len(texts) > 1:
        personas = []
        for t in texts:
            p = {m.group(1).strip().lower() for m in _PERSONA_RE.finditer(t)}
            if p:
                personas.append(p)
        flat = set().union(*personas) if personas else set()
        if len(personas) > 1 and len(flat) > 1:
            conflicts.append({"type": "multiple personas", "a": ", ".join(sorted(flat))[:120], "b": ""})
    if not conflicts:
        return
    out.append(Finding(
        "R008", "warning", "Contradictory instructions in the prompt",
        "The instructions pull in different directions: "
        + "; ".join(f"{c['type']}: \"{c['a']}\"" + (f" vs \"{c['b']}\"" if c['b'] else "") for c in conflicts[:6])
        + ". The model has to guess which one wins, so behaviour becomes inconsistent.",
        "Resolve each conflict explicitly (e.g. 'Be concise by default; give detail only when the user asks for "
        "it'). Keep one persona and one output format per agent.",
        {"conflicts": conflicts},
    ))


def r_duplicates(req, resp, ctx, est, cw, out):
    min_chars = ctx.settings.duplicate_min_chars
    chunks: Counter = Counter()
    sample: dict[str, str] = {}
    sources = ([req.instructions] if req.instructions else []) + [m.text for m in req.messages if m.kind != "tool_call"]
    for text in sources:
        for para in re.split(r"\n\s*\n", text or ""):
            n = _norm_ws(para)
            if len(n) >= min_chars:
                k = _h(n)
                chunks[k] += 1
                sample.setdefault(k, n)
    dups = {k: c for k, c in chunks.items() if c > 1}
    if not dups:
        return
    wasted = sum((c - 1) * tokens.count(sample[k]) for k, c in dups.items())
    share = wasted / est if est else 0
    sev = "warning" if wasted >= 500 or share >= 0.10 else "info"
    out.append(Finding(
        "R009", sev, "Duplicate content in the context",
        f"{len(dups)} block(s) of text appear more than once, wasting ~{wasted:,} tokens ({share:.0%} of input). "
        "Common causes: the same RAG chunk retrieved twice, history re-appended, or the system prompt pasted "
        "into a user message.",
        "De-duplicate retrieved chunks by id/hash before building the prompt, and make sure conversation memory "
        "isn't appended twice.",
        {"blocks": [{"preview": sample[k][:120], "times": c} for k, c in list(dups.items())[:5]]},
        wasted_tokens=wasted,
    ))


def r_tool_outputs(req, resp, ctx, est, cw, out, bd):
    s = ctx.settings
    big = [(m.name or f"#{i}", tokens.count(m.text)) for i, m in enumerate(req.messages)
           if m.kind == "tool_output" and tokens.count(m.text) >= s.tool_output_warn_tokens]
    share = bd.get("tool_outputs", 0) / est if est else 0
    if not big and share < s.tool_share_warn_ratio:
        return
    out.append(Finding(
        "R010", "warning", "Tool / function outputs dominate the context",
        f"Tool outputs are {bd.get('tool_outputs', 0):,} tokens ({share:.0%} of input)."
        + (f" Large outputs: {', '.join(f'{n} ({t:,} tok)' for n, t in big[:5])}." if big else ""),
        "Return only the fields the model needs (project/select columns, paginate, cap rows), summarize large "
        "results before returning them, and drop old tool results from history once they've been used.",
        {"large_outputs": big[:10], "share": round(share, 3)},
    ))


def r_history(req, resp, ctx, est, cw, out, bd):
    s = ctx.settings
    turns = sum(1 for m in req.messages if m.role in ("user", "assistant") and m.kind == "message")
    share = bd.get("history", 0) / est if est else 0
    reasons = []
    if turns > s.history_turns_warn:
        reasons.append(f"{turns} user/assistant turns are being re-sent")
    if share >= s.history_share_warn_ratio and est > 4000:
        reasons.append(f"old turns are {share:.0%} of the input")
    if req.api in ("agents.create_run", "agents.create_thread_and_run") and resp and resp.prompt_tokens:
        tp = req.params.get("truncation_strategy")
        if not tp and not req.params.get("max_prompt_tokens") and resp.prompt_tokens > 0.3 * cw:
            reasons.append(f"thread has grown to {resp.prompt_tokens:,} prompt tokens with no truncation_strategy "
                           "or max_prompt_tokens set")
    if not reasons:
        return
    out.append(Finding(
        "R011", "warning", "Conversation history is growing unbounded",
        "; ".join(reasons).capitalize() + ".",
        "Use a sliding window (keep the last N turns) plus a running summary of older turns. For Agents set "
        "truncation_strategy={'type':'last_messages','last_messages':N} and max_prompt_tokens; for Responses "
        "use truncation='auto'.",
        {"turns": turns, "history_share": round(share, 3)},
    ))


def r_tools(req, resp, ctx, est, cw, out, bd):
    s = ctx.settings
    n = len(req.tools)
    schema_tok = bd.get("tool_schemas", 0)
    undocumented = []
    for t in req.tools:
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        if t.get("type", "function") == "function" and not (fn.get("description") or "").strip():
            undocumented.append(fn.get("name", "?"))
    if n > s.max_tools_warn or schema_tok > s.tool_schema_warn_tokens:
        out.append(Finding(
            "R012", "warning", "Too many tools / tool schemas are large",
            f"{n} tools whose schemas cost ~{schema_tok:,} tokens per call. Tool selection accuracy drops as the "
            "list grows.",
            "Expose only the tools relevant to the current step (route first, then attach a smaller toolset), "
            "split into specialist agents, and trim verbose parameter descriptions.",
            {"tools": n, "schema_tokens": schema_tok},
        ))
    if undocumented:
        out.append(Finding(
            "R012", "info", "Tools without descriptions",
            f"{len(undocumented)} tool(s) have no description: {', '.join(undocumented[:8])}.",
            "Describe what each tool does, when to use it and when not to. The model relies on this text to "
            "choose tools.",
            {"tools": undocumented},
        ))


_DYNAMIC_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}"  # ISO timestamps
    r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"  # UUIDs
    r"|\b1[6-9]\d{8}(?:\d{3})?\b"  # unix epoch
    r"|\b(?:current|today'?s)\s+(?:date|time)\s*(?:is|:)", re.I)


def r_cache(req, resp, ctx, est, cw, out):
    s = ctx.settings
    sys_text = "\n".join(req.system_texts())
    head = sys_text[:8000]
    m = _DYNAMIC_RE.search(head)
    if m and est >= s.cache_min_prompt_tokens:
        out.append(Finding(
            "R013", "warning", "Dynamic values at the start of the prompt break prompt caching",
            f"The system prompt contains a per-request value (\"{m.group(0)}\") near the beginning. Azure OpenAI "
            "caches only an identical prefix (1,024+ tokens), so anything that changes early invalidates the "
            "cache for everything after it.",
            "Order the prompt static → dynamic: tools, system prompt, few-shot examples first; dates, user data and "
            "retrieved docs last.",
            {"match": m.group(0)},
        ))
    if resp and resp.prompt_tokens and resp.prompt_tokens >= s.cache_min_prompt_tokens and ctx.store:
        prefix = json.dumps(req.tools, sort_keys=True)[:4000] + sys_text[:6000]
        if not prefix.strip():
            return
        seen = ctx.store.record_prefix(f"{ctx.app}|{req.model}", _h(prefix))
        if seen >= 1 and not resp.cached_tokens:
            out.append(Finding(
                "R013", "info", "Repeated prompt prefix got no cache hit",
                f"This prefix was sent {seen} time(s) before but cached_tokens is 0 on {resp.prompt_tokens:,} "
                "prompt tokens.",
                "Check the model/deployment supports prompt caching, keep the prefix byte-identical (same tool "
                "order, no whitespace changes), and send related requests close together (cache lives minutes).",
                {"times_seen": seen},
            ))


def r_response(req, resp, ctx, est, cw, out):
    if not resp:
        return
    fr = (resp.finish_reason or "").lower()
    if fr in ("length", "max_tokens", "max_output_tokens") or (resp.run_status == "incomplete" and fr != "content_filter"):
        extra = ""
        if resp.reasoning_tokens and resp.completion_tokens and resp.reasoning_tokens > 0.7 * resp.completion_tokens:
            extra = f" {resp.reasoning_tokens:,} of {resp.completion_tokens:,} output tokens were spent on reasoning."
        out.append(Finding(
            "R014", "warning", "Response was cut off (hit the output token limit)",
            f"finish_reason/incomplete reason = `{fr or resp.run_status}`.{extra}",
            "Raise max_tokens/max_output_tokens, ask for shorter output, or lower reasoning effort on reasoning "
            "models.",
            {"finish_reason": fr, "completion_tokens": resp.completion_tokens, "reasoning_tokens": resp.reasoning_tokens},
        ))
    code = (resp.error_code or "").lower()
    msg = (resp.error_message or "").lower()
    if fr == "content_filter" or "content_filter" in code or "responsibleaipolicy" in code:
        out.append(Finding(
            "R015", "warning", "Blocked by Azure content filter",
            f"code={resp.error_code or fr}. {resp.error_message or ''}"[:400],
            "Inspect which input triggered it (user text vs retrieved docs); sanitize untrusted content or adjust "
            "the content filter configuration for the deployment if the block is a false positive.",
            {"error_code": resp.error_code},
        ))
    if resp.status >= 400 or resp.run_status == "failed":
        if "context_length" in code or "maximum context length" in msg or "too many tokens" in msg:
            out.append(Finding("R016", "critical", "Request exceeded the model's context length",
                               resp.error_message or "context_length_exceeded",
                               "Apply the R001/R011 fixes: trim history, cap tool output, reduce RAG top-k.",
                               {"status": resp.status}))
        elif resp.status == 429 or "rate_limit" in code:
            out.append(Finding("R016", "warning", "Rate limited (429)", resp.error_message or "Too many requests",
                               "Retry with exponential backoff honouring retry-after, cut tokens per request, raise "
                               "the deployment TPM quota or use provisioned throughput.", {"status": resp.status}))
        elif resp.status in (401, 403):
            out.append(Finding("R016", "warning", "Authentication / authorization failed",
                               resp.error_message or f"HTTP {resp.status}",
                               "Check the api-key or Entra ID token audience and the role assignment "
                               "(Cognitive Services OpenAI User / Azure AI User).", {"status": resp.status}))
        elif "content_filter" not in code:
            out.append(Finding("R016", "warning", f"Upstream error (HTTP {resp.status}"
                               + (f", run {resp.run_status}" if resp.run_status == "failed" else "") + ")",
                               f"{resp.error_code or ''} {resp.error_message or ''}".strip()[:400] or "No detail",
                               "Inspect the request body in the dashboard; retry transient 5xx errors with backoff.",
                               {"status": resp.status, "error_code": resp.error_code}))
    if resp.latency_ms > ctx.settings.latency_warn_ms and resp.status < 400:
        tps = (resp.completion_tokens or 0) / (resp.latency_ms / 1000) if resp.latency_ms else 0
        out.append(Finding(
            "R017", "info", "Slow response",
            f"Took {resp.latency_ms/1000:.1f}s ({tps:.0f} output tok/s, {resp.prompt_tokens or est:,} input tokens).",
            "Stream responses, shrink the input, use a smaller/faster model for simple steps, or lower reasoning "
            "effort.",
            {"latency_ms": resp.latency_ms},
        ))


_B64_RE = re.compile(r"[A-Za-z0-9+/]{800,}={0,2}")
_HTML_TAG_RE = re.compile(r"<[a-zA-Z/][^>]{0,200}>")


def r_waste(req, resp, ctx, est, cw, out):
    b64 = 0
    html_chars = 0
    ws_chars = 0
    total_chars = 0
    for m in req.messages:
        t = m.text or ""
        total_chars += len(t)
        b64 += sum(len(x) for x in _B64_RE.findall(t))
        html_chars += sum(len(x) for x in _HTML_TAG_RE.findall(t))
        ws_chars += sum(len(x) - 1 for x in re.findall(r"[ \t]{3,}|\n{3,}", t))
    if b64:
        out.append(Finding(
            "R018", "warning", "Base64 / encoded blobs pasted as text",
            f"~{b64:,} characters (~{b64 // 3:,} tokens) of base64-like data are in text messages.",
            "Send images/files as image_url / input_file parts or upload them, and never inline binary as text.",
            {"chars": b64}, wasted_tokens=b64 // 3,
        ))
    if total_chars > 4000 and html_chars / total_chars > 0.15:
        out.append(Finding(
            "R018", "info", "Raw HTML markup in the context",
            f"{html_chars / total_chars:.0%} of message text is HTML tags.",
            "Convert scraped/retrieved HTML to plain text or markdown before sending it.",
            {"html_share": round(html_chars / total_chars, 3)}, wasted_tokens=html_chars // 4,
        ))
    if total_chars > 4000 and ws_chars / total_chars > 0.10:
        out.append(Finding(
            "R018", "info", "Excess whitespace / padding",
            f"{ws_chars / total_chars:.0%} of message text is repeated spaces or blank lines.",
            "Collapse whitespace in documents and tool outputs before adding them to the prompt.",
            {"whitespace_share": round(ws_chars / total_chars, 3)}, wasted_tokens=ws_chars // 4,
        ))


def r_no_system(req, resp, ctx, est, cw, out):
    if req.api in ("chat_completions", "inference", "responses") and not req.system_texts() \
            and not req.previous_response_id and not req.agent_id and req.messages:
        out.append(Finding(
            "R019", "info", "No system prompt",
            "The request has no system/developer message or instructions, so the agent's role, constraints and "
            "output format are undefined.",
            "Add a short system prompt: role, scope (what to refuse), output format, and tool-use guidance.",
        ))


# ---------------------------------------------------------------- engine ---

def analyze(req: NormalizedRequest, resp: ResponseInfo | None, ctx: RuleContext) -> Analysis:
    bd = token_breakdown(req)
    est = sum(bd.values())
    model = (resp.model if resp and resp.model else None) or req.model
    cw = tokens.context_window(model, ctx.settings.context_windows, ctx.settings.default_context_window)
    out: list[Finding] = []
    args = (req, resp, ctx, est, cw, out)
    if req.analyzable:
        for rule in (r_context_pressure, r_output_budget, r_multiple_system, r_injection,
                     r_agent_instruction_override, r_prompt_drift, r_conflicting_directives, r_duplicates,
                     r_cache, r_waste, r_no_system):
            rule(*args)
        for rule in (r_large_system_prompt, r_tool_outputs, r_history, r_tools):
            rule(*args, bd)
    r_response(*args)
    score = max(0, 100 - sum(PENALTY[f.severity] for f in out))
    out.sort(key=lambda f: -PENALTY[f.severity])
    return Analysis(findings=out, breakdown=bd, est_input_tokens=est, context_window=cw, health_score=score)


def track_agent(req: NormalizedRequest, resp: ResponseInfo | None, store: Store | None) -> None:
    """Remember agent definitions so later runs/updates can be compared against them."""
    if not store or not resp or resp.status >= 400:
        return
    if req.api == "agents.create_assistant":
        agent_id = req.agent_id or resp.object_id
    elif req.api == "agents.update_assistant":
        agent_id = req.agent_id
    else:
        return
    if not agent_id:
        return
    prev = store.get_agent(agent_id)
    instr = req.instructions if req.instructions is not None else (prev or {}).get("instructions")
    store.upsert_agent(agent_id, instr, _h(instr) if instr else None, req.model or (prev or {}).get("model"),
                       len(req.tools) or (prev or {}).get("tools_count") or 0)
