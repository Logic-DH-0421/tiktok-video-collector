"""One process-wide, privacy-filtered diagnostic event stream."""
from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import traceback
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

SESSION_ID = uuid.uuid4().hex[:12]
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 5
RETENTION_DAYS = 30
_queue: queue.Queue[dict | None] = queue.Queue(maxsize=4096)
_thread: threading.Thread | None = None
_directory: Path | None = None
_level = os.environ.get("TIKTOK_LOG_LEVEL", "INFO").upper()
_order = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
_fallback: deque[dict] = deque(maxlen=200)
_dropped = 0
_last_sink_warning = 0.0
_sink_error_count = 0
_lock = threading.Lock()
_context = threading.local()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _clean_string(value: str, limit: int | None = 500) -> str:
    value = re.sub(r"(?i)(cookie|authorization|token|password|secret|api[_-]?key)\s*[:=]\s*[^\s,;]+", r"\1=<redacted>", value)
    value = re.sub(r"https?://[^\s\"'<>]+", lambda m: _safe_url(m.group(0)), value)
    value = re.sub(r"(?i)C:\\Users\\[^\\\s]+", r"%USERPROFILE%", value)
    return value if limit is None else value[:limit]


def _safe_url(value: str) -> str:
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower()
        video = re.search(r"/video/(\d+)", url.path)
        return f"{host}/video/{video.group(1)}" if video else f"{host}/<path>"
    except Exception:
        return "<url>"


