"""Turn raw Foundry request/response bodies into NormalizedRequest / ResponseInfo.

Supported surfaces
------------------
* Azure OpenAI chat completions   /openai/deployments/{dep}/chat/completions, /openai/v1/chat/completions
* Azure AI Inference              /models/chat/completions
* Responses API                   /openai/responses, /openai/v1/responses
* Foundry Agent Service           .../assistants, .../threads, .../threads/{id}/messages,
                                  .../threads/{id}/runs, .../threads/runs, .../submit_tool_outputs
                                  .../agents (v2 agent definitions)
"""
from __future__ import annotations

import json
import re
from typing import Any

from .models import Message, NormalizedRequest, ResponseInfo

_DEPLOYMENT_RE = re.compile(r"/deployments/([^/]+)/")


def classify(method: str, path: str) -> str:
    p = path.split("?", 1)[0].rstrip("/")
    if method.upper() == "GET" and re.search(r"/threads/[^/]+/runs/[^/]+$", p):
        return "agents.run_status"
    if method.upper() != "POST":
        return "other"
    if p.endswith("/models/chat/completions"):
        return "inference"
    if p.endswith("/chat/completions"):
        return "chat_completions"
    if re.search(r"/responses$", p):
        return "responses"
    if re.search(r"/threads/[^/]+/runs/[^/]+/submit_tool_outputs$", p):
        return "agents.submit_tool_outputs"
    if re.search(r"/threads/runs$", p):
        return "agents.create_thread_and_run"
    if re.search(r"/threads/[^/]+/runs$", p):
        return "agents.create_run"
    if re.search(r"/threads/[^/]+/messages$", p):
        return "agents.create_message"
    if re.search(r"/threads$", p):
        return "agents.create_thread"
    if re.search(r"/assistants$", p):
        return "agents.create_assistant"
    if re.search(r"/assistants/[^/]+$", p):
        return "agents.update_assistant"
    if re.search(r"/agents(/[^/]+/versions)?$", p):
        return "agents.create_assistant"
    if re.search(r"/agents/[^/]+$", p):
        return "agents.update_assistant"
    return "other"


# ---------------------------------------------------------------- content ---

