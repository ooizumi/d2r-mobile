#!/usr/bin/env python3
"""Top-level D2R mobile build, device, and source-export orchestrator.

This program validates configuration and invokes the worker CLIs without a
shell. Game files, signing profiles, and private keys remain on the recipient's
machine. Device installation never chooses a serial automatically.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


HERE = Path(__file__).resolve().parent
SOURCE_ROOT = HERE.parents[1]
WORKERS = {
    "doctor": HERE / "mobile_install.py",
    "prepare": HERE / "mobile_resources.py",
    "build": HERE / "mobile_build.py",
    "device": HERE / "mobile_device.py",
    "ohos_import": HERE / "mobile_ohos_import.py",
}
SOURCE_MANIFEST = "SOURCE_EXPORT_MANIFEST.txt"
DM_SOURCE_MANIFEST = "dm/SOURCE_EXPORT_MANIFEST.txt"
SOURCE_MANIFEST_ALIASES = ("dm/SOURCE_EXPORT_MANIFEST.txt", "dm/source_export_manifest.txt", SOURCE_MANIFEST, "source_manifest.txt")


class OrchestratorError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def resolve_path(value: str | os.PathLike[str] | None, base: Path | None = None) -> Path | None:
    if value is None or str(value).strip() == "":
        return None
    path = Path(os.path.expanduser(str(value)))
    if not path.is_absolute() and base is not None:
        path = base / path
    return Path(os.path.abspath(path))


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def emit(value: Mapping[str, Any], as_json: bool) -> None:
    if as_json:
        json.dump(dict(value), sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return
    print(json.dumps(dict(value), ensure_ascii=False, indent=2, sort_keys=True))


def phase(name: str, status: str, **detail: Any) -> dict[str, Any]:
    row = {"name": name, "status": status}
    row.update(detail)
    return row


def base_report(command: str, *, target: str | None, dry_run: bool, config: Path | None) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "command": command,
        "target": target,
        "dry_run": dry_run,
        "status": "pending",
        "config": str(config) if config else None,
        "phases": [],
        "installed": False,
        "error": None,
    }


def fail(report: dict[str, Any], code: str, message: str, exit_code: int = 2) -> tuple[dict[str, Any], int]:
    report["status"] = "failed"
    report["error"] = {"code": code, "message": message}
    return report, exit_code


def command_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="D2R mobile build, device, and source-export orchestrator", allow_abbrev=False)
    parser.add_argument("command", choices=("doctor", "prepare", "build", "devices", "install", "import-resources", "run", "export-source"))
    parser.add_argument("--config", type=Path, help="JSON configuration; required except for doctor/export defaults")
    parser.add_argument("--json", action="store_true", help="emit one structured JSON object")
    parser.add_argument("--dry-run", action="store_true", help="plan only; no writes, worker calls, or device access")
    return parser


def redacted(value: Sequence[str | os.PathLike[str]]) -> list[str]:
    result: list[str] = []
    secret_next = False
    for item in value:
        text = os.fspath(item)
        lower = text.lower()
        if secret_next:
            result.append("<private>")
            secret_next = False
            continue
        if lower in ("--keys", "--build-profile", "--cloud-config", "--debug-keystore"):
            result.append(text)
            secret_next = True
            continue
        if any(word in lower for word in ("password=", "passphrase=", "token=")):
            result.append("<private>")
            continue
        result.append(text)
    return result


def load_config(path: Path | None, *, required: bool) -> tuple[dict[str, Any], Path | None]:
    if path is None:
        if required:
            raise OrchestratorError("config_required", "--config is required for this command")
        return {}, None
    path = resolve_path(path)
    if not path or not path.is_file():
        raise OrchestratorError("config_not_found", "--config does not name a JSON file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise OrchestratorError("config_invalid", f"cannot parse --config JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise OrchestratorError("config_invalid", "--config JSON must contain one object")
    return value, path


def object_field(config: Mapping[str, Any], key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    value = config.get(key, default or {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise OrchestratorError("config_invalid", f"configuration field {key!r} must be an object")
    return dict(value)


def list_field(value: Any, name: str) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise OrchestratorError("config_invalid", f"{name} must be a path string or an array of path strings")
    return list(value)


def normalize_config(config: Mapping[str, Any], config_path: Path | None) -> dict[str, Any]:
    base = config_path.parent if config_path else Path.cwd()
    target = config.get("target")
    if target not in ("android", "ohos"):
        raise OrchestratorError("target_required", "configuration target must be android or ohos")
    workspace = resolve_path(config.get("workspace"), base)
    if not workspace:
        raise OrchestratorError("workspace_required", "configuration workspace is required")
    if workspace == SOURCE_ROOT or is_relative_to(workspace, SOURCE_ROOT):
        raise OrchestratorError("unsafe_workspace", "workspace must be outside the source checkout")

    resource = object_field(config, "resource")
    if not resource and isinstance(config.get("resources"), dict):
        resource = dict(config["resources"])
    build = object_field(config, "build")
    device = object_field(config, "device")
    install = object_field(config, "install")
    export = object_field(config, "export")
    import_mode = str(device.get("import_mode") or "external")
    if import_mode not in ("external", "debug-sandbox") or (target != "ohos" and import_mode != "external"):
        raise OrchestratorError("import_mode_invalid", "debug-sandbox import is only supported for HarmonyOS")

    game_dir = resolve_path(config.get("game_dir"), base)
    roms = [resolve_path(item, base) for item in list_field(config.get("roms"), "roms")]
    roms = [path for path in roms if path is not None]
    keys = resolve_path(config.get("keys"), base)
    if game_dir and (roms or keys):
        raise OrchestratorError("source_conflict", "configuration must use game_dir or roms/keys, not both")

    paths = {
        "hactool": resolve_path(resource.get("hactool"), base),
        "warm_seed": resolve_path(resource.get("warm_seed"), base),
        "astc_pack": resolve_path(resource.get("astc_pack"), base),
        "sdk": resolve_path(build.get("sdk"), base),
        "deveco": resolve_path(build.get("deveco"), base),
        "gradle": resolve_path(build.get("gradle"), base),
        "java_home": resolve_path(build.get("java_home"), base),
        "python": resolve_path(build.get("python"), base),
        "gen_dir": resolve_path(build.get("gen_dir"), base),
        "build_profile": resolve_path(build.get("build_profile"), base),
        "cloud_config": resolve_path(build.get("cloud_config"), base),
        "debug_keystore": resolve_path(build.get("debug_keystore"), base),
        "bridge": resolve_path(device.get("bridge"), base),
        "artifact": resolve_path(install.get("artifact"), base),
        "resources": resolve_path(install.get("resources"), base),
        "export_output": resolve_path(export.get("output"), base),
    }
    return {
        "target": target,
        "workspace": workspace,
        "game_dir": game_dir,
        "roms": roms,
        "keys": keys,
        "resource": {key: paths[key] for key in ("hactool", "warm_seed", "astc_pack")},
        "build": {
            **{key: paths[key] for key in ("sdk", "deveco", "gradle", "java_home", "python", "gen_dir", "build_profile", "cloud_config", "debug_keystore")},
            "app_id": str(build.get("app_id") or ""),
            "label": str(build.get("label") or "暗黑2"),
            "sdk_version": str(build.get("sdk_version") or ""),
            "build_mode": str(build.get("build_mode") or "debug"),
        },
        "device": {
            "bridge": paths["bridge"],
            "serial": str(device.get("serial") or "").strip(),
            "remote_dir": str(device.get("remote_dir") or "").strip(),
            "launch": bool(device.get("launch", False)),
            "import_mode": import_mode,
        },
        "install": {"artifact": paths["artifact"], "resources": paths["resources"]},
        "export": {"output": paths["export_output"]},
    }


def worker_exists(name: str) -> bool:
    return WORKERS[name].is_file()


def invoke_worker(name: str, argv: Sequence[str | os.PathLike[str]]) -> tuple[dict[str, Any] | None, int, list[str]]:
    worker = WORKERS[name]
    if not worker_exists(name):
        raise OrchestratorError("worker_missing", f"worker module is missing: {worker.name}")
    command = [sys.executable, str(worker), *map(os.fspath, argv)]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            check=False,
        )
    except OSError as exc:
        raise OrchestratorError("worker_start_failed", f"{worker.name} could not start: {exc}") from exc
    try:
        value = json.loads(result.stdout)
    except ValueError as exc:
        code = "worker_invalid_json"
        raise OrchestratorError(code, f"{worker.name} did not return one JSON object") from exc
    if not isinstance(value, dict):
        raise OrchestratorError("worker_invalid_json", f"{worker.name} JSON result must be an object")
    return value, int(result.returncode), redacted(command)


def worker_error(value: Mapping[str, Any], returncode: int) -> dict[str, str] | None:
    raw = value.get("error")
    if isinstance(raw, dict):
        return {
            "code": str(raw.get("code") or "worker_error"),
            "message": str(raw.get("message") or "worker failed"),
        }
    status = str(value.get("status") or "")
    if returncode or status == "failed" or value.get("ok") is False:
        return {"code": "worker_failed", "message": str(value.get("message") or f"worker exited {returncode}")}
    return None


def worker_success(value: Mapping[str, Any], returncode: int) -> bool:
    return worker_error(value, returncode) is None


def install_confirmed(value: Mapping[str, Any]) -> bool:
    if value.get("installed") is True:
        return True
    for step in value.get("steps", []):
        if not isinstance(step, dict) or step.get("name") != "install_artifact":
            continue
        if step.get("status") == "completed" and step.get("verified") is True:
            return True
    return False


def add_optional_paths(argv: list[str], rows: Iterable[tuple[str, Path | None]]) -> None:
    for flag, value in rows:
        if value is not None:
            argv.extend((flag, str(value)))


def prepare_argv(config: Mapping[str, Any], *, dry_run: bool) -> list[str]:
    if not config.get("game_dir") and not config.get("roms"):
        raise OrchestratorError("game_source_required", "prepare requires game_dir or roms[] with keys")
    if config.get("roms") and not config.get("keys"):
        raise OrchestratorError("keys_required", "roms[] requires keys")
    argv = ["--target", config["target"], "--workspace", str(config["workspace"])]
    if config.get("game_dir"):
        argv.extend(("--game-dir", str(config["game_dir"])))
    else:
        for rom in config["roms"]:
            argv.extend(("--rom", str(rom)))
        argv.extend(("--keys", str(config["keys"])))
    add_optional_paths(
        argv,
        (
            ("--hactool", config["resource"].get("hactool")),
            ("--warm-seed", config["resource"].get("warm_seed")),
            ("--astc-pack", config["resource"].get("astc_pack")),
        ),
    )
    argv.append("--json")
    if dry_run:
        argv.append("--dry-run")
    return argv


def build_argv(config: Mapping[str, Any], *, game_dir: Path | None, dry_run: bool) -> list[str]:
    build = config["build"]
    if not game_dir and not build.get("gen_dir"):
        raise OrchestratorError("build_game_required", "build requires prepare.game_dir, game_dir, or build.gen_dir")
    argv = ["--target", config["target"], "--workspace", str(config["workspace"])]
    if game_dir:
        argv.extend(("--game-dir", str(game_dir)))
    add_optional_paths(
        argv,
        (
            ("--python", build.get("python")),
            ("--sdk", build.get("sdk")),
            ("--deveco", build.get("deveco")),
            ("--gradle", build.get("gradle")),
            ("--java-home", build.get("java_home")),
            ("--gen-dir", build.get("gen_dir")),
            ("--build-profile", build.get("build_profile")),
            ("--cloud-config", build.get("cloud_config")),
            ("--debug-keystore", build.get("debug_keystore")),
        ),
    )
    if build.get("app_id"):
        argv.extend(("--app-id", build["app_id"]))
    if build.get("label"):
        argv.extend(("--label", build["label"]))
    if build.get("sdk_version"):
        argv.extend(("--sdk-version", build["sdk_version"]))
    if build.get("build_mode"):
        argv.extend(("--build-mode", build["build_mode"]))
    argv.append("--json")
    if dry_run:
        argv.append("--dry-run")
    return argv


def install_argv(
    config: Mapping[str, Any],
    *,
    artifact: Path,
    resources: Path,
    serial: str | None = None,
    dry_run: bool,
) -> list[str]:
    device = config["device"]
    serial = (serial or device.get("serial") or "").strip()
    if not serial:
        raise OrchestratorError("serial_required", "install requires an explicit device serial")
    sandbox = config["target"] == "ohos" and device.get("import_mode") == "debug-sandbox"
    argv = ["install", "--target", config["target"], "--serial", serial, "--artifact", str(artifact), "--json"]
    if not sandbox:
        argv.extend(("--resources", str(resources)))
    if config["build"].get("app_id"):
        argv.extend(("--package", config["build"]["app_id"]))
    add_optional_paths(argv, (("--bridge", discover_bridge(config)), ("--remote-dir", device.get("remote_dir") or None)))
    if config["target"] == "ohos" and not sandbox and not device.get("remote_dir"):
        raise OrchestratorError("remote_dir_required", "HarmonyOS resource installation requires device.remote_dir")
    if device.get("launch"):
        argv.append("--launch")
    if dry_run:
        argv.append("--dry-run")
    return argv


def artifact_signature(path: Path, target: str) -> bool | None:
    name = path.name.lower()
    if "unsigned" in name:
        return False
    if "signed" in name:
        return True
    if target == "ohos":
        return False
    return None


def print_planned(report: dict[str, Any], name: str, argv: Sequence[str]) -> None:
    report["phases"].append(phase(name, "planned", argv=redacted(argv)))


def run_prepare(report: dict[str, Any], config: Mapping[str, Any], dry_run: bool) -> tuple[dict[str, Any] | None, bool]:
    argv = prepare_argv(config, dry_run=dry_run)
    if dry_run:
        print_planned(report, "prepare", argv)
        return None, True
    value, returncode, command = invoke_worker("prepare", [item for item in argv if item != "--dry-run"])
    assert value is not None
    error = worker_error(value, returncode)
    if error:
        report["phases"].append(phase("prepare", "failed", argv=command, error=error))
        return value, False
    report["phases"].append(phase("prepare", str(value.get("status") or "completed"), argv=command, result=value))
    game_dir = value.get("game_dir")
    if not game_dir:
        report["phases"][-1]["status"] = "failed"
        report["phases"][-1]["error"] = {
            "code": "prepare_game_dir_missing",
            "message": "mobile_resources result did not expose game_dir for the build handoff",
        }
        return value, False
    return value, True


def run_build(report: dict[str, Any], config: Mapping[str, Any], *, game_dir: Path | None, dry_run: bool) -> tuple[dict[str, Any] | None, bool]:
    argv = build_argv(config, game_dir=game_dir, dry_run=dry_run)
    if dry_run:
        print_planned(report, "build", argv)
        return None, True
    value, returncode, command = invoke_worker("build", [item for item in argv if item != "--dry-run"])
    assert value is not None
    error = worker_error(value, returncode)
    if error:
        report["phases"].append(phase("build", "failed", argv=command, error=error))
        return value, False
    report["phases"].append(phase("build", "completed", argv=command, result=value))
    artifact = value.get("artifact")
    if not isinstance(artifact, dict) or not artifact.get("path"):
        report["phases"][-1]["status"] = "failed"
        report["phases"][-1]["error"] = {"code": "build_artifact_missing", "message": "build result has no explicit artifact"}
        return value, False
    return value, True


def run_devices(report: dict[str, Any], config: Mapping[str, Any], dry_run: bool) -> tuple[dict[str, Any] | None, bool]:
    device = config["device"]
    argv = ["devices", "--target", config["target"], "--json"]
    add_optional_paths(argv, (("--bridge", discover_bridge(config)),))
    if dry_run:
        print_planned(report, "devices", argv)
        return None, True
    value, returncode, command = invoke_worker("device", argv)
    assert value is not None
    error = worker_error(value, returncode)
    report["phases"].append(phase("devices", str(value.get("status") or "failed"), argv=command, result=value, error=error))
    return value, error is None


def run_install(
    report: dict[str, Any],
    config: Mapping[str, Any],
    *,
    artifact: Path,
    resources: Path,
    serial: str | None = None,
    dry_run: bool,
) -> tuple[dict[str, Any] | None, bool]:
    if dry_run:
        argv = install_argv(config, artifact=artifact, resources=resources, serial=serial, dry_run=True)
        print_planned(report, "install", argv)
        return None, True
    signed = artifact_signature(artifact, config["target"])
    if signed is False:
        report["status"] = "awaiting_signing"
        report["phases"].append(phase("install", "blocked", reason="artifact is unsigned; sign it before installation"))
        return None, True
    argv = install_argv(config, artifact=artifact, resources=resources, serial=serial, dry_run=dry_run)
    value, returncode, command = invoke_worker("device", [item for item in argv if item != "--dry-run"])
    assert value is not None
    report["installed"] = install_confirmed(value)
    error = worker_error(value, returncode)
    if error:
        report["phases"].append(phase("install", "failed", argv=command, error=error, result=value))
        return value, False
    status = str(value.get("status") or "completed")
    if config["target"] == "ohos" and config["device"].get("import_mode") == "debug-sandbox":
        status = "awaiting_debug_import"
        value["status"] = status
    report["phases"].append(phase("install", status, argv=command, result=value))
    return value, True


def handle_import_resources(config: Mapping[str, Any], report: dict[str, Any], dry_run: bool) -> tuple[dict[str, Any], int]:
    if config["target"] != "ohos":
        return fail(report, "target_unsupported", "import-resources is the HarmonyOS debug sandbox import step")
    serial = config["device"].get("serial")
    resources = config["install"].get("resources")
    if not serial:
        return fail(report, "serial_required", "import-resources requires an explicit device serial")
    if not resources or not resources.is_dir():
        return fail(report, "resources_required", "import-resources requires install.resources")
    argv = ["--resources", str(resources), "--serial", serial,
            "--package", config["build"].get("app_id") or "com.xiao.boluo", "--json"]
    add_optional_paths(argv, (("--bridge", discover_bridge(config)),))
    if dry_run:
        print_planned(report, "import-resources", argv + ["--dry-run"])
        report["status"] = "planned"
        return report, 0
    value, returncode, command = invoke_worker("ohos_import", argv)
    assert value is not None
    error = worker_error(value, returncode)
    report["phases"].append(phase("import-resources", str(value.get("status") or "failed"), argv=command, result=value, error=error))
    report["status"] = "failed" if error else str(value.get("status") or "awaiting_app_import")
    report["error"] = error
    return report, 2 if error else 0


def doctor_access(path: Path | None, kind: str) -> dict[str, Any]:
    ok = bool(path and (path.is_file() if kind == "file" else path.is_dir()))
    return {"name": kind, "status": "ok" if ok else "missing", "path": str(path) if path else None}


def executable_check(name: str, configured: Path | None = None) -> dict[str, Any]:
    if configured:
        ok = configured.is_file()
        return {"name": name, "status": "ok" if ok else "missing", "path": str(configured)}
    path = shutil.which(name)
    return {"name": name, "status": "ok" if path else "missing", "path": path}


def discover_resource_tool(name: str, configured: Path | None = None) -> Path | None:
    if configured:
        return configured if configured.is_file() else None
    path = shutil.which(name)
    if path:
        return resolve_path(path)
    roots = sorted((SOURCE_ROOT.parent / "tools/spirv").glob("*/bin/" + name))
    return next((resolve_path(path) for path in roots if path.is_file()), None)


def discover_bridge(config: Mapping[str, Any]) -> Path | None:
    configured = config["device"].get("bridge")
    if configured:
        return configured if configured.is_file() else None
    wanted = "hdc" if config["target"] == "ohos" else "adb"
    path = shutil.which(wanted)
    if path:
        return resolve_path(path)
    if config["target"] == "ohos":
        try:
            worker = load_build_worker()
            deveco = worker.normalize_deveco_root(config["build"].get("deveco")) if config["build"].get("deveco") else worker.discover_deveco(None)
            sdk = worker.discover_harmony_sdk(config["build"].get("sdk"), deveco)
            candidate = sdk / "openharmony/toolchains/hdc"
            if candidate.is_file():
                return resolve_path(candidate)
        except Exception:
            pass
    else:
        sdk = config["build"].get("sdk") or resolve_path(os.environ.get("ANDROID_SDK_ROOT") or os.environ.get("ANDROID_HOME"))
        if sdk:
            candidate = sdk / ("platform-tools/adb.exe" if os.name == "nt" else "platform-tools/adb")
            if candidate.is_file():
                return resolve_path(candidate)
    return None


def load_build_worker() -> Any:
    spec = importlib.util.spec_from_file_location("_mobile_build_doctor", WORKERS["build"])
    if not spec or not spec.loader:
        raise OrchestratorError("worker_missing", "mobile_build.py could not be loaded for discovery")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def success_check(name: str, path: Path | str | None, **detail: Any) -> dict[str, Any]:
    row = {"name": name, "status": "ok" if path else "missing", "path": str(path) if path else None}
    row.update(detail)
    return row


def doctor(config: Mapping[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": 1,
        "command": "doctor",
        "status": "ok",
        "python": sys.executable,
        "checks": [],
    }
    checks = result["checks"]
    for name in ("mobile_install.py", "mobile_resources.py", "mobile_build.py", "mobile_device.py", "mobile_ohos_import.py"):
        path = HERE / name
        checks.append({"name": f"module:{name}", "status": "ok" if path.is_file() else "missing", "path": str(path)})
    for module in ("PIL", "numpy", "lz4", "capstone"):
        checks.append({"name": f"python:{module}", "status": "ok" if importlib.util.find_spec(module) else "missing"})
    checks.append(executable_check("git"))
    checks.append(executable_check("dotnet"))
    for name in ("spirv-opt", "spirv-val", "glslc"):
        checks.append(success_check(name, discover_resource_tool(name)))

    if config:
        target = config["target"]
        checks.append(success_check("hdc" if target == "ohos" else "adb", discover_bridge(config)))
        build = config["build"]
        try:
            worker = load_build_worker()
            if target == "android":
                sdk = worker.discover_android_sdk(build.get("sdk"), SOURCE_ROOT)
                gradle = worker.discover_gradle(build.get("gradle"), SOURCE_ROOT)
                java_home = worker.discover_java_home(build.get("java_home"))
                checks.append(doctor_access(sdk, "dir"))
                checks.append(success_check("gradle:auto", gradle))
                if java_home:
                    checks.append(doctor_access(java_home, "dir"))
            else:
                deveco_root = worker.normalize_deveco_root(build.get("deveco")) if build.get("deveco") else worker.discover_deveco(None)
                tools = worker.discover_deveco_tools(deveco_root)
                sdk = worker.discover_harmony_sdk(build.get("sdk"), deveco_root)
                info = worker.read_harmony_sdk(sdk)
                checks.append(success_check("deveco:auto", tools["root"]))
                for name in ("node", "java", "ohpm", "hvigor"):
                    checks.append(success_check(f"deveco:{name}", tools[name]))
                checks.append(success_check("harmony-sdk:auto", sdk, api=info["api"], version=info["platform"]))
        except Exception as exc:
            checks.append({"name": f"{target}:auto-discovery", "status": "missing", "reason": str(exc)})
        if config.get("game_dir"):
            checks.append(doctor_access(config["game_dir"], "dir"))
        if config.get("roms"):
            checks.append(executable_check("hactool", config["resource"].get("hactool")))
            checks.append(doctor_access(config.get("keys"), "file"))
            checks.extend(executable_check(name) for name in ("bash", "shasum"))

    missing = [check for check in checks if check["status"] == "missing"]
    result["status"] = "ok" if not missing else "issues"
    result["missing"] = missing
    return result


def handle_prepare(config: Mapping[str, Any], report: dict[str, Any], dry_run: bool) -> tuple[dict[str, Any], int]:
    result, ok = run_prepare(report, config, dry_run)
    report["status"] = "planned" if dry_run else ("completed" if ok else "failed")
    if ok and result:
        report["game_dir"] = result.get("game_dir")
        report["resources"] = result.get("mobile_device_resources") or result.get("import_staging")
    return report, 0 if ok else 2


def handle_build(config: Mapping[str, Any], report: dict[str, Any], dry_run: bool) -> tuple[dict[str, Any], int]:
    game_dir = config.get("game_dir")
    result, ok = run_build(report, config, game_dir=game_dir, dry_run=dry_run)
    report["status"] = "planned" if dry_run else ("completed" if ok else "failed")
    if ok and result:
        report["artifact"] = result.get("artifact")
    return report, 0 if ok else 2


def handle_devices(config: Mapping[str, Any], report: dict[str, Any], dry_run: bool) -> tuple[dict[str, Any], int]:
    result, ok = run_devices(report, config, dry_run)
    report["status"] = "planned" if dry_run else (str(result.get("status") if result else "failed"))
    if result:
        report["devices"] = result.get("devices", [])
        report["issues"] = result.get("issues", [])
    return report, 0 if ok else 1


def handle_install(config: Mapping[str, Any], report: dict[str, Any], dry_run: bool) -> tuple[dict[str, Any], int]:
    artifact = config["install"].get("artifact")
    resources = config["install"].get("resources")
    if not artifact or not artifact.is_file():
        return fail(report, "artifact_required", "install requires explicit install.artifact")
    if not resources or not resources.is_dir():
        return fail(report, "resources_required", "install requires explicit install.resources")
    result, ok = run_install(report, config, artifact=artifact, resources=resources, dry_run=dry_run)
    if report["status"] == "awaiting_signing":
        return report, 0
    report["status"] = "planned" if dry_run else (str(result.get("status") if result else "failed"))
    return report, 0 if ok else 2


def handle_run(config: Mapping[str, Any], report: dict[str, Any], dry_run: bool) -> tuple[dict[str, Any], int]:
    if dry_run:
        run_prepare(report, config, True)
        run_build(report, config, game_dir=config.get("game_dir"), dry_run=True)
        serial = config["device"].get("serial") or "<explicit-serial-required>"
        argv = install_argv(config, artifact=Path("<build-artifact>"), resources=Path("<prepared-resources>"), serial=serial, dry_run=True)
        print_planned(report, "install", argv)
        if not config["device"].get("serial"):
            report["phases"][-1]["requirements"] = ["device.serial"]
        report["status"] = "planned"
        return report, 0

    prepared, ok = run_prepare(report, config, False)
    if not ok:
        return fail(report, "prepare_failed", "prepare phase failed; build and install were not started")
    assert prepared is not None
    game_dir = resolve_path(prepared.get("game_dir"))
    resources = resolve_path(prepared.get("mobile_device_resources") or prepared.get("import_staging"))
    if not game_dir:
        return fail(report, "prepare_game_dir_missing", "prepare did not expose game_dir")
    if not resources:
        return fail(report, "prepare_resources_missing", "prepare did not expose mobile_device_resources")

    built, ok = run_build(report, config, game_dir=game_dir, dry_run=False)
    if not ok:
        return fail(report, "build_failed", "build phase failed; install was not started")
    assert built is not None
    artifact_data = built.get("artifact", {})
    artifact = resolve_path(artifact_data.get("path"))
    if not artifact:
        return fail(report, "build_artifact_missing", "build did not expose an artifact path")
    if not bool(artifact_data.get("signed")):
        report["status"] = "awaiting_signing"
        report["phases"].append(phase("install", "blocked", reason="unsigned HAP/APK requires signing; nothing was installed"))
        return report, 0
    configured_serial = config["device"].get("serial")
    if not configured_serial:
        return fail(report, "serial_required", "run reached install without an explicit device serial")
    installed, ok = run_install(report, config, artifact=artifact, resources=resources, serial=configured_serial, dry_run=False)
    if not ok:
        return fail(report, "install_failed", "install phase failed")
    status = str(installed.get("status") if installed else "completed")
    report["status"] = status if status.startswith("awaiting_") else "completed"
    report["artifact"] = artifact_data
    report["resources"] = str(resources)
    return report, 0


def git_source_paths(source: Path) -> list[str]:
    git = shutil.which("git")
    if not git:
        raise OrchestratorError("git_required", "git was not found and no source manifest is available")
    result = subprocess.run(
        [git, "-C", str(source), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        check=False,
    )
    if result.returncode:
        raise OrchestratorError("source_inventory_failed", "git could not enumerate the current source tree")
    return sorted({item.decode("utf-8", "surrogateescape") for item in result.stdout.split(b"\0") if item})


def manifest_source_paths(source: Path) -> list[str]:
    for name in SOURCE_MANIFEST_ALIASES:
        path = source / name
        if path.is_file():
            return [line.split("\t", 1)[0].strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
    raise OrchestratorError("source_manifest_missing", "Git is unavailable and SOURCE_EXPORT_MANIFEST.txt was not found")


def safe_relative(path: str) -> str:
    value = path.replace("\\", "/")
    rel = PurePosixPath(value)
    if not value or rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        raise OrchestratorError("unsafe_source_path", f"unsafe source export path: {path!r}")
    return rel.as_posix()


def excluded_export_path(rel: str, sensitive: set[Path], source: Path, output: Path) -> bool:
    absolute = (source / rel).resolve(strict=False)
    if absolute == output or is_relative_to(absolute, output):
        return True
    if any(absolute == item or is_relative_to(absolute, item) for item in sensitive):
        return True
    parts = PurePosixPath(rel).parts
    if ".git" in parts or ".dart_tool" in parts:
        return True
    if "Gen" in parts or "__pycache__" in parts or ".tmp" in parts:
        return True
    exact = {
        "dm/host/dm_vault_tab.h", "dm/host/dm_hud_exp.h", "dm/host/dm_bright_levels.inc",
        "dm/ios/Sources/dm_touch_native_art.c", "dm/harmony/app/build-profile.json5",
    }
    if rel in exact:
        return True
    lower = rel.lower()
    if re.search(r"(^|/)(build|dist|outputs|oh_modules|node_modules|\.gradle|\.hvigor|\.cxx)(/|$)", lower):
        return True
    if lower.startswith(("cache/", "work/", "data/", "switch/", "s2x/")):
        return True
    if any(part.lower() in {"cache", "work", "switch", "s2x"} for part in parts):
        return True
    if re.search(r"(^|/)data/switch(/|$)", lower):
        return True
    if lower.endswith((
        ".apk", ".aab", ".hap", ".app", ".ipa", ".dmg", ".nsp", ".xci", ".nca", ".tik",
        ".data", ".xgs", ".patched.bin", ".keys", ".p12", ".pfx", ".jks", ".keystore", ".p7b", ".cer",
    )):
        return True
    if re.search(r"(^|/)(prod\.keys|title\.keys|private|artifacts|cloud-save\.json|mobile-install\.json|local\.properties|local\.cmake|local\.env|\.ohpmrc|\.env)(/|$)", lower):
        return True
    if re.search(r"^dm/ios/sources/touchart/dm_native_.*\.png$", lower):
        return True
    return False


def collect_export_entries(config: Mapping[str, Any], output: Path) -> tuple[list[tuple[str, Path]], str]:
    mode = "git" if (SOURCE_ROOT / ".git").exists() else "manifest"
    paths = git_source_paths(SOURCE_ROOT) if mode == "git" else manifest_source_paths(SOURCE_ROOT)
    protected: set[Path] = {resolve_path(config.get("workspace"))}
    for key in ("game_dir", "keys"):
        value = resolve_path(config.get(key))
        if value:
            protected.add(value)
    for key in ("roms",):
        protected.update(resolve_path(item) for item in config.get(key, []))
    for group in ("resource", "build", "install"):
        for value in config.get(group, {}).values():
            if isinstance(value, Path):
                protected.add(resolve_path(value))
    entries: list[tuple[str, Path]] = []
    digest = hashlib.sha256()
    for raw in paths:
        rel = safe_relative(raw)
        if excluded_export_path(rel, protected, SOURCE_ROOT, output):
            continue
        path = SOURCE_ROOT / rel
        if not path.exists():
            continue
        if path.is_symlink() or not path.is_file():
            raise OrchestratorError("unsafe_source_path", f"source export refuses non-regular file: {rel}")
        entries.append((rel, path))
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
    if not entries:
        raise OrchestratorError("source_export_empty", "source export filtered every file")
    return sorted(entries), digest.hexdigest()


def handle_export_source(config: Mapping[str, Any], report: dict[str, Any], dry_run: bool) -> tuple[dict[str, Any], int]:
    output = config["export"].get("output")
    if not output:
        raise OrchestratorError("export_output_required", "export-source requires export.output")
    if output == SOURCE_ROOT or is_relative_to(output, SOURCE_ROOT):
        raise OrchestratorError("unsafe_export_output", "export.output must be outside the source checkout")
    if dry_run:
        report["output"] = str(output)
        report["manifest"] = SOURCE_MANIFEST
        report["dm_manifest"] = DM_SOURCE_MANIFEST
        report["status"] = "planned"
        report["phases"].append(phase("export-source", "planned", reason="no inventory, writes, or archive creation in dry-run"))
        return report, 0
    entries, fingerprint = collect_export_entries(config, output)
    dm_entries = [(rel, path) for rel, path in entries if rel.startswith("dm/")]
    if not dm_entries:
        raise OrchestratorError("source_export_empty", "source export has no dm/ files for worker fallback")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name("." + output.name + ".tmp-" + str(os.getpid()))
    manifest_text = "\n".join(rel for rel, _ in entries) + "\n"
    dm_manifest_text = "\n".join(rel for rel, _ in dm_entries) + "\n"
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for rel, path in entries:
            info = zipfile.ZipInfo(rel, (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if os.access(path, os.X_OK) else 0o644) << 16
            archive.writestr(info, path.read_bytes())
        info = zipfile.ZipInfo(SOURCE_MANIFEST, (2020, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, manifest_text)
        info = zipfile.ZipInfo(DM_SOURCE_MANIFEST, (2020, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, dm_manifest_text)
    os.replace(temporary, output)
    report["status"] = "completed"
    report["output"] = str(output)
    report["sha256"] = sha256_file(output)
    report["source_fingerprint"] = fingerprint
    report["file_count"] = len(entries)
    report["manifest"] = SOURCE_MANIFEST
    report["dm_manifest"] = DM_SOURCE_MANIFEST
    report["phases"].append(phase("export-source", "completed", output=str(output), files=len(entries)))
    return report, 0


def dispatch(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    config_path = resolve_path(args.config) if args.config else None
    config, loaded_path = load_config(config_path, required=args.command != "doctor")
    normalized = normalize_config(config, loaded_path) if config else None
    target = normalized["target"] if normalized else None
    report = base_report(args.command, target=target, dry_run=args.dry_run, config=loaded_path)
    if args.command == "doctor":
        result = doctor(normalized)
        return result, 0 if result["status"] == "ok" else 1
    assert normalized is not None
    if args.command == "prepare":
        return handle_prepare(normalized, report, args.dry_run)
    if args.command == "build":
        return handle_build(normalized, report, args.dry_run)
    if args.command == "devices":
        return handle_devices(normalized, report, args.dry_run)
    if args.command == "install":
        return handle_install(normalized, report, args.dry_run)
    if args.command == "import-resources":
        return handle_import_resources(normalized, report, args.dry_run)
    if args.command == "run":
        return handle_run(normalized, report, args.dry_run)
    return handle_export_source(normalized, report, args.dry_run)


def main(argv: Sequence[str] | None = None) -> int:
    args = command_parser().parse_args(argv)
    try:
        report, status = dispatch(args)
    except OrchestratorError as exc:
        report = base_report(args.command, target=None, dry_run=args.dry_run, config=resolve_path(args.config) if args.config else None)
        report, status = fail(report, exc.code, exc.message)
    except KeyboardInterrupt:
        report = base_report(args.command, target=None, dry_run=args.dry_run, config=resolve_path(args.config) if args.config else None)
        report, status = fail(report, "interrupted", "interrupted", 130)
    emit(report, args.json)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
