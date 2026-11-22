#!/usr/bin/env python3
"""Install D2R Mobile and stage its import pack on an explicitly selected device.

The adapter intentionally knows nothing about signing tools, credential bypasses, app
data, saves, or the app's Vulkan pipeline cache. Remote resource writes stay below the
selected external import directory. An existing import is moved to a timestamped
sibling before a verified stage is published and is retained for recovery.

Usage:
  python3 mobile_device.py devices --target android [--bridge PATH] [--json]
  python3 mobile_device.py install --target android --serial SERIAL \
      --artifact APP.apk [--resources IMPORT_DIR] [--remote-dir DIR] [--launch] \
      [--dry-run] [--json]

Both adb and hdc are discovered on PATH unless --bridge is supplied. Install always
requires an explicit serial. Android resources must have android/ and data/ siblings;
HarmonyOS resources must have dm/ and data/ siblings.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
import uuid


TARGETS: Dict[str, Dict[str, Any]] = {
    "android": {
        "bridge": "adb",
        "artifact": ".apk",
        "package": "com.xiaoboluo.d2r.mate60",
        "layout": ("android", "data"),
        "default_remote": "/sdcard/Download/S2X/D2R Mobile",
    },
    "ohos": {
        "bridge": "hdc",
        "artifact": ".hap",
        "package": "com.xiao.boluo",
        "layout": ("dm", "data"),
        "default_remote": None,
    },
}

INSTALL_TIMEOUT_SECONDS = 15 * 60
TRANSFER_TIMEOUT_SECONDS = 60 * 60
DEVICE_TIMEOUT_SECONDS = 30
LAUNCH_TIMEOUT_SECONDS = 30
HASH_BATCH_MAX_FILES = 128
HASH_BATCH_MAX_BYTES = 48 * 1024
SOURCE_ROOT = Path(__file__).resolve().parents[2]


class AdapterError(Exception):
    """A user-facing validation or device-operation error."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DeviceRecord:
    serial: str
    state: str
    detail: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {"serial": self.serial, "state": self.state, "detail": self.detail}


@dataclass(frozen=True)
class ProcessResult:
    argv: Tuple[str, ...]
    returncode: int
    output: str


@dataclass(frozen=True)
class ManifestEntry:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ResourceManifest:
    files: Tuple[ManifestEntry, ...]
    directories: Tuple[str, ...]
    total_size: int
    digest: str

    def summary(self) -> Dict[str, Any]:
        return {
            "files": len(self.files),
            "directories": len(self.directories),
            "bytes": self.total_size,
            "manifest_sha256": self.digest,
            "verification_batch_limit_files": HASH_BATCH_MAX_FILES,
            "verification_batch_limit_bytes": HASH_BATCH_MAX_BYTES,
        }


@dataclass(frozen=True)
class InstallRequest:
    target: str
    bridge: Optional[str]
    serial: str
    artifact: Path
    package: str
    resources: Optional[Path]
    resource_manifest: Optional[ResourceManifest]
    pack_product: Optional[str]
    pack_target: Optional[str]
    remote_dir: Optional[str]
    launch: bool
    dry_run: bool


def _step(name: str, status: str = "pending", **detail: Any) -> Dict[str, Any]:
    item: Dict[str, Any] = {"name": name, "status": status}
    item.update(detail)
    return item


def _report(command: str, target: Optional[str]) -> Dict[str, Any]:
    report = {
        "command": command,
        "target": target,
        "status": "pending",
        "steps": [],
        "error": None,
    }
    if command == "install":
        report["installed"] = False
    return report


def _fail(report: Dict[str, Any], code: str, message: str) -> Dict[str, Any]:
    report["status"] = "failed"
    report["error"] = {"code": code, "message": message}
    return report


def _resolve_bridge(target: str, explicit: Optional[str]) -> str:
    wanted = TARGETS[target]["bridge"]
    if explicit:
        path = os.path.abspath(os.path.expanduser(explicit))
        if not os.path.isfile(path):
            raise AdapterError("bridge_not_found", "--bridge does not name a file: %s" % path)
        return path

    candidates = [wanted]
    if os.name == "nt":
        candidates.append(wanted + ".exe")
    for name in candidates:
        path = shutil.which(name)
        if path:
            return os.path.abspath(path)
    raise AdapterError("bridge_not_found", "%s was not found on PATH; pass --bridge PATH" % wanted)


