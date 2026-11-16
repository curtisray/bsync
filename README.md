# bsync

A Python 3.10+ CLI that wraps modern rsync with Rich progress, durable SQLite
history, and retries after interrupted transfers. Supports macOS and POSIX Linux.

## Install

### Homebrew (macOS and Linux)

Homebrew installs bsync, Python, and modern rsync together:

```sh
brew install curtisray/tap/bsync
bsync
```

### pipx or uv

Install from the pinned GitHub release using either installer:

```sh
pipx install 'git+https://github.com/curtisray/bsync.git@v0.2.1'
# or
uv tool install 'git+https://github.com/curtisray/bsync.git@v0.2.1'
```

Python-based installers require Python 3.10+ and rsync 3.1+. On macOS install
rsync with `brew install rsync`; on Debian/Ubuntu use `sudo apt install rsync`.

The Python distribution name is **better-rsync**, and the terminal command is
**bsync**. The unrelated package named `bsync` on PyPI is a Box.com sync tool.
PyPI publication is being configured; until it is live, use the pinned GitHub
install commands above. After publication, `pipx install better-rsync`,
`uv tool install better-rsync`, and `pip install better-rsync` will be available.
Use a virtual environment for plain pip installs.

Native Windows is not supported. On Windows, run bsync inside WSL with Linux
Python and rsync. Winget, Scoop, and Chocolatey installers are not provided.

### Development

For development:

```sh
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

See [distribution and release instructions](docs/distribution.md) for
maintainer setup, PyPI activation, and adding additional package managers.

## Use

Run `bsync` with no arguments in your terminal to open the guided interface.
Use **↑ / ↓** and **Enter** to choose commands; **Esc** returns to the menu.
Each command includes a description of what it does.

The new-transfer walkthrough asks you to:

1. **Copy** (keep originals) or **Move** (retain originals in undo vaults).
2. Select one or more source files or folders. Paths pasted or dragged from
   Finder, including quoted paths and spaces, are accepted.
3. Choose one or multiple destinations. Every destination receives the entire
   selection; copies run sequentially to avoid competing for disk bandwidth.
4. Enter existing destination folders.
5. Review exact source and output paths, then start.

The source container drains as the destination container fills. This represents
the current sync's progress, rather than actual source deletion. A flowing arrow,
current filename, byte count, rate, ETA, and step counter accompany the containers.
Narrow terminals stack the containers. During a transfer:

| Key | Action |
| --- | --- |
| `P` or Space | Pause/resume the rsync process group, retaining the current transfer |
| `R` | Resume a paused transfer |
| `Q` | Stop gracefully and save for a later retry |
| `U` | Stop a guided session, then ask whether to undo it |

After completion or stopping, choose **Undo**, **Resume**, or **Return to menu**.
Completed moves also offer **Discard undo vaults**, with confirmation.
You can revisit a session through the menu's history, resume, and undo commands.

### Reversible guided transfers

Guided setup supports **local filesystems and mounted volumes**. Each destination
gets a new `bsync-<session-id>/` folder containing the selected files and folders
under their original names. It never merges guided transfers into existing files.
Sources must have distinct names and cannot contain each other. A destination
cannot be inside a source. Selected symlinks are preserved as links.

For **Move**, every destination copy must finish before originals are renamed into
hidden `.bsync-<session-id>-<name>` vaults beside their original locations. The
same-volume rename makes restoration fast and preserves the originals' metadata.
**Move does not release source disk space until you explicitly discard these
vaults.** Discarding them is permanent and disables undo for that session.

Undo restores any vaulted originals and removes only the session-owned output.
It checks a persisted manifest of file identities, types, sizes, timestamps, and
symlink targets. Added, edited, replaced, or missing outputs cause undo to refuse;
an occupied original path also blocks restoration. Directory deletion uses
manifest entries and empty-directory checks, without following symlinks. It is a
metadata check, not a content hash or protection against concurrent external edits;
avoid changing session files while a transfer, undo, or discard is active.

Session plans and manifests persist in SQLite. Resume continues the whole plan,
including all remaining sources and destinations. A numeric `bsync resume <id>`
for an interrupted guided job also resumes its containing session. A hard kill
may leave output that was not checkpointed; undo refuses unrecognized changes.
Finish the session through Resume before attempting undo in that case. Keep the
history database while you need undo or recovery. Undo cannot be retroactively
provided for older direct `bsync run` transfers.

### Direct commands

Existing commands remain available, including SSH/rsync daemon endpoints:

```sh
bsync run /Volumes/Camera/ /Volumes/Backup/Camera/
bsync run ./videos/ editor@studio:/media/videos/
bsync history
bsync cutoff
bsync resume 12
```

Rsync's trailing-slash semantics apply: `videos/` copies its contents, while
`videos` copies the directory itself. Quote paths containing spaces. Transfers
use archive mode and preserve partial files, without deleting extra destination
files. Existing destination files may be replaced, as with normal rsync.

The live display shows rsync's overall logical byte progress, speed, ETA, and
current file. Discovery happens before transfer because incremental recursion is
disabled. The time on a completed-file progress record is elapsed time rather
than ETA, so bsync shows a dash for those records. Rsync's human-readable output
may round large byte counts; they are progress estimates, not network byte totals.

Ctrl+C and SIGTERM stop the rsync process group, save progress, and mark the job
`CUTOFF`. Nonzero rsync exits also become `CUTOFF`; diagnostic output is shown.
Progress is committed at most twice per second and once on graceful exit. After
a hard crash, the next invocation marks abandoned `RUNNING` rows `CUTOFF`; their
saved progress may lag by a checkpoint interval. One transfer can run per database;
history and cutoff inspection remain available during an active transfer.

`resume` accepts a `CUTOFF` ID and creates a new history row with the original
endpoints. Rsync rechecks the destination and uses preserved partial files where
its transfer algorithm permits. It does not blindly seek to the recorded byte
offset. Local paths are saved as absolute paths; remote relative paths retain
their usual remote-login meaning. After a forced parent kill, check for any
surviving rsync process before retrying.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `BSYNC_DB` | `~/.bsync_history.db` | History database (with an adjacent `.lock` file) |
| `BSYNC_RSYNC` | Homebrew rsync, then `PATH` | Explicit rsync executable; requires 3.1+ |

The database records `id`, `src`, `dst`, `status`, `bytes_done`, `pct`,
`started_at`, `ended_at`, and an optional guided `session_id`. A separate `sessions`
table records guided plans, manifests, and lifecycle state. Existing databases
are migrated automatically. Timestamps use UTC. Both history views show at most
20 rows, newest first. File paths and diagnostics are rendered as literal text.

Rsync options and progress semantics follow the
[official rsync manual](https://www.samba.org/ftp/rsync/rsync.1).
