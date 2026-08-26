"""Tests for shelf.py.

Run: uv run --with pytest pytest test_shelf.py
"""

# noqa I001: with no pyproject.toml, ruff assumes an old target-version and
# classe `tomllib` (stdlib depuis 3.11) en tiers-partie.
from __future__ import annotations  # noqa: I001

import contextlib
import gzip
import importlib.util
import io
import json
import logging
import os
import shutil
import signal
import sys
import tomllib
import types
import unicodedata
from collections.abc import Iterator
from pathlib import Path

import pytest

# --- Boilerplate: import a PEP 723 script as a module ---
# A uv-header script is not on sys.path: we load it by path. Register it in
# sys.modules BEFORE exec_module, or dataclasses (which look the module up by
# name) fail to resolve under `from __future__ import annotations`.
_SCRIPT = Path(__file__).parent / "shelf.py"
_spec = importlib.util.spec_from_file_location("script_under_test", _SCRIPT)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)


# --- Harnais ---

@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    for var in ("NO_COLOR", "FORCE_COLOR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TERM", "xterm")


@contextlib.contextmanager
def _isolated_root_logger() -> Iterator[logging.Logger]:
    """logging.basicConfig() no-ops when the root already owns a handler - and
    pytest always installs one. Without this clear, only the first case passes."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers.clear()
    try:
        yield root
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def _platform(*, name: str = "macos", mount_root: Path | None = None,
              ghost_root: Path | None = None, excluder: str = "tmutil"):
    return mod.Platform(
        name=name,
        mount_root=mount_root or Path("/Volumes"),
        ghost_root=ghost_root or Path("~/Volumes"),
        shortcut_suffix=".command" if name == "macos" else ".sh",
        excluder=excluder,
    )


def _entry(rel: str, *, is_dir: bool = False, size: int = 10, mtime: float = 1000.0):
    return mod.Entry(rel=rel, is_dir=is_dir, size=size, mtime=mtime)


def _entries(*items: object) -> dict[str, object]:
    return {mod.norm_key(e.rel): e for e in items}  # type: ignore[attr-defined]


def _catalog(*items, root: str = "/Volumes/Disk", label: str = "Disk",
             scanned_at: str = "2026-01-01 00:00:00"):
    return mod.Catalog(
        root=root,
        label=label,
        scanned_at=scanned_at,
        entries={mod.norm_key(e.rel): e for e in items},
    )


def _tree(base: Path, spec: dict[str, str | None]) -> Path:
    """Build a tree: value None = directory, str = file contents."""
    base.mkdir(parents=True, exist_ok=True)
    for rel, content in spec.items():
        path = base / rel
        if content is None:
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    return base


# --- norm_key: the NFD/NFC Unicode trap ---

def test_norm_key_unifies_nfd_and_nfc():
    """An accented name catalogued from APFS (NFD) must be findable in NFC."""
    nfd = unicodedata.normalize("NFD", "Été à Nice.heic")
    nfc = unicodedata.normalize("NFC", "Été à Nice.heic")
    assert nfd != nfc
    assert mod.norm_key(nfd) == mod.norm_key(nfc)


# --- parse_size / parse_date ---

@pytest.mark.parametrize(
    ("text", "expected"),
    [("100", 100), ("4k", 4096), ("700M", 700 << 20), ("2G", 2 << 30),
     ("1.5G", int(1.5 * (1 << 30))), ("10MB", 10 << 20), ("10MiB", 10 << 20)],
)
def test_parse_size(text, expected):
    assert mod.parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "beaucoup", "12X", "M"])
def test_parse_size_rejects_garbage(text):
    with pytest.raises(mod.QueryError):
        mod.parse_size(text)


@pytest.mark.parametrize(
    ("text", "iso"),
    [("2024", "2024-01-01 00:00"), ("2024-06", "2024-06-01 00:00"),
     ("2024-06-15", "2024-06-15 00:00"), ("2024-06-15 08:30", "2024-06-15 08:30")],
)
def test_parse_date(text, iso):
    assert mod.human_time(mod.parse_date(text)) == iso


def test_parse_date_rejects_garbage():
    with pytest.raises(mod.QueryError):
        mod.parse_date("hier")


# --- Index et cumuls ---

def test_index_children_groups_by_direct_parent():
    entries = _entries(
        _entry("a", is_dir=True), _entry("a/b", is_dir=True),
        _entry("a/b/f.txt"), _entry("z.txt"),
    )
    index = mod.index_children(entries)
    assert index[""] == ["a", "z.txt"]
    assert index["a"] == ["a/b"]
    assert index["a/b"] == ["a/b/f.txt"]


def test_dir_totals_rolls_sizes_up_through_every_ancestor():
    entries = _entries(
        _entry("a", is_dir=True), _entry("a/b", is_dir=True),
        _entry("a/b/f.txt", size=100), _entry("a/g.txt", size=5),
    )
    totals = mod.dir_totals(entries)
    assert totals["a/b"] == (100, 1)
    assert totals["a"] == (105, 2)
    assert totals[""] == (105, 2)


def test_dir_totals_ignores_directory_sizes():
    """A directory counts as 0 in the catalogue: counting it would skew totals."""
    entries = _entries(_entry("a", is_dir=True, size=4096), _entry("a/f", size=7))
    assert mod.dir_totals(entries)["a"] == (7, 1)


# --- matches: all of find's semantics, pure ---

def test_matches_name_glob_is_case_insensitive_by_default():
    entry = _entry("Photos/IMG_01.JPG")
    assert mod.matches(entry, mod.Query(name="*.jpg")) is True
    assert mod.matches(entry, mod.Query(name="*.jpg", case_sensitive=True)) is False


def test_matches_name_applies_to_the_basename_not_the_path():
    assert mod.matches(_entry("Photos/a.txt"), mod.Query(name="Photos*")) is False
    assert mod.matches(_entry("Photos/a.txt"), mod.Query(path="Photos*")) is True


def test_matches_name_finds_an_nfd_name_typed_in_nfc():
    entry = _entry(unicodedata.normalize("NFD", "Été.heic"))
    assert mod.matches(entry, mod.Query(name=unicodedata.normalize("NFC", "*té*")))


@pytest.mark.parametrize(
    ("kind", "is_dir", "expected"),
    [("any", True, True), ("any", False, True), ("f", True, False),
     ("f", False, True), ("d", True, True), ("d", False, False)],
)
def test_matches_type(kind, is_dir, expected):
    assert mod.matches(_entry("x", is_dir=is_dir), mod.Query(kind=kind)) is expected


def test_matches_size_filter_excludes_directories():
    """Without this rule, --min-size returns the entire tree: a directory
    weighs 0 in the catalogue and would clear every "at least" threshold."""
    query = mod.Query(min_size=1)
    assert mod.matches(_entry("f", size=10), query) is True
    assert mod.matches(_entry("d", is_dir=True, size=0), query) is False


@pytest.mark.parametrize(
    ("size", "low", "high", "expected"),
    [(100, 50, None, True), (100, 200, None, False),
     (100, None, 200, True), (100, None, 50, False)],
)
def test_matches_size_bounds(size, low, high, expected):
    query = mod.Query(min_size=low, max_size=high)
    assert mod.matches(_entry("f", size=size), query) is expected


def test_matches_date_bounds():
    entry = _entry("f", mtime=mod.parse_date("2024-06-15"))
    assert mod.matches(entry, mod.Query(newer=mod.parse_date("2024-01-01")))
    assert not mod.matches(entry, mod.Query(newer=mod.parse_date("2025-01-01")))
    assert mod.matches(entry, mod.Query(older=mod.parse_date("2025-01-01")))
    assert not mod.matches(entry, mod.Query(older=mod.parse_date("2024-01-01")))


def test_matches_under_restricts_to_a_subtree_without_prefix_bleed():
    assert mod.matches(_entry("Photos/a.jpg"), mod.Query(under="Photos"))
    assert not mod.matches(_entry("PhotosBis/a.jpg"), mod.Query(under="Photos"))
    assert not mod.matches(_entry("Docs/a.jpg"), mod.Query(under="Photos"))


def test_search_combines_criteria():
    entries = _entries(
        _entry("Musique/a.flac", size=30_000_000),
        _entry("Musique/b.mp3", size=4_000_000),
        _entry("Docs/c.flac", size=30_000_000),
    )
    hits = mod.search(
        entries, mod.Query(name="*.flac", min_size=10_000_000, under="Musique")
    )
    assert [e.rel for e in hits] == ["Musique/a.flac"]


# --- Tri ---

def test_sort_entries_by_size_then_name():
    items = [_entry("b", size=1), _entry("a", size=9), _entry("c", size=1)]
    assert [e.rel for e in mod.sort_entries(items, key="size")] == ["b", "c", "a"]
    ordered = mod.sort_entries(items, key="size", reverse=True)
    assert [e.rel for e in ordered] == ["a", "c", "b"]


def test_sort_entries_rejects_an_unknown_key():
    with pytest.raises(mod.QueryError):
        mod.sort_entries([], key="couleur")


# --- Rendu ---

def test_render_tree_respects_depth():
    entries = _entries(
        _entry("a", is_dir=True), _entry("a/b", is_dir=True), _entry("a/b/c.txt")
    )
    children = mod.index_children(entries)
    assert len(mod.render_tree(entries, children, depth=1)) == 1
    assert len(mod.render_tree(entries, children, depth=3)) == 3


def test_render_tree_marks_directories_and_the_last_branch():
    entries = _entries(_entry("a", is_dir=True), _entry("z.txt"))
    lines = mod.render_tree(entries, mod.index_children(entries), depth=2)
    assert lines == ["|-- a/", "`-- z.txt"]


def test_format_long_shows_a_dash_for_directory_size():
    assert "d          -  " in mod.format_long(_entry("d", is_dir=True))
    assert "2.0 KiB" in mod.format_long(_entry("f", size=2048))


def test_format_long_show_overrides_the_displayed_text():
    line = mod.format_long(_entry("a/b/c.txt"), show="c.txt")
    assert line.endswith("c.txt") and "a/b" not in line


@pytest.mark.parametrize(
    ("size", "expected"),
    [(0, "0 B"), (512, "512 B"), (2048, "2.0 KiB"), (1 << 20, "1.0 MiB")],
)
def test_human_size(size, expected):
    assert mod.human_size(size) == expected


def test_human_int_groups_thousands():
    assert mod.human_int(1234567) == "1,234,567"


def test_render_info_lists_the_essentials():
    catalog = _catalog(_entry("f", size=100), _entry("d", is_dir=True), label="USB")
    text = mod.render_info(catalog)
    assert "USB" in text and "/Volumes/Disk" in text and "100 B" in text


# --- filter_entries: excludes applied to a flat catalogue ---

def _rels(entries):
    return {e.rel for e in entries.values()}


def test_filter_entries_drops_everything_under_an_excluded_directory():
    """The catalogue is flat: "VMs/disk.img" does not match the name "VMs"."""
    entries = _entries(
        _entry("VMs", is_dir=True),
        _entry("VMs/disk.img"),
        _entry("VMs/nested", is_dir=True),
        _entry("VMs/nested/deep.img"),
        _entry("keep.txt"),
    )
    assert _rels(mod.filter_entries(entries, ("VMs",))) == {"keep.txt"}


def test_filter_entries_keeps_a_lookalike_directory():
    entries = _entries(
        _entry("VMs", is_dir=True),
        _entry("VMs/b"),
        _entry("VMsOther", is_dir=True),
        _entry("VMsOther/a"),
    )
    kept = mod.filter_entries(entries, ("VMs",))
    assert _rels(kept) == {"VMsOther", "VMsOther/a"}


def test_filter_entries_anchors_a_subtree_on_its_directory_entry():
    """scan_tree records every directory it walks, and that entry is what the
    descendants hang off. Without it there is nothing to match a bare name
    against, so the children survive - a catalogue shelf never produces."""
    orphans = _entries(_entry("VMs/b"))  # no "VMs" entry
    assert _rels(mod.filter_entries(orphans, ("VMs",))) == {"VMs/b"}
    assert _rels(mod.filter_entries(orphans, ("VMs/*",))) == set()


def test_filter_entries_without_patterns_returns_the_input_untouched():
    entries = _entries(_entry("a.txt"))
    assert mod.filter_entries(entries, ()) is entries


def test_filter_entries_keeps_everything_when_nothing_matches():
    entries = _entries(_entry("a.txt"))
    assert mod.filter_entries(entries, ("nope",)) == entries


def test_filter_entries_accepts_a_path_pattern():
    entries = _entries(
        _entry("Photos/RAW", is_dir=True),
        _entry("Photos/RAW/img.dng"),
        _entry("Photos/img.jpg"),
    )
    kept = mod.filter_entries(entries, ("Photos/RAW",))
    assert _rels(kept) == {"Photos/img.jpg"}


# --- Exclude resolution: built-ins + config + CLI, each opt-out-able ---

def _args(exclude=(), no_default=False, no_config=False):
    return types.SimpleNamespace(
        exclude=list(exclude),
        no_default_excludes=no_default,
        no_config_excludes=no_config,
    )


def _fleet_with_excludes():
    return mod.Fleet(
        global_excludes=["node_modules"],
        catalogue_excludes={"Backup1": ["Photos/RAW"], "Backup2": ["Downloads"]},
    )


def test_config_excludes_adds_the_catalogue_list_to_the_global_one():
    fleet = _fleet_with_excludes()
    assert mod.config_excludes(fleet, "Backup1") == ("node_modules", "Photos/RAW")
    assert mod.config_excludes(fleet, "Unknown") == ("node_modules",)


def test_excludes_from_unions_all_four_sources():
    got = mod._excludes_from(_args(["mine"]), _fleet_with_excludes(), "Backup1")
    assert "mine" in got and "node_modules" in got and "Photos/RAW" in got
    assert ".DS_Store" in got  # the built-ins are still there


def test_excludes_from_drops_the_built_ins_on_demand():
    got = mod._excludes_from(_args(no_default=True), _fleet_with_excludes(), "Backup1")
    assert ".DS_Store" not in got
    assert "node_modules" in got  # the config half is untouched


def test_excludes_from_drops_the_config_on_demand():
    got = mod._excludes_from(
        _args(["mine"], no_config=True), _fleet_with_excludes(), "Backup1"
    )
    assert "node_modules" not in got and "Photos/RAW" not in got
    assert "mine" in got and ".DS_Store" in got


def test_excludes_from_does_not_repeat_a_pattern_declared_twice():
    fleet = mod.Fleet(global_excludes=["dup"], catalogue_excludes={"D": ["dup"]})
    got = mod._excludes_from(_args(["dup"]), fleet, "D")
    assert got.count("dup") == 1


# --- is_excluded: name vs path, normalised like every other match ---

def test_is_excluded_without_a_slash_matches_the_basename():
    assert mod.is_excluded("a/b/cache", "cache", ("cache",)) is True
    assert mod.is_excluded("a/b/cache", "cache", ("b",)) is False


def test_is_excluded_with_a_slash_matches_the_whole_path():
    assert mod.is_excluded("Photos/RAW", "RAW", ("Photos/RAW",)) is True
    # Anchored at the root: the same name deeper down is a different path.
    assert mod.is_excluded("a/Photos/RAW", "RAW", ("Photos/RAW",)) is False


def test_is_excluded_ignores_case():
    """APFS is case-insensitive: a pattern typed by hand must still catch."""
    assert mod.is_excluded("Photos/raw", "raw", ("Photos/RAW",)) is True
    assert mod.is_excluded(".ds_store", ".ds_store", (".DS_Store",)) is True


def test_is_excluded_unifies_nfd_and_nfc():
    """A pattern typed in NFC must catch a name APFS stored decomposed."""
    nfd = unicodedata.normalize("NFD", "Été")
    nfc = unicodedata.normalize("NFC", "Été")
    assert nfd != nfc  # otherwise the test proves nothing
    assert mod.is_excluded(nfd, nfd, (nfc,)) is True
    assert mod.is_excluded(nfc, nfc, (nfd,)) is True


def test_is_excluded_without_patterns_keeps_everything():
    assert mod.is_excluded("a", "a", ()) is False


# --- scan_tree: the filesystem boundary ---

def test_scan_tree_indexes_relative_paths(tmp_path):
    root = _tree(tmp_path / "d", {"sub": None, "sub/a.txt": "xx", "b.txt": "y"})
    entries, errors, collisions = mod.scan_tree(root, excludes=())
    assert {e.rel for e in entries.values()} == {"sub", "sub/a.txt", "b.txt"}
    assert errors == [] and collisions == []


def test_scan_tree_prunes_excluded_subtrees(tmp_path):
    root = _tree(
        tmp_path / "d",
        {".DS_Store": "j", "cache": None, "cache/big": "x", "k.txt": "y"},
    )
    entries, _, _ = mod.scan_tree(root, excludes=mod.DEFAULT_EXCLUDES + ("cache",))
    assert {e.rel for e in entries.values()} == {"k.txt"}


def test_scan_tree_rejects_an_unplugged_disk(tmp_path):
    with pytest.raises(mod.ScanError) as excinfo:
        mod.scan_tree(tmp_path / "Volumes-absent", excludes=())
    assert "is the disk plugged in" in str(excinfo.value)


# --- Catalogues : aller-retour, compression, robustesse ---

@pytest.mark.parametrize("name", ["c.json", "c.json.gz"])
def test_catalog_round_trip_plain_and_gzipped(tmp_path, name):
    catalog = _catalog(_entry("a/b.txt", size=42), _entry("a", is_dir=True))
    path = tmp_path / name
    mod.write_catalog(path, catalog)
    restored = mod.read_catalog(path)
    assert restored.entries == catalog.entries
    assert restored.root == catalog.root and restored.label == catalog.label


def test_write_catalog_gzips_only_when_the_name_says_so(tmp_path):
    catalog = _catalog(_entry("a.txt"))
    mod.write_catalog(tmp_path / "plain.json", catalog)
    mod.write_catalog(tmp_path / "zipped.json.gz", catalog)
    assert (tmp_path / "plain.json").read_bytes()[:2] != b"\x1f\x8b"
    assert (tmp_path / "zipped.json.gz").read_bytes()[:2] == b"\x1f\x8b"


def test_read_catalog_detects_gzip_by_magic_bytes_not_by_name(tmp_path):
    """A catalogue renamed .json while actually gzipped stays readable."""
    catalog = _catalog(_entry("a.txt"))
    real = tmp_path / "c.json.gz"
    mod.write_catalog(real, catalog)
    misnamed = tmp_path / "menteur.json"
    misnamed.write_bytes(real.read_bytes())
    assert mod.read_catalog(misnamed).entries == catalog.entries


def test_write_catalog_leaves_no_temp_behind(tmp_path):
    mod.write_catalog(tmp_path / "c.json.gz", _catalog(_entry("a.txt")))
    assert [p.name for p in tmp_path.iterdir()] == ["c.json.gz"]


def test_read_catalog_rejects_corruption(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json at all")
    with pytest.raises(mod.CatalogError):
        mod.read_catalog(bad)


def test_read_catalog_rejects_a_json_that_is_not_a_catalog(tmp_path):
    bad = tmp_path / "autre.json"
    bad.write_text('{"quelque": "chose"}')
    with pytest.raises(mod.CatalogError):
        mod.read_catalog(bad)


def test_catalog_is_readable_as_a_mirror_diff_snapshot(tmp_path):
    """The format is shared: mirror_diff.py must be able to use it as-is."""
    path = tmp_path / "c.json.gz"
    mod.write_catalog(path, _catalog(_entry("a.txt")))
    raw = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
    assert set(raw) >= {"root", "entries", "errors", "collisions"}
    first = next(iter(raw["entries"].values()))
    assert set(first) == {"rel", "is_dir", "size", "mtime"}


def test_catalog_records_what_was_skipped_when_it_was_built(tmp_path):
    """"No node_modules on this disk" and "node_modules was skipped" are not
    the same fact, and only the catalogue can tell them apart."""
    root = _tree(tmp_path / "d", {"keep.txt": "x", "junk": None, "junk/a": "y"})
    path = tmp_path / "c.json.gz"
    assert mod.main(["scan", str(root), "-o", str(path), "--exclude", "junk",
                     "-q"]) == 0
    catalog = mod.read_catalog(path)
    assert "junk" in catalog.excludes
    assert "Not scanned" in mod.render_info(catalog)


def test_read_catalog_of_an_older_file_without_excludes(tmp_path):
    """Catalogues written before the field existed must still load."""
    path = tmp_path / "old.json"
    path.write_text(json.dumps({
        "root": "/Volumes/Old", "label": "Old", "scanned_at": "2020-01-01 00:00:00",
        "entries": {},
    }))
    catalog = mod.read_catalog(path)
    assert catalog.excludes == []
    assert "Not scanned" not in mod.render_info(catalog)


# --- ghost: a tree the system's own tools can walk ---

def test_ghost_recreates_the_tree_with_real_sizes_and_dates(tmp_path):
    when = mod.parse_date("2024-06-15 08:30")
    catalog = _catalog(
        _entry("Photos", is_dir=True),
        _entry("Photos/img.jpg", size=3_000_000, mtime=when),
    )
    dest = tmp_path / "ghost"
    n_dirs, n_files = mod.build_ghost(dest, catalog)
    assert (n_dirs, n_files) == (1, 1)
    ghost_file = dest / "Photos/img.jpg"
    assert ghost_file.stat().st_size == 3_000_000
    assert ghost_file.stat().st_mtime == pytest.approx(when, abs=1)


def test_ghost_files_are_sparse_and_cost_no_space(tmp_path):
    catalog = _catalog(_entry("gros.bin", size=200_000_000))
    dest = tmp_path / "ghost"
    mod.build_ghost(dest, catalog)
    st = (dest / "gros.bin").stat()
    assert st.st_size == 200_000_000
    assert st.st_blocks * 512 < 1_000_000  # nothing actually allocated


def test_ghost_empty_mode_writes_zero_byte_files(tmp_path):
    catalog = _catalog(_entry("gros.bin", size=200_000_000))
    dest = tmp_path / "ghost"
    mod.build_ghost(dest, catalog, sparse=False)
    assert (dest / "gros.bin").stat().st_size == 0


def test_ghost_restores_directory_mtimes_after_filling_them(tmp_path):
    """Creating a child updates its parent's mtime: directories must be
    re-dated last, deepest first."""
    when = mod.parse_date("2020-01-02 03:04")
    catalog = _catalog(
        _entry("a", is_dir=True, mtime=when),
        _entry("a/b", is_dir=True, mtime=when),
        _entry("a/b/f.txt", size=1, mtime=when),
    )
    dest = tmp_path / "ghost"
    mod.build_ghost(dest, catalog)
    assert (dest / "a").stat().st_mtime == pytest.approx(when, abs=1)
    assert (dest / "a/b").stat().st_mtime == pytest.approx(when, abs=1)


def test_ghost_drops_a_marker_saying_the_files_are_fake(tmp_path):
    dest = tmp_path / "ghost"
    mod.build_ghost(dest, _catalog(_entry("a.txt")))
    marker = json.loads((dest / mod.GHOST_MARKER).read_text())
    assert "Ghost" in marker["warning"]
    assert marker["root"] == "/Volumes/Disk"


def test_ghost_is_walkable_by_the_real_os_walk(tmp_path):
    catalog = _catalog(
        _entry("Photos", is_dir=True), _entry("Photos/a.jpg", size=5),
        _entry("Docs", is_dir=True), _entry("Docs/b.pdf", size=7),
    )
    dest = tmp_path / "ghost"
    mod.build_ghost(dest, catalog)
    found = {
        os.path.relpath(os.path.join(d, f), dest)
        for d, _, files in os.walk(dest)
        for f in files
        if f != mod.GHOST_MARKER
    }
    assert found == {"Photos/a.jpg", "Docs/b.pdf"}


# --- The fleet: config, enclosures, groups (pure logic) ---

def test_parse_fleet_reads_the_three_tables():
    raw = {
        "enclosures": {"Enclosure1": ["Backup1", "Backup2"]},
        "groups": {"macmini": ["Enclosure1"]},
        "labels": {"Backup1": "Archives"},
    }
    fleet = mod.parse_fleet(raw)
    assert fleet.enclosures == {"Enclosure1": ["Backup1", "Backup2"]}
    assert fleet.groups == {"macmini": ["Enclosure1"]}
    assert fleet.labels == {"Backup1": "Archives"}


@pytest.mark.parametrize(
    "raw",
    [{}, {"enclosures": "not a table"}, {"enclosures": {"E1": "not a list"}}],
)
def test_parse_fleet_tolerates_a_hand_edited_file(raw):
    """The TOML is hand-editable: a typo must not break shelf."""
    fleet = mod.parse_fleet(raw)
    assert isinstance(fleet.enclosures, dict)
    assert all(isinstance(v, list) for v in fleet.enclosures.values())


def test_parse_fleet_reads_both_levels_of_excludes():
    fleet = mod.parse_fleet({
        "excludes": {
            "global": ["node_modules", "*.tmp"],
            "catalogue": {"Backup1": ["Photos/RAW"], "Backup2": ["Downloads"]},
        }
    })
    assert fleet.global_excludes == ["node_modules", "*.tmp"]
    assert fleet.catalogue_excludes == {
        "Backup1": ["Photos/RAW"], "Backup2": ["Downloads"]
    }


@pytest.mark.parametrize(
    "raw",
    [
        {"excludes": "not a table"},
        {"excludes": {"global": "not a list"}},
        {"excludes": {"catalogue": "not a table"}},
        {"excludes": {"catalogue": {"Backup1": "not a list"}}},
    ],
)
def test_parse_fleet_tolerates_a_mistyped_excludes_section(raw):
    """Same contract as the rest of the file: a typo degrades, never raises."""
    fleet = mod.parse_fleet(raw)
    assert fleet.global_excludes == []
    assert all(isinstance(v, list) for v in fleet.catalogue_excludes.values())


def test_excludes_survive_a_config_rewrite(fleet_env):
    """dump_fleet rebuilds the whole file from the Fleet: a section it does not
    model is erased by the next mutating command. This is that regression."""
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    config = fleet_env.config
    config.write_text(
        config.read_text()
        .replace("global = []", 'global = ["node_modules"]')
        .replace(
            "[excludes.catalogue]",
            '[excludes.catalogue]\n"Photo Drive" = ["RAW"]',
        )
    )
    assert _run(fleet_env, "enclosure", "add", "Enclosure2", "Backup2", "-q") == 0
    fleet = mod.read_fleet(config)
    assert fleet.global_excludes == ["node_modules"]
    assert fleet.catalogue_excludes == {"Photo Drive": ["RAW"]}


def test_fleet_round_trips_through_toml(tmp_path):
    fleet = mod.Fleet(
        enclosures={"Enclosure1": ["Backup1"], "Enclosure2": ["Photo Drive"]},
        groups={"macmini": ["Enclosure1", "Enclosure2"]},
        labels={"Photo Drive": "Photos 2024"},
    )
    path = tmp_path / "enclosures.toml"
    mod.write_fleet(path, fleet)
    assert mod.read_fleet(path) == fleet


def test_dump_fleet_quotes_keys_that_need_it():
    fleet = mod.Fleet(enclosures={"Enclosure 1": ["My Disk"]})
    text = mod.dump_fleet(fleet)
    assert '"Enclosure 1" = ["My Disk"]' in text
    assert mod.parse_fleet(tomllib.loads(text)).enclosures == fleet.enclosures


def test_dump_fleet_escapes_quotes_in_volume_names():
    fleet = mod.Fleet(enclosures={"E1": ['Disk "Personal"']})
    assert mod.parse_fleet(tomllib.loads(mod.dump_fleet(fleet))) == fleet


def test_read_fleet_of_a_missing_file_is_an_empty_fleet(tmp_path):
    assert mod.read_fleet(tmp_path / "absent.toml") == mod.Fleet()


def test_read_fleet_rejects_broken_toml(tmp_path):
    path = tmp_path / "enclosures.toml"
    path.write_text("[enclosures\nE1 = ")
    with pytest.raises(mod.CatalogError):
        mod.read_fleet(path)


def test_resolve_volumes_expands_an_enclosure():
    fleet = mod.Fleet(enclosures={"E1": ["A", "B"]})
    assert mod.resolve_volumes(fleet, "E1") == [("E1", "A"), ("E1", "B")]


def test_resolve_volumes_expands_a_group_without_duplicates():
    fleet = mod.Fleet(
        enclosures={"E1": ["A", "B"], "E2": ["B", "C"]},
        groups={"macmini": ["E1", "E2"]},
    )
    assert mod.resolve_volumes(fleet, "macmini") == [
        ("E1", "A"), ("E1", "B"), ("E2", "C")
    ]


def test_resolve_volumes_rejects_an_unknown_name():
    with pytest.raises(mod.QueryError) as excinfo:
        mod.resolve_volumes(mod.Fleet(enclosures={"E1": []}), "E9")
    assert "E1" in str(excinfo.value)  # the error lists what does exist


def test_register_volume_moves_it_instead_of_duplicating():
    """A volume in two enclosures would be scanned twice by `save`."""
    fleet = mod.register_volume(mod.Fleet(), "E1", "Backup1", "Backup1")
    fleet = mod.register_volume(fleet, "E2", "Backup1", "Backup1")
    assert fleet.enclosures["E1"] == []
    assert fleet.enclosures["E2"] == ["Backup1"]


def test_register_volume_records_a_label_only_when_it_differs():
    fleet = mod.register_volume(mod.Fleet(), "E1", "Backup1", "Backup1")
    assert fleet.labels == {}
    fleet = mod.register_volume(fleet, "E1", "Backup1", "Archives")
    assert fleet.labels == {"Backup1": "Archives"}
    fleet = mod.register_volume(fleet, "E1", "Backup1", "Backup1")
    assert fleet.labels == {}


def test_label_of_falls_back_to_the_volume_name():
    fleet = mod.Fleet(labels={"Backup1": "Archives"})
    assert mod.label_of(fleet, "Backup1") == "Archives"
    assert mod.label_of(fleet, "Backup2") == "Backup2"


def test_unregister_volume_drops_it_everywhere():
    fleet = mod.register_volume(mod.Fleet(), "E1", "A", "Alias")
    fleet = mod.unregister_volume(fleet, "A")
    assert fleet.enclosures["E1"] == [] and fleet.labels == {}


def test_set_group_rejects_an_unknown_enclosure():
    with pytest.raises(mod.QueryError):
        mod.set_group(mod.Fleet(enclosures={"E1": []}), "macmini", ["E1", "E9"])


def test_drop_group_rejects_an_unknown_group():
    with pytest.raises(mod.QueryError):
        mod.drop_group(mod.Fleet(), "macmini")


def test_catalog_relpath_uses_the_enclosure_folder():
    assert mod.catalog_relpath("Enclosure1", "Backup1") == Path(
        "catalogs/Enclosure1/Backup1.json.gz"
    )
    assert mod.catalog_relpath("", "Seul") == Path(
        f"catalogs/{mod.DEFAULT_ENCLOSURE}/Seul.json.gz"
    )


def test_shortcut_script_is_self_locating():
    """The folder can be moved, or mounted elsewhere on the other Mac."""
    text = mod.shortcut_script("$DIR/../shelf.py", "Enclosure1")
    assert 'DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"' in text
    assert '"$DIR/../shelf.py" save Enclosure1' in text
    assert text.startswith("#!/bin/bash")


def test_shortcut_script_accepts_an_absolute_reference():
    text = mod.shortcut_script("/opt/tools/shelf.py", "macmini")
    assert '"/opt/tools/shelf.py" save macmini' in text


def test_fleet_report_shows_mounted_and_catalogued_state():
    fleet = mod.Fleet(
        enclosures={"E1": ["A", "B"]}, groups={"macmini": ["E1"]}, labels={"B": "Bis"}
    )
    lines = "\n".join(mod.fleet_report(fleet, {"A"}, {"A": "2026-01-01 10:00"}))
    assert "mounted" in lines and "absent" in lines
    assert "B -> Bis" in lines
    assert "never catalogued" in lines
    assert "macmini = E1" in lines


def test_fleet_report_of_an_empty_fleet_says_what_to_do():
    assert "enclosure add" in mod.fleet_report(mod.Fleet(), set(), {})[0]


# --- Platform: one object holds everything that differs between OSes ---

@pytest.mark.parametrize(
    ("system", "expected"),
    [("darwin", "macos"), ("linux", "linux"), ("linux2", "linux")],
)
def test_platform_from_system(system, expected):
    assert mod.platform_from_system(system) == expected


@pytest.mark.parametrize("system", ["win32", "cygwin", "freebsd14"])
def test_platform_from_system_refuses_windows_and_friends(system):
    """Failing loudly beats filling someone's SSD with a 1 TB "sparse" ghost."""
    with pytest.raises(mod.PlatformError) as excinfo:
        mod.platform_from_system(system)
    assert "macOS and Linux" in str(excinfo.value)


