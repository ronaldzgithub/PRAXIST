"""Asynchronous, allowlisted Praxist lifecycle actions for the dashboard."""

from __future__ import annotations

import copy
import json
import math
import os
import shlex
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from praxist.core.redaction import redact_json, redact_text

MAX_CAPTURE_CHARS = 200_000
MAX_ACTION_HISTORY = 100
MAX_ACTIVE_ACTIONS = 4
STOP_ALL_CONFIRMATION = "STOP ALL PRAXIST RUNS"
GC_CONFIRMATION = "GC STALE"
FORCE_RESUME_CONFIRMATION = "FORCE RESUME"


class ActionRejected(ValueError):
    """A dashboard lifecycle request failed validation or conflict checks."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CommandStep:
    """One allowlisted CLI invocation within a dashboard action plan."""

    name: str
    argv: tuple[str, ...]
    timeout_seconds: float

    def redacted_command(self) -> str:
        """Return a shell-readable display string with secret-like values removed."""
        return redact_text(shlex.join(self.argv))[0]


@dataclass(frozen=True)
class ActionPlan:
    """Validated sequence and concurrency scope for one operator action."""

    kind: str
    label: str
    steps: tuple[CommandStep, ...]
    lock_key: str | None = None
    exclusive: bool = False


@dataclass
class ActionRecord:
    """Thread-safe action status exposed to the browser as immutable copies."""

    action_id: str
    kind: str
    label: str
    status: str
    created_at: str
    commands: list[str]
    started_at: str | None = None
    finished_at: str | None = None
    steps: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | list[Any] | str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a detached JSON-compatible action view."""
        return copy.deepcopy(
            {
                "action_id": self.action_id,
                "kind": self.kind,
                "label": self.label,
                "status": self.status,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "commands": self.commands,
                "steps": self.steps,
                "result": self.result,
                "error": self.error,
            }
        )


