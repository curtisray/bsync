"""A monitored rsync runner for macOS and other POSIX systems."""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import sqlite3
import subprocess
import time

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

__version__ = "0.2.1"

def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class History:
    """Serialize transfers, while permitting read-only inspection during a run."""

    def __init__(self, path: Path):
        self.lock = None
        self.db = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.lock = open(str(path) + ".lock", "a+")
            try:
                fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.owns_lock = True
            except BlockingIOError:
                self.owns_lock = False
            self.db = sqlite3.connect(path, timeout=10)
            self.db.execute("""CREATE TABLE IF NOT EXISTS transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                src TEXT NOT NULL, dst TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('RUNNING','COMPLETED','CUTOFF')),
                bytes_done INTEGER NOT NULL DEFAULT 0,
                pct REAL NOT NULL DEFAULT 0,
                started_at TEXT NOT NULL, ended_at TEXT
            )""")
            self.db.row_factory = sqlite3.Row
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(transfers)")}
            if "session_id" not in columns:
                self.db.execute("ALTER TABLE transfers ADD COLUMN session_id TEXT")
            self.db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, plan TEXT NOT NULL, state TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )""")
            if self.owns_lock:
                self.db.execute("UPDATE transfers SET status='CUTOFF', ended_at=? "
                                "WHERE status='RUNNING'", (now(),))
                self.db.execute("UPDATE sessions SET status='CUTOFF', updated_at=? "
                                "WHERE status IN ('RUNNING','PREPARING','MOVING')", (now(),))
            self.db.commit()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
        if self.lock is not None:
            self.lock.close()

    def start(self, src: str, dst: str, session_id: str | None = None) -> int:
        if not self.owns_lock:
            raise RuntimeError("Another bsync transfer is running with this history database.")
        with self.db:
            cursor = self.db.execute(
                "INSERT INTO transfers(src,dst,status,started_at,session_id) VALUES (?,?,'RUNNING',?,?)",
                (src, dst, now(), session_id),
            )
        return cursor.lastrowid

    def update(self, job_id: int, state: Snapshot, status: str = "RUNNING"):
        with self.db:
            self.db.execute(
                "UPDATE transfers SET bytes_done=?,pct=?,status=?,ended_at=? WHERE id=?",
                (state.bytes_done, state.pct, status,
                 None if status == "RUNNING" else now(), job_id),
            )

    def get(self, job_id: int):
        return self.db.execute("SELECT * FROM transfers WHERE id=?", (job_id,)).fetchone()

    def recent(self, cutoff: bool = False):
        return self.db.execute(
            "SELECT * FROM transfers " + ("WHERE status='CUTOFF' " if cutoff else "")
            + "ORDER BY id DESC LIMIT 20"
        ).fetchall()


@dataclass
class Snapshot:
    bytes_done: int = 0
    pct: float = 0
    speed: str = "—"
    eta: str = "—"
    filename: str = "Scanning files…"


PROGRESS = re.compile(
    r"^\s*([\d,]+(?:\.\d+)?)([kKMGTPE]?)(?:i?B)?\s+"
    r"(\d{1,3})%\s+(\S+/s)\s+(\d+:\d{2}:\d{2})(.*)$"
)


def parse_record(record: str, state: Snapshot) -> bool:
    if record.startswith("FILE:"):
        state.filename = record[5:]
        return True
    match = PROGRESS.match(record)
    if not match:
        return False
    amount, unit, pct, speed, eta, suffix = match.groups()
    power = "KMGTPE".find(unit.upper()) + 1 if unit else 0
    state.bytes_done = int(float(amount.replace(",", "")) * 1000 ** power)
    state.pct = min(100, int(pct))
    state.speed = speed
    # A completed-file update reports elapsed time in the same field as ETA.
    state.eta = "—" if "xfr#" in suffix else eta
    return True


def endpoint(value: str) -> str:
    # Preserve remote-shell and daemon endpoints, including rsync:// URLs.
    if re.match(r"^[^/]+:", value):
        return value
    suffix = "/" if value.endswith("/") else ""
    return os.path.abspath(os.path.expanduser(value)).rstrip("/") + suffix or "/"


def find_rsync() -> str:
    candidate = os.environ.get("BSYNC_RSYNC")
    if not candidate:
        prefixes = ("/opt/homebrew", "/usr/local", "/home/linuxbrew/.linuxbrew")
        candidate = next((str(Path(prefix) / "opt/rsync/bin/rsync") for prefix in prefixes
                          if (Path(prefix) / "opt/rsync/bin/rsync").is_file()), None)
        candidate = candidate or shutil.which("rsync")
    if not candidate:
        raise RuntimeError("Modern rsync is required. On macOS: brew install rsync")
    result = subprocess.run([candidate, "--version"], capture_output=True, text=True, timeout=10)
    match = re.search(r"\bversion\s+(\d+)\.(\d+)", result.stdout)
    if result.returncode or not match or tuple(map(int, match.groups())) < (3, 1):
        raise RuntimeError("rsync 3.1+ is required. Install with 'brew install rsync'; "
                           "set BSYNC_RSYNC to its executable if needed.")
    return candidate


@contextmanager
def interrupt_handlers():
    received = []
    previous = {}
    def handler(signum, frame):
        received.append(signum)
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, handler)
        yield received
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def stop_process(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGCONT)
    except ProcessLookupError:
        return
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=2)
            return
        except subprocess.TimeoutExpired:
            pass


def run_transfer(history: History, src: str, dst: str, console: Console, *,
                 session_id: str | None = None, context: str = "", keyboard=None) -> int:
    from bsync_ui import FlowView, Keyboard
    binary = find_rsync()
    src, dst = endpoint(src), endpoint(dst)
    state = Snapshot()
    diagnostics = deque(maxlen=12)
    process = None
    job_id = None
    result = 1
    undo_requested = False
    paused = False
    with interrupt_handlers() as interrupted:
        try:
            job_id = history.start(src, dst, session_id)
            console.print(f"Transfer #{job_id}: {src} → {dst}", markup=False)
            process = subprocess.Popen(
                [binary, "-avh", "--partial", "--info=progress2", "--no-i-r",
                 "--out-format=FILE:%n", "--outbuf=L", "--", src, dst],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env={**os.environ, "LC_ALL": "C"}, start_new_session=True,
            )
            pending = b""
            saved = time.monotonic()
            def consume(record):
                line = record.decode("utf-8", errors="replace")
                if not parse_record(line, state) and line.strip():
                    diagnostics.append(line)

            view = FlowView(state, src, dst, job_id, context=context, undo=bool(session_id))
            controls = keyboard if keyboard is not None else Keyboard(console.is_terminal)
            with selectors.DefaultSelector() as selector, controls as keys, Live(
                view, console=console, screen=console.is_terminal,
                refresh_per_second=8, auto_refresh=console.is_terminal,
            ) as live:
                selector.register(process.stdout, selectors.EVENT_READ)
                eof = False
                while not eof:
                    key = keys.poll().lower()
                    if key in ("p", " ", "r") and process.poll() is None:
                        target = False if key == "r" else not paused
                        if target != paused:
                            try:
                                os.killpg(process.pid, signal.SIGSTOP if target else signal.SIGCONT)
                                paused = target
                                view.paused = paused
                            except ProcessLookupError:
                                pass
                    elif key == "u" and session_id:
                        undo_requested = True
                        interrupted.append(signal.SIGINT)
                    elif key == "q":
                        interrupted.append(signal.SIGINT)
                    if interrupted:
                        break
                    for ready, _ in selector.select(timeout=0.1):
                        chunk = os.read(ready.fileobj.fileno(), 65536)
                        if not chunk:
                            eof = True
                            break
                        records = re.split(rb"[\r\n]", pending + chunk)
                        pending = records.pop()
                        for record in records:
                            consume(record)
                    if time.monotonic() - saved >= 0.5:
                        history.update(job_id, state)
                        saved = time.monotonic()
                if pending:
                    consume(pending)
                if interrupted:
                    stop_process(process)
                    result = 125 if undo_requested else 128 + interrupted[0]
                else:
                    while process.poll() is None and not interrupted:
                        time.sleep(0.05)
                    if interrupted:
                        stop_process(process)
                        result = 128 + interrupted[0]
                    else:
                        result = process.returncode
                if result == 0:
                    state.pct = 100
                    view.finished = True
                view.paused = False
                live.update(view, refresh=True)
        finally:
            if process is not None:
                stop_process(process)
                if process.stdout is not None:
                    process.stdout.close()
            if job_id is not None:
                if result == 0:
                    state.pct = 100
                history.update(job_id, state, "COMPLETED" if result == 0 else "CUTOFF")
    if result == 0:
        console.print(f"Transfer #{job_id} completed.", style="green")
    else:
        console.print(f"Transfer #{job_id} cut off (exit {result}). Resume: bsync resume {job_id}", style="yellow")
        for line in diagnostics:
            console.print(line, markup=False, highlight=False)
    return result if result >= 0 else 128 - result


def resume_job(history, job_id, console):
    row = history.get(job_id)
    if row is None:
        raise RuntimeError(f"Transfer #{job_id} does not exist.")
    if row["status"] != "CUTOFF":
        raise RuntimeError(f"Transfer #{job_id} is {row['status']}; only CUTOFF jobs can resume.")
    if row["session_id"]:
        from bsync_sessions import Session
        return Session.load(history, row["session_id"]).run(console)
    return run_transfer(history, row["src"], row["dst"], console)


def show_history(history: History, console: Console, cutoff: bool = False):
    table = Table(title="Cut-off transfers (last 20)" if cutoff else "Transfer history (last 20)")
    for name in ("ID", "Source", "Destination", "Status", "Bytes", "%", "Started (UTC)", "Ended (UTC)"):
        table.add_column(name)
    rows = history.recent(cutoff)
    for row in rows:
        table.add_row(*(Text(str(value)) for value in (
            row["id"], row["src"], row["dst"], row["status"], f'{row["bytes_done"]:,}',
            f'{row["pct"]:g}', row["started_at"], row["ended_at"] or "—",
        )))
    console.print(table)
    if not rows:
        console.print("No transfers found.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")
    run = sub.add_parser("run", help="Start a monitored transfer")
    run.add_argument("src")
    run.add_argument("dst")
    sub.add_parser("history", help="Show the last 20 transfers")
    sub.add_parser("cutoff", help="Show the last 20 interrupted transfers")
    resume = sub.add_parser("resume", help="Retry a transfer using its saved endpoints")
    resume.add_argument("id", type=int)
    args = parser.parse_args(argv)
    console = Console()
    history = None
    try:
        history = History(Path(os.environ.get("BSYNC_DB", "~/.bsync_history.db")).expanduser())
        if args.command is None:
            import sys
            if not console.is_terminal or not sys.stdin.isatty():
                parser.print_help()
                console.print("Run bsync in an interactive terminal to open the guided setup.")
                return 1
            from bsync_ui import launcher
            return launcher(history, console)
        if args.command in ("history", "cutoff"):
            show_history(history, console, args.command == "cutoff")
            return 0
        if args.command == "resume":
            return resume_job(history, args.id, console)
        return run_transfer(history, args.src, args.dst, console)
    except (OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        console.print(f"bsync: {exc}", style="red", markup=False)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if history is not None:
            history.close()


if __name__ == "__main__":
    raise SystemExit(main())
