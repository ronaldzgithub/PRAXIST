"""Dependency-free loopback HTTP server for the Praxist dashboard."""

from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
import socket
import sys
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO, cast
from urllib.parse import unquote, urlsplit

from praxist.dashboard.actions import ActionManager, ActionRejected
from praxist.dashboard.codex_tasks import CodexTaskCache, CodexTaskCollector

if TYPE_CHECKING:
    from praxist.dashboard.state import DashboardStateCache

MAX_REQUEST_BYTES = 64 * 1024
ASSET_ROOT = Path(__file__).resolve().parent / "assets"


class DashboardHTTPError(RuntimeError):
    """An expected HTTP response raised out of a route implementation."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class DashboardApplication:
    """Route read-only snapshots and authenticated lifecycle requests."""

    def __init__(
        self,
        *,
        state: DashboardStateCache | None = None,
        actions: ActionManager | None = None,
        read_only: bool = False,
        control_token: str | None = None,
    ) -> None:
        if state is None:
            from praxist.dashboard.state import DashboardStateCache

            state = DashboardStateCache()
        self.state = state
        self.actions = actions or ActionManager()
        self.read_only = read_only
        self.control_token = control_token or secrets.token_urlsafe(32)
        self.allowed_origins: set[str] = set()

    def get_json(self, path: str) -> tuple[int, dict[str, Any] | list[Any]]:
        """Resolve a JSON GET route."""
        if path == "/api/v1/overview":
            overview = self.state.overview()
            overview["control"] = {
                "read_only": self.read_only,
                "authenticated": True,
                "actions": ["doctor", "resolve", "start", "stop", "resume", "gc", "stop-all"],
            }
            return HTTPStatus.OK, overview
        if path == "/api/v1/actions":
            return HTTPStatus.OK, self.actions.list()
        if path.startswith("/api/v1/actions/"):
            action_id = unquote(path.removeprefix("/api/v1/actions/"))
            action = self.actions.get(action_id)
            if action is None:
                raise DashboardHTTPError(HTTPStatus.NOT_FOUND, "action not found")
            return HTTPStatus.OK, action
        if path.startswith("/api/v1/runs/"):
            from praxist.dashboard.state import DashboardRunNotFound

            run_key = unquote(path.removeprefix("/api/v1/runs/"))
            try:
                return HTTPStatus.OK, self.state.detail(run_key)
            except DashboardRunNotFound as exc:
                raise DashboardHTTPError(HTTPStatus.NOT_FOUND, "run not found") from exc
        if path == "/healthz":
            return HTTPStatus.OK, {"status": "ok", "read_only": self.read_only}
        raise DashboardHTTPError(HTTPStatus.NOT_FOUND, "route not found")

    def post_json(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """Resolve one authenticated lifecycle action route."""
        if self.read_only:
            raise DashboardHTTPError(HTTPStatus.FORBIDDEN, "dashboard is read-only")
        prefix = "/api/v1/actions/"
        if not path.startswith(prefix) or path == prefix:
            raise DashboardHTTPError(HTTPStatus.NOT_FOUND, "route not found")
        kind = unquote(path.removeprefix(prefix))
        try:
            record = self.actions.submit(kind, payload)
        except ActionRejected as exc:
            raise DashboardHTTPError(exc.status_code, str(exc)) from exc
        return HTTPStatus.ACCEPTED, record


class PraxistDashboardServer(ThreadingHTTPServer):
    """Thread-per-request loopback server with one shared application."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        app: DashboardApplication,
        *,
        verbose: bool = False,
    ) -> None:
        self.app = app
        self.verbose = verbose
        super().__init__(server_address, DashboardRequestHandler)
        host, port = self.server_address[:2]
        self.base_url = f"http://{host}:{port}"
        self.allowed_hosts = {
            host,
            f"{host}:{port}",
            "localhost",
            f"localhost:{port}",
            "127.0.0.1",
            f"127.0.0.1:{port}",
        }
        self.app.allowed_origins = {
            self.base_url,
            f"http://localhost:{port}",
            f"http://127.0.0.1:{port}",
        }


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Serve the embedded dashboard and a small versioned JSON API."""

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract.
        try:
            self._require_allowed_host()
            path = urlsplit(self.path).path
            if path in {"/", "/index.html"}:
                self._serve_index(head_only=False)
                return
            if path.startswith("/assets/"):
                self._serve_asset(path, head_only=False)
                return
            status_code, payload = self.dashboard_server.app.get_json(path)
            self._send_json(status_code, payload)
        except DashboardHTTPError as exc:
            self._send_error_json(exc.status, str(exc))
        except Exception:
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "internal dashboard error")

    def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract.
        try:
            self._require_allowed_host()
            path = urlsplit(self.path).path
            if path in {"/", "/index.html"}:
                self._serve_index(head_only=True)
                return
            if path.startswith("/assets/"):
                self._serve_asset(path, head_only=True)
                return
            raise DashboardHTTPError(
                HTTPStatus.METHOD_NOT_ALLOWED, "HEAD is only available for assets"
            )
        except DashboardHTTPError as exc:
            self._send_error_json(exc.status, str(exc), head_only=True)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract.
        try:
            self._require_allowed_host()
            self._require_same_origin()
            self._require_control_token()
            payload = self._read_json_object()
            path = urlsplit(self.path).path
            status_code, result = self.dashboard_server.app.post_json(path, payload)
            self._send_json(status_code, result)
        except DashboardHTTPError as exc:
            self._send_error_json(exc.status, str(exc))
        except Exception:
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "internal dashboard error")

    def do_OPTIONS(self) -> None:  # noqa: N802 - deliberately no CORS contract.
        self._send_error_json(
            HTTPStatus.METHOD_NOT_ALLOWED, "cross-origin requests are not allowed"
        )

    def log_message(self, format: str, *args: object) -> None:
        """Keep request logs opt-in so local paths never appear by default."""
        if self.dashboard_server.verbose:
            super().log_message(format, *args)

    @property
    def dashboard_server(self) -> PraxistDashboardServer:
        """Return the concrete server type attached by ``ThreadingHTTPServer``."""
        return cast(PraxistDashboardServer, self.server)

    def _serve_index(self, *, head_only: bool) -> None:
        try:
            template = (ASSET_ROOT / "index.html").read_text(encoding="utf-8")
        except OSError as exc:
            raise DashboardHTTPError(
                HTTPStatus.INTERNAL_SERVER_ERROR, "dashboard asset missing"
            ) from exc
        body = (
            template.replace("__PRAXIST_CONTROL_TOKEN__", self.dashboard_server.app.control_token)
            .replace(
                "__PRAXIST_READ_ONLY__",
                "true" if self.dashboard_server.app.read_only else "false",
            )
            .encode("utf-8")
        )
        self._send_bytes(
            HTTPStatus.OK,
            body,
            "text/html; charset=utf-8",
            cache_control="no-store",
            head_only=head_only,
        )

    def _serve_asset(self, request_path: str, *, head_only: bool) -> None:
        names = {
            "/assets/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/assets/styles.css": ("styles.css", "text/css; charset=utf-8"),
        }
        item = names.get(request_path)
        if item is None:
            raise DashboardHTTPError(HTTPStatus.NOT_FOUND, "asset not found")
        filename, content_type = item
        try:
            body = (ASSET_ROOT / filename).read_bytes()
        except OSError as exc:
            raise DashboardHTTPError(
                HTTPStatus.INTERNAL_SERVER_ERROR, "dashboard asset missing"
            ) from exc
        self._send_bytes(
            HTTPStatus.OK,
            body,
            content_type,
            cache_control="no-cache",
            head_only=head_only,
        )

    def _read_json_object(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise DashboardHTTPError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json"
            )
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "")
        except ValueError as exc:
            raise DashboardHTTPError(
                HTTPStatus.LENGTH_REQUIRED, "valid Content-Length required"
            ) from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise DashboardHTTPError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body is too large"
            )
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DashboardHTTPError(
                HTTPStatus.BAD_REQUEST, "request body must be valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise DashboardHTTPError(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
        return payload

    def _require_allowed_host(self) -> None:
        host = self.headers.get("Host", "").strip().lower()
        if host not in self.dashboard_server.allowed_hosts:
            raise DashboardHTTPError(HTTPStatus.FORBIDDEN, "unrecognized dashboard host")

    def _require_same_origin(self) -> None:
        origin = self.headers.get("Origin", "").strip()
        if origin and origin not in self.dashboard_server.app.allowed_origins:
            raise DashboardHTTPError(HTTPStatus.FORBIDDEN, "cross-origin control request rejected")

    def _require_control_token(self) -> None:
        supplied = self.headers.get("X-Praxist-Control", "")
        if not hmac.compare_digest(supplied, self.dashboard_server.app.control_token):
            raise DashboardHTTPError(HTTPStatus.FORBIDDEN, "invalid dashboard control token")

    def _send_json(
        self,
        status_code: int,
        payload: dict[str, Any] | list[Any],
        *,
        head_only: bool = False,
    ) -> None:
        body = (json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        self._send_bytes(
            status_code,
            body,
            "application/json; charset=utf-8",
            cache_control="no-store",
            head_only=head_only,
        )

    def _send_error_json(self, status_code: int, message: str, *, head_only: bool = False) -> None:
        self._send_json(
            status_code, {"error": message, "status": int(status_code)}, head_only=head_only
        )

    def _send_bytes(
        self,
        status_code: int,
        body: bytes,
        content_type: str,
        *,
        cache_control: str,
        head_only: bool,
    ) -> None:
        self.send_response(int(status_code))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("Content-Security-Policy", _content_security_policy())
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.end_headers()
        if not head_only:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return


def create_dashboard_server(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    state: DashboardStateCache | None = None,
    actions: ActionManager | None = None,
    read_only: bool = False,
    verbose: bool = False,
) -> PraxistDashboardServer:
    """Create a bound loopback dashboard server without serving requests yet."""
    _require_loopback_host(host)
    if port < 0 or port > 65535:
        raise ValueError("dashboard port must be between 0 and 65535")
    app = DashboardApplication(state=state, actions=actions, read_only=read_only)
    return PraxistDashboardServer((host, port), app, verbose=verbose)


def serve_dashboard(
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    read_only: bool = False,
    sample_interval_seconds: float = 1.0,
    codex_tasks_enabled: bool = True,
    codex_bin: str | None = None,
    as_json: bool = False,
    verbose: bool = False,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the foreground dashboard until interrupted.

    The server remains loopback-only.  Stopping this foreground process never
    signals or modifies any Praxist research run.
    """
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    from praxist.dashboard.state import DashboardStateCache, DashboardStateCollector

    codex_tasks = (
        CodexTaskCache(collector=CodexTaskCollector(codex_bin=codex_bin))
        if codex_tasks_enabled
        else None
    )
    state = DashboardStateCache(
        collector=DashboardStateCollector(codex_tasks=codex_tasks),
        sample_interval_seconds=sample_interval_seconds,
    )
    server = create_dashboard_server(
        host=host,
        port=port,
        state=state,
        read_only=read_only,
        verbose=verbose,
    )
    url = server.base_url + "/"
    if as_json:
        stdout.write(
            json.dumps(
                {
                    "url": url,
                    "host": server.server_address[0],
                    "port": server.server_address[1],
                    "read_only": read_only,
                    "codex_tasks": codex_tasks_enabled,
                    "pid": os_getpid(),
                }
            )
            + "\n"
        )
    else:
        stdout.write(url + "\n")
    stdout.flush()
    stderr.write(
        "Praxist Dashboard is loopback-only. Ctrl-C stops the dashboard; research runs continue.\n"
    )
    if read_only:
        stderr.write("Control actions are disabled for this dashboard session.\n")
    if open_browser:
        try:
            opened = webbrowser.open_new_tab(url)
        except Exception:
            opened = False
        if not opened:
            stderr.write("Could not open a local browser automatically; open the URL above.\n")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        stderr.write("Praxist Dashboard stopped; research runs were not changed.\n")
    finally:
        server.server_close()
    return 0


def _require_loopback_host(host: str) -> None:
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                host, None, family=socket.AF_INET, type=socket.SOCK_STREAM
            )
        }
    except socket.gaierror as exc:
        raise ValueError(f"dashboard host could not be resolved: {host}") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_loopback for address in addresses):
        raise ValueError("dashboard host must resolve only to a loopback address")


def _content_security_policy() -> str:
    return (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self'"
    )


def os_getpid() -> int:
    """Small indirection kept injectable for CLI surface tests."""
    import os

    return os.getpid()


__all__ = [
    "DashboardApplication",
    "PraxistDashboardServer",
    "create_dashboard_server",
    "serve_dashboard",
]
