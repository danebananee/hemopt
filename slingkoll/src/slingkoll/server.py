"""The panel's web server: static files and a small JSON API (standard library)."""

from __future__ import annotations

import hashlib
import json
import logging
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any

from .ha import HAError
from .runner import Runner

_LOGGER = logging.getLogger(__name__)
VERSION = "0.1.2"
TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}


def _static(name: str) -> bytes:
    return (resources.files("slingkoll") / "static" / name).read_bytes()


def _asset_hash() -> str:
    digest = hashlib.sha1()
    for name in ("app.js", "styles.css"):
        digest.update(_static(name))
    return digest.hexdigest()[:10]


def make_handler(runner: Runner, *, demo: bool = False) -> type[BaseHTTPRequestHandler]:
    version = _asset_hash()

    class Handler(BaseHTTPRequestHandler):
        server_version = f"slingkoll/{VERSION}"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter than stderr
            _LOGGER.debug("%s " + fmt, self.address_string(), *args)

        # --- helpers --------------------------------------------------------
        def _send(self, status: int, body: bytes, kind: str) -> None:
            self.send_response(status)
            self.send_header(
                "Content-Type",
                kind + ("; charset=utf-8" if kind.startswith("text") or "json" in kind else ""),
            )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: Any, status: int = 200) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode(), "application/json")

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            data = json.loads(self.rfile.read(length) or b"{}")
            return data if isinstance(data, dict) else {}

        def _route(self) -> str:
            return self.path.split("?", 1)[0].rstrip("/") or "/"

        def _guarded(self, action) -> None:
            try:
                self._json(action())
            except ValueError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            except HAError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_GATEWAY)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("request failed")
                self._json({"error": f"Oväntat fel: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

        # --- routes ---------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802
            route = self._route()
            if route in {"/", "/index.html"}:
                html = _static("index.html").decode().replace("__V__", version)
                self._send(200, html.encode(), "text/html")
            elif route.startswith("/static/"):
                name = route.removeprefix("/static/")
                if "/" in name or name.startswith("."):
                    self._send(404, b"", "text/plain")
                    return
                try:
                    body = _static(name)
                except (FileNotFoundError, IsADirectoryError):
                    self._send(404, b"", "text/plain")
                    return
                self._send(200, body, TYPES.get("." + name.rsplit(".", 1)[-1], "application/octet-stream"))
            elif route == "/api/status":
                payload = runner.status()
                payload["version"] = VERSION
                payload["demo"] = demo
                self._json(payload)
            elif route == "/api/discover":
                self._guarded(runner.rediscover)
            elif route == "/api/series":
                self._json(runner.series())
            else:
                self._send(404, b"", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            route = self._route()
            try:
                body = self._body()
            except json.JSONDecodeError:
                self._json({"error": "Ogiltig JSON"}, HTTPStatus.BAD_REQUEST)
                return
            if route == "/api/quick-check":
                days = float(body.get("days") or 7)
                self._guarded(lambda: runner.quick_check(max(1.0, min(30.0, days))))
            elif route == "/api/test/start":
                self._guarded(runner.start_test)
            elif route == "/api/test/stop":
                self._guarded(lambda: runner.stop_test(analyse_now=bool(body.get("analyse", True))))
            else:
                self._send(404, b"", "text/plain")

        def do_PUT(self) -> None:  # noqa: N802
            if self._route() != "/api/settings":
                self._send(404, b"", "text/plain")
                return
            try:
                body = self._body()
            except json.JSONDecodeError:
                self._json({"error": "Ogiltig JSON"}, HTTPStatus.BAD_REQUEST)
                return
            self._guarded(lambda: runner.update_settings(body).as_dict())

    return Handler


def make_server(runner: Runner, host: str, port: int, *, demo: bool = False) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), make_handler(runner, demo=demo))
    server.daemon_threads = True
    return server
