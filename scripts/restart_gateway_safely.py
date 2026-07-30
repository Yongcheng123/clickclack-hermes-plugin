#!/usr/bin/env python3
"""Restart Hermes Gateway without tying it to a timed terminal process.

The Hermes gateway command can remain in the foreground. Running it directly
inside an agent terminal means the terminal timeout may send SIGTERM to the
gateway. This launcher starts the supported Hermes CLI in a new session,
redirects its output to a private log, and performs bounded health checks.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

CONNECTED_MARKERS = (
    "ClickClack connected as",
    "clickclack connected",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely restart Hermes Gateway in a detached process session"
    )
    parser.add_argument("--hermes-bin", help="Hermes CLI path")
    parser.add_argument("--hermes-home", help="Active HERMES_HOME")
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=30,
        help="Maximum bounded health-check time (default: 30)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=1,
        help="Health-check interval (default: 1)",
    )
    parser.add_argument("--log-file", help="Private detached gateway log path")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Check an existing gateway without launching another process",
    )
    return parser


def resolve_hermes_binary(explicit: str | None = None) -> str:
    candidates = (
        explicit,
        shutil.which("hermes"),
        "/opt/hermes/.venv/bin/hermes",
    )
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    raise FileNotFoundError(
        "Hermes CLI not found; pass --hermes-bin or install it in PATH"
    )


def parse_pid_text(text: str) -> int | None:
    value = text.strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict):
        for key in ("pid", "process_pid", "gateway_pid"):
            pid = payload.get(key)
            if isinstance(pid, int) and pid > 0:
                return pid
    return None


def read_gateway_pid(hermes_home: Path) -> int | None:
    path = hermes_home / "gateway.pid"
    try:
        return parse_pid_text(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def log_has_marker_since(
    path: Path,
    offset: int,
    markers: tuple[str, ...] = CONNECTED_MARKERS,
) -> bool:
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            text = handle.read(256 * 1024).decode("utf-8", errors="replace")
    except OSError:
        return False
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)


def launch_detached(
    command: list[str],
    *,
    log_path: Path,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.Popen[bytes]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        log_path,
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o600,
    )
    try:
        return subprocess.Popen(  # noqa: S603
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=descriptor,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=env,
        )
    finally:
        os.close(descriptor)


def _run(args: argparse.Namespace) -> int:
    hermes_home = Path(
        args.hermes_home or os.getenv("HERMES_HOME") or "~/.hermes"
    ).expanduser()
    hermes_home.mkdir(parents=True, exist_ok=True)
    log_path = Path(
        args.log_file or hermes_home / "logs" / "gateway-workshop.log"
    ).expanduser()
    existing_size = log_path.stat().st_size if log_path.exists() else 0

    launcher: subprocess.Popen[bytes] | None = None
    if not args.check_only:
        try:
            hermes = resolve_hermes_binary(args.hermes_bin)
        except FileNotFoundError as exc:
            print(f"FAIL launch: {exc}")
            return 2
        launcher = launch_detached(
            [hermes, "gateway", "restart"],
            log_path=log_path,
            cwd=hermes_home,
            env={**os.environ, "HERMES_HOME": str(hermes_home)},
        )
        launcher_path = hermes_home / "gateway-workshop-launcher.pid"
        launcher_path.write_text(f"{launcher.pid}\n", encoding="utf-8")
        os.chmod(launcher_path, 0o600)
        print(f"PASS launch: detached gateway launcher pid {launcher.pid}")
        print(f"INFO log: {log_path}")
    else:
        existing_size = max(0, existing_size - 256 * 1024)

    deadline = time.monotonic() + max(args.wait_seconds, 1)
    while time.monotonic() < deadline:
        gateway_pid = read_gateway_pid(hermes_home)
        launcher_alive = launcher is not None and launcher.poll() is None
        gateway_alive = pid_alive(gateway_pid)
        connected = log_has_marker_since(log_path, existing_size)

        if connected and (gateway_alive or launcher_alive):
            active_pid = gateway_pid if gateway_alive else launcher.pid
            print(f"PASS gateway: process {active_pid} remains alive")
            print("PASS clickclack: connected")
            return 0

        if (
            launcher is not None
            and launcher.poll() not in (None, 0)
            and not gateway_alive
        ):
            print(f"FAIL gateway: launcher exited with {launcher.returncode}")
            print(f"INFO inspect log: {log_path}")
            return 3
        time.sleep(max(args.poll_seconds, 0.1))

    gateway_pid = read_gateway_pid(hermes_home)
    launcher_alive = launcher is not None and launcher.poll() is None
    if pid_alive(gateway_pid) or launcher_alive:
        active_pid = gateway_pid if pid_alive(gateway_pid) else launcher.pid
        print(f"WARN gateway: process {active_pid} is alive but not yet connected")
        print(f"INFO inspect log: {log_path}")
        return 4

    print("FAIL gateway: no persistent gateway process found")
    print(f"INFO inspect log: {log_path}")
    return 5


def main() -> int:
    return _run(_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
