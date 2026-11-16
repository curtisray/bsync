from contextlib import closing
import fcntl
import io
import os
from pathlib import Path
import pty
import select
import signal
import sqlite3
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from unittest.mock import patch

from rich.console import Console

import bsync
from bsync_sessions import Session, inventory, remove_owned
from bsync_ui import FlowView


class SessionTests(unittest.TestCase):
    def setUp(self):
        try:
            bsync.find_rsync()
        except RuntimeError as exc:
            self.skipTest(str(exc))
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "footage"
        self.source.mkdir()
        (self.source / "video.mov").write_bytes(os.urandom(65536))
        (self.source / "alias").symlink_to("video.mov")
        self.file = self.root / "audio.wav"
        self.file.write_bytes(b"sound")
        self.dest = self.root / "destination"
        self.dest.mkdir()
        self.other = self.root / "other"
        self.other.mkdir()
        self.history = bsync.History(self.root / "history.db")
        self.console = Console(file=io.StringIO(), width=100)

    def tearDown(self):
        self.history.close()
        self.temp.cleanup()

    def session(self, mode="copy", destinations=None):
        return Session.create(self.history, Session.plan([self.source, self.file],
                                                        destinations or [self.dest], mode))

    def test_copy_multiple_destinations_and_undo_after_reload(self):
        session = self.session(destinations=[self.dest, self.other])
        self.assertEqual(session.run(self.console), 0)
        for dest in session.plan_data["destinations"]:
            root = Path(dest["root"])
            self.assertEqual((root / "audio.wav").read_bytes(), b"sound")
            self.assertEqual((root / "footage/video.mov").read_bytes(), (self.source / "video.mov").read_bytes())
            self.assertTrue((root / "footage/alias").is_symlink())
        Session.load(self.history, session.id).undo()
        self.assertTrue(self.source.exists())
        self.assertTrue(self.file.exists())
        self.assertEqual(list(self.dest.iterdir()), [])
        self.assertEqual(list(self.other.iterdir()), [])

    def test_move_undo_and_discard(self):
        session = self.session("move")
        self.assertEqual(session.run(self.console), 0)
        self.assertFalse(self.source.exists())
        self.assertFalse(self.file.exists())
        self.assertTrue(all(Path(src["vault"]).exists() for src in session.plan_data["sources"]))
        session.undo()
        self.assertTrue(self.source.exists())
        self.assertEqual(self.file.read_bytes(), b"sound")
        session = self.session("move")
        session.run(self.console)
        session.discard()
        self.assertEqual(session.status, "FINALIZED")
        self.assertTrue(all(not Path(src["vault"]).exists() for src in session.plan_data["sources"]))
        with self.assertRaises(RuntimeError):
            session.undo()

    def test_undo_refuses_edited_outputs_and_conflicts(self):
        session = self.session("move")
        session.run(self.console)
        copied = Path(session.plan_data["destinations"][0]["root"]) / "audio.wav"
        copied.write_bytes(b"edited")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            session.undo()
        self.assertEqual(copied.read_bytes(), b"edited")
        self.assertTrue(Path(session.plan_data["sources"][0]["vault"]).exists())

    def test_undo_refuses_recreated_source(self):
        session = self.session("move")
        session.run(self.console)
        self.source.mkdir()
        (self.source / "new.txt").write_text("keep me")
        with self.assertRaisesRegex(RuntimeError, "occupied"):
            session.undo()
        self.assertEqual((self.source / "new.txt").read_text(), "keep me")

    def test_resume_keeps_entire_plan(self):
        session = self.session(destinations=[self.dest, self.other])
        original = bsync.run_transfer
        calls = []
        def interrupted(*args, **kwargs):
            calls.append(args)
            return original(*args, **kwargs) if len(calls) == 1 else 130
        with patch("bsync.run_transfer", side_effect=interrupted):
            self.assertEqual(session.run(self.console), 130)
        self.assertEqual(session.status, "CUTOFF")
        self.assertEqual(len(session.state["done"]), 1)
        restored = Session.load(self.history, session.id)
        self.assertEqual(restored.run(self.console), 0)
        self.assertEqual(len(restored.state["done"]), 4)

    def test_changed_source_and_unsafe_plans_refused(self):
        session = self.session()
        self.file.write_bytes(b"changed source")
        with self.assertRaisesRegex(RuntimeError, "Source changed"):
            session.run(self.console)
        for sources, destinations in (([self.source], [self.source]),
                                      ([self.source, self.source / "video.mov"], [self.dest]),
                                      ([self.source, self.source], [self.dest]),
                                      ([Path("/")], [self.dest])):
            with self.assertRaises(ValueError):
                Session.plan(sources, destinations, "copy")

    def test_removal_never_follows_replaced_symlink(self):
        owned = self.dest / "owned"
        owned.mkdir()
        item = owned / "file"
        item.write_text("copy")
        snapshot = inventory(owned)
        item.unlink()
        item.symlink_to(self.file)
        with self.assertRaisesRegex(RuntimeError, "changed"):
            remove_owned(owned, snapshot)
        self.assertEqual(self.file.read_bytes(), b"sound")

    def test_interrupted_undo_can_continue(self):
        session = self.session("move")
        session.run(self.console)
        session.save("UNDOING")
        source = session.plan_data["sources"][0]
        os.rename(source["vault"], source["path"])
        copied = Path(session.plan_data["destinations"][0]["root"]) / "audio.wav"
        copied.unlink()
        Session.load(self.history, session.id).undo()
        self.assertTrue(self.source.exists())
        self.assertTrue(self.file.exists())
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_session_crash_recovery_and_schema_migration(self):
        session = self.session()
        session.save("RUNNING")
        self.history.close()
        self.history = bsync.History(self.root / "history.db")
        recovered = Session.load(self.history, session.id)
        self.assertEqual(recovered.status, "CUTOFF")
        self.assertEqual(recovered.run(self.console), 0)
        legacy_path = self.root / "legacy.db"
        with closing(sqlite3.connect(legacy_path)) as db:
            db.execute("""CREATE TABLE transfers (id INTEGER PRIMARY KEY, src TEXT, dst TEXT,
                          status TEXT, bytes_done INTEGER, pct REAL, started_at TEXT, ended_at TEXT)""")
            db.execute("INSERT INTO transfers VALUES (1,'a','b','RUNNING',1024,50,'2026-01-01',NULL)")
            db.commit()
        migrated = bsync.History(legacy_path)
        try:
            self.assertIsNone(migrated.get(1)["session_id"])
            self.assertEqual(migrated.get(1)["bytes_done"], 1024)
            self.assertEqual(migrated.get(1)["status"], "CUTOFF")
        finally:
            migrated.close()

    def test_visual_layout_wide_and_narrow(self):
        for width in (60, 100):
            stream = io.StringIO()
            console = Console(file=stream, width=width)
            console.print(FlowView(bsync.Snapshot(pct=39), "/source", "/destination", 1, undo=True))
            output = stream.getvalue()
            self.assertIn("61% remaining", output)
            self.assertIn("39% synced", output)
            self.assertIn("[U] Undo", output)
            self.assertTrue(all(len(line) <= width for line in output.splitlines()))

    def test_full_wizard_in_terminal(self):
        # Exercise actual arrows/Enter, path prompts, review, copy, and menu exit.
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 36, 100, 0, 0))
        process = subprocess.Popen([sys.executable, "-m", "bsync"], stdin=slave, stdout=slave, stderr=slave,
                                   env={**os.environ, "TERM": "xterm-256color", "BSYNC_DB": str(self.root / "wizard.db")})
        os.close(slave)
        transcript = b""
        def wait_for(text):
            nonlocal transcript
            deadline = time.monotonic() + 10
            while text.encode() not in transcript:
                if time.monotonic() > deadline:
                    self.fail(f"Missing {text!r}; output: {transcript[-3000:]!r}")
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        transcript += os.read(master, 65536)
                    except OSError:
                        self.fail(f"Terminal closed while waiting for {text!r}")
            transcript = b""
        def send(text):
            os.write(master, text.encode())
        try:
            wait_for("New transfer")
            send("\r")
            wait_for("Copy first")
            send("\r")
            wait_for("Source path")
            send(str(self.source) + "\n")
            wait_for("Add another location?")
            send("n\n")
            wait_for("One destination")
            send("\r")
            wait_for("Destination folder")
            send(str(self.dest) + "\n")
            wait_for("Start this transfer?")
            send("y\n")
            wait_for("Return to menu")
            send("\r")
            wait_for("New transfer")
            send("5\r")
            wait_for("Choose a saved session")
            send("\r")
            wait_for("Restore originals and remove unchanged session copies?")
            send("y\n")
            wait_for("Press Enter to return")
            send("\n")
            wait_for("New transfer")
            send("q")
            deadline = time.monotonic() + 5
            while process.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        os.read(master, 65536)
                    except OSError:
                        break
            process.wait(timeout=5)
            self.assertEqual(process.returncode, 0)
            with closing(sqlite3.connect(self.root / "wizard.db")) as db:
                self.assertEqual(db.execute("SELECT status FROM sessions").fetchone()[0], "UNDONE")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)


if __name__ == "__main__":
    unittest.main()
