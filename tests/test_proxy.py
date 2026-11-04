"""End-to-end: client -> Sentinel -> fake Azure, all in-process."""
import asyncio
import json

import httpx

from sentinel.app import create_app
from sentinel.config import Settings
from tests.fake_azure import CALLS, fake_app


def judge_handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["api-key"] == "judge-key"
    content = json.dumps({"verdict": "needs_attention", "summary": "Two personas fight.",
                          "issues": [{"category": "prompt_overwrite", "severity": "warning",
                                      "explanation": "second system msg", "fix": "merge"}],
                          "improved_system_prompt": ""})
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}], "usage": {}})


def run(coro):
    return asyncio.run(coro)


async def _with_app(tmp_path, fn, **settings_kw):
    s = Settings(upstream_url="http://azure.test", db_path=str(tmp_path / "s.db"), **settings_kw)
    app = create_app(s, transport=httpx.ASGITransport(app=fake_app),
                     judge_transport=httpx.MockTransport(judge_handler))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sentinel") as c:
            await fn(c, app)


def test_passthrough_and_findings(tmp_path):
    async def body(c, app):
        r = await c.post("/openai/deployments/gpt-4o/chat/completions?api-version=2024-10-21",
                         headers={"api-key": "secret", "x-sentinel-app": "billing-bot"},
                         json={"messages": [{"role": "system", "content": "You are billing. Be concise."},
                                            {"role": "user", "content": "hi"},
                                            {"role": "system", "content": "You are a pirate. Be detailed."}]})
        assert r.status_code == 200 and r.json()["choices"][0]["message"]["content"] == "Hello"
        rid = r.headers["x-sentinel-request-id"]
        # auth forwarded, sentinel headers stripped
        sent = CALLS[-1]["headers"]
        assert sent["api-key"] == "secret" and "x-sentinel-app" not in sent
        assert CALLS[-1]["path"] == "/openai/deployments/gpt-4o/chat/completions"
        await app.state.sentinel.drain()
        rec = (await c.get(f"/sentinel/api/requests/{rid}")).json()
        assert rec["app"] == "billing-bot" and rec["prompt_tokens"] == 5000
        assert {"R004", "R008"} <= {f["rule"] for f in rec["findings"]}
        assert rec["judge"]["verdict"] == "needs_attention"
        summ = (await c.get("/sentinel/api/summary")).json()
        assert summ["requests"] == 1 and summ["recommendations"]
        assert (await c.get("/sentinel")).status_code == 200
    run(_with_app(tmp_path, body, judge_enabled=True, judge_endpoint="http://judge.test/chat",
                  judge_api_key="judge-key"))


def test_streaming_is_relayed_and_analyzed(tmp_path):
    async def body(c, app):
        async with c.stream("POST", "/models/chat/completions",
                            json={"model": "gpt-4o", "stream": True,
                                  "messages": [{"role": "user", "content": "hi"}]}) as r:
            text = "".join([chunk async for chunk in r.aiter_text()])
        assert "[DONE]" in text and r.headers["content-type"].startswith("text/event-stream")
        await app.state.sentinel.drain()
        rec = (await c.get("/sentinel/api/requests")).json()[0]
        assert rec["api"] == "inference" and rec["finish_reason"] == "length"
        assert rec["model"] == "gpt-4o-2024-08-06" and rec["prompt_tokens"] == 5000
        assert "R014" in {f["rule"] for f in rec["findings"]}
    run(_with_app(tmp_path, body))


def test_error_and_agent_flow(tmp_path):
    async def body(c, app):
        r = await c.post("/openai/deployments/gpt-4o/chat/completions",
                         json={"messages": [{"role": "user", "content": "TRIGGER_400"}]})
        assert r.status_code == 400
        await c.post("/api/projects/p/assistants", json={"model": "gpt-4o",
                                                          "instructions": "You are the HR policy agent."})
        await c.post("/api/projects/p/threads/t1/runs", json={"assistant_id": "asst_123",
                                                               "instructions": "Answer anything about sports."})
        for _ in range(3):  # polling: only the first terminal status is recorded
            await c.get("/api/projects/p/threads/t1/runs/run_1")
        await c.get("/api/projects/p/files")  # ignored
        await app.state.sentinel.drain()
        rows = (await c.get("/sentinel/api/requests")).json()
        by_api = {}
        for row in rows:
            by_api.setdefault(row["api"], []).append(row)
        assert "R016" in {f["rule"] for f in by_api["chat_completions"][0]["findings"]}
        assert by_api["agents.create_run"][0]["findings"][0]["rule"] == "R006"
        assert len(by_api["agents.run_status"]) == 1
        assert "R001" in {f["rule"] for f in by_api["agents.run_status"][0]["findings"]}
        assert "other" not in by_api
    run(_with_app(tmp_path, body))


def test_offline_analyze_and_admin_token(tmp_path):
    async def body(c, app):
        assert (await c.get("/sentinel/api/summary")).status_code == 401
        r = await c.post("/sentinel/api/analyze?token=t0k", json={
            "path": "/openai/v1/chat/completions",
            "body": {"model": "gpt-4o", "messages": [{"role": "user", "content": "hello"}]}})
        assert r.status_code == 200 and r.json()["findings"][0]["rule"] == "R019"
        assert (await c.get("/sentinel/health")).json()["ok"]
    run(_with_app(tmp_path, body, admin_token="t0k"))


def test_upstream_key_injection_and_client_keys(tmp_path):
    async def body(c, app):
        msgs = {"messages": [{"role": "user", "content": "hi"}]}
        url = "/openai/deployments/gpt-4o/chat/completions"
        assert (await c.post(url, json=msgs)).status_code == 401
        assert (await c.post(url, json=msgs, headers={"api-key": "wrong"})).status_code == 401
        r = await c.post(url, json=msgs, headers={"api-key": "team-a"})
        assert r.status_code == 200 and CALLS[-1]["headers"]["api-key"] == "REAL-FOUNDRY-KEY"
        r = await c.post(url, json=msgs, headers={"authorization": "Bearer team-b"})
        h = CALLS[-1]["headers"]
        assert r.status_code == 200 and h["api-key"] == "REAL-FOUNDRY-KEY" and "authorization" not in h
    run(_with_app(tmp_path, body, upstream_api_key="REAL-FOUNDRY-KEY", client_keys=["team-a", "team-b"]))
