#!/usr/bin/env python3
"""Build a D2R mobile application from a clean source snapshot.

The tool intentionally keeps the source checkout read-only.  Build inputs and
generated files live below ``--workspace``; a supplied Harmony signing profile
is copied only into that private build tree.

Examples:
    python dm/tools/mobile_build.py --target ohos \
      --workspace /path/to/work/harmony-build/application \
      --gen-dir /path/to/generated --dry-run --json

    python dm/tools/mobile_build.py --target android \
      --workspace /path/to/work/android-build \
      --game-dir /path/to/Update --sdk /path/to/android-sdk
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Sequence


HERE = Path(__file__).resolve().parent
SOURCE_ROOT = HERE.parents[1]
DM_ROOT = SOURCE_ROOT / "dm"

ANDROID_DEFAULT_APP_ID = "com.xiaoboluo.d2r.mate60"
OHOS_DEFAULT_APP_ID = "com.xiao.boluo"
DEFAULT_LABEL = "暗黑2"

GENERATED_FILES = (
    "dm_vault_tab.h",
    "dm_hud_exp.h",
    "dm_bright_levels.inc",
    "dm_touch_native_art.c",
)

# These files are generated from the user's game files or are private local
# build configuration.  They must never enter a source snapshot, even if a
# caller has force-added one to Git.
EXCLUDED_SOURCE_FILES = {
    "dm/host/dm_vault_tab.h",
    "dm/host/dm_hud_exp.h",
    "dm/host/dm_bright_levels.inc",
    "dm/ios/Sources/dm_touch_native_art.c",
    "dm/harmony/app/build-profile.json5",
}

SOURCE_MANIFEST_NAMES = (
    "dm/SOURCE_EXPORT_MANIFEST.txt",
    "dm/source_export_manifest.txt",
    "SOURCE_EXPORT_MANIFEST.txt",
)


class BuildError(RuntimeError):
    """A user-actionable build failure."""


def log(message: str) -> None:
    print(f"[mobile-build] {message}", file=sys.stderr, flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def resolve_path(value: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def ensure_external_workspace(workspace: Path, source_root: Path) -> None:
    """Reject a workspace that would write build output into the source tree."""
    workspace = resolve_path(workspace)
    source_root = resolve_path(source_root)
    if workspace == source_root or is_relative_to(workspace, source_root):
        raise BuildError("--workspace must be outside the source checkout")


def safe_source_relative(value: str) -> str:
    """Validate a source-export path before joining it to the checkout."""
    value = value.replace("\\", "/").strip()
    if not value or "\x00" in value:
        raise BuildError("source manifest contains an empty or invalid path")
    rel = PurePosixPath(value)
    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        raise BuildError(f"source manifest path escapes the checkout: {value!r}")
    if not rel.parts or rel.parts[0] != "dm":
        raise BuildError(f"source manifest path must be under dm/: {value!r}")
    return rel.as_posix()


def is_excluded_source(rel: str) -> bool:
    """Return True for ignored/generated assets and private build configuration."""
    rel = rel.replace("\\", "/")
    if rel in EXCLUDED_SOURCE_FILES:
        return True
    if PurePosixPath(rel).name == "cloud-save.json":
        return True  # Pairing credentials are injected only into a private build.
    parts = PurePosixPath(rel).parts
    if "Gen" in parts:
        return True
    if re.match(r"^dm/ios/Sources/TouchArt/dm_native_.*\.png$", rel):
        return True
    if re.match(r"^dm/ios/tools/native_art/(?:skills|skilldesc)\.txt$", rel):
        return True
    if rel.endswith("__pycache__") or "/__pycache__/" in rel:
        return True
    return False


def ensure_destination_in_workspace(destination: Path, workspace: Path) -> None:
    destination = resolve_path(destination)
    workspace = resolve_path(workspace)
    if destination != workspace and not is_relative_to(destination, workspace):
        raise BuildError(f"refusing to write outside --workspace: {destination}")


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    os.replace(temporary, path)


def read_json(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, ValueError) as exc:
        raise BuildError(f"cannot read JSON metadata: {path.name}: {exc}") from exc


def first_existing(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if path.is_file():
            return resolve_path(path)
    return None


def first_dir(paths: Iterable[Path]) -> Path | None:
    for path in paths:
        if path.is_dir():
            return resolve_path(path)
    return None


def python_command_default() -> str:
    return sys.executable or "python"


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build D2R Android or HarmonyOS from a source snapshot",
        allow_abbrev=False,
    )
    parser.add_argument("--target", required=True, choices=("android", "ohos"))
    parser.add_argument("--workspace", required=True, type=Path, help="private build workspace")
    parser.add_argument("--game-dir", type=Path, help="Switch Update directory; optional with --gen-dir")
    parser.add_argument("--python", default=python_command_default(), help="Python for source generators")
    parser.add_argument("--sdk", type=Path, help="Android SDK or DevEco sdk/default directory")
    parser.add_argument("--deveco", type=Path, help="DevEco app/install directory")
    parser.add_argument("--gradle", type=Path, help="Gradle executable (Android)")
    parser.add_argument("--java-home", type=Path, help="Java home used by Gradle or Hvigor")
    parser.add_argument("--gen-dir", type=Path, help="reuse exactly the four generated runtime-stub files")
    parser.add_argument("--build-profile", type=Path, help="private Harmony signing profile")
    parser.add_argument("--cloud-config", type=Path, help="private cloud-save pairing JSON; never included in source exports")
    parser.add_argument("--debug-keystore", type=Path, help="existing Android debug signing key (standard debug alias/password)")
    parser.add_argument("--app-id")
    parser.add_argument("--label", default=DEFAULT_LABEL)
    parser.add_argument("--sdk-version", help="required Android API or Harmony SDK/API version")
    parser.add_argument("--build-mode", choices=("debug", "release"), default="debug", help="mobile app build mode")
    parser.add_argument("--dry-run", action="store_true", help="validate and print the plan without writes or builds")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    return parser


def validate_common_args(args: argparse.Namespace) -> dict[str, Any]:
    workspace = resolve_path(args.workspace)
    source_root = resolve_path(SOURCE_ROOT)
    ensure_external_workspace(workspace, source_root)

    label = args.label.strip()
    if not label or "\n" in label or "\r" in label:
        raise BuildError("--label must be a non-empty single line")

    app_id = args.app_id or (ANDROID_DEFAULT_APP_ID if args.target == "android" else OHOS_DEFAULT_APP_ID)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", app_id):
        raise BuildError("--app-id must be a dotted application identifier")

    python = resolve_path(args.python) if os.path.sep in args.python or (os.path.altsep and os.path.altsep in args.python) else args.python
    if isinstance(python, Path) and not python.is_file():
        raise BuildError("--python does not name an executable file")

    gen_dir = resolve_path(args.gen_dir) if args.gen_dir else None
    game_dir = resolve_path(args.game_dir) if args.game_dir else None
    if gen_dir:
        missing = [name for name in GENERATED_FILES if not (gen_dir / name).is_file()]
        if missing:
            raise BuildError("--gen-dir is missing required generated files: " + ", ".join(missing))
        validate_runtime_stubs(gen_dir)
    else:
        if not game_dir:
            raise BuildError("--game-dir is required unless --gen-dir is supplied")
        romfs = game_dir / "RomFS"
        if not (romfs / "data" / "build_manifest.psv").is_file():
            raise BuildError("--game-dir must be an Update directory containing RomFS/data/build_manifest.psv")

    build_profile = resolve_path(args.build_profile) if args.build_profile else None
    if build_profile:
        if args.target != "ohos":
            raise BuildError("--build-profile is supported only for --target ohos")
        if not build_profile.is_file():
            raise BuildError("--build-profile does not name a file")

    cloud_config = resolve_path(args.cloud_config) if getattr(args, "cloud_config", None) else None
    if cloud_config:
        if is_relative_to(cloud_config, source_root):
            raise BuildError("private --cloud-config must be outside the source checkout")
        read_cloud_config(cloud_config)
    debug_keystore = resolve_path(args.debug_keystore) if getattr(args, "debug_keystore", None) else None
    if debug_keystore:
        if args.target != "android" or not debug_keystore.is_file() or is_relative_to(debug_keystore, source_root):
            raise BuildError("--debug-keystore requires an existing Android debug key outside the source checkout")

    sdk = resolve_path(args.sdk) if args.sdk else None
    deveco = resolve_path(args.deveco) if args.deveco else None
    gradle = resolve_path(args.gradle) if args.gradle else None
    java_home = resolve_path(args.java_home) if args.java_home else None
    if java_home and not (java_home / "bin").is_dir():
        raise BuildError("--java-home does not contain bin/")

    product_path = source_root / "dm/PRODUCT"
    try:
        product = product_path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError, UnicodeError) as exc:
        raise BuildError("cannot read dm/PRODUCT for the runtime product") from exc
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", product):
        raise BuildError("dm/PRODUCT is empty or contains unsupported characters")

    return {
        "workspace": workspace,
        "source_root": source_root,
        "dm_root": source_root / "dm",
        "python": python,
        "gen_dir": gen_dir,
        "game_dir": game_dir,
        "romfs": game_dir / "RomFS" if game_dir else None,
        "build_profile": build_profile,
        "cloud_config": cloud_config,
        "debug_keystore": debug_keystore,
        "sdk": sdk,
        "deveco": deveco,
        "gradle": gradle,
        "java_home": java_home,
        "app_id": app_id,
        "label": label,
        "product": product,
    }


def read_cloud_config(path: Path) -> dict[str, str]:
    # Deliberately omit input data and parser exception text from errors.
    from urllib.parse import urlsplit
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        endpoint, token = value["endpoint"], value["token"]
        parsed = urlsplit(endpoint)
        if (not isinstance(token, str) or not 32 <= len(token) <= 512 or
                any(ord(c) < 33 or ord(c) > 126 for c in token) or
                parsed.scheme != "https" or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment or not parsed.path.endswith("/v1")):
            raise ValueError()
        return {"endpoint": endpoint.rstrip("/"), "token": token}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise BuildError("invalid private cloud config: require HTTPS endpoint ending /v1 and a 32–512 character token") from None


def stage_cloud_config(dm_copy: Path, target: str, config: Path | None) -> None:
    if not config:
        return
    relative = ("android/app/src/main/assets/cloud-save.json" if target == "android" else
                "harmony/app/entry/src/main/resources/rawfile/cloud-save.json")
    destination = dm_copy / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(read_cloud_config(config)), encoding="utf-8")
    destination.chmod(0o600)


def validate_runtime_stubs(gen_dir: Path) -> None:
    """Require the data-free stubs expected by mobile builds."""
    try:
        hud = (gen_dir / "dm_hud_exp.h").read_text(encoding="utf-8")
        vault = (gen_dir / "dm_vault_tab.h").read_text(encoding="utf-8")
        art = (gen_dir / "dm_touch_native_art.c").read_text(encoding="utf-8")
    except OSError as exc:
        raise BuildError(f"cannot read generated runtime stubs: {exc}") from exc
    checks = (
        ("dm_hud_exp.h", "static uint32_t k_exp[100];" in hud),
        ("dm_vault_tab.h", "static vt_row vt_rows[" in vault and "dm_derived_each(\"VTROW\"" in vault),
        (
            "dm_touch_native_art.c",
            re.search(r"\}\s+rows\[\d+\]\s*;", art) is not None
            and "ta_row_cb" in art
            and "dm_derived_each(\"TAROW\"" in art,
        ),
    )
    bad = [name for name, ok in checks if not ok]
    if bad:
        raise BuildError(
            "generated files are not runtime stubs (run shell_stubs.py stubs-gen): " + ", ".join(bad)
        )


def source_manifest(root: Path) -> Path | None:
    configured = os.environ.get("DM_SOURCE_MANIFEST")
    candidates = [resolve_path(configured)] if configured else []
    candidates.extend(root / name for name in SOURCE_MANIFEST_NAMES)
    return first_existing(candidates)


def snapshot_mode(source_root: Path) -> str:
    return "git" if (source_root / ".git").exists() else "manifest"


def source_entries_from_git(source_root: Path) -> list[str]:
    command = ["git", "-C", str(source_root), "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "dm"]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except OSError as exc:
        raise BuildError(f"cannot run git for the source snapshot: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()[-1:]
        raise BuildError("git source export failed" + (": " + detail[0] if detail else ""))
    values = [item.decode("utf-8", "surrogateescape") for item in result.stdout.split(b"\0") if item]
    return values


def source_entries_from_manifest(source_root: Path) -> list[str]:
    manifest = source_manifest(source_root)
    if not manifest:
        raise BuildError(
            "Git is unavailable and no source export manifest was found "
            "(set DM_SOURCE_MANIFEST or add dm/SOURCE_EXPORT_MANIFEST.txt)"
        )
    entries: list[str] = []
    for raw in manifest.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        entries.append(line.split("\t", 1)[0])
    return entries


def copy_source_snapshot(source_root: Path, tree_root: Path) -> dict[str, Any]:
    entries = (
        source_entries_from_git(source_root)
        if snapshot_mode(source_root) == "git"
        else source_entries_from_manifest(source_root)
    )
    digest = hashlib.sha256()
    copied = 0
    excluded = 0
    seen: set[str] = set()
    for raw_rel in sorted(entries):
        rel = safe_source_relative(raw_rel)
        if rel in seen:
            continue
        seen.add(rel)
        if is_excluded_source(rel):
            excluded += 1
            continue
        source = source_root / rel
        if not source.exists():
            continue  # a staged deletion is not part of the current source tree
        if source.is_symlink() or not source.is_file():
            raise BuildError(f"source export refuses non-regular file: {rel}")
        destination = tree_root / rel
        ensure_destination_in_workspace(destination, tree_root)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        shutil.copystat(source, destination)
        content_hash = sha256_file(source)
        digest.update(rel.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(content_hash))
        copied += 1
    if not copied:
        raise BuildError("source snapshot is empty")
    return {
        "mode": snapshot_mode(source_root),
        "files": copied,
        "excluded": excluded,
        "digest": digest.hexdigest(),
    }


def copy_exact_generated(source: Path, destination: Path) -> dict[str, str]:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    hashes: dict[str, str] = {}
    for name in GENERATED_FILES:
        item = source / name
        target = destination / name
        shutil.copyfile(item, target)
        hashes[name] = sha256_file(target)
    validate_runtime_stubs(destination)
    return hashes


def run_command(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    env: dict[str, str],
    label: str,
    log_path: Path | None = None,
    capture_on_failure: bool = False,
) -> None:
    command_text = [os.fspath(value) for value in command]
    if log_path:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("wb") as stream:
            result = subprocess.run(command_text, cwd=cwd, env=env, stdout=stream, stderr=subprocess.STDOUT, check=False)
    elif capture_on_failure:
        result = subprocess.run(command_text, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    else:
        result = subprocess.run(command_text, cwd=cwd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if result.returncode:
        suffix = f"; see {log_path}" if log_path else ""
        raise BuildError(f"{label} failed (exit {result.returncode}){suffix}")


def child_environment(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def generate_runtime_stubs(
    dm_copy: Path,
    romfs: Path,
    python: str | os.PathLike[str],
    build_root: Path,
) -> tuple[Path, Path, dict[str, str]]:
    raw = build_root / "generated-real"
    native = build_root / "native-art"
    gen = build_root / "gen"
    logs = build_root / "logs"
    for path in (raw, native, gen):
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
    romfs = resolve_path(romfs)
    commands: list[tuple[str, list[str | os.PathLike[str]], Path, Path]] = [
        (
            "generate dm_vault_tab.h",
            [python, dm_copy / "ios/tools/gen_vault_table.py", romfs],
            dm_copy,
            raw / "dm_vault_tab.h",
        ),
        (
            "generate dm_hud_exp.h",
            [python, dm_copy / "ios/tools/gen_hud_exp.py", romfs],
            dm_copy,
            raw / "dm_hud_exp.h",
        ),
        (
            "generate dm_bright_levels.inc",
            [python, dm_copy / "tools/gen_bright_levels.py", romfs],
            dm_copy,
            raw / "dm_bright_levels.inc",
        ),
    ]
    for label, command, cwd, output in commands:
        with output.open("wb") as stream:
            result = subprocess.run(command, cwd=cwd, env=child_environment(), stdout=stream, stderr=subprocess.PIPE, check=False)
        if result.returncode:
            raise BuildError(f"{label} failed (exit {result.returncode}); generator output was not printed")

    run_command(
        [python, dm_copy / "ios/tools/native_art/generate.py", romfs, "--output-dir", native],
        cwd=dm_copy,
        env=child_environment(),
        label="generate native art index",
        log_path=logs / "native-art.log",
    )
    shutil.copyfile(native / "dm_touch_native_art.c", raw / "dm_touch_native_art.c")
    for name in GENERATED_FILES:
        if not (raw / name).is_file() or (raw / name).stat().st_size == 0:
            raise BuildError(f"generator did not produce {name}")

    derived = build_root / "derived.tsv"
    run_command(
        [python, dm_copy / "ios/tools/shell_stubs.py", "data-gen", raw, derived],
        cwd=dm_copy,
        env=child_environment(),
        label="extract runtime game data",
        log_path=logs / "shell-stubs-data.log",
    )
    run_command(
        [python, dm_copy / "ios/tools/shell_stubs.py", "stubs-gen", raw],
        cwd=dm_copy,
        env=child_environment(),
        label="write runtime table stubs",
        log_path=logs / "shell-stubs.log",
    )
    hashes = copy_exact_generated(raw, gen)
    return gen, derived, hashes


def parse_android_source(dm_root: Path) -> dict[str, Any]:
    gradle = dm_root / "android/app/build.gradle"
    try:
        text = gradle.read_text(encoding="utf-8")
    except OSError as exc:
        raise BuildError(f"cannot read Android build.gradle: {exc}") from exc

    def one(pattern: str, name: str) -> str:
        match = re.search(pattern, text, re.MULTILINE)
        if not match:
            raise BuildError(f"cannot find {name} in dm/android/app/build.gradle")
        return match.group(1)

    return {
        "compile_sdk": int(one(r"^\s*compileSdk\s+(\d+)\s*$", "compileSdk")),
        "target_sdk": int(one(r"^\s*targetSdk\s+(\d+)\s*$", "targetSdk")),
        "ndk_version": one(r"^\s*ndkVersion\s+'([^']+)'\s*$", "ndkVersion"),
        "cmake_version": one(r"cmake\s*\{[^}]*?version\s+'([^']+)'", "CMake version"),
        "version_code": int(one(r"^\s*versionCode\s+(\d+)\s*$", "versionCode")),
        "version_name": one(r"^\s*versionName\s+project\.findProperty\('dmVersionName'\)\s*\?:\s*'([^']+)'\s*$", "versionName"),
    }


def discover_android_sdk(explicit: Path | None, source_root: Path) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit)
    for key in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        if os.environ.get(key):
            candidates.append(resolve_path(os.environ[key]))
    candidates.extend(
        (
            source_root.parent / "tools/android-build/sdk",
            Path.home() / "Library/Android/sdk",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Android/Sdk",
        )
    )
    return first_dir(candidates)


def discover_gradle(explicit: Path | None, source_root: Path) -> Path | None:
    if explicit:
        return explicit / "bin/gradle" if explicit.is_dir() else explicit
    for key in ("GRADLE", "GRADLE_HOME"):
        value = os.environ.get(key)
        if value:
            candidate = resolve_path(value)
            return candidate / "bin/gradle" if candidate.is_dir() else candidate
    path_hit = shutil.which("gradle")
    if path_hit:
        return resolve_path(path_hit)
    roots = sorted((source_root.parent / "tools/android-build").glob("gradle-*"), reverse=True)
    for root in roots:
        candidate = root / "bin/gradle"
        if candidate.is_file():
            return candidate
    return None


def discover_java_home(explicit: Path | None) -> Path | None:
    if explicit:
        return explicit
    if os.environ.get("JAVA_HOME"):
        candidate = resolve_path(os.environ["JAVA_HOME"])
        if (candidate / "bin").is_dir():
            return candidate
    return None


def validate_android_sdk(sdk: Path, requirements: dict[str, Any], compile_sdk: int) -> dict[str, Any]:
    platform = sdk / f"platforms/android-{compile_sdk}"
    ndk = sdk / f"ndk/{requirements['ndk_version']}"
    cmake_root = sdk / f"cmake/{requirements['cmake_version']}"
    cmake = first_existing((cmake_root / "bin/cmake.exe", cmake_root / "bin/cmake"))
    missing = []
    if not platform.is_dir():
        missing.append(f"platforms/android-{compile_sdk}")
    if not ndk.is_dir():
        missing.append(f"ndk/{requirements['ndk_version']}")
    if not cmake:
        missing.append(f"cmake/{requirements['cmake_version']}")
    if missing:
        raise BuildError("Android SDK is missing source-matched components: " + ", ".join(missing))
    return {"platform": str(platform), "ndk": str(ndk), "cmake": str(cmake)}


def android_plan(args: argparse.Namespace, common: dict[str, Any]) -> dict[str, Any]:
    requirements = parse_android_source(common["dm_root"])
    compile_sdk = int(args.sdk_version) if args.sdk_version and args.sdk_version.isdigit() else requirements["compile_sdk"]
    if args.sdk_version and not args.sdk_version.isdigit():
        raise BuildError("--sdk-version for Android must be a numeric API level")
    sdk = discover_android_sdk(common["sdk"], common["source_root"])
    gradle = discover_gradle(common["gradle"], common["source_root"])
    java_home = discover_java_home(common["java_home"])
    gen_dir = common["workspace"] / "build/gen"
    tree = common["workspace"] / "build/tree"
    task = ":app:assembleDebug" if args.build_mode == "debug" else ":app:assembleRelease"
    gradle_command: list[str] = [
        str(gradle or "gradle"), "--no-daemon", "-q", "-p", str(tree / "dm/android"), task,
        f"-PdmGenDir={gen_dir}", f"-PdmPython={common['python']}", f"-PdmAppId={common['app_id']}",
        f"-PdmAppLabel={common['label']}", f"-PdmVersionName={requirements['version_name']}",
        f"-PdmVersionCode={requirements['version_code']}",
    ]
    return {
        "target": "android",
        "product": common["product"],
        "build_product": "default",
        "app_id": common["app_id"],
        "label": common["label"],
        "sdk_version": str(compile_sdk),
        "native": {
            "mode": "gradle-externalNativeBuild",
            "prebuilt_fallback": False,
            "ndk": requirements["ndk_version"],
            "cmake": requirements["cmake_version"],
        },
        "build_mode": args.build_mode,
        "sdk_path": str(sdk) if sdk else None,
        "gradle": str(gradle or "gradle"),
        "java_home": str(java_home) if java_home else None,
        "version_code": requirements["version_code"],
        "version_name": requirements["version_name"],
        "commands": [redacted_command(gradle_command)],
    }


def normalize_deveco_root(path: Path) -> Path:
    path = resolve_path(path)
    if path.name == "Contents" and (path / "tools").is_dir():
        return path
    if (path / "Contents/tools").is_dir():
        return path / "Contents"
    if (path / "tools").is_dir():
        return path
    return path


def discover_deveco(explicit: Path | None) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit)
    for key in ("DEVECO_HOME", "DEVECO_STUDIO_HOME", "DEVECO_STUDIO_PATH"):
        if os.environ.get(key):
            candidates.append(resolve_path(os.environ[key]))
    if sys.platform == "darwin":
        candidates.append(Path("/Applications/DevEco-Studio.app"))
    elif os.name == "nt":
        for key in ("ProgramFiles", "ProgramFiles(x86)"):
            if os.environ.get(key):
                candidates.extend(
                    (
                        Path(os.environ[key]) / "Huawei/DevEco Studio",
                        Path(os.environ[key]) / "DevEco Studio",
                    )
                )
    else:
        candidates.append(Path.home() / "deveco-sdk")
    normalized = [normalize_deveco_root(path) for path in candidates]
    for path in normalized:
        if (path / "tools/node").is_dir() and (path / "sdk").is_dir():
            return resolve_path(path)
    raise BuildError("DevEco Studio was not found; pass --deveco PATH")


def discover_deveco_tools(root: Path) -> dict[str, Any]:
    root = normalize_deveco_root(root)
    node = first_existing(
        (
            root / "tools/node/bin/node",
            root / "tools/node/bin/node.exe",
            root / "tools/node/node.exe",
        )
    )
    hvigor = first_existing(
        (
            root / "tools/hvigor/bin/hvigorw",
            root / "tools/hvigor/bin/hvigorw.bat",
        )
    )
    ohpm = first_existing(
        (
            root / "tools/ohpm/bin/ohpm",
            root / "tools/ohpm/bin/ohpm.bat",
        )
    )
    java = first_existing(
        (
            root / "jbr/Contents/Home/bin/java",
            root / "jbr/bin/java.exe",
            root / "jbr/bin/java",
        )
    )
    ohpm_config = first_existing((root / "tools/ohpm/resources/.ohpmrc",))
    missing = [
        name
        for name, value in (("node", node), ("hvigor", hvigor), ("ohpm", ohpm), ("Java", java))
        if not value
    ]
    if missing:
        raise BuildError("DevEco installation is missing: " + ", ".join(missing))
    java_home = java.parent.parent  # .../Home/bin/java
    if java_home.name != "Home":
        java_home = java.parent.parent
    return {
        "root": root,
        "node": node,
        "node_home": node.parent.parent,
        "hvigor": hvigor,
        "hvigor_home": hvigor.parent.parent,
        "ohpm": ohpm,
        "ohpm_home": ohpm.parent.parent,
        "ohpm_config": ohpm_config,
        "java": java,
        "java_home": java_home,
    }


def discover_harmony_sdk(explicit: Path | None, deveco_root: Path) -> Path:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit)
    for key in ("DEVECO_SDK_HOME", "HOS_SDK_HOME", "OHOS_SDK_HOME"):
        if os.environ.get(key):
            candidates.append(resolve_path(os.environ[key]))
    candidates.extend((deveco_root / "sdk/default", deveco_root / "sdk"))
    for candidate in candidates:
        if candidate.is_file() and candidate.name == "sdk-pkg.json":
            return candidate.parent
        if (candidate / "sdk-pkg.json").is_file():
            return resolve_path(candidate)
    raise BuildError("HarmonyOS SDK sdk-pkg.json was not found; pass --sdk PATH")


def read_harmony_sdk(sdk: Path) -> dict[str, Any]:
    value = read_json(sdk / "sdk-pkg.json")
    data = value.get("data", value) if isinstance(value, dict) else {}
    try:
        api = int(data["apiVersion"])
        display = str(data.get("displayName") or "")
        platform = str(data.get("platformVersion") or "")
    except (KeyError, TypeError, ValueError) as exc:
        raise BuildError("HarmonyOS sdk-pkg.json has no usable apiVersion/platformVersion") from exc
    match = re.search(r"(\d+(?:\.\d+){1,2})", display)
    platform = platform or (match.group(1) if match else "")
    short_display = re.sub(r"^HarmonyOS\s+", "", display, flags=re.IGNORECASE).strip() or platform
    label = f"{short_display}({api})" if short_display else str(api)
    return {
        "api": api,
        "display": display,
        "platform": platform,
        "label": label,
        "model_version": "6.0.0",
    }


def required_harmony_api(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r"\((\d+)\)\s*$", value.strip())
    if match:
        return int(match.group(1))
    if value.strip().isdigit():
        return int(value.strip())
    major = re.match(r"^(\d+)", value.strip())
    return int(major.group(1)) if major else None


def ohos_plan(args: argparse.Namespace, common: dict[str, Any]) -> dict[str, Any]:
    deveco_root = normalize_deveco_root(common["deveco"]) if common["deveco"] else discover_deveco(None)
    tools = discover_deveco_tools(deveco_root)
    sdk = discover_harmony_sdk(common["sdk"], deveco_root)
    sdk_info = read_harmony_sdk(sdk)
    required_api = required_harmony_api(args.sdk_version)
    if required_api is not None and required_api > sdk_info["api"]:
        raise BuildError(
            f"requested Harmony API {required_api} is newer than the installed API {sdk_info['api']}; refusing to lower it"
        )
    effective_sdk = sdk_info["label"] if required_api is None else sdk_info["label"]
    app = common["workspace"] / "build/tree/dm/harmony/app"
    env = hvigor_environment(tools, sdk, common["java_home"])
    ohpm_command = [str(tools["ohpm"]), "install"]
    hvigor_command = [
        str(tools["hvigor"]), "--mode", "module", "-p", "module=entry@default", "-p", "product=default",
        f"-p", f"buildMode={args.build_mode}", "assembleHap"
    ]
    return {
        "target": "ohos",
        "product": common["product"],
        "build_product": "default",
        "app_id": common["app_id"],
        "label": common["label"],
        "sdk_version": effective_sdk,
        "installed_sdk": {
            "display": sdk_info["display"],
            "platform": sdk_info["platform"],
            "api": sdk_info["api"],
        },
        "model_version": sdk_info["model_version"],
        "build_mode": args.build_mode,
        "deveco": str(tools["root"]),
        "tools": {name: str(tools[name]) for name in ("node", "hvigor", "ohpm", "java")},
        "signing": "provided-private-profile" if common["build_profile"] else "unsigned",
        "tablet_device_type": True,
        "empty_debugpack_resources": str(app / "entry/src/debugpack/resources"),
        "local_cmake": str(app / "entry/src/main/cpp/local.cmake"),
        "environment_keys": sorted(key for key in env if key.endswith(("_HOME", "_SDK_HOME"))),
        "commands": [redacted_command(ohpm_command), redacted_command(hvigor_command)],
    }


def make_build_plan(args: argparse.Namespace) -> dict[str, Any]:
    common = validate_common_args(args)
    target_plan = android_plan(args, common) if args.target == "android" else ohos_plan(args, common)
    workspace = common["workspace"]
    plan = {
        "schema_version": 1,
        "dry_run": bool(args.dry_run),
        "target": args.target,
        "product": common["product"],
        "source": {
            "root": str(common["source_root"]),
            "snapshot": str(workspace / "build/tree"),
            "mode": snapshot_mode(common["source_root"]),
            "isolated": True,
            "excludes": sorted(EXCLUDED_SOURCE_FILES) + ["dm/**/Gen/**", "generated native-art PNGs", "private build-profile.json5"],
        },
        "generated": {
            "action": "reuse" if common["gen_dir"] else "generate",
            "source": str(common["gen_dir"]) if common["gen_dir"] else None,
            "destination": str(workspace / "build/gen"),
            "files": list(GENERATED_FILES),
            "runtime_stubs": True,
            "dynamic_native_art_rows": True,
        },
        "build": target_plan,
    }
    return plan


def hvigor_environment(tools: dict[str, Any], sdk: Path, requested_java_home: Path | None) -> dict[str, str]:
    env = child_environment()
    node_bin = str(tools["node"].parent)
    ohpm_bin = str(tools["ohpm"].parent)
    hvigor_bin = str(tools["hvigor"].parent)
    env["PATH"] = os.pathsep.join((node_bin, ohpm_bin, hvigor_bin, env.get("PATH", "")))
    env["NODE_HOME"] = str(tools["node_home"])
    env["OHPM_HOME"] = str(tools["ohpm_home"])
    env["DEVECO_SDK_HOME"] = str(sdk.parent)
    env["HOS_SDK_HOME"] = str(sdk.parent)
    env["JAVA_HOME"] = str(requested_java_home or tools["java_home"])
    return env


def replace_assignment(text: str, pattern: str, replacement: str, label: str) -> str:
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise BuildError(f"cannot override {label} in the private build copy")
    return updated


def patch_android_build_copy(app_root: Path, compile_sdk: int, version_code: int) -> None:
    gradle = app_root / "app/build.gradle"
    text = gradle.read_text(encoding="utf-8")
    text = replace_assignment(text, r"^(\s*compileSdk\s+)\d+\s*$", rf"\g<1>{compile_sdk}", "compileSdk")
    text = replace_assignment(text, r"^(\s*targetSdk\s+)\d+\s*$", rf"\g<1>{compile_sdk}", "targetSdk")
    text = replace_assignment(text, r"^(\s*versionCode\s+)\d+\s*$", rf"\g<1>{version_code}", "versionCode")
    gradle.write_text(text, encoding="utf-8", newline="\n")


def patch_json_string_file(path: Path, updates: dict[str, str]) -> None:
    value = read_json(path)
    changed = False
    for row in value.get("string", []):
        if row.get("name") in updates:
            row["value"] = updates[row["name"]]
            changed = True
    if not changed:
        raise BuildError(f"cannot override generated label in {path.name}")
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def cmake_quote(path: Path | str) -> str:
    value = os.fspath(path).replace("\\", "/")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{value}"'


def write_default_build_profile(path: Path, app_id: str, sdk_label: str) -> None:
    value = {
        "app": {
            "products": [
                {
                    "name": "default",
                    "bundleName": app_id,
                    "targetSdkVersion": sdk_label,
                    "compatibleSdkVersion": sdk_label,
                    "runtimeOS": "HarmonyOS",
                }
            ],
            "buildModeSet": [{"name": "debug"}, {"name": "release"}],
        },
        "modules": [
            {
                "name": "entry",
                "srcPath": "./entry",
                "targets": [{"name": "default", "applyToProducts": ["default"]}],
            }
        ],
    }
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def adjust_private_build_profile(path: Path, sdk_label: str) -> None:
    """Adjust only SDK fields in a copied signing profile; never print it."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BuildError(f"cannot prepare private Harmony build profile: {exc}") from exc
    for key in ("targetSdkVersion", "compatibleSdkVersion"):
        text, count = re.subn(
            rf'("{key}"\s*:\s*)"[^"]+"',
            rf'\g<1>"{sdk_label}"',
            text,
        )
        if not count:
            raise BuildError(f"private Harmony build profile does not define {key}")
    path.write_text(text, encoding="utf-8", newline="\n")


