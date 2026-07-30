from __future__ import annotations

import os
import signal
import sys
import tempfile
import time
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace

from scripts.restart_gateway_safely import (
    _run,
    launch_detached,
    log_has_marker_since,
    parse_pid_text,
    pid_alive,
)


class RestartGatewaySafelyTests(unittest.TestCase):
    def test_parse_pid_text_supports_plain_and_json_formats(self):
        self.assertEqual(parse_pid_text("123\n"), 123)
        self.assertEqual(parse_pid_text('{"pid": 456, "argv": ["gateway"]}'), 456)
        self.assertIsNone(parse_pid_text(""))
        self.assertIsNone(parse_pid_text("not-json"))

    def test_pid_alive_rejects_missing_process(self):
        self.assertFalse(pid_alive(None))
        self.assertFalse(pid_alive(-1))

    def test_detached_process_writes_log_and_starts_new_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log_path = root / "gateway.log"
            process = launch_detached(
                [
                    sys.executable,
                    "-c",
                    (
                        "import time; "
                        "print('ClickClack connected as @test', flush=True); "
                        "time.sleep(0.4)"
                    ),
                ],
                log_path=log_path,
                cwd=root,
            )
            try:
                self.assertEqual(os.getsid(process.pid), process.pid)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if log_has_marker_since(log_path, 0):
                        break
                    time.sleep(0.05)
                self.assertTrue(log_has_marker_since(log_path, 0))
            finally:
                process.wait(timeout=2)

    def test_run_passes_hermes_home_and_keeps_gateway_detached(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_hermes = root / "hermes"
            fake_hermes.write_text(
                (
                    f"#!{sys.executable}\n"
                    "import os, pathlib, time\n"
                    "home = pathlib.Path(os.environ['HERMES_HOME'])\n"
                    "(home / 'gateway.pid').write_text(str(os.getpid()))\n"
                    "print('ClickClack connected as @test', flush=True)\n"
                    "time.sleep(5)\n"
                ),
                encoding="utf-8",
            )
            fake_hermes.chmod(0o755)
            args = SimpleNamespace(
                hermes_bin=str(fake_hermes),
                hermes_home=str(root),
                wait_seconds=2,
                poll_seconds=0.05,
                log_file=str(root / "gateway.log"),
                check_only=False,
            )

            with warnings.catch_warnings():
                warnings.simplefilter("ignore", ResourceWarning)
                self.assertEqual(_run(args), 0)
            pid = parse_pid_text((root / "gateway.pid").read_text())
            self.assertTrue(pid_alive(pid))
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)


if __name__ == "__main__":
    unittest.main()
