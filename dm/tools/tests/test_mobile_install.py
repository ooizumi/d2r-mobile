from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1]
MODULE_PATH = TOOLS / "mobile_install.py"
SPEC = importlib.util.spec_from_file_location("mobile_install_under_test", MODULE_PATH)
assert SPEC and SPEC.loader
tool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tool)
TEST_TMP_ROOT = Path(tempfile.gettempdir())


def touch(path: Path, body: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class TempWorkspace:
    def __enter__(self) -> Path:
        self._temp = tempfile.TemporaryDirectory(prefix="mobile-install-test-", dir=TEST_TMP_ROOT)
        return Path(self._temp.name)

    def __exit__(self, *exc: object) -> None:
        self._temp.cleanup()


class FakeWorker:
    def __init__(self, responses: dict[str, tuple[int, dict[str, object]]]):
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        assert kwargs.get("shell") is False
        worker = Path(argv[1]).name
        code, body = self.responses[worker]
        return subprocess.CompletedProcess(argv, code, json.dumps(body), "")


class ConfigTests(unittest.TestCase):
    def test_relative_paths_are_config_relative_and_secret_values_stay_private(self) -> None:
        with TempWorkspace() as root:
            config_path = root / "configs/mobile.json"
            value = {
                "target": "ohos",
                "workspace": "../work",
                "game_dir": "../Update",
                "resource": {"warm_seed": "../seed.bin"},
                "build": {"deveco": "/Applications/DevEco Studio.app"},
                "device": {"serial": "WEB-W00", "remote_dir": "/storage/Download/D2R"},
            }
            touch(config_path, json.dumps(value))
            loaded, path = tool.load_config(config_path, required=True)
            normalized = tool.normalize_config(loaded, path)
            self.assertEqual(normalized["workspace"], root / "work")
            self.assertEqual(normalized["game_dir"], root / "Update")
            self.assertEqual(normalized["resource"]["warm_seed"], root / "seed.bin")
            redacted = tool.redacted(["--keys", "/private/prod.keys", "--build-profile", "/private/sign.json5"])
            self.assertEqual(redacted, ["--keys", "<private>", "--build-profile", "<private>"])

    def test_workspace_inside_source_is_rejected(self) -> None:
        with self.assertRaisesRegex(tool.OrchestratorError, "outside"):
            tool.normalize_config({"target": "ohos", "workspace": str(TOOLS.parent)}, None)

    def test_android_rejects_harmony_debug_import_mode(self) -> None:
        with self.assertRaisesRegex(tool.OrchestratorError, "HarmonyOS"):
            tool.normalize_config({"target": "android", "workspace": str(TEST_TMP_ROOT / "work"), "device": {"import_mode": "debug-sandbox"}}, None)


class DryRunTests(unittest.TestCase):
    def test_run_dry_run_plans_all_three_phases_without_process_or_write(self) -> None:
        with TempWorkspace() as root:
            workspace = root / "workspace"
            config = {
                "target": "ohos",
                "workspace": str(workspace),
                "game_dir": str(root / "Update"),
                "device": {"serial": "WEB-W00", "remote_dir": "/storage/Download/D2R"},
            }
            config_path = root / "mobile.json"
            touch(config_path, json.dumps(config))
            stdout = io.StringIO()
            with mock.patch.object(tool.subprocess, "run", side_effect=AssertionError("process started")), contextlib.redirect_stdout(stdout):
                code = tool.main(["run", "--config", str(config_path), "--dry-run", "--json"])
            report = json.loads(stdout.getvalue())
            self.assertEqual(code, 0)
            self.assertFalse(workspace.exists())
            self.assertEqual(report["status"], "planned")
            self.assertEqual([row["name"] for row in report["phases"]], ["prepare", "build", "install"])
            self.assertEqual(report["phases"][-1]["status"], "planned")
            self.assertFalse(report["installed"])
            self.assertNotIn("result", report["phases"][0])


class WorkflowTests(unittest.TestCase):
    def standard_config(self, root: Path, target: str = "ohos") -> tuple[Path, dict[str, object]]:
        artifact = root / "own outputs/entry-default-signed.hap" if target == "ohos" else root / "own outputs/app-debug.apk"
        touch(artifact, "artifact")
        resources = root / "prepared resources"
        (resources / ("dm" if target == "ohos" else "android")).mkdir(parents=True)
        (resources / "data").mkdir()
        config = {
            "target": target,
            "workspace": str(root / "workspace"),
            "game_dir": str(root / "Update"),
            "build": {"build_mode": "release"},
            "device": {
                "serial": "WEB-W00" if target == "ohos" else "phone",
                "remote_dir": "/storage/Download/D2R" if target == "ohos" else "Download/D2R",
                "launch": False,
            },
            "install": {"artifact": str(artifact), "resources": str(resources)},
        }
        path = root / "mobile.json"
        touch(path, json.dumps(config))
        return path, config

    def invoke(self, args: list[str], fake: FakeWorker) -> tuple[int, dict[str, object], FakeWorker]:
        stdout = io.StringIO()
        with mock.patch.object(tool.subprocess, "run", side_effect=fake), contextlib.redirect_stdout(stdout):
            code = tool.main(args)
        return code, json.loads(stdout.getvalue()), fake

    def prepare_result(self, root: Path, target: str = "ohos") -> dict[str, object]:
        game = root / "extracted Update"
        game.mkdir()
        resources = root / "prepared resources"
        resources.mkdir(exist_ok=True)
        return {
            "status": "complete",
            "target": target,
            "game_dir": str(game),
            "mobile_device_resources": str(resources),
            "import_staging": str(resources),
        }

    def test_prepare_uses_worker_and_exposes_game_path(self) -> None:
        with TempWorkspace() as root:
            config_path, _ = self.standard_config(root)
            fake = FakeWorker({"mobile_resources.py": (0, self.prepare_result(root))})
            code, report, fake = self.invoke(["prepare", "--config", str(config_path), "--json"], fake)
            self.assertEqual(code, 0)
            self.assertEqual(report["status"], "completed")
            self.assertEqual(report["game_dir"], str(root / "extracted Update"))
            self.assertEqual(Path(fake.calls[0][1]).name, "mobile_resources.py")
            self.assertNotIn("shell", fake.calls[0][1])

    def test_run_stops_awaiting_signing_and_never_calls_device_install(self) -> None:
        with TempWorkspace() as root:
            config_path, _ = self.standard_config(root)
            prepared = self.prepare_result(root)
            unsigned = root / "workspace/build/tree/dm/harmony/app/entry/build/default/outputs/default/entry-default-unsigned.hap"
            touch(unsigned, "unsigned")
            build = {
                "status": "completed",
                "artifact": {"path": str(unsigned), "signed": False, "signature_status": "unsigned"},
            }
            fake = FakeWorker({"mobile_resources.py": (0, prepared), "mobile_build.py": (0, build)})
            code, report, fake = self.invoke(["run", "--config", str(config_path), "--json"], fake)
            self.assertEqual(code, 0)
            self.assertEqual(report["status"], "awaiting_signing")
            self.assertFalse(report["installed"])
            workers = [Path(call[1]).name for call in fake.calls]
            self.assertEqual(workers, ["mobile_resources.py", "mobile_build.py"])

    def test_explicit_serial_is_required_for_install(self) -> None:
        with TempWorkspace() as root:
            config_path, config = self.standard_config(root)
            del config["device"]["serial"]
            touch(config_path, json.dumps(config))
            code, report, _ = self.invoke(["install", "--config", str(config_path), "--json"], FakeWorker({}))
            self.assertEqual(code, 2)
            self.assertEqual(report["error"]["code"], "serial_required")

    def test_launch_is_opt_in_and_install_passes_explicit_inputs(self) -> None:
        with TempWorkspace() as root:
            config_path, config = self.standard_config(root)
            artifact = Path(config["install"]["artifact"])
            resources = Path(config["install"]["resources"])
            device = {
                "status": "ok",
                "steps": [{"name": "install_artifact", "status": "completed", "verified": True}],
            }
            fake = FakeWorker({"mobile_device.py": (0, device)})
            code, report, fake = self.invoke(["install", "--config", str(config_path), "--json"], fake)
            self.assertEqual(code, 0)
            self.assertTrue(report["installed"])
            argv = fake.calls[0]
            self.assertIn(str(artifact), argv)
            self.assertIn(str(resources), argv)
            self.assertNotIn("--launch", argv)

    def test_custom_app_id_is_passed_as_device_package(self) -> None:
        with TempWorkspace() as root:
            config_path, config = self.standard_config(root)
            config["build"]["app_id"] = "com.example.custom"
            config["device"]["launch"] = True
            touch(config_path, json.dumps(config))
            device = {"status": "ok", "steps": [{"name": "install_artifact", "status": "completed", "verified": True}]}
            fake = FakeWorker({"mobile_device.py": (0, device)})
            code, _, fake = self.invoke(["install", "--config", str(config_path), "--json"], fake)
            self.assertEqual(code, 0)
            argv = fake.calls[0]
            self.assertEqual(argv[argv.index("--package") + 1], "com.example.custom")
            self.assertIn("--launch", argv)

    def test_debug_sandbox_install_only_installs_artifact_and_reports_pending_import(self) -> None:
        with TempWorkspace() as root:
            config_path, config = self.standard_config(root)
            config["device"].pop("remote_dir")
            config["device"]["import_mode"] = "debug-sandbox"
            touch(config_path, json.dumps(config))
            fake = FakeWorker({"mobile_device.py": (0, {"status": "ok", "installed": True})})
            code, report, fake = self.invoke(["install", "--config", str(config_path), "--json"], fake)
            self.assertEqual(code, 0)
            self.assertEqual(report["status"], "awaiting_debug_import")
            self.assertTrue(report["installed"])
            self.assertNotIn("--resources", fake.calls[0])
            self.assertNotIn("--launch", fake.calls[0])

    def test_import_resources_uses_explicit_helper_and_does_not_claim_gameplay_success(self) -> None:
        with TempWorkspace() as root:
            config_path, config = self.standard_config(root)
            config["build"]["app_id"] = "com.example.custom"
            touch(config_path, json.dumps(config))
            fake = FakeWorker({"mobile_ohos_import.py": (0, {"status": "awaiting_app_import"})})
            with mock.patch.object(tool, "worker_exists", return_value=True):
                code, report, fake = self.invoke(["import-resources", "--config", str(config_path), "--json"], fake)
            self.assertEqual(code, 0)
            self.assertEqual(report["status"], "awaiting_app_import")
            self.assertEqual(fake.calls[0][fake.calls[0].index("--serial") + 1], "WEB-W00")
            self.assertEqual(fake.calls[0][fake.calls[0].index("--package") + 1], "com.example.custom")

    def test_awaiting_import_marks_verified_artifact_as_installed(self) -> None:
        with TempWorkspace() as root:
            config_path, _ = self.standard_config(root)
            device = {
                "status": "awaiting_import",
                "steps": [{"name": "install_artifact", "status": "completed", "verified": True}],
            }
            fake = FakeWorker({"mobile_device.py": (0, device)})
            code, report, _ = self.invoke(["install", "--config", str(config_path), "--json"], fake)
            self.assertEqual(code, 0)
            self.assertEqual(report["status"], "awaiting_import")
            self.assertTrue(report["installed"])

    def test_transfer_failure_after_verified_install_keeps_installed_true(self) -> None:
        with TempWorkspace() as root:
            config_path, _ = self.standard_config(root)
            device = {
                "status": "failed",
                "error": {"code": "transfer_failed", "message": "resource copy failed"},
                "steps": [{"name": "install_artifact", "status": "completed", "verified": True}],
            }
            fake = FakeWorker({"mobile_device.py": (1, device)})
            code, report, _ = self.invoke(["install", "--config", str(config_path), "--json"], fake)
            self.assertEqual(code, 2)
            self.assertEqual(report["status"], "failed")
            self.assertTrue(report["installed"])
            self.assertEqual(report["phases"][-1]["error"]["code"], "transfer_failed")

    def test_worker_installed_flag_is_authoritative(self) -> None:
        with TempWorkspace() as root:
            config_path, _ = self.standard_config(root)
            device = {"status": "awaiting_import", "installed": True, "steps": []}
            fake = FakeWorker({"mobile_device.py": (0, device)})
            code, report, _ = self.invoke(["install", "--config", str(config_path), "--json"], fake)
            self.assertEqual(code, 0)
            self.assertTrue(report["installed"])

    def test_phase_error_fails_closed(self) -> None:
        with TempWorkspace() as root:
            config_path, _ = self.standard_config(root)
            failed = {"status": "failed", "error": {"code": "bad_rom", "message": "bad owned ROM"}}
            fake = FakeWorker({"mobile_resources.py": (2, failed)})
            code, report, fake = self.invoke(["run", "--config", str(config_path), "--json"], fake)
            self.assertEqual(code, 2)
            self.assertEqual(report["error"]["code"], "prepare_failed")
            self.assertEqual(len(fake.calls), 1)
            self.assertFalse(report["installed"])


class DoctorTests(unittest.TestCase):
    def test_doctor_detects_modules_and_has_no_mutation(self) -> None:
        with TempWorkspace() as root:
            config_path = root / "config.json"
            touch(config_path, json.dumps({"target": "ohos", "workspace": str(root / "work")}))
            fake_build = SimpleNamespace(
                normalize_deveco_root=lambda path: path,
                discover_deveco=lambda path: root / "DevEco.app/Contents",
                discover_deveco_tools=lambda path: {
                    "root": path,
                    "node": path / "tools/node/bin/node",
                    "java": path / "jbr/Contents/Home/bin/java",
                    "ohpm": path / "tools/ohpm/bin/ohpm",
                    "hvigor": path / "tools/hvigor/bin/hvigorw",
                },
                discover_harmony_sdk=lambda explicit, deveco: root / "sdk/default",
                read_harmony_sdk=lambda sdk: {"api": 24, "platform": "6.1.1"},
            )
            stdout = io.StringIO()
            with mock.patch.object(tool, "executable_check", return_value={"name": "x", "status": "ok", "path": "x"}), \
                 mock.patch.object(tool, "load_build_worker", return_value=fake_build), \
                 mock.patch.object(tool, "discover_bridge", return_value=root / "hdc"), \
                 contextlib.redirect_stdout(stdout):
                code = tool.main(["doctor", "--config", str(config_path), "--json"])
            report = json.loads(stdout.getvalue())
            self.assertIn(code, (0, 1))
            checks = {item["name"]: item["status"] for item in report["checks"]}
            self.assertEqual(checks["module:mobile_build.py"], "ok")
            self.assertEqual(checks["module:mobile_resources.py"], "ok")
            self.assertEqual(checks["deveco:node"], "ok")
            self.assertIn("python:lz4", checks)
            self.assertFalse((root / "work").exists())


class ExportSourceTests(unittest.TestCase):
    def test_export_contains_dirty_source_and_excludes_game_keys_outputs_and_generated_files(self) -> None:
        with TempWorkspace() as root:
            source = root / "source"
            source.mkdir()
            (source / ".gitignore").write_text("ignored.bin\n*.nca\n*.p7b\n*.data\n", encoding="utf-8")
            touch(source / "dm/tools/worker.py", "print('dirty')\n")
            touch(source / "dm/tools/new.py", "print('untracked')\n")
            touch(source / "README.md", "editable source export\n")
            touch(source / "dm/harmony/app/entry/build-profile.json5", "{ \"targets\": [] }\n")
            touch(source / "ignored.bin", "ignored")
            touch(source / "game.nsp", "rom")
            touch(source / "prod.keys", "key")
            touch(source / "dm/ios/Gen/generated.bin", "generated")
            touch(source / "dm/host/dm_vault_tab.h", "generated table")
            touch(source / "dm/android/app/build/outputs/apk/debug/app-debug.apk", "apk")
            private_paths = (
                "game-update.nca", "ticket.tik", "resource.data", "shader.xgs", "main.patched.bin",
                "profile.p7b", "certificate.cer", "local.cmake", "local.env",
                "cache/private.bin", "work/private.bin", "data/private.bin",
                "switch/private.bin", "S2X/private.bin", "dm/cache/private.bin", "dm/harmony/app/build-profile.json5",
            )
            for rel in private_paths:
                touch(source / rel, "private")
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            subprocess.run([
                "git", "-C", str(source), "add", ".gitignore", "README.md", "dm/tools/worker.py",
                "dm/harmony/app/entry/build-profile.json5",
            ], check=True)
            for rel in ("game-update.nca", "ticket.tik", "resource.data", "shader.xgs", "main.patched.bin",
                        "profile.p7b", "certificate.cer", "local.cmake", "local.env",
                        "cache/private.bin", "work/private.bin", "data/private.bin",
                        "switch/private.bin", "S2X/private.bin", "dm/cache/private.bin", "dm/harmony/app/build-profile.json5"):
                subprocess.run(["git", "-C", str(source), "add", "-f", rel], check=True)
            touch(source / "dm/tools/worker.py", "print('modified working tree')\n")
            output = root / "D2R source.zip"
            config = {
                "target": "ohos",
                "workspace": str(root / "workspace"),
                "roms": [str(source / "game.nsp")],
                "keys": str(source / "prod.keys"),
                "export": {"output": str(output)},
            }
            config_path = root / "config.json"
            touch(config_path, json.dumps(config))
            old_source = tool.SOURCE_ROOT
            try:
                tool.SOURCE_ROOT = source
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = tool.main(["export-source", "--config", str(config_path), "--json"])
            finally:
                tool.SOURCE_ROOT = old_source
            self.assertEqual(code, 0)
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                self.assertIn("dm/tools/worker.py", names)
                self.assertIn("dm/tools/new.py", names)
                self.assertIn("dm/harmony/app/entry/build-profile.json5", names)
                self.assertIn(tool.SOURCE_MANIFEST, names)
                self.assertIn(tool.DM_SOURCE_MANIFEST, names)
                self.assertNotIn("game.nsp", names)
                self.assertNotIn("prod.keys", names)
                self.assertNotIn("dm/ios/Gen/generated.bin", names)
                self.assertNotIn("dm/host/dm_vault_tab.h", names)
                self.assertNotIn("dm/android/app/build/outputs/apk/debug/app-debug.apk", names)
                for rel in private_paths:
                    self.assertNotIn(rel, names)
                self.assertEqual(archive.read("dm/tools/worker.py"), b"print('modified working tree')\n")
                manifest = archive.read(tool.SOURCE_MANIFEST).decode().splitlines()
                self.assertIn("dm/tools/worker.py", manifest)
                self.assertIn("README.md", manifest)
                self.assertNotIn(tool.SOURCE_MANIFEST, manifest)
                dm_manifest = archive.read(tool.DM_SOURCE_MANIFEST).decode().splitlines()
                self.assertIn("dm/tools/worker.py", dm_manifest)
                self.assertNotIn("README.md", dm_manifest)
                self.assertNotIn(tool.DM_SOURCE_MANIFEST, dm_manifest)

            extracted = root / "extracted source"
            with zipfile.ZipFile(output) as archive:
                archive.extractall(extracted)
            build_spec = importlib.util.spec_from_file_location("export_build_worker", TOOLS / "mobile_build.py")
            assert build_spec and build_spec.loader
            build_worker = importlib.util.module_from_spec(build_spec)
            sys.modules[build_spec.name] = build_worker
            build_spec.loader.exec_module(build_worker)
            snapshot = build_worker.copy_source_snapshot(extracted, extracted / "snapshot")
            self.assertGreater(snapshot["files"], 0)
            self.assertTrue((extracted / "snapshot/dm/tools/worker.py").is_file())
            self.assertTrue((extracted / "snapshot/dm/harmony/app/entry/build-profile.json5").is_file())

            resources_spec = importlib.util.spec_from_file_location("export_resources_worker", TOOLS / "mobile_resources.py")
            assert resources_spec and resources_spec.loader
            resources_worker = importlib.util.module_from_spec(resources_spec)
            sys.modules[resources_spec.name] = resources_worker
            resources_spec.loader.exec_module(resources_worker)
            resource_entries, fingerprint = resources_worker._source_inventory(extracted / "dm", extracted)
            self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")
            self.assertIn("tools/worker.py", {entry.relative.as_posix() for entry in resource_entries})

    def test_build_mode_is_passed_to_worker(self) -> None:
        with TempWorkspace() as root:
            game = root / "Update"
            game.mkdir()
            config = {
                "target": "ohos",
                "workspace": str(root / "workspace"),
                "game_dir": str(game),
                "build": {"gen_dir": str(root / "gen")},
            }
            config_path = root / "config.json"
            touch(config_path, json.dumps(config))
            build = {
                "status": "completed",
                "artifact": {"path": str(root / "entry-default-unsigned.hap"), "signed": False},
            }
            fake = FakeWorker({"mobile_build.py": (0, build)})
            stdout = io.StringIO()
            with mock.patch.object(tool.subprocess, "run", side_effect=fake), contextlib.redirect_stdout(stdout):
                code = tool.main(["build", "--config", str(config_path), "--json"])
            self.assertEqual(code, 0)
            argv = fake.calls[0]
            index = argv.index("--build-mode")
            self.assertEqual(argv[index + 1], "debug")


if __name__ == "__main__":
    unittest.main()
