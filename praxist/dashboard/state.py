"""Read-only dashboard snapshots composed from canonical Praxist surfaces."""

from __future__ import annotations

import copy
import json
import math
import os
import socket
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from praxist.cli import monitor, status
from praxist.core.redaction import redact_json, redact_text
from praxist.plugins.workflow_stages.research_loop.backend.orchestrator_status import (
    read_effective_orchestrator_status,
)

DASHBOARD_SCHEMA_VERSION = 1
DEFAULT_SAMPLE_INTERVAL_SECONDS = 1.0
MAX_JSON_BYTES = 1_000_000
MAX_PEERS = 200
MAX_FRONTIER_ENTRIES_PER_LANE = 12

_LIVE_STATES = frozenset({"running", "starting", status.STATE_INCONSISTENT, "unknown"})
_RESUMABLE_STATES = frozenset({"stale", "stopped", status.STATE_COMPLETED, status.STATE_FAILED})


class DashboardRunNotFound(LookupError):
    """Raised when a dashboard row key no longer resolves to a known run."""


@dataclass(frozen=True)
class DashboardSnapshot:
    """One immutable host-wide sample shared by every connected browser."""

    sampled_at_monotonic: float
    rows: tuple[status.StatusRow, ...]
    overview: dict[str, Any]


class DashboardStateCollector:
    """Compose host-wide and run-detail views without mutating run artifacts."""

    def collect(self) -> DashboardSnapshot:
        """Collect one bounded registry, artifact, peer, and hardware sample."""
        errors: list[str] = []
        rows = status.collect_status_rows(
            errors=errors,
            include_peer_health=True,
            process_probe_timeout=1.0,
        )
        hardware = monitor.collect_hardware_snapshot(probe_timeout=1.0)
        run_views = [self._run_summary(row) for row in rows]
        counts = _run_counts(run_views)
        warnings = [*errors, *hardware.warnings]
        overview = {
            "schema_version": DASHBOARD_SCHEMA_VERSION,
            "generated_at": _utc_now(),
            "refresh_after_ms": 2000,
            "host": {
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "cpu_count": os.cpu_count(),
                "loadavg": hardware.loadavg,
                "memory": hardware.memory,
                "gpus": list(hardware.gpus),
            },
            "counts": counts,
            "runs": run_views,
            "warnings": _redacted_strings(warnings),
            "authority": {
                "metrics": "result and finding summaries",
                "frontier": "frontier/frontier_manifest.json",
                "gems": "gems/gems_state.json",
                "boundaries": "gen_N/generation_boundary.json",
                "operational": "registry, process probe, and orchestrator status",
            },
        }
        return DashboardSnapshot(
            sampled_at_monotonic=time.monotonic(),
            rows=tuple(rows),
            overview=overview,
        )

    def collect_detail(self, row: status.StatusRow) -> dict[str, Any]:
        """Read bounded, known artifacts for one selected status row."""
        summary = self._run_summary(row)
        run_dir = _usable_run_dir(row.run_dir)
        orchestrator: dict[str, Any] = {}
        run_summary: dict[str, Any] = {}
        frontier: dict[str, Any] = {}
        gems: dict[str, Any] = {}
        scheduler: dict[str, Any] = {}
        recent_logs: list[str] = []
        resume_plan: dict[str, Any] = {"available": False}
        warnings = list(summary.get("warnings", []))

        if run_dir is not None:
            orchestrator = read_effective_orchestrator_status(run_dir)
            run_summary = _bounded_json_object(run_dir / "run_summary.json")
            frontier = _compact_frontier(
                _bounded_json_object(run_dir / "frontier" / "frontier_manifest.json")
            )
            gems = _compact_gems(_bounded_json_object(run_dir / "gems" / "gems_state.json"))
            scheduler = _compact_scheduler(
                _bounded_json_object(run_dir / "resource_scheduler" / "status.json")
            )
            recent_logs = [
                redact_text(line)[0] for line in monitor.tail_recent_logs(run_dir, max_lines=80)
            ]
            if row.state in _RESUMABLE_STATES:
                resume_plan = _inspect_resume_plan(run_dir)

        redacted_orchestrator, _ = redact_json(orchestrator)
        redacted_summary, _ = redact_json(_compact_run_summary(run_summary))
        return {
            "schema_version": DASHBOARD_SCHEMA_VERSION,
            "generated_at": _utc_now(),
            "run": summary,
            "orchestrator_status": redacted_orchestrator,
            "runtime_summary": redacted_summary,
            "frontier": frontier,
            "gems": gems,
            "resource_scheduler": scheduler,
            "peers": copy.deepcopy(row.peers[:MAX_PEERS]),
            "recent_logs": recent_logs,
            "resume_plan": resume_plan,
            "warnings": _redacted_strings(warnings),
        }

    def _run_summary(self, row: status.StatusRow) -> dict[str, Any]:
        run_dir = _usable_run_dir(row.run_dir)
        orchestrator = read_effective_orchestrator_status(run_dir) if run_dir else {}
        current_generation = _safe_int(
            orchestrator.get("current_generation"),
            fallback=row.generation,
        )
        reported_completed = _safe_int(orchestrator.get("generations_completed"), fallback=0) or 0
        max_generations = _safe_int(orchestrator.get("max_generations"), fallback=None)
        committed_boundaries = _contiguous_boundary_count(run_dir) if run_dir else 0
        phase = "missing-run-directory" if row.run_dir and run_dir is None else "unavailable"
        boundary = _generation_boundary_state(run_dir, current_generation)
        if run_dir is not None:
            phase = monitor.infer_run_phase(
                run_dir=run_dir,
                row=row,
                orchestrator_status=orchestrator,
            )

        warnings: list[str] = []
        if reported_completed != committed_boundaries:
            warnings.append(
                "reported completed generations differ from contiguous committed boundaries "
                f"({reported_completed} reported, {committed_boundaries} committed)"
            )
        probe_error = row.extras.get("probe_error")
        if probe_error:
            warnings.append(probe_error)
        if row.state == status.STATE_INCONSISTENT:
            warnings.append("registry state is stopped while an identity-matched process is live")

        progress_percent: float | None = None
        if max_generations and max_generations > 0:
            progress_percent = min(100.0, committed_boundaries * 100.0 / max_generations)

        key = _row_key(row)
        payload = {
            **row.to_dict(),
            "key": key,
            "phase": phase,
            "task_name": orchestrator.get("task_name") or orchestrator.get("task_id"),
            "progress": {
                "current_generation": current_generation,
                "max_generations": max_generations,
                "reported_completed": reported_completed,
                "committed_boundaries": committed_boundaries,
                "percent": progress_percent,
                "cohort_size": _safe_int(orchestrator.get("cohort_size"), fallback=None),
                "findings_total": _safe_int(
                    orchestrator.get("findings_total"),
                    fallback=row.findings_total,
                ),
                "variants_total": _safe_int(orchestrator.get("variants_total"), fallback=None),
                "variants_above_baseline": _safe_int(
                    orchestrator.get("variants_above_baseline"), fallback=None
                ),
                "frontier_candidates": _safe_int(
                    orchestrator.get("frontier_candidates"), fallback=None
                ),
                "gems_count": _safe_int(orchestrator.get("gems_count"), fallback=None),
                "strategy": orchestrator.get("strategy"),
                "exit_condition": orchestrator.get("exit_condition"),
                "boundary": boundary,
            },
            "best_mature_result": _compact_result(orchestrator.get("best_mature_result")),
            "best_validation_signal": _compact_result(
                orchestrator.get("best_validation_signal"), include_reason=True
            ),
            "capabilities": {
                "can_stop": bool(row.run_id and row.source == status.SOURCE_REGISTRY)
                and row.state in _LIVE_STATES,
                "can_resume": bool(row.run_id or row.run_dir)
                and row.source != status.SOURCE_REMOTE
                and row.state in _RESUMABLE_STATES,
                "can_gc": bool(row.run_id and row.source == status.SOURCE_STALE),
                "can_monitor": bool(row.run_id or row.run_dir),
            },
            "warnings": _redacted_strings(warnings),
        }
        redacted, _ = redact_json(cast(Any, payload))
        return redacted if isinstance(redacted, dict) else payload


