"""Contracts for read-only Codex takeover/setup discovery in the dashboard."""

from __future__ import annotations

import io
import os
import queue
import subprocess
import tempfile
import threading
import time
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch


class _FakeClient:
    def __init__(
        self,
        threads: list[dict[str, object]],
        statuses: dict[str, str | BaseException] | None = None,
    ) -> None:
        self.threads = threads
        self.statuses = statuses or {}
        self.closed = False

    def list_threads(self, *, limit: int) -> list[dict[str, object]]:
        return self.threads[:limit]

    def latest_turn_status(self, thread_id: str) -> str | None:
        value = self.statuses.get(thread_id)
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self) -> None:
        self.closed = True


def _write_task(path: Path, name: str = "Demo Research") -> None:
    path.mkdir(parents=True)
    (path / "task.yaml").write_text(
        f"schema_version: 1\ntask_id: demo\ntask_name: {name!r}\n",
        encoding="utf-8",
    )


class CodexTaskProjectionTest(unittest.TestCase):
    def test_dashboard_state_includes_setup_counts_without_blocking_canonical_runs(self) -> None:
        from praxist.cli.monitor import HardwareSnapshot
        from praxist.dashboard.codex_tasks import CodexTaskCache, CodexTaskSource
        from praxist.dashboard.state import DashboardStateCollector

        class Source(CodexTaskSource):
            def collect(self, runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
                self.runs = runs
                return {
                    "source": {"available": True, "status": "ready"},
                    "tasks": [
                        {"id": "working", "stage": "working"},
                        {"id": "approval", "stage": "awaiting_approval"},
                    ],
                }

        source = Source()
        cache = CodexTaskCache(collector=source, refresh_seconds=30)
        cache.refresh_now([])
        hardware = HardwareSnapshot(loadavg="0 0 0", memory="1 / 2", gpus=[], warnings=[])
        with (
            patch("praxist.dashboard.state.status.collect_status_rows", return_value=[]),
            patch(
                "praxist.dashboard.state.monitor.collect_hardware_snapshot",
                return_value=hardware,
            ),
        ):
            overview = DashboardStateCollector(codex_tasks=cache).collect().overview
            disabled = DashboardStateCollector().collect().overview

        self.assertEqual(overview["counts"]["setup_total"], 2)
        self.assertEqual(overview["counts"]["setup_active"], 1)
        self.assertEqual(overview["counts"]["setup_attention"], 1)
        self.assertEqual(overview["setup"]["status"], "ready")
        self.assertEqual(disabled["setup"]["status"], "disabled")

    def test_discovers_bounded_external_tasks_and_skips_generated_and_source_paths(self) -> None:
        from praxist.dashboard.codex_tasks import discover_task_paths

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expected = root / "research" / "praxist_tasks" / "demo"
            ignored = root / "experiments" / "nested" / "ignored"
            hidden = root / ".hidden" / "ignored"
            _write_task(expected)
            _write_task(ignored)
            _write_task(hidden)
            self.assertEqual(discover_task_paths(root), [expected.resolve()])
            self.assertEqual(discover_task_paths(expected), [expected.resolve()])

        source_root = Path(__file__).resolve().parents[2]
        self.assertEqual(discover_task_paths(source_root), [])
        self.assertEqual(discover_task_paths(Path("/definitely/missing")), [])

    def test_projects_latest_setup_thread_correlates_run_and_redacts_metadata(self) -> None:
        from praxist.dashboard.codex_tasks import CodexTaskCollector

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path = root / "research" / "praxist_tasks" / "demo"
            _write_task(task_path)
            unrelated = root / "other"
            unrelated.mkdir()
            threads: list[dict[str, object]] = [
                {
                    "id": "thread-old",
                    "name": "older takeover",
                    "preview": "praxist takeover approval",
                    "cwd": str(root),
                    "updatedAt": 100,
                },
                {
                    "id": "thread-new",
                    "name": "forge OPENAI_API_KEY=sk-123456789abcdef",
                    "preview": "Praxist task.yaml launch",
                    "cwd": str(root),
                    "updatedAt": 200,
                },
                {
                    "id": "unrelated",
                    "name": "ordinary coding",
                    "preview": "fix a formatter",
                    "cwd": str(unrelated),
                    "updatedAt": 300,
                },
            ]
            client = _FakeClient(
                threads,
                {"thread-old": "completed", "thread-new": "inProgress"},
            )
            run = {
                "key": "run:demo",
                "run_id": "demo-run",
                "task_path": str(task_path),
                "state": "running",
            }
            with (
                patch(
                    "praxist.dashboard.codex_tasks.resolve_codex_operator_binary",
                    return_value="/mock/codex",
                ),
                patch(
                    "praxist.dashboard.codex_tasks.codex_binary_version",
                    return_value="codex-cli test",
                ),
            ):
                payload = CodexTaskCollector(
                    client_factory=lambda _binary: client  # type: ignore[arg-type]
                ).collect([run])

        self.assertTrue(payload["source"]["available"])
        self.assertEqual(payload["source"]["binary_version"], "codex-cli test")
        self.assertTrue(client.closed)
        self.assertEqual(len(payload["tasks"]), 1)
        task = payload["tasks"][0]
        self.assertEqual(task["thread_id"], "thread-new")
        self.assertEqual(task["task_name"], "Demo Research")
        self.assertEqual(task["stage"], "running")
        self.assertEqual(task["linked_run"]["key"], "run:demo")
        self.assertEqual(task["resume_command"], "/mock/codex resume thread-new")
        self.assertIn("<redacted:", task["title"])
        self.assertNotIn("preview", task)

    def test_projects_takeover_without_manifest_and_turn_status_failures(self) -> None:
        from praxist.dashboard.codex_tasks import CodexTaskCollector

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            threads: list[dict[str, object]] = [
                {
                    "id": "approval",
                    "name": "Praxist takeover",
                    "preview": "A0 approval required",
                    "cwd": str(root),
                    "updatedAt": "2026-08-29T01:02:03Z",
                },
                {
                    "id": "weak",
                    "name": "Praxist paper review",
                    "preview": "read a paper",
                    "cwd": str(root),
                    "updatedAt": "not-a-date",
                },
            ]
            client = _FakeClient(threads, {"approval": OSError("lineage mismatch")})
            with (
                patch(
                    "praxist.dashboard.codex_tasks.resolve_codex_operator_binary",
                    return_value="/mock/codex",
                ),
                patch("praxist.dashboard.codex_tasks.codex_binary_version", return_value="unknown"),
            ):
                payload = CodexTaskCollector(
                    client_factory=lambda _binary: client  # type: ignore[arg-type]
                ).collect([])

        self.assertEqual(len(payload["tasks"]), 1)
        self.assertEqual(payload["tasks"][0]["stage"], "awaiting_approval")
        self.assertIsNone(payload["tasks"][0]["task_path"])
        self.assertEqual(payload["source"]["status"], "degraded")
        self.assertTrue(payload["source"]["degraded"])
        self.assertIn("lineage mismatch", payload["source"]["warnings"][0])

    def test_manifest_only_candidates_and_failure_stages(self) -> None:
        from praxist.dashboard.codex_tasks import _project_setup_tasks

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_path = root / "research" / "task"
            _write_task(task_path)
            client = _FakeClient(
                [
                    {
                        "id": "failed",
                        "name": "Praxist task init",
                        "cwd": str(root),
                        "updatedAt": 20,
                    }
                ],
                {"failed": "failed"},
            )
            warnings: list[str] = []
            failed = _project_setup_tasks(client.threads, [], client, warnings)
            self.assertEqual(failed[0]["stage"], "failed")

            manifest_only_client = _FakeClient(
                [
                    {
                        "id": "ordinary",
                        "name": "ordinary coding",
                        "cwd": str(root),
                        "updatedAt": 30,
                    }
                ]
            )
            manifest_only = _project_setup_tasks(
                manifest_only_client.threads, [], manifest_only_client, warnings
            )
            self.assertEqual(manifest_only[0]["stage"], "ready")
            self.assertIsNone(manifest_only[0]["thread_id"])

    def test_collector_degrades_when_binary_or_client_is_unavailable(self) -> None:
        from praxist.dashboard.codex_tasks import CodexTaskCollector

        with patch(
            "praxist.dashboard.codex_tasks.resolve_codex_operator_binary",
            side_effect=OSError("OPENAI_API_KEY=sk-123456789abcdef missing"),
        ):
            payload = CodexTaskCollector().collect([])
        self.assertFalse(payload["source"]["available"])
        self.assertEqual(payload["source"]["status"], "unavailable")
        self.assertIn("<redacted:", payload["source"]["warnings"][0])

        class ClosingClient(_FakeClient):
            def close(self) -> None:
                raise OSError("close failed")

        client = ClosingClient([])
        with (
            patch(
                "praxist.dashboard.codex_tasks.resolve_codex_operator_binary",
                return_value="/mock/codex",
            ),
            patch(
                "praxist.dashboard.codex_tasks.codex_binary_version",
                return_value="codex-cli test",
            ),
        ):
            payload = CodexTaskCollector(
                client_factory=lambda _binary: client  # type: ignore[arg-type]
            ).collect([])
        self.assertEqual(payload["source"]["status"], "degraded")
        self.assertIn("close failed", payload["source"]["warnings"])

    def test_helper_classification_fallbacks_and_task_metadata_are_bounded(self) -> None:
        from praxist.dashboard.codex_tasks import (
            _iso_from_epoch,
            _manifest_candidate,
            _normalized_path,
            _project_setup_tasks,
            _safe_directory,
            _stage_rank,
            _task_name,
            _task_stage,
            _timestamp,
        )

        self.assertEqual(_task_stage("inProgress", "", False, None), "working")
        self.assertEqual(_task_stage("interrupted", "", True, None), "interrupted")
        self.assertEqual(_task_stage("completed", "", False, None), "initializing")
        self.assertEqual(_stage_rank("unexpected"), 0)
        self.assertEqual(_normalized_path(None), "")
        self.assertIsNone(_safe_directory(None))
        self.assertEqual(_timestamp("not-a-date"), ("not-a-date", 0.0))
        self.assertEqual(_timestamp(False), (None, 0.0))
        self.assertIsNone(_iso_from_epoch(0))
        self.assertIsNone(_iso_from_epoch(float("inf")))
        self.assertIsNone(_task_name(None))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(Path, "resolve", side_effect=OSError("bad path")):
                self.assertEqual(_normalized_path(root), str(root))

            missing = root / "missing-task"
            manifest = _manifest_candidate(root, missing, [])
            self.assertIsNone(manifest["updated_at"])
            self.assertEqual(manifest["task_name"], "missing-task")

            malformed = root / "malformed"
            malformed.mkdir()
            (malformed / "task.yaml").write_text("[not: yaml", encoding="utf-8")
            self.assertEqual(_task_name(malformed), "malformed")

            sequence = root / "sequence"
            sequence.mkdir()
            (sequence / "task.yaml").write_text("- one\n- two\n", encoding="utf-8")
            self.assertEqual(_task_name(sequence), "sequence")

            large = root / "large"
            large.mkdir()
            (large / "task.yaml").write_text("x" * (512 * 1024 + 1), encoding="utf-8")
            self.assertEqual(_task_name(large), "large")

            blank_thread = {
                "id": "",
                "name": "Praxist takeover approval",
                "cwd": str(root),
                "updatedAt": 1,
            }
            tasks = _project_setup_tasks([blank_thread], [], _FakeClient([]), [])
            self.assertEqual(tasks[0]["stage"], "awaiting_approval")
            self.assertFalse(tasks[0]["capabilities"]["can_copy_resume"])

            invalid_cwd = {
                "id": "missing-cwd",
                "name": "Praxist takeover",
                "cwd": str(root / "does-not-exist"),
            }
            self.assertEqual(
                _project_setup_tasks([invalid_cwd], [], _FakeClient([]), []),
                [],
            )

    def test_projection_caps_many_setup_threads(self) -> None:
        from praxist.dashboard.codex_tasks import MAX_SETUP_TASKS, _project_setup_tasks

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            threads: list[dict[str, object]] = []
            statuses: dict[str, str | BaseException] = {}
            for index in range(MAX_SETUP_TASKS + 2):
                cwd = root / f"project-{index}"
                cwd.mkdir()
                thread_id = f"thread-{index}"
                threads.append(
                    {
                        "id": thread_id,
                        "name": "Praxist takeover",
                        "cwd": str(cwd),
                        "updatedAt": index + 1,
                    }
                )
                statuses[thread_id] = "completed"
            tasks = _project_setup_tasks(threads, [], _FakeClient(threads, statuses), [])
        self.assertEqual(len(tasks), MAX_SETUP_TASKS)

    def test_discovery_recovers_from_unreadable_directory(self) -> None:
        from praxist.dashboard import codex_tasks

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(Path, "iterdir", side_effect=OSError("denied")):
                self.assertEqual(codex_tasks.discover_task_paths(root), [])
            with patch.object(Path, "resolve", side_effect=OSError("bad path")):
                self.assertIsNone(codex_tasks._safe_directory(root))

            nested = root
            for index in range(codex_tasks.MAX_SCAN_DEPTH + 2):
                nested = nested / f"level-{index}"
                nested.mkdir()
            self.assertEqual(codex_tasks.discover_task_paths(root), [])

    def test_cache_refreshes_off_request_path_and_recovers_injected_failure(self) -> None:
        from praxist.dashboard.codex_tasks import CodexTaskCache, CodexTaskSource

        entered = threading.Event()
        release = threading.Event()

        class Collector(CodexTaskSource):
            def collect(self, runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
                del runs
                entered.set()
                release.wait(2)
                return {"source": {"status": "ready"}, "tasks": [{"id": "one"}]}

        cache = CodexTaskCache(collector=Collector(), refresh_seconds=30)
        first = cache.overview([])
        self.assertEqual(first["source"]["status"], "sampling")
        self.assertTrue(entered.wait(1))
        second = cache.overview([])
        self.assertEqual(second["source"]["status"], "sampling")
        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            ready = cache.overview([])
            if ready["source"]["status"] == "ready":
                break
            time.sleep(0.01)
        self.assertEqual(ready["tasks"], [{"id": "one"}])

        class BrokenCollector(CodexTaskSource):
            def collect(self, runs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
                del runs
                raise RuntimeError("collector broke")

        broken = CodexTaskCache(
            collector=BrokenCollector(),
            refresh_seconds=1,
        )
        degraded = broken.refresh_now([])
        self.assertEqual(degraded["source"]["status"], "unavailable")
        self.assertIn("collector broke", degraded["source"]["warnings"])
        with self.assertRaises(ValueError):
            CodexTaskCache(refresh_seconds=0)
        with self.assertRaises(ValueError):
            CodexTaskCache(refresh_seconds=float("nan"))


class CodexAppServerClientTest(unittest.TestCase):
    def _fake_server(self, root: Path, *, rpc_error: bool = False) -> Path:
        script = root / "codex-fake"
        error_response = (
            "{'jsonrpc':'2.0','id':request['id'],'error':{'message':'history mismatch'}}"
            if rpc_error
            else "{'jsonrpc':'2.0','id':request['id'],'result':{'data':[{'status':'completed'}]}}"
        )
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            "for line in sys.stdin:\n"
            " request=json.loads(line)\n"
            " if 'id' not in request:\n"
            "  continue\n"
            " method=request.get('method')\n"
            " if method == 'initialize':\n"
            "  response={'jsonrpc':'2.0','id':request['id'],'result':{}}\n"
            " elif method == 'thread/list':\n"
            "  response={'jsonrpc':'2.0','id':request['id'],'result':{'data':[{'id':'t1'}]}}\n"
            " else:\n"
            f"  response={error_response}\n"
            " print(json.dumps(response), flush=True)\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    def test_jsonl_client_lists_threads_reads_status_and_closes(self) -> None:
        from praxist.dashboard.codex_tasks import CodexAppServerClient

        with tempfile.TemporaryDirectory() as tmp:
            client = CodexAppServerClient(str(self._fake_server(Path(tmp))))
            self.assertEqual(client.list_threads(limit=999), [{"id": "t1"}])
            self.assertEqual(client.latest_turn_status("t1"), "completed")
            client.close()
            client.close()

    def test_jsonl_client_surfaces_redacted_rpc_errors(self) -> None:
        from praxist.dashboard.codex_tasks import CodexAppServerClient

        with tempfile.TemporaryDirectory() as tmp:
            client = CodexAppServerClient(str(self._fake_server(Path(tmp), rpc_error=True)))
            try:
                with self.assertRaises(OSError) as raised:
                    client.latest_turn_status("t1")
                self.assertIn("history mismatch", str(raised.exception))
            finally:
                client.close()

    def test_low_level_rpc_timeout_pipe_and_reader_failures_are_bounded(self) -> None:
        from praxist.dashboard import codex_tasks

        client: Any = object.__new__(codex_tasks.CodexAppServerClient)
        client.binary = "/mock/codex"
        client.timeout_seconds = 0.01
        client._next_id = 1
        client._responses = queue.Queue()
        client._process = SimpleNamespace(stdin=io.StringIO(), stdout=None, poll=lambda: None)

        with self.assertRaises(TimeoutError):
            client._request("thread/list", {})

        with (
            patch.object(codex_tasks.time, "monotonic", side_effect=[0.0, 1.0]),
            self.assertRaises(TimeoutError),
        ):
            client._request("thread/list", {})

        with patch.object(client, "_request", return_value={"data": []}):
            self.assertIsNone(client.latest_turn_status("missing"))

        client._responses.put(OSError("gone"))
        with self.assertRaises(OSError) as ended:
            client._request("thread/list", {})
        self.assertIn("gone", str(ended.exception))

        client._responses.put({"id": -1, "result": {}})
        client._responses.put({"id": client._next_id, "result": "not-an-object"})
        self.assertEqual(client._request("thread/list", {}), {})

        client._process.stdin = None
        with self.assertRaises(OSError):
            client._write({})

        class BrokenInput:
            def write(self, _value: str) -> None:
                raise BrokenPipeError

            def flush(self) -> None:
                return None

        client._process.stdin = BrokenInput()
        with self.assertRaises(OSError):
            client._write({})

        client._read_responses()
        self.assertIsInstance(client._responses.get_nowait(), OSError)

        client._process.stdout = io.StringIO('not-json\n{"id": 9, "result": {}}\n')
        client._process.poll = lambda: 0
        client._read_responses()
        response = client._responses.get_nowait()
        self.assertEqual(response["id"], 9)
        self.assertIsInstance(client._responses.get_nowait(), OSError)

        client._process.stdout = io.StringIO("too long\n")
        client._process.poll = lambda: None
        with patch.object(codex_tasks, "MAX_RPC_LINE_BYTES", 1):
            client._read_responses()
        self.assertIn("exceeded", str(client._responses.get_nowait()))

        class BrokenOutput:
            def __iter__(self) -> Any:
                raise OSError("read failed")

        client._process.stdout = BrokenOutput()
        client._process.poll = lambda: None
        client._read_responses()
        self.assertIn("read failed", str(client._responses.get_nowait()))

    def test_close_escalates_when_app_server_does_not_terminate(self) -> None:
        from praxist.dashboard import codex_tasks

        client: Any = object.__new__(codex_tasks.CodexAppServerClient)
        stdin, stdout = io.StringIO(), io.StringIO()
        process = SimpleNamespace(
            stdin=stdin,
            stdout=stdout,
            poll=Mock(return_value=None),
            terminate=Mock(),
            wait=Mock(side_effect=[subprocess.TimeoutExpired(cmd="codex", timeout=1.0), 0]),
            kill=Mock(),
        )
        client._process = process
        client.close()
        process.terminate.assert_called_once()
        process.kill.assert_called_once()
        self.assertTrue(stdin.closed)
        self.assertTrue(stdout.closed)


class CodexBinaryResolutionTest(unittest.TestCase):
    def test_explicit_environment_system_and_pinned_binary_precedence(self) -> None:
        from praxist.dashboard import codex_tasks

        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "codex"
            binary.write_text("#!/bin/sh\necho codex-cli test\n", encoding="utf-8")
            binary.chmod(0o755)
            self.assertEqual(
                codex_tasks.resolve_codex_operator_binary(str(binary)), str(binary.resolve())
            )
            with patch.dict(os.environ, {"CODEX_CLI_PATH": str(binary)}):
                self.assertEqual(codex_tasks.resolve_codex_operator_binary(), str(binary.resolve()))
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(codex_tasks, "_DESKTOP_CODEX", binary),
            ):
                self.assertEqual(codex_tasks.resolve_codex_operator_binary(), str(binary))
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(codex_tasks, "_DESKTOP_CODEX", Path("/missing/codex")),
                patch("praxist.dashboard.codex_tasks.shutil.which", return_value=str(binary)),
            ):
                self.assertEqual(codex_tasks.resolve_codex_operator_binary(), str(binary))
            with (
                patch.dict(os.environ, {}, clear=True),
                patch.object(codex_tasks, "_DESKTOP_CODEX", Path("/missing/codex")),
                patch("praxist.dashboard.codex_tasks.shutil.which", return_value=None),
                patch(
                    "praxist.plugins.agent_runtimes.codex_sdk._auth.resolve_codex_binary",
                    return_value=str(binary),
                ),
            ):
                self.assertEqual(codex_tasks.resolve_codex_operator_binary(), str(binary.resolve()))
            self.assertEqual(codex_tasks.codex_binary_version(str(binary)), "codex-cli test")
            with patch(
                "praxist.dashboard.codex_tasks.subprocess.run",
                side_effect=subprocess.TimeoutExpired(cmd=str(binary), timeout=2.0),
            ):
                self.assertEqual(codex_tasks.codex_binary_version(str(binary)), "unknown")
            self.assertEqual(
                codex_tasks._resume_command("thread-id", "/Applications/Codex App/codex"),
                "'/Applications/Codex App/codex' resume thread-id",
            )

        with self.assertRaises(OSError):
            codex_tasks.resolve_codex_operator_binary("/missing/codex")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
