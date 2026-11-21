#!/usr/bin/env python3
"""Минимальный приёмник отметок менеджера для страницы цен WB.

Слушает 127.0.0.1:8092. Nginx (или другой прокси) пробрасывает сюда
/wb-feedback и /run/feedback. Токен отметки – FB_KEY из .env корня проекта.

Запуск:
  WB_REPRICER_ROOT=/opt/wb-repricer python3 control/feedback_server.py

Переменные:
  WB_REPRICER_ROOT  – корень репозитория (по умолчанию родитель control/)
  FB_HOST           – 127.0.0.1
  FB_PORT           – 8092
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(os.environ.get("WB_REPRICER_ROOT") or Path(__file__).resolve().parents[1])
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "op"))

def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("\"'")
    env.update({k: v for k, v in os.environ.items()})
    return env


ENV = load_env()
FB_KEY = ENV.get("FB_KEY") or ""
FB_KEY_REQUIRED = ENV.get("FB_KEY_REQUIRED", "").lower() in ("1", "true", "yes")

try:
    import feedback as fb_main
except Exception as e:
    fb_main = None
    FB_MAIN_ERR = str(e)
else:
    FB_MAIN_ERR = None

try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("op_feedback", ROOT / "op" / "feedback.py")
    fb_op = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fb_op)
    FB_OP_ERR = None
except Exception as e:
    fb_op = None
    FB_OP_ERR = str(e)


def pick_mod(campaign: str):
    if campaign == "op":
        if not fb_op:
            raise RuntimeError(f"op/feedback.py не загружен: {FB_OP_ERR}")
        return fb_op
    if not fb_main:
        raise RuntimeError(f"feedback.py не загружен: {FB_MAIN_ERR}")
    return fb_main


def check_key(headers) -> bool:
    if not FB_KEY_REQUIRED and not FB_KEY:
        return True
    got = headers.get("X-FB-Key") or ""
    auth = headers.get("Authorization") or ""
    if got and FB_KEY and got == FB_KEY:
        return True
    if auth.startswith("Basic ") and FB_KEY:
        # совместимость: basic auth уже на nginx; ключ опционален
        return True
    if FB_KEY and got == FB_KEY:
        return True
    return not FB_KEY_REQUIRED


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-FB-Key, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, code: int, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/health", "/"):
            return self._json(200, {"ok": True, "root": str(ROOT)})
        if u.path in ("/wb-feedback", "/run/feedback"):
            if not check_key(self.headers):
                return self._json(401, {"ok": False, "error": "нужен X-FB-Key"})
            qs = parse_qs(u.query)
            try:
                mod = pick_mod((qs.get("c") or ["main"])[0])
                data = mod.state_for(qs)
                return self._json(200, {"ok": True, "data": data})
            except Exception as e:
                return self._json(400, {"ok": False, "error": str(e)})
        return self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        u = urlparse(self.path)
        if u.path not in ("/wb-feedback", "/run/feedback"):
            return self._json(404, {"ok": False, "error": "not found"})
        if not check_key(self.headers):
            return self._json(401, {"ok": False, "error": "нужен X-FB-Key"})
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return self._json(400, {"ok": False, "error": "неверный JSON"})
        try:
            mod = pick_mod(payload.get("c") or "main")
            result = mod.mark(payload)
            return self._json(200, {"ok": True, "result": result})
        except Exception as e:
            return self._json(400, {"ok": False, "error": str(e)})


def main():
    host = ENV.get("FB_HOST", "127.0.0.1")
    port = int(ENV.get("FB_PORT", "8092"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"[feedback] {host}:{port} root={ROOT}", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
