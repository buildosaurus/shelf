#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Catalogue a disk's tree so you can browse it once it is unplugged."""

# The noqa suppresses a false positive: with no pyproject.toml to read, ruff
# assumes an old target-version and misfiles `tomllib` (stdlib since 3.11) as
# third-party. This order is right for the ">=3.12" declared above.
from __future__ import annotations  # noqa: I001

import argparse
import fnmatch
import gzip
import json
import getpass
import logging
import os
import platform
import plistlib
import shlex
import shutil
import signal
import functools
import subprocess
import sys
import tempfile
import tomllib
import unicodedata
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path

__version__ = "1.0.0"

CATALOG_FORMAT = "shelf/1"
GHOST_MARKER = ".shelf-ghost.json"

# Layout, relative to the base (the script's own directory by default):
#   <base>/shelf.toml
#   <base>/catalogs/<enclosure>/<label>.json.gz
#   <base>/shortcuts/save-<enclosure>.command
# Ghosts, by contrast, stay LOCAL to each machine: dropped in a synced base
# (iCloud, Dropbox), their sparse files would be read, materialised and
# uploaded — terabytes of zeros for empty content.
CATALOGS_DIRNAME = "catalogs"
SHORTCUTS_DIRNAME = "shortcuts"
CONFIG_NAME = "shelf.toml"
LEGACY_CONFIG_NAME = "enclosures.toml"
DEFAULT_ENCLOSURE = "misc"
DEFAULT_TIMEOUT = 30.0  # no subprocess may hang forever

# Supported platforms. Windows is deliberately absent: os.truncate() does not
# mark a file sparse on NTFS, so a ghost of a 1 TB disk would allocate 1 TB —
# which defeats the whole point of a ghost.
MACOS, LINUX = "macos", "linux"
PLATFORMS = (MACOS, LINUX)

# Where distributions mount removable media. Probed in order; {user} is filled
# in at run time, never baked into the file.
# Shown, commented out, for whichever platform this machine is not running.
# <user> stays a placeholder: no account name is ever written into the file
# for a machine other than the one generating it.
CONFIG_HINTS = {
    "macos": {"mount_root": "/Volumes", "ghost_root": "~/Volumes"},
    "linux": {
        "mount_root": "/run/media/<user>",
        "ghost_root": "~/.local/share/shelf/ghosts",
    },
}

LINUX_MOUNT_CANDIDATES = (
    "/run/media/{user}",
    "/media/{user}",
    "/media",
    "/mnt",
)

# macOS / Windows / NAS noise: pointless in a catalogue.
DEFAULT_EXCLUDES = (
    ".DS_Store",
    "._*",
    ".Spotlight-V100",
    ".fseventsd",
    ".Trashes",
    ".TemporaryItems",
    ".DocumentRevisions-V100",
    ".apdisk",
    ".localized",
    ".AppleDouble",
    ".AppleDB",
    ".AppleDesktop",
    "Network Trash Folder",
    "Temporary Items",
    "$RECYCLE.BIN",
    "System Volume Information",
    "Thumbs.db",
    "desktop.ini",
    "@eaDir",
    ".@__thumb",
)

_SIZE_UNITS = {"": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}
_DATE_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m", "%Y")
PROGRESS_EVERY = 5000

log = logging.getLogger("shelf")


# --- Model ---

@dataclass(frozen=True)
class Entry:
    """One entry of the tree, identified by its path relative to the root."""

    rel: str
    is_dir: bool
    size: int
    mtime: float

    @property
    def name(self) -> str:
        return self.rel.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class Catalog:
    """A frozen disk: what `scan` writes and every other command reads."""

    root: str
    label: str
    scanned_at: str
    entries: dict[str, Entry] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    collisions: list[str] = field(default_factory=list)
    source: str = ""
    enclosure: str = ""
    hostname: str = ""
    shelf_version: str = ""
    platform: str = ""
    filesystem: str = ""
    excludes: list[str] = field(default_factory=list)

    @property
    def display(self) -> str:
        """"Enclosure1/Backup1" when the enclosure is known, else the label."""
        return f"{self.enclosure}/{self.label}" if self.enclosure else self.label

    @property
    def files(self) -> list[Entry]:
        return [e for e in self.entries.values() if not e.is_dir]

    @property
    def dirs(self) -> list[Entry]:
        return [e for e in self.entries.values() if e.is_dir]

    @property
    def total_size(self) -> int:
        return sum(e.size for e in self.entries.values() if not e.is_dir)


# --- Pure logic: no side effects, testable without mocks ---

def norm_key(rel: str, *, case_insensitive: bool = True) -> str:
    """Comparison/index key for a relative path.

    NFC is mandatory: APFS stores accents decomposed (NFD) while an SMB share
    hands them back precomposed (NFC). Without normalisation, searching for
    "Resume" in a catalogue taken on the other system finds nothing.
    """
    key = unicodedata.normalize("NFC", rel)
    return key.casefold() if case_insensitive else key


@functools.lru_cache(maxsize=64)
def norm_patterns(patterns: tuple[str, ...]) -> tuple[str, ...]:
    """Normalise a pattern set once per walk, not once per file.

    scan_tree calls is_excluded on every entry; folding twenty patterns 46,000
    times is work for nothing. Cached on the tuple, which is the same object
    for the whole walk. Normalising inside rather than asking callers to do it
    keeps the raw DEFAULT_EXCLUDES safe to pass straight in.
    """
    return tuple(norm_key(p) for p in patterns)


def is_excluded(rel: str, name: str, patterns: tuple[str, ...]) -> bool:
    """A pattern containing a "/" is matched on the path, else on the name.

    Compared through norm_key, like every other match in shelf: APFS is
    case-insensitive by default, so a pattern typed "Photos/RAW" into the config
    must still catch "Photos/raw". Both sides are normalised - fnmatch would
    otherwise compare a decomposed name against a precomposed pattern.
    """
    rel_key, name_key = norm_key(rel), norm_key(name)
    return any(
        fnmatch.fnmatchcase(rel_key if "/" in pattern else name_key, pattern)
        for pattern in norm_patterns(patterns)
    )


def parse_size(text: str) -> int:
    """"100", "4k", "700M", "2G" -> bytes (binary units)."""
    raw = text.strip().upper().removesuffix("IB").removesuffix("B")
    unit = raw[-1] if raw[-1:] in _SIZE_UNITS and raw[-1:] != "" else ""
    number = raw[:-1] if unit else raw
    try:
        return int(float(number) * _SIZE_UNITS[unit])
    except ValueError as exc:
        raise QueryError(
            f"unreadable size: {text!r} (try 700M, 2G, 4096)"
        ) from exc


def parse_date(text: str) -> float:
    """« 2024 », « 2024-06 », « 2024-06-15 », « 2024-06-15 08:30 » -> timestamp."""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).timestamp()
        except ValueError:
            continue
    raise QueryError(f"unreadable date: {text!r} (try 2024-06-15)")


def parent_key(key: str) -> str:
    return key.rsplit("/", 1)[0] if "/" in key else ""


def ancestors(key: str) -> list[str]:
    parts = key.split("/")
    return [""] + ["/".join(parts[:i]) for i in range(1, len(parts))]


def filter_entries(
    entries: dict[str, Entry], patterns: tuple[str, ...]
) -> dict[str, Entry]:
    """Apply excludes to an already-built catalogue.

    `scan_tree` prunes: an excluded directory is never descended into. A
    catalogue has no tree to prune - it is a flat dict - so excluding "VMs"
    would drop the directory and leave "VMs/disk.img" behind, whose basename
    matches nothing.

    Sorted keys put a directory immediately before its whole subtree, so one
    string prefix skips the lot - and skips testing every pattern against every
    buried file, which is the point on the folders big enough to be worth
    excluding. Keys are already norm_key-ed, so matching here is normalised for
    free.
    """
    if not patterns:
        return entries
    kept: dict[str, Entry] = {}
    pruned = ""
    for key in sorted(entries):
        if pruned and key.startswith(pruned):
            continue
        pruned = ""
        entry = entries[key]
        if is_excluded(key, key.rsplit("/", 1)[-1], patterns):
            if entry.is_dir:
                pruned = f"{key}/"
            continue
        kept[key] = entry
    return kept


def index_children(entries: dict[str, Entry]) -> dict[str, list[str]]:
    """Parent -> direct children index, built once per command.

    Without it, every level of `tree` would rescan the whole catalogue: O(n)
    per directory instead of O(n) overall.
    """
    index: dict[str, list[str]] = defaultdict(list)
    for key in entries:
        index[parent_key(key)].append(key)
    for keys in index.values():
        keys.sort()
    return index


def dir_totals(entries: dict[str, Entry]) -> dict[str, tuple[int, int]]:
    """For each directory (and "" = the root): (bytes, number of files).

    Rolled up recursively: every file is counted in all of its ancestors.
    """
    totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for key, entry in entries.items():
        if entry.is_dir:
            continue
        for anc in ancestors(key):
            totals[anc][0] += entry.size
            totals[anc][1] += 1
    return {key: (size, count) for key, (size, count) in totals.items()}


@dataclass(frozen=True)
class Query:
    """The criteria of `find`, grouped so they stay testable in one block."""

    name: str | None = None
    path: str | None = None
    kind: str = "any"
    min_size: int | None = None
    max_size: int | None = None
    newer: float | None = None
    older: float | None = None
    under: str = ""
    case_sensitive: bool = False


def matches(entry: Entry, query: Query) -> bool:
    """A single pure predicate: this is where all of `find`'s semantics live."""
    if query.kind == "f" and entry.is_dir:
        return False
    if query.kind == "d" and not entry.is_dir:
        return False
    if query.under:
        prefix = norm_key(query.under, case_insensitive=True).rstrip("/") + "/"
        if not norm_key(entry.rel, case_insensitive=True).startswith(prefix):
            return False
    if query.name is not None:
        target, pattern = entry.name, query.name
        if not query.case_sensitive:
            target, pattern = target.casefold(), pattern.casefold()
        if not fnmatch.fnmatch(unicodedata.normalize("NFC", target), pattern):
            return False
    if query.path is not None:
        target, pattern = entry.rel, query.path
        if not query.case_sensitive:
            target, pattern = target.casefold(), pattern.casefold()
        if not fnmatch.fnmatch(unicodedata.normalize("NFC", target), pattern):
            return False
    # A directory's size is meaningless here (it is 0 in the catalogue), so a
    # size filter excludes directories rather than letting them all through —
    # otherwise "--min-size 20M" would return the entire tree.
    if query.min_size is not None or query.max_size is not None:
        if entry.is_dir:
            return False
        if query.min_size is not None and entry.size < query.min_size:
            return False
        if query.max_size is not None and entry.size > query.max_size:
            return False
    if query.newer is not None and entry.mtime < query.newer:
        return False
    if query.older is not None and entry.mtime > query.older:
        return False
    return True


