"""Durable, reversible local transfer plans for the guided interface.

Every destination has its own newly allocated directory. Originals are renamed
into same-volume vaults for reversible moves, never deleted during a transfer.
"""
from datetime import datetime
import json
import os
from pathlib import Path
import stat
import uuid


def exists(path):
    return os.path.lexists(path)


def fingerprint(path):
    info = path.lstat()
    if stat.S_ISDIR(info.st_mode):
        return [info.st_dev, info.st_ino, info.st_mode]
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)):
        raise RuntimeError(f"Unsupported special file: {path}")
    return [info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns,
            os.readlink(path) if path.is_symlink() else None]


def inventory(root):
    """Do not follow symlinks. Stat identity avoids rereading 200GB of content."""
    root = Path(root)
    result = {".": fingerprint(root)}
    if root.is_dir() and not root.is_symlink():
        def visit(directory):
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    result[str(path.relative_to(root))] = fingerprint(path)
                    if entry.is_dir(follow_symlinks=False):
                        visit(path)
        visit(root)
    return result


def unchanged(root, expected, subset=False):
    if not exists(root):
        return subset
    actual = inventory(root)
    return (all(name in expected and stamp == expected[name] for name, stamp in actual.items())
            if subset else actual == expected)


def remove_owned(root, expected):
    """Remove only manifest entries, refusing additions, replacements, or edits.

    A subset is valid so an interrupted undo can continue. rmdir, rather than a
    recursive wildcard deletion, refuses directories that gained unknown files.
    """
    root = Path(root)
    if not exists(root):
        return
    if not unchanged(root, expected, subset=True):
        raise RuntimeError(f"Files changed since the transfer; left untouched: {root}")
    for name in sorted(expected, key=lambda value: (value.count(os.sep), len(value)), reverse=True):
        if name == ".":
            continue
        path = root / name
        if not exists(path):
            continue
        if fingerprint(path) != expected[name]:
            raise RuntimeError(f"File changed during undo; left untouched: {path}")
        if path.is_dir() and not path.is_symlink():
            path.rmdir()
        else:
            path.unlink()
    if fingerprint(root) != expected["."]:
        raise RuntimeError(f"Session folder replaced; left untouched: {root}")
    root.rmdir()


