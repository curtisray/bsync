"""Rich terminal screens and small POSIX keyboard controls."""
import os
import select
import sys
import termios
import time
import tty

from rich import box
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text


class Keyboard:
    def __init__(self, enabled=True):
        self.enabled = enabled and sys.stdin.isatty()
        self.fd = None
        self.buffer = ""

    def __enter__(self):
        if self.enabled:
            self.fd = sys.stdin.fileno()
            self.previous = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.previous)

    def poll(self):
        if self.fd is None:
            return ""
        if select.select([self.fd], [], [], 0)[0]:
            self.buffer += os.read(self.fd, 64).decode(errors="ignore")
        if not self.buffer:
            return ""
        for sequence in ("\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D"):
            if self.buffer.startswith(sequence):
                self.buffer = self.buffer[len(sequence):]
                return sequence
        value, self.buffer = self.buffer[0], self.buffer[1:]
        return value


def tank(label, path, fraction, caption, color, width, rows=7, compact=False):
    cells = max(8, width - 6)
    filled = round(max(0, min(1, fraction)) * rows * cells)
    water = Text()
    for row in range(rows):
        count = max(0, min(cells, filled - (rows - row - 1) * cells))
        water.append("█" * count, style=color)
        water.append("░" * (cells - count), style="bright_black")
        if row != rows - 1:
            water.append("\n")
    content = [Text(path, style="dim", overflow="ellipsis", no_wrap=True)]
    if not compact:
        content.append(Text(""))
    content.append(water)
    if not compact:
        content.append(Text(""))
    content.append(Text(caption, style=color))
    return Panel(Group(*content),
                 title=label, border_style=color, box=box.ROUNDED, width=width)


