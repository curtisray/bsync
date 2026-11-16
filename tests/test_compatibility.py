import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from rich.console import Console

import port
from port_sessions import Session


class CompatibilityTests(unittest.TestCase):
    def test_history_defaults_and_legacy_override(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            home = Path(directory)
            with patch("port.Path.home", return_value=home):
                self.assertEqual(port.history_path(), home / ".port_history.db")
                (home / ".bsync_history.db").touch()
                self.assertEqual(port.history_path(), home / ".bsync_history.db")
                (home / ".port_history.db").touch()
                self.assertEqual(port.history_path(), home / ".port_history.db")
                os.environ["BSYNC_DB"] = str(home / "legacy-override.db")
                self.assertEqual(port.history_path(), home / "legacy-override.db")
                os.environ["PORT_DB"] = str(home / "new-override.db")
                self.assertEqual(port.history_path(), home / "new-override.db")

    def test_old_module_command_still_runs(self):
        result = subprocess.run([sys.executable, "-m", "bsync", "--version"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(port.__version__, result.stdout)

    def test_legacy_rsync_override_and_new_precedence(self):
        completed = subprocess.CompletedProcess([], 0, "rsync version 3.4.1", "")
        with patch.dict(os.environ, {"BSYNC_RSYNC": "/legacy/rsync"}, clear=True), \
                patch("port.subprocess.run", return_value=completed) as run:
            self.assertEqual(port.find_rsync(), "/legacy/rsync")
            os.environ["PORT_RSYNC"] = "/current/rsync"
            self.assertEqual(port.find_rsync(), "/current/rsync")
            self.assertEqual(run.call_args.args[0][0], "/current/rsync")

    def test_legacy_session_paths_resume_and_undo(self):
        try:
            port.find_rsync()
        except RuntimeError as exc:
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, destination = root / "clip.mov", root / "destination"
            source.write_bytes(b"original video")
            destination.mkdir()
            plan = Session.plan([source], [destination], "move")
            plan["sources"][0]["vault"] = str(root / ".bsync-legacy-clip.mov")
            plan["destinations"][0]["root"] = str(destination / "bsync-legacy-session")
            history = port.History(root / ".bsync_history.db")
            try:
                original = Session.create(history, plan)
                restored = Session.load(history, original.id)
                self.assertEqual(restored.run(Console(file=io.StringIO())), 0)
                self.assertFalse(source.exists())
                self.assertTrue(Path(plan["sources"][0]["vault"]).exists())
                restored.undo()
                self.assertEqual(source.read_bytes(), b"original video")
                self.assertEqual(list(destination.iterdir()), [])
            finally:
                history.close()


if __name__ == "__main__":
    unittest.main()
