"""Bounded, read-only discovery of Codex-hosted Praxist setup work."""

from __future__ import annotations

import copy
import json
import math
import os
import queue
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

import yaml

from praxist.core.redaction import redact_json, redact_text

DEFAULT_REFRESH_SECONDS = 5.0
MAX_CODEX_THREADS = 40
MAX_SETUP_TASKS = 12
MAX_TASKS_PER_WORKSPACE = 8
MAX_SCAN_DIRECTORIES = 512
MAX_SCAN_DEPTH = 3
MAX_RPC_LINE_BYTES = 4 * 1024 * 1024

_DESKTOP_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex")
_SKIP_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "__pycache__",
        "artifacts",
        "build",
        "cache",
        "cover",
        "data",
        "datasets",
        "dist",
        "experiments",
        "node_modules",
        "site",
    }
)
_ACTIVE_RUN_STATES = frozenset({"running", "starting", "status_inconsistent", "unknown"})
_STRONG_PRAXIST_MARKERS = (
    "praxist-takeover",
    "$praxist-",
    "/praxist-",
    "praxist takeover",
    "praxist take over",
)
_OPERATIONAL_MARKERS = (
    "task.yaml",
    "task init",
    "task initialization",
    "forge",
    "shadow",
    "doctor",
    "resolve",
    "daemonize",
    "run id",
    "a0",
    "approval",
    "正式启动",
    "接管",
)
_APPROVAL_MARKERS = ("a0", "approval", "approve", "confirm", "批准", "确认", "审批")


class CodexTaskClient(Protocol):
    """Small read-only surface used by the dashboard collector."""

    def list_threads(self, *, limit: int) -> list[dict[str, Any]]:
        """Return recent thread metadata."""

    def latest_turn_status(self, thread_id: str) -> str | None:
        """Return the most recently persisted turn status when available."""

    def close(self) -> None:
        """Release the app-server subprocess."""