def test_linux_mount_candidates_substitutes_the_user_at_runtime():
    """No account name is ever baked into the shipped defaults."""
    assert mod.linux_mount_candidates("alice") == [
        Path("/run/media/alice"),
        Path("/media/alice"),
        Path("/media"),
        Path("/mnt"),
    ]
    assert "alice" not in "".join(mod.LINUX_MOUNT_CANDIDATES)


def test_mount_point_climbs_to_the_volume_root():
    """`diskutil info` only answers for a mount point, not a path inside one."""
    mounts = {Path("/"), Path("/Volumes/Backup1")}
    mount = mod.mount_point(
        Path("/Volumes/Backup1/Photos/2024"), mounts.__contains__
    )
    assert mount == Path("/Volumes/Backup1")
    assert mod.mount_point(Path("/home/u/docs"), mounts.__contains__) == Path("/")


def test_mount_point_terminates_on_a_path_that_does_not_exist():
    assert mod.mount_point(Path("/a/b/c"), lambda p: False) == Path("/")


def test_config_hints_never_carry_a_real_account_name():
    """The commented template is a shape, not somebody's home directory."""
    rendered = json.dumps(mod.CONFIG_HINTS)
    assert "<user>" in rendered
    assert os.path.expanduser("~") not in rendered


def test_dump_fleet_uses_the_hints_for_the_absent_platform():
    text = mod.dump_fleet(mod.Fleet(platforms={"macos": {"mount_root": "/Volumes"}}))
    assert '# mount_root = "/run/media/<user>"' in text
    assert '# ghost_root = "~/.local/share/shelf/ghosts"' in text