def replace_model_version(path: Path, model_version: str) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BuildError(f"cannot read {path.name} in the private build copy: {exc}") from exc
    text, count = re.subn(r'("modelVersion"\s*:\s*)"[^"]+"', rf'\g<1>"{model_version}"', text, count=1)
    if count != 1:
        raise BuildError(f"cannot set modelVersion in {path.name}")
    path.write_text(text, encoding="utf-8", newline="\n")


def ensure_tablet_device_type(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    match = re.search(r'"deviceTypes"\s*:\s*\[(.*?)\]', text, re.DOTALL)
    if not match:
        raise BuildError("Harmony module.json5 has no deviceTypes array")
    values = re.findall(r'"([^"]+)"', match.group(1))
    if "tablet" not in values:
        values.append("tablet")
    replacement = '"deviceTypes": [' + ", ".join(json.dumps(value) for value in values) + "]"
    text = text[: match.start()] + replacement + text[match.end() :]
    path.write_text(text, encoding="utf-8", newline="\n")


def prepare_ohos_tree(
    app: Path,
    *,
    app_id: str,
    label: str,
    sdk_label: str,
    model_version: str,
    gen_dir: Path,
    python: str | os.PathLike[str],
    build_profile: Path | None,
    tools: dict[str, Any],
) -> None:
    # The debug product references this ignored directory.  A clean source
    # export has no runtime payload, so provide the directory without assets.
    (app / "entry/src/debugpack/resources").mkdir(parents=True, exist_ok=True)

    patch_json_string_file(app / "AppScope/resources/base/element/string.json", {"app_name": label})
    patch_json_string_file(app / "entry/src/main/resources/base/element/string.json", {"EntryAbility_label": label})
    app_json = app / "AppScope/app.json5"
    text = app_json.read_text(encoding="utf-8")
    text = replace_assignment(text, r'("bundleName"\s*:\s*)"[^"]+"', rf'\g<1>"{app_id}"', "bundleName")
    app_json.write_text(text, encoding="utf-8", newline="\n")
    ensure_tablet_device_type(app / "entry/src/main/module.json5")

    replace_model_version(app / "oh-package.json5", model_version)
    replace_model_version(app / "hvigor/hvigor-config.json5", model_version)
    profile = app / "build-profile.json5"
    if build_profile:
        shutil.copyfile(build_profile, profile)
        adjust_private_build_profile(profile, sdk_label)
    else:
        write_default_build_profile(profile, app_id, sdk_label)

    local_cmake = app / "entry/src/main/cpp/local.cmake"
    with local_cmake.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(f"set(DM_GEN_DIR {cmake_quote(gen_dir)} CACHE PATH \"generated runtime stubs\")\n")
        stream.write(f"set(Python3_EXECUTABLE {cmake_quote(python)} CACHE FILEPATH \"build Python\")\n")

    if tools.get("ohpm_config"):
        shutil.copyfile(tools["ohpm_config"], app / ".ohpmrc")


def redacted_command(command: Sequence[str | os.PathLike[str]]) -> list[str]:
    values = [os.fspath(value) for value in command]
    redacted: list[str] = []
    hide_next = False
    for value in values:
        lower = value.lower()
        if hide_next or any(word in lower for word in ("password", "passphrase", "secret", "token=", "build-profile")):
            redacted.append("<redacted>")
            hide_next = False
            continue
        redacted.append(value)
        if lower in ("--password", "--passphrase", "--token"):
            hide_next = True
    return redacted


def artifact_signature_status(path: Path, target: str, has_profile: bool) -> tuple[bool, str]:
    name = path.name.lower()
    if "unsigned" in name or "unsigned" in path.parent.name.lower():
        return False, "unsigned"
    if "signed" in name:
        return True, "signature_detected"
    if target == "ohos":
        return (True, "signature_detected") if has_profile else (False, "unsigned")
    try:
        with zipfile.ZipFile(path) as archive:
            signed = any(
                item.filename.upper().startswith("META-INF/")
                and item.filename.upper().endswith((".RSA", ".DSA", ".EC"))
                for item in archive.infolist()
            )
    except (OSError, zipfile.BadZipFile):
        signed = False
    return (True, "signature_detected") if signed else (False, "unsigned")


def select_harmony_artifact(app: Path, has_profile: bool) -> Path:
    outputs = app / "entry/build/default/outputs/default"
    unsigned = outputs / "entry-default-unsigned.hap"
    signed = outputs / "entry-default-signed.hap"
    preferred = (signed, unsigned) if has_profile else (unsigned, signed)
    for candidate in preferred:
        if candidate.is_file():
            return resolve_path(candidate)
    raise BuildError(
        "Harmony build completed without an explicit output: expected "
        "entry-default-unsigned.hap or entry-default-signed.hap"
    )


def select_android_artifact(android_root: Path, build_mode: str = "debug") -> Path:
    output_dir = android_root / f"app/build/outputs/apk/{build_mode}"
    candidates = (
        (output_dir / f"app-{build_mode}.apk", output_dir / f"app-{build_mode}-unsigned.apk")
        if build_mode == "release"
        else (output_dir / f"app-{build_mode}.apk",)
    )
    for artifact in candidates:
        if artifact.is_file():
            return resolve_path(artifact)
    names = " or ".join(candidate.name for candidate in candidates)
    raise BuildError(f"Android build completed without the explicit {names} output")


def make_manifest(
    *,
    target: str,
    app_id: str,
    label: str,
    product: str,
    artifact: Path,
    source: dict[str, Any],
    generated: dict[str, str],
    build: dict[str, Any],
    has_profile: bool,
    derived: Path | None,
) -> dict[str, Any]:
    signed, status = artifact_signature_status(artifact, target, has_profile)
    return {
        "schema_version": 1,
        "target": target,
        "product": product,
        "app_id": app_id,
        "label": label,
        "source_digest": source["digest"],
        "source": source,
        "generated": {"files": generated, "runtime_stubs": True},
        "runtime_data": str(derived) if derived else None,
        "artifact": {
            "path": str(resolve_path(artifact)),
            "sha256": sha256_file(artifact),
            "signed": signed,
            "signature_status": status,
            "signature_verification": "not_verified",
        },
        "build": build,
        "signing_profile_used": has_profile,
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def execute_build(args: argparse.Namespace) -> dict[str, Any]:
    # Re-plan here so dry-run and real builds share validation/discovery.
    plan = make_build_plan(args)
    common = validate_common_args(args)
    workspace = common["workspace"]
    build_root = workspace / "build"
    tree = build_root / "tree"
    gen = build_root / "gen"
    if tree.exists():
        shutil.rmtree(tree)
    tree.mkdir(parents=True)

    log("isolating tracked and non-ignored source files")
    source = copy_source_snapshot(common["source_root"], tree)
    dm_copy = tree / "dm"
    stage_cloud_config(dm_copy, args.target, common["cloud_config"])
    derived: Path | None = None
    if common["gen_dir"]:
        generated = copy_exact_generated(common["gen_dir"], gen)
    else:
        log("generating runtime stubs from the supplied ROM")
        gen, derived, generated = generate_runtime_stubs(dm_copy, common["romfs"], common["python"], build_root)

    if args.target == "android":
        requirements = parse_android_source(dm_copy)
        compile_sdk = int(args.sdk_version) if args.sdk_version else requirements["compile_sdk"]
        sdk = discover_android_sdk(common["sdk"], common["source_root"])
        gradle = discover_gradle(common["gradle"], common["source_root"])
        java_home = discover_java_home(common["java_home"])
        if not sdk:
            raise BuildError("Android SDK was not found; pass --sdk PATH")
        if not gradle or not (gradle.is_file() or shutil.which(os.fspath(gradle))):
            raise BuildError("Gradle was not found; pass --gradle PATH")
        sdk_details = validate_android_sdk(sdk, requirements, compile_sdk)
        patch_android_build_copy(dm_copy / "android", compile_sdk, requirements["version_code"])
        env = child_environment()
        env["ANDROID_HOME"] = str(sdk)
        env["ANDROID_SDK_ROOT"] = str(sdk)
        if common["debug_keystore"]:
            signing_home = build_root / "private-android-user-home"
            signing_home.mkdir(mode=0o700, parents=True, exist_ok=True)
            signing_home.chmod(0o700)
            shutil.copyfile(common["debug_keystore"], signing_home / "debug.keystore")
            (signing_home / "debug.keystore").chmod(0o600)
            env["ANDROID_USER_HOME"] = str(signing_home)
        if java_home:
            env["JAVA_HOME"] = str(java_home)
        task = ":app:assembleDebug" if args.build_mode == "debug" else ":app:assembleRelease"
        command = [
            str(gradle), "--no-daemon", "-q", "-p", str(dm_copy / "android"), task,
            f"-PdmGenDir={gen}", f"-PdmPython={common['python']}", f"-PdmAppId={common['app_id']}",
            f"-PdmAppLabel={common['label']}", f"-PdmVersionName={requirements['version_name']}",
            f"-PdmVersionCode={requirements['version_code']}",
        ]
        log("running Gradle externalNativeBuild")
        run_command(command, cwd=dm_copy / "android", env=env, label="Gradle Android build", capture_on_failure=True)
        artifact = select_android_artifact(dm_copy / "android", args.build_mode)
        build_details = plan["build"] | {"sdk_resolved": sdk_details}
    else:
        deveco_root = normalize_deveco_root(common["deveco"]) if common["deveco"] else discover_deveco(None)
        tools = discover_deveco_tools(deveco_root)
        sdk = discover_harmony_sdk(common["sdk"], deveco_root)
        sdk_info = read_harmony_sdk(sdk)
        app = dm_copy / "harmony/app"
        prepare_ohos_tree(
            app,
            app_id=common["app_id"],
            label=common["label"],
            sdk_label=sdk_info["label"],
            model_version=sdk_info["model_version"],
            gen_dir=gen,
            python=common["python"],
            build_profile=common["build_profile"],
            tools=tools,
        )
        env = hvigor_environment(tools, sdk, common["java_home"])
        log("installing Harmony package dependencies")
        run_command([tools["ohpm"], "install"], cwd=app, env=env, label="ohpm install", capture_on_failure=True)
        log("running Hvigor assembleHap")
        run_command(
            [tools["hvigor"], "--mode", "module", "-p", "module=entry@default", "-p", "product=default", "-p", f"buildMode={args.build_mode}", "assembleHap"],
            cwd=app,
            env=env,
            label="Hvigor assembleHap",
            capture_on_failure=True,
        )
        artifact = select_harmony_artifact(app, bool(common["build_profile"]))
        build_details = plan["build"]

    manifest = make_manifest(
        target=args.target,
        app_id=common["app_id"],
        label=common["label"],
        product=common["product"],
        artifact=artifact,
        source=source,
        generated=generated,
        build=build_details,
        has_profile=bool(common["build_profile"]),
        derived=derived,
    )
    atomic_json(build_root / "build-manifest.json", manifest)
    return manifest


def emit(value: Any, as_json: bool) -> None:
    if as_json:
        json.dump(value, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        if args.dry_run:
            emit(make_build_plan(args), args.json)
            return 0
        manifest = execute_build(args)
        emit(manifest, args.json)
        return 0
    except BuildError as exc:
        if args.json:
            emit({"ok": False, "error": str(exc)}, True)
        else:
            print(f"[mobile-build] error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
