# shelf

[![CI](https://github.com/buildosaurus/shelf/actions/workflows/ci.yml/badge.svg)](https://github.com/buildosaurus/shelf/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**Browse your unplugged disks.**

`shelf` takes the inventory of a disk once, then lets you keep using `ls`,
`find`, `du` and `tree` on it — and browse it in your file manager — long after
you have put it back in a drawer.

```console
$ shelf find --name '*.flac' --min-size 20M -l
-   28.6 MiB  2026-06-14 21:03  [Enclosure1/Backup2] Music/Miles Davis/track1.flac
-   31.2 MiB  2026-06-14 21:03  [Enclosure1/Backup2] Music/Miles Davis/track2.flac
```

Neither disk is plugged in.

---

## Why

If you keep more than a couple of external disks, you know the loop: plug one
in, `ls`, no, wrong one, unplug, plug the next one in. The information you
needed — *which disk holds this file* — is a few hundred kilobytes of
metadata, and you were spinning terabytes of spinning rust to get at it.

`shelf` stores that metadata and answers the question offline. On a disk of
46,000 files, the catalogue weighs **1.3 MiB**, loads in **0.05 s**, and a
search across the whole fleet takes **0.02 s**.

Then it goes one better. `shelf ghost` rebuilds the tree as **sparse files** —
real directories, real names, real sizes, real dates, occupying essentially no
space. Your existing tools work on it unchanged, because as far as they can
tell, the disk is right there.

```console
$ du -sh ~/Volumes/Backup1          # 46,036 files, declaring 1,060 GB
108K
```

### Where other tools fit

Honest comparison — each of these wins somewhere:

| | Strength | Why you might still want `shelf` |
|---|---|---|
| **NeoFinder**, **DiskCatalogMaker** | Thumbnails, EXIF, ID3, a real GUI | Paid, macOS-only, and not scriptable |
| `locate` + a custom database | Built in, instant | Names only — no sizes, no dates, no `du` |
| `rclone check`, `mtree` | Excellent at verifying two trees | Built for comparison, not for browsing |
| `find` piped to a text file | Zero install | No structure, no queries, no ghosts |

`shelf` is for the case where you want the *shell* you already know, applied to
disks that are not there.

---

## Install

`shelf` is a **single file with no dependencies**. Python 3.12 or newer.

```console
$ git clone https://github.com/buildosaurus/shelf.git && cd shelf
$ ./shelf.py --version
```

The shebang uses [uv](https://docs.astral.sh/uv/), which makes the file
directly executable and needs no virtualenv:

```console
$ brew install uv            # macOS
$ curl -LsSf https://astral.sh/uv/install.sh | sh   # Linux
```

uv is a convenience, not a requirement — `python3 shelf.py` works just as well.

> **macOS only:** removable volumes are protected by the system. Grant
> **System Settings → Privacy & Security → Full Disk Access** to your terminal,
> or `shelf` cannot read your disks. It says so explicitly rather than writing
> an empty catalogue.

---

## Quick start

Plug in the disks of one enclosure and declare it — with no volume named,
`add` registers everything currently mounted:

```console
$ ./shelf.py enclosure add Enclosure1
  Backup1 -> Enclosure1
  Backup2 -> Enclosure1
```

Group the enclosures that live on one machine, then generate the clickable
shortcuts:

```console
$ ./shelf.py group add deskside Enclosure1 Enclosure2
$ ./shelf.py shortcuts
```

From now on, double-click `shortcuts/save-Enclosure1.command` whenever you plug
that enclosure in. It scans every mounted disk, rebuilds the ghosts, reports
the absent ones, and waits for a keypress before closing.

```console
$ ./shelf.py save deskside
  Enclosure1/Backup1: 46,036 files, 1.0 TiB  -> ~/Volumes/Backup1
  Enclosure1/Backup2: 8,210 files, 412.6 GiB -> ~/Volumes/Backup2
deskside: 2 volume(s) done, 1 absent(s), 0 failed
  absent: Backup3
```

---

## Layout

The base is **the directory holding `shelf.py`**. Put that folder in iCloud,
Dropbox or Syncthing and your fleet follows you to every machine, with no
hard-coded path anywhere. Override with `SHELF_HOME` or `--base`.

```
shelf/
├── shelf.py
├── shelf.toml                          enclosures, groups, per-platform paths
├── catalogs/
│   └── Enclosure1/Backup1.json.gz      the inventories (small, syncable)
└── shortcuts/
    └── save-Enclosure1.command         generated, clickable

~/Volumes/Backup1/                      the ghosts — LOCAL, never synced
```

> **Ghosts must never live in a synced folder.** Their files are sparse: a sync
> client reads them, materialises the zeros, and uploads a terabyte of nothing
> per disk. `shelf` keeps them out of the base, and on macOS excludes the ghost
> root from Time Machine on first use.

`shelf config` prints every path it will actually use — the fastest way to
check what a second machine will do:

```console
$ ./shelf.py config
platform      : macos
base          : /Users/you/Sync/shelf
config        : /Users/you/Sync/shelf/shelf.toml
catalogs      : /Users/you/Sync/shelf/catalogs
shortcuts     : /Users/you/Sync/shelf/shortcuts (*.command)
mount root    : /Volumes
ghost root    : /Users/you/Volumes
backup opt-out: tmutil
```

---

## Commands

### Managing the fleet

| Command | What it does |
|---|---|
| `shelf enclosure add NAME [VOLUME…]` | Attach volumes to an enclosure (default: all mounted) |
| `shelf enclosure rm VOLUME…` | Remove volumes from the fleet |
| `shelf enclosure list` | What is plugged in, what is catalogued, since when |
| `shelf group add NAME ENCLOSURE…` | Group enclosures under one name |
| `shelf group rm NAME` | Delete a group |
| `shelf config` | Detected platform and resolved paths |
| `shelf shortcuts` | One clickable launcher per enclosure and group |

```console
$ ./shelf.py enclosure list
Enclosure1
  * Backup1                      mounted   2026-06-14 21:03:44
  * Backup2                      mounted   2026-06-14 21:03:51
Enclosure2
    Backup3                      absent    never catalogued

groups
    deskside = Enclosure1, Enclosure2
```

### Cataloguing

`shelf save NAME` is the everyday gesture. For one disk at a time, or any
subfolder:

```console
$ ./shelf.py scan /Volumes/Backup1 --enclosure Enclosure1 --ghost
```

Without `--label` the label is the volume name. With `--enclosure` the
catalogue is filed under `catalogs/<enclosure>/` and the volume registers
itself in `shelf.toml`.

| Option | Effect |
|---|---|
| `--exclude PATTERN` | Ignore a pattern, repeatable (name, or path if it contains `/`) |
| `--no-default-excludes` | Keep `.DS_Store`, `._*`, `.Spotlight-V100`, `@eaDir`… |
| `--follow-symlinks` | Follow symlinks instead of recording them |
| `--allow-empty` | Overwrite a stocked inventory with an empty catalogue |
| `--mount-root`, `--ghost-root` | Override this platform's paths |

### Browsing, disk unplugged

With no catalogue argument, `find` and `info` query **the whole fleet** and tell
you which disk each hit lives on.

| Command | What it does |
|---|---|
| `shelf find [CATALOGUE…]` | Search the fleet |
| `shelf info [CATALOGUE…]` | Label, enclosure, machine, filesystem, size, date |
| `shelf ls CATALOGUE [PATH]` | List a directory (`-l` for size and date) |
| `shelf du CATALOGUE [PATH]` | What weighs the most (`--depth`, `--top`) |
| `shelf tree CATALOGUE [PATH]` | Draw the tree (`--depth`) |

Filters: `--name`, `--path`, `--under`, `--type f|d`, `--min-size`,
`--max-size`, `--newer`, `--older`, `--enclosure`, `--case-sensitive`. Sizes as
`700M`, `2G`, `4096`; dates as `2024`, `2024-06`, `2024-06-15`,
`2024-06-15 08:30`. Display with `-l`, `--sort name|size|date`, `-r`,
`--limit N`, `--json`.

**Accents are normalised.** APFS stores them decomposed (NFD), SMB shares hand
them back precomposed (NFC). Without normalisation, searching `Été` in a
catalogue taken on the other system would find nothing. `shelf` compares in NFC
throughout.

---

## Ghosts

A **sparse file** declares its true size without occupying a single block: the
filesystem records "bytes 0 to 1,000,000,000 are zero" and manufactures them on
read. `shelf ghost` rebuilds an entire tree that way.

The result is a directory with real names, sizes and dates, on which `ls -lh`,
`find -size +2G`, `du`, `grep`, Spotlight and your file manager all work
**natively**, for a footprint close to zero.

```console
$ shelf ghost catalogs/Enclosure1/Backup1.json.gz
Ghost created: /Users/you/Volumes/Backup1
  3,411 directories, 46,036 empty files
  Declared size: 1.0 TiB (real footprint ~0)
```

A `.shelf-ghost.json` at the root records which disk it came from, when, from
which machine, with which `shelf` version, and the source filesystem.

**Three traps worth knowing:**

- **exFAT and FAT cannot make holes.** Use `--empty` for zero-byte files
  (you lose the sizes, you keep the tree).
- **`rsync` breaks sparseness** and writes the bytes for real. Use `rsync -S`.
  `cp` preserves it on APFS and on Linux.
- **Never back a ghost up.** It holds no data and rebuilds in one command. The
  catalogue is what has value.

---

## Configuration

`shelf.toml` is hand-editable; a typo will not stop `shelf` from starting.

```toml
[platform.macos]
mount_root = "/Volumes"
ghost_root = "~/Volumes"

# [platform.linux]
# mount_root = "/run/media/<user>"
# ghost_root = "~/.local/share/shelf/ghosts"

[enclosures]
Enclosure1 = ["Backup1", "Backup2"]
Enclosure2 = ["Backup3"]

[groups]
deskside = ["Enclosure1", "Enclosure2"]

[labels]
"Photo Drive" = "Photos 2024"
```

The platform this machine runs is filled in; the other stays commented, so one
file documents both without imposing either. Precedence is **CLI flag > config
> built-in default**. `[labels]` is only needed when a label differs from the
volume name. A volume belongs to exactly one enclosure — redeclaring it
elsewhere moves it.

---

## Platform support

| | macOS | Linux | Windows |
|---|---|---|---|
| Catalogue, `ls` / `find` / `du` / `tree` | ✅ | ✅ | ❌ |
| Sparse ghosts | ✅ APFS | ✅ ext4, btrfs, XFS | ❌ |
| Mount root | `/Volumes` | `/run/media/<user>`, `/media/<user>`, `/media`, `/mnt` | ❌ |
| Shortcuts | `.command` | `.sh` | ❌ |
| Backup opt-out | `tmutil` | not needed | ❌ |

**Windows is not supported, deliberately.** `os.truncate()` does not mark a
file sparse on NTFS, so a ghost of a 1 TB disk would allocate 1 TB — which
defeats the entire point. `shelf` refuses to run there rather than fill your
drive. Both supported platforms are exercised by CI on every commit.

Generating one platform's shortcuts from the other is supported, and the two
sets coexist:

```console
$ ./shelf.py shortcuts --platform linux    # .sh files, from a Mac
$ ./shelf.py config --platform linux       # preview what Linux will resolve
```

---

## Safeguards

A catalogue is sometimes the **only** inventory of a disk you can no longer
consult. `shelf` refuses to damage one:

- **A failed walk writes nothing.** Zero entries *and* read errors — macOS
  blocking the disk, typically — stops `shelf`, which names the setting to
  change instead of writing an empty catalogue.
- **A genuinely empty disk needs no flag.** Zero entries *and* zero errors is
  an empty disk, not a failure.
- **A stocked inventory is never replaced by an empty one.** If the existing
  catalogue describes 46,000 entries and the walk returns none, `shelf` keeps
  the old one. `--allow-empty` if the disk really was emptied.
- **One unreadable disk does not sink the others.** In `save` it is reported,
  its catalogue is kept, the rest of the enclosure is processed, and the
  command exits `1` with the list of failures.
- **An unplugged disk is never a deletion.** It is skipped, catalogue and ghost
  intact.
- **A ghost is only replaced if it is one.** Deletion requires
  `.shelf-ghost.json` to be present, so a typo in a label cannot erase a real
  directory.
- **Every write is atomic** (temp file + `os.replace`) — never a half-written
  catalogue.

Each catalogue also records the machine that produced it, which matters when
two computers write into the same synced folder.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | Runtime error — or, for `save`, at least one disk failed |
| `2` | Usage error |
| `124` | A subprocess exceeded its timeout |
| `130` | Interrupted (Ctrl-C) |
| `141` | Downstream pipe closed (`shelf find … \| head`) |
| `143` | Terminated by `kill` |

---

## Interoperability

Catalogues are gzipped JSON with a stable shape:

```json
{
  "format": "shelf/1",
  "label": "Backup1",
  "enclosure": "Enclosure1",
  "root": "/Volumes/Backup1",
  "scanned_at": "2026-06-14 21:03:44",
  "hostname": "deskside",
  "shelf_version": "1.0.0",
  "platform": "macos 24.4.0 arm64",
  "filesystem": "apfs",
  "errors": [],
  "collisions": [],
  "entries": {"photos/img.jpg": {"rel": "Photos/img.jpg", "is_dir": false,
                                 "size": 3145728, "mtime": 1781470000.0}}
}
```

Keys are NFC-normalised, case-folded paths; `rel` keeps the name as stored on
disk. Anything that can read JSON can consume a catalogue — including a mirror
comparison tool that needs one side of the comparison to be a disk that is not
currently mounted.

---

## Development

Three gates, all green on macOS and Linux in CI:

```console
$ uv run --with pytest pytest test_shelf.py
$ uv run --with ruff ruff check --select E,F,I,E501 shelf.py test_shelf.py
$ uv run --with mypy --with pytest mypy --check-untyped-defs shelf.py test_shelf.py
```

Coverage — note `--cov=script_under_test`: the test file loads `shelf.py` by
path under that name, and `--cov=shelf` silently collects nothing.

```console
$ uv run --with pytest --with pytest-cov pytest test_shelf.py \
    --cov=script_under_test --cov-report=term-missing
shelf.py    1086    69    94%
```

**212 tests, 94% coverage.** The uncovered lines are mostly OS error branches
that would need a broken filesystem to reach; they are not padded to chase a
round number.

The script is built so testing stays cheap: pure logic (comparison, filters,
rendering, config, platform resolution) never touches the system, and every
side effect goes through **one** boundary function — `scan_tree`,
`read_catalog`, `run`, `mounted_volumes`, `build_ghost`, `detect_system` — which
a test swaps for a fake. No test reads a real disk, runs `tmutil`, or looks at
`sys.platform`.

---

## License

MIT. See [LICENSE](LICENSE).