def _run(argv: Sequence[str], timeout: int) -> ProcessResult:
    """Run without a host shell; device-shell strings are quoted by their callers."""
    args = tuple(str(x) for x in argv)
    try:
        result = subprocess.run(
            list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ProcessResult(args, 127, str(exc))
    return ProcessResult(args, int(result.returncode), result.stdout or "")


def _serial_prefix(target: str, bridge: str, serial: str) -> List[str]:
    return [bridge, "-s" if target == "android" else "-t", serial]


def _device_shell(target: str, bridge: str, serial: str, command: Sequence[str]) -> List[str]:
    return _serial_prefix(target, bridge, serial) + ["shell", shlex.join(command)]


def _device_shell_script(target: str, bridge: str, serial: str, script: str) -> List[str]:
    return _serial_prefix(target, bridge, serial) + ["shell", script]


def _normalise_state(state: str, target: str) -> str:
    value = state.strip().lower()
    if "unauthor" in value:
        return "unauthorized"
    if "offline" in value:
        return "offline"
    if value in {"device", "ready", "connected", "online"}:
        return "device"
    if not value and target == "ohos":
        return "device"
    return "unknown"


def parse_devices(output: str, target: str) -> List[DeviceRecord]:
    """Parse adb devices -l or hdc list targets text without choosing a device."""
    rows: List[DeviceRecord] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith("*") or line.lower().startswith("list of devices"):
            continue
        if target == "ohos" and line.lower() in {"[empty]", "empty"}:
            continue
        fields = line.split()
        if target == "android":
            if len(fields) < 2:
                continue
            serial, state = fields[0], _normalise_state(fields[1], target)
            detail = " ".join(fields[2:])
        else:
            serial = fields[0]
            state = "device"
            for field in fields[1:]:
                normal = _normalise_state(field, target)
                if normal != "unknown":
                    state = normal
                    break
            detail = " ".join(fields[1:])
        rows.append(DeviceRecord(serial, state, detail))
    return rows


def classify_devices(devices: Sequence[DeviceRecord]) -> Tuple[str, List[str]]:
    """Return a primary connection state while retaining independent issues."""
    issues: List[str] = []
    if len(devices) > 1:
        issues.append("multiple_devices")
    if any(device.state == "unauthorized" for device in devices):
        issues.append("unauthorized")
    if any(device.state == "offline" for device in devices):
        issues.append("offline")
    if any(device.state == "unknown" for device in devices):
        issues.append("unknown_state")

    if not devices:
        return "no_devices", issues
    if len(devices) > 1:
        return "multiple_devices", issues
    state = devices[0].state
    if state in {"device", "unauthorized", "offline", "unknown"}:
        return state, issues
    return "unknown_state", issues


def _read_devices(target: str, bridge: str) -> Tuple[List[DeviceRecord], ProcessResult]:
    command = [bridge, "devices", "-l"] if target == "android" else [bridge, "list", "targets"]
    result = _run(command, DEVICE_TIMEOUT_SECONDS)
    devices = parse_devices(result.output, target) if result.returncode == 0 else []
    return devices, result


def _validate_serial(serial: str) -> None:
    if not serial or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in serial):
        raise AdapterError("invalid_serial", "--serial must be one non-empty device identifier without whitespace or control characters")


def _validate_package(package: str) -> None:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", package):
        raise AdapterError("invalid_package", "--package is not a plausible application package / bundle name")


def _validate_remote_dir(remote_dir: str, target: str) -> str:
    if not remote_dir:
        raise AdapterError("invalid_remote_dir", "--remote-dir must not be empty")
    if remote_dir.startswith("//"):
        raise AdapterError("invalid_remote_dir", "--remote-dir must name one local device path, not a host-qualified path")
    if "\\" in remote_dir:
        raise AdapterError("invalid_remote_dir", "--remote-dir must use device POSIX '/' separators")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in remote_dir):
        raise AdapterError("invalid_remote_dir", "--remote-dir contains a newline or control character")
    if any(part == ".." for part in PurePosixPath(remote_dir).parts):
        raise AdapterError("invalid_remote_dir", "--remote-dir must not contain '..' traversal")
    normalised = remote_dir.rstrip("/") or "/"
    if PurePosixPath(remote_dir) == PurePosixPath(".") or normalised == "/":
        raise AdapterError("invalid_remote_dir", "--remote-dir must name a specific external directory")
    lower = normalised.lower()
    if (lower == "/data" or lower.startswith("/data/")) and not (
        lower == "/data/local/tmp" or lower.startswith("/data/local/tmp/")
    ):
        raise AdapterError("invalid_remote_dir", "--remote-dir points into the protected /data filesystem")
    if "/android/data/" in lower + "/":
        raise AdapterError("invalid_remote_dir", "--remote-dir points into an application's external data directory")
    if not remote_dir.startswith("/") and remote_dir.startswith("-"):
        raise AdapterError("invalid_remote_dir", "--relative remote directories must not start with '-'")
    if not normalised.startswith("/"):
        if target == "android":
            normalised = _remote_join("/sdcard", normalised)
        else:
            raise AdapterError(
                "remote_dir_absolute_required",
                "HarmonyOS --remote-dir must be an absolute path verified on the selected tablet",
            )
    return normalised