def test_pick_mount_root_takes_the_first_that_exists():
    candidates = [Path("/run/media/u"), Path("/media/u"), Path("/mnt")]
    assert mod.pick_mount_root(candidates, lambda p: p == Path("/media/u")) == Path(
        "/media/u"
    )


def test_pick_mount_root_falls_back_to_the_first_so_the_error_names_it():
    candidates = [Path("/run/media/u"), Path("/mnt")]
    assert mod.pick_mount_root(candidates, lambda p: False) == Path("/run/media/u")


def test_build_platform_macos_defaults():
    plat = mod.build_platform("macos", user="u", env={})
    assert plat.mount_root == Path("/Volumes")
    assert plat.ghost_root == Path("~/Volumes")
    assert plat.shortcut_suffix == ".command"
    assert plat.excluder == "tmutil"


def test_build_platform_linux_defaults():
    plat = mod.build_platform("linux", user="u", env={}, exists=lambda p: False)
    assert plat.ghost_root == Path("~/.local/share/shelf/ghosts")
    assert plat.shortcut_suffix == ".sh"
    assert plat.excluder == ""  # nothing to opt out of outside macOS


def test_build_platform_linux_honours_xdg_data_home():
    plat = mod.build_platform(
        "linux", user="u", env={"XDG_DATA_HOME": "/data"}, exists=lambda p: False
    )
    assert plat.ghost_root == Path("/data/shelf/ghosts")