def search(entries: dict[str, Entry], query: Query) -> list[Entry]:
    return [e for e in entries.values() if matches(e, query)]


def sort_entries(
    items: list[Entry], *, key: str = "name", reverse: bool = False
) -> list[Entry]:
    keys = {
        "name": lambda e: e.rel.casefold(),
        "size": lambda e: (e.size, e.rel.casefold()),
        "date": lambda e: (e.mtime, e.rel.casefold()),
    }
    if key not in keys:
        raise QueryError(f"unknown sort key: {key}")
    return sorted(items, key=keys[key], reverse=reverse)


# --- The fleet: enclosures, groups, labels (pure logic) ---

@dataclass(frozen=True)
class Platform:
    """Everything that differs between macOS and Linux, resolved once.

    Keeping it in one object means no other function has to know which OS it
    is running on, and a test can build any platform without touching sys.
    """

    name: str
    mount_root: Path
    ghost_root: Path
    shortcut_suffix: str
    excluder: str = ""  # "tmutil" on macOS, nothing elsewhere


@dataclass(frozen=True)
class Fleet:
    """What enclosures.toml declares: who lives in which enclosure.

    `enclosures`: enclosure name -> volume names as they appear in /Volumes.
    `groups`: name -> list of enclosures. `labels`: volume -> label, only when
    it differs from the volume name.
    """

    enclosures: dict[str, list[str]] = field(default_factory=dict)
    groups: dict[str, list[str]] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    platforms: dict[str, dict[str, str]] = field(default_factory=dict)
    global_excludes: list[str] = field(default_factory=list)
    catalogue_excludes: dict[str, list[str]] = field(default_factory=dict)


def _as_str_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _as_table(value: object) -> dict[str, object]:
    return {str(k): v for k, v in value.items()} if isinstance(value, dict) else {}


def parse_fleet(raw: dict[str, object]) -> Fleet:
    """Build a Fleet from already-parsed TOML. Forgiving: a missing or
    mistyped section yields an empty dict, never an exception — a hand-edited
    file must not break every command."""
    excludes = _as_table(raw.get("excludes"))
    return Fleet(
        enclosures={
            name: _as_str_list(v)
            for name, v in _as_table(raw.get("enclosures")).items()
        },
        groups={
            name: _as_str_list(v) for name, v in _as_table(raw.get("groups")).items()
        },
        labels={
            name: str(v) for name, v in _as_table(raw.get("labels")).items()
        },
        platforms={
            name: {k: str(v) for k, v in _as_table(section).items()}
            for name, section in _as_table(raw.get("platform")).items()
            if name in PLATFORMS
        },
        global_excludes=_as_str_list(excludes.get("global")),
        catalogue_excludes={
            label: _as_str_list(patterns)
            for label, patterns in _as_table(excludes.get("catalogue")).items()
        },
    )


