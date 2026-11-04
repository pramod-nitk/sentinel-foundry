"""A tiny fake Azure AI Foundry used by the tests (no network needed)."""
import json

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

CALLS: list[dict] = []


async def chat(request: Request):
    body = await request.json()
    CALLS.append({"path": request.url.path, "headers": dict(request.headers), "body": body})
    if body.get("messages") and "TRIGGER_400" in json.dumps(body):
        return JSONResponse({"error": {"code": "context_length_exceeded",
                                       "message": "This model's maximum context length is 128000 tokens."}},
                            status_code=400)
    usage = {"prompt_tokens": 5000, "completion_tokens": 50, "total_tokens": 5050,
             "prompt_tokens_details": {"cached_tokens": 0}}
    if body.get("stream"):
        async def gen():
            yield 'data: {"choices":[{"index":0,"delta":{"content":"Hel"}}],"model":"gpt-4o-2024-08-06"}\n\n'
            yield 'data: {"choices":[{"index":0,"delta":{"content":"lo"},"finish_reason":"length"}]}\n\n'
            yield "data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n"
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")
    return JSONResponse({"id": "chatcmpl-1", "object": "chat.completion", "model": "gpt-4o-2024-08-06",
                         "choices": [{"index": 0, "finish_reason": "stop",
                                      "message": {"role": "assistant", "content": "Hello"}}],
                         "usage": usage})


async def responses(request: Request):
    body = await request.json()
    CALLS.append({"path": request.url.path, "body": body})
    return JSONResponse({"id": "resp_1", "object": "response", "model": "gpt-4.1", "status": "incomplete",
                         "incomplete_details": {"reason": "max_output_tokens"},
                         "output": [{"type": "message", "content": [{"type": "output_text", "text": "partial"}]}],
                         "usage": {"input_tokens": 900, "output_tokens": 100,
                                   "input_tokens_details": {"cached_tokens": 0}}})


async def create_assistant(request: Request):
    body = await request.json()
    return JSONResponse({"id": "asst_123", "object": "assistant", "model": body.get("model"),
                         "instructions": body.get("instructions")})


async def create_run(request: Request):
    body = await request.json()
    return JSONResponse({"id": "run_1", "object": "thread.run", "status": "queued",
                         "assistant_id": body.get("assistant_id")})


async def get_run(request: Request):
    return JSONResponse({"id": "run_1", "object": "thread.run", "status": "completed", "model": "gpt-4o",
                         "usage": {"prompt_tokens": 110000, "completion_tokens": 300, "total_tokens": 110300}})


async def other(request: Request):
    return Response(b"ok", media_type="text/plain")


fake_app = Starlette(routes=[
    Route("/openai/deployments/{dep}/chat/completions", chat, methods=["POST"]),
    Route("/models/chat/completions", chat, methods=["POST"]),
    Route("/openai/v1/responses", responses, methods=["POST"]),
    Route("/api/projects/{p}/assistants", create_assistant, methods=["POST"]),
    Route("/api/projects/{p}/threads/{t}/runs", create_run, methods=["POST"]),
    Route("/api/projects/{p}/threads/{t}/runs/{r}", get_run, methods=["GET"]),
    Route("/{path:path}", other, methods=["GET", "POST"]),
])