def test_build_platform_linux_probes_the_distribution_mount_point():
    plat = mod.build_platform(
        "linux", user="bob", env={}, exists=lambda p: p == Path("/media/bob")
    )
    assert plat.mount_root == Path("/media/bob")


def test_build_platform_config_overrides_the_defaults():
    plat = mod.build_platform(
        "macos", user="u", env={},
        overrides={"mount_root": "/mnt/disks", "ghost_root": "/tmp/ghosts"},
    )
    assert plat.mount_root == Path("/mnt/disks")
    assert plat.ghost_root == Path("/tmp/ghosts")
    assert plat.shortcut_suffix == ".command"  # not overridable: it is the OS


def test_build_platform_rejects_an_unknown_name():
    with pytest.raises(mod.PlatformError):
        mod.build_platform("solaris", user="u", env={})


def test_platform_as_config_round_trips_through_build_platform():
    plat = mod.build_platform("linux", user="u", env={}, exists=lambda p: False)
    same = mod.build_platform(
        "linux", user="other", env={}, overrides=mod.platform_as_config(plat)
    )
    assert same == plat


def test_render_platform_shows_every_path_that_will_be_used(tmp_path):
    text = mod.render_platform(_platform(), base=tmp_path)
    for expected in ("platform", "mount root", "ghost root", "catalogs", "shortcuts"):
        assert expected in text


def test_ensure_ghost_root_skips_the_excluder_off_macos(tmp_path, monkeypatch):
    """Linux has no Time Machine: nothing to run, nothing to warn about."""
    def boom(*a, **k):
        raise AssertionError("no backup tool should be invoked on linux")

    monkeypatch.setattr(mod, "require_tools", boom)
    monkeypatch.setattr(mod, "time_machine_excluded", boom)
    root = tmp_path / "ghosts"
    plat = _platform(name="linux", ghost_root=root, excluder="")
    assert mod.ensure_ghost_root(plat) == root
    assert root.is_dir()


# --- Config: platform sections ---

def test_fleet_round_trips_platform_sections(tmp_path):
    fleet = mod.Fleet(
        enclosures={"E1": ["A"]},
        platforms={"macos": {"mount_root": "/Volumes", "ghost_root": "~/Volumes"}},
    )
    path = tmp_path / mod.CONFIG_NAME
    mod.write_fleet(path, fleet)
    assert mod.read_fleet(path).platforms == fleet.platforms


def test_dump_fleet_comments_out_the_platform_you_are_not_on():
    fleet = mod.Fleet(platforms={"macos": {"mount_root": "/Volumes"}})
    text = mod.dump_fleet(fleet)
    assert "[platform.macos]" in text
    assert "# [platform.linux]" in text  # documented, not imposed
    assert mod.parse_fleet(tomllib.loads(text)).platforms.keys() == {"macos"}


def test_parse_fleet_ignores_an_unknown_platform_section():
    raw = {"platform": {"macos": {"mount_root": "/V"}, "amiga": {"mount_root": "DF0:"}}}
    assert set(mod.parse_fleet(raw).platforms) == {"macos"}


def test_read_fleet_adopts_a_legacy_enclosures_toml(tmp_path):
    """An existing fleet keeps working without anyone renaming a file."""
    legacy = tmp_path / mod.LEGACY_CONFIG_NAME
    mod.write_fleet(legacy, mod.Fleet(enclosures={"E1": ["Backup1"]}))
    fleet = mod.read_fleet(tmp_path / mod.CONFIG_NAME)
    assert fleet.enclosures == {"E1": ["Backup1"]}


def test_read_fleet_prefers_the_new_name_when_both_exist(tmp_path):
    legacy = tmp_path / mod.LEGACY_CONFIG_NAME
    mod.write_fleet(legacy, mod.Fleet(enclosures={"old": []}))
    mod.write_fleet(tmp_path / mod.CONFIG_NAME, mod.Fleet(enclosures={"new": []}))
    assert set(mod.read_fleet(tmp_path / mod.CONFIG_NAME).enclosures) == {"new"}


# --- resolve_base: where the fleet comes from ---

def test_resolve_base_prefers_the_flag(tmp_path):
    assert mod.resolve_base(tmp_path, env={"SHELF_HOME": "/elsewhere"}) == tmp_path


def test_resolve_base_then_the_env_var(tmp_path):
    assert mod.resolve_base(None, env={"SHELF_HOME": str(tmp_path)}) == tmp_path


def test_resolve_base_falls_back_to_the_script_directory():
    """Deliberate default: put on iCloud, the script carries its fleet along."""
    assert mod.resolve_base(None, env={}) == _SCRIPT.resolve().parent


# --- System boundaries: /Volumes, tmutil, ghost deletion ---

def test_mounted_volumes_excludes_the_boot_disk(tmp_path, monkeypatch):
    """"Macintosh HD" is always in /Volumes and is not a removable disk."""
    fake = tmp_path / "Volumes"
    (fake / "Externe").mkdir(parents=True)
    (fake / "Macintosh HD").mkdir()
    boot_dev = os.stat(fake / "Macintosh HD").st_dev
    monkeypatch.setattr(mod.os, "stat", _stat_with_boot(boot_dev, fake / "Externe"))
    assert mod.mounted_volumes(fake) == {"Externe"}


def _stat_with_boot(boot_dev, external):
    """An os.stat that makes `external` look like a different device from /."""
    real = os.stat

    class _Fake:
        def __init__(self, st, dev):
            self._st, self.st_dev = st, dev

        def __getattr__(self, name):
            return getattr(self._st, name)

    def fake_stat(path, *args, **kwargs):
        st = real(path, *args, **kwargs)
        if str(path) == str(external):
            return _Fake(st, boot_dev + 1)
        return _Fake(st, boot_dev)

    return fake_stat


def test_run_raises_a_typed_error_on_failure():
    with pytest.raises(mod.CommandError) as excinfo:
        mod.run(["false"])
    assert excinfo.value.code != 0


def test_run_timeout_becomes_a_command_timeout():
    with pytest.raises(mod.CommandTimeout):
        mod.run(["sleep", "5"], timeout=0.05)


def test_require_tools_lists_everything_missing(monkeypatch):
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    with pytest.raises(mod.ScanError) as excinfo:
        mod.require_tools("tmutil", "inexistant")
    assert "tmutil" in str(excinfo.value) and "inexistant" in str(excinfo.value)


@pytest.mark.parametrize(
    ("output", "expected"), [("[Excluded]  /x", True), ("[Included]  /x", False)]
)
def test_time_machine_excluded_reads_tmutil(monkeypatch, output, expected):
    monkeypatch.setattr(mod, "run", lambda *a, **k: output)
    assert mod.time_machine_excluded(Path("/x")) is expected