class DashboardStateCache:
    """Share a one-Hz maximum state sample across all dashboard clients."""

    def __init__(
        self,
        *,
        collector: DashboardStateCollector | None = None,
        sample_interval_seconds: float = DEFAULT_SAMPLE_INTERVAL_SECONDS,
    ) -> None:
        if not math.isfinite(sample_interval_seconds) or sample_interval_seconds < 1.0:
            raise ValueError("dashboard sample interval must be finite and at least one second")
        self._collector = collector or DashboardStateCollector()
        self._sample_interval = sample_interval_seconds
        self._lock = threading.RLock()
        self._snapshot: DashboardSnapshot | None = None
        self._details: dict[str, dict[str, Any]] = {}

    def overview(self, *, force: bool = False) -> dict[str, Any]:
        """Return a deep copy of the current host-wide overview."""
        snapshot = self._sample(force=force)
        return copy.deepcopy(snapshot.overview)

    def detail(self, run_key: str) -> dict[str, Any]:
        """Return one selected run detail from the same shared sample."""
        snapshot = self._sample()
        with self._lock:
            cached = self._details.get(run_key)
            if cached is not None:
                return copy.deepcopy(cached)
            row = next(
                (candidate for candidate in snapshot.rows if _row_key(candidate) == run_key), None
            )
            if row is None:
                raise DashboardRunNotFound(run_key)
            detail = self._collector.collect_detail(row)
            self._details[run_key] = detail
            return copy.deepcopy(detail)

    def _sample(self, *, force: bool = False) -> DashboardSnapshot:
        with self._lock:
            now = time.monotonic()
            current = self._snapshot
            if (
                not force
                and current is not None
                and now - current.sampled_at_monotonic < self._sample_interval
            ):
                return current
            sampled = self._collector.collect()
            self._snapshot = sampled
            self._details.clear()
            return sampled