def _toml_str(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_key(key: str) -> str:
    plain = key and all(c.isalnum() or c in "-_" for c in key)
    return key if plain else _toml_str(key)


def dump_fleet(fleet: Fleet) -> str:
    """Serialise the fleet to TOML. Hand-written: the stdlib can read TOML
    (tomllib) but not write it, and the shape to emit is three tables."""
    lines = [
        "# shelf configuration - edit by hand if you like.",
        "# Volume names are the ones that appear under the platform's mount root.",
        "",
    ]
    for name in PLATFORMS:
        section = fleet.platforms.get(name)
        # The platform this machine is not running gets a commented template
        # carrying that platform's shape - with <user> left as a placeholder,
        # never a real account name - so the file documents both without
        # imposing either.
        prefix = "" if section else "# "
        lines.append(f"{prefix}[platform.{name}]")
        for key in ("mount_root", "ghost_root"):
            value = (section or CONFIG_HINTS[name]).get(key, "")
            lines.append(f"{prefix}{key} = {_toml_str(value)}")
        lines.append("")
    lines.append("[enclosures]")
    for name in sorted(fleet.enclosures):
        volumes = ", ".join(_toml_str(v) for v in fleet.enclosures[name])
        lines.append(f"{_toml_key(name)} = [{volumes}]")
    lines += ["", "[groups]"]
    for name in sorted(fleet.groups):
        members = ", ".join(_toml_str(e) for e in fleet.groups[name])
        lines.append(f"{_toml_key(name)} = [{members}]")
    lines += ["", "[labels]"]
    for volume in sorted(fleet.labels):
        lines.append(f"{_toml_key(volume)} = {_toml_str(fleet.labels[volume])}")
    # Skipped when scanning AND hidden from ls/find/du/tree, so a rule added
    # here bites at once, with the disk still in its drawer. A pattern holding
    # a "/" matches the path from the root of the disk, else the name alone.
    lines += ["", "[excludes]"]
    patterns = ", ".join(_toml_str(p) for p in fleet.global_excludes)
    lines.append(f"global = [{patterns}]")
    # Keyed by label - the name the catalogue is filed under, not the volume.
    lines += ["", "[excludes.catalogue]"]
    for label in sorted(fleet.catalogue_excludes):
        patterns = ", ".join(_toml_str(p) for p in fleet.catalogue_excludes[label])
        lines.append(f"{_toml_key(label)} = [{patterns}]")
    return "\n".join(lines) + "\n"


def resolve_volumes(fleet: Fleet, name: str) -> list[tuple[str, str]]:
    """"Enclosure1" or "macmini" -> [(enclosure, volume), ...], deduplicated.

    A group wins over an enclosure of the same name: the aggregate is what you
    meant to name, otherwise you would call the enclosure directly.
    """
    if name in fleet.groups:
        enclosures = fleet.groups[name]
    elif name in fleet.enclosures:
        enclosures = [name]
    else:
        known = ", ".join(sorted(set(fleet.enclosures) | set(fleet.groups))) or "none"
        raise QueryError(f"unknown enclosure or group: {name} (known: {known})")
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for enclosure in enclosures:
        for volume in fleet.enclosures.get(enclosure, []):
            if volume not in seen:
                seen.add(volume)
                out.append((enclosure, volume))
    return out


def enclosure_of(fleet: Fleet, volume: str) -> str:
    for name, volumes in sorted(fleet.enclosures.items()):
        if volume in volumes:
            return name
    return ""


def label_of(fleet: Fleet, volume: str) -> str:
    """The declared label, else the volume name — the automatic-naming rule."""
    return fleet.labels.get(volume, volume)


def register_volume(fleet: Fleet, enclosure: str, volume: str, label: str) -> Fleet:
    """Declare a volume in an enclosure. Pure: returns a new Fleet.

    A volume belongs to exactly one enclosure: redeclaring it elsewhere moves
    it, otherwise `resolve_volumes` would scan it twice.
    """
    enclosures = {
        name: [v for v in volumes if v != volume]
        for name, volumes in fleet.enclosures.items()
    }
    enclosures.setdefault(enclosure, [])
    enclosures[enclosure] = sorted({*enclosures[enclosure], volume})
    labels = dict(fleet.labels)
    if label == volume:
        labels.pop(volume, None)
    else:
        labels[volume] = label
    return replace(fleet, enclosures=enclosures, labels=labels)


def unregister_volume(fleet: Fleet, volume: str) -> Fleet:
    return replace(
        fleet,
        enclosures={
            name: [v for v in volumes if v != volume]
            for name, volumes in fleet.enclosures.items()
        },
        labels={k: v for k, v in fleet.labels.items() if k != volume},
    )


def set_group(fleet: Fleet, name: str, enclosures: list[str]) -> Fleet:
    unknown = [e for e in enclosures if e not in fleet.enclosures]
    if unknown:
        raise QueryError(f"unknown enclosure(s): {', '.join(unknown)}")
    groups = dict(fleet.groups)
    groups[name] = list(dict.fromkeys(enclosures))
    return replace(fleet, groups=groups)


def drop_group(fleet: Fleet, name: str) -> Fleet:
    if name not in fleet.groups:
        raise QueryError(f"unknown group: {name}")
    groups = {k: v for k, v in fleet.groups.items() if k != name}
    return replace(fleet, groups=groups)


def platform_from_system(system: str) -> str:
    """sys.platform -> the name shelf exposes. Windows is refused, not guessed.

    os.truncate() does not mark a file sparse on NTFS, so a ghost there would
    allocate the disk's full size. Failing loudly beats filling someone's SSD.
    """
    if system == "darwin":
        return MACOS
    if system.startswith("linux"):
        return LINUX
    raise PlatformError(
        f"unsupported platform: {system}. shelf runs on macOS and Linux "
        "(Windows has no usable sparse files through Python)."
    )


def linux_mount_candidates(user: str) -> list[Path]:
    """Where the common distributions mount removable media, in probe order."""
    return [Path(c.format(user=user)) for c in LINUX_MOUNT_CANDIDATES]


def mount_point(path: Path, is_mount: Callable[[Path], bool]) -> Path:
    """Walk up to the volume's own mount point.

    `diskutil info` only answers for a mount point or a device, not for an
    arbitrary path inside one - so asking about ~/Documents returns nothing
    useful unless we climb first. Pure, given the predicate.
    """
    current = path
    while current != current.parent and not is_mount(current):
        current = current.parent
    return current


def pick_mount_root(candidates: list[Path], exists: Callable[[Path], bool]) -> Path:
    """First candidate that exists, else the first one so the error names it.

    `exists` is injected rather than called directly: that keeps this pure and
    lets a test lay out any distribution's conventions with no filesystem.
    """
    for candidate in candidates:
        if exists(candidate):
            return candidate
    return candidates[0]


def build_platform(
    name: str,
    *,
    user: str,
    env: dict[str, str],
    overrides: dict[str, str] | None = None,
    exists: Callable[[Path], bool] | None = None,
) -> Platform:
    """Assemble a Platform. Precedence: overrides (config) > built-in defaults.

    `user` is substituted at run time and never written into the defaults, so
    no home directory or account name is ever baked into the shipped script.
    """
    if name not in PLATFORMS:
        raise PlatformError(f"unknown platform: {name} (known: {', '.join(PLATFORMS)})")
    probe = exists if exists is not None else Path.is_dir
    if name == MACOS:
        base = Platform(
            name=MACOS,
            mount_root=Path("/Volumes"),
            ghost_root=Path("~/Volumes"),
            shortcut_suffix=".command",
            excluder="tmutil",
        )
    else:
        data_home = env.get("XDG_DATA_HOME") or "~/.local/share"
        base = Platform(
            name=LINUX,
            mount_root=pick_mount_root(linux_mount_candidates(user), probe),
            ghost_root=Path(data_home) / "shelf" / "ghosts",
            shortcut_suffix=".sh",
            excluder="",
        )
    picked = overrides or {}

    def override(key: str, fallback: Path) -> Path:
        return Path(picked[key]) if picked.get(key) else fallback

    return Platform(
        name=base.name,
        mount_root=override("mount_root", base.mount_root),
        ghost_root=override("ghost_root", base.ghost_root),
        shortcut_suffix=base.shortcut_suffix,
        excluder=base.excluder,
    )


def platform_as_config(platform: Platform) -> dict[str, str]:
    """What gets written back into [platform.<name>] of shelf.toml."""
    return {
        "mount_root": str(platform.mount_root),
        "ghost_root": str(platform.ghost_root),
    }


def render_platform(platform: Platform, *, base: Path) -> str:
    """What `shelf config` prints: every path shelf will actually use."""
    return "\n".join(
        [
            f"platform      : {platform.name}",
            f"base          : {base}",
            f"config        : {base / CONFIG_NAME}",
            f"catalogs      : {base / CATALOGS_DIRNAME}",
            f"shortcuts     : {base / SHORTCUTS_DIRNAME} (*{platform.shortcut_suffix})",
            f"mount root    : {platform.mount_root}",
            f"ghost root    : {platform.ghost_root.expanduser()}",
            f"backup opt-out: {platform.excluder or '(none on this platform)'}",
        ]
    )


def ghost_identity(catalog: Catalog) -> dict[str, str]:
    """What identifies the exact content a ghost was built from.

    The timestamp alone is not enough: `scanned_at` has one-second resolution,
    so two scans within the same second look identical. Entry count and total
    size are content-derived and settle it.
    """
    return {
        "label": catalog.label,
        "scanned_at": catalog.scanned_at,
        "entries": str(len(catalog.entries)),
        "bytes": str(catalog.total_size),
    }


def ghost_is_current(marker: dict[str, str], catalog: Catalog) -> bool:
    """Does an existing ghost already match this catalogue?

    Compared on identity, not by walking the tree: rebuilding is cheap but not
    free, and a fleet of ten disks is mostly unchanged on any given day.
    """
    if not marker:
        return False
    wanted = ghost_identity(catalog)
    return all(marker.get(key) == value for key, value in wanted.items())


def catalog_relpath(enclosure: str, label: str) -> Path:
    folder = enclosure or DEFAULT_ENCLOSURE
    return Path(CATALOGS_DIRNAME) / folder / f"{label}.json.gz"


def shortcut_script(
    tool_ref: str, target: str, options: list[str] | None = None
) -> str:
    """The clickable .command.

    `tool_ref` is already a shell expression: "$DIR/../shelf.py" when the
    script sits inside the shortcut's own tree — the folder can then be moved
    or mounted elsewhere on the other Mac and it follows — or an absolute path
    otherwise. `options` is appended verbatim to the `save` line, each element
    quoted by shlex so it survives spaces.
    """
    extra = "".join(f" {shlex.quote(opt)}" for opt in options or [])
    return f"""#!/bin/bash
# Generated by shelf.py - do not edit by hand, re-run `shelf shortcuts`.
set -euo pipefail
DIR="$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)"
"{tool_ref}" save {shlex.quote(target)} -v{extra}
echo
read -n 1 -s -r -p "Done - press any key to close." || true
echo
"""


def fleet_report(
    fleet: Fleet, mounted: set[str], catalogued: dict[str, str]
) -> list[str]:
    """Fleet status: what is plugged in, what is catalogued, and since when."""
    lines: list[str] = []
    for enclosure in sorted(fleet.enclosures):
        lines.append(enclosure)
        volumes = fleet.enclosures[enclosure]
        if not volumes:
            lines.append("    (no volume declared)")
        for volume in volumes:
            bullet = "*" if volume in mounted else " "
            state = "mounted" if volume in mounted else "absent "
            label = label_of(fleet, volume)
            shown = volume if label == volume else f"{volume} -> {label}"
            when = catalogued.get(label, "never catalogued")
            lines.append(f"  {bullet} {shown:<28} {state}   {when}")
    if fleet.groups:
        lines.append("")
        lines.append("groups")
        for name in sorted(fleet.groups):
            lines.append(f"    {name} = {', '.join(fleet.groups[name])}")
    return lines or ["(no enclosure declared — see `shelf enclosure add`)"]


# --- Rendu (pur) ---

def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def human_int(value: int) -> str:
    return f"{value:,}"


def human_time(mtime: float) -> str:
    try:
        return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "?"


def format_long(entry: Entry, *, prefix: str = "", show: str | None = None) -> str:
    """`show` lets ls print the bare name where find prints the full path."""
    kind = "d" if entry.is_dir else "-"
    size = "-" if entry.is_dir else human_size(entry.size)
    text = entry.rel if show is None else show
    return f"{kind} {size:>10}  {human_time(entry.mtime)}  {prefix}{text}"


def render_tree(
    entries: dict[str, Entry],
    children: dict[str, list[str]],
    *,
    start: str = "",
    depth: int = 3,
    show_size: bool = False,
) -> list[str]:
    """Tree drawing, `tree`-style, bounded by depth."""
    lines: list[str] = []

    def walk(key: str, prefix: str, level: int) -> None:
        if level > depth:
            return
        kids = children.get(key, [])
        for index, child in enumerate(kids):
            entry = entries[child]
            last = index == len(kids) - 1
            branch = "`-- " if last else "|-- "
            suffix = ""
            if show_size and not entry.is_dir:
                suffix = f"  ({human_size(entry.size)})"
            slash = "/" if entry.is_dir else ""
            lines.append(f"{prefix}{branch}{entry.name}{slash}{suffix}")
            if entry.is_dir:
                walk(child, prefix + ("    " if last else "|   "), level + 1)

    walk(start, "", 1)
    return lines


def render_info(catalog: Catalog) -> str:
    lines = [
        f"Label       : {catalog.label}",
        f"Enclosure   : {catalog.enclosure or '(not declared)'}",
        f"Root        : {catalog.root}",
        f"Catalogued  : {catalog.scanned_at}",
        f"Files       : {human_int(len(catalog.files))}",
        f"Directories : {human_int(len(catalog.dirs))}",
        f"Total size  : {human_size(catalog.total_size)}",
    ]
    if catalog.filesystem:
        lines.append(f"Filesystem  : {catalog.filesystem}")
    if catalog.hostname:
        stamp = catalog.hostname
        if catalog.platform:
            stamp += f" ({catalog.platform})"
        if catalog.shelf_version:
            stamp += f", shelf {catalog.shelf_version}"
        lines.append(f"Scanned by  : {stamp}")
    if catalog.excludes:
        # The built-ins are noise - what matters is what YOU asked to skip.
        chosen = [p for p in catalog.excludes if p not in DEFAULT_EXCLUDES]
        builtins = len(catalog.excludes) - len(chosen)
        summary = ", ".join(chosen) or "(built-ins only)"
        if builtins:
            summary += f" (+{human_int(builtins)} built-ins)"
        lines.append(f"Not scanned : {summary}")
    if catalog.errors:
        lines.append(f"Errors      : {human_int(len(catalog.errors))} (see --json)")
    if catalog.collisions:
        lines.append(f"Collisions  : {human_int(len(catalog.collisions))}")
    return "\n".join(lines)


# --- Boundaries: isolated side effects (the only things to mock) ---

def scan_tree(
    root: Path,
    *,
    excludes: tuple[str, ...],
    follow_symlinks: bool = False,
) -> tuple[dict[str, Entry], list[str], list[str]]:
    """THE single entry point to the filesystem for a walk."""
    if not root.is_dir():
        raise ScanError(
            f"{root} is not a reachable directory (is the disk plugged in?)"
        )
    # Probe the root BEFORE walking: macOS denies access to removable volumes
    # until the app running shelf is authorised. Without this probe, os.walk
    # only reports one error line and yields an empty catalogue — which would
    # then overwrite a good one on the next `save`.
    try:
        with os.scandir(root) as probe:
            next(iter(probe), None)
    except PermissionError as exc:
        raise ScanError(
            f"{root}: access denied by macOS. Authorise the application running "
            "shelf in System Settings > Privacy & Security > Full Disk Access "
            "(or > Removable Volumes)."
        ) from exc
    except OSError as exc:
        raise ScanError(f"{root} unreadable: {exc.strerror}") from exc
    entries: dict[str, Entry] = {}
    errors: list[str] = []
    collisions: list[str] = []

    def on_error(exc: OSError) -> None:
        errors.append(f"{exc.filename}: {exc.strerror}")

    def record(path: Path, rel: str, is_dir: bool) -> None:
        try:
            st = os.stat(path, follow_symlinks=follow_symlinks)
        except OSError as exc:
            errors.append(f"{path}: {exc.strerror}")
            return
        key = norm_key(rel)
        if key in entries:
            collisions.append(f"{entries[key].rel} / {rel}")
            return
        entries[key] = Entry(
            rel=rel, is_dir=is_dir, size=0 if is_dir else st.st_size, mtime=st.st_mtime
        )

    walker = os.walk(root, topdown=True, onerror=on_error, followlinks=follow_symlinks)
    for dirpath, dirnames, filenames in walker:
        base = Path(dirpath)
        rel_dir = "" if base == root else base.relative_to(root).as_posix()
        kept: list[str] = []
        for name in dirnames:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if is_excluded(rel, name, excludes):
                continue
            kept.append(name)
            record(base / name, rel, True)
        dirnames[:] = kept
        for name in filenames:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            if is_excluded(rel, name, excludes):
                continue
            record(base / name, rel, False)
        if len(entries) % PROGRESS_EVERY < len(filenames) + 1:
            log.info("cataloguing: %s entries...", human_int(len(entries)))
    return entries, errors, collisions


def _read_text(path: Path) -> str:
    """Read a catalogue, compressed or not — detected by magic bytes, not name.

    A catalogue renamed .json while actually gzipped stays readable.
    """
    data = path.read_bytes()
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data.decode("utf-8")


def read_catalog(path: Path) -> Catalog:
    """THE single entry point to reading a catalogue."""
    try:
        raw = json.loads(_read_text(path))
        return Catalog(
            root=raw["root"],
            label=raw.get("label") or path.stem,
            scanned_at=raw.get("scanned_at", "?"),
            entries={k: Entry(**v) for k, v in raw["entries"].items()},
            errors=list(raw.get("errors", [])),
            collisions=list(raw.get("collisions", [])),
            source=str(path),
            enclosure=str(raw.get("enclosure", "")),
            hostname=str(raw.get("hostname", "")),
            shelf_version=str(raw.get("shelf_version", "")),
            platform=str(raw.get("platform", "")),
            filesystem=str(raw.get("filesystem", "")),
            excludes=_as_str_list(raw.get("excludes")),
        )
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise CatalogError(f"unreadable catalogue {path}: {exc}") from exc


def write_catalog(path: Path, catalog: Catalog) -> None:
    """Written via a temp file in the same dir + os.replace: never truncated."""
    payload = {
        "format": CATALOG_FORMAT,
        "label": catalog.label,
        "enclosure": catalog.enclosure,
        "hostname": catalog.hostname,
        "shelf_version": catalog.shelf_version,
        "platform": catalog.platform,
        "filesystem": catalog.filesystem,
        "root": catalog.root,
        "scanned_at": catalog.scanned_at,
        # Keys identical to mirror_diff.py's: a catalogue can serve it directly
        # as side A or B, with no need to rescan the disk.
        "entries": {k: asdict(v) for k, v in catalog.entries.items()},
        "errors": catalog.errors,
        "collisions": catalog.collisions,
        "excludes": catalog.excludes,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic_bytes(path, gzip.compress(data) if _wants_gzip(path) else data)


def _wants_gzip(path: Path) -> bool:
    return path.suffix.lower() == ".gz"


def _write_atomic_bytes(path: Path, data: bytes) -> None:
    """Temp file in the same dir + os.replace: never a half-written file."""
    parent = path.parent if str(path.parent) else Path(".")
    with tempfile.NamedTemporaryFile(
        "wb", dir=parent, prefix=f".{path.name}.", delete=False
    ) as tmp:
        tmp.write(data)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def build_ghost(
    dest: Path, catalog: Catalog, *, sparse: bool = True
) -> tuple[int, int]:
    """Recreate the tree as sparse files: ls, find, du and the Finder all work.

    A sparse file declares its true size without occupying blocks (APFS).
    On a filesystem without sparse files (exFAT), use sparse=False.
    """
    dest.mkdir(parents=True, exist_ok=True)
    everything = catalog.entries.values()
    dirs = sorted((e for e in everything if e.is_dir), key=lambda e: e.rel)
    files = [e for e in everything if not e.is_dir]
    for entry in dirs:
        (dest / entry.rel).mkdir(parents=True, exist_ok=True)
    for entry in files:
        path = dest / entry.rel
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            if sparse and entry.size:
                os.truncate(handle.fileno(), entry.size)
        os.utime(path, (entry.mtime, entry.mtime))
    (dest / GHOST_MARKER).write_text(
        json.dumps(
            {
                "warning": "Ghost: the files are empty, only "
                "the tree is real.",
                "label": catalog.label,
                "enclosure": catalog.enclosure,
                "root": catalog.root,
                "scanned_at": catalog.scanned_at,
                "hostname": catalog.hostname,
                "shelf_version": catalog.shelf_version,
                "platform": catalog.platform,
                "filesystem": catalog.filesystem,
                **ghost_identity(catalog),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    # Directory dates last, deepest first: creating a child updates its
    # parent's mtime.
    for entry in sorted(dirs, key=lambda e: e.rel.count("/"), reverse=True):
        os.utime(dest / entry.rel, (entry.mtime, entry.mtime))
    return len(dirs), len(files)


def read_env() -> dict[str, str]:
    """THE single entry point to the environment — patch this, not os.environ."""
    return dict(os.environ)


def run(cmd: list[str], *, timeout: float | None = DEFAULT_TIMEOUT) -> str:
    """THE single entry point to subprocesses. Always bounded by a timeout, and
    always a list of arguments — never shell=True, so no injection is possible
    from a volume name."""
    log.debug("run: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandTimeout(cmd, 124, f"timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise CommandError(cmd, result.returncode, result.stderr.strip())
    return result.stdout.strip()


def require_tools(*tools: str) -> None:
    """Preflight: a clear `tool not found` beats a cryptic FileNotFoundError."""
    missing = [t for t in tools if shutil.which(t) is None]
    if missing:
        raise ScanError(f"tool(s) not found on PATH: {', '.join(missing)}")


def detect_system() -> str:
    """THE single entry point to sys.platform — patch this, not sys."""
    return sys.platform


def current_user() -> str:
    """THE single entry point to the account name, for /media/<user> probing."""
    try:
        return getpass.getuser()
    except (OSError, KeyError):  # no passwd entry (some containers)
        return read_env().get("USER") or "user"


def detect_filesystem(path: Path) -> str:
    """Best effort filesystem type of the volume holding `path`, else "".

    Worth recording: knowing a source disk was exFAT is what later explains
    two-second timestamp drift to whoever reads the catalogue.
    Never fatal - an unknown filesystem is simply not reported.
    """
    try:
        target = mount_point(path.resolve(), os.path.ismount)
        if detect_system() == "darwin":
            raw = run(["diskutil", "info", "-plist", str(target)], timeout=10.0)
            info = plistlib.loads(raw.encode("utf-8"))
            return str(info.get("FilesystemType") or info.get("FilesystemName") or "")
        return run(["findmnt", "-no", "FSTYPE", "--target", str(target)], timeout=10.0)
    except (CommandError, OSError, ValueError, plistlib.InvalidFileException):
        return ""


def platform_label() -> str:
    """A short, human-readable stamp of the machine: "macos 24.4.0 arm64"."""
    try:
        name = platform_from_system(detect_system())
    except PlatformError:
        name = detect_system()
    return f"{name} {platform.release()} {platform.machine()}".strip()


def read_hostname() -> str:
    """THE single entry point to the machine name, to record who scanned."""
    return platform.node().split(".")[0] or "?"


def mounted_volumes(mount_root: Path) -> set[str]:
    """THE single entry point to the mount root: what is plugged in right now.

    The boot disk is discarded: "Macintosh HD" is always listed under /Volumes
    and has no business in a removable enclosure. It is recognised by its
    st_dev, identical to that of "/". (You can still catalogue it directly:
    `shelf scan /`.)
    """
    try:
        boot = os.stat("/").st_dev
        candidates = list(mount_root.iterdir())
    except OSError as exc:
        log.warning("%s unreadable: %s", mount_root, exc.strerror)
        return set()
    found: set[str] = set()
    for path in candidates:
        try:
            if path.is_dir() and os.stat(path).st_dev != boot:
                found.add(path.name)
        except OSError:
            continue  # volume being unmounted, or permission denied
    return found


def time_machine_excluded(path: Path) -> bool:
    """`tmutil isexcluded` answers `[Excluded]` or "[Included]`."""
    return run(["tmutil", "isexcluded", str(path)]).startswith("[Excluded]")


def exclude_from_time_machine(path: Path) -> None:
    run(["tmutil", "addexclusion", str(path)])


def read_fleet(path: Path) -> Fleet:
    """THE single entry point to the config. Missing = empty fleet, not error.

    Transparently adopts a legacy enclosures.toml sitting next to it, so an
    existing fleet keeps working without anyone having to rename a file.
    """
    if not path.exists():
        legacy = path.parent / LEGACY_CONFIG_NAME
        if legacy.is_file():
            log.warning("adopting %s - you can delete it once %s looks right",
                        legacy, path.name)
            path = legacy
        else:
            return Fleet()
    try:
        return parse_fleet(tomllib.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise CatalogError(f"unreadable config {path}: {exc}") from exc


def write_fleet(path: Path, fleet: Fleet) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic_bytes(path, dump_fleet(fleet).encode("utf-8"))


def find_catalogs(base: Path) -> list[Path]:
    """Every catalogue in the fleet, sorted — what `find` reads by default."""
    root = base / CATALOGS_DIRNAME
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.rglob("*.json*") if p.is_file() and not p.name.startswith(".")
    )


def read_ghost_marker(dest: Path) -> dict[str, str]:
    """THE single entry point to a ghost's marker. Absent or broken = empty."""
    try:
        raw = json.loads((dest / GHOST_MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def remove_ghost(dest: Path) -> None:
    """Delete a ghost — and NOTHING else.

    Deletion is allowed only if the marker is there: without that guard, a typo
    in a label would recursively erase a real directory.
    """
    if not dest.exists():
        return
    if dest.is_symlink() or not dest.is_dir():
        raise GhostError(f"{dest} is not a directory - deletion refused")
    if not (dest / GHOST_MARKER).is_file():
        raise GhostError(
            f"{dest} exists but is not a shelf ghost "
            f"({GHOST_MARKER} missing) - deletion refused"
        )
    shutil.rmtree(dest)


# --- Typed errors ---

class ScanError(RuntimeError):
    """The disk to catalogue is not reachable."""


class CatalogError(RuntimeError):
    """A catalogue file is missing, corrupt, or in an unknown format."""


class QueryError(RuntimeError):
    """A search criterion is unreadable (size, date, sort key)."""


class GhostError(RuntimeError):
    """A ghost destination is unusable."""


class PlatformError(RuntimeError):
    """shelf is running somewhere it does not support."""


class CommandError(RuntimeError):
    def __init__(self, cmd: list[str], code: int, stderr: str) -> None:
        super().__init__(f"`{' '.join(cmd)}` failed (exit {code}): {stderr}")
        self.cmd, self.code, self.stderr = cmd, code, stderr


class CommandTimeout(CommandError):
    """A subprocess exceeded its timeout; main() turns it into exit 124.

    A distinct type rather than a CommandError carrying 124: the exit code is a
    property of the failure CATEGORY. main() must catch it BEFORE CommandError,
    which would otherwise shadow it — `except` matches in source order.
    """


# --- Orchestration: one function per subcommand ---

def resolve_base(base: Path | None, *, env: dict[str, str] | None = None) -> Path:
    """Where config, catalogues and shortcuts live. Priority: --base >
    SHELF_HOME > the script's directory. That last default is deliberate: put
    on iCloud, the script carries its fleet along and behaves identically on
    both Macs."""
    if base is not None:
        return base.expanduser().resolve()
    env = read_env() if env is None else env
    if env.get("SHELF_HOME"):
        return Path(env["SHELF_HOME"]).expanduser().resolve()
    return Path(__file__).resolve().parent


@dataclass(frozen=True)
class Context:
    """Base directory plus resolved platform: what every command starts from."""

    base: Path
    platform: Platform
    fleet: Fleet = field(default_factory=Fleet)

    @property
    def config_path(self) -> Path:
        return self.base / CONFIG_NAME


def build_context(args: argparse.Namespace) -> Context:
    """Resolve the base, then the platform, config overrides included.

    `--platform` is honoured only where it makes sense (shortcuts, config):
    the parser simply does not offer it on scan or save, where forcing a
    foreign platform onto a real disk could only do damage.
    """
    base = resolve_base(args.base)
    forced = getattr(args, "platform", None)
    name = forced or platform_from_system(detect_system())
    fleet = read_fleet(base / CONFIG_NAME)
    plat = build_platform(
        name,
        user=current_user(),
        env=read_env(),
        overrides=fleet.platforms.get(name),
    )
    ghost_root = getattr(args, "ghost_root", None)
    mount_root = getattr(args, "mount_root", None)
    if ghost_root or mount_root:
        plat = Platform(
            name=plat.name,
            mount_root=mount_root or plat.mount_root,
            ghost_root=ghost_root or plat.ghost_root,
            shortcut_suffix=plat.shortcut_suffix,
            excluder=plat.excluder,
        )
    return Context(base=base, platform=plat, fleet=fleet)


def ensure_ghost_root(platform: Platform) -> Path:
    """Create the ghost root and opt it out of backups - once, after checking.

    Only macOS has something to opt out of; elsewhere there is nothing to do
    and nothing to warn about. A tmutil failure is not fatal (Time Machine may
    not be configured): warn and carry on, the ghost stays usable.
    """
    root = platform.ghost_root.expanduser()
    root.mkdir(parents=True, exist_ok=True)
    if platform.excluder != "tmutil":
        return root
    try:
        require_tools("tmutil")
        if time_machine_excluded(root):
            log.debug("%s is already excluded from Time Machine", root)
        else:
            exclude_from_time_machine(root)
            log.warning("%s added to the Time Machine exclusions", root)
    except (CommandError, ScanError) as exc:
        log.warning("could not exclude from Time Machine: %s", exc)
    return root


def _scan_one(
    root: Path,
    *,
    label: str,
    enclosure: str,
    excludes: tuple[str, ...],
    follow_symlinks: bool,
    allow_empty: bool = False,
) -> Catalog:
    log.info("cataloguing %s", root)
    entries, errors, collisions = scan_tree(
        root, excludes=excludes, follow_symlinks=follow_symlinks
    )
    for message in errors[:5]:
        log.warning("unread: %s", message)
    if len(errors) > 5:
        log.warning(
            "... and %s more read error(s)", human_int(len(errors) - 5)
        )
    if not entries and errors and not allow_empty:
        # Nothing read AND errors: the walk failed, it does not describe the
        # disk. A REALLY empty disk yields zero entries and zero errors — it goes
        # through with no flag. Protecting an already-written inventory is handled
        # separately by _guard_overwrite().
        raise ScanError(
            f"{root}: read no entries and {human_int(len(errors))} "
            "read error(s) — the walk failed, catalogue NOT written. Use "
            "--allow-empty to write it anyway."
        )
    return Catalog(
        root=str(root),
        label=label,
        scanned_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        entries=entries,
        errors=errors,
        collisions=collisions,
        enclosure=enclosure,
        hostname=read_hostname(),
        shelf_version=__version__,
        platform=platform_label(),
        filesystem=detect_filesystem(root),
        excludes=list(excludes),
    )


def _guard_overwrite(
    destination: Path, catalog: Catalog, *, allow_empty: bool = False
) -> None:
    """Refuse to replace a stocked inventory with an empty catalogue.

    This is THE dreaded loss: the disk is unplugged, the catalogue is the only
    copy, and a walk yielding zero entries would erase it. Writing an empty
    catalogue is still allowed when there is nothing to lose — first scan, or
    a previous catalogue that is itself empty.
    """
    if catalog.entries or allow_empty or not destination.exists():
        return
    try:
        previous = read_catalog(destination)
    except CatalogError:
        return  # previous catalogue unreadable: nothing left to protect
    if previous.entries:
        raise ScanError(
            f"{catalog.label}: the walk read nothing while the existing catalogue "
            f"describes {human_int(len(previous.entries))} entry(ies) - "
            "catalogue NOT written, the previous one is kept. Use --allow-empty "
            "if the disk really was emptied."
        )


def _make_ghost(catalog: Catalog, ghost_root: Path, *, empty: bool = False) -> Path:
    """Rebuild a catalogue's ghost, replacing the previous one.

    `ghost_root` is assumed already prepared by ensure_ghost_root(): the caller
    does it ONCE, not once per disk — otherwise `save` on a five-disk enclosure
    would query tmutil five times for nothing.
    """
    dest = ghost_root / catalog.label
    remove_ghost(dest)
    n_dirs, n_files = build_ghost(dest, catalog, sparse=not empty)
    log.info(
        "ghost %s: %s directories, %s files",
        dest,
        human_int(n_dirs),
        human_int(n_files),
    )
    return dest


def config_excludes(fleet: Fleet, label: str) -> tuple[str, ...]:
    """What shelf.toml says to skip for one catalogue: global, then its own."""
    return tuple(
        dict.fromkeys(
            [*fleet.global_excludes, *fleet.catalogue_excludes.get(label, [])]
        )
    )


def _excludes_from(
    args: argparse.Namespace, fleet: Fleet, label: str
) -> tuple[str, ...]:
    """Built-ins, config and CLI are additive; each half opts out on its own.

    A set, not a scalar: "also skip this one" is the useful gesture, so --exclude
    adds to the config rather than replacing it the way --mount-root replaces a
    path. Order is irrelevant - is_excluded is an any() - so dedup is free.
    """
    defaults = () if args.no_default_excludes else DEFAULT_EXCLUDES
    config = () if args.no_config_excludes else config_excludes(fleet, label)
    return tuple(dict.fromkeys([*defaults, *config, *args.exclude]))


def cmd_scan(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    base = ctx.base
    root = args.folder.expanduser().resolve()
    label = args.label or root.name or str(root)
    enclosure = args.enclosure or ""
    catalog = _scan_one(
        root,
        label=label,
        enclosure=enclosure,
        excludes=_excludes_from(args, ctx.fleet, label),
        follow_symlinks=args.follow_symlinks,
        allow_empty=args.allow_empty,
    )
    destination = args.output or base / catalog_relpath(enclosure, label)
    _guard_overwrite(destination, catalog, allow_empty=args.allow_empty)
    write_catalog(destination, catalog)
    print(f"Catalogue written: {destination}")
    print(render_info(catalog))

    # Automatic registration: only a volume mounted directly under the mount
    # root has a stable name for `shelf save` to reuse later.
    if enclosure and root.parent == ctx.platform.mount_root:
        _register(ctx, enclosure, root.name, label)
    elif enclosure:
        log.warning(
            "%s is not a volume of %s: not registered in the config",
            root,
            ctx.platform.mount_root,
        )

    if args.ghost:
        dest = _make_ghost(catalog, ensure_ghost_root(ctx.platform), empty=args.empty)
        print(f"Ghost            : {dest}")
    return 0


def _register(ctx: Context, enclosure: str, volume: str, label: str) -> None:
    """Record a volume in the config, keeping this platform's paths with it."""
    fleet = read_fleet(ctx.config_path)
    fleet = register_volume(fleet, enclosure, volume, label)
    fleet = _with_platform(fleet, ctx.platform)
    write_fleet(ctx.config_path, fleet)
    log.info("%s registered in %s of %s", volume, enclosure, ctx.config_path)


def _with_platform(fleet: Fleet, platform: Platform) -> Fleet:
    """Stamp the running platform's resolved paths into the config.

    Written on every config update so the file always documents what this
    machine actually uses - and so the other machine can read it.
    """
    platforms = dict(fleet.platforms)
    platforms[platform.name] = platform_as_config(platform)
    return replace(fleet, platforms=platforms)


def cmd_save(args: argparse.Namespace) -> int:
    """Scan and ghost every volume of an enclosure or a group."""
    ctx = build_context(args)
    base = ctx.base
    fleet = read_fleet(ctx.config_path)
    targets = resolve_volumes(fleet, args.name)
    if not targets:
        log.warning("%s declares no volume", args.name)
        return 0
    mounted = mounted_volumes(ctx.platform.mount_root)
    ghost_root = None if args.no_ghost else ensure_ghost_root(ctx.platform)
    done: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    for enclosure, volume in targets:
        if volume not in mounted:
            # An unplugged disk keeps its catalogue and its ghost: absence is not
            # a deletion.
            skipped.append(volume)
            log.warning("%s absent - catalogue and ghost kept", volume)
            continue
        label = label_of(fleet, volume)
        destination = base / catalog_relpath(enclosure, label)
        try:
            catalog = _scan_one(
                ctx.platform.mount_root / volume,
                label=label,
                enclosure=enclosure,
                excludes=_excludes_from(args, fleet, label),
                follow_symlinks=args.follow_symlinks,
                allow_empty=args.allow_empty,
            )
            _guard_overwrite(destination, catalog, allow_empty=args.allow_empty)
        except ScanError as exc:
            # An unreadable disk must not sink the others, nor have its catalogue
            # replaced by an empty one.
            failed.append(volume)
            log.error("%s: %s", volume, exc)
            continue
        write_catalog(destination, catalog)
        line = f"  {enclosure}/{label}: {human_int(len(catalog.files))} files, "
        line += human_size(catalog.total_size)
        if ghost_root is not None:
            line += f"  -> {_make_ghost(catalog, ghost_root, empty=args.empty)}"
        print(line)
        done.append(label)
    print(
        f"{args.name}: {human_int(len(done))} volume(s) done, "
        f"{human_int(len(skipped))} absent(s), {human_int(len(failed))} failed"
    )
    if skipped:
        print(f"  absent: {', '.join(skipped)}")
    if failed:
        print(f"  FAILED (catalogue kept): {', '.join(failed)}")
        return 1
    return 0


def cmd_shortcuts(args: argparse.Namespace) -> int:
    """Write one clickable .command per enclosure and per group."""
    ctx = build_context(args)
    fleet = read_fleet(ctx.config_path)
    targets = sorted(fleet.enclosures) + sorted(fleet.groups)
    if not targets:
        raise QueryError(
            f"no enclosure declared in {ctx.config_path} - see `shelf scan "
            f"--enclosure` or `shelf enclosure add`"
        )
    outdir = (args.output or ctx.base / SHORTCUTS_DIRNAME).expanduser()
    outdir.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    rel = os.path.relpath(script, outdir)
    # A rambling relative path ("../../../../..") would be unreadable and
    # brittle: beyond one level up, freeze the absolute path.
    ref = str(script) if rel.startswith("../../") else f"$DIR/{rel}"
    for target in targets:
        path = outdir / f"save-{target}{ctx.platform.shortcut_suffix}"
        script_text = shortcut_script(ref, target, args.save_option)
        _write_atomic_bytes(path, script_text.encode("utf-8"))
        path.chmod(0o755)
        print(f"  {path}")
    script.chmod(script.stat().st_mode | 0o111)  # the shortcut executes it directly
    print(
        f"{human_int(len(targets))} {ctx.platform.name} shortcut(s) "
        f"written to {outdir}"
    )
    return 0


def _catalogued_dates(base: Path) -> dict[str, str]:
    dates: dict[str, str] = {}
    for path in find_catalogs(base):
        try:
            catalog = read_catalog(path)
        except CatalogError as exc:
            log.warning("%s", exc)
            continue
        dates[catalog.label] = catalog.scanned_at
    return dates


def cmd_enclosure_list(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    fleet = read_fleet(ctx.config_path)
    if args.as_json:
        print(json.dumps(asdict(fleet), ensure_ascii=False))
        return 0
    mounted = mounted_volumes(ctx.platform.mount_root)
    for line in fleet_report(fleet, mounted, _catalogued_dates(ctx.base)):
        print(line)
    return 0


def cmd_enclosure_add(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    fleet = read_fleet(ctx.config_path)
    volumes = args.volume or sorted(mounted_volumes(ctx.platform.mount_root))
    if not volumes:
        raise QueryError(f"no mounted volume under {ctx.platform.mount_root}")
    for volume in volumes:
        name = Path(volume).name
        fleet = register_volume(fleet, args.enclosure, name, label_of(fleet, name))
        print(f"  {name} -> {args.enclosure}")
    write_fleet(ctx.config_path, _with_platform(fleet, ctx.platform))
    print(f"Config updated: {ctx.config_path}")
    return 0


def cmd_enclosure_rm(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    fleet = read_fleet(ctx.config_path)
    for volume in args.volume:
        name = Path(volume).name
        if not enclosure_of(fleet, name):
            log.warning("%s was not declared anywhere", name)
        fleet = unregister_volume(fleet, name)
        print(f"  {name} removed")
    write_fleet(ctx.config_path, _with_platform(fleet, ctx.platform))
    print(f"Config updated: {ctx.config_path}")
    return 0


def cmd_group_add(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    fleet = set_group(read_fleet(ctx.config_path), args.name, args.enclosure)
    write_fleet(ctx.config_path, _with_platform(fleet, ctx.platform))
    print(f"{args.name} = {', '.join(fleet.groups[args.name])}")
    print(f"Config updated: {ctx.config_path}")
    return 0


def cmd_group_rm(args: argparse.Namespace) -> int:
    ctx = build_context(args)
    fleet = drop_group(read_fleet(ctx.config_path), args.name)
    write_fleet(ctx.config_path, _with_platform(fleet, ctx.platform))
    print(f"{args.name} deleted - config updated: {ctx.config_path}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    """Print the platform shelf detected and every path it will actually use."""
    ctx = build_context(args)
    if args.as_json:
        print(
            json.dumps(
                {
                    "platform": ctx.platform.name,
                    "base": str(ctx.base),
                    "config": str(ctx.config_path),
                    "catalogs": str(ctx.base / CATALOGS_DIRNAME),
                    "shortcuts": str(ctx.base / SHORTCUTS_DIRNAME),
                    "shortcut_suffix": ctx.platform.shortcut_suffix,
                    "mount_root": str(ctx.platform.mount_root),
                    "ghost_root": str(ctx.platform.ghost_root.expanduser()),
                    "excluder": ctx.platform.excluder,
                },
                ensure_ascii=False,
            )
        )
        return 0
    print(render_platform(ctx.platform, base=ctx.base))
    return 0


def filter_catalog(catalog: Catalog, patterns: tuple[str, ...]) -> Catalog:
    """The catalogue as [excludes] says it should be seen."""
    return replace(catalog, entries=filter_entries(catalog.entries, patterns))


def _view_fleet(args: argparse.Namespace) -> Fleet:
    """The config, for read commands that never build a full Context.

    A config that will not parse must not stop you browsing an unplugged disk:
    the catalogues are the data, shelf.toml is only a lens over them.
    """
    if getattr(args, "no_excludes", False):
        return Fleet()
    try:
        return read_fleet(resolve_base(args.base) / CONFIG_NAME)
    except CatalogError as exc:
        log.warning("%s - browsing without [excludes]", exc)
        return Fleet()


def _view(
    catalog: Catalog, fleet: Fleet, args: argparse.Namespace
) -> tuple[Catalog, int]:
    """Hide what [excludes] hides, and count what was hidden.

    A rule added to the config applies to catalogues written before it, which is
    the point - no re-scan, no disk. The count is what keeps that honest.
    """
    if getattr(args, "no_excludes", False):
        return catalog, 0
    patterns = config_excludes(fleet, catalog.label)
    if not patterns:
        return catalog, 0
    shown = filter_catalog(catalog, patterns)
    return shown, len(catalog.entries) - len(shown.entries)


def _warn_hidden(hidden: int) -> None:
    """Say so on stderr: a filtered listing must never look exhaustive."""
    if hidden:
        log.warning(
            "%s entry(ies) hidden by [excludes] in %s - --no-excludes shows them",
            human_int(hidden), CONFIG_NAME,
        )


def _start_key(args: argparse.Namespace, shown: Catalog, whole: Catalog) -> str:
    """Resolve the starting path, telling "excluded" from "absent" apart.

    Looked up in the unfiltered catalogue on failure: "path not in the
    catalogue" would be a lie for a folder the config is hiding, and would send
    the reader looking for a disk problem that does not exist.
    """
    start = norm_key(args.path.strip("/")) if args.path else ""
    if start and start not in shown.entries:
        if start in whole.entries:
            raise QueryError(
                f"{args.path} is hidden by [excludes] in {CONFIG_NAME} "
                "(--no-excludes to see it)"
            )
        raise QueryError(f"path not in the catalogue: {args.path}")
    return start


def _catalog_paths(args: argparse.Namespace) -> list[Path]:
    """The catalogues to read: those given as arguments, else the whole fleet."""
    if args.catalogue:
        return list(args.catalogue)
    base = resolve_base(args.base)
    found = find_catalogs(base)
    if not found:
        raise CatalogError(
            f"no catalogue under {base / CATALOGS_DIRNAME} - run `shelf scan`"
        )
    log.info("%s catalogue(s) under %s", human_int(len(found)), base)
    return found


def cmd_info(args: argparse.Namespace) -> int:
    fleet = _view_fleet(args)
    total_hidden = 0
    for index, path in enumerate(_catalog_paths(args)):
        catalog, hidden = _view(read_catalog(path), fleet, args)
        total_hidden += hidden
        if args.as_json:
            print(
                json.dumps(
                    {
                        "source": catalog.source,
                        "label": catalog.label,
                        "enclosure": catalog.enclosure,
                        "hostname": catalog.hostname,
                        "shelf_version": catalog.shelf_version,
                        "platform": catalog.platform,
                        "filesystem": catalog.filesystem,
                        "root": catalog.root,
                        "scanned_at": catalog.scanned_at,
                        "files": len(catalog.files),
                        "directories": len(catalog.dirs),
                        "bytes": catalog.total_size,
                        "errors": catalog.errors,
                        "collisions": catalog.collisions,
                        "not_scanned": catalog.excludes,
                        "hidden": hidden,
                    },
                    ensure_ascii=False,
                )
            )
            continue
        if index:
            print()
        print(f"[{catalog.source}]")
        print(render_info(catalog))
    _warn_hidden(total_hidden)
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    whole = read_catalog(args.catalogue)
    catalog, hidden = _view(whole, _view_fleet(args), args)
    children = index_children(catalog.entries)
    start = _start_key(args, catalog, whole)
    if start and not catalog.entries[start].is_dir:
        items = [catalog.entries[start]]
    else:
        items = [catalog.entries[k] for k in children.get(start, [])]
    items = sort_entries(items, key=args.sort, reverse=args.reverse)
    _warn_hidden(hidden)
    if args.as_json:
        print(json.dumps([asdict(e) for e in items], ensure_ascii=False))
        return 0
    for entry in items:
        shown = entry.name + ("/" if entry.is_dir else "")
        print(format_long(entry, show=shown) if args.long else shown)
    log.info("%s entry(ies)", human_int(len(items)))
    return 0


def _build_query(args: argparse.Namespace) -> Query:
    return Query(
        name=args.name,
        path=args.path,
        kind=args.type,
        min_size=parse_size(args.min_size) if args.min_size else None,
        max_size=parse_size(args.max_size) if args.max_size else None,
        newer=parse_date(args.newer) if args.newer else None,
        older=parse_date(args.older) if args.older else None,
        under=args.under or "",
        case_sensitive=args.case_sensitive,
    )


def cmd_find(args: argparse.Namespace) -> int:
    query = _build_query(args)
    paths = _catalog_paths(args)
    multi = len(paths) > 1
    found = 0
    fleet = _view_fleet(args)
    total_hidden = 0
    payload: list[dict[str, object]] = []
    for path in paths:
        catalog, hidden = _view(read_catalog(path), fleet, args)
        if args.enclosure and catalog.enclosure != args.enclosure:
            continue
        total_hidden += hidden
        hits = sort_entries(
            search(catalog.entries, query), key=args.sort, reverse=args.reverse
        )
        for entry in hits:
            if args.limit and found >= args.limit:
                log.warning("limit of %s result(s) reached", human_int(args.limit))
                _warn_hidden(total_hidden)
                return 0
            found += 1
            marque = f"[{catalog.display}] " if multi else ""
            if args.as_json:
                payload.append(
                    {
                        "catalogue": catalog.label,
                        "enclosure": catalog.enclosure,
                        **asdict(entry),
                    }
                )
            elif args.long:
                print(format_long(entry, prefix=marque))
            else:
                print(f"{marque}{entry.rel}")
    if args.as_json:
        print(json.dumps(payload, ensure_ascii=False))
    log.info("%s result(s)", human_int(found))
    if not found:
        log.warning("no result")
    _warn_hidden(total_hidden)
    return 0


def cmd_du(args: argparse.Namespace) -> int:
    whole = read_catalog(args.catalogue)
    catalog, hidden = _view(whole, _view_fleet(args), args)
    totals = dir_totals(catalog.entries)
    start = _start_key(args, catalog, whole)
    depth = args.depth
    base_depth = start.count("/") + 1 if start else 0
    rows = [
        (catalog.entries[key].rel, size, count)
        for key, (size, count) in totals.items()
        if key
        and key in catalog.entries
        and (not start or key.startswith(start + "/"))
        and key.count("/") - base_depth < depth
    ]
    rows.sort(key=lambda row: row[1], reverse=True)
    _warn_hidden(hidden)
    if args.top:
        rows = rows[: args.top]
    if args.as_json:
        print(
            json.dumps(
                [{"path": r, "bytes": s, "files": c} for r, s, c in rows],
                ensure_ascii=False,
            )
        )
        return 0
    root_size, root_count = totals.get(start, (0, 0))
    for rel, size, count in rows:
        print(f"{human_size(size):>10}  {human_int(count):>9} fich.  {rel}")
    label = args.path or "."
    print(f"{human_size(root_size):>10}  {human_int(root_count):>9} fich.  {label}")
    return 0


def cmd_tree(args: argparse.Namespace) -> int:
    whole = read_catalog(args.catalogue)
    catalog, hidden = _view(whole, _view_fleet(args), args)
    children = index_children(catalog.entries)
    start = _start_key(args, catalog, whole)
    _warn_hidden(hidden)
    print(args.path or catalog.root)
    lines = render_tree(
        catalog.entries, children, start=start, depth=args.depth, show_size=args.long
    )
    for line in lines:
        print(line)
    log.info("%s ligne(s)", human_int(len(lines)))
    return 0


def cmd_ghost_all(args: argparse.Namespace) -> int:
    """Rebuild every ghost in the fleet from the catalogues on this machine.

    The point of the two-machine workflow: catalogues travel (kilobytes), the
    ghosts are rebuilt locally (seconds). Nothing that carries a fake size is
    ever transferred.
    """
    ctx = build_context(args)
    paths = find_catalogs(ctx.base)
    if not paths:
        raise CatalogError(
            f"no catalogue under {ctx.base / CATALOGS_DIRNAME} - run `shelf scan`"
        )
    root = ensure_ghost_root(ctx.platform)
    rebuilt: list[str] = []
    current: list[str] = []
    failed: list[str] = []
    total_hidden = 0
    for path in paths:
        try:
            catalog, hidden = _view(read_catalog(path), ctx.fleet, args)
            total_hidden += hidden
            dest = root / catalog.label
            if not args.force and ghost_is_current(read_ghost_marker(dest), catalog):
                current.append(catalog.label)
                log.info("%s already current", catalog.display)
                continue
            _make_ghost(catalog, root, empty=args.empty)
            print(
                f"  {catalog.display}: {human_int(len(catalog.files))} files, "
                f"{human_size(catalog.total_size)} -> {dest}"
            )
            rebuilt.append(catalog.label)
        except (CatalogError, GhostError, OSError) as exc:
            # One unreadable catalogue must not stop the rest of the fleet.
            failed.append(path.name)
            log.error("%s: %s", path.name, exc)
    _warn_hidden(total_hidden)
    print(
        f"{human_int(len(rebuilt))} rebuilt, {human_int(len(current))} already "
        f"current, {human_int(len(failed))} failed"
    )
    if failed:
        print(f"  FAILED: {', '.join(failed)}")
        return 1
    return 0


def cmd_ghost(args: argparse.Namespace) -> int:
    if args.all:
        if args.catalogue is not None or args.destination is not None:
            raise QueryError(
                "--all rebuilds the whole fleet: pass no catalogue and no "
                "destination"
            )
        return cmd_ghost_all(args)
    if args.catalogue is None:
        raise QueryError(
            "give a catalogue file, or --all to rebuild every ghost in the fleet"
        )
    catalog, hidden = _view(read_catalog(args.catalogue), _view_fleet(args), args)
    _warn_hidden(hidden)
    if args.destination is not None:
        dest = args.destination.expanduser()
        if dest.exists() and any(dest.iterdir()):
            remove_ghost(dest)  # refuses if it is not a shelf ghost
    else:
        dest = ensure_ghost_root(build_context(args).platform) / catalog.label
        remove_ghost(dest)
    log.info(
        "rebuilding %s entries into %s",
        human_int(len(catalog.entries)),
        dest,
    )
    n_dirs, n_files = build_ghost(dest, catalog, sparse=not args.empty)
    print(f"Ghost created: {dest}")
    print(f"  {human_int(n_dirs)} directories, {human_int(n_files)} empty files")
    declared = human_size(catalog.total_size)
    print(f"  Declared size: {declared} (real footprint ~0)")
    print("  ls, find, du and the Finder all work on it normally.")
    return 0


# --- Journalisation + couleur ---

def should_color(
    stream: object, mode: str = "auto", *, env: dict[str, str] | None = None
) -> bool:
    """Priority: --color > NO_COLOR/FORCE_COLOR > TTY detection."""
    if mode == "always":
        return True
    if mode == "never":
        return False
    env = read_env() if env is None else env
    if env.get("NO_COLOR") is not None:
        return False
    if env.get("FORCE_COLOR"):
        return True
    is_tty = bool(getattr(stream, "isatty", lambda: False)())
    return is_tty and env.get("TERM") != "dumb"


class ColorFormatter(logging.Formatter):
    COLORS = {"ERROR": "31", "WARNING": "33", "INFO": "36", "DEBUG": "2"}

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        code = self.COLORS.get(record.levelname)
        return f"\x1b[{code}m{msg}\x1b[0m" if code else msg


def setup_logging(verbosity: int, *, color: str = "auto") -> None:
    """0 -> WARNING; -v INFO; -vv DEBUG; -q ERROR; -qq CRITICAL. All to stderr."""
    level = max(logging.DEBUG, min(logging.CRITICAL, logging.WARNING - 10 * verbosity))
    handler = logging.StreamHandler(sys.stderr)
    fmt = "%(levelname)s: %(message)s"
    handler.setFormatter(
        ColorFormatter(fmt)
        if should_color(sys.stderr, color)
        else logging.Formatter(fmt)
    )
    logging.basicConfig(level=level, handlers=[handler])


def install_signal_handlers() -> None:
    """SIGTERM -> 143, to unwind finally/with blocks cleanly."""
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))


# --- CLI ---

def _add_general(parser: argparse.ArgumentParser) -> None:
    general = parser.add_argument_group("general")
    general.add_argument(
        "-h", "--help", action="help", help="Show this help message and exit"
    )
    general.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Increase verbosity (-v = info, -vv = debug); logs go to stderr",
    )
    general.add_argument(
        "-q", "--quiet", action="count", default=0,
        help="Decrease verbosity (-q = errors only, -qq = silent)",
    )
    general.add_argument(
        "--color", choices=("auto", "always", "never"), default="auto",
        help="Colorize logs: auto (default), always, never",
    )
    general.add_argument(
        "--json", action="store_true", dest="as_json",
        help="Emit JSON on stdout instead of text",
    )
    general.add_argument(
        "--base", type=Path, default=None, metavar="DIRECTORY",
        help="Root of the fleet (config, catalogs/, shortcuts/); defaults to "
             "$SHELF_HOME, else the directory holding shelf.py",
    )


def _add_scan_options(parser: argparse.ArgumentParser) -> None:
    """Walk options shared by `scan` and `save`."""
    group = parser.add_argument_group("walk")
    group.add_argument(
        "--exclude", action="append", default=[], metavar="PATTERN",
        help="Pattern to ignore, repeatable; matched on the name, or on the path "
             "if it contains a '/'",
    )
    group.add_argument(
        "--no-default-excludes", action="store_true",
        help="Do not ignore macOS/Windows junk (.DS_Store, ._*, ...)",
    )
    group.add_argument(
        "--no-config-excludes", action="store_true",
        help=f"Ignore the [excludes] section of {CONFIG_NAME}",
    )
    group.add_argument(
        "--follow-symlinks", action="store_true",
        help="Follow symlinks (default: treat them as plain entries)",
    )
    group.add_argument(
        "--mount-root", type=Path, default=None, metavar="DIRECTORY",
        help="Where removable volumes are mounted (default: this platform's, "
             "see `shelf config`)",
    )
    group.add_argument(
        "--allow-empty", action="store_true",
        help="Allow writing an empty catalogue; by default a walk that reads "
             "nothing is refused, so an existing inventory is never overwritten",
    )


def _add_view_options(parser: argparse.ArgumentParser) -> None:
    """Reading options shared by every command that opens a catalogue."""
    parser.add_argument(
        "--no-excludes", action="store_true",
        help=f"Show everything the catalogue holds, ignoring the [excludes] "
             f"section of {CONFIG_NAME}",
    )


def _add_ghost_options(parser: argparse.ArgumentParser) -> None:
    """Ghost options shared by `scan`, `save` and `ghost`."""
    group = parser.add_argument_group("ghost")
    group.add_argument(
        "--ghost-root", type=Path, default=None, metavar="DIRECTORY",
        help="Where to put ghosts (default: this platform's, see `shelf config`); "
             "local to each machine and kept out of backups, never inside a "
             "synced folder",
    )
    group.add_argument(
        "--empty", action="store_true",
        help="Zero-byte files instead of sparse ones (required when the target is "
             "exFAT/FAT, which cannot make holes)",
    )


def _add_platform_flag(parser: argparse.ArgumentParser) -> None:
    """Only on commands that produce files, never on ones that read a disk.

    Forcing a foreign platform onto `scan` or `save` could only mislead: those
    read real volumes, and the mount root is not a matter of preference.
    """
    parser.add_argument(
        "--platform", choices=PLATFORMS, default=None,
        help="Target another platform's conventions, e.g. generate .sh "
             "shortcuts for the Linux machine from a Mac",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        add_help=False,
        epilog=(
            "Plug the disk in once to catalogue it, then browse it whenever you "
            "like: shelf ls/find/du/tree read the catalogue alone. "
            "shelf ghost recreates the tree as empty files so that ls, find and "
            "the Finder work on it as-is. The format is that of mirror_diff.py "
            "snapshots: a catalogue serves it directly as side A or B."
        ),
    )
    parser.add_argument(
        "-h", "--help", action="help", help="Show this help message and exit"
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}",
        help="Show the program version and exit",
    )
    subs = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    scan = subs.add_parser(
        "scan", add_help=False, help="Catalogue a folder or a plugged-in disk"
    )
    scan.add_argument("folder", type=Path, help="The folder or volume to catalogue")
    scan.add_argument(
        "--enclosure", default=None, metavar="ENCLOSURE",
        help="Enclosure this disk belongs to; files the catalogue under "
             "catalogs/<ENCLOSURE>/ and registers the volume in the config",
    )
    scan.add_argument(
        "--label", default=None,
        help="Readable name for the disk (default: the volume name)",
    )
    scan.add_argument(
        "-o", "--output", type=Path, default=None, metavar="CATALOGUE",
        help="Explicit catalogue path; defaults to "
             "<base>/catalogs/<enclosure>/<label>.json.gz",
    )
    scan.add_argument(
        "--ghost", action="store_true",
        help="Also build the ghost in --ghost-root, replacing the previous "
             "one",
    )
    _add_scan_options(scan)
    _add_ghost_options(scan)
    _add_general(scan)
    scan.set_defaults(func=cmd_scan)

    save = subs.add_parser(
        "save", add_help=False,
        help="Scan and ghost every mounted volume of an enclosure or a group",
    )
    save.add_argument(
        "name", help="Name of an enclosure (Enclosure1) or a group (macmini)"
    )
    save.add_argument(
        "--no-ghost",
        action="store_true",
        help="Catalogue without building the ghosts",
    )
    _add_scan_options(save)
    _add_ghost_options(save)
    _add_general(save)
    save.set_defaults(func=cmd_save)

    shortcuts = subs.add_parser(
        "shortcuts", add_help=False,
        help="Generate one clickable .command per enclosure and per group",
    )
    shortcuts.add_argument(
        "-o", "--output", type=Path, default=None, metavar="DIRECTORY",
        help=f"Where to write the shortcuts (default <base>/{SHORTCUTS_DIRNAME}/)",
    )
    shortcuts.add_argument(
        "--save-option", action="append", default=[], metavar="OPTION",
        help="Extra option to pass to `save` inside the shortcuts, repeatable. "
             "Write it with an '=', otherwise argparse takes the value for an "
             "option: --save-option=--no-ghost",
    )
    _add_platform_flag(shortcuts)
    _add_general(shortcuts)
    shortcuts.set_defaults(func=cmd_shortcuts)

    config = subs.add_parser(
        "config", add_help=False,
        help="Show the detected platform and every path shelf will use",
    )
    _add_platform_flag(config)
    _add_general(config)
    config.set_defaults(func=cmd_config)

    enclosure = subs.add_parser(
        "enclosure", add_help=False, help="Declare and inspect enclosures"
    )
    enclosure.add_argument(
        "-h", "--help", action="help", help="Show this help message and exit"
    )
    esubs = enclosure.add_subparsers(dest="action", required=True, metavar="ACTION")

    elist = esubs.add_parser(
        "list", add_help=False, help="Fleet status: plugged in, catalogued, when"
    )
    _add_general(elist)
    elist.set_defaults(func=cmd_enclosure_list)

    eadd = esubs.add_parser(
        "add", add_help=False, help="Attach volumes to an enclosure"
    )
    eadd.add_argument("enclosure", help="Enclosure name, e.g. Enclosure1")
    eadd.add_argument(
        "volume", nargs="*",
        help="Volume names (default: every one currently mounted)",
    )
    _add_general(eadd)
    eadd.set_defaults(func=cmd_enclosure_add)

    erm = esubs.add_parser("rm", add_help=False, help="Remove volumes from the fleet")
    erm.add_argument("volume", nargs="+", help="Volume names to remove")
    _add_general(erm)
    erm.set_defaults(func=cmd_enclosure_rm)

    group = subs.add_parser(
        "group", add_help=False, help="Group enclosures under a single name"
    )
    group.add_argument(
        "-h", "--help", action="help", help="Show this help message and exit"
    )
    gsubs = group.add_subparsers(dest="action", required=True, metavar="ACTION")

    gadd = gsubs.add_parser("add", add_help=False, help="Create or redefine a group")
    gadd.add_argument("name", help="Group name, e.g. macmini")
    gadd.add_argument("enclosure", nargs="+", help="Enclosures it is made of")
    _add_general(gadd)
    gadd.set_defaults(func=cmd_group_add)

    grm = gsubs.add_parser("rm", add_help=False, help="Delete a group")
    grm.add_argument("name", help="Group name")
    _add_general(grm)
    grm.set_defaults(func=cmd_group_rm)

    info = subs.add_parser(
        "info", add_help=False, help="Summary of one or more catalogues"
    )
    info.add_argument(
        "catalogue", type=Path, nargs="*",
        help="Catalogue file(s) (default: the whole fleet under <base>/catalogs/)",
    )
    _add_view_options(info)
    _add_general(info)
    info.set_defaults(func=cmd_info)

    ls = subs.add_parser("ls", add_help=False, help="List the contents of a directory")
    ls.add_argument("catalogue", type=Path, help="Catalogue file")
    ls.add_argument(
        "path",
        nargs="?",
        default="",
        help="Path inside the catalogue (default: the root)",
    )
    ls.add_argument(
        "-l", "--long", action="store_true", help="Size and date, ls -l style"
    )
    ls.add_argument(
        "--sort",
        choices=("name", "size", "date"),
        default="name",
        help="Sort key",
    )
    ls.add_argument("-r", "--reverse", action="store_true", help="Reverse the sort")
    _add_view_options(ls)
    _add_general(ls)
    ls.set_defaults(func=cmd_ls)

    find = subs.add_parser(
        "find", add_help=False,
        help="Search one or more catalogues (tells you which disk)",
    )
    find.add_argument(
        "catalogue", type=Path, nargs="*",
        help="Catalogue file(s) (default: the whole fleet under <base>/catalogs/)",
    )
    filters = find.add_argument_group("filters")
    filters.add_argument("--name", default=None, metavar="PATTERN",
                          help="Pattern on the file name, e.g. '*.raw'")
    filters.add_argument("--path", default=None, metavar="PATTERN",
                          help="Pattern on the full path, e.g. '2024/*/[Pp]hotos/*'")
    filters.add_argument("--under", default="", metavar="PATH",
                          help="Restrict to this subdirectory")
    filters.add_argument("--type", choices=("any", "f", "d"), default="any",
                          help="f = files only, d = directories only")
    filters.add_argument("--min-size", default=None, metavar="SIZE",
                          help="Minimum size, e.g. 700M")
    filters.add_argument("--max-size", default=None, metavar="SIZE",
                          help="Maximum size, e.g. 10k")
    filters.add_argument("--newer", default=None, metavar="DATE",
                          help="Modified after this date, e.g. 2024-06-15")
    filters.add_argument("--older", default=None, metavar="DATE",
                          help="Modified before this date")
    filters.add_argument("--case-sensitive", action="store_true",
                          help="Respect case (default: case-insensitive)")
    filters.add_argument("--enclosure", default=None, metavar="ENCLOSURE",
                          help="Search only the catalogues of this enclosure")
    display = find.add_argument_group("display")
    display.add_argument("-l", "--long", action="store_true", help="Size and date")
    display.add_argument("--sort", choices=("name", "size", "date"), default="name",
                           help="Sort key")
    display.add_argument(
        "-r", "--reverse", action="store_true", help="Reverse the sort"
    )
    display.add_argument("--limit", type=int, default=0, metavar="N",
                           help="Stop after N results (0 = all)")
    _add_view_options(find)
    _add_general(find)
    find.set_defaults(func=cmd_find)

    du = subs.add_parser(
        "du", add_help=False, help="What weighs the most, by directory"
    )
    du.add_argument("catalogue", type=Path, help="Catalogue file")
    du.add_argument(
        "path", nargs="?", default="", help="Subdirectory (default: the root)"
    )
    du.add_argument("--depth", type=int, default=1, metavar="N",
                    help="Detail depth (default 1)")
    du.add_argument("--top", type=int, default=0, metavar="N",
                    help="Only the N biggest (0 = all)")
    _add_view_options(du)
    _add_general(du)
    du.set_defaults(func=cmd_du)

    tree = subs.add_parser("tree", add_help=False, help="Draw the tree")
    tree.add_argument("catalogue", type=Path, help="Catalogue file")
    tree.add_argument(
        "path", nargs="?", default="", help="Subdirectory (default: the root)"
    )
    tree.add_argument("--depth", type=int, default=3, metavar="N",
                      help="Maximum depth (default 3)")
    tree.add_argument("-l", "--long", action="store_true", help="Show sizes")
    _add_view_options(tree)
    _add_general(tree)
    tree.set_defaults(func=cmd_tree)

    ghost = subs.add_parser(
        "ghost", add_help=False,
        help="Recreate the tree as empty files, usable by ls/find/Finder",
    )
    ghost.add_argument(
        "catalogue", type=Path, nargs="?", default=None,
        help="Catalogue file (omit it with --all)",
    )
    ghost.add_argument(
        "destination", type=Path, nargs="?", default=None,
        help="Directory to rebuild the tree into (default <ghost-root>/<label>)",
    )
    ghost.add_argument(
        "--all", action="store_true",
        help="Rebuild every ghost in the fleet from the local catalogues, "
             "skipping the ones already up to date",
    )
    ghost.add_argument(
        "--force", action="store_true",
        help="With --all, rebuild even the ghosts that are already current",
    )
    _add_view_options(ghost)
    _add_ghost_options(ghost)
    _add_general(ghost)
    ghost.set_defaults(func=cmd_ghost)
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(args.verbose - args.quiet, color=args.color)
    install_signal_handlers()
    try:
        code = int(args.func(args))
        sys.stdout.flush()
    except CommandTimeout as exc:
        # Must precede CommandError: `except` matches in source order, and
        # CommandTimeout is a subclass. That ordering is what makes 124 reachable.
        log.error("%s", exc)
        return 124
    except (
        ScanError,
        CatalogError,
        QueryError,
        GhostError,
        PlatformError,
        CommandError,
    ) as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except (OSError, ValueError):
            pass
        return 141
    return code


if __name__ == "__main__":
    sys.exit(main())
