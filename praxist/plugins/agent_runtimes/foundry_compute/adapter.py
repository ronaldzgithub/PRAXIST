"""Praxist AgentRuntime adapter for Foundry's untrusted compute pool."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from praxist.core.credentials import CredentialRef
from praxist.core.protocol import (
    AgentEvent,
    AgentRunRequest,
    AgentRunResult,
    JSONValue,
    ToolCallRecord,
)
from praxist.core.runtimes import AgentRuntimeExecutionContext
from praxist.plugins.agent_runtimes.foundry_compute.transport import (
    FoundryTransport,
    FoundryTransportError,
)
from praxist.plugins.agent_runtimes.foundry_compute.workspace import (
    apply_workspace_result,
    snapshot_workspace,
)

RUNTIME_REF = "agent_runtime:foundry_compute"
LOCAL_RUNTIME_REF = "agent_runtime:codex_sdk"
PROVIDER_REF = "model_provider:openai_compatible"
CHATGPT_KEY_ID = "openai_compatible:codex_sdk:chatgpt:foundry_compute_pool"


def create_runtime() -> FoundryComputeRuntime:
    """Return a process-local Foundry compute runtime adapter."""
    return FoundryComputeRuntime()


class FoundryComputeRuntime:
    """Submit one peer request while Praxist retains all research-loop authority."""

    runtime_ref = RUNTIME_REF

    def __init__(
        self,
        *,
        transport_factory: Callable[[str, str], FoundryTransport] | None = None,
    ) -> None:
        self._transport_factory = transport_factory or (
            lambda url, token: FoundryTransport(url, token)
        )

    def discover_managed_credential(self, model_provider_ref: str) -> CredentialRef | None:
        """Declare the remote node pool's redacted, node-local ChatGPT identity."""
        if model_provider_ref != PROVIDER_REF:
            return None
        return CredentialRef(
            scope="model_provider",
            provider="openai_compatible",
            target_ref=PROVIDER_REF,
            key_id=CHATGPT_KEY_ID,
            source="runtime_session",
        )

    async def execute(
        self,
        request: AgentRunRequest,
        context: AgentRuntimeExecutionContext,
    ) -> AgentRunResult:
        """Run one remote peer without exposing the central Praxist run store."""
        return await asyncio.to_thread(self._execute_blocking, request, context)

    def execute_sync(self, request: AgentRunRequest) -> AgentRunResult:
        """Synchronous entrypoint for conformance harnesses and direct callers."""
        return self._execute_blocking(request, AgentRuntimeExecutionContext())

    def _execute_blocking(
        self,
        request: AgentRunRequest,
        context: AgentRuntimeExecutionContext,
    ) -> AgentRunResult:
        validation = _validate_request(request)
        if validation:
            return _failure(request, validation, "invalid_request")
        try:
            server_url = _required_environment("PRAXIST_FOUNDRY_COMPUTE_URL")
            token = _required_environment("PRAXIST_FOUNDRY_COMPUTE_ADMIN_TOKEN")
            transport = self._transport_factory(server_url, token)
            workspace = Path(request.cwd).expanduser().resolve()
            snapshot = snapshot_workspace(workspace)
            omitted_tools, tool_servers = _remote_tool_servers(request.tool_servers)
            request_payload = _remote_request_payload(
                request,
                context=context,
                tool_servers=tool_servers,
            )
            request_bytes = (
                json.dumps(request_payload, ensure_ascii=False, sort_keys=True) + "\n"
            ).encode("utf-8")
            request_artifact = transport.upload(
                request_bytes,
                media_type="application/json",
                filename="agent_request.json",
            )
            workspace_artifact = transport.upload(
                snapshot.archive,
                media_type="application/vnd.foundry.directory+tar",
                filename="workspace.tar",
            )
            input_artifacts = [
                _artifact_ref("agent_request", request_artifact, "agent_request.json"),
                _artifact_ref("workspace", workspace_artifact, "workspace.tar"),
            ]
            timeout_seconds = _remote_timeout(request.timeout_seconds)
            job_id = _job_id(
                request=request,
                request_digest=request_artifact["sha256"],
                workspace_digest=workspace_artifact["sha256"],
            )
            draft = {
                "job_id": job_id,
                "job_kind": "praxist.peer.v1",
                "task": {
                    "request_id": request.request_id,
                    "run_id": request.run_id,
                    "stage_id": request.stage_id,
                    "workspace_baseline_sha256": hashlib.sha256(snapshot.archive).hexdigest(),
                    "omitted_central_tool_servers": omitted_tools,
                },
                "input_artifacts": input_artifacts,
                "runtime_capsule": _runtime_capsule(),
                "requirements": {
                    "capabilities": ["praxist.codex_peer"],
                    "platforms": _csv_environment("PRAXIST_FOUNDRY_COMPUTE_PLATFORMS"),
                    "runtime_labels": _json_object_environment(
                        "PRAXIST_FOUNDRY_COMPUTE_RUNTIME_LABELS", default={}
                    ),
                    "resources": {
                        "cpu_cores": 1,
                        "memory_mb": 2048,
                        "disk_mb": 4096,
                    },
                },
                "policy": {
                    "filesystem": "isolated_workspace",
                    "network": "on",
                    "external_actions_allowed": False,
                    "secrets_allowed": False,
                    "human_approval": "none",
                },
                "acceptance": {
                    "schema_version": "foundry-praxist-worker-result.v1",
                    "agent_result_schema": "praxist.agent-run-result.v1",
                    "research_state_authority": "praxist_central",
                },
                "provenance": {
                    "submitted_by": "praxist-agent-runtime",
                    "evidence_class": "internal_review",
                    "lineage_refs": [request.run_id, request.request_id, request.stage_id],
                },
                "timeout_seconds": timeout_seconds,
                "max_attempts": 2,
                "priority": 50,
            }
            submitted = transport.submit(draft)
            job = transport.wait_job(
                submitted["job_id"],
                timeout_seconds=timeout_seconds * 2 + 300,
                stop_requested=context.stop_requested,
            )
            if job["status"] != "SUCCEEDED":
                error = job.get("error") if isinstance(job.get("error"), dict) else {}
                return _failure(
                    request,
                    str(error.get("message") or f"remote job ended as {job['status']}"),
                    "runtime_error" if job["status"] == "FAILED" else "none",
                    cancelled=job["status"] == "CANCELLED",
                )
            result_payload, workspace_archive = _download_result(transport, job)
            result = _result_from_payload(result_payload, request=request)
            try:
                merge = apply_workspace_result(
                    workspace,
                    baseline_manifest=snapshot.manifest,
                    archive=workspace_archive,
                )
                merge_type = "workspace_merged"
                merge_payload: dict[str, Any] = merge
            except RuntimeError as exc:
                # A completed peer result remains valid evidence even when the central
                # workspace changed concurrently. Never overwrite that newer state.
                merge_type = "workspace_merge_conflict"
                merge_payload = {"applied": False, "error": _safe_error(exc)}
            if omitted_tools:
                warning = AgentEvent(
                    event_id=f"evt_{hashlib.sha256((job_id + ':tools').encode()).hexdigest()[:20]}",
                    run_id=request.run_id,
                    agent_run_id=request.request_id,
                    stage_id=request.stage_id,
                    type="runtime_warning",
                    payload={
                        "warning": "central in-process tool servers were omitted on the remote node",
                        "omitted_server_names": cast(list[JSONValue], omitted_tools),
                    },
                    artifact_refs=[],
                    credential_refs=[],
                    timestamp_ms=int(time.time() * 1000),
                )
                events = list(result.events)
                insert_at = max(0, len(events) - 1)
                events.insert(insert_at, warning)
                result = replace(result, events=events)
            merge_event = AgentEvent(
                event_id=f"evt_{hashlib.sha256((job_id + ':merge').encode()).hexdigest()[:20]}",
                run_id=request.run_id,
                agent_run_id=request.request_id,
                stage_id=request.stage_id,
                type=merge_type,
                payload=merge_payload,
                artifact_refs=[],
                credential_refs=[],
                timestamp_ms=int(time.time() * 1000),
            )
            events = list(result.events)
            events.insert(max(0, len(events) - 1), merge_event)
            result = replace(result, events=events, credential_ref=request.credential_ref)
            if context.message_callback is not None:
                for event in result.events:
                    with suppress(Exception):
                        context.message_callback(event)
            return result
        except (FoundryTransportError, OSError, RuntimeError, ValueError) as exc:
            return _failure(request, _safe_error(exc), "runtime_error")