def test_ensure_ghost_root_excludes_only_once(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/stub")
    monkeypatch.setattr(mod, "time_machine_excluded", lambda p: bool(calls))
    monkeypatch.setattr(
        mod, "exclude_from_time_machine", lambda p: calls.append([str(p)])
    )
    root = tmp_path / "Volumes"
    plat = _platform(ghost_root=root)
    mod.ensure_ghost_root(plat)
    mod.ensure_ghost_root(plat)
    assert root.is_dir()
    assert len(calls) == 1


def test_ensure_ghost_root_survives_a_tmutil_failure(tmp_path, monkeypatch):
    """Time Machine may not be configured: the ghost stays usable."""
    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/stub")

    def boom(path):
        raise mod.CommandError(["tmutil"], 1, "no backup configured")

    monkeypatch.setattr(mod, "time_machine_excluded", boom)
    root = mod.ensure_ghost_root(_platform(ghost_root=tmp_path / "Volumes"))
    assert root.is_dir()


def test_remove_ghost_refuses_a_folder_that_is_not_a_ghost(tmp_path):
    """The guard: a typo in a label must not erase anything."""
    real = tmp_path / "Documents importants"
    (real / "subdir").mkdir(parents=True)
    (real / "these.pdf").write_text("mon travail")
    with pytest.raises(mod.GhostError) as excinfo:
        mod.remove_ghost(real)
    assert mod.GHOST_MARKER in str(excinfo.value)
    assert (real / "these.pdf").exists()


def test_remove_ghost_deletes_a_real_ghost(tmp_path):
    dest = tmp_path / "Backup1"
    mod.build_ghost(dest, _catalog(_entry("a/b.txt")))
    mod.remove_ghost(dest)
    assert not dest.exists()


def test_remove_ghost_is_a_no_op_on_a_missing_folder(tmp_path):
    mod.remove_ghost(tmp_path / "jamais-existe")


def test_remove_ghost_refuses_a_symlink(tmp_path):
    target = tmp_path / "vrai"
    target.mkdir()
    link = tmp_path / "lien"
    link.symlink_to(target)
    with pytest.raises(mod.GhostError):
        mod.remove_ghost(link)
    assert target.exists()


def test_find_catalogs_walks_the_enclosure_folders(tmp_path):
    for enclosure, label in (("E1", "A"), ("E1", "B"), ("E2", "C")):
        mod.write_catalog(
            tmp_path / mod.catalog_relpath(enclosure, label), _catalog(_entry("f"))
        )
    found = mod.find_catalogs(tmp_path)
    assert [p.name for p in found] == ["A.json.gz", "B.json.gz", "C.json.gz"]


def test_find_catalogs_of_an_empty_base_is_empty(tmp_path):
    assert mod.find_catalogs(tmp_path) == []


# --- CLI ---

def test_a_subcommand_is_required():
    with pytest.raises(SystemExit) as excinfo:
        mod.parse_args([])
    assert excinfo.value.code == 2


def test_version_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        mod.parse_args(["--version"])
    assert excinfo.value.code == 0
    assert mod.__version__ in capsys.readouterr().out


def test_parse_args_dispatches_to_the_right_command():
    assert mod.parse_args(["ls", "c.json"]).func is mod.cmd_ls
    assert mod.parse_args(["find", "c.json"]).func is mod.cmd_find
    assert mod.parse_args(["ghost", "c.json", "d"]).func is mod.cmd_ghost


@pytest.mark.parametrize(
    ("verbosity", "level"),
    [(-2, logging.CRITICAL), (-1, logging.ERROR), (0, logging.WARNING),
     (1, logging.INFO), (2, logging.DEBUG), (5, logging.DEBUG), (-9, logging.CRITICAL)],
)
def test_setup_logging_maps_and_clamps_verbosity(verbosity, level):
    with _isolated_root_logger() as root:
        mod.setup_logging(verbosity)
        assert root.level == level


class _FakeStream:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.mark.parametrize(
    ("tty", "mode", "env", "expected"),
    [
        (True, "auto", {"TERM": "xterm"}, True),
        (False, "auto", {"TERM": "xterm"}, False),
        (False, "always", {}, True),
        (True, "never", {}, False),
        (True, "auto", {"NO_COLOR": "", "TERM": "xterm"}, False),
        (False, "auto", {"FORCE_COLOR": "1"}, True),
        (True, "auto", {"TERM": "dumb"}, False),
    ],
)
def test_should_color(tty, mode, env, expected):
    assert mod.should_color(_FakeStream(tty), mode, env=env) is expected


def test_color_formatter_wraps_the_level_name():
    record = logging.LogRecord("t", logging.ERROR, "f", 1, "boum", None, None)
    assert "\x1b[31m" in mod.ColorFormatter("%(levelname)s: %(message)s").format(record)


def test_sigterm_handler_exits_143():
    mod.install_signal_handlers()
    handler = signal.getsignal(signal.SIGTERM)
    assert callable(handler)
    with pytest.raises(SystemExit) as excinfo:
        handler(signal.SIGTERM, None)
    assert excinfo.value.code == 143


# --- main: end to end, on a real temporary directory ---

@pytest.fixture
def disk(tmp_path):
    """A small disk and its catalogue, ready for the command tests."""
    root = _tree(
        tmp_path / "disk",
        {
            "Photos": None,
            "Photos/img.jpg": "x" * 5000,
            "Photos/2024": None,
            "Photos/2024/vieux.jpg": "y" * 100,
            "Docs/note.txt": "z" * 10,
            ".DS_Store": "junk",
        },
    )
    catalogue = tmp_path / "c.json.gz"
    scan = ["scan", str(root), "-o", str(catalogue), "--label", "USB", "-q"]
    assert mod.main(scan) == 0
    return root, catalogue


def test_main_scan_then_read_without_the_disk(disk, tmp_path, capsys):
    root, catalogue = disk
    capsys.readouterr()
    import shutil

    shutil.rmtree(root)  # the disk is unplugged
    assert mod.main(["ls", str(catalogue)]) == 0
    assert capsys.readouterr().out.split() == ["Docs/", "Photos/"]
    assert mod.main(["find", str(catalogue), "--name", "*.jpg"]) == 0
    out = capsys.readouterr().out
    assert "Photos/img.jpg" in out and "Photos/2024/vieux.jpg" in out


def test_main_scan_skips_macos_junk(disk, capsys):
    _, catalogue = disk
    mod.main(["find", str(catalogue), "--name", "*"])
    assert ".DS_Store" not in capsys.readouterr().out


def test_main_ls_rejects_an_unknown_path(disk, capsys):
    _, catalogue = disk
    with _isolated_root_logger():
        assert mod.main(["ls", str(catalogue), "Nimportequoi"]) == 1
    assert "not in the catalogue" in capsys.readouterr().err


def test_main_ls_shows_names_not_paths(disk, capsys):
    _, catalogue = disk
    capsys.readouterr()
    mod.main(["ls", str(catalogue), "Photos", "-l"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert [line.split()[-1] for line in lines] == ["2024/", "img.jpg"]


def test_main_du_ranks_the_heaviest_first(disk, capsys):
    _, catalogue = disk
    capsys.readouterr()
    mod.main(["du", str(catalogue), "--depth", "1"])
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[0].endswith("Photos")
    assert lines[-1].endswith(".")  # the total line


def test_main_find_across_two_catalogues_says_which_disk(tmp_path, capsys):
    for name in ("un", "deux"):
        root = _tree(tmp_path / name, {f"{name}.txt": "x"})
        mod.main(
            ["scan", str(root), "-o", str(tmp_path / f"{name}.json.gz"),
             "--label", name.upper(), "-q"]
        )
    capsys.readouterr()
    mod.main(
        ["find", str(tmp_path / "un.json.gz"), str(tmp_path / "deux.json.gz"),
         "--name", "*.txt"]
    )
    out = capsys.readouterr().out
    assert "[UN] un.txt" in out and "[DEUX] deux.txt" in out


def test_main_find_limit_stops_early(disk, capsys):
    _, catalogue = disk
    capsys.readouterr()
    with _isolated_root_logger():
        mod.main(["find", str(catalogue), "--type", "f", "--limit", "1"])
    assert len(capsys.readouterr().out.strip().splitlines()) == 1


def test_main_json_output_is_valid(disk, capsys):
    _, catalogue = disk
    capsys.readouterr()
    mod.main(["find", str(catalogue), "--name", "*.jpg", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert {item["catalogue"] for item in payload} == {"USB"}


def test_main_ghost_refuses_a_destination_that_is_not_a_ghost(disk, tmp_path, capsys):
    _, catalogue = disk
    dest = _tree(tmp_path / "ghost", {"already-there.txt": "x"})
    with _isolated_root_logger():
        assert mod.main(["ghost", str(catalogue), str(dest)]) == 1
    assert mod.GHOST_MARKER in capsys.readouterr().err
    assert (dest / "already-there.txt").read_text() == "x"  # nothing was touched


def test_main_ghost_replaces_a_previous_ghost(disk, tmp_path):
    """Replacement must be clean: no leftovers from the previous scan."""
    _, catalogue = disk
    dest = tmp_path / "ghost"
    assert mod.main(["ghost", str(catalogue), str(dest)]) == 0
    (dest / "Photos/stale.jpg").write_text("")
    assert mod.main(["ghost", str(catalogue), str(dest)]) == 0
    assert not (dest / "Photos/stale.jpg").exists()
    assert (dest / "Photos/img.jpg").exists()


def test_main_ghost_then_native_tools_work(disk, tmp_path, capsys):
    _, catalogue = disk
    dest = tmp_path / "ghost"
    assert mod.main(["ghost", str(catalogue), str(dest)]) == 0
    assert (dest / "Photos/img.jpg").stat().st_size == 5000


def test_main_reports_a_missing_catalogue_cleanly(tmp_path, capsys):
    with _isolated_root_logger():
        assert mod.main(["ls", str(tmp_path / "absent.json.gz")]) == 1
    captured = capsys.readouterr()
    assert "unreadable" in captured.err
    assert captured.out == ""


def test_main_reports_an_unplugged_disk_cleanly(tmp_path, capsys):
    with _isolated_root_logger():
        code = mod.main(
            ["scan", str(tmp_path / "absent"), "-o", str(tmp_path / "c.json")]
        )
    assert code == 1
    assert "is the disk plugged in" in capsys.readouterr().err


def test_main_stream_discipline_data_on_stdout_logs_on_stderr(disk, capsys):
    _, catalogue = disk
    capsys.readouterr()
    with _isolated_root_logger():
        mod.main(["ls", str(catalogue), "-v"])
    captured = capsys.readouterr()
    assert "Photos/" in captured.out and "Photos/" not in captured.err
    assert "entry(ies)" in captured.err and "entry(ies)" not in captured.out


def test_main_broken_pipe_returns_141(disk, monkeypatch, capsys):
    """capsys carries the test: with no real descriptor, main()'s os.dup2()
    hits its guard instead of redirecting pytest's own capture."""
    _, catalogue = disk

    def boom(*args, **kwargs):
        raise BrokenPipeError

    monkeypatch.setattr(mod, "cmd_ls", boom)
    assert mod.main(["ls", str(catalogue)]) == 141
    assert capsys.readouterr().out == ""


class _BrokenPipeStdout:
    """A stdout whose writes succeed but whose flush fails - like a real pipe:
    print() only fills a buffer, the OS write happens at flush.

    fileno() raises io.UnsupportedOperation, not ValueError: since Python 3.14
    argparse probes the descriptor to decide whether to colorize its help, and
    it catches only that exception. It inherits from OSError AND ValueError, so
    main()'s guard still catches it."""

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        raise BrokenPipeError

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        raise io.UnsupportedOperation("no real descriptor")


def test_main_flushes_stdout_inside_the_try(disk, monkeypatch):
    """Without the flush inside the try, `shelf find ... | head -c 20` exits 120."""
    _, catalogue = disk
    monkeypatch.setattr(sys, "stdout", _BrokenPipeStdout())
    assert mod.main(["ls", str(catalogue)]) == 141


def test_main_keyboard_interrupt_returns_130(disk, monkeypatch):
    _, catalogue = disk

    def boom(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(mod, "cmd_ls", boom)
    assert mod.main(["ls", str(catalogue)]) == 130


# --- The fleet end to end: scan/save/shortcuts on a fake /Volumes ---

@pytest.fixture
def fleet_env(tmp_path, monkeypatch):
    """A shelf base, a fake /Volumes with two mounted disks, tmutil stubbed."""
    volumes = tmp_path / "Volumes"
    _tree(volumes / "Backup1", {"Photos": None, "Photos/a.jpg": "x" * 3000})
    _tree(volumes / "Backup2", {"Musique/b.flac": "y" * 500})
    # mounted_volumes() is stubbed rather than reading the fake directory: on
    # macOS a temp dir shares its st_dev with "/", so the real boot-disk filter
    # would discard every fake volume. The mount root itself is passed for real.
    monkeypatch.setattr(mod, "mounted_volumes", lambda root: {"Backup1", "Backup2"})
    monkeypatch.setattr(mod, "require_tools", lambda *a: None)
    monkeypatch.setattr(mod, "time_machine_excluded", lambda p: True)
    monkeypatch.setattr(mod, "detect_filesystem", lambda p: "apfs")
    # Pin the platform too, or these tests assert macOS conventions (.command,
    # [platform.macos]) against whatever OS happens to run them - green on a Mac,
    # red on Linux CI. The filesystem stub above already assumes macOS; this says
    # so out loud. Linux behaviour is covered by the build_platform tests, which
    # name the platform instead of detecting it.
    monkeypatch.setattr(mod, "detect_system", lambda: "darwin")
    return types.SimpleNamespace(
        base=tmp_path / "parc",
        volumes=volumes,
        ghosts=tmp_path / "ghosts",
        config=tmp_path / "parc" / mod.CONFIG_NAME,
    )


def _run(fleet_env, *argv: str) -> int:
    return mod.main([*argv, "--base", str(fleet_env.base)])


def _roots(fleet_env) -> list[str]:
    return [
        "--mount-root", str(fleet_env.volumes),
        "--ghost-root", str(fleet_env.ghosts),
    ]


def _save(fleet_env, target: str, *extra: str) -> int:
    return _run(fleet_env, "save", target, *_roots(fleet_env), *extra)


def _scan(fleet_env, volume: str, enclosure: str, *extra: str) -> int:
    return _run(
        fleet_env,
        "scan",
        str(fleet_env.volumes / volume),
        "--enclosure",
        enclosure,
        *_roots(fleet_env),
        *extra,
    )


def test_scan_files_the_catalogue_under_its_enclosure(fleet_env, capsys):
    assert _scan(fleet_env, "Backup1", "Enclosure1", "-q") == 0
    expected = fleet_env.base / "catalogs/Enclosure1/Backup1.json.gz"
    assert expected.is_file()
    assert mod.read_catalog(expected).enclosure == "Enclosure1"


def test_scan_labels_the_disk_after_the_volume_by_default(fleet_env):
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    catalogue = fleet_env.base / "catalogs/Enclosure1/Backup1.json.gz"
    assert mod.read_catalog(catalogue).label == "Backup1"


def test_scan_records_the_machine_that_ran_it(fleet_env):
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    catalogue = fleet_env.base / "catalogs/Enclosure1/Backup1.json.gz"
    assert mod.read_catalog(catalogue).hostname == mod.read_hostname()


def test_scan_registers_the_volume_in_the_config(fleet_env):
    assert _scan(fleet_env, "Backup1", "Enclosure1", "-q") == 0
    assert mod.read_fleet(fleet_env.config).enclosures == {"Enclosure1": ["Backup1"]}


def test_scan_with_a_custom_label_records_the_mapping(fleet_env):
    _scan(fleet_env, "Backup1", "Enclosure1", "--label", "Archives", "-q")
    fleet = mod.read_fleet(fleet_env.config)
    assert fleet.labels == {"Backup1": "Archives"}
    assert (fleet_env.base / "catalogs/Enclosure1/Archives.json.gz").is_file()


def test_scan_does_not_register_a_path_outside_volumes(fleet_env, tmp_path, capsys):
    """Only a volume mounted directly under /Volumes has a reusable name."""
    elsewhere = _tree(tmp_path / "elsewhere", {"f.txt": "x"})
    with _isolated_root_logger():
        code = _run(fleet_env, "scan", str(elsewhere), "--enclosure", "Enclosure1")
    assert code == 0
    assert "not registered" in capsys.readouterr().err
    assert mod.read_fleet(fleet_env.config).enclosures.get("Enclosure1", []) == []


def test_scan_ghost_builds_the_tree(fleet_env):
    assert _scan(fleet_env, "Backup1", "Enclosure1", "--ghost", "-q") == 0
    assert (fleet_env.ghosts / "Backup1/Photos/a.jpg").stat().st_size == 3000


def test_save_scans_and_ghosts_every_mounted_disk(fleet_env, capsys):
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    _scan(fleet_env, "Backup2", "Enclosure1", "-q")
    capsys.readouterr()
    assert _save(fleet_env, "Enclosure1") == 0
    assert (fleet_env.ghosts / "Backup1/Photos/a.jpg").is_file()
    assert (fleet_env.ghosts / "Backup2/Musique/b.flac").is_file()
    assert "2 volume(s) done, 0 absent(s)" in capsys.readouterr().out


def test_save_skips_an_unplugged_disk_without_destroying_anything(fleet_env, capsys):
    """An unplugged disk keeps its catalogue and its ghost."""
    _scan(fleet_env, "Backup1", "Enclosure1", "--ghost", "-q")
    fleet = mod.register_volume(
        mod.read_fleet(fleet_env.config), "Enclosure1", "Backup9", "Backup9"
    )
    mod.write_fleet(fleet_env.config, fleet)
    ancien = fleet_env.base / "catalogs/Enclosure1/Backup9.json.gz"
    mod.write_catalog(ancien, _catalog(_entry("vieux.txt"), label="Backup9"))
    capsys.readouterr()
    with _isolated_root_logger():
        assert _save(fleet_env, "Enclosure1") == 0
    out = capsys.readouterr()
    assert "1 volume(s) done, 1 absent(s)" in out.out
    assert "Backup9" in out.err
    assert ancien.is_file()  # catalogue kept
    assert (fleet_env.ghosts / "Backup1/Photos/a.jpg").is_file()


def test_save_accepts_a_group(fleet_env, capsys):
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    _scan(fleet_env, "Backup2", "Enclosure2", "-q")
    _run(fleet_env, "group", "add", "macmini", "Enclosure1", "Enclosure2", "-q")
    capsys.readouterr()
    assert _save(fleet_env, "macmini") == 0
    assert "2 volume(s) done" in capsys.readouterr().out


def test_save_no_ghost_only_writes_catalogues(fleet_env):
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    assert _save(fleet_env, "Enclosure1", "--no-ghost", "-q") == 0
    assert not (fleet_env.ghosts / "Backup1").exists()


def test_save_rejects_an_unknown_name(fleet_env, capsys):
    with _isolated_root_logger():
        assert _run(fleet_env, "save", "Enclosure9") == 1
    assert "unknown" in capsys.readouterr().err


def test_enclosure_add_registers_the_mounted_disks_by_default(fleet_env, capsys):
    assert _run(fleet_env, "enclosure", "add", "Enclosure1", "-q") == 0
    assert mod.read_fleet(fleet_env.config).enclosures == {
        "Enclosure1": ["Backup1", "Backup2"]
    }


def test_enclosure_add_accepts_explicit_volumes(fleet_env):
    _run(fleet_env, "enclosure", "add", "Enclosure2", "Backup2", "-q")
    assert mod.read_fleet(fleet_env.config).enclosures == {"Enclosure2": ["Backup2"]}


def test_enclosure_rm_drops_a_volume(fleet_env):
    _run(fleet_env, "enclosure", "add", "Enclosure1", "-q")
    _run(fleet_env, "enclosure", "rm", "Backup1", "-q")
    assert mod.read_fleet(fleet_env.config).enclosures == {"Enclosure1": ["Backup2"]}


def test_enclosure_list_shows_mounted_and_catalogued(fleet_env, capsys):
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    capsys.readouterr()
    assert _run(fleet_env, "enclosure", "list") == 0
    out = capsys.readouterr().out
    assert "Enclosure1" in out and "Backup1" in out and "mounted" in out


def test_group_add_rejects_an_unknown_enclosure(fleet_env, capsys):
    with _isolated_root_logger():
        assert _run(fleet_env, "group", "add", "macmini", "Enclosure9") == 1
    assert "unknown" in capsys.readouterr().err


def test_group_rm_removes_it(fleet_env):
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    _run(fleet_env, "group", "add", "macmini", "Enclosure1", "-q")
    _run(fleet_env, "group", "rm", "macmini", "-q")
    assert mod.read_fleet(fleet_env.config).groups == {}


def test_shortcuts_writes_one_executable_command_per_target(fleet_env, capsys):
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    _scan(fleet_env, "Backup2", "Enclosure2", "-q")
    _run(fleet_env, "group", "add", "macmini", "Enclosure1", "Enclosure2", "-q")
    capsys.readouterr()
    assert _run(fleet_env, "shortcuts") == 0
    outdir = fleet_env.base / mod.SHORTCUTS_DIRNAME
    names = sorted(p.name for p in outdir.iterdir())
    assert names == [
        "save-Enclosure1.command", "save-Enclosure2.command", "save-macmini.command"
    ]
    for path in outdir.iterdir():
        assert os.access(path, os.X_OK)
        assert "save " in path.read_text()


def test_shortcuts_without_any_enclosure_says_what_to_do(fleet_env, capsys):
    with _isolated_root_logger():
        assert _run(fleet_env, "shortcuts") == 1
    assert "enclosure add" in capsys.readouterr().err


def test_find_searches_the_whole_fleet_by_default(fleet_env, capsys):
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    _scan(fleet_env, "Backup2", "Enclosure2", "-q")
    capsys.readouterr()
    assert _run(fleet_env, "find", "--name", "*.flac") == 0
    assert "[Enclosure2/Backup2] Musique/b.flac" in capsys.readouterr().out


def test_find_can_be_narrowed_to_one_enclosure(fleet_env, capsys):
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    _scan(fleet_env, "Backup2", "Enclosure2", "-q")
    capsys.readouterr()
    _run(fleet_env, "find", "--type", "f", "--enclosure", "Enclosure1")
    out = capsys.readouterr().out
    assert "a.jpg" in out and "b.flac" not in out


def test_find_on_an_empty_fleet_explains_itself(fleet_env, capsys):
    with _isolated_root_logger():
        assert _run(fleet_env, "find", "--name", "*") == 1
    assert "shelf scan" in capsys.readouterr().err


def test_info_defaults_to_the_whole_fleet(fleet_env, capsys):
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    _scan(fleet_env, "Backup2", "Enclosure2", "-q")
    capsys.readouterr()
    assert _run(fleet_env, "info") == 0
    out = capsys.readouterr().out
    assert out.count("Label") == 2
    assert "Enclosure1" in out and "Enclosure2" in out


# --- Anti-loss guard: an empty catalogue must never overwrite ---

def test_scan_tree_names_the_macos_privacy_setting_on_permission_denied(
    tmp_path, monkeypatch
):
    """macOS denies removable volumes until the app is authorised."""
    root = _tree(tmp_path / "Externe", {"f.txt": "x"})
    real_scandir = os.scandir

    def refuse(path, *args, **kwargs):
        if str(path) == str(root):
            raise PermissionError(1, "Operation not permitted")
        return real_scandir(path, *args, **kwargs)

    monkeypatch.setattr(mod.os, "scandir", refuse)
    with pytest.raises(mod.ScanError) as excinfo:
        mod.scan_tree(root, excludes=())
    assert "Full Disk Access" in str(excinfo.value)


def test_scan_accepts_a_genuinely_empty_disk_without_any_flag(fleet_env, tmp_path):
    """Zero entries AND zero errors = a really empty disk. Nothing to protect,
    nothing to report: it goes through, with no --allow-empty."""
    empty = tmp_path / "Volumes" / "Vide"
    empty.mkdir(parents=True)
    assert _run(fleet_env, "scan", str(empty), "--enclosure", "Enclosure1", "-q") == 0
    catalogue = fleet_env.base / "catalogs/Enclosure1/Vide.json.gz"
    assert catalogue.is_file()
    assert mod.read_catalog(catalogue).entries == {}


def test_scan_refuses_when_the_walk_itself_failed(
    fleet_env, tmp_path, monkeypatch, capsys
):
    """Zero entries BUT errors: the walk failed, it describes nothing."""
    disk = _tree(tmp_path / "Volumes" / "Broken", {"f.txt": "x"})
    monkeypatch.setattr(mod, "scan_tree", lambda root, **k: ({}, ["unreadable"], []))
    with _isolated_root_logger():
        code = _run(fleet_env, "scan", str(disk), "--enclosure", "Enclosure1")
    assert code == 1
    assert "the walk failed" in capsys.readouterr().err
    assert not (fleet_env.base / "catalogs/Enclosure1/Broken.json.gz").exists()


def test_scan_refuses_to_replace_a_stocked_inventory_with_an_empty_one(
    fleet_env, tmp_path, capsys
):
    """The dreaded loss: the disk is unplugged, this catalogue is the only
    copy, and a walk that returns nothing would erase it."""
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    catalogue = fleet_env.base / "catalogs/Enclosure1/Backup1.json.gz"
    before = catalogue.read_bytes()
    (fleet_env.volumes / "Backup1" / "Photos/a.jpg").unlink()
    (fleet_env.volumes / "Backup1" / "Photos").rmdir()
    with _isolated_root_logger():
        code = _scan(fleet_env, "Backup1", "Enclosure1")
    assert code == 1
    assert "catalogue NOT written" in capsys.readouterr().err
    assert catalogue.read_bytes() == before


def test_scan_allow_empty_overrides_the_inventory_guard(fleet_env):
    """The disk really was emptied: we accept it and overwrite."""
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    (fleet_env.volumes / "Backup1" / "Photos/a.jpg").unlink()
    (fleet_env.volumes / "Backup1" / "Photos").rmdir()
    assert _scan(fleet_env, "Backup1", "Enclosure1", "--allow-empty", "-q") == 0
    catalogue = fleet_env.base / "catalogs/Enclosure1/Backup1.json.gz"
    assert mod.read_catalog(catalogue).entries == {}


def test_scan_refreshes_an_empty_catalogue_over_an_empty_one(fleet_env, tmp_path):
    """Nothing to lose: an empty replacing an empty goes through, no flag."""
    empty = tmp_path / "Volumes" / "Vide"
    empty.mkdir(parents=True)
    assert _run(fleet_env, "scan", str(empty), "--enclosure", "Enclosure1", "-q") == 0
    assert _run(fleet_env, "scan", str(empty), "--enclosure", "Enclosure1", "-q") == 0


def test_save_handles_a_mix_of_full_and_empty_disks(fleet_env, monkeypatch, capsys):
    """The real case: Backup1 has files, Backup2 and Backup3 are empty.
    A `save` shortcut must handle all three with no flag at all."""
    for name in ("Backup2", "Backup3"):
        for child in sorted(
            (fleet_env.volumes / name).rglob("*"), key=lambda p: -len(p.parts)
        ):
            child.unlink() if child.is_file() else child.rmdir()
    (fleet_env.volumes / "Backup3").mkdir(exist_ok=True)
    monkeypatch.setattr(
        mod, "mounted_volumes", lambda root: {"Backup1", "Backup2", "Backup3"}
    )
    for name in ("Backup1", "Backup2", "Backup3"):
        _run(fleet_env, "enclosure", "add", "Enclosure1", name, "-q")
    capsys.readouterr()
    assert _save(fleet_env, "Enclosure1") == 0
    out = capsys.readouterr().out
    assert "3 volume(s) done, 0 absent(s), 0 failed" in out
    assert (fleet_env.base / "catalogs/Enclosure1/Backup2.json.gz").is_file()
    assert (fleet_env.ghosts / "Backup3").is_dir()


def test_shortcuts_can_embed_extra_save_options(fleet_env, capsys):
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    capsys.readouterr()
    # The "=" form is mandatory: without it argparse takes "--exclude" for an
    # option of shortcuts, not for the value of --save-option.
    assert _run(
        fleet_env, "shortcuts",
        "--save-option=--allow-empty",
        "--save-option=--exclude", "--save-option=a folder",
    ) == 0
    shortcut = fleet_env.base / mod.SHORTCUTS_DIRNAME / "save-Enclosure1.command"
    text = shortcut.read_text()
    assert "save Enclosure1 -v --allow-empty --exclude 'a folder'" in text


def test_save_keeps_the_old_catalogue_when_a_disk_becomes_unreadable(
    fleet_env, monkeypatch, capsys
):
    """An unreadable disk must neither overwrite its catalogue nor sink the
    other volumes of the enclosure."""
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    _scan(fleet_env, "Backup2", "Enclosure1", "-q")
    ancien = fleet_env.base / "catalogs/Enclosure1/Backup1.json.gz"
    before = ancien.read_bytes()
    real = mod.scan_tree

    def refuse(root, **kwargs):
        if Path(root).name == "Backup1":
            raise mod.ScanError(f"{root}: access denied by macOS.")
        return real(root, **kwargs)

    monkeypatch.setattr(mod, "scan_tree", refuse)
    capsys.readouterr()
    with _isolated_root_logger():
        code = _run(fleet_env, "save", "Enclosure1", "--no-ghost")
    out = capsys.readouterr()
    assert code == 1  # the failure is reported
    assert "1 volume(s) done, 0 absent(s), 1 failed" in out.out
    assert "FAILED (catalogue kept): Backup1" in out.out
    assert ancien.read_bytes() == before  # intact


# --- Provenance and the config/shortcuts commands, end to end ---

def test_catalogue_records_its_provenance(fleet_env, monkeypatch):
    monkeypatch.setattr(mod, "detect_filesystem", lambda p: "exfat")
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    catalog = mod.read_catalog(
        fleet_env.base / "catalogs/Enclosure1/Backup1.json.gz"
    )
    assert catalog.shelf_version == mod.__version__
    assert catalog.filesystem == "exfat"
    assert catalog.platform.startswith("macos") or catalog.platform.startswith("linux")
    assert catalog.hostname


def test_ghost_marker_carries_the_same_provenance(fleet_env, monkeypatch):
    monkeypatch.setattr(mod, "detect_filesystem", lambda p: "apfs")
    _scan(fleet_env, "Backup1", "Enclosure1", "--ghost", "-q")
    marker = json.loads(
        (fleet_env.ghosts / "Backup1" / mod.GHOST_MARKER).read_text()
    )
    assert marker["shelf_version"] == mod.__version__
    assert marker["filesystem"] == "apfs"
    assert "platform" in marker and marker["warning"].startswith("Ghost")


def test_detect_filesystem_never_raises_when_the_tool_fails(monkeypatch):
    """An unknown filesystem is simply not reported - never a failed scan."""
    def boom(*a, **k):
        raise mod.CommandError(["diskutil"], 1, "nope")

    monkeypatch.setattr(mod, "run", boom)
    assert mod.detect_filesystem(Path("/nowhere")) == ""


def test_info_shows_the_filesystem_when_known():
    catalog = _catalog(_entry("a.txt"))
    text = mod.render_info(
        mod.Catalog(
            root=catalog.root, label=catalog.label, scanned_at=catalog.scanned_at,
            entries=catalog.entries, filesystem="exfat", hostname="box",
            platform="linux 6.8 x86_64", shelf_version="1.0.0",
        )
    )
    assert "Filesystem  : exfat" in text
    assert "box (linux 6.8 x86_64), shelf 1.0.0" in text


def test_scan_writes_this_platform_into_the_config(fleet_env):
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    fleet = mod.read_fleet(fleet_env.config)
    assert set(fleet.platforms) == {"macos"}
    assert fleet.platforms["macos"]["mount_root"] == str(fleet_env.volumes)


def test_config_command_prints_the_resolved_paths(fleet_env, capsys):
    assert _run(fleet_env, "config") == 0
    out = capsys.readouterr().out
    assert "platform      : macos" in out
    assert str(fleet_env.base) in out


def test_config_can_preview_another_platform(fleet_env, capsys):
    """From a Mac, check what the Linux machine will resolve."""
    assert _run(fleet_env, "config", "--platform", "linux") == 0
    out = capsys.readouterr().out
    assert "platform      : linux" in out
    assert ".sh" in out
    assert "(none on this platform)" in out


def test_shortcuts_use_this_platform_suffix(fleet_env, capsys):
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    _run(fleet_env, "shortcuts", "-q")
    names = sorted(p.name for p in (fleet_env.base / mod.SHORTCUTS_DIRNAME).iterdir())
    assert names == ["save-Enclosure1.command"]


def test_shortcuts_can_target_the_other_platform(fleet_env, capsys):
    """Generate the Linux shortcuts from the Mac; both sets coexist."""
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    _run(fleet_env, "shortcuts", "-q")
    _run(fleet_env, "shortcuts", "--platform", "linux", "-q")
    names = sorted(p.name for p in (fleet_env.base / mod.SHORTCUTS_DIRNAME).iterdir())
    assert names == ["save-Enclosure1.command", "save-Enclosure1.sh"]


def test_main_refuses_an_unsupported_platform(fleet_env, monkeypatch, capsys):
    monkeypatch.setattr(mod, "detect_system", lambda: "win32")
    with _isolated_root_logger():
        assert _run(fleet_env, "config") == 1
    assert "macOS and Linux" in capsys.readouterr().err


# --- Excludes end to end: declared once, honoured by scan ---

def _write_excludes(fleet_env, *, glob=(), per_catalogue=None):
    """Hand-edit the [excludes] section the way a user would."""
    config = fleet_env.config
    text = config.read_text()
    patterns = ", ".join(f'"{p}"' for p in glob)
    text = text.replace("global = []", f"global = [{patterns}]")
    for label, rules in (per_catalogue or {}).items():
        joined = ", ".join(f'"{r}"' for r in rules)
        text = text.replace(
            "[excludes.catalogue]", f'[excludes.catalogue]\n"{label}" = [{joined}]'
        )
    config.write_text(text)


def test_scan_honours_a_global_exclude_from_the_config(fleet_env):
    (fleet_env.volumes / "Backup1" / "node_modules").mkdir()
    (fleet_env.volumes / "Backup1" / "node_modules" / "x.js").write_text("x")
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    _write_excludes(fleet_env, glob=["node_modules"])
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    catalog = mod.read_catalog(
        fleet_env.base / "catalogs/Enclosure1/Backup1.json.gz"
    )
    assert not any("node_modules" in e.rel for e in catalog.entries.values())
    assert any(e.rel == "Photos/a.jpg" for e in catalog.entries.values())


def test_scan_honours_a_per_catalogue_exclude(fleet_env):
    """Backup1's rule must not reach into Backup2."""
    for volume in ("Backup1", "Backup2"):
        (fleet_env.volumes / volume / "Secret").mkdir()
        (fleet_env.volumes / volume / "Secret" / "k.txt").write_text("k")
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    _scan(fleet_env, "Backup2", "Enclosure1", "-q")
    _write_excludes(fleet_env, per_catalogue={"Backup1": ["Secret"]})
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    _scan(fleet_env, "Backup2", "Enclosure1", "-q")
    first = mod.read_catalog(fleet_env.base / "catalogs/Enclosure1/Backup1.json.gz")
    second = mod.read_catalog(fleet_env.base / "catalogs/Enclosure1/Backup2.json.gz")
    assert not any("Secret" in e.rel for e in first.entries.values())
    assert any("Secret" in e.rel for e in second.entries.values())


def test_scan_no_config_excludes_ignores_the_declared_rules(fleet_env):
    (fleet_env.volumes / "Backup1" / "node_modules").mkdir()
    (fleet_env.volumes / "Backup1" / "node_modules" / "x.js").write_text("x")
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    _write_excludes(fleet_env, glob=["node_modules"])
    _scan(fleet_env, "Backup1", "Enclosure1", "-q", "--no-config-excludes")
    catalog = mod.read_catalog(
        fleet_env.base / "catalogs/Enclosure1/Backup1.json.gz"
    )
    assert any("node_modules" in e.rel for e in catalog.entries.values())


# --- Excludes at read time: the rule bites without a re-scan ---
def test_an_exclude_rule_cannot_empty_a_stocked_catalogue(fleet_env, capsys):
    """_guard_overwrite reads the PREVIOUS catalogue unfiltered. Filter it too
    and a disk hidden entirely by a rule looks empty, the guard stands down,
    and the only copy is overwritten - the exact loss it exists to prevent."""
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    catalogue = fleet_env.base / "catalogs/Enclosure1/Backup1.json.gz"
    before = catalogue.read_bytes()
    _write_excludes(fleet_env, glob=["*"])
    capsys.readouterr()
    with _isolated_root_logger():
        assert _scan(fleet_env, "Backup1", "Enclosure1") == 1
    assert catalogue.read_bytes() == before



def _backup1_with_junk(fleet_env):
    """A catalogue written BEFORE any exclude rule exists."""
    (fleet_env.volumes / "Backup1" / "node_modules").mkdir()
    (fleet_env.volumes / "Backup1" / "node_modules" / "x.js").write_text("x" * 40)
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    return fleet_env.base / "catalogs/Enclosure1/Backup1.json.gz"


def test_find_hides_entries_a_rescan_would_have_kept(fleet_env, capsys):
    """The whole point: the rule applies to a catalogue written before it."""
    _backup1_with_junk(fleet_env)
    _write_excludes(fleet_env, glob=["node_modules"])
    capsys.readouterr()
    with _isolated_root_logger():
        assert _run(fleet_env, "find", "--name", "*.js") == 0
    out = capsys.readouterr()
    assert "x.js" not in out.out
    assert "hidden by [excludes]" in out.err  # never looks exhaustive


def test_find_no_excludes_brings_them_back(fleet_env, capsys):
    _backup1_with_junk(fleet_env)
    _write_excludes(fleet_env, glob=["node_modules"])
    capsys.readouterr()
    with _isolated_root_logger():
        assert _run(fleet_env, "find", "--name", "*.js", "--no-excludes") == 0
    out = capsys.readouterr()
    assert "x.js" in out.out
    assert "hidden by [excludes]" not in out.err


def test_ls_of_an_excluded_path_says_it_is_hidden_not_absent(fleet_env, capsys):
    """"path not in the catalogue" would send the reader hunting a disk fault."""
    catalogue = _backup1_with_junk(fleet_env)
    _write_excludes(fleet_env, glob=["node_modules"])
    capsys.readouterr()
    with _isolated_root_logger():
        assert _run(fleet_env, "ls", str(catalogue), "node_modules") == 1
    assert "hidden by [excludes]" in capsys.readouterr().err


def test_ls_of_a_genuinely_absent_path_still_says_so(fleet_env, capsys):
    catalogue = _backup1_with_junk(fleet_env)
    _write_excludes(fleet_env, glob=["node_modules"])
    capsys.readouterr()
    with _isolated_root_logger():
        assert _run(fleet_env, "ls", str(catalogue), "nowhere") == 1
    assert "path not in the catalogue" in capsys.readouterr().err


def test_du_totals_drop_the_hidden_bytes(fleet_env, capsys):
    catalogue = _backup1_with_junk(fleet_env)
    capsys.readouterr()
    _run(fleet_env, "du", str(catalogue), "-q")
    before = capsys.readouterr().out
    _write_excludes(fleet_env, glob=["node_modules"])
    _run(fleet_env, "du", str(catalogue), "-q")
    assert "node_modules" in before
    assert "node_modules" not in capsys.readouterr().out


def test_a_broken_config_does_not_stop_you_browsing(fleet_env, capsys):
    """The catalogues are the data; shelf.toml is only a lens over them."""
    catalogue = _backup1_with_junk(fleet_env)
    fleet_env.config.write_text("this is not : toml [[[")
    capsys.readouterr()
    with _isolated_root_logger():
        assert _run(fleet_env, "ls", str(catalogue)) == 0
    out = capsys.readouterr()
    assert "Photos" in out.out
    assert "browsing without [excludes]" in out.err


def test_an_exclude_rule_makes_ghost_all_rebuild_on_its_own(fleet_env, capsys):
    """ghost_identity keys on entry count and size, so it must be computed
    AFTER filtering - otherwise the fleet looks stale on every single run."""
    _backup1_with_junk(fleet_env)
    _run(fleet_env, "ghost", "--all", "--ghost-root", str(fleet_env.ghosts), "-q")
    capsys.readouterr()
    _run(fleet_env, "ghost", "--all", "--ghost-root", str(fleet_env.ghosts))
    assert "0 rebuilt, 1 already current" in capsys.readouterr().out
    _write_excludes(fleet_env, glob=["node_modules"])
    with _isolated_root_logger():
        _run(fleet_env, "ghost", "--all", "--ghost-root", str(fleet_env.ghosts))
    assert "1 rebuilt" in capsys.readouterr().out
    assert not (fleet_env.ghosts / "Backup1/node_modules").exists()


def test_ghost_all_stays_current_when_no_rule_changed(fleet_env, capsys):
    """The identity must be stable across runs, not merely recomputed."""
    _backup1_with_junk(fleet_env)
    _write_excludes(fleet_env, glob=["node_modules"])
    _run(fleet_env, "ghost", "--all", "--ghost-root", str(fleet_env.ghosts), "-q")
    capsys.readouterr()
    with _isolated_root_logger():
        _run(fleet_env, "ghost", "--all", "--ghost-root", str(fleet_env.ghosts))
    assert "0 rebuilt, 1 already current" in capsys.readouterr().out


# --- ghost --all: the two-machine workflow ---

def test_ghost_is_current_compares_the_full_identity():
    catalog = _catalog(_entry("a.txt"))
    marker = mod.ghost_identity(catalog)
    assert mod.ghost_is_current(marker, catalog) is True
    for key, wrong in (
        ("scanned_at", "2020-01-01 00:00:00"),
        ("label", "Other"),
        ("entries", "999"),
        ("bytes", "1"),
    ):
        assert mod.ghost_is_current({**marker, key: wrong}, catalog) is False
    assert mod.ghost_is_current({}, catalog) is False


def test_ghost_identity_separates_two_scans_inside_one_second():
    """scanned_at has one-second resolution: content must break the tie."""
    stamp = "2026-01-01 12:00:00"
    before = _catalog(_entry("a.txt", size=10), scanned_at=stamp)
    after = _catalog(_entry("a.txt", size=10), _entry("b.txt", size=10),
                     scanned_at=stamp)
    assert mod.ghost_is_current(mod.ghost_identity(before), after) is False


def test_read_ghost_marker_of_a_missing_or_broken_ghost_is_empty(tmp_path):
    assert mod.read_ghost_marker(tmp_path / "nope") == {}
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / mod.GHOST_MARKER).write_text("{not json")
    assert mod.read_ghost_marker(broken) == {}


def test_ghost_all_rebuilds_every_catalogue_in_the_fleet(fleet_env, capsys):
    """Catalogues travel between machines; ghosts are rebuilt where they land."""
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    _scan(fleet_env, "Backup2", "Enclosure2", "-q")
    shutil.rmtree(fleet_env.ghosts, ignore_errors=True)
    capsys.readouterr()
    code = _run(fleet_env, "ghost", "--all", "--ghost-root", str(fleet_env.ghosts))
    assert code == 0
    assert (fleet_env.ghosts / "Backup1/Photos/a.jpg").is_file()
    assert (fleet_env.ghosts / "Backup2").is_dir()
    assert "2 rebuilt, 0 already current, 0 failed" in capsys.readouterr().out


def test_ghost_all_skips_the_ghosts_already_current(fleet_env, capsys):
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    _run(fleet_env, "ghost", "--all", "--ghost-root", str(fleet_env.ghosts), "-q")
    capsys.readouterr()
    _run(fleet_env, "ghost", "--all", "--ghost-root", str(fleet_env.ghosts))
    assert "0 rebuilt, 1 already current" in capsys.readouterr().out


def test_ghost_all_force_rebuilds_anyway(fleet_env, capsys):
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    _run(fleet_env, "ghost", "--all", "--ghost-root", str(fleet_env.ghosts), "-q")
    capsys.readouterr()
    _run(fleet_env, "ghost", "--all", "--force", "--ghost-root", str(fleet_env.ghosts))
    assert "1 rebuilt, 0 already current" in capsys.readouterr().out


def test_ghost_all_rebuilds_after_a_rescan(fleet_env, capsys):
    """A newer catalogue must win over a stale ghost."""
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    _run(fleet_env, "ghost", "--all", "--ghost-root", str(fleet_env.ghosts), "-q")
    (fleet_env.volumes / "Backup1" / "Photos" / "new.jpg").write_text("x" * 50)
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    capsys.readouterr()
    _run(fleet_env, "ghost", "--all", "--ghost-root", str(fleet_env.ghosts))
    assert "1 rebuilt" in capsys.readouterr().out
    assert (fleet_env.ghosts / "Backup1/Photos/new.jpg").is_file()


def test_ghost_all_survives_one_broken_catalogue(fleet_env, capsys):
    """One unreadable catalogue must not stop the rest of the fleet."""
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    broken = fleet_env.base / "catalogs/Enclosure1/Broken.json.gz"
    broken.write_bytes(b"not a catalogue")
    capsys.readouterr()
    with _isolated_root_logger():
        code = _run(fleet_env, "ghost", "--all", "--ghost-root", str(fleet_env.ghosts))
    out = capsys.readouterr()
    assert code == 1
    assert "1 rebuilt" in out.out and "1 failed" in out.out
    assert "FAILED: Broken.json.gz" in out.out
    assert (fleet_env.ghosts / "Backup1").is_dir()  # the healthy one still built


def test_ghost_all_rejects_a_catalogue_argument(fleet_env, capsys):
    _scan(fleet_env, "Backup1", "Enclosure1", "-q")
    catalogue = fleet_env.base / "catalogs/Enclosure1/Backup1.json.gz"
    with _isolated_root_logger():
        assert _run(fleet_env, "ghost", "--all", str(catalogue)) == 1
    assert "pass no catalogue" in capsys.readouterr().err


def test_ghost_without_a_catalogue_says_what_to_do(fleet_env, capsys):
    with _isolated_root_logger():
        assert _run(fleet_env, "ghost") == 1
    assert "--all" in capsys.readouterr().err


def test_ghost_all_on_an_empty_fleet_explains_itself(fleet_env, capsys):
    with _isolated_root_logger():
        assert _run(fleet_env, "ghost", "--all") == 1
    assert "shelf scan" in capsys.readouterr().err