class Session:
    def __init__(self, history, sid, plan, state, status):
        self.history, self.id = history, sid
        self.plan_data, self.state, self.status = plan, state, status

    @staticmethod
    def plan(sources, destinations, mode):
        if mode not in ("copy", "move") or not sources or not destinations:
            raise ValueError("Choose copy or move, at least one source, and a destination.")
        sid = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
        selected = []
        for raw in sources:
            path = Path(os.path.abspath(Path(raw).expanduser()))
            # Resolve parent aliases but preserve a selected symlink itself.
            path = path.parent.resolve() / path.name
            if not exists(path) or not path.name:
                raise ValueError(f"Choose an existing file or a non-root folder: {path}")
            if any(path == prior or path in prior.parents or prior in path.parents for prior in selected):
                raise ValueError("Source selections must be distinct and cannot contain each other.")
            if path.name in [prior.name for prior in selected]:
                raise ValueError("Selected sources need different names to share a destination folder.")
            selected.append(path)
        targets = []
        for raw in destinations:
            dest = Path(raw).expanduser().resolve()
            if not dest.is_dir() or dest in targets:
                raise ValueError("Choose distinct, existing destination folders.")
            for source in selected:
                real = source.resolve()
                if dest == real or real in dest.parents:
                    raise ValueError("A destination cannot be inside a source selection.")
            targets.append(dest)
        return {
            "id": sid, "mode": mode,
            "sources": [{"path": str(path), "name": path.name,
                         "vault": str(path.parent / f".bsync-{sid}-{path.name}")}
                        for path in selected],
            "destinations": [{"path": str(dest), "root": str(dest / f"bsync-{sid}")}
                             for dest in targets],
        }

    @classmethod
    def create(cls, history, plan):
        if not history.owns_lock:
            raise RuntimeError("Another bsync transfer is active.")
        state = {"sources": {}, "outputs": {}, "done": [], "allocated": []}
        # Complete preflight before creating any output folders.
        for source in plan["sources"]:
            if exists(source["vault"]):
                raise RuntimeError(f"Undo vault already exists: {source['vault']}")
            state["sources"][source["path"]] = inventory(source["path"])
        session = cls(history, plan["id"], plan, state, "PREPARING")
        from bsync import now
        with history.db:
            history.db.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?)",
                               (session.id, json.dumps(plan), json.dumps(state), session.status, now(), now()))
        try:
            for destination in plan["destinations"]:
                root = Path(destination["root"])
                root.mkdir(mode=0o700)
                state["allocated"].append(str(root))
                state["outputs"][str(root)] = inventory(root)
                session.save()
        except BaseException:
            session.save("CUTOFF")
            raise
        session.save("CUTOFF")
        return session

    @classmethod
    def load(cls, history, sid):
        row = history.db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise RuntimeError(f"Guided session {sid} does not exist.")
        return cls(history, sid, json.loads(row["plan"]), json.loads(row["state"]), row["status"])

    def save(self, status=None):
        from bsync import now
        if status is not None:
            self.status = status
        with self.history.db:
            self.history.db.execute("UPDATE sessions SET state=?,status=?,updated_at=? WHERE id=?",
                                    (json.dumps(self.state), self.status, now(), self.id))

    def require_lock(self):
        if not self.history.owns_lock:
            raise RuntimeError("Another bsync transfer is active.")

    def checkpoint_output(self, root, source):
        current = inventory(root)
        allowed = {"."}
        for selection in self.plan_data["sources"]:
            for relative in self.state["sources"][selection["path"]]:
                allowed.add(selection["name"] if relative == "." else str(Path(selection["name"]) / relative))
        if current.keys() - allowed:
            raise RuntimeError(f"Unrelated files appeared inside the session folder. Left untouched: {root}")
        for relative, stamp in self.state["outputs"][root].items():
            # Rsync may change only the selection currently being synced.
            if relative == source["name"] or relative.startswith(source["name"] + os.sep):
                continue
            if current.get(relative) != stamp:
                raise RuntimeError(f"Earlier destination files changed during the transfer. Left untouched: {root}")
        self.state["outputs"][root] = current

    def run(self, console):
        from bsync import run_transfer
        self.require_lock()
        if self.status not in ("CUTOFF", "PREPARING"):
            raise RuntimeError(f"Session is {self.status}; it cannot be resumed.")
        for source in self.plan_data["sources"]:
            location = source["vault"] if exists(source["vault"]) else source["path"]
            if not unchanged(location, self.state["sources"][source["path"]]):
                raise RuntimeError(f"Source changed since setup: {location}. Create a new session.")
        self.save("RUNNING")
        try:
            count = len(self.plan_data["sources"]) * len(self.plan_data["destinations"])
            for di, destination in enumerate(self.plan_data["destinations"]):
                root = destination["root"]
                if root not in self.state["allocated"] or not Path(root).is_dir() or Path(root).is_symlink():
                    raise RuntimeError(f"Session output folder is missing or replaced: {root}")
                expected = self.state["outputs"][root]
                if fingerprint(Path(root)) != expected["."]:
                    raise RuntimeError(f"Session output folder was replaced: {root}")
                for si, source in enumerate(self.plan_data["sources"]):
                    key = f"{di}:{si}"
                    if key in self.state["done"]:
                        continue
                    actual_source = source["vault"] if exists(source["vault"]) else source["path"]
                    # A moved file must retain its original name at the receiver.
                    if actual_source != source["path"]:
                        raise RuntimeError("A move was interrupted during finalization. Undo this session before retrying.")
                    result = run_transfer(self.history, actual_source, root + "/", console,
                                          session_id=self.id, context=f"{di*len(self.plan_data['sources'])+si+1}/{count}")
                    self.checkpoint_output(root, source)
                    self.save()
                    if result:
                        self.save("CUTOFF")
                        if result == 125:
                            from rich.prompt import Confirm
                            if Confirm.ask("Undo this session and remove its unchanged copies?", default=False, console=console):
                                self.undo()
                        return result
                    self.state["done"].append(key)
                    self.save()
            self.validate_outputs()
            for source in self.plan_data["sources"]:
                location = source["vault"] if exists(source["vault"]) else source["path"]
                if not unchanged(location, self.state["sources"][source["path"]]):
                    raise RuntimeError(f"Source changed during copying; originals retained: {location}")
            if self.plan_data["mode"] == "move":
                self.save("MOVING")
                for source in self.plan_data["sources"]:
                    if exists(source["vault"]):
                        if exists(source["path"]):
                            raise RuntimeError("Original path and undo vault both exist. Undo requires resolving the conflict.")
                        continue
                    # Vault target is unique and beside source, so rename stays on the same volume.
                    if exists(source["vault"]):
                        raise RuntimeError(f"Undo vault exists: {source['vault']}")
                    os.rename(source["path"], source["vault"])
                    self.save()
            self.save("COMPLETED")
            console.print(f"Session {self.id} completed.", style="green")
            return 0
        except BaseException:
            self.save("CUTOFF")
            raise

    def validate_outputs(self, subset=False):
        for root in self.state["allocated"]:
            if not unchanged(root, self.state["outputs"][root], subset=subset):
                raise RuntimeError(f"Destination files changed or were not checkpointed. Left untouched: {root}")

    def undo(self):
        self.require_lock()
        if self.status in ("UNDONE", "FINALIZED", "DISCARDING"):
            raise RuntimeError(f"Session is {self.status}; undo is unavailable.")
        self.validate_outputs(subset=self.status == "UNDOING")
        # Validate everything before restoring or removing anything.
        for source in self.plan_data["sources"]:
            if exists(source["vault"]):
                if exists(source["path"]) or not unchanged(source["vault"], self.state["sources"][source["path"]]):
                    raise RuntimeError(f"Original path occupied or vault changed. Left untouched: {source['path']}")
            elif not unchanged(source["path"], self.state["sources"][source["path"]]):
                raise RuntimeError(f"Original is missing or changed. Left untouched: {source['path']}")
        self.save("UNDOING")
        for source in self.plan_data["sources"]:
            if exists(source["vault"]):
                os.rename(source["vault"], source["path"])
        for root in self.state["allocated"]:
            remove_owned(root, self.state["outputs"][root])
        self.save("UNDONE")

    def discard(self):
        self.require_lock()
        if self.status not in ("COMPLETED", "DISCARDING") or self.plan_data["mode"] != "move":
            raise RuntimeError("Only completed moves have undo vaults to discard.")
        self.validate_outputs()
        for source in self.plan_data["sources"]:
            if not unchanged(source["vault"], self.state["sources"][source["path"]],
                             subset=self.status == "DISCARDING"):
                raise RuntimeError(f"Undo vault changed; left untouched: {source['vault']}")
        # Once deletion begins, never offer undo of a partially discarded vault.
        self.save("DISCARDING")
        for source in self.plan_data["sources"]:
            root = Path(source["vault"])
            if not exists(root):
                continue
            expected = self.state["sources"][source["path"]]
            if root.is_dir() and not root.is_symlink():
                remove_owned(root, expected)
            elif fingerprint(root) == expected["."]:
                root.unlink()
        self.save("FINALIZED")
