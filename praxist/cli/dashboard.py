"""``praxist dashboard`` — browser control room for every local run."""

from __future__ import annotations

import argparse
import math
import sys

from praxist.dashboard.server import serve_dashboard


def register(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """Register the local browser dashboard command."""
    parser = subparsers.add_parser(
        "dashboard",
        help="Open the local browser dashboard for monitoring and lifecycle control.",
        description=(
            "Serve a loopback-only browser control room for every Praxist run known to this "
            "host. The dashboard shares the status sampler and invokes only the canonical "
            "doctor, resolve, start, stop, resume, and registry-cleanup CLI paths."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Loopback address to bind (default: 127.0.0.1; non-loopback hosts are rejected).",
    )
    parser.add_argument(
        "--port",
        type=_port,
        default=8765,
        help="TCP port to bind, or 0 for an ephemeral port (default: 8765).",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the dashboard in the default local browser.",
    )
    parser.add_argument(
        "--read-only",
        action="store_true",
        help="Disable all lifecycle actions while retaining monitoring.",
    )
    parser.add_argument(
        "--sample-interval",
        type=_sample_interval,
        default=1.0,
        help="Shared host/artifact sampling interval in seconds, minimum 1 (default: 1).",
    )
    parser.add_argument(
        "--no-codex-tasks",
        action="store_true",
        help="Disable read-only discovery of Codex-hosted Praxist takeover and setup tasks.",
    )
    parser.add_argument(
        "--codex-bin",
        help=(
            "Codex executable used for read-only task discovery. By default the active desktop "
            "or system Codex is preferred before the SDK-pinned fallback."
        ),
    )
    parser.add_argument(
        "--json",
        dest="as_json",
        action="store_true",
        help="Emit one startup JSON document before serving instead of only the URL.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Write HTTP request logs to stderr (disabled by default to protect local paths).",
    )
    parser.set_defaults(func=cmd_dashboard)


def cmd_dashboard(args: argparse.Namespace) -> int:
    """Run the local foreground dashboard server."""
    try:
        return serve_dashboard(
            host=args.host,
            port=args.port,
            open_browser=not args.no_open,
            read_only=args.read_only,
            sample_interval_seconds=args.sample_interval,
            codex_tasks_enabled=not args.no_codex_tasks,
            codex_bin=args.codex_bin,
            as_json=args.as_json,
            verbose=args.verbose,
        )
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"praxist dashboard: {exc}\n")
        return 1


def _port(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if value < 0 or value > 65535:
        raise argparse.ArgumentTypeError("port must be between 0 and 65535")
    return value


def _sample_interval(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("sample interval must be a number") from exc
    if not math.isfinite(value) or value < 1.0:
        raise argparse.ArgumentTypeError("sample interval must be finite and at least 1 second")
    return value


__all__ = ["cmd_dashboard", "register"]
