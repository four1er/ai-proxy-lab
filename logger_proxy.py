"""Logging reverse proxy.

Sits in front of CLIProxyAPI (or any OpenAI/Claude/Gemini-compatible
upstream), forwards traffic untouched, and appends one JSON record per
request to data/raw_logs/YYYY-MM-DD.jsonl — the raw material for dataset
building.

- Streaming (SSE) responses are reassembled into a single completion text.
- The full request body is stored as-is (it already contains the whole
  conversation history for chat-style APIs, which is what makes these
  logs valuable as training data).
- Bodies are NEVER logged anywhere else; keep data/ out of git.

Run:  UPSTREAM=http://127.0.0.1:8317 python3 logger_proxy.py
Listens on 127.0.0.1:8400 by default.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import pathlib
import time
import uuid

import sys

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

UPSTREAM = os.environ.get("UPSTREAM", "http://127.0.0.1:8317")
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8400"))
DATA_DIR = pathlib.Path(__file__).parent / "data" / "raw_logs"

app = FastAPI()
client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0), trust_env=False)
_lock = asyncio.Lock()


def _log_path() -> pathlib.Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{dt.date.today().isoformat()}.jsonl"


async def _append(record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False)
    async with _lock:
        with _log_path().open("a", encoding="utf-8") as f:
            f.write(line + "\n")


async def _append_safe(record: dict) -> None:
    """Logging must never break forwarding: a full disk or a bad record
    loses one log line, not the user's request."""
    try:
        await _append(record)
    except Exception as exc:  # noqa: BLE001 - deliberate fail-open for logging
        print(f"logger_proxy: failed to write log record: {exc}", file=sys.stderr)


@app.get("/healthz")
async def healthz():
    """Liveness + upstream reachability, used by Docker healthcheck and
    ./proxy.sh. Any HTTP response from upstream counts as 'up'."""
    try:
        resp = await client.get(f"{UPSTREAM}/", timeout=httpx.Timeout(3.0))
        return {"status": "ok", "upstream": UPSTREAM, "upstream_status": resp.status_code}
    except httpx.HTTPError as exc:
        return JSONResponse(status_code=503,
                            content={"status": "degraded", "upstream": UPSTREAM,
                                     "error": str(exc)})


def _extract_completion_from_sse(chunks: list[bytes]) -> tuple[str, dict]:
    """Reassemble an OpenAI-style SSE stream into (text, last_chunk_json)."""
    text_parts: list[str] = []
    last_json: dict = {}
    for raw in chunks:
        for line in raw.decode("utf-8", "ignore").splitlines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            last_json = obj
            for choice in obj.get("choices", []):
                delta = choice.get("delta", {}) or {}
                if delta.get("content"):
                    text_parts.append(delta["content"])
                msg = choice.get("message", {}) or {}
                if msg.get("content"):
                    text_parts.append(msg["content"])
    return "".join(text_parts), last_json


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    body = await request.body()
    url = f"{UPSTREAM}/{path}"
    params = dict(request.query_params)
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in ("host", "content-length")}
    req_id = uuid.uuid4().hex
    started = time.time()

    try:
        req_json = json.loads(body) if body else None
    except json.JSONDecodeError:
        req_json = None

    upstream_req = client.build_request(request.method, url, params=params,
                                        headers=headers, content=body)
    try:
        upstream_resp = await client.send(upstream_req, stream=True)
    except httpx.HTTPError as exc:
        await _append_safe({
            "id": req_id, "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
            "method": request.method, "path": "/" + path,
            "status": 502,
            "latency_ms": int((time.time() - started) * 1000),
            "stream": False,
            "model": (req_json or {}).get("model") if isinstance(req_json, dict) else None,
            "request": req_json,
            "response_text": None,
            "error": f"upstream unreachable: {exc}",
        })
        return JSONResponse(status_code=502,
                            content={"error": {"message": "upstream unreachable",
                                               "detail": str(exc),
                                               "upstream": UPSTREAM}})
    resp_headers = {k: v for k, v in upstream_resp.headers.items()
                    if k.lower() not in ("content-length", "transfer-encoding",
                                         "content-encoding", "connection")}
    is_sse = "text/event-stream" in upstream_resp.headers.get("content-type", "")

    if is_sse:
        chunks: list[bytes] = []

        async def stream():
            try:
                async for chunk in upstream_resp.aiter_raw():
                    chunks.append(chunk)
                    yield chunk
            finally:
                await upstream_resp.aclose()
                text, last_json = _extract_completion_from_sse(chunks)
                await _append_safe({
                    "id": req_id, "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "method": request.method, "path": "/" + path,
                    "status": upstream_resp.status_code,
                    "latency_ms": int((time.time() - started) * 1000),
                    "stream": True,
                    "model": (req_json or {}).get("model") or last_json.get("model"),
                    "request": req_json,
                    "response_text": text,
                    "usage": last_json.get("usage"),
                })

        return StreamingResponse(stream(), status_code=upstream_resp.status_code,
                                 headers=resp_headers,
                                 media_type="text/event-stream")

    raw = await upstream_resp.aread()
    await upstream_resp.aclose()
    try:
        resp_json = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        resp_json = None
    await _append_safe({
        "id": req_id, "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "method": request.method, "path": "/" + path,
        "status": upstream_resp.status_code,
        "latency_ms": int((time.time() - started) * 1000),
        "stream": False,
        "model": (req_json or {}).get("model") or (resp_json or {}).get("model"),
        "request": req_json,
        "response": resp_json,
        "response_text": raw.decode("utf-8", "ignore") if resp_json is None else None,
    })
    return Response(content=raw, status_code=upstream_resp.status_code,
                    headers=resp_headers,
                    media_type=upstream_resp.headers.get("content-type"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")