def _read_pack_info(root: Path, layout: Sequence[str], target: str) -> Tuple[str, str]:
    pack_info = root / layout[0] / "pack_info.txt"
    if not pack_info.is_file():
        raise AdapterError("pack_info_missing", "resource pack is missing %s" % pack_info)
    try:
        text = pack_info.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AdapterError("pack_info_invalid", "could not read %s: %s" % (pack_info, exc)) from exc
    if len(text.encode("utf-8")) > 1024 * 1024:
        raise AdapterError("pack_info_invalid", "pack_info.txt is unexpectedly large")

    values: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key in values and values[key] != value:
            raise AdapterError("pack_info_invalid", "pack_info.txt repeats %s with different values" % key)
        values[key] = value

    product = values.get("PRODUCT", "")
    pack_target = values.get("TARGET", "")
    if not product or not pack_target:
        raise AdapterError("pack_info_invalid", "pack_info.txt must contain non-empty PRODUCT= and TARGET= values")
    if pack_target != target:
        raise AdapterError(
            "pack_target_mismatch",
            "resource pack TARGET=%s does not match --target %s; nothing was installed" % (pack_target, target),
        )
    return product, pack_target


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_resource_manifest(root: Path, layout: Sequence[str]) -> ResourceManifest:
    """Hash every expected file before any device-side transfer begins."""
    entries: List[ManifestEntry] = []
    directories: List[str] = []
    for component in layout:
        top = root / component
        if top.is_symlink():
            raise AdapterError("resource_symlink_rejected", "resource tree contains a symlink: %s" % top)
        directories.append(component)
        for current, dirs, files in os.walk(top, followlinks=False):
            current_path = Path(current)
            for name in list(dirs) + list(files):
                local = current_path / name
                if local.is_symlink():
                    raise AdapterError("resource_symlink_rejected", "resource tree contains a symlink: %s" % local)
                if "\\" in name or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
                    raise AdapterError("unsafe_resource_name", "resource path contains a backslash or control character: %s" % local)
            for name in sorted(dirs):
                directories.append((current_path / name).relative_to(root).as_posix())
            for name in sorted(files):
                local = current_path / name
                if not local.is_file():
                    raise AdapterError("resource_not_regular_file", "resource entry is not a regular file: %s" % local)
                before = local.stat()
                digest = _sha256_file(local)
                after = local.stat()
                if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                    raise AdapterError("resource_changed_during_hash", "resource changed while it was being hashed: %s" % local)
                rel = local.relative_to(root).as_posix()
                entries.append(ManifestEntry(rel, after.st_size, digest))

    entries.sort(key=lambda entry: entry.path)
    directories = sorted(set(directories))
    aggregate = hashlib.sha256()
    for entry in entries:
        aggregate.update(entry.path.encode("utf-8"))
        aggregate.update(b"\0%d\0" % entry.size)
        aggregate.update(entry.sha256.encode("ascii"))
        aggregate.update(b"\n")
    return ResourceManifest(tuple(entries), tuple(directories), sum(entry.size for entry in entries), aggregate.hexdigest())


def _prepare_install(argv: argparse.Namespace) -> InstallRequest:
    target = argv.target
    config = TARGETS[target]
    _validate_serial(argv.serial)
    package = argv.package or config["package"]
    _validate_package(package)

    artifact = Path(argv.artifact).expanduser().resolve()
    if not artifact.is_file():
        raise AdapterError("artifact_not_found", "--artifact does not name a file: %s" % artifact)
    if artifact.suffix.lower() != config["artifact"]:
        raise AdapterError(
            "artifact_type_mismatch",
            "%s requires a %s artifact, got %s" % (target, config["artifact"], artifact.name),
        )

    resources: Optional[Path] = None
    resource_manifest: Optional[ResourceManifest] = None
    pack_product: Optional[str] = None
    pack_target: Optional[str] = None
    if argv.resources:
        resources = Path(argv.resources).expanduser().resolve()
        if not resources.is_dir():
            raise AdapterError("resources_not_found", "--resources does not name a directory: %s" % resources)
        missing = [name for name in config["layout"] if not (resources / name).is_dir()]
        if missing:
            raise AdapterError(
                "incomplete_resources",
                "--resources must contain %s/ at its top level; missing %s"
                % (" and ".join(config["layout"]), ", ".join(missing)),
            )
        pack_product, pack_target = _read_pack_info(resources, config["layout"], target)
        resource_manifest = _build_resource_manifest(resources, config["layout"])

    remote_dir: Optional[str] = None
    if resources:
        requested_remote = argv.remote_dir or config["default_remote"]
        if not requested_remote:
            raise AdapterError(
                "remote_dir_required",
                "--remote-dir is required for HarmonyOS resources because its external folder varies by tablet",
            )
        remote_dir = _validate_remote_dir(str(requested_remote), target)
    elif argv.remote_dir:
        remote_dir = _validate_remote_dir(str(argv.remote_dir), target)

    return InstallRequest(
        target=target,
        bridge=argv.bridge,
        serial=argv.serial,
        artifact=artifact,
        package=package,
        resources=resources,
        resource_manifest=resource_manifest,
        pack_product=pack_product,
        pack_target=pack_target,
        remote_dir=remote_dir,
        launch=bool(argv.launch),
        dry_run=bool(argv.dry_run),
    )


def _adb_install_verified(returncode: int, output: str) -> bool:
    return returncode == 0 and any(line.strip() == "Success" for line in output.splitlines())


def _hdc_install_verified(returncode: int, output: str) -> bool:
    return returncode == 0 and "install bundle successfully" in output.lower()


def _hdc_launch_verified(returncode: int, output: str) -> bool:
    return returncode == 0 and "start ability successfully" in output.lower()


def _install_command(request: InstallRequest, bridge: str) -> List[str]:
    return _serial_prefix(request.target, bridge, request.serial) + ["install", "-r", str(request.artifact)]


def _remote_join(*parts: str) -> str:
    return posixpath.join(*parts)


def _new_token() -> str:
    return "%s-%s" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()), uuid.uuid4().hex[:8])


