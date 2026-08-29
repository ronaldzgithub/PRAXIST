"""Contracts for the local Praxist browser dashboard and control API."""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def _row(run_dir: str, **overrides: object):
    from praxist.cli.status import SOURCE_REGISTRY, StatusRow

    values: dict[str, object] = {
        "pid": 4321,
        "ppid": 1,
        "etime": "00:42",
        "command": f"python -m praxist.run run --run-dir {run_dir}",
        "run_dir": run_dir,
        "source": SOURCE_REGISTRY,
        "state": "running",
        "run_id": "run_demo",
        "task_path": "/tasks/demo",
        "model": "gpt-demo",
        "model_provider_ref": "model_provider:openai_compatible",
        "started_at": "2026-08-29T01:00:00+00:00",
        "generation": 1,
        "findings_total": 7,
        "updated_at": "2026-08-29T01:01:00+00:00",
        "peer_health_summary": {"green": 2, "yellow": 1, "red": 0},
        "peers": [
            {
                "peer_id": "gen1_peer0",
                "health": "green",
                "research_state": "evaluating",
                "active_variant": "variant_a",
                "best_metric_value": 1.2,
            }
        ],
    }
    values.update(overrides)
    return StatusRow(**values)


class DashboardStateTest(unittest.TestCase):
    def test_collector_projects_canonical_progress_evidence_logs_and_warnings(self) -> None:
        from praxist.cli.monitor import HardwareSnapshot
        from praxist.dashboard.state import DashboardStateCollector

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run_demo"
            (run_dir / "gen_0").mkdir(parents=True)
            (run_dir / "gen_0" / "generation_boundary.json").write_text("{}", encoding="utf-8")
            (run_dir / "gen_1").mkdir()
            (run_dir / "gen_1" / "STOP_SIGNAL").write_text("stop", encoding="utf-8")
            (run_dir / "gen_1" / "generation_results.json").write_text("{}", encoding="utf-8")
            (run_dir / "logs").mkdir()
            (run_dir / "logs" / "launcher.nohup.log").write_text(
                "progress\nOPENAI_API_KEY=sk-123456789abcdef\n",
                encoding="utf-8",
            )
            (run_dir / "orchestrator_status.json").write_text(
                json.dumps(
                    {
                        "task_name": "Demo task",
                        "current_generation": 1,
                        "max_generations": 4,
                        "generations_completed": 2,
                        "cohort_size": 3,
                        "findings_total": 9,
                        "variants_total": 5,
                        "variants_above_baseline": 2,
                        "frontier_candidates": 2,
                        "gems_count": 1,
                        "strategy": "mixed",
                        "exit_condition": "in_progress",
                        "best_mature_result": {
                            "variant_name": "strong",
                            "metric_name": "score",
                            "metric_value": 0.0,
                            "evidence_stage": "complete",
                        },
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "run_summary.json").write_text(
                json.dumps({"status": "running", "runtime_usage": {"input_tokens": 10}}),
                encoding="utf-8",
            )
            (run_dir / "frontier").mkdir()
            (run_dir / "frontier" / "frontier_manifest.json").write_text(
                json.dumps(
                    {
                        "lane_frontiers": {
                            "confirmed": [
                                {
                                    "variant_name": "zero",
                                    "generation_id": 0,
                                    "metric_name": "loss",
                                    "metric_value": 0.0,
                                }
                            ]
                        },
                        "validation_candidates": {"cumulative": [{"variant_name": "lead"}]},
                    }
                ),
                encoding="utf-8",
            )
            (run_dir / "gems").mkdir()
            (run_dir / "gems" / "gems_state.json").write_text(
                json.dumps({"cycle_index": 2, "gems": [{"variant_name": "gem_a"}]}),
                encoding="utf-8",
            )
            (run_dir / "resource_scheduler").mkdir()
            (run_dir / "resource_scheduler" / "status.json").write_text(
                json.dumps({"queued": 2, "running": 1, "secret": "do-not-project"}),
                encoding="utf-8",
            )
            row = _row(str(run_dir))
            collector = DashboardStateCollector()
            with (
                patch("praxist.dashboard.state.status.collect_status_rows", return_value=[row]),
                patch(
                    "praxist.dashboard.state.monitor.collect_hardware_snapshot",
                    return_value=HardwareSnapshot(
                        loadavg="1.00 2.00 3.00",
                        memory="2 GiB / 8 GiB",
                        gpus=["GPU 0: util 20%"],
                        warnings=["accelerator probe partial"],
                    ),
                ),
            ):
                snapshot = collector.collect()

            overview = snapshot.overview
            self.assertEqual(overview["counts"]["active"], 1)
            self.assertEqual(overview["counts"]["findings"], 9)
            self.assertEqual(overview["counts"]["committed_generations"], 1)
            self.assertEqual(overview["host"]["gpus"], ["GPU 0: util 20%"])
            run = overview["runs"][0]
            self.assertEqual(run["phase"], "gen1:boundary-pending")
            self.assertEqual(run["progress"]["percent"], 25.0)
            self.assertTrue(run["progress"]["boundary"]["stop_signal"])
            self.assertTrue(run["progress"]["boundary"]["generation_results"])
            self.assertEqual(run["best_mature_result"]["metric_value"], 0.0)
            self.assertTrue(run["warnings"])

            detail = collector.collect_detail(row)
            self.assertEqual(detail["frontier"]["lanes"]["confirmed"]["count"], 1)
            entry = detail["frontier"]["lanes"]["confirmed"]["entries"][0]
            self.assertEqual(entry["generation_id"], 0)
            self.assertEqual(entry["metric_value"], 0.0)
            self.assertEqual(detail["frontier"]["validation_candidates"], 1)
            self.assertEqual(detail["gems"]["count"], 1)
            self.assertEqual(detail["resource_scheduler"]["queued"], 2)
            self.assertNotIn("secret", detail["resource_scheduler"])
            self.assertIn("<redacted:", "\n".join(detail["recent_logs"]))
            self.assertFalse(detail["resume_plan"]["available"])
            stopped_row = _row(str(run_dir), state="stopped", source="stale")
            with patch(
                "praxist.dashboard.state._inspect_resume_plan",
                return_value={"available": True, "start_generation": 1},
            ):
                stopped_detail = collector.collect_detail(stopped_row)
            self.assertTrue(stopped_detail["resume_plan"]["available"])

    def test_cache_reuses_samples_caches_details_and_can_force_refresh(self) -> None:
        from praxist.dashboard.state import (
            DashboardRunNotFound,
            DashboardSnapshot,
            DashboardStateCache,
        )

        row = _row("/missing")

        class Collector:
            def __init__(self) -> None:
                self.samples = 0
                self.details = 0

            def collect(self) -> DashboardSnapshot:
                self.samples += 1
                return DashboardSnapshot(
                    sampled_at_monotonic=10_000_000.0,
                    rows=(row,),
                    overview={"samples": self.samples},
                )

            def collect_detail(self, selected: object) -> dict[str, object]:
                self.details += 1
                return {"key": selected.run_id, "details": self.details}  # type: ignore[attr-defined]

        collector = Collector()
        cache = DashboardStateCache(collector=collector, sample_interval_seconds=1000)
        with patch("praxist.dashboard.state.time.monotonic", return_value=10_000_000.1):
            self.assertEqual(cache.overview()["samples"], 1)
            self.assertEqual(cache.overview()["samples"], 1)
            self.assertEqual(cache.detail("run:run_demo")["details"], 1)
            self.assertEqual(cache.detail("run:run_demo")["details"], 1)
            self.assertEqual(cache.overview(force=True)["samples"], 2)
        self.assertEqual(collector.samples, 2)
        self.assertEqual(collector.details, 1)
        with self.assertRaises(DashboardRunNotFound):
            cache.detail("run:missing")

    def test_cache_rejects_subsecond_or_nonfinite_sampling(self) -> None:
        from praxist.dashboard.state import DashboardStateCache

        for value in (0.9, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                DashboardStateCache(sample_interval_seconds=value)

    def test_state_helpers_cover_offline_legacy_and_malformed_artifacts(self) -> None:
        from praxist.dashboard import state

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            malformed = root / "malformed.json"
            malformed.write_text("{bad", encoding="utf-8")
            oversized = root / "oversized.json"
            oversized.write_text("x" * (state.MAX_JSON_BYTES + 1), encoding="utf-8")
            array = root / "array.json"
            array.write_text("[]", encoding="utf-8")
            self.assertEqual(state._bounded_json_object(malformed), {})
            self.assertEqual(state._bounded_json_object(oversized), {})
            self.assertEqual(state._bounded_json_object(array), {})
            self.assertEqual(state._bounded_json_object(root / "missing.json"), {})

            cumulative = state._compact_frontier(
                {"cumulative_top": [{"finding_id": "f1"}], "updated_at": "now"}
            )
            self.assertEqual(cumulative["lanes"]["frontier"]["count"], 1)
            self.assertEqual(state._compact_frontier_entry("bad"), {})
            self.assertEqual(state._compact_frontier({})["lanes"], {})
            self.assertEqual(state._compact_gems({})["count"], 0)
            self.assertEqual(state._compact_scheduler({"queued": 0, "private": 1}), {"queued": 0})
            self.assertEqual(state._generation_boundary_state(None, None)["committed"], False)
            self.assertEqual(state._safe_int(True, fallback=9), 9)
            self.assertEqual(state._safe_int("bad", fallback=8), 8)
            self.assertEqual(state._safe_int(2.9, fallback=0), 2)
            self.assertEqual(state._coalesce(0, 4), 0)
            self.assertEqual(state._coalesce(None, 4), 4)
            self.assertIsNone(state._usable_run_dir(None))
            self.assertIsNone(state._usable_run_dir(str(root / "missing")))
            with patch("pathlib.Path.is_dir", side_effect=OSError("unavailable")):
                self.assertIsNone(state._usable_run_dir(str(root)))

            collector = state.DashboardStateCollector()
            missing_row = _row(
                str(root / "missing"),
                state="stopped",
                source="stale",
                extras={"probe_error": "probe failed"},
            )
            summary = collector._run_summary(missing_row)
            self.assertEqual(summary["phase"], "missing-run-directory")
            self.assertTrue(summary["capabilities"]["can_resume"])
            detail = collector.collect_detail(missing_row)
            self.assertEqual(detail["recent_logs"], [])
            pid_row = _row(str(root / "missing"), run_id=None, pid=99)
            self.assertEqual(state._row_key(pid_row), "pid:99")
            inconsistent = _row(
                str(root / "missing"),
                state="status_inconsistent",
                extras={},
            )
            self.assertIn("registry state", collector._run_summary(inconsistent)["warnings"][0])
            self.assertEqual(
                state._compact_result(
                    {"variant_name": "lead", "validation_reason": "preview"},
                    include_reason=True,
                )["validation_reason"],
                "preview",
            )

    def test_resume_plan_inspection_success_missing_inputs_and_failure(self) -> None:
        from praxist.dashboard import state

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task = root / "task"
            run = root / "run"
            task.mkdir()
            run.mkdir()
            (task / "task.yaml").write_text("task_id: demo\n", encoding="utf-8")
            (run / "startup_config.json").write_text(
                json.dumps({"canonical_args": {"task_path": str(task)}}),
                encoding="utf-8",
            )

            fake_spec = SimpleNamespace(
                generation_policy=SimpleNamespace(max_generations=5),
                multi_pi=SimpleNamespace(enabled=True),
            )
            fake_plan = SimpleNamespace(
                to_dict=lambda: {
                    "start_generation": 3,
                    "completed_generations": [0, 1, 2],
                    "pending_boundary_generation": None,
                    "warnings": [],
                    "ignored": "not projected",
                }
            )
            with (
                patch("praxist.task_spec.load_task_spec", return_value=fake_spec),
                patch(
                    "praxist.plugins.workflow_stages.research_loop.backend.resume_state.inspect_resume_plan",
                    return_value=fake_plan,
                ) as inspect,
            ):
                result = state._inspect_resume_plan(run)
            self.assertTrue(result["available"])
            self.assertEqual(result["start_generation"], 3)
            self.assertNotIn("ignored", result)
            inspect.assert_called_once()

            (run / "startup_config.json").write_text("{}", encoding="utf-8")
            self.assertIn("task path", state._inspect_resume_plan(run)["warning"])
            (run / "startup_config.json").write_text(
                json.dumps({"canonical_args": {"task_path": str(root / "gone")}}),
                encoding="utf-8",
            )
            self.assertIn("task.yaml", state._inspect_resume_plan(run)["warning"])
            (run / "startup_config.json").write_text(
                json.dumps({"canonical_args": {"task_path": str(task)}}),
                encoding="utf-8",
            )
            with patch(
                "praxist.task_spec.load_task_spec", side_effect=RuntimeError("api_key=abcdefghi")
            ):
                failed = state._inspect_resume_plan(run)
            self.assertFalse(failed["available"])
            self.assertIn("<redacted:", failed["warning"])


class DashboardActionBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.task = Path(self.temp.name) / "task"
        self.task.mkdir()
        (self.task / "task.yaml").write_text("task_id: demo\n", encoding="utf-8")

    def test_start_plan_runs_doctor_resolve_and_daemonized_start(self) -> None:
        from praxist.dashboard.actions import ActionCommandBuilder

        builder = ActionCommandBuilder(executable="python-test")
        plan = builder.build(
            "start",
            {
                "task_path": str(self.task),
                "agent_system": "codex_sdk",
                "runtime": "agent_runtime:codex_sdk",
                "model_provider": "model_provider:openai_compatible",
                "model": "gpt-demo",
                "codex_native": True,
                "strategy": "explore",
                "cohort": 4,
                "generations": 3,
                "startup_timeout": 12,
            },
        )
        self.assertEqual([step.name for step in plan.steps], ["doctor", "resolve", "start"])
        self.assertIn("--daemonize", plan.steps[-1].argv)
        self.assertIn("--codex-native", plan.steps[0].argv)
        self.assertIn("agent_runtime:codex_sdk", plan.steps[1].argv)
        self.assertEqual(plan.lock_key, f"task:{self.task.resolve()}")

    def test_read_only_plans_and_lifecycle_confirmations(self) -> None:
        from praxist.dashboard.actions import (
            FORCE_RESUME_CONFIRMATION,
            GC_CONFIRMATION,
            STOP_ALL_CONFIRMATION,
            ActionCommandBuilder,
            ActionRejected,
        )

        builder = ActionCommandBuilder(executable="python-test")
        self.assertEqual(builder.build("doctor", {"task_path": str(self.task)}).kind, "doctor")
        self.assertEqual(builder.build("resolve", {"task_path": str(self.task)}).kind, "resolve")
        preview = builder.build("stop", {"run_id": "run_demo", "dry_run": True})
        self.assertIn("--dry-run", preview.steps[0].argv)
        with self.assertRaises(ActionRejected):
            builder.build("stop", {"run_id": "run_demo"})
        stopped = builder.build(
            "stop",
            {"run_id": "run_demo", "confirm_run_id": "run_demo", "grace": 10},
        )
        self.assertEqual(stopped.lock_key, "run:run_demo")
        with self.assertRaises(ActionRejected):
            builder.build("gc", {})
        self.assertTrue(
            builder.build("gc", {"confirmation": GC_CONFIRMATION, "dry_run": False}).exclusive
        )
        with self.assertRaises(ActionRejected):
            builder.build("stop-all", {"confirmation": "wrong"})
        stop_all = builder.build(
            "stop-all",
            {"confirmation": STOP_ALL_CONFIRMATION, "scope": "registry", "grace": 3},
        )
        self.assertTrue(stop_all.exclusive)
        self.assertIn("--registry-only", stop_all.steps[0].argv)
        with self.assertRaises(ActionRejected):
            builder.build(
                "resume",
                {
                    "target": "run_demo",
                    "confirm_target": "run_demo",
                    "force": True,
                    "confirm_force": "wrong",
                },
            )
        resumed = builder.build(
            "resume",
            {
                "target": "run_demo",
                "confirm_target": "run_demo",
                "force": True,
                "confirm_force": FORCE_RESUME_CONFIRMATION,
            },
        )
        self.assertIn("--force", resumed.steps[0].argv)

    def test_builder_rejects_unknown_bad_paths_types_refs_and_ranges(self) -> None:
        from praxist.dashboard.actions import ActionCommandBuilder, ActionRejected

        builder = ActionCommandBuilder(executable="python-test")
        cases = [
            ("unknown", {}),
            ("start", {"task_path": ""}),
            ("start", {"task_path": str(self.task / "missing")}),
            ("start", {"task_path": str(self.task), "runtime": "not-a-plugin"}),
            ("start", {"task_path": str(self.task), "model_provider": "bad"}),
            ("start", {"task_path": str(self.task), "cohort": 0}),
            ("start", {"task_path": str(self.task), "startup_timeout": float("nan")}),
            ("start", {"task_path": str(self.task), "server": "yes"}),
            ("stop", {"run_id": "../bad", "confirm_run_id": "../bad"}),
        ]
        for kind, payload in cases:
            with self.subTest(kind=kind, payload=payload), self.assertRaises(ActionRejected):
                builder.build(kind, payload)
        more_cases = [
            ("resolve", []),
            ("resume", {"target": "run_demo", "confirm_target": "wrong"}),
            ("start", {"task_path": str(self.task), "strategy": "chaos"}),
            ("start", {"task_path": str(self.task), "model": 123}),
            ("start", {"task_path": str(self.task), "model": "x" * 600}),
            ("start", {"task_path": str(self.task), "cohort": True}),
            ("start", {"task_path": str(self.task), "cohort": "not-an-int"}),
            ("start", {"task_path": str(self.task), "startup_timeout": True}),
            ("start", {"task_path": str(self.task), "startup_timeout": "bad"}),
        ]
        for kind, payload in more_cases:
            with self.subTest(kind=kind, payload=payload), self.assertRaises(ActionRejected):
                builder.build(kind, payload)  # type: ignore[arg-type]
        missing_yaml = Path(self.temp.name) / "empty-task"
        missing_yaml.mkdir()
        with self.assertRaises(ActionRejected):
            builder.build("start", {"task_path": str(missing_yaml)})

    def test_optional_builder_fields_map_to_closed_cli_flags(self) -> None:
        from praxist.dashboard.actions import (
            GC_CONFIRMATION,
            STOP_ALL_CONFIRMATION,
            ActionCommandBuilder,
        )

        builder = ActionCommandBuilder(executable="python-test")
        config = Path(self.temp.name) / "config.env"
        result_summary = Path(self.temp.name) / "summary.json"
        resolved = builder.build(
            "resolve",
            {
                "task_path": str(self.task),
                "config_file": str(config),
                "result_summary": str(result_summary),
            },
        )
        self.assertIn(str(config.resolve()), resolved.steps[0].argv)
        self.assertIn(str(result_summary), resolved.steps[0].argv)
        started = builder.build(
            "start",
            {
                "task_path": str(self.task),
                "run_dir": str(Path(self.temp.name) / "run"),
                "server": True,
                "startup_timeout": 0,
            },
        )
        self.assertIn("--run-dir", started.steps[-1].argv)
        self.assertIn("--server", started.steps[-1].argv)
        blank_model = builder.build("start", {"task_path": str(self.task), "model": "   "})
        self.assertNotIn("--model", blank_model.steps[-1].argv)
        resumed = builder.build(
            "resume",
            {
                "target": "run_demo",
                "confirm_target": "run_demo",
                "task_path": str(self.task),
                "agent_system": "claude_sdk",
                "runtime": "agent_runtime:claude_sdk",
                "model_provider": "model_provider:anthropic_messages",
                "model": "model-a",
                "strategy": "mixed",
                "cohort": 2,
                "generations": 4,
                "server": True,
            },
        )
        resume_argv = resumed.steps[0].argv
        for flag in ("--task-path", "--strategy", "--cohort", "--generations", "--server"):
            self.assertIn(flag, resume_argv)
        self.assertIn("--dry-run", builder.build("gc", {"dry_run": True}).steps[0].argv)
        self.assertIn(
            "--ps-scan-only",
            builder.build(
                "stop-all",
                {"confirmation": STOP_ALL_CONFIRMATION, "scope": "ps-scan"},
            )
            .steps[0]
            .argv,
        )
        self.assertFalse(
            builder.build("gc", {"confirmation": GC_CONFIRMATION, "dry_run": False})
            .steps[0]
            .argv[-1]
            == "--dry-run"
        )


class DashboardActionManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.task = Path(self.temp.name) / "task"
        self.task.mkdir()
        (self.task / "task.yaml").write_text("task_id: demo\n", encoding="utf-8")

    def test_manager_runs_plan_to_success_and_retains_structured_result(self) -> None:
        from praxist.dashboard.actions import ActionCommandBuilder, ActionManager

        class Runner:
            def run(self, step: object) -> dict[str, object]:
                return {"name": step.name, "status": "succeeded", "output": {"step": step.name}}  # type: ignore[attr-defined]

        manager = ActionManager(
            builder=ActionCommandBuilder(executable="python-test"),
            runner=Runner(),  # type: ignore[arg-type]
        )
        submitted = manager.submit("resolve", {"task_path": str(self.task)})
        finished = manager.wait(submitted["action_id"], timeout=2)
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(finished["result"], {"step": "resolve"})
        self.assertEqual(manager.list()[0]["action_id"], submitted["action_id"])
        self.assertIsNone(manager.get("missing"))

    def test_manager_stops_plan_on_failure_and_reports_adapter_exception(self) -> None:
        from praxist.dashboard.actions import ActionCommandBuilder, ActionManager

        class FailedRunner:
            def run(self, step: object) -> dict[str, object]:
                return {"name": "doctor", "status": "failed", "stderr": "preflight failed"}

        manager = ActionManager(
            builder=ActionCommandBuilder(executable="python-test"),
            runner=FailedRunner(),  # type: ignore[arg-type]
        )
        submitted = manager.submit("start", {"task_path": str(self.task)})
        finished = manager.wait(submitted["action_id"], timeout=2)
        self.assertEqual(finished["status"], "failed")
        self.assertEqual(finished["error"], "preflight failed")
        self.assertEqual(len(finished["steps"]), 1)

        class RaisingRunner:
            def run(self, step: object) -> dict[str, object]:
                raise RuntimeError("adapter exploded")

        raising = ActionManager(
            builder=ActionCommandBuilder(executable="python-test"),
            runner=RaisingRunner(),  # type: ignore[arg-type]
        )
        submitted = raising.submit("resolve", {"task_path": str(self.task)})
        finished = raising.wait(submitted["action_id"], timeout=2)
        self.assertEqual(finished["status"], "failed")
        self.assertIn("adapter exploded", finished["error"])

    def test_manager_rejects_same_target_exclusive_and_capacity_conflicts(self) -> None:
        from praxist.dashboard.actions import (
            GC_CONFIRMATION,
            ActionCommandBuilder,
            ActionManager,
            ActionRejected,
        )

        started = threading.Event()
        release = threading.Event()

        class BlockingRunner:
            def run(self, step: object) -> dict[str, object]:
                started.set()
                release.wait(2)
                return {"name": "stop", "status": "succeeded", "output": {}}

        manager = ActionManager(
            builder=ActionCommandBuilder(executable="python-test"),
            runner=BlockingRunner(),  # type: ignore[arg-type]
            max_active=1,
        )
        first = manager.submit(
            "stop",
            {"run_id": "run_demo", "confirm_run_id": "run_demo", "grace": 0},
        )
        self.assertTrue(started.wait(1))
        with self.assertRaises(ActionRejected) as capacity:
            manager.submit(
                "stop",
                {"run_id": "run_demo", "confirm_run_id": "run_demo", "grace": 0},
            )
        self.assertEqual(capacity.exception.status_code, 429)
        with self.assertRaises(ActionRejected):
            manager.submit("gc", {"confirmation": GC_CONFIRMATION, "dry_run": False})
        release.set()
        self.assertEqual(manager.wait(first["action_id"], 2)["status"], "succeeded")

    def test_constructor_rejects_nonpositive_limits(self) -> None:
        from praxist.dashboard.actions import ActionManager

        with self.assertRaises(ValueError):
            ActionManager(max_active=0)
        with self.assertRaises(ValueError):
            ActionManager(max_history=0)

    def test_manager_reports_same_target_and_exclusive_conflicts(self) -> None:
        from praxist.dashboard.actions import (
            GC_CONFIRMATION,
            ActionCommandBuilder,
            ActionManager,
            ActionRejected,
        )

        started = threading.Event()
        release = threading.Event()

        class BlockingRunner:
            def run(self, step: object) -> dict[str, object]:
                started.set()
                release.wait(2)
                return {"name": "stop", "status": "succeeded", "output": {}}

        manager = ActionManager(
            builder=ActionCommandBuilder(executable="python-test"),
            runner=BlockingRunner(),  # type: ignore[arg-type]
            max_active=3,
        )
        first = manager.submit(
            "stop",
            {"run_id": "run_demo", "confirm_run_id": "run_demo", "grace": 0},
        )
        self.assertTrue(started.wait(1))
        with self.assertRaises(ActionRejected) as same:
            manager.submit(
                "stop",
                {"run_id": "run_demo", "confirm_run_id": "run_demo", "grace": 0},
            )
        self.assertEqual(same.exception.status_code, 409)
        with self.assertRaises(ActionRejected) as exclusive:
            manager.submit("gc", {"confirmation": GC_CONFIRMATION, "dry_run": False})
        self.assertEqual(exclusive.exception.status_code, 409)
        release.set()
        manager.wait(first["action_id"], 2)

        exclusive_started = threading.Event()
        exclusive_release = threading.Event()

        class ExclusiveRunner:
            def run(self, step: object) -> dict[str, object]:
                exclusive_started.set()
                exclusive_release.wait(2)
                return {"name": "gc", "status": "succeeded", "output": {}}

        exclusive_manager = ActionManager(
            builder=ActionCommandBuilder(executable="python-test"),
            runner=ExclusiveRunner(),  # type: ignore[arg-type]
        )
        action = exclusive_manager.submit("gc", {"confirmation": GC_CONFIRMATION, "dry_run": False})
        self.assertTrue(exclusive_started.wait(1))
        with self.assertRaises(ActionRejected):
            exclusive_manager.submit(
                "stop",
                {"run_id": "another", "confirm_run_id": "another", "grace": 0},
            )
        exclusive_release.set()
        exclusive_manager.wait(action["action_id"], 2)

    def test_manager_trims_completed_history_and_waits_for_unknown_action(self) -> None:
        from praxist.dashboard.actions import ActionCommandBuilder, ActionManager

        class Runner:
            def run(self, step: object) -> dict[str, object]:
                return {"name": step.name, "status": "succeeded", "output": {}}  # type: ignore[attr-defined]

        manager = ActionManager(
            builder=ActionCommandBuilder(executable="python-test"),
            runner=Runner(),  # type: ignore[arg-type]
            max_history=1,
        )
        first = manager.submit("resolve", {"task_path": str(self.task)})
        manager.wait(first["action_id"], 2)
        second = manager.submit("doctor", {"task_path": str(self.task)})
        manager.wait(second["action_id"], 2)
        self.assertEqual(len(manager.list()), 1)
        self.assertIsNone(manager.get(first["action_id"]))
        self.assertIsNone(manager.wait("unknown", 0))

        active_first = ActionManager(max_history=1)
        active_first._order.extend(("active", "newer"))
        active_first._active_plans["active"] = object()  # type: ignore[assignment]
        active_first._trim_history()
        self.assertEqual(active_first._order, ["active", "newer"])


class CommandRunnerTest(unittest.TestCase):
    def test_runner_parses_and_redacts_json_and_handles_timeout_and_oserror(self) -> None:
        from praxist.dashboard.actions import CommandRunner, CommandStep

        runner = CommandRunner()
        step = CommandStep("status", ("praxist", "status", "--json"), 1.0)
        completed = subprocess.CompletedProcess(
            step.argv,
            0,
            stdout=json.dumps({"api_key": "sk-123456789abcdef", "ok": True}),
            stderr="",
        )
        with patch("praxist.dashboard.actions.subprocess.run", return_value=completed):
            result = runner.run(step)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["output"]["api_key"], "<redacted:named_secret>")

        timeout = subprocess.TimeoutExpired(step.argv, 1.0, output="partial", stderr="late")
        with patch("praxist.dashboard.actions.subprocess.run", side_effect=timeout):
            result = runner.run(step)
        self.assertEqual(result["status"], "timed_out")

        with patch(
            "praxist.dashboard.actions.subprocess.run", side_effect=OSError("no executable")
        ):
            result = runner.run(step)
        self.assertEqual(result["status"], "failed")
        self.assertIn("no executable", result["error"])

    def test_runner_preserves_redacted_non_json_and_nonzero_status(self) -> None:
        from praxist.dashboard.actions import CommandRunner, CommandStep, _bounded_capture

        step = CommandStep("resolve", ("praxist", "resolve", "/task with space"), 2.0)
        completed = subprocess.CompletedProcess(
            step.argv,
            2,
            stdout="not json api_key=abcdefghi",
            stderr="validation failed",
        )
        with patch("praxist.dashboard.actions.subprocess.run", return_value=completed):
            result = CommandRunner().run(step)
        self.assertEqual(result["status"], "failed")
        self.assertIn("<redacted:", result["stdout"])
        self.assertIn("'/task with space'", result["command"])
        self.assertEqual(_bounded_capture(None), "")
        self.assertEqual(_bounded_capture(b"bytes"), "bytes")


class _FakeState:
    def overview(self) -> dict[str, object]:
        return {"schema_version": 1, "runs": [], "counts": {}, "host": {}, "warnings": []}

    def detail(self, key: str) -> dict[str, object]:
        if key == "run:missing":
            from praxist.dashboard.state import DashboardRunNotFound

            raise DashboardRunNotFound(key)
        return {"run": {"key": key}}


class _FakeActions:
    def __init__(self) -> None:
        self.submitted: list[tuple[str, dict[str, object]]] = []

    def list(self) -> list[dict[str, object]]:
        return [{"action_id": "a1", "status": "succeeded"}]

    def get(self, action_id: str) -> dict[str, object] | None:
        return {"action_id": action_id} if action_id == "a1" else None

    def submit(self, kind: str, payload: dict[str, object]) -> dict[str, object]:
        self.submitted.append((kind, payload))
        return {"action_id": "queued", "kind": kind, "status": "queued"}


class DashboardServerTest(unittest.TestCase):
    def setUp(self) -> None:
        from praxist.dashboard.server import DashboardApplication

        self.actions = _FakeActions()
        self.application = DashboardApplication(
            state=_FakeState(),  # type: ignore[arg-type]
            actions=self.actions,  # type: ignore[arg-type]
            control_token="control-token",
        )
        self.application.allowed_origins = {"http://127.0.0.1:8765"}

    def _handler(self, headers: dict[str, str] | None = None, body: bytes = b""):
        from praxist.dashboard.server import DashboardRequestHandler

        handler = object.__new__(DashboardRequestHandler)
        message = Message()
        for key, value in (headers or {}).items():
            message[key] = value
        handler.headers = message
        handler.rfile = io.BytesIO(body)
        handler.wfile = io.BytesIO()
        handler.server = SimpleNamespace(
            app=self.application,
            allowed_hosts={"127.0.0.1:8765"},
            verbose=False,
        )
        handler.responses_seen = []
        handler.headers_seen = []
        handler.send_response = lambda code: handler.responses_seen.append(code)
        handler.send_header = lambda key, value: handler.headers_seen.append((key, value))
        handler.end_headers = lambda: None
        return handler

    def test_application_routes_overview_detail_actions_and_health(self) -> None:
        status_code, overview = self.application.get_json("/api/v1/overview")
        self.assertEqual(status_code, 200)
        self.assertFalse(overview["control"]["read_only"])
        self.assertEqual(
            self.application.get_json("/api/v1/runs/run%3Arun_demo")[1]["run"]["key"],
            "run:run_demo",
        )
        self.assertEqual(self.application.get_json("/api/v1/actions")[1][0]["action_id"], "a1")
        self.assertEqual(self.application.get_json("/api/v1/actions/a1")[1]["action_id"], "a1")
        self.assertEqual(self.application.get_json("/healthz")[1]["status"], "ok")
        accepted, record = self.application.post_json(
            "/api/v1/actions/resolve", {"task_path": "/tmp"}
        )
        self.assertEqual(accepted, 202)
        self.assertEqual(record["kind"], "resolve")
        self.assertEqual(self.actions.submitted, [("resolve", {"task_path": "/tmp"})])

    def test_application_reports_missing_and_read_only_routes(self) -> None:
        from praxist.dashboard.actions import ActionRejected
        from praxist.dashboard.server import DashboardApplication, DashboardHTTPError

        for path in ("/missing", "/api/v1/actions/missing", "/api/v1/runs/run%3Amissing"):
            with self.subTest(path=path), self.assertRaises(DashboardHTTPError) as raised:
                self.application.get_json(path)
            self.assertEqual(raised.exception.status, 404)

        application = DashboardApplication(
            state=_FakeState(),  # type: ignore[arg-type]
            actions=self.actions,  # type: ignore[arg-type]
            read_only=True,
        )
        with self.assertRaises(DashboardHTTPError) as raised:
            application.post_json("/api/v1/actions/stop", {})
        self.assertEqual(raised.exception.status, 403)
        with self.assertRaises(DashboardHTTPError) as missing_post:
            self.application.post_json("/not-an-action", {})
        self.assertEqual(missing_post.exception.status, 404)

        class RejectingActions(_FakeActions):
            def submit(self, kind: str, payload: dict[str, object]) -> dict[str, object]:
                raise ActionRejected("conflict", status_code=409)

        rejected = DashboardApplication(
            state=_FakeState(),  # type: ignore[arg-type]
            actions=RejectingActions(),  # type: ignore[arg-type]
        )
        with self.assertRaises(DashboardHTTPError) as conflict:
            rejected.post_json("/api/v1/actions/stop", {})
        self.assertEqual(conflict.exception.status, 409)

    def test_handler_injects_index_token_serves_assets_and_security_headers(self) -> None:
        from praxist.dashboard.server import DashboardHTTPError

        handler = self._handler()
        handler._serve_index(head_only=False)
        body = handler.wfile.getvalue().decode()
        self.assertIn("Praxist Local Command", body)
        self.assertIn("control-token", body)
        self.assertNotIn("__PRAXIST_CONTROL_TOKEN__", body)
        self.assertIn(("X-Frame-Options", "DENY"), handler.headers_seen)

        asset = self._handler()
        asset._serve_asset("/assets/app.js", head_only=False)
        self.assertIn("application/json", asset.wfile.getvalue().decode())
        with self.assertRaises(DashboardHTTPError) as missing:
            asset._serve_asset("/assets/missing.js", head_only=False)
        self.assertEqual(missing.exception.status, 404)

        with patch("praxist.dashboard.server.ASSET_ROOT", Path("/definitely/missing")):
            with self.assertRaises(DashboardHTTPError) as missing_index:
                handler._serve_index(head_only=False)
            self.assertEqual(missing_index.exception.status, 500)
            with self.assertRaises(DashboardHTTPError) as missing_asset:
                asset._serve_asset("/assets/app.js", head_only=False)
            self.assertEqual(missing_asset.exception.status, 500)

    def test_handler_requires_host_origin_token_and_valid_bounded_json(self) -> None:
        from praxist.dashboard.server import DashboardHTTPError

        handler = self._handler(
            {
                "Host": "127.0.0.1:8765",
                "Origin": "http://127.0.0.1:8765",
                "X-Praxist-Control": "control-token",
                "Content-Type": "application/json",
                "Content-Length": "12",
            },
            b'{"ok": true}',
        )
        handler._require_allowed_host()
        handler._require_same_origin()
        handler._require_control_token()
        self.assertEqual(handler._read_json_object(), {"ok": True})

        bad_host = self._handler({"Host": "evil.invalid"})
        with self.assertRaises(DashboardHTTPError) as raised:
            bad_host._require_allowed_host()
        self.assertEqual(raised.exception.status, 403)
        bad_origin = self._handler({"Origin": "https://evil.invalid"})
        with self.assertRaises(DashboardHTTPError):
            bad_origin._require_same_origin()
        bad_token = self._handler({"X-Praxist-Control": "wrong"})
        with self.assertRaises(DashboardHTTPError):
            bad_token._require_control_token()
        bad_media = self._handler({"Content-Type": "text/plain", "Content-Length": "2"}, b"{}")
        with self.assertRaises(DashboardHTTPError) as media:
            bad_media._read_json_object()
        self.assertEqual(media.exception.status, 415)
        too_large = self._handler(
            {"Content-Type": "application/json", "Content-Length": "999999"}, b"{}"
        )
        with self.assertRaises(DashboardHTTPError) as large:
            too_large._read_json_object()
        self.assertEqual(large.exception.status, 413)

        missing_length = self._handler({"Content-Type": "application/json"}, b"{}")
        with self.assertRaises(DashboardHTTPError) as length:
            missing_length._read_json_object()
        self.assertEqual(length.exception.status, 411)
        malformed = self._handler(
            {"Content-Type": "application/json", "Content-Length": "4"}, b"nope"
        )
        with self.assertRaises(DashboardHTTPError) as bad_json:
            malformed._read_json_object()
        self.assertEqual(bad_json.exception.status, 400)
        array = self._handler({"Content-Type": "application/json", "Content-Length": "2"}, b"[]")
        with self.assertRaises(DashboardHTTPError) as bad_shape:
            array._read_json_object()
        self.assertEqual(bad_shape.exception.status, 400)

    def test_handler_get_head_post_options_and_internal_error_dispatch(self) -> None:
        valid_headers = {
            "Host": "127.0.0.1:8765",
            "Origin": "http://127.0.0.1:8765",
            "X-Praxist-Control": "control-token",
        }
        overview = self._handler(valid_headers)
        overview.path = "/api/v1/overview"
        overview.do_GET()
        self.assertEqual(overview.responses_seen[-1], 200)
        self.assertEqual(json.loads(overview.wfile.getvalue())["schema_version"], 1)

        index = self._handler(valid_headers)
        index.path = "/"
        index.do_GET()
        self.assertIn("Praxist Local Command", index.wfile.getvalue().decode())
        asset = self._handler(valid_headers)
        asset.path = "/assets/styles.css"
        asset.do_GET()
        self.assertIn(":root", asset.wfile.getvalue().decode())

        head = self._handler(valid_headers)
        head.path = "/index.html"
        head.do_HEAD()
        self.assertEqual(head.responses_seen[-1], 200)
        self.assertEqual(head.wfile.getvalue(), b"")
        head_asset = self._handler(valid_headers)
        head_asset.path = "/assets/app.js"
        head_asset.do_HEAD()
        self.assertEqual(head_asset.responses_seen[-1], 200)
        self.assertEqual(head_asset.wfile.getvalue(), b"")
        bad_head = self._handler(valid_headers)
        bad_head.path = "/api/v1/overview"
        bad_head.do_HEAD()
        self.assertEqual(bad_head.responses_seen[-1], 405)

        body = b'{"task_path":"/tmp"}'
        post = self._handler(
            {
                **valid_headers,
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
            body,
        )
        post.path = "/api/v1/actions/resolve"
        post.do_POST()
        self.assertEqual(post.responses_seen[-1], 202)
        self.assertEqual(json.loads(post.wfile.getvalue())["kind"], "resolve")

        forbidden = self._handler({"Host": "127.0.0.1:8765"})
        forbidden.path = "/api/v1/actions/resolve"
        forbidden.do_POST()
        self.assertEqual(forbidden.responses_seen[-1], 403)

        options = self._handler(valid_headers)
        options.do_OPTIONS()
        self.assertEqual(options.responses_seen[-1], 405)

        class BrokenState:
            def overview(self) -> dict[str, object]:
                raise RuntimeError("broken")

        broken_app = self.application
        broken_app.state = BrokenState()  # type: ignore[assignment]
        internal = self._handler(valid_headers)
        internal.path = "/api/v1/overview"
        internal.do_GET()
        self.assertEqual(internal.responses_seen[-1], 500)
        broken_app.state = _FakeState()  # type: ignore[assignment]

        missing_route = self._handler(valid_headers)
        missing_route.path = "/missing"
        missing_route.do_GET()
        self.assertEqual(missing_route.responses_seen[-1], 404)

        class BrokenActions(_FakeActions):
            def submit(self, kind: str, payload: dict[str, object]) -> dict[str, object]:
                raise RuntimeError("broken action adapter")

        broken_app.actions = BrokenActions()  # type: ignore[assignment]
        broken_post = self._handler(
            {
                **valid_headers,
                "Content-Type": "application/json",
                "Content-Length": str(len(body)),
            },
            body,
        )
        broken_post.path = "/api/v1/actions/resolve"
        broken_post.do_POST()
        self.assertEqual(broken_post.responses_seen[-1], 500)
        broken_app.actions = self.actions  # type: ignore[assignment]

    def test_handler_verbose_logging_and_disconnected_client_are_nonfatal(self) -> None:
        from http.server import BaseHTTPRequestHandler

        handler = self._handler()
        handler.server.verbose = True
        with patch.object(BaseHTTPRequestHandler, "log_message") as parent_log:
            handler.log_message("request %s", "ok")
        parent_log.assert_called_once_with("request %s", "ok")

        class BrokenWriter:
            def write(self, _body: bytes) -> None:
                raise BrokenPipeError

        handler.wfile = BrokenWriter()
        handler._send_bytes(
            200,
            b"payload",
            content_type="text/plain",
            cache_control="no-store",
            head_only=False,
        )
        self.assertEqual(handler.responses_seen[-1], 200)

    def test_nonloopback_and_invalid_ports_are_rejected(self) -> None:
        from praxist.dashboard import server as dashboard_server
        from praxist.dashboard.server import (
            DashboardApplication,
            PraxistDashboardServer,
            create_dashboard_server,
        )

        with self.assertRaises(ValueError):
            create_dashboard_server(host="192.0.2.10", port=8765)
        with self.assertRaises(ValueError):
            create_dashboard_server(port=70_000)
        sentinel = object()
        with patch("praxist.dashboard.server.PraxistDashboardServer", return_value=sentinel):
            created = create_dashboard_server(
                host="127.0.0.1",
                port=8765,
                state=_FakeState(),  # type: ignore[arg-type]
                actions=self.actions,  # type: ignore[arg-type]
            )
        self.assertIs(created, sentinel)

        def fake_init(instance: object, address: tuple[str, int], handler: object) -> None:
            instance.server_address = address  # type: ignore[attr-defined]

        with patch("praxist.dashboard.server.ThreadingHTTPServer.__init__", new=fake_init):
            concrete = PraxistDashboardServer(
                ("127.0.0.1", 8765),
                DashboardApplication(
                    state=_FakeState(),  # type: ignore[arg-type]
                    actions=self.actions,  # type: ignore[arg-type]
                ),
                verbose=True,
            )
        self.assertEqual(concrete.base_url, "http://127.0.0.1:8765")
        self.assertIn("localhost:8765", concrete.allowed_hosts)

        with (
            patch(
                "praxist.dashboard.server.socket.getaddrinfo",
                side_effect=dashboard_server.socket.gaierror("resolution failed"),
            ),
            self.assertRaises(ValueError),
        ):
            create_dashboard_server(host="unresolvable.invalid", port=8765)


class DashboardCliTest(unittest.TestCase):
    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        from praxist.cli import main

        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                main(argv)
        except SystemExit as exc:
            return int(exc.code or 0), stdout.getvalue(), stderr.getvalue()
        return 0, stdout.getvalue(), stderr.getvalue()

    def test_dispatcher_forwards_dashboard_options(self) -> None:
        with patch("praxist.cli.dashboard.serve_dashboard", return_value=0) as serve:
            code, _out, _err = self._run(
                [
                    "dashboard",
                    "--port",
                    "0",
                    "--no-open",
                    "--read-only",
                    "--sample-interval",
                    "2",
                    "--json",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(serve.call_args.kwargs["port"], 0)
        self.assertFalse(serve.call_args.kwargs["open_browser"])
        self.assertTrue(serve.call_args.kwargs["read_only"])
        self.assertEqual(serve.call_args.kwargs["sample_interval_seconds"], 2.0)

    def test_cli_reports_bind_errors_and_argument_types(self) -> None:
        with patch(
            "praxist.cli.dashboard.serve_dashboard", side_effect=ValueError("loopback only")
        ):
            code, _out, err = self._run(["dashboard", "--no-open"])
        self.assertEqual(code, 1)
        self.assertIn("loopback only", err)
        for argv in (
            ["dashboard", "--port", "70000"],
            ["dashboard", "--sample-interval", "0.2"],
            ["dashboard", "--sample-interval", "nan"],
        ):
            with self.subTest(argv=argv):
                code, _out, _err = self._run(argv)
                self.assertEqual(code, 2)

    def test_serve_dashboard_prints_no_token_and_handles_browser_and_interrupt(self) -> None:
        from praxist.dashboard import server as dashboard_server

        class FakeServer:
            base_url = "http://127.0.0.1:9999"
            server_address = ("127.0.0.1", 9999)
            closed = False

            def serve_forever(self, poll_interval: float) -> None:
                self.poll_interval = poll_interval
                raise KeyboardInterrupt

            def server_close(self) -> None:
                self.closed = True

        fake = FakeServer()
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("praxist.dashboard.server.create_dashboard_server", return_value=fake),
            patch(
                "praxist.dashboard.server.webbrowser.open_new_tab",
                side_effect=RuntimeError("browser unavailable"),
            ) as opened,
        ):
            result = dashboard_server.serve_dashboard(
                port=0,
                open_browser=True,
                as_json=True,
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(result, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["url"], "http://127.0.0.1:9999/")
        self.assertNotIn("token", stdout.getvalue().lower())
        self.assertIn("research runs were not changed", stderr.getvalue())
        self.assertTrue(fake.closed)
        opened.assert_called_once()

    def test_serve_dashboard_read_only_without_browser_returns_normally(self) -> None:
        from praxist.dashboard import server as dashboard_server

        class FakeServer:
            base_url = "http://127.0.0.1:9998"
            server_address = ("127.0.0.1", 9998)
            closed = False

            def serve_forever(self, poll_interval: float) -> None:
                self.poll_interval = poll_interval

            def server_close(self) -> None:
                self.closed = True

        fake = FakeServer()
        stdout, stderr = io.StringIO(), io.StringIO()
        with (
            patch("praxist.dashboard.server.create_dashboard_server", return_value=fake),
            patch("praxist.dashboard.server.webbrowser.open_new_tab") as opened,
        ):
            result = dashboard_server.serve_dashboard(
                open_browser=False,
                read_only=True,
                stdout=stdout,
                stderr=stderr,
            )
        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue().strip(), "http://127.0.0.1:9998/")
        self.assertIn("Control actions are disabled", stderr.getvalue())
        self.assertTrue(fake.closed)
        opened.assert_not_called()

    def test_dashboard_argument_helpers_accept_valid_values_and_reject_text(self) -> None:
        from praxist.cli.dashboard import _port, _sample_interval

        self.assertEqual(_port("0"), 0)
        self.assertEqual(_port("8765"), 8765)
        self.assertEqual(_sample_interval("1.5"), 1.5)
        with self.assertRaises(argparse.ArgumentTypeError):
            _port("not-a-port")
        with self.assertRaises(argparse.ArgumentTypeError):
            _sample_interval("not-a-number")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