class FlowView:
    def __init__(self, state, src, dst, job_id, *, context="", undo=False):
        self.state, self.src, self.dst, self.job_id = state, src, dst, job_id
        self.context, self.undo = context, undo
        self.paused = False
        self.finished = False

    def __rich_console__(self, console, options):
        width = options.max_width
        pct = max(0, min(100, self.state.pct))
        status = "COMPLETED" if self.finished else "PAUSED" if self.paused else "TRANSFERRING"
        yield Text.assemble(("bsync ", "bold cyan"), ("/ data in motion", "dim"))
        yield Text(f"\n{status}  ·  Transfer #{self.job_id}  {self.context}", style="bold")
        if width >= 72:
            size = max(20, (width - 11) // 2)
            grid = Table.grid(padding=(0, 1))
            phase = int(time.monotonic() * 5) % 3
            arrow = "  ‖  " if self.paused else (" ▸ · ", " · ▸ ", " ─ ▸ ")[phase]
            grid.add_row(tank("SOURCE", self.src, 1 - pct / 100,
                              f"{100-pct:.0f}% remaining to sync", "cyan", size),
                         Text(arrow, style="bold cyan"),
                         tank("DESTINATION", self.dst, pct / 100,
                              f"{pct:.0f}% synced", "green", size))
            yield grid
        else:
            yield tank("SOURCE", self.src, 1 - pct / 100,
                       f"{100-pct:.0f}% remaining to sync", "cyan", width, rows=2, compact=True)
            yield Text("                   ↓", style="cyan")
            yield tank("DESTINATION", self.dst, pct / 100,
                       f"{pct:.0f}% synced", "green", width, rows=2, compact=True)
        yield Text(f"\n{pct:.0f}%  ·  {self.state.bytes_done:,} bytes  ·  {self.state.speed}  ·  ETA {self.state.eta}")
        yield Text.assemble(("Current file  ", "dim"), self.state.filename)
        yield Text("\nContainers show sync progress; copying keeps the source files.", style="dim")
        controls = "[P/Space] Pause/resume  [R] Resume  [Q] Stop"
        if self.undo:
            controls += "  [U] Undo…"
        yield Text("\n" + controls, style="bold")


def choose(console, title, description, choices):
    """Choices are (label, description, value), navigable by arrows or number."""
    index = 0
    rendered = None
    with Keyboard() as keys, Live(console=console, screen=True, auto_refresh=False) as live:
        while True:
            items = [Text("bsync", style="bold cyan"), Text(description, style="dim"), Text("")]
            for number, (label, help_text, _) in enumerate(choices):
                selected = number == index
                items.append(Text(f"{'›' if selected else ' '} {number+1}. {label}",
                                  style="bold cyan" if selected else "bold"))
                items.append(Text(f"     {help_text}\n", style="dim"))
            items.append(Text("↑ ↓ Navigate   Enter Select   Number Jump   Esc Back", style="dim"))
            if rendered != index:
                live.update(Panel(Group(*items), title=title, border_style="cyan"), refresh=True)
                rendered = index
            key = keys.poll()
            if "\x1b[A" in key or key == "k":
                index = (index - 1) % len(choices)
            elif "\x1b[B" in key or key == "j":
                index = (index + 1) % len(choices)
            elif key in ("\r", "\n"):
                return choices[index][2]
            elif key.isdigit() and 1 <= int(key) <= len(choices):
                index = int(key) - 1
            elif key in ("\x1b", "q"):
                return None
            time.sleep(0.04)


def heading(console, step, message):
    console.clear()
    console.print(Panel(Text(message), title=f"bsync / {step}", border_style="cyan"))


def ask_paths(console, title, source=False, multiple=True):
    from pathlib import Path
    import shlex
    paths = []
    while True:
        raw = Prompt.ask(title + (" (blank to finish)" if paths else ""), console=console, default="").strip()
        if not raw:
            if paths:
                return paths
            console.print("Enter at least one location, or press Ctrl+C to return.", style="yellow")
            continue
        # Accept a quoted or shell-escaped path pasted/dragged from Finder.
        if raw.startswith(("'", '"')) or "\\ " in raw:
            try:
                parts = shlex.split(raw)
                if len(parts) == 1:
                    raw = parts[0]
            except ValueError:
                pass
        path = Path(raw).expanduser().absolute()
        if not path.exists() or (not source and not path.is_dir()):
            console.print("Choose an existing " + ("file or folder." if source else "destination folder."), style="yellow")
            continue
        paths.append(str(path))
        console.print(Text(f"  ✓ {path}", style="green"))
        if not multiple or not Confirm.ask("Add another location?", default=False, console=console):
            return paths


def launcher(history, console):
    from bsync import show_history
    from bsync_sessions import Session
    while True:
        action = choose(console, "Welcome", "Copy or move files with a clear view of where they’re going.", [
            ("New transfer", "Guided setup · sources, destinations, and a review before starting", "run"),
            ("Resume a transfer", "Continue an interrupted guided session or rsync job", "resume"),
            ("History", "Inspect recent transfers and their status", "history"),
            ("Interrupted transfers", "Find work that stopped before completion", "cutoff"),
            ("Undo a guided transfer", "Restore moved originals and remove unchanged session copies", "undo"),
            ("Exit", "Return to your terminal", "exit"),
        ])
        if action in (None, "exit"):
            return 0
        try:
            if action == "run":
                mode = choose(console, "1 / What would you like to do?", "Choose what happens to the originals.", [
                    ("Copy", "Keep the originals and create destination copies", "copy"),
                    ("Move", "Copy first, then put originals in an undo vault; discard it later to free space", "move"),
                ])
                if mode is None:
                    continue
                heading(console, "2 / Sources", "Choose local files or folders. Folder contents stay together under the folder’s name.")
                sources = ask_paths(console, "Source path", source=True)
                many = choose(console, "3 / Distribution", "Each destination receives the full selection.", [
                    ("One destination", "Send all selected files and folders to one place", False),
                    ("Multiple destinations", "Create a complete copy at each location, one destination at a time", True),
                ])
                if many is None:
                    continue
                heading(console, "4 / Destinations", "Choose existing folders. A named bsync session folder keeps this transfer separate and reversible.")
                destinations = ask_paths(console, "Destination folder", multiple=many)
                plan = Session.plan(sources, destinations, mode)
                heading(console, "5 / Review", f"{mode.upper()} · {len(sources)} source(s) · {len(destinations)} destination(s)")
                for src in plan["sources"]:
                    console.print(Text(f"FROM  {src['path']}"))
                for dst in plan["destinations"]:
                    console.print(Text(f"INTO  {dst['root']}"))
                if mode == "move":
                    console.print("Originals go to hidden sibling undo vaults after every copy succeeds.\n"
                                  "Source disk space is released only when you discard those vaults.", style="yellow")
                console.print("Undo removes only unchanged copies inside the session folders.", style="dim")
                if Confirm.ask("Start this transfer?", default=True, console=console):
                    session = Session.create(history, plan)
                    session.run(console)
                    session_actions(session, console)
            elif action in ("history", "cutoff"):
                heading(console, action.title(), "Recent rsync jobs")
                show_history(history, console, action == "cutoff")
                show_sessions(history, console)
                Prompt.ask("Press Enter to return", default="", console=console)
            elif action == "undo":
                sid = pick_session(history, console, "undo")
                if sid and Confirm.ask("Restore originals and remove unchanged session copies?", default=False, console=console):
                    Session.load(history, sid).undo()
                    console.print("Transfer undone.", style="green")
                    Prompt.ask("Press Enter to return", default="", console=console)
            elif action == "resume":
                from bsync import resume_job
                value = pick_session(history, console, "resume")
                if value:
                    if value == "direct":
                        heading(console, "Resume a direct job", "Select a numeric ID from the interrupted job history.")
                        show_history(history, console, True)
                        job_id = Prompt.ask("Transfer ID (blank to return)", default="", console=console)
                        if job_id:
                            resume_job(history, int(job_id), console)
                    else:
                        session = Session.load(history, value)
                        session.run(console)
                        session_actions(session, console)
        except KeyboardInterrupt:
            console.print("\nReturned to menu.", style="dim")
        except (OSError, RuntimeError, ValueError) as exc:
            console.print(Text(str(exc), style="red"))
            Prompt.ask("Press Enter to return", default="", console=console)


def show_sessions(history, console):
    rows = history.db.execute("SELECT id,status,plan FROM sessions ORDER BY created_at DESC LIMIT 20").fetchall()
    table = Table(title="Guided sessions")
    for name in ("Session", "Status", "Mode"):
        table.add_column(name)
    import json
    for row in rows:
        table.add_row(row["id"], row["status"], json.loads(row["plan"])["mode"])
    console.print(table)


def pick_session(history, console, action):
    import json
    statuses = ("CUTOFF",) if action == "resume" else ("COMPLETED", "CUTOFF", "UNDOING", "PREPARING")
    rows = history.db.execute("SELECT id,status,plan FROM sessions ORDER BY created_at DESC").fetchall()
    choices = []
    for row in rows:
        if row["status"] in statuses:
            plan = json.loads(row["plan"])
            choices.append((f"{row['id']} · {row['status']}",
                            f"{plan['mode'].title()} · {len(plan['sources'])} source(s) → {len(plan['destinations'])} destination(s)",
                            row["id"]))
    if action == "resume":
        choices.append(("Resume a direct rsync job", "Look up an interrupted numeric transfer ID", "direct"))
    choices.append(("Back", "Return to the main menu", None))
    return choose(console, action.title() + " a guided session", "Choose a saved session. Esc returns to the menu.", choices)


def session_actions(session, console):
    while session.status not in ("UNDONE", "FINALIZED"):
        options = [("Return to menu", "Keep this session in history", "back")]
        if session.status == "CUTOFF":
            options.append(("Resume", "Continue all unfinished copies", "resume"))
        if session.status != "DISCARDING":
            options.append(("Undo", "Restore moved originals; remove unchanged session copies", "undo"))
        if session.status in ("COMPLETED", "DISCARDING") and session.plan_data["mode"] == "move":
            options.append(("Discard undo vaults", "Permanently release the original disk space; keep destination copies", "discard"))
        action = choose(console, f"Session {session.id} / {session.status}", "Choose what happens next.", options)
        if action in (None, "back"):
            return
        if action == "resume":
            session.run(console)
        elif action == "undo" and Confirm.ask("Undo this session?", default=False, console=console):
            session.undo()
            console.print("Transfer undone.", style="green")
        elif action == "discard" and Confirm.ask("Permanently delete the undo vaults? You cannot undo afterward.", default=False, console=console):
            session.discard()
            console.print("Undo vaults discarded. Destination copies kept.", style="green")
