"""Secret-aware workspace snapshots and optimistic remote-result merging."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import tarfile
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

MAX_FILES = 50_000
MAX_BYTES = 512 * 1024 * 1024
EXCLUDED_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".codex",
        ".env",
        ".env.local",
        "credentials.json",
        "auth.json",
    }
)
SECRET_FILENAME_MARKERS = ("private_key", "id_rsa", "id_ed25519", ".pem", ".key")
SECRET_CONTENT_MARKERS = (
    b"-----BEGIN PRIVATE KEY-----",
    b"-----BEGIN OPENSSH PRIVATE KEY-----",
    b"OPENAI_API_KEY=",
    b"ANTHROPIC_API_KEY=",
    b"OPENROUTER_API_KEY=",
)


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """Deterministic workspace archive paired with its file manifest."""

    archive: bytes
    manifest: dict[str, dict[str, Any]]


def snapshot_workspace(root: Path) -> WorkspaceSnapshot:
    """Freeze regular, non-secret workspace files into a deterministic tar."""
    source = root.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"Praxist remote workspace does not exist: {source}")
    files = _workspace_files(source)
    manifest: dict[str, dict[str, Any]] = {}
    output = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path in files:
            relative = path.relative_to(source).as_posix()
            data = path.read_bytes()
            total += len(data)
            if total > MAX_BYTES:
                raise ValueError("Praxist remote workspace exceeds the size limit")
            _reject_secret_content(relative, data)
            mode = 0o755 if os.access(path, os.X_OK) else 0o644
            manifest[relative] = {
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
                "mode": mode,
            }
            info = tarfile.TarInfo(relative)
            info.size = len(data)
            info.mode = mode
            info.mtime = 0
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, io.BytesIO(data))
    return WorkspaceSnapshot(archive=output.getvalue(), manifest=manifest)


def apply_workspace_result(
    root: Path,
    *,
    baseline_manifest: dict[str, dict[str, Any]],
    archive: bytes,
) -> dict[str, int]:
    """Merge a remote workspace only if the central baseline is unchanged."""
    destination = root.expanduser().resolve()
    current = snapshot_workspace(destination).manifest
    if current != baseline_manifest:
        raise RuntimeError(
            "Praxist workspace changed while its remote peer was running; refusing stale merge"
        )
    temporary = Path(tempfile.mkdtemp(prefix="praxist-foundry-result-"))
    try:
        _safe_extract(archive, temporary)
        incoming = snapshot_workspace(temporary).manifest
        changed = 0
        created = 0
        deleted = 0
        for relative, metadata in incoming.items():
            if baseline_manifest.get(relative) == metadata:
                continue
            source = temporary / Path(relative)
            target = (destination / Path(relative)).resolve()
            if not target.is_relative_to(destination):
                raise ValueError(f"Remote workspace result path escaped: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary_file = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            shutil.copyfile(source, temporary_file)
            os.chmod(temporary_file, int(metadata["mode"]))
            os.replace(temporary_file, target)
            if relative in baseline_manifest:
                changed += 1
            else:
                created += 1
        for relative in sorted(set(baseline_manifest) - set(incoming), reverse=True):
            target = (destination / Path(relative)).resolve()
            if not target.is_relative_to(destination):
                raise ValueError(f"Remote workspace deletion escaped: {relative}")
            if target.is_file():
                target.unlink()
                deleted += 1
        _remove_empty_directories(destination)
        return {"changed": changed, "created": created, "deleted": deleted}
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _workspace_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in EXCLUDED_NAMES for part in relative.parts):
            continue
        lowered = path.name.lower()
        if any(marker in lowered for marker in SECRET_FILENAME_MARKERS):
            continue
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError(f"Praxist remote workspace cannot contain symlinks: {relative}")
        if path.is_dir():
            continue
        if not stat.S_ISREG(mode):
            raise ValueError(f"Praxist remote workspace contains a special file: {relative}")
        files.append(path)
        if len(files) > MAX_FILES:
            raise ValueError("Praxist remote workspace contains too many files")
    return files


def _safe_extract(data: bytes, destination: Path) -> None:
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
            members = archive.getmembers()
            if len(members) > MAX_FILES:
                raise ValueError("Praxist remote workspace result contains too many entries")
            seen: set[Path] = set()
            for member in members:
                relative = _safe_relative(member.name)
                if relative in seen:
                    raise ValueError(
                        f"Praxist remote workspace result contains a duplicate path: {member.name}"
                    )
                seen.add(relative)
                if not member.isfile() or member.issym() or member.islnk() or member.isdev():
                    raise ValueError(
                        f"Praxist remote workspace result has a forbidden entry: {member.name}"
                    )
                total += member.size
                if total > MAX_BYTES:
                    raise ValueError("Praxist remote workspace result expands beyond its limit")
                output = (destination / relative).resolve()
                if not output.is_relative_to(destination.resolve()):
                    raise ValueError(f"Praxist remote workspace result escaped: {member.name}")
            for member in members:
                relative = _safe_relative(member.name)
                output = destination / relative
                output.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(
                        f"Praxist remote workspace result is unreadable: {member.name}"
                    )
                member_data = source.read()
                _reject_secret_content(member.name, member_data)
                output.write_bytes(member_data)
                os.chmod(output, 0o755 if member.mode & 0o111 else 0o644)
    except tarfile.TarError as exc:
        raise ValueError("Praxist remote workspace result is not a valid tar") from exc


def _safe_relative(value: str) -> Path:
    if not value or "\\" in value:
        raise ValueError("Praxist remote workspace result path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Praxist remote workspace result path is unsafe: {value}")
    return Path(*path.parts)


def _reject_secret_content(relative: str, data: bytes) -> None:
    if any(marker in data for marker in SECRET_CONTENT_MARKERS):
        raise ValueError(f"Praxist remote workspace contains credential material: {relative}")


def _remove_empty_directories(root: Path) -> None:
    for path in sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True):
        if path.name in EXCLUDED_NAMES:
            continue
        with suppress(OSError):
            path.rmdir()
