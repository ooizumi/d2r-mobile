#!/usr/bin/env python3
"""Prepare a portable Android or HarmonyOS import staging folder from owned game files.

Usage:
  python3 mobile_resources.py --target android|ohos --workspace PATH \
      (--game-dir UPDATE | --rom PACKAGE.nsp --rom PACKAGE.xci --keys PROD.KEYS) \
      [--hactool PATH] [--warm-seed PATH] [--astc-pack PATH] \
      [--dry-run] [--json] [--verbose]

`--game-dir` names an already extracted Update directory containing ExeFS/ and
RomFS/. Package extraction uses the repository's extract_game.sh, including its
base/update and title-ID checks. The recipient supplies the game packages and
keys; this tool never downloads or copies either into source.

The completed import staging parent is:

  <workspace>/mobile-resources/<target>/<build-key>/
    android/                 Android only
    dm/                      HarmonyOS only
    data/                    required archives + .build.config or build.config
    resource-manifest.json   atomic completion record and complete file inventory

An `android/` + `data/` or `dm/` + `data/` directory is accepted directly by
`mobile_device.py install --resources IMPORT_STAGING_PARENT`. This tool does not
install an app, access a phone, build an APK/HAP, or write the main installer.

Dry-run only reads and validates inputs and prints the plan. It creates no
directories, writes no files, and launches neither extraction nor generation.
Child output is sanitized, kept in workspace-local log files, and shown on
stderr only with --verbose. Key values and key arguments are never emitted.

Source isolation: generation runs from a workspace snapshot of current tracked
and nonignored `dm/` files from a Git checkout. A no-.git source ZIP uses its
compatible `SOURCE_EXPORT_MANIFEST.txt` fallback. Ignored generated art, ROMs,
resources, keys, and private build configuration are not copied. The snapshot
is necessary because upstream d2rm_android.py/gen_assets.py can write generated
TouchArt under dm/ios; the original checkout is never used as the generator
working tree. Complete and reused results expose the resolved canonical
`game_dir` for the build worker, including when resources were prepared from
owned ROM packages.

Portability: macOS is the first supported host. Native Windows package
extraction is rejected because extract_game.sh requires a Bash environment and
its Windows path/interoperability behavior is not verified. Use an extracted
Update via --game-dir on Windows, or run package extraction in the supported
Bash/WSL workflow and then pass that Update directory. This tool makes no claim
about native-Windows generation beyond `--dry-run`.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
import uuid


TOOL_PATH = Path(__file__).resolve()
SOURCE_DM = TOOL_PATH.parent.parent
SOURCE_REPO = SOURCE_DM.parent
PRODUCT_PATH = SOURCE_DM / "PRODUCT"
EXTRACT_SCRIPT = Path("tools/extract_game.sh")
D2RM_SCRIPT = Path("tools/d2rm_android.py")
SOURCE_MANIFEST_NAMES = (
    "dm/SOURCE_EXPORT_MANIFEST.txt",
    "dm/source_export_manifest.txt",
    "SOURCE_EXPORT_MANIFEST.txt",
)
SOURCE_MANIFEST_ENV = "DM_SOURCE_MANIFEST"
EXCLUDED_SOURCE_FILES = {
    "host/dm_vault_tab.h",
    "host/dm_hud_exp.h",
    "host/dm_bright_levels.inc",
    "ios/Sources/dm_touch_native_art.c",
    "harmony/app/build-profile.json5",
}
FORBIDDEN_SOURCE_SUFFIXES = (".nsp", ".xci", ".nca", ".keys", ".tik")
FORBIDDEN_SOURCE_NAMES = {"prod.keys", "title.keys"}
SOURCE_SNAPSHOT_VERSION = "2"
TOOL_IDENTITY_NAMES = ("glslc", "spirv-opt", "spirv-val", "dotnet")

TITLE_ID = "0100726014352000"
SWITCH_VERSION = "1.0.34.0"
PC_VERSION = "3.3.94012"
GAME_PROFILE = "Diablo II: Resurrected Switch %s / PC %s" % (SWITCH_VERSION, PC_VERSION)
EXPECTED_SHADER_COUNT = 6931
EXPECTED_OHOS_UI_FILES = 18
MANIFEST_NAME = "resource-manifest.json"
MANIFEST_VERSION = 1

EXPECTED_EXEFS_SHA256 = {
    "main": "55fd909bb7230b002357e5b0d8499c11f06888f345c49e69eb4734df44a27d0e",
    "sdk": "1c9f198a91c0bcfee5cf8f5b53fb941895e91334dfb766ce79fc68576797f017",
    "subsdk0": "67a38fcf89cd13e040a021eb3d157072f38ebf6680a907874c35c7b231b5d4e2",
}

# Kept in the same order as d2rm_pack.PHONE_ARCHIVES and host/romfs_validate.c.
ARCHIVE_RAW_SIZES = {
    "00-00000001.data": 1073741824,
    "00-00000002.data": 1073741821,
    "00-00000003.data": 1026342790,
    "00-00000004.data": 3664822,
    "00-00000005.data": 391990840,
    "00-00000006.data": 3794407,
    "01-00000001.data": 344377972,
    "01-00000002.data": 37189352,
    "02-00000001.data": 368762692,
    "02-00000002.data": 23206186,
    "03-00000001.data": 251620752,
    "03-00000002.data": 55071917,
    "04-00000001.data": 118831405,
    "04-00000002.data": 8915855,
    "05-00000001.data": 478931036,
    "05-00000002.data": 92048235,
    "17-00000001.data": 204495617,
    "17-00000002.data": 4496514,
}
REQUIRED_ARCHIVES = tuple(ARCHIVE_RAW_SIZES)
CONFIG_NAMES = (".build.config", "build.config")
LAYOUTS = {"android": ("android", "data"), "ohos": ("dm", "data")}
TARGETS = tuple(LAYOUTS)
HASH_CHUNK = 8 << 20
HEX_SECRET_RE = re.compile(r"(?i)(?<![0-9a-f])[0-9a-f]{32,128}(?![0-9a-f])")


class ToolError(Exception):
    """A user-facing failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str, hint: Optional[str] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def as_dict(self) -> Dict[str, Any]:
        value: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            value["hint"] = self.hint
        return value


class CliParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ToolError("usage", message, "Run with --help for usage.")


@dataclass(frozen=True)
class SourceEntry:
    relative: PurePosixPath
    path: Path
    size: int
    sha256: str
    executable: bool


