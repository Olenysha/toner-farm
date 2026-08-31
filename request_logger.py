"""HTTP-логгер запросов/ответов для Flask.

Пишет в logs/access.log, дублирует в stdout и раздаёт SSE-поток на /api/logs/stream.
Доступ к просмотру логов — только пользователям с ролью edit.
"""
import json
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
from datetime import datetime, timezone

from flask import Response, render_template, request, session

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "access.log")

# SSE-очереди активных клиентов
_queues = []
_queues_lock = threading.Lock()
MAX_QUEUE_SIZE = 200

_logger = logging.getLogger("tonerfarm.requests")
_logger.setLevel(logging.INFO)


def _setup_handlers():
    """Один раз настраиваем файл (с ротацией) и консоль."""
    if _logger.handlers:
        return
    fmt = logging.Formatter("%(asctime)s | %(message)s")

    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    _logger.addHandler(fh)

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    _logger.addHandler(ch)


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _safe_body():
    """Читает тело запроса безопасно, не ломая дальнейшее чтение Flask."""
    try:
        if request.content_length and request.content_length > 50 * 1024:
            return "<TOO_LARGE>"
        ct = (request.content_type or "").lower()
        if "multipart/form-data" in ct:
            # для загрузки файлов логируем только поля формы
            out = {}
            for key, vals in request.form.lists():
                if "password" in key.lower():
                    out[key] = "***"
                else:
                    out[key] = vals[0] if len(vals) == 1 else vals
            return out or None
        data = request.get_data(as_text=True)
        if not data:
            return None
        if "application/json" in ct:
            try:
                return json.loads(data)
            except Exception:
                return data[:2000]
        if "application/x-www-form-urlencoded" in ct:
            out = {}
            for key, vals in request.form.lists():
                if "password" in key.lower():
                    out[key] = "***"
                else:
                    out[key] = vals[0] if len(vals) == 1 else vals
            return out or None
        return data[:2000]
    except Exception as e:
        return f"<READ_ERROR: {e}>"


def _broadcast(record):
    """Отправить запись всем подключённым SSE-клиентам."""
    with _queues_lock:
        dead = []
        for q in _queues:
            try:
                q.put_nowait(record)
            except queue.Full:
                pass
            except Exception:
                dead.append(q)
        for q in dead:
            try:
                _queues.remove(q)
            except ValueError:
                pass


def _build_record(response, start_time, extra=None):
    user = session.get("user")
    body = _safe_body()

    # не светим пароль даже если он пришёл в JSON
    if request.endpoint == "login" and isinstance(body, dict) and "password" in body:
        body["password"] = "***"

    rec = {
        "timestamp": _now_iso(),
        "event": "request",
        "remote_addr": request.remote_addr,
        "real_ip": request.headers.get("X-Forwarded-For", request.remote_addr),
        "method": request.method,
        "path": request.path,
        "query": request.query_string.decode("utf-8", "ignore"),
        "endpoint": request.endpoint,
        "user": user.get("name") if user else None,
        "role": user.get("role") if user else None,
        "user_agent": request.headers.get("User-Agent"),
        "referer": request.headers.get("Referer"),
        "content_type": request.content_type,
        "body": body,
        "status_code": getattr(response, "status_code", None),
        "duration_ms": round((time.perf_counter() - start_time) * 1000, 2),
        "response_size": response.content_length if response else None,
    }
    if extra:
        rec.update(extra)
    return rec


def init_request_logging(app):
    """Подключить логирование к Flask-приложению."""
    _setup_handlers()

    @app.before_request
    def _start_timer():
        request._tf_log_start = time.perf_counter()

    @app.after_request
    def _log_response(response):
        start = getattr(request, "_tf_log_start", None)
        if start is None:
            return response
        rec = _build_record(response, start)
        line = json.dumps(rec, ensure_ascii=False, default=str)
        _logger.info(line)
        _broadcast(rec)
        return response

    @app.teardown_request
    def _log_exception(exc):
        if exc is None:
            return
        start = getattr(request, "_tf_log_start", None)
        if start is None:
            return
        rec = _build_record(None, start, extra={
            "event": "exception",
            "error": str(exc),
            "status_code": 500,
        })
        line = json.dumps(rec, ensure_ascii=False, default=str)
        _logger.info(line)
        _broadcast(rec)

    @app.route("/logs")
    def logs_page():
        import auth
        u = auth.current_user()
        if not u or u.get("role") != "edit":
            return Response(
                "<html><body><h3>Нужен вход с правами edit</h3>"
                '<a href="/login?next=/logs">Войти</a></body></html>',
                status=401,
                mimetype="text/html",
            )
        return render_template("logs.html")

    @app.route("/api/logs/stream")
    def logs_stream():
        import auth
        u = auth.current_user()
        if not u or u.get("role") != "edit":
            return Response(
                "event: auth\ndata: need login\n\n",
                mimetype="text/event-stream",
            )

        q = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        with _queues_lock:
            _queues.append(q)

        def gen():
            try:
                yield "event: ping\ndata: connected\n\n"
                while True:
                    try:
                        rec = q.get(timeout=15)
                    except queue.Empty:
                        yield "event: ping\ndata: keepalive\n\n"
                        continue
                    yield (
                        "event: log\ndata: "
                        + json.dumps(rec, ensure_ascii=False, default=str)
                        + "\n\n"
                    )
            except GeneratorExit:
                pass
            finally:
                with _queues_lock:
                    try:
                        _queues.remove(q)
                    except ValueError:
                        pass

        return Response(
            gen(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