def _remote_exists(target: str, bridge: str, serial: str, path: str) -> Tuple[bool, ProcessResult]:
    quoted = shlex.quote(path)
    script = "if [ -e %s ]; then printf 'yes\\n'; else printf 'no\\n'; fi" % quoted
    result = _run(_device_shell_script(target, bridge, serial, script), DEVICE_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise AdapterError("remote_check_failed", "could not check remote path %s: %s" % (path, result.output.strip()))
    lines = [line.strip() for line in result.output.splitlines() if line.strip()]
    if not lines or lines[-1] not in {"yes", "no"}:
        raise AdapterError("remote_check_failed", "remote path check returned an unexpected result for %s" % path)
    return lines[-1] == "yes", result


def _remote_mkdir(target: str, bridge: str, serial: str, path: str) -> ProcessResult:
    quoted = shlex.quote(path)
    script = (
        "if mkdir -p %s && [ -d %s ]; then printf 'D2R_MKDIR_OK\\n'; "
        "else printf 'D2R_MKDIR_FAILED\\n'; fi" % (quoted, quoted)
    )
    result = _run(_device_shell_script(target, bridge, serial, script), TRANSFER_TIMEOUT_SECONDS)
    lines = [line.strip() for line in result.output.splitlines() if line.strip()]
    if result.returncode != 0 or not lines or lines[-1] != "D2R_MKDIR_OK":
        raise AdapterError("remote_mkdir_failed", "could not create and verify external staging directory %s: %s" % (path, result.output.strip()))
    return result


def _remote_mv(target: str, bridge: str, serial: str, source: str, destination: str) -> ProcessResult:
    quoted_source = shlex.quote(source)
    quoted_destination = shlex.quote(destination)
    script = (
        "if [ ! -e %s ] && mv %s %s && [ ! -e %s ] && [ -e %s ]; then "
        "printf 'D2R_MV_OK\\n'; else printf 'D2R_MV_FAILED\\n'; fi"
        % (quoted_destination, quoted_source, quoted_destination, quoted_source, quoted_destination)
    )
    result = _run(_device_shell_script(target, bridge, serial, script), TRANSFER_TIMEOUT_SECONDS)
    lines = [line.strip() for line in result.output.splitlines() if line.strip()]
    if result.returncode != 0 or not lines or lines[-1] != "D2R_MV_OK":
        raise AdapterError(
            "remote_rename_failed",
            "could not atomically rename and verify %s to %s: %s" % (source, destination, result.output.strip()),
        )
    return result


def _verified_remote_dir(target: str, bridge: str, serial: str, path: str) -> None:
    exists, result = _remote_exists(target, bridge, serial, path)
    if not exists:
        raise AdapterError("transfer_verification_failed", "remote transfer did not create %s" % path)


def _remote_sha256_tool(target: str, bridge: str, serial: str) -> str:
    script = (
        "if [ -x /bin/sha256sum ]; then printf '/bin/sha256sum\\n'; "
        "elif command -v sha256sum >/dev/null 2>&1; then command -v sha256sum; "
        "elif command -v shasum >/dev/null 2>&1; then printf 'shasum\\n'; "
        "else printf 'none\\n'; fi"
    )
    result = _run(_device_shell_script(target, bridge, serial, script), DEVICE_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise AdapterError("remote_hash_probe_failed", "could not probe remote SHA-256 support: %s" % result.output.strip())
    lines = [line.strip() for line in result.output.splitlines() if line.strip()]
    tool = lines[-1] if lines else "none"
    if tool not in {"/bin/sha256sum", "sha256sum", "shasum"}:
        raise AdapterError("remote_sha256_unavailable", "remote shell has neither sha256sum nor shasum; refusing to publish unverified files")
    return tool


def _remote_tar_path(target: str, bridge: str, serial: str) -> Optional[str]:
    script = (
        "if [ -x /bin/tar ]; then printf '/bin/tar\\n'; "
        "elif command -v tar >/dev/null 2>&1; then command -v tar; "
        "else printf 'none\\n'; fi"
    )
    result = _run(_device_shell_script(target, bridge, serial, script), DEVICE_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise AdapterError("remote_tar_probe_failed", "could not probe remote tar support: %s" % result.output.strip())
    lines = [line.strip() for line in result.output.splitlines() if line.strip()]
    path = lines[-1] if lines else "none"
    return None if path == "none" else path


def _hash_batches(files: Sequence[ManifestEntry]) -> List[List[ManifestEntry]]:
    batches: List[List[ManifestEntry]] = []
    batch: List[ManifestEntry] = []
    batch_bytes = 0
    for entry in files:
        path_bytes = len(entry.path.encode("utf-8")) + 3
        if batch and (len(batch) >= HASH_BATCH_MAX_FILES or batch_bytes + path_bytes > HASH_BATCH_MAX_BYTES):
            batches.append(batch)
            batch = []
            batch_bytes = 0
        batch.append(entry)
        batch_bytes += path_bytes
    if batch:
        batches.append(batch)
    return batches


def _verify_remote_manifest(
    request: InstallRequest,
    bridge: str,
    remote_root: str,
) -> Dict[str, Any]:
    manifest = request.resource_manifest
    assert manifest is not None
    tool = _remote_sha256_tool(request.target, bridge, request.serial)
    batches = _hash_batches(manifest.files)

    checked = 0
    for batch_number, batch in enumerate(batches, start=1):
        remote_paths = [_remote_join(remote_root, entry.path) for entry in batch]
        command = [tool]
        if tool == "shasum":
            command.extend(["-a", "256"])
        command.extend(remote_paths)
        result = _run(_device_shell(request.target, bridge, request.serial, command), TRANSFER_TIMEOUT_SECONDS)
        if result.returncode != 0:
            raise AdapterError(
                "transfer_verification_failed",
                "remote SHA-256 batch %d failed: %s" % (batch_number, result.output.strip()),
            )
        output_lines = [line.strip() for line in result.output.splitlines() if line.strip()]
        if len(output_lines) != len(batch):
            raise AdapterError(
                "transfer_verification_failed",
                "remote SHA-256 batch %d returned %d entries, expected %d (missing or unreadable file)"
                % (batch_number, len(output_lines), len(batch)),
            )
        for entry, line in zip(batch, output_lines):
            remote_digest = line.split(None, 1)[0].lower() if line else ""
            if not re.fullmatch(r"[0-9a-f]{64}", remote_digest) or remote_digest != entry.sha256:
                raise AdapterError(
                    "transfer_verification_failed",
                    "remote file hash mismatch for %s; existing import was not replaced" % entry.path,
                )
            checked += 1
    return {"verified_files": checked, "batches": len(batches), "sha256_tool": tool}


def _copy_component(request: InstallRequest, bridge: str, component: str, remote_component: str) -> Dict[str, Any]:
    assert request.resources is not None
    local_component = request.resources / component
    _remote_mkdir(request.target, bridge, request.serial, remote_component)

    if request.target == "android":
        destination = remote_component + "/"
        result = _run(
            _serial_prefix(request.target, bridge, request.serial)
            + ["push", str(local_component) + os.sep + ".", destination],
            TRANSFER_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise AdapterError("transfer_failed", "adb push %s failed: %s" % (component, result.output.strip()))
        method = "adb_push_directory_pending_hash_verification"
    else:
        # This fallback deliberately sends the whole directory once. The SHA-256
        # manifest below decides whether the bridge version copied it correctly.
        result = _run(
            _serial_prefix(request.target, bridge, request.serial)
            + ["file", "send", str(local_component), remote_component],
            TRANSFER_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            raise AdapterError("transfer_failed", "hdc file send %s failed: %s" % (component, result.output.strip()))
        method = "hdc_explicit_directory_send_pending_hash_verification"

    return {"component": component, "method": method}


def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def _create_resource_archive(resources: Path, manifest: ResourceManifest, archive: Path) -> None:
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as bundle:
        for rel_dir in manifest.directories:
            bundle.add(resources / rel_dir, arcname=rel_dir, recursive=False, filter=_tar_filter)
        for entry in manifest.files:
            bundle.add(resources / entry.path, arcname=entry.path, recursive=False, filter=_tar_filter)


def _cleanup_remote_archive(target: str, bridge: str, serial: str, archive: str) -> ProcessResult:
    # This removes only the tool-created, unique transfer archive; it never targets
    # the selected import directory or a retained backup.
    quoted = shlex.quote(archive)
    script = (
        "if rm -f %s && [ ! -e %s ]; then printf 'D2R_RM_OK\\n'; "
        "else printf 'D2R_RM_FAILED\\n'; fi" % (quoted, quoted)
    )
    result = _run(_device_shell_script(target, bridge, serial, script), TRANSFER_TIMEOUT_SECONDS)
    lines = [line.strip() for line in result.output.splitlines() if line.strip()]
    verified = result.returncode == 0 and bool(lines) and lines[-1] == "D2R_RM_OK"
    return ProcessResult(result.argv, 0 if verified else 1, result.output)


def _transfer_hdc_archive(
    request: InstallRequest,
    bridge: str,
    remote_stage: str,
    token: str,
    report: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    assert request.resources is not None
    assert request.resource_manifest is not None
    remote_tar = _remote_tar_path(request.target, bridge, request.serial)
    if not remote_tar:
        return None

    remote_archive = "/data/local/tmp/.d2r-mobile-transfer-%s.tar" % token
    report["steps"].append(_step("create_private_tar_workspace", archive_location="system temporary directory"))
    with tempfile.TemporaryDirectory(prefix="d2r-mobile-transfer-") as workspace_name:
        workspace = Path(workspace_name).resolve()
        if workspace == request.resources or request.resources in workspace.parents:
            raise AdapterError("unsafe_temp_workspace", "temporary archive workspace must be outside the resource source tree")
        if workspace == SOURCE_ROOT or SOURCE_ROOT in workspace.parents:
            raise AdapterError("unsafe_temp_workspace", "temporary archive workspace must be outside the source tree")
        archive = workspace / "D2R Mobile import.tar"
        _create_resource_archive(request.resources, request.resource_manifest, archive)
        report["steps"][-1].update({"status": "completed", "archive_bytes": archive.stat().st_size})

        report["steps"].append(_step("send_and_extract_tar", remote_stage=remote_stage))
        send = _run(
            _serial_prefix(request.target, bridge, request.serial) + ["file", "send", str(archive), remote_archive],
            TRANSFER_TIMEOUT_SECONDS,
        )
        if send.returncode != 0:
            _cleanup_remote_archive(request.target, bridge, request.serial, remote_archive)
            raise AdapterError("transfer_failed", "hdc tar archive send failed: %s" % send.output.strip())
        extract = _run(
            _device_shell(request.target, bridge, request.serial, [remote_tar, "-xf", remote_archive, "-C", remote_stage]),
            TRANSFER_TIMEOUT_SECONDS,
        )
        if extract.returncode != 0:
            _cleanup_remote_archive(request.target, bridge, request.serial, remote_archive)
            raise AdapterError("transfer_failed", "remote tar extraction failed: %s" % extract.output.strip())
        report["steps"][-1]["status"] = "completed"

        try:
            verification = _verify_remote_manifest(request, bridge, remote_stage)
        except Exception:
            _cleanup_remote_archive(request.target, bridge, request.serial, remote_archive)
            raise

    cleanup = _cleanup_remote_archive(request.target, bridge, request.serial, remote_archive)
    if cleanup.returncode != 0:
        raise AdapterError(
            "archive_cleanup_failed",
            "verified staged files but could not remove the unique transfer archive: %s" % cleanup.output.strip(),
        )
    return {
        "method": "hdc_tar_archive",
        "archive_remote_name": posixpath.basename(remote_archive),
        "device_tar": remote_tar,
        "archive_scratch": "/data/local/tmp",
        "archive_cleaned": True,
        "stage_token": token,
        "verification": verification,
    }


def _remote_stage_path(remote_dir: str, token: str) -> str:
    parent, name = posixpath.split(remote_dir)
    return _remote_join(parent, "." + name + ".d2r-import-stage-" + token)


def _remote_backup_path(remote_dir: str, token: str) -> str:
    return remote_dir + ".d2r-backup-" + token


def _publish_staged_directory(
    request: InstallRequest,
    bridge: str,
    remote_stage: str,
    token: str,
) -> Dict[str, Any]:
    assert request.remote_dir is not None
    final_path = request.remote_dir
    backup_path = _remote_backup_path(final_path, token)
    backup: Optional[str] = None
    moved_final = False
    try:
        existed, _ = _remote_exists(request.target, bridge, request.serial, final_path)
        if existed:
            backup_exists, _ = _remote_exists(request.target, bridge, request.serial, backup_path)
            if backup_exists:
                raise AdapterError("backup_collision", "refusing to overwrite existing import backup %s" % backup_path)
            _remote_mv(request.target, bridge, request.serial, final_path, backup_path)
            backup = backup_path
            moved_final = True
        _remote_mv(request.target, bridge, request.serial, remote_stage, final_path)
    except Exception:
        rollback_errors: List[str] = []
        try:
            final_exists, _ = _remote_exists(request.target, bridge, request.serial, final_path)
        except AdapterError as exc:
            final_exists = False
            rollback_errors.append(exc.message)
        if final_exists and moved_final:
            try:
                _remote_mv(request.target, bridge, request.serial, final_path, remote_stage)
            except AdapterError as exc:
                rollback_errors.append(exc.message)
        if backup:
            try:
                backup_exists, _ = _remote_exists(request.target, bridge, request.serial, backup)
            except AdapterError as exc:
                backup_exists = False
                rollback_errors.append(exc.message)
            if backup_exists:
                try:
                    _remote_mv(request.target, bridge, request.serial, backup, final_path)
                except AdapterError as exc:
                    rollback_errors.append(exc.message)
        if rollback_errors:
            raise AdapterError("transfer_rollback_failed", "; ".join(rollback_errors))
        raise
    return {"published": final_path, "backup": backup}


def _transfer_resources(
    request: InstallRequest,
    bridge: str,
    report: Dict[str, Any],
) -> Dict[str, Any]:
    assert request.resources is not None
    assert request.remote_dir is not None
    token = _new_token()
    remote_stage = _remote_stage_path(request.remote_dir, token)

    report["steps"].append(_step("create_external_import_stage", remote_dir=remote_stage))
    _remote_mkdir(request.target, bridge, request.serial, remote_stage)

    copied: List[Dict[str, Any]] = []
    transfer_info: Optional[Dict[str, Any]] = None
    if request.target == "ohos":
        transfer_info = _transfer_hdc_archive(request, bridge, remote_stage, token, report)

    if transfer_info is None:
        for component in TARGETS[request.target]["layout"]:
            step = _step("copy_%s_to_stage" % component, component=component)
            report["steps"].append(step)
            info = _copy_component(request, bridge, component, _remote_join(remote_stage, component))
            step.update(info)
            step["status"] = "completed"
            copied.append(info)
        verification = _verify_remote_manifest(request, bridge, remote_stage)
    else:
        verification = transfer_info["verification"]

    for component in TARGETS[request.target]["layout"]:
        _verified_remote_dir(request.target, bridge, request.serial, _remote_join(remote_stage, component))
    if request.target == "ohos" and transfer_info is None:
        transfer_info = {
            "method": "hdc_explicit_directory_send_verify",
            "archive_cleaned": True,
            "verification": verification,
        }

    report["steps"].append(_step("verify_remote_manifest", status="completed", **verification))
    report["steps"].append(_step("publish_verified_import_directory", backups_retained=True))
    published = _publish_staged_directory(request, bridge, remote_stage, token)
    report["steps"][-1].update({"status": "completed", "published": published})
    assert request.resource_manifest is not None
    return {
        "remote_dir": request.remote_dir,
        "stage": remote_stage,
        "components": copied,
        "transfer": transfer_info,
        "manifest": request.resource_manifest.summary(),
        "verification": verification,
        "published": published,
        "old_import_backups_retained": True,
        "import_state": "awaiting_manual_app_import",
        "note": "Files are staged outside app data. Open the app and use its system folder picker; import and gameplay are not verified by this adapter.",
    }


def _plan_transfer(request: InstallRequest) -> Dict[str, Any]:
    assert request.resources is not None
    assert request.remote_dir is not None
    token = "<unique>"
    stage = _remote_stage_path(request.remote_dir, token)
    components = []
    for component in TARGETS[request.target]["layout"]:
        components.append(
            {
                "component": component,
                "local": str(request.resources / component),
                "remote_stage": _remote_join(stage, component),
                "target": _remote_join(request.remote_dir, component),
            }
        )
    return {
        "remote_dir": request.remote_dir,
        "stage": stage,
        "components": components,
        "backup_if_present": _remote_backup_path(request.remote_dir, token),
        "manifest": request.resource_manifest.summary() if request.resource_manifest else None,
        "steps": [
            "create a unique staging directory beside the selected external import directory",
            "HARMONY: create a Python tar archive and send it once; extract on devices with tar"
            if request.target == "ohos"
            else "push %s into the stage" % " and ".join(TARGETS[request.target]["layout"]),
            "SHA-256 verify every expected file in bounded remote batches",
            "fallback to explicit top-level directory sends if Harmony tar is unavailable",
            "rename the old import directory to a retained timestamped backup",
            "rename the complete verified stage into the selected import path",
        ],
        "import_state": "awaiting_manual_app_import",
    }


def _launch_command(request: InstallRequest, bridge: str) -> List[str]:
    if request.target == "android":
        command = ["monkey", "-p", request.package, "-c", "android.intent.category.LAUNCHER", "1"]
    else:
        command = ["aa", "start", "-b", request.package, "-a", "EntryAbility"]
    return _device_shell(request.target, bridge, request.serial, command)


def _resource_preflight(request: InstallRequest) -> Optional[Dict[str, Any]]:
    if request.resources is None or request.resource_manifest is None:
        return None
    return {
        "layout": list(TARGETS[request.target]["layout"]),
        "pack_product": request.pack_product,
        "pack_target": request.pack_target,
        "manifest": request.resource_manifest.summary(),
    }


def _dry_run_report(request: InstallRequest) -> Dict[str, Any]:
    target_config = TARGETS[request.target]
    report = _report("install", request.target)
    report.update(
        {
            "status": "dry_run",
            "serial": request.serial,
            "bridge": request.bridge or "<discover %s>" % target_config["bridge"],
            "package": request.package,
            "artifact": str(request.artifact),
            "launch_requested": request.launch,
            "launch_sent": False,
            "launch_verified": False,
            "launch_state": "planned" if request.launch else "not_requested",
            "resource_preflight": _resource_preflight(request),
        }
    )
    report["steps"].append(
        _step(
            "install_artifact",
            planned_argv=[
                request.bridge or "<%s>" % target_config["bridge"],
                "-s" if request.target == "android" else "-t",
                request.serial,
                "install",
                "-r",
                str(request.artifact),
            ],
            acknowledgement=("Success" if request.target == "android" else "install bundle successfully"),
        )
    )
    if request.resources:
        report["steps"].append(_step("transfer_resources", plan=_plan_transfer(request)))
    else:
        report["steps"].append(_step("transfer_resources", status="skipped", reason="--resources not supplied"))
    if request.launch:
        report["steps"].append(_step("launch", package=request.package))
    else:
        report["steps"].append(_step("launch", status="skipped", reason="--launch not supplied"))
    return report


def install_device(request: InstallRequest) -> Tuple[Dict[str, Any], int]:
    if request.dry_run:
        return _dry_run_report(request), 0

    report = _report("install", request.target)
    report.update(
        {
            "serial": request.serial,
            "package": request.package,
            "artifact": str(request.artifact),
            "launch_requested": request.launch,
            "launch_sent": False,
            "launch_verified": False,
            "launch_state": "not_requested",
            "resource_preflight": _resource_preflight(request),
        }
    )
    try:
        bridge = _resolve_bridge(request.target, request.bridge)
    except AdapterError as exc:
        return _fail(report, exc.code, exc.message), 1
    report["bridge"] = bridge

    report["steps"].append(_step("inventory_target_serial"))
    devices, inventory_result = _read_devices(request.target, bridge)
    connection_state, issues = classify_devices(devices)
    report["device_inventory"] = {
        "status": connection_state,
        "issues": issues,
        "devices": [device.as_dict() for device in devices],
    }
    inventory_step = report["steps"][-1]
    inventory_step.update(report["device_inventory"])
    if inventory_result.returncode != 0:
        inventory_step["status"] = "failed"
        return _fail(report, "device_inventory_failed", inventory_result.output.strip() or "device inventory command failed"), 1

    matches = [device for device in devices if device.serial == request.serial]
    if not matches:
        inventory_step["status"] = "failed"
        return _fail(report, "serial_not_found", "the explicit serial is not connected: %s" % request.serial), 1
    target_device = matches[0]
    if target_device.state != "device":
        inventory_step["status"] = "failed"
        return _fail(
            report,
            "target_%s" % target_device.state,
            "the explicit serial is %s; nothing was installed" % target_device.state,
        ), 1
    inventory_step["status"] = "completed"

    install_step = _step("install_artifact", argv=None)
    report["steps"].append(install_step)
    install_command = _install_command(request, bridge)
    install_step["argv"] = install_command
    result = _run(install_command, INSTALL_TIMEOUT_SECONDS)
    install_step["returncode"] = result.returncode
    install_step["output_tail"] = "\n".join(result.output.strip().splitlines()[-12:])
    verified = (
        _adb_install_verified(result.returncode, result.output)
        if request.target == "android"
        else _hdc_install_verified(result.returncode, result.output)
    )
    install_step["verified"] = verified
    if not verified:
        install_step["status"] = "failed"
        wanted = "a literal Success line" if request.target == "android" else "'install bundle successfully'"
        return _fail(report, "install_not_verified", "install did not produce %s; transfer was not started" % wanted), 1
    install_step["status"] = "completed"
    report["installed"] = True

    transfer_info: Optional[Dict[str, Any]] = None
    if request.resources:
        try:
            transfer_info = _transfer_resources(request, bridge, report)
        except AdapterError as exc:
            return _fail(report, exc.code, exc.message), 1
        report["transfer"] = transfer_info

    if request.launch:
        launch_step = _step("launch")
        report["steps"].append(launch_step)
        report["launch_sent"] = True
        launch_result = _run(_launch_command(request, bridge), LAUNCH_TIMEOUT_SECONDS)
        launch_step.update({"returncode": launch_result.returncode, "output_tail": launch_result.output.strip()[-2000:]})
        if launch_result.returncode != 0:
            launch_step["status"] = "failed"
            report["launch_state"] = "send_failed"
            return _fail(report, "launch_failed", launch_result.output.strip() or "launch command failed"), 1
        launch_verified = (
            launch_result.returncode == 0
            if request.target == "android"
            else _hdc_launch_verified(launch_result.returncode, launch_result.output)
        )
        launch_step["verified"] = launch_verified
        if not launch_verified:
            launch_step["status"] = "sent_unverified"
            report["launch_state"] = "sent_unverified"
            return _fail(
                report,
                "launch_not_verified",
                "HDC launch returned success without 'start ability successfully'; the command was sent but launch is unverified",
            ), 1
        launch_step["status"] = "completed"
        report["launch_verified"] = True
        report["launch_state"] = "verified_success"
    else:
        report["steps"].append(_step("launch", status="skipped", reason="--launch not supplied"))

    report["status"] = "awaiting_import" if transfer_info else "ok"
    return report, 0


def devices_report(target: str, bridge_arg: Optional[str]) -> Tuple[Dict[str, Any], int]:
    report = _report("devices", target)
    try:
        bridge = _resolve_bridge(target, bridge_arg)
    except AdapterError as exc:
        return _fail(report, exc.code, exc.message), 1
    report["bridge"] = bridge
    devices, result = _read_devices(target, bridge)
    if result.returncode != 0:
        return _fail(report, "device_inventory_failed", result.output.strip() or "device inventory command failed"), 1
    state, issues = classify_devices(devices)
    report.update({"status": state, "issues": issues, "devices": [device.as_dict() for device in devices]})
    return report, 0 if state == "device" and not issues else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install D2R Mobile or stage an external import pack on one explicitly selected device.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    devices = subparsers.add_parser("devices", help="list connected devices without selecting one")
    devices.add_argument("--target", choices=sorted(TARGETS), required=True)
    devices.add_argument("--bridge", metavar="PATH", help="adb or hdc executable; otherwise discover the target bridge on PATH")
    devices.add_argument("--json", action="store_true", help="emit one JSON object")

    install = subparsers.add_parser("install", help="install -r on one explicit serial, then stage resources")
    install.add_argument("--target", choices=sorted(TARGETS), required=True)
    install.add_argument("--serial", required=True, help="exact connected device serial; never inferred")
    install.add_argument("--artifact", required=True, metavar="APK_OR_HAP")
    install.add_argument("--package", help="application package / bundle name (used only by --launch)")
    install.add_argument("--resources", metavar="PATH", help="external import staging directory containing target layout directories")
    install.add_argument("--remote-dir", help="external import directory on the device")
    install.add_argument("--launch", action="store_true", default=False, help="open the app after install and transfer")
    install.add_argument("--bridge", metavar="PATH", help="adb or hdc executable; otherwise discover the target bridge on PATH")
    install.add_argument("--dry-run", action="store_true", help="validate inputs and print the plan without device calls or writes")
    install.add_argument("--json", action="store_true", help="emit one JSON object")
    return parser


def _print_report(report: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return
    print("%s: %s" % (report["command"], report["status"]))
    if report.get("error"):
        print("error[%s]: %s" % (report["error"]["code"], report["error"]["message"]))
    for device in report.get("devices", []):
        print("  %s\t%s%s" % (device["serial"], device["state"], ("\t" + device["detail"]) if device["detail"] else ""))
    for step in report.get("steps", []):
        print("  [%s] %s" % (step.get("status", "pending"), step["name"]))
        if step.get("reason"):
            print("    %s" % step["reason"])
        if step.get("note"):
            print("    %s" % step["note"])
    if report.get("transfer"):
        print("  import_state: %s" % report["transfer"]["import_state"])


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "devices":
        report, status = devices_report(args.target, args.bridge)
    else:
        try:
            request = _prepare_install(args)
        except AdapterError as exc:
            report = _fail(_report("install", args.target), exc.code, exc.message)
            status = 1
        else:
            report, status = install_device(request)
    _print_report(report, bool(args.json))
    return status


if __name__ == "__main__":
    sys.exit(main())