def _validate_request(request: AgentRunRequest) -> str | None:
    if request.agent_runtime_ref != RUNTIME_REF:
        return f"runtime mismatch: expected {RUNTIME_REF}, got {request.agent_runtime_ref}"
    if request.model_call.provider_ref != PROVIDER_REF:
        return (
            "foundry_compute supports only model_provider:openai_compatible with node-local login"
        )
    if not request.cwd:
        return "foundry_compute requires a workspace cwd"
    return None


def _remote_request_payload(
    request: AgentRunRequest,
    *,
    context: AgentRuntimeExecutionContext,
    tool_servers: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = request.to_dict()
    managed = CredentialRef(
        scope="model_provider",
        provider="openai_compatible",
        target_ref=PROVIDER_REF,
        key_id=CHATGPT_KEY_ID,
        source="runtime_session",
    ).to_dict()
    payload["agent_runtime_ref"] = LOCAL_RUNTIME_REF
    payload["cwd"] = "."
    payload["credential_ref"] = managed
    payload["model_call"]["credential_ref"] = managed
    payload["credential_mode"] = "single"
    payload["tool_servers"] = tool_servers
    payload["env_policy"]["scoped_credential_refs"] = [managed]
    runtime_options = dict(payload.get("runtime_options") or {})
    for key in ("run_dir", "provider_base_url", "codex_bin", "runtime_env_overrides"):
        runtime_options.pop(key, None)
    runtime_options["foundry_remote_request_id"] = request.request_id
    payload["runtime_options"] = runtime_options
    return {
        "schema_version": "praxist-foundry-agent-request.v1",
        "request": payload,
        "remote_env": _remote_environment(request, context.env),
    }


def _remote_tool_servers(
    values: list[dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    omitted: list[str] = []
    forwarded: list[dict[str, Any]] = []
    for value in values:
        if not isinstance(value, dict):
            continue
        name = str(value.get("server_name") or "unknown")
        if bool(value.get("requires_run_dir")):
            omitted.append(name)
            continue
        forwarded.append(dict(value))
    return sorted(set(omitted)), forwarded


def _remote_environment(request: AgentRunRequest, environment: Mapping[str, str]) -> dict[str, str]:
    exposed = set(request.env_policy.exposed_env_keys)
    denied = {"PATH", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PRAXIST_TASK_PYTHON"}
    result: dict[str, str] = {}
    for key in sorted(exposed):
        upper = key.upper()
        value = environment.get(key)
        if (
            value in (None, "")
            or key in denied
            or any(marker in upper for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD"))
            or len(str(value)) > 4096
        ):
            continue
        result[key] = str(value)
    return result


def _download_result(
    transport: FoundryTransport, job: dict[str, Any]
) -> tuple[dict[str, Any], bytes]:
    receipt = job.get("receipt")
    if not isinstance(receipt, dict) or not isinstance(receipt.get("result"), dict):
        raise FoundryTransportError("Foundry compute receipt is missing its Praxist result")
    wrapper = receipt["result"]
    if wrapper.get("schema_version") != "foundry-praxist-worker-result.v1":
        raise FoundryTransportError("Foundry compute Praxist result version is unsupported")
    outputs = {item["name"]: item for item in receipt.get("output_artifacts", [])}
    if "praxist_result" not in outputs or "workspace_result" not in outputs:
        raise FoundryTransportError("Foundry compute Praxist outputs are incomplete")
    result_bytes = transport.download(outputs["praxist_result"]["sha256"])
    try:
        result = json.loads(result_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FoundryTransportError("Foundry compute Praxist result is invalid JSON") from exc
    if result != wrapper.get("agent_run_result"):
        raise FoundryTransportError("Foundry compute Praxist result differs from its receipt")
    return result, transport.download(outputs["workspace_result"]["sha256"])


def _result_from_payload(value: dict[str, Any], *, request: AgentRunRequest) -> AgentRunResult:
    events = [
        AgentEvent(
            event_id=str(item["event_id"]),
            run_id=str(item["run_id"]),
            agent_run_id=item.get("agent_run_id"),
            stage_id=item.get("stage_id"),
            type=str(item["type"]),
            payload=dict(item.get("payload") or {}),
            artifact_refs=list(item.get("artifact_refs") or []),
            credential_refs=[],
            timestamp_ms=int(item["timestamp_ms"]),
        )
        for item in value.get("events", [])
    ]
    tools = [
        ToolCallRecord(
            tool_call_id=str(item["tool_call_id"]),
            server_name=str(item["server_name"]),
            tool_name=str(item["tool_name"]),
            started_at_ms=int(item["started_at_ms"]),
            finished_at_ms=(
                int(item["finished_at_ms"]) if item.get("finished_at_ms") is not None else None
            ),
            success=bool(item["success"]),
            artifact_refs=list(item.get("artifact_refs") or []),
            failover_reason=item.get("failover_reason"),
        )
        for item in value.get("tool_uses", [])
    ]
    usage = {
        str(key): float(amount)
        for key, amount in dict(value.get("usage") or {}).items()
        if isinstance(amount, (int, float)) and not isinstance(amount, bool)
    }
    return AgentRunResult(
        success=bool(value.get("success")),
        events=events,
        text_output_refs=list(value.get("text_output_refs") or []),
        tool_uses=tools,
        error=value.get("error"),
        failover_reason=value.get("failover_reason"),
        credential_ref=request.credential_ref,
        usage=usage,
        terminal_status=value.get("terminal_status"),
        timed_out=bool(value.get("timed_out")),
        cancelled=bool(value.get("cancelled")),
    )


def _failure(
    request: AgentRunRequest,
    message: str,
    failover_reason: str,
    *,
    cancelled: bool = False,
) -> AgentRunResult:
    event = AgentEvent(
        event_id=f"evt_{hashlib.sha256((request.request_id + ':failure').encode()).hexdigest()[:20]}",
        run_id=request.run_id,
        agent_run_id=request.request_id,
        stage_id=request.stage_id,
        type="final_result",
        payload={"success": False, "error": message[:4000]},
        artifact_refs=[],
        credential_refs=[],
        timestamp_ms=int(time.time() * 1000),
    )
    return AgentRunResult(
        success=False,
        events=[event],
        text_output_refs=[],
        tool_uses=[],
        error=message[:4000],
        failover_reason=failover_reason,
        credential_ref=request.credential_ref,
        usage={},
        terminal_status="cancelled" if cancelled else "failed",
        cancelled=cancelled,
    )


def _artifact_ref(name: str, value: dict[str, Any], filename: str) -> dict[str, Any]:
    return {
        "name": name,
        "sha256": value["sha256"],
        "size_bytes": value["size_bytes"],
        "media_type": value["media_type"],
        "filename": filename,
    }


def _job_id(*, request: AgentRunRequest, request_digest: str, workspace_digest: str) -> str:
    value = json.dumps(
        {
            "runtime": RUNTIME_REF,
            "request_id": request.request_id,
            "run_id": request.run_id,
            "request_digest": request_digest,
            "workspace_digest": workspace_digest,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"job_{hashlib.sha256(value.encode()).hexdigest()[:24]}"


def _remote_timeout(value: int) -> int:
    default = int(os.environ.get("PRAXIST_FOUNDRY_COMPUTE_TIMEOUT_SECONDS", "7200"))
    selected = value if value > 0 else default
    return max(1, min(selected, 86_400))


def _runtime_capsule() -> dict[str, Any] | None:
    raw_path = os.environ.get("PRAXIST_FOUNDRY_COMPUTE_RUNTIME_CAPSULE", "").strip()
    if not raw_path:
        return None
    try:
        value = json.loads(Path(raw_path).expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Praxist Foundry runtime capsule reference is invalid") from exc
    if not isinstance(value, dict):
        raise ValueError("Praxist Foundry runtime capsule reference must be an object")
    return value


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"Required Praxist Foundry environment variable is missing: {name}")
    return value


def _csv_environment(name: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, "").split(",") if item.strip()]


def _json_object_environment(name: str, *, default: dict[str, str]) -> dict[str, str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return dict(default)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must contain a JSON object") from exc
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(item, str) for key, item in value.items()
    ):
        raise ValueError(f"{name} must map strings to strings")
    return dict(value)


def _safe_error(exc: Exception) -> str:
    value = str(exc) or exc.__class__.__name__
    for marker in (
        "PRAXIST_FOUNDRY_COMPUTE_ADMIN_TOKEN",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        value = value.replace(marker, "[REDACTED]")
    return value[:4000]