def content_to_text(content: Any) -> str:
    """Flatten the many content shapes (str, parts list, dicts) into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(t for t in (content_to_text(c) for c in content) if t)
    if isinstance(content, dict):
        t = content.get("type")
        if "text" in content:
            txt = content["text"]
            return txt.get("value", "") if isinstance(txt, dict) else str(txt)
        if t in ("image_url", "input_image", "image_file"):
            return "[image]"
        if t in ("input_file", "file"):
            return "[file]"
        if t in ("input_audio",):
            return "[audio]"
        if "content" in content:
            return content_to_text(content["content"])
        if "output" in content:
            return content_to_text(content["output"])
        return ""
    return str(content)


def _chat_messages(raw: list[dict]) -> list[Message]:
    out: list[Message] = []
    for m in raw or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role", "user")
        text = content_to_text(m.get("content"))
        if role in ("tool", "function"):
            out.append(Message("tool", text, kind="tool_output", name=m.get("name") or m.get("tool_call_id")))
            continue
        if text:
            out.append(Message(role, text))
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
            out.append(Message("assistant", str(fn.get("arguments", "")), kind="tool_call", name=fn.get("name")))
    return out


def _responses_input(inp: Any) -> list[Message]:
    if isinstance(inp, str):
        return [Message("user", inp)]
    out: list[Message] = []
    for item in inp or []:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t in ("function_call_output", "custom_tool_call_output", "computer_call_output", "mcp_call_output"):
            out.append(Message("tool", content_to_text(item.get("output")), kind="tool_output", name=item.get("call_id")))
        elif t in ("function_call", "custom_tool_call"):
            out.append(Message("assistant", str(item.get("arguments") or item.get("input") or ""), kind="tool_call", name=item.get("name")))
        elif t == "reasoning":
            continue
        elif "role" in item:
            out.append(Message(item["role"], content_to_text(item.get("content"))))
    return out


def _agent_instructions(body: dict) -> str | None:
    if body.get("instructions"):
        return body["instructions"]
    d = body.get("definition")
    if isinstance(d, dict) and d.get("instructions"):
        return d["instructions"]
    return None


def normalize_request(method: str, path: str, body: Any) -> NormalizedRequest:
    api = classify(method, path)
    if api == "agents.run_status":
        parts = path.split("?", 1)[0].rstrip("/").split("/")
        return NormalizedRequest(api=api, thread_id=parts[-3])
    if api == "other" or not isinstance(body, dict):
        return NormalizedRequest(api="other" if not isinstance(body, dict) else api)

    p = path.split("?", 1)[0]
    dep = _DEPLOYMENT_RE.search(p)
    model = body.get("model") or (dep.group(1) if dep else None)
    params = {k: body[k] for k in ("temperature", "top_p", "seed", "response_format", "tool_choice",
                                   "parallel_tool_calls", "reasoning_effort", "reasoning", "truncation",
                                   "store", "text") if k in body}
    req = NormalizedRequest(api=api, model=model, stream=bool(body.get("stream")), params=params)

    tools = body.get("tools")
    if tools is None and isinstance(body.get("definition"), dict):
        tools = body["definition"].get("tools")
    req.tools = [t for t in (tools or []) if isinstance(t, dict)]

    if api in ("chat_completions", "inference"):
        req.messages = _chat_messages(body.get("messages", []))
        req.max_output_tokens = body.get("max_completion_tokens") or body.get("max_tokens")

    elif api == "responses":
        req.instructions = body.get("instructions")
        req.messages = _responses_input(body.get("input"))
        req.max_output_tokens = body.get("max_output_tokens")
        req.previous_response_id = body.get("previous_response_id")
        agent = body.get("agent")
        if isinstance(agent, dict):
            req.agent_id = agent.get("name") or agent.get("id")

    elif api in ("agents.create_assistant", "agents.update_assistant"):
        req.instructions = _agent_instructions(body)
        if api == "agents.update_assistant":
            req.agent_id = p.rstrip("/").split("/")[-1]
        if isinstance(body.get("definition"), dict):
            req.model = body["definition"].get("model") or req.model
        if re.search(r"/agents/([^/]+)/versions$", p):
            req.agent_id = p.rstrip("/").split("/")[-2]

    elif api == "agents.create_message":
        req.thread_id = p.rstrip("/").split("/")[-2]
        req.messages = [Message(body.get("role", "user"), content_to_text(body.get("content")))]

    elif api == "agents.create_thread":
        req.messages = _chat_messages(body.get("messages", []))

    elif api in ("agents.create_run", "agents.create_thread_and_run"):
        req.agent_id = body.get("assistant_id") or body.get("agent_id")
        req.instructions = body.get("instructions")
        req.additional_instructions = body.get("additional_instructions")
        req.max_output_tokens = body.get("max_completion_tokens")
        if api == "agents.create_run":
            req.thread_id = p.rstrip("/").split("/")[-2]
            req.messages = _chat_messages(body.get("additional_messages") or [])
        else:
            thread = body.get("thread") or {}
            req.messages = _chat_messages(thread.get("messages") or [])
        req.params["max_prompt_tokens"] = body.get("max_prompt_tokens")
        req.params["truncation_strategy"] = body.get("truncation_strategy")

    elif api == "agents.submit_tool_outputs":
        req.messages = [Message("tool", content_to_text(o.get("output")), kind="tool_output", name=o.get("tool_call_id"))
                        for o in body.get("tool_outputs", []) if isinstance(o, dict)]
    return req


# --------------------------------------------------------------- response ---

def _apply_usage(info: ResponseInfo, usage: dict | None) -> None:
    if not isinstance(usage, dict):
        return
    info.prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens", info.prompt_tokens))
    info.completion_tokens = usage.get("completion_tokens", usage.get("output_tokens", info.completion_tokens))
    det = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    if isinstance(det, dict) and det.get("cached_tokens") is not None:
        info.cached_tokens = det["cached_tokens"]
    odet = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    if isinstance(odet, dict) and odet.get("reasoning_tokens") is not None:
        info.reasoning_tokens = odet["reasoning_tokens"]


def _apply_object(info: ResponseInfo, obj: dict) -> None:
    if not isinstance(obj, dict):
        return
    if "error" in obj and isinstance(obj["error"], dict):
        err = obj["error"]
        info.error_code = err.get("code") or err.get("type") or info.error_code
        info.error_message = err.get("message") or info.error_message
        inner = err.get("innererror") or {}
        if isinstance(inner, dict) and inner.get("code"):
            info.error_code = f"{info.error_code}:{inner['code']}"
    if obj.get("id") and not info.object_id:
        info.object_id = obj["id"]
    if obj.get("model"):
        info.model = obj["model"]
    _apply_usage(info, obj.get("usage"))
    choices = obj.get("choices")
    if isinstance(choices, list):
        for c in choices:
            if isinstance(c, dict) and c.get("finish_reason"):
                info.finish_reason = c["finish_reason"]
            msg = (c or {}).get("message") or {}
            if isinstance(msg, dict) and msg.get("content"):
                info.output_text = content_to_text(msg["content"])
    # Responses API / Agents run objects
    status = obj.get("status")
    if isinstance(status, str) and obj.get("object") in ("response", "thread.run", "agent.run", None):
        info.run_status = status
    inc = obj.get("incomplete_details")
    if isinstance(inc, dict) and inc.get("reason"):
        info.finish_reason = inc["reason"]
    last_err = obj.get("last_error")
    if isinstance(last_err, dict) and last_err:
        info.error_code = last_err.get("code") or info.error_code
        info.error_message = last_err.get("message") or info.error_message
    if obj.get("object") == "response" and isinstance(obj.get("output"), list):
        texts = []
        for item in obj["output"]:
            if isinstance(item, dict) and item.get("type") == "message":
                texts.append(content_to_text(item.get("content")))
        if texts:
            info.output_text = "\n".join(texts)


def parse_response(status: int, latency_ms: int, content_type: str, raw: bytes) -> ResponseInfo:
    info = ResponseInfo(status=status, latency_ms=latency_ms)
    text = raw.decode("utf-8", errors="replace")
    if "text/event-stream" in content_type:
        stream_text: list[str] = []
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                ev = json.loads(data)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            # Responses API wraps the object: {"type": "response.completed", "response": {...}}
            if isinstance(ev.get("response"), dict):
                _apply_object(info, ev["response"])
            else:
                _apply_object(info, ev)
            for c in ev.get("choices") or []:
                d = (c or {}).get("delta") or {}
                if isinstance(d, dict) and isinstance(d.get("content"), str):
                    stream_text.append(d["content"])
            if ev.get("type") == "response.output_text.delta" and isinstance(ev.get("delta"), str):
                stream_text.append(ev["delta"])
        if stream_text:
            info.output_text = "".join(stream_text)
        return info
    try:
        obj = json.loads(text) if text else {}
    except json.JSONDecodeError:
        if status >= 400:
            info.error_message = text[:500]
        return info
    _apply_object(info, obj)
    return info
