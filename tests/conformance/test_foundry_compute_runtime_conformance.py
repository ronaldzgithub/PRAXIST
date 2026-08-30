"""Offline contract tests for the Foundry compute AgentRuntime plugin."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from praxist.core.credentials import CredentialRef
from praxist.core.protocol import (
    AgentRunRequest,
    CachePolicy,
    EnvPolicy,
    ModelCallSpec,
    ToolPermissionSet,
)
from praxist.core.runtimes import AgentRuntimeExecutionContext, runtime_for_ref
from praxist.plugins.agent_runtimes.foundry_compute import adapter, worker
from praxist.plugins.agent_runtimes.foundry_compute.workspace import (
    apply_workspace_result,
    snapshot_workspace,
)


class _FakeTransport:
    def __init__(self) -> None:
        self.artifacts: dict[str, bytes] = {}
        self.draft: dict | None = None
        self.cancelled = False

    def upload(self, data: bytes, *, media_type: str, filename: str) -> dict:
        digest = hashlib.sha256(data).hexdigest()
        self.artifacts[digest] = data
        return {
            "sha256": digest,
            "size_bytes": len(data),
            "media_type": media_type,
            "filename": filename,
        }

    def download(self, digest: str) -> bytes:
        return self.artifacts[digest]

    def submit(self, draft: dict) -> dict:
        self.draft = draft
        return {"job_id": draft["job_id"]}

    def wait_job(self, job_id: str, **_kwargs: object) -> dict:
        assert self.draft is not None
        workspace_ref = next(
            item for item in self.draft["input_artifacts"] if item["name"] == "workspace"
        )
        result = {
            "success": True,
            "events": [
                {
                    "event_id": "evt_started",
                    "run_id": "run_remote",
                    "agent_run_id": "request_remote",
                    "stage_id": "research_loop",
                    "type": "agent_run_started",
                    "payload": {},
                    "artifact_refs": [],
                    "credential_refs": [],
                    "timestamp_ms": 1,
                },
                {
                    "event_id": "evt_final",
                    "run_id": "run_remote",
                    "agent_run_id": "request_remote",
                    "stage_id": "research_loop",
                    "type": "final_result",
                    "payload": {"success": True},
                    "artifact_refs": [],
                    "credential_refs": [],
                    "timestamp_ms": 2,
                },
            ],
            "text_output_refs": [{"text": "remote complete"}],
            "tool_uses": [],
            "error": None,
            "failover_reason": "none",
            "credential_ref": None,
            "usage": {"input_tokens": 3, "output_tokens": 2},
            "terminal_status": "completed",
            "timed_out": False,
            "cancelled": False,
        }
        result_bytes = (json.dumps(result, sort_keys=True) + "\n").encode()
        result_ref = self.upload(
            result_bytes, media_type="application/json", filename="agent_run_result.json"
        )
        workspace_bytes = self.artifacts[workspace_ref["sha256"]]
        workspace_result = self.upload(
            workspace_bytes,
            media_type="application/vnd.foundry.directory+tar",
            filename="workspace_result.tar",
        )
        return {
            "job_id": job_id,
            "status": "SUCCEEDED",
            "receipt": {
                "result": {
                    "schema_version": "foundry-praxist-worker-result.v1",
                    "agent_run_result": result,
                    "workspace_artifact": "workspace_result",
                },
                "output_artifacts": [
                    {"name": "praxist_result", **result_ref},
                    {"name": "workspace_result", **workspace_result},
                ],
            },
        }

    def cancel(self, job_id: str, reason: str) -> dict:
        self.cancelled = True
        return {"job_id": job_id, "status": "CANCELLED", "error": {"message": reason}}


def _request(cwd: Path, *, provider_ref: str = adapter.PROVIDER_REF) -> AgentRunRequest:
    credential = CredentialRef(
        scope="model_provider",
        provider="openai_compatible",
        target_ref=provider_ref,
        key_id=adapter.CHATGPT_KEY_ID,
        source="runtime_session",
    )
    return AgentRunRequest(
        request_id="request_remote",
        run_id="run_remote",
        stage_id="research_loop",
        role_ref="task_role:peer",
        agent_runtime_ref=adapter.RUNTIME_REF,
        prompt_ref={"kind": "inline", "text": "Improve the task"},
        system_prompt_ref=None,
        cwd=str(cwd),
        model_profile_ref="codex_peer",
        model_call=ModelCallSpec(
            profile_id="codex_peer",
            provider_ref=provider_ref,
            api_format="responses",
            model="gpt-test",
            parameters={},
            credential_ref=credential,
        ),
        tool_permissions=ToolPermissionSet(mode="allow_list", allowed_tools=[]),
        tool_servers=[
            {
                "server_name": "frontier-tools",
                "transport": "legacy_inprocess",
                "requires_run_dir": True,
            }
        ],
        env_policy=EnvPolicy(
            exposed_env_keys=["TASK_MODE", "OPENAI_API_KEY", "PATH"],
            scoped_credential_refs=[credential],
        ),
        credential_ref=credential,
        credential_mode="single",
        budget_grant_id="grant_remote",
        artifact_scope="run",
        timeout_seconds=30,
        cache_policy=CachePolicy(mode="runtime_auto", frozen_prefix_hash=None),
        runtime_options={"run_dir": "/central/run", "reasoning_effort": "low"},
    )


class FoundryComputeRuntimeConformanceTest(unittest.IsolatedAsyncioTestCase):
    async def test_remote_result_round_trip_preserves_central_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "task"
            workspace.mkdir()
            workspace.joinpath("candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
            fake = _FakeTransport()
            runtime = adapter.FoundryComputeRuntime(
                transport_factory=lambda _url, _token: fake  # type: ignore[arg-type]
            )
            observed = []
            with patch.dict(
                os.environ,
                {
                    "PRAXIST_FOUNDRY_COMPUTE_URL": "http://127.0.0.1:8765",
                    "PRAXIST_FOUNDRY_COMPUTE_ADMIN_TOKEN": "t" * 20,
                },
                clear=False,
            ):
                result = await runtime.execute(
                    _request(workspace),
                    AgentRuntimeExecutionContext(
                        env={
                            "TASK_MODE": "fixture",
                            "OPENAI_API_KEY": "must-not-leave-central",
                            "PATH": "/central/bin",
                        },
                        message_callback=observed.append,
                    ),
                )
            self.assertTrue(result.success)
            self.assertEqual(result.usage, {"input_tokens": 3.0, "output_tokens": 2.0})
            self.assertIn("runtime_warning", [event.type for event in result.events])
            self.assertIn("workspace_merged", [event.type for event in result.events])
            self.assertEqual(observed, result.events)
            self.assertIsNotNone(fake.draft)
            assert fake.draft is not None
            self.assertEqual(fake.draft["job_kind"], "praxist.peer.v1")
            self.assertEqual(
                fake.draft["acceptance"]["research_state_authority"], "praxist_central"
            )
            request_ref = next(
                item for item in fake.draft["input_artifacts"] if item["name"] == "agent_request"
            )
            remote_request = json.loads(fake.artifacts[request_ref["sha256"]])
            self.assertEqual(remote_request["remote_env"], {"TASK_MODE": "fixture"})
            self.assertEqual(remote_request["request"]["tool_servers"], [])
            self.assertNotIn("run_dir", remote_request["request"]["runtime_options"])

    async def test_invalid_provider_fails_without_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = await adapter.FoundryComputeRuntime().execute(
                _request(Path(tmp), provider_ref="model_provider:openrouter"),
                AgentRuntimeExecutionContext(),
            )
        self.assertFalse(result.success)
        self.assertEqual(result.failover_reason, "invalid_request")

    async def test_completed_result_survives_stale_workspace_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            workspace.joinpath("candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
            fake = _FakeTransport()
            runtime = adapter.FoundryComputeRuntime(
                transport_factory=lambda _url, _token: fake  # type: ignore[arg-type]
            )
            with (
                patch.dict(
                    os.environ,
                    {
                        "PRAXIST_FOUNDRY_COMPUTE_URL": "http://127.0.0.1:8765",
                        "PRAXIST_FOUNDRY_COMPUTE_ADMIN_TOKEN": "t" * 20,
                    },
                    clear=False,
                ),
                patch.object(
                    adapter,
                    "apply_workspace_result",
                    side_effect=RuntimeError("workspace changed while peer was running"),
                ),
            ):
                result = await runtime.execute(_request(workspace), AgentRuntimeExecutionContext())
        self.assertTrue(result.success)
        self.assertEqual(result.text_output_refs, [{"text": "remote complete"}])
        self.assertIn("workspace_merge_conflict", [event.type for event in result.events])

    def test_plugin_loads_through_registry(self) -> None:
        runtime = runtime_for_ref(adapter.RUNTIME_REF)
        self.assertEqual(runtime.runtime_ref, adapter.RUNTIME_REF)
        credential = runtime.discover_managed_credential(adapter.PROVIDER_REF)
        self.assertIsNotNone(credential)
        self.assertEqual(credential.key_id, adapter.CHATGPT_KEY_ID)


class FoundryComputeWorkspaceTest(unittest.TestCase):
    def test_workspace_merge_changes_creates_and_deletes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "central"
            remote = Path(tmp) / "remote"
            root.mkdir()
            remote.mkdir()
            root.joinpath("change.txt").write_text("old\n", encoding="utf-8")
            root.joinpath("delete.txt").write_text("delete\n", encoding="utf-8")
            baseline = snapshot_workspace(root)
            remote.joinpath("change.txt").write_text("new\n", encoding="utf-8")
            remote.joinpath("create.txt").write_text("create\n", encoding="utf-8")
            output = snapshot_workspace(remote)
            merged = apply_workspace_result(
                root,
                baseline_manifest=baseline.manifest,
                archive=output.archive,
            )
            self.assertEqual(merged, {"changed": 1, "created": 1, "deleted": 1})
            self.assertEqual(root.joinpath("change.txt").read_text(), "new\n")
            self.assertFalse(root.joinpath("delete.txt").exists())

    def test_workspace_rejects_secrets_and_stale_merge(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.joinpath("safe.txt").write_text("safe\n", encoding="utf-8")
            root.joinpath(".env").write_text("OPENAI_API_KEY=excluded\n", encoding="utf-8")
            baseline = snapshot_workspace(root)
            self.assertNotIn(".env", baseline.manifest)
            root.joinpath("leak.txt").write_text("OPENAI_API_KEY=not-allowed\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "credential material"):
                snapshot_workspace(root)
            root.joinpath("leak.txt").unlink()
            root.joinpath("safe.txt").write_text("concurrent\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "changed while"):
                apply_workspace_result(
                    root,
                    baseline_manifest=baseline.manifest,
                    archive=baseline.archive,
                )

    def test_workspace_rejects_escaping_tar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.joinpath("safe.txt").write_text("safe\n", encoding="utf-8")
            baseline = snapshot_workspace(root)
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode="w") as archive:
                data = b"escape"
                info = tarfile.TarInfo("../escape.txt")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
            with self.assertRaisesRegex(ValueError, "unsafe"):
                apply_workspace_result(
                    root,
                    baseline_manifest=baseline.manifest,
                    archive=output.getvalue(),
                )

    def test_workspace_rejects_duplicate_tar_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            root.joinpath("safe.txt").write_text("safe\n", encoding="utf-8")
            baseline = snapshot_workspace(root)
            output = io.BytesIO()
            with tarfile.open(fileobj=output, mode="w") as archive:
                for data in (b"first", b"second"):
                    info = tarfile.TarInfo("same.txt")
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            with self.assertRaisesRegex(ValueError, "duplicate"):
                apply_workspace_result(
                    root,
                    baseline_manifest=baseline.manifest,
                    archive=output.getvalue(),
                )

    def test_worker_rehydrates_only_local_codex_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            request = _request(Path(tmp))
            payload = adapter._remote_request_payload(  # noqa: SLF001
                request,
                context=AgentRuntimeExecutionContext(env={"TASK_MODE": "fixture"}),
                tool_servers=[],
            )
            rehydrated = worker._request_from_payload(  # noqa: SLF001
                payload["request"], workspace=Path(tmp)
            )
            self.assertEqual(rehydrated.agent_runtime_ref, "agent_runtime:codex_sdk")
            self.assertEqual(rehydrated.cwd, str(Path(tmp)))
            self.assertNotIn("run_dir", rehydrated.runtime_options)


if __name__ == "__main__":
    unittest.main()
