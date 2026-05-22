"""Structured logging for WineTone.

Replaces uvicorn's default "INFO: 127.0.0.1 - GET / 200" with JSON
lines that include request_id, path, status, latency_ms, and any
extra context the handler attached. Easier to grep through, easier to
ship to a log aggregator later.

Two pieces:

  1. JsonFormatter — Python logging formatter that serializes
     LogRecord into a single JSON line per entry.
  2. RequestIdMiddleware — assigns each HTTP request a UUIDv4
     request_id, threads it through a contextvar so downstream
     loggers can include it, and emits a single access-log line
     per request with method/path/status/duration.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware

_request_id_var: ContextVar[str] = ContextVar("winetone_request_id", default="")


def current_request_id() -> str:
    """Return the request_id of the in-flight request, or '' outside one."""
    return _request_id_var.get()


class JsonFormatter(logging.Formatter):
    """JSON line per log entry. Stable schema across log statements."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        # %f isn't supported by logging.Formatter.formatTime — build
        # the ISO-with-millis timestamp ourselves from record.created.
        from datetime import datetime, timezone
        ts = datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        payload: dict = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        rid = current_request_id()
        if rid:
            payload["request_id"] = rid
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info).splitlines()[-1]
        # Pick up any extras attached to the record.
        for k, v in record.__dict__.items():
            if k in (
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process",
                "asctime", "message", "taskName",
            ):
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except (TypeError, ValueError):
                payload[k] = repr(v)
        return json.dumps(payload, default=str)


def configure(level: str = "INFO") -> None:
    """Wire up the root logger to emit JSON lines."""
    root = logging.getLogger()
    # Clear any handlers uvicorn or Rich may have installed.
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    # uvicorn's access log is too noisy when we have our own per-request
    # access log line; shush it down to WARNING.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Assign each request a UUID, expose via contextvar, log one
    access line per request with method/path/status/latency."""

    async def dispatch(self, request, call_next):
        rid = request.headers.get("x-request-id") or str(uuid.uuid4())
        token = _request_id_var.set(rid)
        t0 = time.monotonic()
        log = logging.getLogger("winetone.access")
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:  # noqa: BLE001
            status = 500
            raise
        finally:
            duration_ms = (time.monotonic() - t0) * 1000
            log.info(
                "%s %s -> %d",
                request.method, request.url.path, status,
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "duration_ms": round(duration_ms, 1),
                },
            )
            _request_id_var.reset(token)
        # Echo request_id back to the client so external tools can
        # correlate when they hit a problem.
        response.headers["X-Request-Id"] = rid
        return response
