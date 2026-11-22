#!/usr/bin/env python3
"""Publish a verified D2R resource handoff to a HarmonyOS debug app's common cache.

The helper never writes module-sandbox files and never extracts or publishes game
resources itself. It validates a complete ``dm/`` + ``data/`` staging tree, creates
a stored Zip64 archive, sends it through bundle-scoped HDC, verifies its SHA-256,
then sends a small request JSON. The app imports from the request using its own
sandbox permissions.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, Optional, Sequence, Tuple
import uuid
import zipfile


SOURCE_ROOT = Path(__file__).resolve().parents[2]
COMMON_BASE = "./data/storage/el2/base"
COMMON_CACHE = COMMON_BASE + "/cache"
REQUEST_NAME = "d2r-installer-request.json"
TIMEOUT_SECONDS = 30 * 60
HASH_CHUNK = 8 * 1024 * 1024


class ToolError(Exception):
    def __init__(self, code: str, message: str, *, recovery: Optional[Dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.recovery = recovery or {}


@dataclass(frozen=True)
class Entry:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Bundle:
    root: Path
    manifest_bytes: bytes
    manifest_sha256: str
    files: Tuple[Entry, ...]
    directories: Tuple[str, ...]


@dataclass(frozen=True)
class Result:
    returncode: int
    output: str


def _sha256(path: Path) -> Tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(HASH_CHUNK), b""):
            digest.update(block)
            size += len(block)
    return digest.hexdigest(), size


def _safe_path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ToolError("invalid_manifest_path", "resource manifest contains an unsafe path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ToolError("invalid_manifest_path", "resource manifest contains an unsafe path: %r" % value)
    if len(path.parts) < 2 or path.parts[0] not in ("dm", "data"):
        raise ToolError("invalid_manifest_path", "resource files must be below dm/ or data/: %s" % value)
    return path.as_posix()


def _walk(root: Path) -> Tuple[Tuple[Entry, ...], Tuple[str, ...]]:
    files = []
    directories = set()
    for top in ("dm", "data"):
        base = root / top
        if base.is_symlink() or not base.is_dir():
            raise ToolError("incomplete_resources", "--resources must contain a regular %s/ directory" % top)
        directories.add(top)
        for current, dirs, names in os.walk(base, followlinks=False):
            current_path = Path(current)
            for name in list(dirs) + names:
                local = current_path / name
                if local.is_symlink():
                    raise ToolError("unsafe_resource_symlink", "resource tree contains a symlink: %s" % local)
            for name in sorted(dirs):
                directories.add((current_path / name).relative_to(root).as_posix())
            for name in sorted(names):
                local = current_path / name
                if not local.is_file():
                    raise ToolError("resource_not_regular_file", "resource entry is not a regular file: %s" % local)
                before = local.stat()
                digest, size = _sha256(local)
                after = local.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ToolError("resource_changed_during_hash", "resource changed while being hashed: %s" % local)
                files.append(Entry(local.relative_to(root).as_posix(), size, digest))
    files.sort(key=lambda row: row.path)
    return tuple(files), tuple(sorted(directories))


def _read_bundle(resources: Path) -> Bundle:
    requested = resources.expanduser()
    if requested.is_symlink():
        raise ToolError("unsafe_resource_symlink", "--resources must not be a symlink")
    resources = requested.resolve()
    if not resources.is_dir():
        raise ToolError("resources_not_found", "--resources must name a regular directory: %s" % resources)
    manifest_path = resources / "resource-manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ToolError("manifest_missing", "--resources is missing resource-manifest.json")
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, UnicodeError, ValueError) as exc:
        raise ToolError("manifest_invalid", "cannot parse resource-manifest.json: %s" % exc) from exc
    if not isinstance(manifest, dict) or manifest.get("status") != "complete" or manifest.get("target") != "ohos":
        raise ToolError("manifest_incomplete", "resource-manifest.json must have status=complete and target=ohos")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ToolError("manifest_incomplete", "resource-manifest.json must contain a nonempty files inventory")
    declared: Dict[str, Entry] = {}
    for row in raw_files:
        if not isinstance(row, dict):
            raise ToolError("manifest_invalid", "resource-manifest.json files entries must be objects")
        path = _safe_path(row.get("path"))
        size = row.get("size")
        digest = row.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ToolError("manifest_invalid", "invalid size for %s" % path)
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest.lower()):
            raise ToolError("manifest_invalid", "invalid SHA-256 for %s" % path)
        if path in declared:
            raise ToolError("manifest_invalid", "duplicate manifest path: %s" % path)
        declared[path] = Entry(path, size, digest.lower())
    actual_files, actual_dirs = _walk(resources)
    actual = {row.path: row for row in actual_files}
    if actual != declared:
        missing = sorted(set(declared) - set(actual))
        extra = sorted(set(actual) - set(declared))
        changed = sorted(path for path in set(actual) & set(declared) if actual[path] != declared[path])
        detail = "missing=%s extra=%s changed=%s" % (missing[:3], extra[:3], changed[:3])
        raise ToolError("manifest_mismatch", "actual resources do not match resource-manifest.json: " + detail)
    if manifest.get("file_count", len(declared)) != len(declared):
        raise ToolError("manifest_mismatch", "resource-manifest.json file_count is inconsistent")
    if manifest.get("total_bytes", sum(row.size for row in actual_files)) != sum(row.size for row in actual_files):
        raise ToolError("manifest_mismatch", "resource-manifest.json total_bytes is inconsistent")
    return Bundle(resources, manifest_bytes, hashlib.sha256(manifest_bytes).hexdigest(), actual_files, actual_dirs)


def _temp_root() -> Path:
    root = Path(tempfile.gettempdir()).expanduser().resolve()
    try:
        root.relative_to(SOURCE_ROOT.resolve())
    except ValueError:
        return root
    raise ToolError("unsafe_temp", "TMPDIR resolves inside the source checkout: %s" % root)


def _create_zip(bundle: Bundle, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        archive.writestr("resource-manifest.json", bundle.manifest_bytes)
        for directory in bundle.directories:
            archive.writestr(directory.rstrip("/") + "/", b"")
        for entry in bundle.files:
            archive.write(bundle.root / entry.path, arcname=entry.path)
    with zipfile.ZipFile(destination, "r") as archive:
        if archive.read("resource-manifest.json") != bundle.manifest_bytes:
            raise ToolError("archive_verify_failed", "created archive has a different manifest")
        for entry in bundle.files:
            digest = hashlib.sha256()
            with archive.open(entry.path, "r") as stream:
                for block in iter(lambda: stream.read(HASH_CHUNK), b""):
                    digest.update(block)
            if digest.hexdigest() != entry.sha256:
                raise ToolError("archive_verify_failed", "created archive differs from actual file: %s" % entry.path)


def _run(argv: Sequence[str]) -> Result:
    command = tuple(str(item) for item in argv)
    try:
        result = subprocess.run(
            list(command), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", timeout=TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Result(127, str(exc))
    return Result(result.returncode, result.stdout or "")


def _shell(bridge: str, serial: str, package: str, script: str, *, bundle: bool = True) -> Result:
    argv = [bridge, "-t", serial, "shell"]
    if bundle:
        argv.extend(("-b", package))
    if "\n" in script:
        argv.extend(("sh", "-c", shlex.quote(script)))
    else:
        argv.append(script)
    return _run(argv)


def _assert_stopped(bridge: str, serial: str, package: str) -> None:
    result = _shell(bridge, serial, package, "pidof %s" % shlex.quote(package), bundle=False)
    pids = re.findall(r"(?m)^\s*(\d+)(?:\s+\d+)*\s*$", result.output)
    if pids:
        raise ToolError("app_running", "the application is running; stop it before publishing an import request")
    if result.returncode not in (0, 1) or (result.output.strip() and not pids):
        raise ToolError("process_probe_failed", "could not determine whether the application is stopped: %s" % result.output.strip())


def _preflight_script(module: str) -> str:
    files = COMMON_BASE + "/haps/" + module + "/files"
    request = COMMON_CACHE + "/" + REQUEST_NAME
    dm = files + "/dm"
    return r'''
files=%s
request=%s
dm=$files/dm
if [ -e "$request" ] || [ -L "$request" ]; then echo D2R_BLOCK_REQUEST_EXISTS; exit 0; fi
if [ ! -d "$files" ]; then echo D2R_BLOCK_SANDBOX_MISSING; exit 0; fi
if [ -e "$dm" ] || [ -L "$dm" ]; then
  if [ ! -d "$dm" ] || [ -L "$dm" ]; then echo D2R_BLOCK_DM_NOT_DIRECTORY; exit 0; fi
  for entry in "$dm"/* "$dm"/.[!.]* "$dm"/..?*; do
    if [ -e "$entry" ] || [ -L "$entry" ]; then
      if [ "$entry" != "$dm/boot.log" ] || [ ! -f "$entry" ] || [ -L "$entry" ]; then
        echo "D2R_BLOCK_DM_ENTRY:$entry"; exit 0
      fi
    fi
  done
fi
romfs_data=$files/romfs/data
if [ -e "$romfs_data" ] || [ -L "$romfs_data" ]; then
  if [ ! -d "$romfs_data" ] || [ -L "$romfs_data" ]; then echo D2R_BLOCK_ROMFS_DATA; exit 0; fi
  for entry in "$romfs_data"/* "$romfs_data"/.[!.]* "$romfs_data"/..?*; do
    if [ -e "$entry" ] || [ -L "$entry" ]; then echo D2R_BLOCK_ROMFS_DATA; exit 0; fi
  done
fi
for save in "$files/save" "$files/saves"; do
  if [ -d "$save" ] && [ ! -L "$save" ]; then
    for entry in "$save"/* "$save"/.[!.]* "$save"/..?*; do
      if [ -e "$entry" ] || [ -L "$entry" ]; then echo "D2R_BLOCK_SAVES:$save"; exit 0; fi
    done
  elif [ -e "$save" ] || [ -L "$save" ]; then
    echo "D2R_BLOCK_SAVES:$save"; exit 0
  fi
done
echo D2R_RUNTIME_OK
''' % (shlex.quote(files), shlex.quote(request))


def _preflight(bridge: str, serial: str, package: str, module: str) -> None:
    result = _shell(bridge, serial, package, _preflight_script(module))
    last = [line.strip() for line in result.output.splitlines() if line.strip()]
    if result.returncode != 0:
        raise ToolError("preflight_failed", "bundle-scoped shell failed; initialize the sandbox with one app launch first: %s" % result.output.strip())
    if not last or last[-1] != "D2R_RUNTIME_OK":
        reason = last[-1] if last else "no sentinel"
        code = "pending_request" if "REQUEST_EXISTS" in reason else "runtime_not_fresh"
        raise ToolError(code, "resource import blocked before transfer: %s" % reason)


def _remote_hash(bridge: str, serial: str, package: str, path: str) -> str:
    probe = _shell(bridge, serial, package, "if [ -x /bin/sha256sum ]; then printf '/bin/sha256sum\\n'; elif command -v sha256sum >/dev/null 2>&1; then printf 'sha256sum\\n'; elif command -v shasum >/dev/null 2>&1; then printf 'shasum\\n'; else printf 'none\\n'; fi")
    lines = [line.strip() for line in probe.output.splitlines() if line.strip()]
    command = lines[-1] if lines else "none"
    if probe.returncode != 0 or command not in {"/bin/sha256sum", "sha256sum", "shasum"}:
        raise ToolError("remote_hash_unavailable", "remote shell cannot calculate SHA-256")
    quoted = shlex.quote(path)
    if command == "shasum":
        operation = "shasum -a 256 %s" % quoted
    else:
        operation = "%s %s" % (command, quoted)
    result = _shell(bridge, serial, package, "if hash=$(%s 2>/dev/null); then printf '%%s\\nD2R_HASH_OK\\n' \"$hash\"; else printf 'D2R_HASH_FAILED\\n'; fi" % operation)
    output = [line.strip() for line in result.output.splitlines() if line.strip()]
    if result.returncode != 0 or not output or output[-1] != "D2R_HASH_OK":
        raise ToolError("remote_hash_failed", "could not hash remote file: %s" % path)
    digest = output[0].split(None, 1)[0].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ToolError("remote_hash_failed", "remote hash output is invalid for %s" % path)
    return digest


def _send(bridge: str, serial: str, package: str, local: Path, remote: str) -> None:
    result = _run([bridge, "-t", serial, "file", "send", "-b", package, str(local), remote])
    if result.returncode != 0:
        raise ToolError("transfer_failed", "HDC send failed for %s: %s" % (local.name, result.output.strip()))


def _validate(serial: str, package: str, module: str, bridge: Optional[str]) -> str:
    if not serial or any(ch.isspace() or ord(ch) < 32 for ch in serial):
        raise ToolError("invalid_serial", "--serial must be one explicit device identifier")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", package):
        raise ToolError("invalid_package", "--package must be a bundle name")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._-]*", module):
        raise ToolError("invalid_module", "--module must be one simple HarmonyOS module name")
    if bridge:
        path = Path(bridge).expanduser().resolve()
        if not path.is_file():
            raise ToolError("bridge_not_found", "--bridge does not name a file: %s" % path)
        return str(path)
    found = shutil.which("hdc")
    if not found:
        raise ToolError("bridge_not_found", "hdc was not found on PATH; pass --bridge PATH")
    return str(Path(found).resolve())


def _report(args: argparse.Namespace, bundle: Bundle, **extra: Any) -> Dict[str, Any]:
    report = {
        "schema_version": 1, "status": "pending", "imported": False, "dry_run": bool(args.dry_run),
        "serial": args.serial, "package": args.package, "module": args.module,
        "resources": str(bundle.root), "manifest_sha256": bundle.manifest_sha256,
        "files": len(bundle.files), "bytes": sum(row.size for row in bundle.files), "error": None,
    }
    report.update(extra)
    return report


def run(args: argparse.Namespace) -> Tuple[Dict[str, Any], int]:
    current: Optional[Dict[str, Any]] = None
    temp_root: Optional[Path] = None
    try:
        bundle = _read_bundle(Path(args.resources))
        bridge = _validate(args.serial, args.package, args.module, args.bridge)
        report = _report(args, bundle, bridge=bridge)
        token = uuid.uuid4().hex
        archive_name = "d2r-installer-%s.zip" % token
        remote_archive = COMMON_CACHE + "/" + archive_name
        remote_request = COMMON_CACHE + "/" + REQUEST_NAME
        report.update({
            "token": token, "archive": archive_name, "remote_archive": remote_archive,
            "request": REQUEST_NAME, "remote_request": remote_request,
            "caveats": ["app import completion is not awaited", "remote archive and request cleanup belongs to the app"],
        })
        if args.dry_run:
            report["status"] = "planned"
            report["plan"] = {"archive": "ZIP_STORED with Zip64", "publish": "after remote SHA-256 verification", "launch_app": False}
            return report, 0

        _assert_stopped(bridge, args.serial, args.package)
        _preflight(bridge, args.serial, args.package, args.module)
        temp_root = _temp_root()
        workspace = Path(tempfile.mkdtemp(prefix="d2r-ohos-import-", dir=str(temp_root)))
        current = {"local_staging": str(workspace), "remote_archive": remote_archive, "remote_request": remote_request}
        archive = workspace / archive_name
        request = workspace / REQUEST_NAME
        _create_zip(bundle, archive)
        archive_sha, archive_bytes = _sha256(archive)
        payload = {
            "schema_version": 1, "token": token, "archive": archive_name,
            "manifest_sha256": bundle.manifest_sha256,
            "archive_sha256": archive_sha, "archive_bytes": archive_bytes,
        }
        request.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
        request_sha, _ = _sha256(request)
        report["archive_sha256"] = archive_sha
        report["archive_bytes"] = archive_bytes
        report["local_staging"] = str(workspace)

        _assert_stopped(bridge, args.serial, args.package)
        _send(bridge, args.serial, args.package, archive, remote_archive)
        if _remote_hash(bridge, args.serial, args.package, remote_archive) != archive_sha:
            raise ToolError("archive_verify_failed", "remote archive SHA-256 does not match the local archive")

        # A request may have appeared after preflight; never overwrite it.
        _preflight(bridge, args.serial, args.package, args.module)
        _assert_stopped(bridge, args.serial, args.package)
        _send(bridge, args.serial, args.package, request, remote_request)
        if _remote_hash(bridge, args.serial, args.package, remote_request) != request_sha:
            raise ToolError("request_verify_failed", "remote request SHA-256 does not match the local request")
        report.update({"status": "awaiting_app_import", "request_sha256": request_sha})
        shutil.rmtree(workspace)
        report["local_staging"] = None
        return report, 0
    except ToolError as exc:
        if "report" not in locals():
            report = {"schema_version": 1, "status": "failed", "imported": False, "dry_run": bool(args.dry_run), "error": None}
        report["status"] = "failed"
        report["error"] = {"code": exc.code, "message": exc.message}
        if exc.recovery:
            report["recovery"] = exc.recovery
        if current:
            report.setdefault("recovery", {}).update(current)
        return report, 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Publish a D2R resource archive to a HarmonyOS debug app")
    parser.add_argument("--resources", required=True, help="complete parent containing dm/, data/, and resource-manifest.json")
    parser.add_argument("--serial", required=True, help="explicit connected HarmonyOS device serial")
    parser.add_argument("--bridge", help="hdc executable; otherwise discover hdc on PATH")
    parser.add_argument("--package", default="com.xiao.boluo", help="debug application bundle name")
    parser.add_argument("--module", default="entry", help="HarmonyOS module whose sandbox is checked read-only")
    parser.add_argument("--dry-run", action="store_true", help="validate local inputs without device access or writes")
    parser.add_argument("--json", action="store_true", help="emit one structured JSON object")
    args = parser.parse_args(argv)
    report, status = run(args)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return status


if __name__ == "__main__":
    sys.exit(main())