def _safe(key: str, value):
    lower = key.lower()
    if any(word in lower for word in ("cookie", "token", "password", "secret", "authorization", "header", "bodytext", "title", "label", "content", "html")):
        return "<redacted>"
    if isinstance(value, BaseException):
        return _clean_string(str(value))
    if isinstance(value, str):
        if "url" in lower or "href" in lower:
            return _safe_url(value)
        if "path" in lower or "target" in lower or "profile" in lower:
            return Path(value).name or "<path>"
        return _clean_string(value)
    if isinstance(value, dict):
        return {str(k): _safe(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(key, v) for v in value[:30]]
    return value if isinstance(value, (int, float, bool)) or value is None else _clean_string(str(value))


def _write_line(item: dict) -> None:
    global _directory, _last_sink_warning, _sink_error_count
    try:
        assert _directory is not None
        _directory.mkdir(parents=True, exist_ok=True)
        path = _directory / "events.jsonl"
        if path.exists() and path.stat().st_size >= MAX_BYTES:
            for n in range(BACKUPS - 1, 0, -1):
                previous = _directory / f"events.{n}.jsonl"
                if previous.exists():
                    previous.replace(_directory / f"events.{n + 1}.jsonl")
            path.replace(_directory / "events.1.jsonl")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
    except Exception as exc:
        _fallback.append(item)
        _sink_error_count += 1
        now = time.monotonic()
        if now - _last_sink_warning >= 60 or _last_sink_warning == 0:
            _last_sink_warning = now
            try:
                print(f"TikTok diagnostics unavailable ({_sink_error_count} writes): {type(exc).__name__}: {_clean_string(str(exc))}", file=sys.stderr)
            except Exception:
                pass
            _sink_error_count = 0


def _writer() -> None:
    global _dropped
    while True:
        item = _queue.get()
        try:
            if item is None:
                return
            with _lock:
                lost = _dropped
                _dropped = 0
            if lost:
                _write_line({"time": datetime.now().astimezone().isoformat(timespec="milliseconds"), "level": "WARN", "module": "diagnostics", "event": "log.dropped", "session_id": SESSION_ID, "message": "Low-priority log events dropped because queue was full", "count": lost})
            _write_line(item)
        finally:
            _queue.task_done()


def start(directory: Path, *, level: str | None = None) -> None:
    global _thread, _directory, _level
    if _thread is not None:
        return
    _directory = Path(directory)
    try:
        cutoff = time.time() - RETENTION_DAYS * 86400
        for old in _directory.glob("events*.jsonl"):
            if old.stat().st_mtime < cutoff:
                old.unlink()
    except OSError:
        pass
    if level and level.upper() in _order:
        _level = level.upper()
    _thread = threading.Thread(target=_writer, name="diagnostic-writer", daemon=True)
    _thread.start()
    log("INFO", "diagnostics", "session.started", "Diagnostic session started", pid=os.getpid(), log_level=_level, format_version=1)
    _report_interrupted()


def log(level: str, module: str, event: str, message: str, *, operation_id: str | None = None,
        task_id: str | None = None, record_id: int | None = None, outcome: str | None = None,
        duration_ms: int | None = None, error: BaseException | None = None, **fields) -> None:
    global _dropped
    level = level.upper()
    if _order.get(level, 20) < _order.get(_level, 20):
        return
    operation_id = operation_id or getattr(_context, "operation_id", None)
    task_id = task_id or getattr(_context, "task_id", None)
    item = {"time": datetime.now().astimezone().isoformat(timespec="milliseconds"), "level": level,
            "module": module, "event": event, "session_id": SESSION_ID,
            "message": _clean_string(message)}
    for key, value in (("operation_id", operation_id), ("task_id", task_id), ("record_id", record_id),
                       ("outcome", outcome), ("duration_ms", duration_ms)):
        if value is not None:
            item[key] = value
    item.update({key: _safe(key, value) for key, value in fields.items()})
    if error is not None and not isinstance(error, BaseException):
        item["error"] = _clean_string(str(error))
        error = None
    if error is not None:
        item["error_type"] = type(error).__name__
        item["error_code"] = getattr(error, "errno", None) or getattr(error, "code", None)
        item["error"] = _clean_string(str(error))
        item["stack"] = _clean_string("".join(traceback.format_exception(error)), None)
    if _thread is None:
        _fallback.append(item)
        return
    try:
        _queue.put_nowait(item)
    except queue.Full:
        if level in ("ERROR", "WARN") or event.endswith((".finished", ".failed", ".cancelled", ".timeout")):
            try:
                _queue.put(item, timeout=0.2)
            except queue.Full:
                _fallback.append(item)
                try:
                    print(json.dumps(item, ensure_ascii=False), file=sys.stderr)
                except Exception:
                    pass
        else:
            with _lock:
                _dropped += 1


def operation(event: str, module: str, message: str, **fields) -> str:
    operation_id = new_id()
    _context.operation_id = operation_id
    _context.operation_event = event
    _context.operation_module = module
    _context.operation_start = time.monotonic()
    log("INFO", module, "task.requested" if event == "task" else event + ".started",
        message, operation_id=operation_id, **fields)
    return operation_id


def finish(event: str, module: str, operation_id: str, began: float, outcome: str,
           message: str, *, error: BaseException | None = None, **fields) -> None:
    level = "ERROR" if outcome == "failed" else "WARN" if outcome == "timeout" else "INFO"
    log(level, module, event + ".finished", message, operation_id=operation_id, outcome=outcome,
        duration_ms=round((time.monotonic() - began) * 1000), error=error, **fields)
    if getattr(_context, "operation_id", None) == operation_id:
        _context.operation_id = None
        _context.operation_event = None


def fail_current(error: BaseException) -> None:
    op = getattr(_context, "operation_id", None)
    if op:
        finish(getattr(_context, "operation_event", "operation"), getattr(_context, "operation_module", "ui"),
               op, getattr(_context, "operation_start", time.monotonic()), "failed", "UI operation failed", error=error)


def bind(operation_id: str | None, task_id: str | None = None) -> None:
    _context.operation_id = operation_id
    _context.task_id = task_id
    if operation_id is None:
        _context.operation_event = None


def _report_interrupted() -> None:
    """Find prior-session starts without a terminal event; never call them failures."""
    try:
        assert _directory is not None
        files = sorted(_directory.glob("events*.jsonl"), key=lambda p: p.stat().st_mtime)[-BACKUPS-1:]
        starts: dict[str, dict] = {}
        sessions: dict[str, dict] = {}
        for path in files:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                op = item.get("operation_id")
                if not op or item.get("session_id") == SESSION_ID:
                    prior = item.get("session_id")
                    if prior and prior != SESSION_ID:
                        state = sessions.setdefault(prior, {"started": False, "closed": False})
                        if item.get("event") == "app.starting":
                            state["started"] = True
                        if item.get("event") in {"app.exited", "app.self_test"}:
                            state["closed"] = True
                    continue
                if str(item.get("event", "")).endswith(".started"):
                    starts[op] = item
                elif str(item.get("event", "")).endswith(".finished"):
                    starts.pop(op, None)
        for op, item in list(starts.items())[-50:]:
            log("WARN", "diagnostics", "operation.interrupted", "Previous session operation has no terminal event; outcome unknown",
                operation_id=op, prior_session_id=item.get("session_id"), prior_event=item.get("event"))
        for prior, state in list(sessions.items())[-10:]:
            if state["started"] and not state["closed"]:
                log("WARN", "diagnostics", "session.previous_unclosed", "Prior session has no normal exit event; cause unknown",
                    prior_session_id=prior)
    except Exception:
        pass


def export(destination: Path) -> Path:
    """Export only already-redacted JSONL, not browser profiles or legacy raw logs."""
    import zipfile
    if _thread is not None:
        _queue.join()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if _directory is not None:
            for path in sorted(_directory.glob("events*.jsonl")):
                archive.write(path, arcname=path.name)
    return destination


def close() -> None:
    global _thread
    if _thread is None:
        return
    _queue.join()
    _queue.put(None)
    _thread.join(timeout=3)
    _thread = None


def fallback_count() -> int:
    return len(_fallback)