class CodexTaskSource(Protocol):
    """Collector surface consumed by the asynchronous cache."""

    def collect(self, runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Return one setup-task projection."""


class CodexAppServerClient:
    """One short-lived JSONL app-server session for read-only task discovery."""

    def __init__(self, binary: str, *, timeout_seconds: float = 3.0) -> None:
        self.binary = binary
        self.timeout_seconds = timeout_seconds
        self._next_id = 1
        self._responses: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._process = subprocess.Popen(
            [binary, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._reader = threading.Thread(
            target=self._read_responses,
            name="praxist-dashboard-codex-reader",
            daemon=True,
        )
        self._reader.start()
        self._request(
            "initialize",
            {
                "clientInfo": {
                    "name": "praxist-dashboard",
                    "title": "Praxist Dashboard",
                    "version": "1",
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        self._notify("initialized", {})

    def list_threads(self, *, limit: int) -> list[dict[str, Any]]:
        """List recent operator threads without returning conversation bodies."""
        result = self._request(
            "thread/list",
            {
                "limit": max(1, min(limit, MAX_CODEX_THREADS)),
                "sortKey": "updated_at",
                "sourceKinds": ["cli", "vscode", "appServer"],
            },
        )
        data = result.get("data") if isinstance(result, dict) else None
        return [item for item in data or [] if isinstance(item, dict)]

    def latest_turn_status(self, thread_id: str) -> str | None:
        """Read only the newest persisted turn status for one thread."""
        result = self._request(
            "thread/turns/list",
            {
                "threadId": thread_id,
                "limit": 1,
                "sortOrder": "desc",
                "itemsView": "summary",
            },
        )
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            return None
        status_value = data[0].get("status")
        return str(status_value) if status_value else None

    def close(self) -> None:
        """Stop the helper app-server without affecting desktop Codex tasks."""
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=1.0)
        for stream in (self._process.stdin, self._process.stdout):
            if stream is not None and not stream.closed:
                stream.close()

    def _request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._write({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Codex app-server timed out during {method}")
            try:
                response = self._responses.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"Codex app-server timed out during {method}") from exc
            if isinstance(response, BaseException):
                raise OSError(f"Codex app-server ended during {method}: {response}") from response
            if response.get("id") != request_id:
                continue
            error = response.get("error")
            if isinstance(error, dict):
                message = redact_text(str(error.get("message") or "request failed"))[0]
                raise OSError(f"Codex app-server {method} failed: {message}")
            result = response.get("result")
            return result if isinstance(result, dict) else {}

    def _notify(self, method: str, params: Mapping[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def _write(self, payload: Mapping[str, Any]) -> None:
        stream = self._process.stdin
        if stream is None:
            raise OSError("Codex app-server stdin is unavailable")
        try:
            stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
            stream.flush()
        except (BrokenPipeError, OSError) as exc:
            raise OSError("Codex app-server stopped accepting requests") from exc

    def _read_responses(self) -> None:
        stream = self._process.stdout
        if stream is None:
            self._responses.put(OSError("stdout is unavailable"))
            return
        try:
            for line in stream:
                if len(line.encode("utf-8", errors="replace")) > MAX_RPC_LINE_BYTES:
                    self._responses.put(OSError("response line exceeded the dashboard limit"))
                    return
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (
                    isinstance(payload, dict)
                    and "id" in payload
                    and ("result" in payload or "error" in payload)
                ):
                    self._responses.put(payload)
        except (OSError, ValueError) as exc:
            self._responses.put(exc)
        finally:
            if self._process.poll() is not None:
                self._responses.put(OSError("process exited"))


class CodexTaskCollector:
    """Project recent Codex takeover work and discovered task manifests."""

    def __init__(
        self,
        *,
        codex_bin: str | None = None,
        client_factory: Callable[[str], CodexTaskClient] = CodexAppServerClient,
    ) -> None:
        self._codex_bin = codex_bin
        self._client_factory = client_factory

    def collect(self, runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Return a bounded setup-task projection linked to canonical run rows."""
        client: CodexTaskClient | None = None
        binary = ""
        version = ""
        warnings: list[str] = []
        try:
            binary = resolve_codex_operator_binary(self._codex_bin)
            version = codex_binary_version(binary)
            client = self._client_factory(binary)
            threads = client.list_threads(limit=MAX_CODEX_THREADS)
            tasks = _project_setup_tasks(threads, runs, client, warnings, codex_binary=binary)
            return _redacted_payload(
                tasks,
                status="degraded" if warnings else "ready",
                available=True,
                binary_version=version,
                warnings=warnings,
            )
        except Exception as exc:
            warnings.append(redact_text(str(exc))[0])
            return _redacted_payload(
                [],
                status="unavailable",
                available=False,
                binary_version=version,
                warnings=warnings,
            )
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception as exc:
                    warnings.append(redact_text(str(exc))[0])


class CodexTaskCache:
    """Refresh Codex setup discovery off the dashboard request path."""

    def __init__(
        self,
        *,
        collector: CodexTaskSource | None = None,
        refresh_seconds: float = DEFAULT_REFRESH_SECONDS,
    ) -> None:
        if not math.isfinite(refresh_seconds) or refresh_seconds <= 0:
            raise ValueError("Codex task refresh interval must be finite and positive")
        self._collector = collector or CodexTaskCollector()
        self._refresh_seconds = refresh_seconds
        self._lock = threading.RLock()
        self._refreshed_at = 0.0
        self._refreshing = False
        self._payload = _redacted_payload([], status="sampling", available=False)

    def overview(self, runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Return immediately and schedule a refresh when the cache is stale."""
        with self._lock:
            now = time.monotonic()
            if not self._refreshing and now - self._refreshed_at >= self._refresh_seconds:
                self._refreshing = True
                thread = threading.Thread(
                    target=self._refresh,
                    args=(copy.deepcopy(list(runs)),),
                    name="praxist-dashboard-codex-sampler",
                    daemon=True,
                )
                thread.start()
            return copy.deepcopy(self._payload)

    def refresh_now(self, runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Synchronously refresh the cache for tests and explicit diagnostics."""
        self._refresh(copy.deepcopy(list(runs)))
        with self._lock:
            return copy.deepcopy(self._payload)

    def _refresh(self, runs: Sequence[Mapping[str, Any]]) -> None:
        try:
            payload = self._collector.collect(runs)
        except Exception as exc:  # Defensive: injected collectors must not break the dashboard.
            payload = _redacted_payload(
                [],
                status="unavailable",
                available=False,
                warnings=[redact_text(str(exc))[0]],
            )
        with self._lock:
            self._payload = payload
            self._refreshed_at = time.monotonic()
            self._refreshing = False


def resolve_codex_operator_binary(explicit: str | None = None) -> str:
    """Prefer the active desktop/system Codex before the SDK-pinned fallback."""
    if explicit:
        return _require_executable(explicit)
    configured = os.environ.get("CODEX_CLI_PATH", "").strip()
    if configured:
        return _require_executable(configured)
    if _DESKTOP_CODEX.is_file() and os.access(_DESKTOP_CODEX, os.X_OK):
        return str(_DESKTOP_CODEX)
    system = shutil.which("codex")
    if system:
        return system
    from praxist.plugins.agent_runtimes.codex_sdk._auth import resolve_codex_binary

    return _require_executable(resolve_codex_binary())


def codex_binary_version(binary: str) -> str:
    """Return a bounded, redacted Codex version label."""
    try:
        completed = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    label = (completed.stdout or completed.stderr).strip().splitlines()
    return redact_text(label[0][:200])[0] if label else "unknown"


def _require_executable(value: str) -> str:
    expanded = str(Path(value).expanduser()) if os.sep in value else value
    resolved = shutil.which(expanded) if os.sep not in expanded else expanded
    if not resolved or not Path(resolved).is_file() or not os.access(resolved, os.X_OK):
        raise OSError(f"Codex executable is unavailable: {value}")
    return str(Path(resolved).resolve())


def _project_setup_tasks(
    threads: Sequence[Mapping[str, Any]],
    runs: Sequence[Mapping[str, Any]],
    client: CodexTaskClient,
    warnings: list[str],
    *,
    codex_binary: str = "codex",
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    task_roots: dict[str, list[Path]] = {}
    for thread in threads:
        cwd = _safe_directory(thread.get("cwd"))
        if cwd is None:
            continue
        cwd_key = str(cwd)
        if cwd_key not in task_roots:
            task_roots[cwd_key] = discover_task_paths(cwd)
        task_paths = task_roots[cwd_key]
        signal = _thread_signal(thread)
        if not task_paths and not signal:
            continue
        if task_paths and not signal:
            continue
        selected_paths = task_paths or [None]
        for task_path in selected_paths:
            candidates.append(
                _thread_candidate(
                    thread,
                    cwd,
                    task_path,
                    signal,
                    runs,
                    client,
                    warnings,
                    codex_binary=codex_binary,
                )
            )

    seen_paths = {item.get("task_path") for item in candidates if item.get("task_path")}
    for cwd_key, paths in task_roots.items():
        for task_path in paths:
            if str(task_path) not in seen_paths:
                candidates.append(_manifest_candidate(Path(cwd_key), task_path, runs))

    candidates.sort(
        key=lambda item: (_stage_rank(str(item["stage"])), item["updated_sort"]), reverse=True
    )
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        identity = str(candidate.get("task_path") or candidate.get("thread_id"))
        if identity in seen:
            continue
        seen.add(identity)
        candidate.pop("updated_sort", None)
        deduped.append(candidate)
        if len(deduped) >= MAX_SETUP_TASKS:
            break
    return deduped


def _thread_candidate(
    thread: Mapping[str, Any],
    cwd: Path,
    task_path: Path | None,
    signal: str,
    runs: Sequence[Mapping[str, Any]],
    client: CodexTaskClient,
    warnings: list[str],
    *,
    codex_binary: str,
) -> dict[str, Any]:
    thread_id = str(thread.get("id") or "")
    turn_status: str | None = None
    if thread_id:
        try:
            turn_status = client.latest_turn_status(thread_id)
        except Exception as exc:
            warnings.append(f"could not read Codex turn status for {thread_id[:8]}: {exc}")
    linked_run = _linked_run(task_path, runs)
    stage = _task_stage(turn_status, signal, task_path is not None, linked_run)
    updated_at, updated_sort = _timestamp(thread.get("updatedAt") or thread.get("updated_at"))
    title = str(thread.get("name") or thread.get("title") or "Praxist setup task")
    task_name = _task_name(task_path) if task_path else None
    return {
        "id": f"codex:{thread_id}" if thread_id else f"workspace:{cwd}",
        "thread_id": thread_id or None,
        "title": title[:160],
        "project": cwd.name,
        "cwd": str(cwd),
        "task_name": task_name,
        "task_path": str(task_path) if task_path else None,
        "stage": stage,
        "turn_status": turn_status,
        "updated_at": updated_at,
        "updated_sort": updated_sort,
        "resume_command": _resume_command(thread_id, codex_binary) if thread_id else None,
        "linked_run": linked_run,
        "capabilities": {
            "can_copy_resume": bool(thread_id),
            "can_copy_task_path": task_path is not None,
            "can_open_run": linked_run is not None,
        },
    }


def _manifest_candidate(
    cwd: Path, task_path: Path, runs: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    linked_run = _linked_run(task_path, runs)
    try:
        timestamp = task_path.stat().st_mtime
    except OSError:
        timestamp = 0.0
    return {
        "id": f"task:{task_path}",
        "thread_id": None,
        "title": _task_name(task_path) or task_path.name,
        "project": cwd.name,
        "cwd": str(cwd),
        "task_name": _task_name(task_path),
        "task_path": str(task_path),
        "stage": "running" if linked_run and linked_run["state"] in _ACTIVE_RUN_STATES else "ready",
        "turn_status": None,
        "updated_at": _iso_from_epoch(timestamp),
        "updated_sort": timestamp,
        "resume_command": None,
        "linked_run": linked_run,
        "capabilities": {
            "can_copy_resume": False,
            "can_copy_task_path": True,
            "can_open_run": linked_run is not None,
        },
    }


def discover_task_paths(cwd: Path) -> list[Path]:
    """Find nearby external ``task.yaml`` projects without a broad host scan."""
    root = _safe_directory(cwd)
    if root is None or _inside_praxist_source(root):
        return []
    found: list[Path] = []
    pending: deque[tuple[Path, int]] = deque([(root, 0)])
    visited = 0
    while pending and visited < MAX_SCAN_DIRECTORIES and len(found) < MAX_TASKS_PER_WORKSPACE:
        directory, depth = pending.popleft()
        visited += 1
        manifest = directory / "task.yaml"
        if manifest.is_file() and not manifest.is_symlink():
            found.append(directory)
            continue
        if depth >= MAX_SCAN_DEPTH:
            continue
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        children: list[Path] = []
        for entry in entries:
            if entry.name in _SKIP_DIRECTORIES or entry.name.startswith(".") or entry.is_symlink():
                continue
            try:
                if entry.is_dir():
                    children.append(entry)
            except OSError:
                continue
        children.sort(
            key=lambda entry: (
                not any(marker in entry.name.lower() for marker in ("praxist", "research", "task")),
                entry.name,
            )
        )
        for child in reversed(children):
            pending.appendleft((child, depth + 1))
    return found


def _inside_praxist_source(path: Path) -> bool:
    source_root = Path(__file__).resolve().parents[2]
    try:
        path.resolve().relative_to(source_root)
        return True
    except ValueError:
        return False


def _safe_directory(value: object) -> Path | None:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        return None
    try:
        path = Path(value).expanduser().resolve()
        return path if path.is_dir() else None
    except OSError:
        return None


def _thread_signal(thread: Mapping[str, Any]) -> str:
    text = " ".join(
        str(thread.get(key) or "") for key in ("name", "title", "preview", "summary")
    ).lower()
    if any(marker in text for marker in _STRONG_PRAXIST_MARKERS):
        return text
    if "praxist" in text and any(marker in text for marker in _OPERATIONAL_MARKERS):
        return text
    return ""


def _task_stage(
    turn_status: str | None,
    signal: str,
    has_manifest: bool,
    linked_run: dict[str, Any] | None,
) -> str:
    if linked_run and linked_run.get("state") in _ACTIVE_RUN_STATES:
        return "running"
    normalized = str(turn_status or "").lower()
    if normalized in {"inprogress", "in_progress", "running"}:
        return "working"
    if normalized == "failed":
        return "failed"
    if normalized in {"interrupted", "cancelled", "canceled"}:
        return "interrupted"
    if signal and any(marker in signal for marker in _APPROVAL_MARKERS):
        return "awaiting_approval"
    return "ready" if has_manifest else "initializing"


def _linked_run(task_path: Path | None, runs: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    if task_path is None:
        return None
    target = _normalized_path(task_path)
    matches = [run for run in runs if _normalized_path(run.get("task_path")) == target]
    if not matches:
        return None
    matches.sort(key=lambda run: run.get("state") in _ACTIVE_RUN_STATES, reverse=True)
    selected = matches[0]
    return {
        "key": selected.get("key"),
        "run_id": selected.get("run_id"),
        "state": selected.get("state"),
    }


def _normalized_path(value: object) -> str:
    if not isinstance(value, (str, Path)):
        return ""
    try:
        return str(Path(value).expanduser().resolve())
    except OSError:
        return str(value)


def _task_name(task_path: Path | None) -> str | None:
    if task_path is None:
        return None
    manifest = task_path / "task.yaml"
    try:
        if manifest.stat().st_size > 512 * 1024:
            return task_path.name
        value = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError):
        return task_path.name
    if not isinstance(value, dict):
        return task_path.name
    label = value.get("task_name") or value.get("task_id")
    return str(label)[:160] if label else task_path.name


def _resume_command(thread_id: str, binary: str) -> str:
    return shlex.join([binary, "resume", thread_id])


def _timestamp(value: object) -> tuple[str | None, float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _iso_from_epoch(float(value)), float(value)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return value, parsed.timestamp()
        except ValueError:
            return value[:80], 0.0
    return None, 0.0


def _iso_from_epoch(value: float) -> str | None:
    if value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _stage_rank(stage: str) -> int:
    return {
        "running": 6,
        "working": 5,
        "awaiting_approval": 4,
        "failed": 3,
        "interrupted": 2,
        "ready": 1,
        "initializing": 0,
    }.get(stage, 0)


def _redacted_payload(
    tasks: list[dict[str, Any]],
    *,
    status: str,
    available: bool,
    binary_version: str = "",
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    warning_values = [redact_text(str(item))[0] for item in warnings or []]
    payload = {
        "source": {
            "available": available,
            "status": status,
            "protocol": "codex-app-server-jsonl",
            "binary_version": binary_version or None,
            "degraded": bool(warning_values),
            "warnings": warning_values[:8],
        },
        "tasks": tasks,
    }
    redacted, _ = redact_json(cast(Any, payload))
    return redacted if isinstance(redacted, dict) else payload


__all__ = [
    "CodexAppServerClient",
    "CodexTaskCache",
    "CodexTaskCollector",
    "codex_binary_version",
    "discover_task_paths",
    "resolve_codex_operator_binary",
]
