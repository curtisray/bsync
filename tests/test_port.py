import io
from contextlib import closing
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from rich.console import Console

import port


class ParserTests(unittest.TestCase):
    def test_progress_and_filename(self):
        state = port.Snapshot()
        self.assertTrue(port.parse_record("FILE:clips/[red] café.mov", state))
        self.assertEqual(state.filename, "clips/[red] café.mov")
        self.assertTrue(port.parse_record(" 1,234,567  42%  12.34MB/s  0:01:23", state))
        self.assertEqual((state.bytes_done, state.pct, state.eta), (1234567, 42, "0:01:23"))
        port.parse_record(" 200.12G 100% 80.00MB/s 0:12:00 (xfr#1, to-chk=0/2)", state)
        self.assertEqual(state.bytes_done, 200120000000)
        self.assertEqual(state.eta, "—")
        self.assertFalse(port.parse_record("sent 42 bytes", state))

    def test_endpoints(self):
        self.assertEqual(port.endpoint("./clips/"), os.path.abspath("clips") + "/")
        self.assertEqual(port.endpoint("/"), "/")
        for endpoint in ("user@host:clips/", "rsync://host/module/", "[::1]:/clips/"):
            self.assertEqual(port.endpoint(endpoint), endpoint)


class HistoryTests(unittest.TestCase):
    def test_active_lock_and_crash_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            first = port.History(path)
            job_id = first.start("src", "dst")
            first.update(job_id, port.Snapshot(bytes_done=123, pct=12))
            observer = port.History(path)
            try:
                self.assertEqual(observer.get(job_id)["status"], "RUNNING")
                with self.assertRaises(RuntimeError):
                    observer.start("a", "b")
            finally:
                observer.close()
                first.close()
            recovered = port.History(path)
            try:
                row = recovered.get(job_id)
                self.assertEqual((row["status"], row["bytes_done"], row["pct"]), ("CUTOFF", 123, 12))
                self.assertIsNotNone(row["ended_at"])
                for _ in range(22):
                    current = recovered.start("a", "b")
                    recovered.update(current, port.Snapshot(), "COMPLETED")
                self.assertEqual(len(recovered.recent()), 20)
                self.assertEqual(len(recovered.recent(True)), 1)
            finally:
                recovered.close()


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.db = self.root / "history.db"
        self.fake = self.root / "rsync"
        self.fake.write_text(
            f"#!{sys.executable}\n"
            "import os, sys, time\n"
            "if '--version' in sys.argv:\n"
            "    print('rsync  version 3.4.1  protocol version 32'); sys.exit(0)\n"
            "sys.stdout.write('FILE:clip [red].mov\\n 1,024 50% 1.00MB/s 0:00:01\\r'); sys.stdout.flush()\n"
            "if os.environ.get('FAKE_WAIT'):\n"
            "    time.sleep(30)\n"
            "sys.stdout.write(' 2,048 100% 1.00MB/s 0:00:01 (xfr#1, to-chk=0/1)'); sys.stdout.flush()\n"
            "if os.environ.get('FAKE_FAIL'):\n"
            "    print('\\nrsync: permission denied', file=sys.stderr); sys.exit(23)\n"
        )
        self.fake.chmod(0o755)
        self.env = {**os.environ, "PORT_DB": str(self.db), "PORT_RSYNC": str(self.fake)}

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, *args, **env):
        return subprocess.run([sys.executable, "-m", "port", *args],
                              env={**self.env, **env}, capture_output=True, text=True, timeout=10)

    def row(self, job=1):
        with closing(sqlite3.connect(self.db)) as db:
            db.row_factory = sqlite3.Row
            return db.execute("SELECT * FROM transfers WHERE id=?", (job,)).fetchone()

    def test_success_and_failure_resume(self):
        failure = self.cli("run", "./source/", "./destination/", FAKE_FAIL="1")
        self.assertEqual(failure.returncode, 23, failure.stderr)
        self.assertIn("permission denied", failure.stdout)
        self.assertEqual(self.row()["status"], "CUTOFF")
        result = self.cli("resume", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.row(2)["status"], "COMPLETED")
        self.assertEqual(self.row(2)["bytes_done"], 2048)
        self.assertEqual(self.row()["status"], "CUTOFF")
        self.assertEqual(self.cli("resume", "2").returncode, 1)
        self.assertEqual(self.cli("resume", "999").returncode, 1)
        self.assertIn("CUTOFF", self.cli("cutoff").stdout)
        self.assertIn("COMPLETED", self.cli("history").stdout)

    def test_signals_checkpoint_and_stop_child(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=sig):
                process = subprocess.Popen([sys.executable, "-m", "port", "run", "a", "b"],
                                           env={**self.env, "FAKE_WAIT": "1"},
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        try:
                            with closing(sqlite3.connect(self.db)) as db:
                                row = db.execute("SELECT bytes_done FROM transfers WHERE status='RUNNING' ORDER BY id DESC LIMIT 1").fetchone()
                            if row and row[0] == 1024:
                                break
                        except sqlite3.OperationalError:
                            pass
                        time.sleep(0.05)
                    else:
                        self.fail("No progress checkpoint")
                    process.send_signal(sig)
                    stdout, stderr = process.communicate(timeout=8)
                    self.assertEqual(process.returncode, 128 + sig, stderr)
                    with closing(sqlite3.connect(self.db)) as db:
                        row = db.execute("SELECT status, bytes_done FROM transfers ORDER BY id DESC LIMIT 1").fetchone()
                    self.assertEqual(row, ("CUTOFF", 1024))
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()

    def test_spawn_failure_marks_cutoff(self):
        history = port.History(self.db)
        try:
            with patch("port.find_rsync", return_value="/nonexistent/rsync"):
                with self.assertRaises(OSError):
                    port.run_transfer(history, "a", "b", Console(file=io.StringIO()))
            self.assertEqual(history.get(1)["status"], "CUTOFF")
        finally:
            history.close()

    def test_pause_resume_then_stop_while_paused(self):
        class ScriptedKeys:
            def __enter__(self):
                self.started = time.monotonic()
                self.events = [(0.2, "p"), (0.5, "r"), (0.7, "p"), (0.9, "q")]
                return self
            def __exit__(self, *args):
                pass
            def poll(self):
                if self.events and time.monotonic() - self.started >= self.events[0][0]:
                    return self.events.pop(0)[1]
                return ""
        history = port.History(self.db)
        try:
            with patch.dict(os.environ, {**self.env, "FAKE_WAIT": "1"}), \
                    patch("port.os.killpg", wraps=os.killpg) as signals:
                result = port.run_transfer(history, "a", "b", Console(file=io.StringIO()),
                                            keyboard=ScriptedKeys())
            self.assertEqual(result, 130)
            sent = [call.args[1] for call in signals.call_args_list]
            self.assertEqual(sent[:3], [signal.SIGSTOP, signal.SIGCONT, signal.SIGSTOP])
            self.assertEqual(sent[3:5], [signal.SIGCONT, signal.SIGINT])
            self.assertEqual(history.get(1)["status"], "CUTOFF")
        finally:
            history.close()


class RealRsyncTests(unittest.TestCase):
    def test_local_copy_and_noop(self):
        try:
            binary = port.find_rsync()
        except RuntimeError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            src, dst = root / "source", root / "destination"
            src.mkdir()
            (src / "clip [red] café.mov").write_bytes(os.urandom(2 * 1024 * 1024))
            (src / "empty").touch()
            (src / "link").symlink_to("empty")
            history = port.History(root / "history.db")
            try:
                with patch.dict(os.environ, {"PORT_RSYNC": binary}):
                    for _ in range(2):
                        self.assertEqual(port.run_transfer(history, str(src) + "/", str(dst) + "/",
                                                           Console(file=io.StringIO())), 0)
                self.assertEqual((src / "clip [red] café.mov").read_bytes(),
                                 (dst / "clip [red] café.mov").read_bytes())
                self.assertTrue((dst / "link").is_symlink())
                self.assertEqual(history.get(1)["status"], "COMPLETED")
                # -h rounds modern rsync's large byte counts (e.g. 2.10M).
                self.assertAlmostEqual(history.get(1)["bytes_done"], 2097152, delta=10000)
                with patch.dict(os.environ, {"PORT_RSYNC": binary}):
                    self.assertNotEqual(port.run_transfer(history, str(root / "missing"), str(dst),
                                                          Console(file=io.StringIO())), 0)
                self.assertEqual(history.get(3)["status"], "CUTOFF")
            finally:
                history.close()


if __name__ == "__main__":
    unittest.main()