class CommandRunner:
    """Execute an already validated CLI step without a shell."""

    def run(self, step: CommandStep) -> dict[str, Any]:
        """Run one step and return bounded, redacted structured output."""
        try:
            completed = subprocess.run(
                list(step.argv),
                check=False,
                capture_output=True,
                text=True,
                timeout=step.timeout_seconds,
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _redacted_capture(exc.stdout)
            stderr = _redacted_capture(exc.stderr)
            return {
                "name": step.name,
                "command": step.redacted_command(),
                "status": "timed_out",
                "exit_code": None,
                "stdout": stdout,
                "stderr": stderr,
                "error": f"command timed out after {step.timeout_seconds:g} seconds",
            }
        except OSError as exc:
            return {
                "name": step.name,
                "command": step.redacted_command(),
                "status": "failed",
                "exit_code": None,
                "stdout": "",
                "stderr": "",
                "error": redact_text(str(exc))[0],
            }

        raw_stdout = _bounded_capture(completed.stdout)
        stdout = redact_text(raw_stdout)[0]
        stderr = _redacted_capture(completed.stderr)
        parsed: Any = None
        if raw_stdout.strip():
            try:
                parsed = json.loads(raw_stdout)
            except json.JSONDecodeError:
                parsed = None
        if parsed is not None:
            parsed, _ = redact_json(parsed)
        return {
            "name": step.name,
            "command": step.redacted_command(),
            "status": "succeeded" if completed.returncode == 0 else "failed",
            "exit_code": completed.returncode,
            "output": parsed,
            "stdout": "" if parsed is not None else stdout,
            "stderr": stderr,
        }


class ActionCommandBuilder:
    """Translate typed JSON payloads into a closed set of Praxist CLI calls."""

    def __init__(self, *, executable: str | None = None) -> None:
        self._base = (executable or sys.executable, "-m", "praxist")

    def build(self, kind: str, payload: dict[str, Any]) -> ActionPlan:
        """Validate ``payload`` and build the requested lifecycle plan."""
        if not isinstance(payload, dict):
            raise ActionRejected("action payload must be a JSON object")
        handlers = {
            "doctor": self._doctor,
            "resolve": self._resolve,
            "start": self._start,
            "stop": self._stop,
            "resume": self._resume,
            "gc": self._gc,
            "stop-all": self._stop_all,
        }
        handler = handlers.get(kind)
        if handler is None:
            raise ActionRejected(f"unsupported dashboard action: {kind}", status_code=404)
        return handler(payload)

    def _doctor(self, payload: dict[str, Any]) -> ActionPlan:
        task_path = _task_path(payload)
        argv = [*self._base, "doctor", "--json", "--task-path", task_path]
        argv.extend(_profile_args(payload, command="doctor"))
        return ActionPlan(
            kind="doctor",
            label=f"Check {Path(task_path).name}",
            steps=(CommandStep("doctor", tuple(argv), 120.0),),
        )

    def _resolve(self, payload: dict[str, Any]) -> ActionPlan:
        task_path = _task_path(payload)
        argv = [*self._base, "resolve", task_path]
        argv.extend(_profile_args(payload, command="resolve"))
        result_summary = _optional_arg(payload, "result_summary", max_length=4096)
        if result_summary:
            argv.extend(("--result-summary", result_summary))
        return ActionPlan(
            kind="resolve",
            label=f"Validate {Path(task_path).name}",
            steps=(CommandStep("resolve", tuple(argv), 180.0),),
        )

    def _start(self, payload: dict[str, Any]) -> ActionPlan:
        task_path = _task_path(payload)
        doctor = [*self._base, "doctor", "--json", "--task-path", task_path]
        doctor.extend(_profile_args(payload, command="doctor"))
        resolve = [*self._base, "resolve", task_path]
        resolve.extend(_profile_args(payload, command="resolve"))
        start = [*self._base, "start", "--task-path", task_path, "--daemonize", "--json"]
        start.extend(_profile_args(payload, command="start"))

        strategy = _choice(payload, "strategy", {"auto", "mixed", "explore", "exploit"})
        if strategy:
            start.extend(("--strategy", strategy))
        for key, flag in (("cohort", "--cohort"), ("generations", "--generations")):
            value = _optional_positive_int(payload, key)
            if value is not None:
                start.extend((flag, str(value)))
        run_dir = _optional_arg(payload, "run_dir", max_length=4096)
        if run_dir:
            start.extend(("--run-dir", str(Path(run_dir).expanduser().resolve())))
        if _optional_bool(payload, "server", default=False):
            start.append("--server")
        startup_timeout = _bounded_float(
            payload, "startup_timeout", default=30.0, low=0.0, high=600.0
        )
        start.extend(("--startup-timeout", f"{startup_timeout:g}"))
        return ActionPlan(
            kind="start",
            label=f"Launch {Path(task_path).name}",
            steps=(
                CommandStep("doctor", tuple(doctor), 120.0),
                CommandStep("resolve", tuple(resolve), 180.0),
                CommandStep("start", tuple(start), startup_timeout + 60.0),
            ),
            lock_key=f"task:{task_path}",
        )

    def _stop(self, payload: dict[str, Any]) -> ActionPlan:
        run_id = _run_id(payload.get("run_id"))
        dry_run = _optional_bool(payload, "dry_run", default=False)
        if not dry_run and payload.get("confirm_run_id") != run_id:
            raise ActionRejected("stop confirmation must exactly match the run id")
        grace = _bounded_float(payload, "grace", default=300.0, low=0.0, high=3600.0)
        argv = [*self._base, "stop", run_id, "--grace", f"{grace:g}", "--json"]
        if dry_run:
            argv.append("--dry-run")
        return ActionPlan(
            kind="stop",
            label=("Preview stop " if dry_run else "Stop ") + run_id,
            steps=(CommandStep("stop", tuple(argv), grace + 90.0),),
            lock_key=None if dry_run else f"run:{run_id}",
        )

    def _resume(self, payload: dict[str, Any]) -> ActionPlan:
        target = _required_arg(payload, "target", max_length=4096)
        if payload.get("confirm_target") != target:
            raise ActionRejected("resume confirmation must exactly match the target")
        argv = [*self._base, "resume", target, "--daemonize", "--json"]
        argv.extend(_profile_args(payload, command="resume"))
        strategy = _choice(payload, "strategy", {"auto", "mixed", "explore", "exploit"})
        if strategy:
            argv.extend(("--strategy", strategy))
        for key, flag in (("cohort", "--cohort"), ("generations", "--generations")):
            value = _optional_positive_int(payload, key)
            if value is not None:
                argv.extend((flag, str(value)))
        task_path = _optional_arg(payload, "task_path", max_length=4096)
        if task_path:
            argv.extend(("--task-path", str(Path(task_path).expanduser().resolve())))
        if _optional_bool(payload, "server", default=False):
            argv.append("--server")
        force = _optional_bool(payload, "force", default=False)
        if force:
            if payload.get("confirm_force") != FORCE_RESUME_CONFIRMATION:
                raise ActionRejected(
                    f"forced resume requires the exact confirmation {FORCE_RESUME_CONFIRMATION!r}"
                )
            argv.append("--force")
        startup_timeout = _bounded_float(
            payload, "startup_timeout", default=30.0, low=0.0, high=600.0
        )
        argv.extend(("--startup-timeout", f"{startup_timeout:g}"))
        return ActionPlan(
            kind="resume",
            label=f"Resume {Path(target).name or target}",
            steps=(CommandStep("resume", tuple(argv), startup_timeout + 90.0),),
            lock_key=f"run:{target}",
        )

    def _gc(self, payload: dict[str, Any]) -> ActionPlan:
        dry_run = _optional_bool(payload, "dry_run", default=False)
        if not dry_run and payload.get("confirmation") != GC_CONFIRMATION:
            raise ActionRejected(
                f"registry cleanup requires the exact confirmation {GC_CONFIRMATION!r}"
            )
        argv = [*self._base, "stop", "--gc", "--json"]
        if dry_run:
            argv.append("--dry-run")
        return ActionPlan(
            kind="gc",
            label="Preview registry cleanup" if dry_run else "Clean stale registry entries",
            steps=(CommandStep("gc", tuple(argv), 60.0),),
            exclusive=not dry_run,
        )

    def _stop_all(self, payload: dict[str, Any]) -> ActionPlan:
        if payload.get("confirmation") != STOP_ALL_CONFIRMATION:
            raise ActionRejected(
                f"stop-all requires the exact confirmation {STOP_ALL_CONFIRMATION!r}"
            )
        grace = _bounded_float(payload, "grace", default=300.0, low=0.0, high=3600.0)
        argv = [*self._base, "stop", "--all", "--grace", f"{grace:g}", "--json"]
        scope = _choice(payload, "scope", {"all", "registry", "ps-scan"}) or "all"
        if scope == "registry":
            argv.append("--registry-only")
        elif scope == "ps-scan":
            argv.append("--ps-scan-only")
        return ActionPlan(
            kind="stop-all",
            label=f"Stop all Praxist runs ({scope})",
            steps=(CommandStep("stop-all", tuple(argv), grace + 120.0),),
            exclusive=True,
        )


class ActionManager:
    """Run validated lifecycle plans in bounded background daemon threads."""

    def __init__(
        self,
        *,
        builder: ActionCommandBuilder | None = None,
        runner: CommandRunner | None = None,
        max_active: int = MAX_ACTIVE_ACTIONS,
        max_history: int = MAX_ACTION_HISTORY,
    ) -> None:
        if max_active < 1 or max_history < 1:
            raise ValueError("action limits must be positive")
        self._builder = builder or ActionCommandBuilder()
        self._runner = runner or CommandRunner()
        self._max_active = max_active
        self._max_history = max_history
        self._lock = threading.RLock()
        self._records: dict[str, ActionRecord] = {}
        self._order: list[str] = []
        self._active_plans: dict[str, ActionPlan] = {}
        self._threads: dict[str, threading.Thread] = {}

    def submit(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Validate, enqueue, and return one action record immediately."""
        plan = self._builder.build(kind, payload)
        with self._lock:
            self._check_conflicts(plan)
            action_id = uuid.uuid4().hex
            record = ActionRecord(
                action_id=action_id,
                kind=plan.kind,
                label=plan.label,
                status="queued",
                created_at=_utc_now(),
                commands=[step.redacted_command() for step in plan.steps],
            )
            self._records[action_id] = record
            self._order.append(action_id)
            self._active_plans[action_id] = plan
            self._trim_history()
            thread = threading.Thread(
                target=self._run,
                args=(action_id, plan),
                name=f"praxist-dashboard-{plan.kind}-{action_id[:8]}",
                daemon=True,
            )
            self._threads[action_id] = thread
            thread.start()
            return record.to_dict()

    def get(self, action_id: str) -> dict[str, Any] | None:
        """Return one action record, or ``None`` when it is unknown."""
        with self._lock:
            record = self._records.get(action_id)
            return record.to_dict() if record is not None else None

    def list(self) -> list[dict[str, Any]]:
        """Return newest-first action history."""
        with self._lock:
            return [self._records[action_id].to_dict() for action_id in reversed(self._order)]

    def wait(self, action_id: str, timeout: float | None = None) -> dict[str, Any] | None:
        """Wait for one background action; intended for embedding and tests."""
        with self._lock:
            thread = self._threads.get(action_id)
        if thread is not None:
            thread.join(timeout)
        return self.get(action_id)

    def _check_conflicts(self, plan: ActionPlan) -> None:
        if len(self._active_plans) >= self._max_active:
            raise ActionRejected("too many dashboard actions are already active", status_code=429)
        if plan.exclusive and self._active_plans:
            raise ActionRejected(
                "an exclusive lifecycle action requires an idle action queue", status_code=409
            )
        for active in self._active_plans.values():
            if active.exclusive:
                raise ActionRejected(
                    "an exclusive lifecycle action is already active", status_code=409
                )
            if plan.lock_key and active.lock_key == plan.lock_key:
                raise ActionRejected(
                    "another lifecycle action already owns this task or run",
                    status_code=409,
                )

    def _run(self, action_id: str, plan: ActionPlan) -> None:
        with self._lock:
            record = self._records[action_id]
            record.status = "running"
            record.started_at = _utc_now()
        last_result: Any = None
        try:
            for step in plan.steps:
                result = self._runner.run(step)
                with self._lock:
                    record.steps.append(copy.deepcopy(result))
                if result.get("status") != "succeeded":
                    error = result.get("error") or result.get("stderr") or f"{step.name} failed"
                    with self._lock:
                        record.status = "failed"
                        record.error = redact_text(str(error))[0]
                    break
                last_result = result.get("output")
            else:
                with self._lock:
                    record.status = "succeeded"
                    record.result = copy.deepcopy(last_result)
        except Exception as exc:  # The queue must retain a terminal record on unexpected adapters.
            with self._lock:
                record.status = "failed"
                record.error = redact_text(str(exc))[0]
        finally:
            with self._lock:
                record.finished_at = _utc_now()
                self._active_plans.pop(action_id, None)
                self._threads.pop(action_id, None)
                self._trim_history()

    def _trim_history(self) -> None:
        while len(self._order) > self._max_history:
            oldest = self._order[0]
            if oldest in self._active_plans:
                break
            self._order.pop(0)
            self._records.pop(oldest, None)


def _profile_args(payload: dict[str, Any], *, command: str) -> list[str]:
    argv: list[str] = []
    config_file = _optional_arg(payload, "config_file", max_length=4096)
    if config_file:
        argv.extend(("--config-file", str(Path(config_file).expanduser().resolve())))
    agent_system = _choice(payload, "agent_system", {"claude_sdk", "codex_sdk"})
    if agent_system:
        argv.extend(("--agent-system", agent_system))
    runtime = _optional_arg(payload, "runtime", max_length=512)
    if runtime and command in {"resolve", "start", "resume"}:
        if not runtime.startswith("agent_runtime:"):
            raise ActionRejected("runtime must be an agent_runtime:* plugin reference")
        argv.extend(("--runtime", runtime))
    model_provider = _optional_arg(payload, "model_provider", max_length=512)
    if model_provider:
        if not model_provider.startswith("model_provider:"):
            raise ActionRejected("model_provider must be a model_provider:* plugin reference")
        argv.extend(("--model-provider", model_provider))
    model = _optional_arg(payload, "model", max_length=512)
    if model:
        argv.extend(("--model", model))
    if _optional_bool(payload, "codex_native", default=False):
        argv.append("--codex-native")
    return argv


def _task_path(payload: dict[str, Any]) -> str:
    raw = _required_arg(payload, "task_path", max_length=4096)
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise ActionRejected(f"task path is not a directory: {path}")
    if not (path / "task.yaml").is_file():
        raise ActionRejected(f"task.yaml is missing: {path}")
    return str(path)


def _run_id(value: object) -> str:
    run_id = str(value or "").strip()
    if (
        not run_id
        or len(run_id) > 255
        or "/" in run_id
        or run_id.startswith(".")
        or "\x00" in run_id
    ):
        raise ActionRejected("invalid run id")
    return run_id


def _required_arg(payload: dict[str, Any], key: str, *, max_length: int) -> str:
    value = _optional_arg(payload, key, max_length=max_length)
    if not value:
        raise ActionRejected(f"{key} is required")
    return value


def _optional_arg(payload: dict[str, Any], key: str, *, max_length: int) -> str | None:
    raw = payload.get(key)
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise ActionRejected(f"{key} must be a string")
    value = raw.strip()
    if not value:
        return None
    if len(value) > max_length or "\x00" in value:
        raise ActionRejected(f"{key} is too long or contains an invalid character")
    return value


def _choice(payload: dict[str, Any], key: str, choices: set[str]) -> str | None:
    value = _optional_arg(payload, key, max_length=128)
    if value is None:
        return None
    if value not in choices:
        raise ActionRejected(f"{key} must be one of: {', '.join(sorted(choices))}")
    return value


def _optional_positive_int(payload: dict[str, Any], key: str) -> int | None:
    raw = payload.get(key)
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        raise ActionRejected(f"{key} must be a positive integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ActionRejected(f"{key} must be a positive integer") from exc
    if value <= 0 or value > 100_000:
        raise ActionRejected(f"{key} must be between 1 and 100000")
    return value


def _optional_bool(payload: dict[str, Any], key: str, *, default: bool) -> bool:
    raw = payload.get(key, default)
    if not isinstance(raw, bool):
        raise ActionRejected(f"{key} must be a boolean")
    return raw


def _bounded_float(
    payload: dict[str, Any],
    key: str,
    *,
    default: float,
    low: float,
    high: float,
) -> float:
    raw = payload.get(key, default)
    if isinstance(raw, bool):
        raise ActionRejected(f"{key} must be a number")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ActionRejected(f"{key} must be a number") from exc
    if not math.isfinite(value) or value < low or value > high:
        raise ActionRejected(f"{key} must be between {low:g} and {high:g}")
    return value


def _redacted_capture(value: str | bytes | None) -> str:
    return redact_text(_bounded_capture(value))[0]


def _bounded_capture(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    return text[:MAX_CAPTURE_CHARS]


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


__all__ = [
    "ActionCommandBuilder",
    "ActionManager",
    "ActionPlan",
    "ActionRecord",
    "ActionRejected",
    "CommandRunner",
    "CommandStep",
    "FORCE_RESUME_CONFIRMATION",
    "GC_CONFIRMATION",
    "STOP_ALL_CONFIRMATION",
]