def _row_key(row: status.StatusRow) -> str:
    if row.run_id:
        return f"run:{row.run_id}"
    return f"pid:{row.pid}"


def _usable_run_dir(value: str | None) -> Path | None:
    if not value:
        return None
    try:
        path = Path(value).expanduser()
        return path if path.is_dir() else None
    except OSError:
        return None


def _contiguous_boundary_count(run_dir: Path) -> int:
    count = 0
    while count < 100_000 and (run_dir / f"gen_{count}" / "generation_boundary.json").is_file():
        count += 1
    return count


def _generation_boundary_state(run_dir: Path | None, generation: int | None) -> dict[str, bool]:
    if run_dir is None or generation is None or generation < 0:
        return {"stop_signal": False, "generation_results": False, "committed": False}
    generation_dir = run_dir / f"gen_{generation}"
    return {
        "stop_signal": (generation_dir / "STOP_SIGNAL").is_file()
        or (generation_dir / "CLOSING_SIGNAL").is_file(),
        "generation_results": (generation_dir / "generation_results.json").is_file(),
        "committed": (generation_dir / "generation_boundary.json").is_file(),
    }


def _bounded_json_object(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_JSON_BYTES:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _compact_frontier(payload: dict[str, Any]) -> dict[str, Any]:
    lane_frontiers = payload.get("lane_frontiers")
    lanes: dict[str, dict[str, Any]] = {}
    if isinstance(lane_frontiers, dict):
        for lane, raw_entries in lane_frontiers.items():
            entries = raw_entries if isinstance(raw_entries, list) else []
            lanes[str(lane)] = {
                "count": len(entries),
                "entries": [
                    _compact_frontier_entry(entry)
                    for entry in entries[:MAX_FRONTIER_ENTRIES_PER_LANE]
                ],
            }
    elif isinstance(payload.get("cumulative_top"), list):
        entries = payload["cumulative_top"]
        lanes["frontier"] = {
            "count": len(entries),
            "entries": [
                _compact_frontier_entry(entry) for entry in entries[:MAX_FRONTIER_ENTRIES_PER_LANE]
            ],
        }
    validation = payload.get("validation_candidates")
    validation_count = 0
    if isinstance(validation, dict) and isinstance(validation.get("cumulative"), list):
        validation_count = len(validation["cumulative"])
    return {
        "lanes": lanes,
        "validation_candidates": validation_count,
        "updated_at": payload.get("updated_at"),
    }


def _compact_frontier_entry(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    extra = value.get("extra") if isinstance(value.get("extra"), dict) else {}
    metrics = value.get("metrics") if isinstance(value.get("metrics"), dict) else {}
    fields = {
        "finding_id": value.get("finding_id") or value.get("id"),
        "variant_name": _coalesce(value.get("variant_name"), extra.get("variant_name")),
        "generation_id": _coalesce(value.get("generation_id"), extra.get("generation_id")),
        "metric_name": _coalesce(value.get("metric_name"), extra.get("metric_name")),
        "metric_value": _coalesce(value.get("metric_value"), extra.get("metric_value")),
        "evidence_stage": _coalesce(value.get("evidence_stage"), extra.get("evidence_stage")),
        "promotion_eligible": value.get("promotion_eligible"),
        "metrics": {key: metrics[key] for key in list(metrics)[:8]},
    }
    redacted, _ = redact_json(fields)
    return redacted if isinstance(redacted, dict) else fields


def _compact_gems(payload: dict[str, Any]) -> dict[str, Any]:
    entries = payload.get("gems") if isinstance(payload.get("gems"), list) else []
    compact = [_compact_frontier_entry(entry) for entry in entries[:MAX_FRONTIER_ENTRIES_PER_LANE]]
    return {
        "cycle_index": payload.get("cycle_index"),
        "logical_generation": payload.get("logical_generation"),
        "reset_count": payload.get("reset_count"),
        "pending_reset": payload.get("pending_reset"),
        "selection_policy": payload.get("selection_policy"),
        "min_mature_eval_units": payload.get("min_mature_eval_units"),
        "evidence_stage_min_units": payload.get("evidence_stage_min_units"),
        "count": len(entries),
        "entries": compact,
    }


def _compact_scheduler(payload: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "mode",
        "queued",
        "running",
        "completed",
        "failed",
        "rejected",
        "concurrency_limit",
        "admission_closed",
        "frozen_generations",
        "assessment_state",
        "running_activity",
        "peer_capacity_blocked",
        "queue_blocked_reasons",
        "accelerator_probe",
        "supply_stats",
        "work_class_mix",
        "release_pending",
    )
    compact = {key: payload.get(key) for key in keys if key in payload}
    redacted, _ = redact_json(compact)
    return redacted if isinstance(redacted, dict) else compact


def _compact_run_summary(payload: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "status",
        "exit_condition",
        "started_at",
        "finished_at",
        "current_generation",
        "generations_completed",
        "runtime_usage",
        "warnings",
    )
    return {key: payload.get(key) for key in keys if key in payload}


def _compact_result(value: object, *, include_reason: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    keys = [
        "variant_name",
        "metric_name",
        "metric_value",
        "generation_id",
        "evidence_stage",
        "baseline_relation",
        "maturity_basis",
        "promotion_status",
    ]
    if include_reason:
        keys.append("validation_reason")
    return {key: value.get(key) for key in keys if key in value}


def _inspect_resume_plan(run_dir: Path) -> dict[str, Any]:
    """Return the public recovery plan for an offline run, without modifying it."""
    try:
        startup = _bounded_json_object(run_dir / "startup_config.json")
        canonical = startup.get("canonical_args")
        canonical = canonical if isinstance(canonical, dict) else {}
        task_project = startup.get("task_project")
        task_project = task_project if isinstance(task_project, dict) else {}
        task_path_raw = canonical.get("task_path") or task_project.get("path")
        if not task_path_raw:
            return {"available": False, "warning": "task path is unavailable"}
        task_path = Path(str(task_path_raw)).expanduser()
        task_spec_path = task_path / "task.yaml"
        if not task_spec_path.is_file():
            return {"available": False, "warning": "task.yaml is unavailable"}
        from praxist.plugins.workflow_stages.research_loop.backend.resume_state import (
            inspect_resume_plan,
        )
        from praxist.task_spec import load_task_spec

        spec = load_task_spec(str(task_spec_path))
        plan = inspect_resume_plan(
            run_dir,
            max_generations=spec.generation_policy.max_generations,
            pi_enabled=bool(getattr(spec.multi_pi, "enabled", False)),
        ).to_dict()
        compact = {
            key: plan.get(key)
            for key in (
                "start_generation",
                "completed_generations",
                "pending_boundary_generation",
                "warnings",
            )
            if key in plan
        }
        compact["available"] = True
        redacted, _ = redact_json(compact)
        return redacted if isinstance(redacted, dict) else compact
    except Exception as exc:  # Recovery inspection is advisory; resume remains CLI-owned.
        return {"available": False, "warning": redact_text(str(exc))[0]}


def _run_counts(runs: list[dict[str, Any]]) -> dict[str, int]:
    active = sum(1 for run in runs if run.get("state") in _LIVE_STATES)
    attention = sum(1 for run in runs if run.get("warnings"))
    findings = 0
    committed = 0
    for run in runs:
        progress = run.get("progress") if isinstance(run.get("progress"), dict) else {}
        findings += _safe_int(progress.get("findings_total"), fallback=0) or 0
        committed += _safe_int(progress.get("committed_boundaries"), fallback=0) or 0
    return {
        "total": len(runs),
        "active": active,
        "attention": attention,
        "findings": findings,
        "committed_generations": committed,
    }


def _safe_int(value: object, *, fallback: int | None) -> int | None:
    if isinstance(value, bool):
        return fallback
    try:
        return int(value) if isinstance(value, (int, float, str)) else fallback
    except (TypeError, ValueError):
        return fallback


def _coalesce(primary: Any, fallback: Any) -> Any:
    return fallback if primary is None else primary


def _redacted_strings(values: list[str]) -> list[str]:
    return [redact_text(str(value))[0] for value in values if str(value).strip()]


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()