@dataclass(frozen=True)
class GameInfo:
    update: Path
    identity: Dict[str, Any]
    identity_digest: str
    archive_sha256: Dict[str, str]
    archive_sizes: Dict[str, int]
    config_name: str
    config_sha256: str
    config_size: int


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest_value(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _within(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_regular_file(path: Path, label: str) -> Path:
    requested = path.expanduser()
    if requested.is_symlink():
        raise ToolError("unsafe_input_symlink", "%s must not be a symbolic link: %s" % (label, requested.absolute()))
    path = _resolved(requested)
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise ToolError("input_missing", "%s was not found: %s" % (label, path)) from exc
    if not stat.S_ISREG(mode):
        raise ToolError("input_not_file", "%s must be a regular file: %s" % (label, path))
    return path


def _safe_regular_dir(path: Path, label: str) -> Path:
    requested = path.expanduser()
    if requested.is_symlink():
        raise ToolError("unsafe_input_symlink", "%s must not be a symbolic link: %s" % (label, requested.absolute()))
    path = _resolved(requested)
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise ToolError("input_missing", "%s was not found: %s" % (label, path)) from exc
    if not stat.S_ISDIR(mode):
        raise ToolError("input_not_directory", "%s must be a directory: %s" % (label, path))
    return path


def sha256_file(path: Path) -> Tuple[str, int]:
    path = _safe_regular_file(path, "input file")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(HASH_CHUNK), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _validate_relative_path(value: str) -> PurePosixPath:
    if not value or "\x00" in value or "\\" in value:
        raise ToolError("unsafe_relative_path", "unsafe inventory path: %r" % value)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ToolError("unsafe_relative_path", "unsafe inventory path: %r" % value)
    return path


def _snapshot_mode(source_root: Path) -> str:
    return "git" if (source_root / ".git").exists() else "manifest"


def _source_manifest_path(source_root: Path) -> Optional[Path]:
    configured = os.environ.get(SOURCE_MANIFEST_ENV)
    candidates: List[Path] = [Path(configured)] if configured else []
    candidates.extend(source_root / name for name in SOURCE_MANIFEST_NAMES)
    for candidate in candidates:
        if candidate.is_file() and not candidate.is_symlink():
            return _resolved(candidate)
    return None


def _excluded_source(relative: PurePosixPath) -> bool:
    value = relative.as_posix()
    if value in EXCLUDED_SOURCE_FILES:
        return True
    if "Gen" in relative.parts or "__pycache__" in relative.parts:
        return True
    if relative.name.lower() in FORBIDDEN_SOURCE_NAMES or relative.suffix.lower() in FORBIDDEN_SOURCE_SUFFIXES:
        return True
    if re.match(r"^ios/Sources/TouchArt/dm_native_.*\.png$", value):
        return True
    return value in {"ios/tools/native_art/skills.txt", "ios/tools/native_art/skilldesc.txt"}


def _source_names_from_git(dm_root: Path, source_root: Path) -> List[str]:
    git = shutil.which("git")
    if not git:
        raise ToolError(
            "git_required_for_source_isolation",
            "A Git executable is required for this source checkout's tracked/nonignored inventory.",
            "Install git or use a source export containing SOURCE_EXPORT_MANIFEST.txt.",
        )
    requested_repo = _resolved(source_root)
    top = subprocess.run(
        [git, "-C", str(requested_repo), "rev-parse", "--show-toplevel"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if top.returncode:
        raise ToolError("git_checkout_required", "The source tree is not a readable Git checkout.", top.stderr.strip())
    try:
        repo = Path(top.stdout.strip()).resolve()
        prefix = dm_root.relative_to(repo).as_posix()
    except (OSError, ValueError) as exc:
        raise ToolError("git_checkout_required", "The source dm directory is outside its Git checkout.") from exc
    listing = subprocess.run(
        [git, "-C", str(repo), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", prefix],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if listing.returncode:
        raise ToolError("source_inventory_failed", "git could not enumerate the source tree.", listing.stderr.decode("utf-8", "replace").strip())
    return sorted({item.decode("utf-8", "surrogateescape") for item in listing.stdout.split(b"\0") if item})


def _source_names_from_manifest(source_root: Path) -> List[str]:
    manifest = _source_manifest_path(source_root)
    if not manifest:
        raise ToolError(
            "source_manifest_required",
            "This source archive has no .git and no compatible SOURCE_EXPORT_MANIFEST.txt fallback.",
            "Use a source ZIP produced by export-source, set DM_SOURCE_MANIFEST, or restore the Git checkout.",
        )
    names: List[str] = []
    for number, raw in enumerate(manifest.read_text(encoding="utf-8", errors="strict").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        value = raw.split("\t", 1)[0].strip().replace("\\", "/")
        if value != "dm" and not value.startswith("dm/"):
            continue
        rel = _validate_relative_path(value)
        if not rel.parts or rel.parts[0] != "dm":
            raise ToolError("unsafe_source_path", "source manifest line %d is not under dm/: %r" % (number, value))
        names.append(rel.as_posix())
    return sorted(set(names))


def _source_inventory(dm_root: Path, repo_root: Optional[Path] = None) -> Tuple[List[SourceEntry], str]:
    """Inventory current Git-tracked/nonignored files or a no-.git export manifest."""
    dm_root = _safe_regular_dir(dm_root, "source dm directory")
    source_root = _resolved(repo_root or dm_root.parent)
    mode = _snapshot_mode(source_root)
    names = _source_names_from_git(dm_root, source_root) if mode == "git" else _source_names_from_manifest(source_root)
    if not names:
        raise ToolError("source_inventory_empty", "The source inventory returned no dm/ files.")

    repo = source_root
    if mode == "git":
        top = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if top.returncode == 0:
            repo = Path(top.stdout.strip()).resolve()
    prefix = dm_root.relative_to(repo).as_posix()
    prefix_path = PurePosixPath(prefix)
    entries: List[SourceEntry] = []
    seen: set[str] = set()
    for name in names:
        rel = _validate_relative_path(name)
        if rel.parts[: len(prefix_path.parts)] != prefix_path.parts:
            raise ToolError("unsafe_source_path", "source inventory returned a path outside %s: %s" % (prefix, name))
        if name in seen:
            continue
        seen.add(name)
        source = repo / Path(*rel.parts)
        resolved = source.resolve(strict=False)
        if not _within(dm_root, resolved):
            raise ToolError("unsafe_source_path", "source path escapes dm/: %s" % name)
        rel_under_dm = PurePosixPath(*rel.parts[len(prefix_path.parts) :])
        if _excluded_source(rel_under_dm) or not source.exists():
            continue
        if source.is_symlink():
            raise ToolError("unsafe_input_symlink", "source files must not be symbolic links: %s" % name)
        digest, size = sha256_file(source)
        entries.append(SourceEntry(rel_under_dm, resolved, size, digest, bool(source.stat().st_mode & 0o111)))
    entries.sort(key=lambda entry: entry.relative.as_posix())
    rolled = hashlib.sha256()
    for entry in entries:
        rolled.update(entry.relative.as_posix().encode("utf-8"))
        rolled.update(b"\0")
        rolled.update(str(entry.size).encode("ascii"))
        rolled.update(b"\0")
        rolled.update(entry.sha256.encode("ascii"))
        rolled.update(b"\0")
    return entries, rolled.hexdigest()


def _source_fingerprint(dm_root: Path = SOURCE_DM) -> Tuple[List[SourceEntry], str]:
    return _source_inventory(dm_root)


def _write_json_atomic(path: Path, value: Any) -> None:
    if path.is_symlink():
        raise ToolError("unsafe_output_symlink", "refusing to write through a symbolic link: %s" % path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("." + path.name + ".tmp-" + uuid.uuid4().hex)
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def _ensure_source_snapshot(workspace: Path, entries: Sequence[SourceEntry], fingerprint: str) -> Path:
    root = workspace / "cache" / "sources" / ("v" + SOURCE_SNAPSHOT_VERSION) / fingerprint
    marker = root / "source-snapshot.json"
    if root.exists():
        if root.is_symlink() or marker.is_symlink():
            raise ToolError("incompatible_source_snapshot", "existing source snapshot is unsafe: %s" % root)
        try:
            current = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ToolError("incompatible_source_snapshot", "existing source snapshot is incomplete and will not be deleted: %s" % root) from exc
        if current.get("fingerprint") != fingerprint or not (root / "dm" / "tools" / "d2rm_android.py").is_file():
            raise ToolError("incompatible_source_snapshot", "existing source snapshot does not match its key and will not be reused: %s" % root)
        return root / "dm"

    root.parent.mkdir(parents=True, exist_ok=True)
    stage = root.parent / (".stage-" + fingerprint[:12] + "-" + uuid.uuid4().hex)
    created = False
    try:
        stage.mkdir()
        created = True
        copied = hashlib.sha256()
        for entry in entries:
            dest = stage / "dm" / Path(*entry.relative.parts)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(entry.path, dest)
            digest = sha256_file(dest)[0]
            if digest != entry.sha256:
                raise ToolError("source_changed_during_snapshot", "source changed while it was being isolated: %s" % entry.relative)
            if entry.executable:
                dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            copied.update(entry.relative.as_posix().encode("utf-8")); copied.update(b"\0")
            copied.update(str(entry.size).encode("ascii")); copied.update(b"\0")
            copied.update(entry.sha256.encode("ascii")); copied.update(b"\0")
        if copied.hexdigest() != fingerprint:
            raise ToolError("source_changed_during_snapshot", "the source inventory changed while it was being isolated; retry after edits finish.")
        _write_json_atomic(
            stage / "source-snapshot.json",
            {"version": 1, "fingerprint": fingerprint, "files": len(entries), "layout": "dm/"},
        )
        try:
            os.replace(stage, root)
        except OSError:
            if not root.exists():
                raise
            return _ensure_source_snapshot(workspace, entries, fingerprint)
        created = False
        return root / "dm"
    finally:
        if created and stage.exists():
            shutil.rmtree(stage)


def _read_product(snapshot_dm: Optional[Path] = None) -> str:
    path = (snapshot_dm or SOURCE_DM) / "PRODUCT"
    text = _safe_regular_file(path, "PRODUCT").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", text):
        raise ToolError("invalid_product", "PRODUCT is empty or contains unsupported characters: %r" % text)
    return text


def _parse_holes(path: Path, raw_size: int) -> Tuple[int, int]:
    if path.is_symlink():
        raise ToolError("unsafe_input_symlink", "archive hole table must not be a symbolic link: %s" % path)
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ToolError("invalid_holes", "cannot read archive hole table: %s" % path) from exc
    first = re.fullmatch(r"size\s+([0-9]+)", lines[0].strip()) if lines else None
    if not first or int(first.group(1)) != raw_size:
        raise ToolError("invalid_holes", "hole table first line must be `size %d`: %s" % (raw_size, path))
    total = 0
    previous_end = -1
    for number, line in enumerate(lines[1:], 2):
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 2 or not all(field.isdigit() for field in fields):
            raise ToolError("invalid_holes", "invalid hole extent on line %d: %s" % (number, path))
        offset, length = map(int, fields)
        if length <= 0 or offset < 0 or offset + length > raw_size or offset < previous_end:
            raise ToolError("invalid_holes", "hole extent is unordered or outside the archive on line %d: %s" % (number, path))
        previous_end = offset + length
        total += length
    if total >= raw_size:
        raise ToolError("invalid_holes", "hole table removes the complete archive: %s" % path)
    return raw_size - total, total


def _find_config(romfs_data: Path) -> Path:
    for name in CONFIG_NAMES:
        path = romfs_data / name
        if path.is_file() and not path.is_symlink():
            if path.stat().st_size <= 0:
                raise ToolError("empty_build_config", "owned RomFS build config is empty: %s" % path)
            return path
    raise ToolError(
        "missing_build_config",
        "owned RomFS/data has neither .build.config nor build.config: %s" % romfs_data,
        "Supply an extracted Update directory from your own matching game copy; this tool does not synthesize a config.",
    )


def inspect_game_update(
    update: Path,
    expected_exefs: Optional[Mapping[str, str]] = None,
    archive_sizes: Optional[Mapping[str, int]] = None,
) -> GameInfo:
    """Strictly identify the update by ExeFS hashes and all required resource bytes."""
    update = _safe_regular_dir(update, "Update directory")
    expected_exefs = expected_exefs or EXPECTED_EXEFS_SHA256
    archive_sizes = archive_sizes or ARCHIVE_RAW_SIZES
    exefs = _safe_regular_dir(update / "ExeFS", "ExeFS directory")
    romfs = _safe_regular_dir(update / "RomFS", "RomFS directory")
    romfs_data = _safe_regular_dir(romfs / "data", "RomFS data directory")
    exe_info: Dict[str, Any] = {}
    for name in ("main", "sdk", "subsdk0"):
        digest, size = sha256_file(exefs / name)
        wanted = expected_exefs.get(name)
        if digest != wanted:
            raise ToolError(
                "wrong_game_executable",
                "ExeFS/%s is not the verified %s payload (got %s, expected %s)." % (name, GAME_PROFILE, digest, wanted),
                "Archive filenames and console UI versions are not used as identity; use the exact Switch 1.0.34.0 update payload.",
            )
        exe_info[name] = {"sha256": digest, "size": size}
    manifest_path = _safe_regular_file(romfs_data / "build_manifest.psv", "build_manifest.psv")
    manifest_sha, manifest_size = sha256_file(manifest_path)
    archive_hashes: Dict[str, str] = {}
    stored_sizes: Dict[str, int] = {}
    archive_rows: List[Dict[str, Any]] = []
    for name in REQUIRED_ARCHIVES:
        raw_size = int(archive_sizes[name])
        archive = _safe_regular_file(romfs_data / name, "required RomFS archive")
        holes = romfs_data / (name + ".holes")
        expected_stored = raw_size
        holes_meta: Optional[Dict[str, Any]] = None
        if holes.exists():
            expected_stored, removed = _parse_holes(holes, raw_size)
            holes_sha, holes_size = sha256_file(holes)
            holes_meta = {"size": holes_size, "sha256": holes_sha, "zero_bytes_removed": removed}
        actual = archive.stat().st_size
        if actual != expected_stored:
            raise ToolError(
                "wrong_resource_size",
                "%s has %d bytes; this game profile requires %d%s." % (name, actual, expected_stored, " stored bytes after .holes" if holes_meta else " bytes"),
            )
        digest, hashed_size = sha256_file(archive)
        archive_hashes[name] = digest
        stored_sizes[name] = hashed_size
        row: Dict[str, Any] = {"name": name, "raw_size": raw_size, "size": hashed_size, "sha256": digest}
        if holes_meta:
            row["holes"] = holes_meta
        archive_rows.append(row)
    config = _find_config(romfs_data)
    config_sha, config_size = sha256_file(config)
    archive_set_sha = _digest_value(archive_rows)
    identity: Dict[str, Any] = {
        "profile": GAME_PROFILE,
        "title_id": TITLE_ID,
        "switch_version": SWITCH_VERSION,
        "pc_version": PC_VERSION,
        "exefs": exe_info,
        "build_manifest": {"sha256": manifest_sha, "size": manifest_size},
        "resource_archives": archive_rows,
        "resource_archive_set_sha256": archive_set_sha,
        "build_config": {"name": config.name, "sha256": config_sha, "size": config_size},
    }
    return GameInfo(
        update=update,
        identity=identity,
        identity_digest=_digest_value(identity),
        archive_sha256=archive_hashes,
        archive_sizes=stored_sizes,
        config_name=config.name,
        config_sha256=config_sha,
        config_size=config_size,
    )


def _copy_verified(source: Path, destination: Path, expected_sha: str, expected_size: int) -> None:
    if destination.exists() or destination.is_symlink():
        raise ToolError("output_collision", "refusing to overwrite an existing generated file: %s" % destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    digest, size = sha256_file(destination)
    if size != expected_size or digest != expected_sha:
        raise ToolError("copy_verification_failed", "copied file does not match its verified source: %s" % source)


def stage_data_resources(game: GameInfo, destination: Path, archive_entries: Mapping[str, Dict[str, Any]]) -> None:
    """Copy only the 18 required archives, selected config, and optional hole tables."""
    destination = _resolved(destination)
    if destination.exists():
        raise ToolError("output_collision", "data staging directory already exists: %s" % destination)
    romfs_data = game.update / "RomFS" / "data"
    destination.mkdir(parents=True)
    for name in REQUIRED_ARCHIVES:
        row = archive_entries[name]
        _copy_verified(romfs_data / name, destination / name, game.archive_sha256[name], game.archive_sizes[name])
        holes = romfs_data / (name + ".holes")
        if holes.exists():
            holes_meta = row["holes"]
            _copy_verified(holes, destination / (name + ".holes"), holes_meta["sha256"], holes_meta["size"])
    _copy_verified(romfs_data / game.config_name, destination / game.config_name, game.config_sha256, game.config_size)


def _quick_file_fingerprint(path: Path) -> str:
    path = _safe_regular_file(path, "game package")
    size = path.stat().st_size
    digest = hashlib.sha256(str(size).encode("ascii"))
    offsets = sorted({0, max(0, size // 2 - 32768), max(0, size - 65536)})
    with path.open("rb") as stream:
        for offset in offsets:
            stream.seek(offset)
            digest.update(offset.to_bytes(8, "little"))
            digest.update(stream.read(65536 if offset else min(65536, size)))
    return digest.hexdigest()


def _rom_input_key(roms: Sequence[Path], source_fingerprint: str) -> Tuple[str, List[Dict[str, Any]]]:
    rows: List[Dict[str, Any]] = []
    for path in roms:
        rows.append({"name": path.name, "size": path.stat().st_size, "sample_sha256": _quick_file_fingerprint(path)})
    return _digest_value({"source": source_fingerprint, "packages": rows}), rows


def _resolve_extraction_runtime(hactool: Optional[Path]) -> Tuple[str, Path]:
    if os.name == "nt":
        raise ToolError(
            "native_windows_extraction_not_supported",
            "Native Windows package extraction is not supported by this Mac-first tool because extract_game.sh requires Bash and its Windows interoperability has not been verified.",
            "Use --game-dir with an Update directory extracted on your own console/host, or run package extraction in a verified Bash/WSL workflow and pass that Update directory here.",
        )
    bash = shutil.which("bash")
    shasum = shutil.which("shasum")
    if not bash:
        raise ToolError("bash_required", "extract_game.sh requires a Bash executable on PATH.", "Install Bash or use --game-dir with already extracted owned game files.")
    if not shasum:
        raise ToolError("shasum_required", "extract_game.sh requires `shasum` on PATH for its ExeFS verification.", "Install a compatible shasum utility or use --game-dir.")
    wanted = hactool or (Path(os.environ["HACTOOL"]) if os.environ.get("HACTOOL") else None) or (Path(shutil.which("hactool")) if shutil.which("hactool") else None)
    if not wanted:
        raise ToolError(
            "hactool_required",
            "Package extraction requires hactool, but none was supplied or found on PATH.",
            "Build hactool from https://github.com/SciresM/hactool and pass --hactool PATH, or use --game-dir.",
        )
    wanted = _safe_regular_file(wanted, "hactool executable")
    if not os.access(wanted, os.X_OK):
        raise ToolError("hactool_not_executable", "hactool is not executable: %s" % wanted)
    return bash, wanted


def _sanitize_log(text: str, secrets: Sequence[str] = ()) -> str:
    for value in secrets:
        if value:
            text = text.replace(value, "<redacted-path>")
    text = HEX_SECRET_RE.sub("<redacted-key-like-value>", text)
    return text


def _run_child(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: Path,
    log_path: Path,
    label: str,
    verbose: bool,
    secrets: Sequence[str] = (),
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [str(item) for item in argv],
            cwd=str(cwd),
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
        )
    except OSError as exc:
        message = _sanitize_log("%s could not start: %s" % (label, exc), secrets)
        log_path.write_text(message + "\n", encoding="utf-8", newline="\n")
        raise ToolError("child_start_failed", message, "See %s" % log_path) from exc
    text = _sanitize_log((result.stdout or b"").decode("utf-8", "replace"), secrets)
    log_path.write_text(text + ("" if text.endswith("\n") else "\n"), encoding="utf-8", newline="\n")
    if verbose and text:
        sys.stderr.write(text if text.endswith("\n") else text + "\n")
        sys.stderr.flush()
    if result.returncode:
        tail = "\n".join(line for line in text.splitlines()[-8:] if line.strip())
        raise ToolError("child_failed", "%s exited with status %d.%s" % (label, result.returncode, ("\n" + tail) if tail else ""), "Sanitized log: %s" % log_path)


def _extraction_update(workspace: Path, input_key: str) -> Path:
    return workspace / "cache" / "extractions" / input_key / "data" / "switch" / PC_VERSION / "Update"


def _extract_packages(
    *,
    workspace: Path,
    snapshot_dm: Path,
    roms: Sequence[Path],
    keys: Path,
    hactool: Path,
    bash: str,
    input_key: str,
    verbose: bool,
) -> Path:
    update = _extraction_update(workspace, input_key)
    romfs_data = update / "RomFS" / "data"
    if romfs_data.is_dir():
        return update
    extraction_root = update.parents[3]
    if extraction_root.exists() and any(extraction_root.iterdir()):
        raise ToolError(
            "incompatible_extraction_cache",
            "The keyed extraction cache exists but is not a complete verified Update tree; it will not be deleted or reused: %s" % extraction_root,
        )
    extraction_root.mkdir(parents=True, exist_ok=True)
    tmp = workspace / "cache" / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({"DM_ROOT": str(extraction_root), "TMPDIR": str(tmp), "TMP": str(tmp), "TEMP": str(tmp), "HACTOOL": str(hactool), "PYTHONUTF8": "1"})
    argv = [bash, str(snapshot_dm / EXTRACT_SCRIPT), *map(str, roms), str(keys)]
    _run_child(
        argv,
        env=env,
        cwd=snapshot_dm,
        log_path=workspace / "cache" / "extractions" / input_key / "extract.log",
        label="extract_game.sh",
        verbose=verbose,
        secrets=(str(keys), keys.name),
    )
    return update


def _read_pack_info(pack: Path) -> Dict[str, str]:
    path = _safe_regular_file(pack / "pack_info.txt", "generated pack_info.txt")
    result: Dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result


def _validate_generated_pack(pack: Path, target: str, product: str) -> Dict[str, Any]:
    pack = _safe_regular_dir(pack, "generated pack")
    info = _read_pack_info(pack)
    if info.get("PRODUCT") != product:
        raise ToolError("pack_product_mismatch", "generated pack PRODUCT is %r; expected %r." % (info.get("PRODUCT"), product))
    if info.get("TARGET") != target:
        raise ToolError("pack_target_mismatch", "generated pack TARGET is %r; expected %r." % (info.get("TARGET"), target))
    shader_count = sum(1 for path in (pack / "shaders").rglob("*.xgs") if path.is_file() and not path.is_symlink()) if (pack / "shaders").is_dir() else 0
    if shader_count != EXPECTED_SHADER_COUNT:
        raise ToolError("wrong_shader_count", "generated pack has %d .xgs shaders; expected exactly %d." % (shader_count, EXPECTED_SHADER_COUNT))
    result: Dict[str, Any] = {"shader_xgs": shader_count}
    if target == "ohos":
        ui = pack / "ui-art"
        count = sum(1 for path in ui.iterdir() if path.is_file() and not path.is_symlink()) if ui.is_dir() else 0
        if count != EXPECTED_OHOS_UI_FILES:
            raise ToolError("wrong_ohos_ui_count", "generated HarmonyOS ui-art has %d files; expected exactly %d." % (count, EXPECTED_OHOS_UI_FILES))
        result["ui_art_files"] = count
    return result


def _validate_astc_pack(path: Path) -> Dict[str, Any]:
    pack = _safe_regular_dir(path, "ASTC pack")
    for relative in ("pack_index.tsv", "pack.json", "Legacy Device Pack/manifest.json"):
        _safe_regular_file(pack / relative, "ASTC pack member")
    try:
        info = json.loads((pack / "pack.json").read_text(encoding="utf-8"))
        name, size, digest = info["name"], int(info["size"]), str(info["sha256"]).lower()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ToolError("invalid_astc_pack", "ASTC pack.json is invalid: %s" % pack) from exc
    if name != "00-00000008.data" or size <= 0 or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ToolError("invalid_astc_pack", "ASTC pack.json has an invalid archive name, size, or SHA-256: %s" % pack)
    archive = _safe_regular_file(pack / "Legacy Device Pack" / "data" / name, "ASTC data archive")
    actual_sha, actual_size = sha256_file(archive)
    if actual_size != size or actual_sha != digest:
        raise ToolError("invalid_astc_pack", "ASTC data archive does not match pack.json: %s" % archive)
    return {"pack_json_sha256": sha256_file(pack / "pack.json")[0], "archive": name, "size": size, "sha256": digest}


def _workspace_plan(workspace: Path, source_dm: Path, protected_inputs: Sequence[Path]) -> None:
    workspace = _resolved(workspace)
    source_dm = _resolved(source_dm)
    source_root = source_dm.parent
    if _within(source_root, workspace) or _within(workspace, source_root):
        raise ToolError(
            "unsafe_workspace",
            "The workspace and source checkout must be disjoint: workspace=%s source=%s" % (workspace, source_root),
            "Choose a workspace outside the source checkout, for example a sibling working directory.",
        )
    for item in protected_inputs:
        item = _resolved(item)
        if _within(item, workspace) if item.is_dir() else False:
            raise ToolError("unsafe_workspace", "The workspace must not be inside an input directory: %s" % item)


def _output_disjoint(path: Path, workspace: Path, inputs: Sequence[Path]) -> None:
    path = _resolved(path)
    workspace = _resolved(workspace)
    if not _within(workspace, path):
        raise ToolError("unsafe_output", "generated output must stay inside the workspace: %s" % path)
    for item in inputs:
        item = _resolved(item)
        if _within(item, path) or _within(path, item):
            raise ToolError("unsafe_output_overlap", "generated output overlaps an input/source path: output=%s input=%s" % (path, item))


def _tree_files(root: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    root = _safe_regular_dir(root, "generated output root")
    for base, dirs, files in os.walk(root, topdown=True, followlinks=False):
        dirs.sort()
        files.sort()
        base_path = Path(base)
        for name in dirs:
            if (base_path / name).is_symlink():
                raise ToolError("unsafe_generated_symlink", "generated output contains a symbolic link: %s" % (base_path / name))
        for name in files:
            path = base_path / name
            if path.is_symlink():
                raise ToolError("unsafe_generated_symlink", "generated output contains a symbolic link: %s" % path)
            digest, size = sha256_file(path)
            rows.append({"path": path.relative_to(root).as_posix(), "sha256": digest, "size": size})
    rows.sort(key=lambda row: row["path"])
    return rows


def _existing_compatible(final: Path, expected: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    if not final.exists():
        return None
    if not final.is_dir() or final.is_symlink():
        raise ToolError("incompatible_output", "existing output path is not a safe directory and will not be deleted: %s" % final)
    manifest = final / MANIFEST_NAME
    if not manifest.is_file() or manifest.is_symlink():
        raise ToolError("incompatible_output", "existing output has no compatible resource manifest and will not be reused: %s" % final)
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ToolError("incompatible_output", "existing output manifest is invalid and will not be reused: %s" % manifest) from exc
    checks = {
        "schema_version": MANIFEST_VERSION,
        "status": "complete",
        "build_key": expected["build_key"],
        "target": expected["target"],
        "product": expected["product"],
        "source.fingerprint": expected["source_fingerprint"],
        "game_identity.sha256": expected["game_identity_digest"],
    }
    if data.get("schema_version") != checks["schema_version"] or data.get("status") != checks["status"] or data.get("build_key") != checks["build_key"] or data.get("target") != checks["target"] or data.get("product") != checks["product"]:
        raise ToolError("incompatible_output", "existing output is keyed differently and will not be reused or deleted: %s" % final)
    if data.get("source", {}).get("fingerprint") != checks["source.fingerprint"] or data.get("game_identity", {}).get("sha256") != checks["game_identity.sha256"]:
        raise ToolError("incompatible_output", "existing output source/game identity differs and will not be reused or deleted: %s" % final)
    layout = LAYOUTS[expected["target"]]
    if any(not (final / name).is_dir() for name in layout):
        raise ToolError("incompatible_output", "existing output lacks the complete import layout and will not be reused: %s" % final)
    return {
        "status": "reused",
        "dry_run": False,
        "target": expected["target"],
        "product": expected["product"],
        "build_key": expected["build_key"],
        "workspace": str(final.parents[2]),
        "cache_dir": expected["cache_dir"],
        "game_dir": expected["game_dir"],
        "runtime_root": str(final / expected["pack_name"]),
        "import_staging": str(final),
        "manifest_path": str(manifest),
        "source_fingerprint": expected["source_fingerprint"],
        "source_mode": data.get("source", {}).get("mode", "unknown"),
        "game_identity_sha256": expected["game_identity_digest"],
        "file_count": data.get("file_count", len(data.get("files", []))),
        "total_bytes": data.get("total_bytes", sum(int(row.get("size", 0)) for row in data.get("files", []))),
        "mobile_device_resources": str(final),
    }


def _tool_identity() -> Dict[str, Any]:
    """Hash the executables that materially select generation output, without running them."""
    rows: Dict[str, Any] = {}
    candidates = {"python": Path(sys.executable)}
    for name in TOOL_IDENTITY_NAMES:
        declared = os.environ.get("GLSLC") if name == "glslc" and os.environ.get("GLSLC") else None
        found = declared or shutil.which(name)
        candidates[name] = Path(found) if found else None
    for name, candidate in candidates.items():
        if not candidate:
            rows[name] = {"status": "missing", "path": None, "sha256": None, "size": None}
            continue
        path = candidate.expanduser().resolve(strict=False)
        if not path.is_file():
            rows[name] = {"status": "missing", "path": None if os.environ.get("GLSLC") == os.fspath(candidate) else str(path), "sha256": None, "size": None}
            continue
        digest, size = sha256_file(path)
        rows[name] = {"status": "ok", "path": str(path), "resolved_sha256": digest, "size": size}
    return rows


def _fresh_generation_source(snapshot_dm: Path, cache_dir: Path) -> Path:
    """Copy the clean base snapshot to a private mutable tree for one build."""
    root = cache_dir / ("generation-source-" + uuid.uuid4().hex)
    destination = root / "dm"
    try:
        destination.parent.mkdir(parents=True, exist_ok=False)
        shutil.copytree(snapshot_dm, destination, copy_function=shutil.copy2, symlinks=False)
        if not (destination / "tools" / "d2rm_android.py").is_file():
            raise ToolError("source_snapshot_invalid", "fresh generation source is missing tools/d2rm_android.py")
        return destination
    except Exception:
        if root.exists():
            shutil.rmtree(root)
        raise


def _child_env(snapshot_dm: Path, cache_dir: Path, tmp: Path) -> Dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("DM_")}
    paths = [str(snapshot_dm / "tools" / "winlf")]
    if env.get("PYTHONPATH"):
        paths.append(env["PYTHONPATH"])
    env.update(
        {
            "DM_ROOT": str(cache_dir),
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": os.pathsep.join(paths),
            "TMPDIR": str(tmp),
            "TMP": str(tmp),
            "TEMP": str(tmp),
        }
    )
    return env


def _build_import(
    *,
    target: str,
    workspace: Path,
    snapshot_dm: Path,
    game: GameInfo,
    product: str,
    source_fingerprint: str,
    source_mode: str,
    tool_identity: Mapping[str, Any],
    build_key: str,
    cache_dir: Path,
    final: Path,
    warm_seed: Optional[Path],
    astc_pack: Optional[Path],
    verbose: bool,
) -> Dict[str, Any]:
    parent = final.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = parent / (".stage-" + build_key[:16] + "-" + uuid.uuid4().hex)
    stage_created = False
    generation_root: Optional[Path] = None
    try:
        stage.mkdir()
        stage_created = True
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = workspace / "cache" / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        pack_output = stage / ".pack-output"
        generation_dm = _fresh_generation_source(snapshot_dm, cache_dir)
        generation_root = generation_dm.parent
        env = _child_env(generation_dm, cache_dir, tmp)
        argv: List[Path | str] = [sys.executable, generation_dm / D2RM_SCRIPT, "--target", target]
        if warm_seed:
            argv += ["--warm-seed", warm_seed]
        if astc_pack:
            argv += ["--astc-pack", astc_pack]
        argv += [game.update, pack_output]
        _run_child(
            argv,
            env=env,
            cwd=generation_dm / "ios",
            log_path=cache_dir / "logs" / "d2rm_android.log",
            label="d2rm_android.py",
            verbose=verbose,
            secrets=(),
        )
        generated = pack_output / "android" if target == "android" else pack_output / "harmony" / "dm"
        pack_meta = _validate_generated_pack(generated, target, product)
        pack_name = LAYOUTS[target][0]
        published_pack = stage / pack_name
        os.replace(generated, published_pack)
        shutil.rmtree(pack_output)

        archive_entries = {row["name"]: row for row in game.identity["resource_archives"]}
        stage_data_resources(game, stage / "data", archive_entries)
        archive_files = [stage / "data" / name for name in REQUIRED_ARCHIVES]

        # Rebuild the source inventory after generation: the snapshot itself may be mutated if an
        # upstream script still writes into its checkout, but that source digest remains the key.
        files = _tree_files(stage)
        total_bytes = sum(row["size"] for row in files)
        manifest: Dict[str, Any] = {
            "schema_version": MANIFEST_VERSION,
            "status": "complete",
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "build_key": build_key,
            "target": target,
            "product": product,
            "runtime_root": pack_name,
            "import_staging": {"path": ".", "layout": list(LAYOUTS[target]), "mobile_device_resources": "."},
            "pack": {"path": pack_name, "product": product, "target": target, **pack_meta},
            "data": {"path": "data", "archives": len(REQUIRED_ARCHIVES), "config": game.config_name},
            "source": {
                "fingerprint": source_fingerprint,
                "mode": source_mode,
                "isolation": "workspace snapshot of tracked and nonignored dm/ files" if source_mode == "git" else "workspace snapshot from SOURCE_EXPORT_MANIFEST.txt",
                "profile": GAME_PROFILE,
            },
            "build_environment": {"inherited_dm_variables": "removed", "tools": dict(tool_identity)},
            "game_identity": {"sha256": game.identity_digest, **game.identity},
            "game_dir": str(game.update),
            "file_count": len(files),
            "total_bytes": total_bytes,
            "files": files,
        }
        if warm_seed:
            manifest["optional_inputs"] = manifest.get("optional_inputs", {})
            manifest["optional_inputs"]["warm_seed_sha256"] = sha256_file(warm_seed)[0]
        if astc_pack:
            manifest["optional_inputs"] = manifest.get("optional_inputs", {})
            manifest["optional_inputs"]["astc_pack"] = _validate_astc_pack(astc_pack)
        manifest_path = stage / MANIFEST_NAME
        _write_json_atomic(manifest_path, manifest)
        try:
            os.replace(stage, final)
        except OSError:
            existing = _existing_compatible(final, {"build_key": build_key, "target": target, "product": product, "source_fingerprint": source_fingerprint, "game_identity_digest": game.identity_digest, "game_dir": str(game.update), "cache_dir": str(cache_dir), "pack_name": pack_name})
            if existing:
                return existing
            raise
        stage_created = False
        return {
            "status": "complete",
            "dry_run": False,
            "target": target,
            "product": product,
            "build_key": build_key,
            "workspace": str(workspace),
            "cache_dir": str(cache_dir),
            "game_dir": str(game.update),
            "runtime_root": str(final / pack_name),
            "import_staging": str(final),
            "manifest_path": str(final / MANIFEST_NAME),
            "source_fingerprint": source_fingerprint,
            "source_mode": source_mode,
            "game_identity_sha256": game.identity_digest,
            "file_count": len(files),
            "total_bytes": total_bytes,
            "mobile_device_resources": str(final),
        }
    finally:
        if generation_root and generation_root.exists():
            shutil.rmtree(generation_root)
        if stage_created and stage.exists():
            shutil.rmtree(stage)


def _parser() -> argparse.ArgumentParser:
    parser = CliParser(description="Portable resource preparation for Android and HarmonyOS.", add_help=True)
    parser.add_argument("--target", required=True, choices=TARGETS)
    parser.add_argument("--workspace", required=True, type=Path, help="workspace that will contain generated output and caches")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--game-dir", type=Path, help="already extracted Update directory containing ExeFS/ and RomFS/")
    source.add_argument("--rom", action="append", type=Path, help="owned .nsp/.xci package; repeat for base and update")
    parser.add_argument("--keys", type=Path, help="recipient-owned prod.keys, required with --rom")
    parser.add_argument("--hactool", type=Path, help="hactool executable; otherwise HACTOOL or PATH")
    parser.add_argument("--warm-seed", type=Path, help="optional pipeline warm-up seed")
    parser.add_argument("--astc-pack", type=Path, help="optional Android-only ASTC pack directory")
    parser.add_argument("--dry-run", action="store_true", help="validate and plan without creating directories, writing files, extracting, or generating")
    parser.add_argument("--json", action="store_true", help="emit one structured JSON object on stdout")
    parser.add_argument("--verbose", action="store_true", help="send sanitized child logs to stderr and retain them in workspace logs")
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.game_dir and (args.rom or args.keys):
        raise ToolError("source_conflict", "use either --game-dir or --rom/--keys, not both.")
    if args.rom:
        if not args.keys:
            raise ToolError("keys_required", "--keys is required with --rom.")
        for package in args.rom:
            suffix = package.suffix.lower()
            if suffix not in (".nsp", ".xci"):
                raise ToolError("unsupported_rom_type", "only .nsp and .xci packages are accepted: %s" % package)
    elif args.keys:
        raise ToolError("rom_required", "--keys is only valid with --rom.")
    if args.target != "android" and args.astc_pack:
        raise ToolError("target_option_mismatch", "--astc-pack is Android-only; target %s cannot use it." % args.target)


def _planned_report(args: argparse.Namespace, workspace: Path, source_fingerprint: str, source_mode: str, source_files: int, product: str, game: Optional[GameInfo], input_key: Optional[str], output: Optional[Path], cache_dir: Optional[Path]) -> Dict[str, Any]:
    report: Dict[str, Any] = {
        "status": "planned",
        "dry_run": True,
        "target": args.target,
        "product": product,
        "workspace": str(workspace),
        "source_fingerprint": source_fingerprint,
        "source_mode": source_mode,
        "source_file_count": source_files,
        "launch_extraction": bool(args.rom),
        "launch_generation": False,
        "write_performed": False,
        "import_layout": list(LAYOUTS[args.target]),
    }
    if input_key:
        report["extraction_cache_would_be"] = str(_extraction_update(workspace, input_key).parents[3])
    if output:
        report["output_would_be"] = str(output)
        report["runtime_root_would_be"] = str(output / LAYOUTS[args.target][0])
        report["manifest_would_be"] = str(output / MANIFEST_NAME)
    if cache_dir:
        report["cache_would_be"] = str(cache_dir)
    if game:
        report["game_dir"] = str(game.update)
        report["game_identity_sha256"] = game.identity_digest
    else:
        report["game_identity"] = {"status": "pending_package_extraction", "profile": GAME_PROFILE}
    return report


def run(argv: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    args = _parser().parse_args(argv)
    _validate_args(args)
    workspace = _resolved(args.workspace)
    game_dir = _resolved(args.game_dir) if args.game_dir else None
    roms = tuple(_safe_regular_file(path, "game package") for path in (args.rom or ()))
    keys = _safe_regular_file(args.keys, "keys file") if args.keys else None
    warm_seed = _safe_regular_file(args.warm_seed, "warm seed") if args.warm_seed else None
    astc_pack = _safe_regular_dir(args.astc_pack, "ASTC pack") if args.astc_pack else None
    hactool = _safe_regular_file(args.hactool, "hactool executable") if args.hactool else None
    protected = [SOURCE_DM]
    if game_dir:
        protected.append(game_dir)
    protected += list(roms)
    if keys:
        protected.append(keys)
    if warm_seed:
        protected.append(warm_seed)
    if astc_pack:
        protected.append(astc_pack)
    _workspace_plan(workspace, SOURCE_DM, protected)

    entries, source_fingerprint = _source_fingerprint()
    source_mode = _snapshot_mode(SOURCE_REPO)
    product = _read_product()
    game: Optional[GameInfo] = None
    extraction_bash: Optional[str] = None
    extraction_hactool: Optional[Path] = None
    input_key: Optional[str] = None
    rom_rows: List[Dict[str, Any]] = []
    if game_dir:
        game = inspect_game_update(game_dir)
    else:
        extraction_bash, extraction_hactool = _resolve_extraction_runtime(hactool)
        input_key, rom_rows = _rom_input_key(roms, source_fingerprint)

    if args.dry_run:
        warm_sha = sha256_file(warm_seed)[0] if warm_seed else None
        astc_identity = _validate_astc_pack(astc_pack) if astc_pack else None
        tools_identity = _tool_identity()
        output: Optional[Path] = None
        cache_dir: Optional[Path] = None
        if game:
            build_basis = {
                "target": args.target,
                "source_fingerprint": source_fingerprint,
                "game_identity_sha256": game.identity_digest,
                "warm_seed_sha256": warm_sha,
                "astc_pack": astc_identity,
                "tools": tools_identity,
            }
            build_key = _digest_value(build_basis)
            output = workspace / "mobile-resources" / args.target / build_key
            cache_dir = workspace / "cache" / "mobile-resources" / args.target / build_key
        return _planned_report(args, workspace, source_fingerprint, source_mode, len(entries), product, game, input_key, output, cache_dir)

    snapshot_dm = _ensure_source_snapshot(workspace, entries, source_fingerprint)
    if not game:
        assert keys is not None and extraction_bash is not None and extraction_hactool is not None and input_key is not None
        extracted_update = _extract_packages(
            workspace=workspace,
            snapshot_dm=snapshot_dm,
            roms=roms,
            keys=keys,
            hactool=extraction_hactool,
            bash=extraction_bash,
            input_key=input_key,
            verbose=args.verbose,
        )
        game = inspect_game_update(extracted_update)
        # The build must correspond to the source snapshot that performed extraction.
        current_entries, current_fp = _source_fingerprint()
        if current_fp != source_fingerprint:
            raise ToolError("source_changed_during_run", "source changed after extraction; rerun with the new source fingerprint.")

    assert game is not None
    warm_sha = sha256_file(warm_seed)[0] if warm_seed else None
    astc_identity = _validate_astc_pack(astc_pack) if astc_pack else None
    tools_identity = _tool_identity()
    build_basis = {
        "target": args.target,
        "source_fingerprint": source_fingerprint,
        "game_identity_sha256": game.identity_digest,
        "warm_seed_sha256": warm_sha,
        "astc_pack": astc_identity,
        "tools": tools_identity,
    }
    build_key = _digest_value(build_basis)
    final = workspace / "mobile-resources" / args.target / build_key
    cache_dir = workspace / "cache" / "mobile-resources" / args.target / build_key
    inputs = [game.update, SOURCE_DM] + ([warm_seed] if warm_seed else []) + ([astc_pack] if astc_pack else [])
    _output_disjoint(final, workspace, [item for item in inputs if item is not None])
    _output_disjoint(cache_dir, workspace, [game.update])
    expected = {
        "build_key": build_key,
        "target": args.target,
        "product": product,
        "source_fingerprint": source_fingerprint,
        "game_identity_digest": game.identity_digest,
        "game_dir": str(game.update),
        "cache_dir": str(cache_dir),
        "pack_name": LAYOUTS[args.target][0],
    }
    existing = _existing_compatible(final, expected)
    if existing:
        return existing
    return _build_import(
        target=args.target,
        workspace=workspace,
        snapshot_dm=snapshot_dm,
        game=game,
        product=product,
        source_fingerprint=source_fingerprint,
        source_mode=source_mode,
        tool_identity=tools_identity,
        build_key=build_key,
        cache_dir=cache_dir,
        final=final,
        warm_seed=warm_seed,
        astc_pack=astc_pack,
        verbose=args.verbose,
    )


def _print_human(report: Mapping[str, Any]) -> None:
    status = report.get("status", "unknown")
    if status == "planned":
        print("Resource preparation plan (%s, dry-run; no writes)" % report.get("target"))
        if report.get("output_would_be"):
            print("Output: %s" % report["output_would_be"])
        else:
            print("Game identity will be established after package extraction.")
        print("Source fingerprint: %s" % report.get("source_fingerprint", ""))
        return
    print("Resource staging %s: %s" % (status, report.get("import_staging", "")))
    print("Runtime root: %s" % report.get("runtime_root", ""))
    print("Manifest: %s" % report.get("manifest_path", ""))
    print("mobile_device.py --resources %s" % report.get("mobile_device_resources", ""))


def main(argv: Optional[Sequence[str]] = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    json_mode = "--json" in raw
    try:
        report = run(raw)
        if json_mode:
            print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        else:
            _print_human(report)
        return 0
    except ToolError as exc:
        if json_mode:
            print(json.dumps({"status": "failed", "error": exc.as_dict()}, ensure_ascii=False, sort_keys=True))
        else:
            print("mobile_resources: %s (%s)" % (exc.message, exc.code), file=sys.stderr)
            if exc.hint:
                print(exc.hint, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        if json_mode:
            print(json.dumps({"status": "failed", "error": {"code": "interrupted", "message": "Interrupted."}}, ensure_ascii=False, sort_keys=True))
        return 130
    except Exception as exc:
        if json_mode:
            print(json.dumps({"status": "failed", "error": {"code": "internal_error", "message": str(exc)}}, ensure_ascii=False, sort_keys=True))
        else:
            print("mobile_resources: internal error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
