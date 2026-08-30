"""Node-side bridge from frozen JSON to the local Codex SDK runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from praxist.core.credentials import CredentialRef
from praxist.core.protocol import (
    AgentRunRequest,
    CachePolicy,
    EnvPolicy,
    ModelCallSpec,
    ToolPermissionSet,
)
from praxist.core.runtimes import AgentRuntimeExecutionContext
from praxist.plugins.agent_runtimes.codex_sdk.adapter import CodexSdkRuntime


def build_parser() -> argparse.ArgumentParser:
    """Build the fixed node-side worker argument parser."""
    parser = argparse.ArgumentParser(prog="praxist-foundry-worker")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Execute one frozen request with the node-local Codex SDK identity."""
    args = build_parser().parse_args(argv)
    wrapper = _read_object(args.request.expanduser().resolve())
    if wrapper.get("schema_version") != "praxist-foundry-agent-request.v1":
        raise ValueError("Praxist Foundry request version is unsupported")
    request = _request_from_payload(
        _object(wrapper["request"], "request"),
        workspace=args.workspace.expanduser().resolve(),
    )
    environment = {
        str(key): str(value)
        for key, value in _object(wrapper.get("remote_env", {}), "remote_env").items()
    }
    result = asyncio.run(_execute(request, environment))
    _atomic_json(args.result.expanduser().resolve(), result.to_dict())
    return 0


async def _execute(request: AgentRunRequest, environment: dict[str, str]) -> Any:
    runtime = CodexSdkRuntime()
    credential = runtime.discover_managed_credential(request.model_call.provider_ref)
    if credential is None:
        raise RuntimeError("Node-local Codex ChatGPT login is unavailable")
    model_call = replace(request.model_call, credential_ref=credential)
    request = replace(request, credential_ref=credential, model_call=model_call)
    try:
        return await runtime.execute(
            request,
            AgentRuntimeExecutionContext(env=environment),
        )
    finally:
        await runtime.aclose()


def _request_from_payload(value: dict[str, Any], *, workspace: Path) -> AgentRunRequest:
    if value.get("agent_runtime_ref") != "agent_runtime:codex_sdk":
        raise ValueError("Praxist Foundry worker accepts only the local codex_sdk runtime")
    model = _object(value["model_call"], "model_call")
    tool_permissions = _object(value["tool_permissions"], "tool_permissions")
    env_policy = _object(value["env_policy"], "env_policy")
    cache = _object(value["cache_policy"], "cache_policy")
    return AgentRunRequest(
        request_id=str(value["request_id"]),
        run_id=str(value["run_id"]),
        stage_id=str(value["stage_id"]),
        role_ref=value.get("role_ref"),
        agent_runtime_ref="agent_runtime:codex_sdk",
        prompt_ref=_object(value["prompt_ref"], "prompt_ref"),
        system_prompt_ref=(
            _object(value["system_prompt_ref"], "system_prompt_ref")
            if value.get("system_prompt_ref") is not None
            else None
        ),
        cwd=str(workspace),
        model_profile_ref=str(value["model_profile_ref"]),
        model_call=ModelCallSpec(
            profile_id=str(model["profile_id"]),
            provider_ref=str(model["provider_ref"]),
            api_format=str(model["api_format"]),
            model=str(model["model"]),
            parameters=dict(model.get("parameters") or {}),
            credential_ref=_credential(model.get("credential_ref")),
        ),
        tool_permissions=ToolPermissionSet(
            mode=str(tool_permissions.get("mode", "allow_all")),
            allowed_tools=[str(item) for item in tool_permissions.get("allowed_tools", [])],
            denied_tools=[str(item) for item in tool_permissions.get("denied_tools", [])],
        ),
        tool_servers=[dict(item) for item in value.get("tool_servers", [])],
        env_policy=EnvPolicy(
            redaction_required=bool(env_policy.get("redaction_required", True)),
            exposed_env_keys=[str(item) for item in env_policy.get("exposed_env_keys", [])],
            scoped_credential_refs=[
                credential
                for item in env_policy.get("scoped_credential_refs", [])
                if (credential := _credential(item)) is not None
            ],
        ),
        credential_ref=_credential(value.get("credential_ref")),
        credential_mode=str(value.get("credential_mode", "single")),
        budget_grant_id=value.get("budget_grant_id"),
        artifact_scope=str(value.get("artifact_scope", "run")),
        timeout_seconds=int(value.get("timeout_seconds", 0)),
        cache_policy=CachePolicy(
            mode=str(cache["mode"]),
            frozen_prefix_hash=cache.get("frozen_prefix_hash"),
            cache_breakpoints=[str(item) for item in cache.get("cache_breakpoints", [])],
            runtime_cache_strategy=cache.get("runtime_cache_strategy"),
            provider_cache_strategy=cache.get("provider_cache_strategy"),
        ),
        runtime_options=dict(value.get("runtime_options") or {}),
        role_skill_sha256=value.get("role_skill_sha256"),
    )


def _credential(value: Any) -> CredentialRef | None:
    if value is None:
        return None
    item = _object(value, "credential_ref")
    return CredentialRef(
        scope=str(item["scope"]),
        provider=str(item["provider"]),
        target_ref=item.get("target_ref"),
        key_id=str(item["key_id"]),
        source=str(item["source"]),
        status=str(item.get("status", "active")),
    )


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Praxist Foundry worker request is invalid JSON") from exc
    return _object(value, "worker request")


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Praxist Foundry {label} must be an object")
    return dict(value)


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


if __name__ == "__main__":
    raise SystemExit(main())
