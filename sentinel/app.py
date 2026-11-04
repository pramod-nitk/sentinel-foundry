"""Foundry Sentinel: a transparent proxy that diagnoses every Azure AI Foundry call.

Point your SDK's endpoint at this service instead of the Azure resource. Every
request is forwarded unchanged (auth headers included) and the response is
streamed straight back. Analysis happens after the response, off the hot path.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import Counter, OrderedDict, defaultdict
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from . import judge, tokens
from .config import Settings
from .dashboard import DASHBOARD_HTML
from .models import SEVERITY_RANK, NormalizedRequest, ResponseInfo
from .normalize import normalize_request, parse_response
from .rules import RuleContext, analyze, track_agent
from .store import Store

log = logging.getLogger("sentinel")

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "host", "content-length", "accept-encoding"}
RESPONSE_DROP = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length"}
TERMINAL_RUN = {"completed", "failed", "incomplete", "expired", "cancelled"}
STREAM_CAPTURE_CAP = 5_000_000


class Sentinel:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None,
                 judge_transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.store = Store(settings.db_path)
        timeout = httpx.Timeout(settings.upstream_timeout_s, connect=15.0)
        self.client = httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False)
        self.judge_client = httpx.AsyncClient(transport=judge_transport, timeout=settings.judge_timeout_s)
        self._tasks: set[asyncio.Task] = set()
        self._seen_runs: OrderedDict[str, None] = OrderedDict()

    async def close(self) -> None:
        await self.drain()
        await self.client.aclose()
        await self.judge_client.aclose()
        self.store.close()

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for background analysis (used by tests and on shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    def _clip(self, b: bytes | str | None) -> str | None:
        if not self.settings.store_bodies or b is None:
            return None
        t = b.decode("utf-8", errors="replace") if isinstance(b, (bytes, bytearray)) else b
        n = self.settings.max_stored_body_chars
        return t if len(t) <= n else t[:n] + f"\n...[truncated {len(t) - n:,} chars]"

    def _first_time_run(self, run_id: str | None) -> bool:
        if not run_id:
            return True
        if run_id in self._seen_runs:
            return False
        self._seen_runs[run_id] = None
        if len(self._seen_runs) > 10_000:
            self._seen_runs.popitem(last=False)
        return True

    async def process(self, rid: str, app: str, method: str, path: str, req_body: bytes, req_ctype: str,
                      status: int, latency_ms: int, resp_ctype: str, resp_body: bytes) -> None:
        try:
            body_obj = None
            if req_body and ("json" in req_ctype or req_body[:1] in (b"{", b"[")):
                try:
                    body_obj = json.loads(req_body)
                except json.JSONDecodeError:
                    body_obj = None
            nreq = normalize_request(method, path, body_obj)
            resp = parse_response(status, latency_ms, resp_ctype, resp_body)
            if nreq.api == "other" and status < 400:
                return  # GET polling, file uploads, etc.
            if nreq.api == "agents.run_status":
                if resp.run_status not in TERMINAL_RUN or not self._first_time_run(resp.object_id):
                    return
            ctx = RuleContext(self.settings, self.store, app)
            analysis = await asyncio.to_thread(analyze, nreq, resp, ctx)
            await asyncio.to_thread(track_agent, nreq, resp, self.store)
            rec = {
                "id": rid, "ts": time.time(), "app": app, "api": nreq.api, "method": method, "path": path,
                "model": resp.model or nreq.model, "status": status, "latency_ms": latency_ms,
                "stream": int(nreq.stream), "est_input_tokens": analysis.est_input_tokens,
                "context_window": analysis.context_window, "prompt_tokens": resp.prompt_tokens,
                "completion_tokens": resp.completion_tokens, "cached_tokens": resp.cached_tokens,
                "finish_reason": resp.finish_reason or resp.run_status, "error_code": resp.error_code,
                "health_score": analysis.health_score, "max_severity": analysis.max_severity,
                "findings_json": json.dumps([f.to_dict() for f in analysis.findings]),
                "breakdown_json": json.dumps(analysis.breakdown), "judge_json": None,
                "request_body": self._clip(req_body), "response_body": self._clip(resp_body),
            }
            await asyncio.to_thread(self.store.save_request, rec)
            for f in analysis.findings:
                if f.severity != "info":
                    log.warning("[%s] %s %s %s: %s", rid[:8], app, f.severity.upper(), f.rule, f.title)
            if judge.should_judge(self.settings, analysis):
                result = await judge.run_judge(self.settings, self.judge_client, nreq, resp, analysis)
                await asyncio.to_thread(self.store.update_judge, rid, result)
        except Exception:  # noqa: BLE001 - analysis must never break traffic
            log.exception("analysis failed for %s %s", method, path)


def _authorized(request: Request, settings: Settings) -> bool:
    if not settings.admin_token:
        return True
    tok = request.headers.get("x-sentinel-admin-token") or request.query_params.get("token")
    return tok == settings.admin_token


def _summarize(rows: list[dict]) -> dict:
    total = len(rows)
    by_rule: dict[str, dict] = {}
    by_app: dict[str, dict] = defaultdict(lambda: {"requests": 0, "health_sum": 0, "input_sum": 0,
                                                     "critical": 0, "warning": 0})
    sev_counts: Counter = Counter()
    errors = prompt = cached = completion = 0
    for r in rows:
        a = by_app[r["app"] or "default"]
        a["requests"] += 1
        a["health_sum"] += r["health_score"] or 0
        a["input_sum"] += r["prompt_tokens"] or r["est_input_tokens"] or 0
        if (r["status"] or 0) >= 400:
            errors += 1
        prompt += r["prompt_tokens"] or 0
        cached += r["cached_tokens"] or 0
        completion += r["completion_tokens"] or 0
        if r["max_severity"]:
            sev_counts[r["max_severity"]] += 1
            if r["max_severity"] in ("critical", "warning"):
                a[r["max_severity"]] += 1
        for f in json.loads(r["findings_json"] or "[]"):
            key = f"{f['rule']}|{f['title']}"
            e = by_rule.setdefault(key, {"rule": f["rule"], "title": f["title"], "severity": f["severity"],
                                         "count": 0, "wasted_tokens": 0, "suggestion": f["suggestion"],
                                         "apps": set()})
            e["count"] += 1
            e["wasted_tokens"] += f.get("wasted_tokens", 0)
            e["apps"].add(r["app"] or "default")
            if SEVERITY_RANK[f["severity"]] > SEVERITY_RANK[e["severity"]]:
                e["severity"] = f["severity"]
    recs = sorted(by_rule.values(), key=lambda e: (-SEVERITY_RANK[e["severity"]], -e["count"]))
    for e in recs:
        e["apps"] = sorted(e["apps"])
        e["share"] = round(e["count"] / total, 3) if total else 0
    apps = []
    for name, a in by_app.items():
        apps.append({"app": name, "requests": a["requests"],
                     "avg_health": round(a["health_sum"] / a["requests"], 1),
                     "avg_input_tokens": round(a["input_sum"] / a["requests"]),
                     "critical": a["critical"], "warning": a["warning"]})
    apps.sort(key=lambda x: x["avg_health"])
    return {
        "requests": total,
        "avg_health": round(sum(r["health_score"] or 0 for r in rows) / total, 1) if total else None,
        "error_rate": round(errors / total, 3) if total else 0,
        "requests_by_severity": dict(sev_counts),
        "tokens": {"prompt": prompt, "completion": completion, "cached": cached,
                   "cache_hit_ratio": round(cached / prompt, 3) if prompt else 0},
        "recommendations": recs,
        "apps": apps,
    }


def create_app(settings: Settings | None = None, transport: httpx.AsyncBaseTransport | None = None,
               judge_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    holder: dict[str, Sentinel] = {}

    @asynccontextmanager
    async def lifespan(app):
        if not settings.upstream_url:
            raise RuntimeError("SENTINEL_UPSTREAM_URL is not set (e.g. https://<resource>.openai.azure.com)")
        holder["s"] = Sentinel(settings, transport, judge_transport)
        app.state.sentinel = holder["s"]
        log.info("Foundry Sentinel -> %s (tokenizer: %s, judge: %s)", settings.upstream_url,
                 tokens.tokenizer_name(), "on" if settings.judge_enabled else "off")
        yield
        await holder["s"].close()

    app = FastAPI(title="Foundry Sentinel", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def deny():
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    @app.get("/sentinel/health")
    async def health(request: Request):
        return JSONResponse({"ok": True, "upstream": settings.upstream_url, "tokenizer": tokens.tokenizer_name(),
                             "judge": settings.judge_enabled})

    @app.get("/sentinel")
    async def dashboard(request: Request):
        if not _authorized(request, settings):
            return deny()
        return HTMLResponse(DASHBOARD_HTML)

    @app.get("/sentinel/api/requests")
    async def list_requests(request: Request):
        if not _authorized(request, settings):
            return deny()
        q = request.query_params
        hours = float(q.get("hours", 0) or 0)
        rows = await asyncio.to_thread(
            holder["s"].store.list_requests, min(int(q.get("limit", 100)), 1000), q.get("app") or None,
            q.get("severity") or None, time.time() - hours * 3600 if hours else None)
        return JSONResponse(rows)

    @app.get("/sentinel/api/requests/{rid}")
    async def get_request(request: Request):
        if not _authorized(request, settings):
            return deny()
        rec = await asyncio.to_thread(holder["s"].store.get_request, request.path_params["rid"])
        return JSONResponse(rec) if rec else JSONResponse({"error": "not found"}, status_code=404)

    @app.get("/sentinel/api/summary")
    async def summary(request: Request):
        if not _authorized(request, settings):
            return deny()
        hours = float(request.query_params.get("hours", 24))
        rows = await asyncio.to_thread(holder["s"].store.rows_since, time.time() - hours * 3600)
        out = _summarize(rows)
        out["hours"] = hours
        return JSONResponse(out)

    @app.post("/sentinel/api/analyze")
    async def analyze_offline(request: Request):
        """Lint a request body without sending it to Azure (handy in CI).

        Body: {"path": "/openai/deployments/gpt-4o/chat/completions", "body": {...}, "method": "POST"}
        """
        if not _authorized(request, settings):
            return deny()
        payload = await request.json()
        nreq: NormalizedRequest = normalize_request(payload.get("method", "POST"), payload.get("path", ""),
                                                    payload.get("body"))
        if not nreq.analyzable:
            return JSONResponse({"error": "unrecognised path/body; expected a Foundry chat, responses or agents "
                                          "request"}, status_code=400)
        a = analyze(nreq, None, RuleContext(settings, None, payload.get("app", "offline")))
        return JSONResponse({"api": nreq.api, "health_score": a.health_score, "est_input_tokens": a.est_input_tokens,
                             "context_window": a.context_window, "breakdown": a.breakdown,
                             "findings": [f.to_dict() for f in a.findings]})

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
                   include_in_schema=False)
    async def proxy(request: Request):
        s = holder["s"]
        method = request.method
        path = request.url.path
        query = request.url.query
        url = settings.upstream_url + path + (f"?{query}" if query else "")
        req_body = await request.body()
        skip = request.headers.get("x-sentinel-skip") == "1"
        app_name = request.headers.get(settings.app_header) or "default"
        # --- client auth + upstream key injection ---
        presented_key = request.headers.get("api-key")
        auth = request.headers.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        if settings.client_keys and presented_key not in settings.client_keys \
                and bearer not in settings.client_keys:
            return JSONResponse({"error": {"code": "sentinel_unauthorized",
                                           "message": "Missing or invalid Sentinel client key"}}, status_code=401)
        drop = set()
        if settings.upstream_api_key:
            drop.add("api-key")  # never forward the client's key; we use ours
            if bearer is not None and (bearer in settings.client_keys or not bearer.startswith("ey")):
                drop.add("authorization")  # a proxy key/dummy, not a real Entra ID token (JWTs start with "ey")
        elif bearer is not None and bearer in settings.client_keys:
            drop.add("authorization")
        if not settings.upstream_api_key and presented_key in settings.client_keys:
            drop.add("api-key")
        fwd_headers = [(k, v) for k, v in request.headers.items()
                       if k.lower() not in HOP_BY_HOP and not k.lower().startswith("x-sentinel")
                       and k.lower() != settings.app_header and k.lower() not in drop]
        if settings.upstream_api_key:
            fwd_headers.append(("api-key", settings.upstream_api_key))
        fwd_headers.append(("accept-encoding", "identity"))
        rid = uuid.uuid4().hex
        start = time.perf_counter()
        req_ctype = request.headers.get("content-type", "")

        try:
            upstream = await s.client.send(
                s.client.build_request(method, url, headers=fwd_headers, content=req_body), stream=True)
        except httpx.HTTPError as e:
            latency = int((time.perf_counter() - start) * 1000)
            err = json.dumps({"error": {"code": "sentinel_upstream_unreachable", "message": str(e)}}).encode()
            if not skip:
                s.spawn(s.process(rid, app_name, method, path, req_body, req_ctype, 502, latency,
                                  "application/json", err))
            return Response(err, status_code=502, media_type="application/json",
                            headers={"x-sentinel-request-id": rid})

        resp_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in RESPONSE_DROP}
        resp_headers["x-sentinel-request-id"] = rid
        resp_ctype = upstream.headers.get("content-type", "")

        if "text/event-stream" in resp_ctype:
            captured = bytearray()

            async def relay():
                try:
                    async for chunk in upstream.aiter_bytes():
                        if len(captured) < STREAM_CAPTURE_CAP:
                            captured.extend(chunk)
                        yield chunk
                finally:
                    await upstream.aclose()
                    latency = int((time.perf_counter() - start) * 1000)
                    if not skip:
                        s.spawn(s.process(rid, app_name, method, path, req_body, req_ctype, upstream.status_code,
                                          latency, resp_ctype, bytes(captured)))

            return StreamingResponse(relay(), status_code=upstream.status_code, headers=resp_headers)

        raw = await upstream.aread()
        await upstream.aclose()
        latency = int((time.perf_counter() - start) * 1000)
        if not skip:
            s.spawn(s.process(rid, app_name, method, path, req_body, req_ctype, upstream.status_code, latency,
                              resp_ctype, raw))
        return Response(raw, status_code=upstream.status_code, headers=resp_headers)

    return app
