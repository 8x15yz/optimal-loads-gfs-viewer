"""Bounded, best-effort GET access logging; no response-body buffering."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qsl

from pymongo.errors import OperationFailure

from app.db import get_request_logs_collection

logger = logging.getLogger("weatherapi.requests")
SAFE_QUERY_KEYS = {
    "source", "dataset_code", "model", "variable", "run_time_utc", "step_hours",
    "lat", "lon", "buffer_km", "nw_lon", "nw_lat", "se_lon", "se_lat",
    "expires_in", "product", "path", "key", "limit", "page", "hours",
    "category", "status", "client_ip", "endpoint", "stream", "type", "include_monitoring",
    "status_ecmwf", "variable_ecmwf", "stream_ecmwf", "limit_ecmwf",
    "status_noaa", "variable_noaa", "stream_noaa", "limit_noaa",
}
DATA_PATHS = {
    "/api/griddata", "/api/gridfile", "/api/latest", "/api/latest-forecast",
    "/api/s100/tiles", "/api/s100/forecast-tiles", "/api/sources", "/api/variables",
}


# 중계 서버(Blue-X 등)가 보내는 추적 헤더. 형식이 맞는 값만 기록하고 나머지는 무시함.
#  - X-Request-ID     : 중계 서버의 요청 ID (Blue-X 활동 로그 ID와 동일). 있으면 이 값을 요청 ID로 사용
#  - X-BlueX-Account  : Blue-X 요청 계정 ID
# 누구나 보낼 수 있는 헤더이므로 인증 정보가 아님. 출처 확인은 client_ip·user_agent와 함께 판단.
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}")
ACCOUNT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}")


def header_value(headers, name, pattern):
    raw = headers.get(name)
    if not raw:
        return None
    value = raw.decode("latin-1").strip()
    return value if pattern.fullmatch(value) else None


def category_for(path, matched):
    path = path.rstrip("/") or "/"
    if path == "/monitoring" or path.startswith("/api/monitoring/"):
        return "monitoring"
    if path in DATA_PATHS:
        return "data_api"
    if path.startswith("/api/ingestion/"):
        return "admin_api"
    if path.startswith(("/guide/", "/svgs/")) or path in {"/guide", "/svgs"}:
        return "static"
    return "page" if matched else "unknown"


def safe_query(raw):
    # Never store raw queries, Authorization/Cookie headers or response bodies.
    clipped = raw[:8192].decode("utf-8", errors="replace")
    pairs = parse_qsl(clipped, keep_blank_values=True)
    return [
        {"name": key[:64], "value": value[:256] if key in SAFE_QUERY_KEYS else "[REDACTED]"}
        for key, value in pairs[:32]
    ], len(raw) > 8192 or len(pairs) > 32 or any(len(v) > 256 for _, v in pairs[:32])


class RequestLogWriter:
    def __init__(self):
        self.queue = asyncio.Queue(maxsize=2000)
        self.task = None
        self.collection = None
        self.fallback_count = 0
        self.last_write_ok = None
        self.indexes_ready = False
        self.next_index_attempt = 0.0

    async def start(self):
        self.collection = await get_request_logs_collection()
        # Remove the legacy expiry rule before accepting requests where possible.
        await self._indexes()
        self.task = asyncio.create_task(self._run())

    async def _indexes(self):
        if self.indexes_ready or time.monotonic() < self.next_index_attempt:
            return
        self.next_index_attempt = time.monotonic() + 60
        try:
            async def create():
                indexes = await self.collection.index_information()
                expiry = indexes.get("request_expiry", {})
                if "expireAfterSeconds" in expiry:
                    try:
                        await self.collection.drop_index("request_expiry")
                    except OperationFailure as exc:
                        # Another worker may already have removed the same index.
                        if exc.code != 27:
                            raise
                await self.collection.create_index([("started_at", -1), ("_id", -1)])
                for key in ("category", "path", "client_ip", "request_id", "relay_account"):
                    await self.collection.create_index([(key, 1), ("started_at", -1)])
            await asyncio.wait_for(create(), timeout=5)
            self.indexes_ready = True
        except Exception:
            logger.warning("Request log indexes unavailable; retrying later", exc_info=True)

    def fallback(self, document):
        self.fallback_count += 1
        logger.error("REQUEST_LOG_FALLBACK %s", json.dumps(document, default=str, ensure_ascii=True))

    def submit(self, document):
        # Permanent retention, including when the old environment variable remains set.
        document.pop("expires_at", None)
        try:
            self.queue.put_nowait(document)
        except asyncio.QueueFull:
            self.fallback(document)

    async def _run(self):
        while True:
            await self._indexes()
            batch = [await self.queue.get()]
            try:
                await asyncio.sleep(0.25)
                while len(batch) < 100:
                    try:
                        batch.append(self.queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break
                await asyncio.wait_for(self.collection.insert_many(batch, ordered=False), timeout=2)
                self.last_write_ok = True
            except asyncio.CancelledError:
                for document in batch:
                    self.fallback(document)
                raise
            except Exception:
                self.last_write_ok = False
                for document in batch:
                    self.fallback(document)
            finally:
                for _ in batch:
                    self.queue.task_done()

    async def close(self):
        if self.task is None:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.queue.join(), timeout=8)
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task
        while not self.queue.empty():
            self.fallback(self.queue.get_nowait())
            self.queue.task_done()

    def health(self):
        return {
            "scope": "current_worker", "pending": self.queue.qsize(),
            "fallback_count": self.fallback_count, "last_write_ok": self.last_write_ok,
            "indexes_ready": self.indexes_ready, "retention_days": None,
            "retention_policy": "permanent",
        }


@asynccontextmanager
async def request_logging_lifespan(app):
    writer = RequestLogWriter()
    app.state.request_log_writer = writer
    await writer.start()
    try:
        yield
    finally:
        await writer.close()


class RequestLogMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "GET":
            return await self.app(scope, receive, send)
        writer = scope["app"].state.request_log_writer
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        # _id는 항상 서버가 생성(중복 방지). 표시·검색용 request_id는 중계 서버가 보낸 값을 우선 사용.
        log_id = uuid.uuid4().hex
        request_headers = dict(scope.get("headers", []))
        relay_request_id = header_value(request_headers, b"x-request-id", REQUEST_ID_RE)
        relay_account = header_value(request_headers, b"x-bluex-account", ACCOUNT_RE)
        request_id = relay_request_id or log_id
        status = None
        completed = False
        ended = None
        response_bytes = 0
        error_type = None

        async def observed_send(message):
            nonlocal status, completed, ended, response_bytes
            if message["type"] == "http.response.start":
                status = message["status"]
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [(b"x-request-id", request_id.encode())]
            await send(message)
            if message["type"] == "http.response.body":
                response_bytes += len(message.get("body", b""))
                if not message.get("more_body", False):
                    completed = True
                    ended = time.perf_counter()
            elif message["type"] == "http.response.pathsend":
                completed = True
                ended = time.perf_counter()

        try:
            await self.app(scope, receive, observed_send)
        except BaseException as exc:
            error_type = type(exc).__name__
            if status is None:
                status = 499 if isinstance(exc, asyncio.CancelledError) else 500
            raise
        finally:
            # Access logging must not replace the application's response/exception.
            try:
                route = scope.get("route")
                path = scope.get("path", "/")[:2048]
                matched = route is not None or "endpoint" in scope
                category = category_for(path, matched)
                endpoint = getattr(route, "path", None)
                if not endpoint:
                    if category == "static":
                        endpoint = "/" + path.strip("/").split("/", 1)[0] + "/{path}"
                    elif matched or path.rstrip("/") in DATA_PATHS:
                        endpoint = path.rstrip("/") or "/"
                    else:
                        endpoint = "[unmatched]"
                params, truncated = safe_query(scope.get("query_string", b""))
                headers = request_headers
                writer.submit({
                    "_id": log_id, "request_id": request_id, "started_at": started_at,
                    "request_id_source": "header" if relay_request_id else "server",
                    "relay_account": relay_account,
                    "method": "GET", "path": path,
                    "endpoint": endpoint,
                    "category": category,
                    # Uvicorn resolves forwarded headers only from configured trusted proxies.
                    # Do not trust arbitrary X-Forwarded-For / X-Real-IP here.
                    "client_ip": str((scope.get("client") or ("unknown",))[0])[:128],
                    "user_agent": headers.get(b"user-agent", b"").decode("latin-1")[:512],
                    "query": params, "query_truncated": truncated,
                    "status_code": status if status is not None else 499,
                    "duration_ms": round(((ended or time.perf_counter()) - started) * 1000, 3),
                    "response_bytes": response_bytes, "response_complete": completed,
                    "error_type": error_type,
                })
            except Exception:
                logger.exception("Could not build GET request log")
