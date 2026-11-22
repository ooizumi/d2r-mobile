#!/usr/bin/env python3
"""Synthetic tests for the mobile install adapter; subprocess is always mocked."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import posixpath
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import mobile_device  # noqa: E402


TEST_TMP_ROOT = Path(tempfile.gettempdir())


class MockAdb:
    def __init__(
        self,
        devices_output="phone\tdevice\n",
        install_output="Performing Streamed Install\nSuccess\n",
        old_imports=True,
        resource_root=None,
        corrupt_paths=None,
        omit_paths=None,
        tar_available=True,
        fail_mkdir_paths=None,
        fail_mkdir_contains=None,
        fail_mv_destinations=None,
        fail_mv_once=None,
        launch_output="started\n",
    ):
        self.devices_output = devices_output
        self.install_output = install_output
        self.old_imports = old_imports
        self.resource_root = resource_root
        self.corrupt_paths = set(corrupt_paths or ())
        self.omit_paths = set(omit_paths or ())
        self.tar_available = tar_available
        self.fail_mkdir_paths = set(fail_mkdir_paths or ())
        self.fail_mkdir_contains = set(fail_mkdir_contains or ())
        self.fail_mv_destinations = set(fail_mv_destinations or ())
        self.fail_mv_once = set(fail_mv_once or ())
        self.launch_output = launch_output
        self.created_paths = set()
        self.removed_paths = set()
        self.calls = []

    def _resource_path(self, remote_path):
        for marker in ("/android/", "/dm/", "/data/"):
            offset = remote_path.find(marker)
            if offset >= 0:
                return remote_path[offset + 1 :]
        raise AssertionError("mocked hash request is outside dm/ or data/: %s" % remote_path)

    def __call__(self, argv, **kwargs):
        argv = [str(item) for item in argv]
        self.calls.append((argv, kwargs))
        if "devices" in argv or argv[1:3] == ["list", "targets"]:
            return subprocess.CompletedProcess(argv, 0, self.devices_output)
        if "install" in argv:
            return subprocess.CompletedProcess(argv, 0, self.install_output)
        if "push" in argv:
            return subprocess.CompletedProcess(argv, 0, "1 file pushed\n")
        if "file" in argv and "send" in argv:
            return subprocess.CompletedProcess(argv, 0, "FileTransfer finish\n")
        if "shell" in argv:
            script = argv[-1]
            if "[ -x /bin/sha256sum ]" in script:
                return subprocess.CompletedProcess(argv, 0, "/bin/sha256sum\n")
            if "command -v sha256sum" in script:
                return subprocess.CompletedProcess(argv, 0, "sha256sum\n")
            if "[ -x /bin/tar ]" in script:
                return subprocess.CompletedProcess(argv, 0, "/bin/tar\n" if self.tar_available else "none\n")
            if script.startswith("/bin/sha256sum ") or script.startswith("sha256sum "):
                paths = shlex.split(script)[1:]
                lines = []
                for remote_path in paths:
                    rel = self._resource_path(remote_path)
                    if rel in self.omit_paths:
                        continue
                    local = Path(self.resource_root) / rel
                    body = b"corrupt" if rel in self.corrupt_paths else local.read_bytes()
                    lines.append("%s  %s" % (hashlib.sha256(body).hexdigest(), remote_path))
                return subprocess.CompletedProcess(argv, 0, "\n".join(lines) + ("\n" if lines else ""))
            if script.startswith("/bin/tar -xf "):
                return subprocess.CompletedProcess(argv, 0, "extracted\n")
            if script.startswith("if rm -f "):
                return subprocess.CompletedProcess(argv, 0, "D2R_RM_OK\n")
            if script.startswith("if mkdir -p "):
                tokens = shlex.split(script)
                path = tokens[3]
                should_fail = path in self.fail_mkdir_paths or any(marker in path for marker in self.fail_mkdir_contains)
                output = "D2R_MKDIR_FAILED\n" if should_fail else "D2R_MKDIR_OK\n"
                return subprocess.CompletedProcess(argv, 0, output)
            if script.startswith("if [ ! -e "):
                tokens = shlex.split(script)
                mv_index = tokens.index("mv")
                source = tokens[mv_index + 1]
                destination = tokens[mv_index + 2]
                should_fail = destination in self.fail_mv_destinations or destination in self.fail_mv_once
                self.fail_mv_once.discard(destination)
                output = "D2R_MV_FAILED\n" if should_fail else "D2R_MV_OK\n"
                if not should_fail:
                    self.removed_paths.add(source)
                    self.created_paths.add(destination)
                return subprocess.CompletedProcess(argv, 0, output)
            if "if [ -e " in script:
                checked_tokens = shlex.split(script)
                checked = checked_tokens[checked_tokens.index("-e") + 1]
                if checked in self.removed_paths:
                    return subprocess.CompletedProcess(argv, 0, "no\n")
                if checked in self.created_paths:
                    return subprocess.CompletedProcess(argv, 0, "yes\n")
                if ".d2r-import-stage-" in script:
                    return subprocess.CompletedProcess(argv, 0, "yes\n")
                if ".d2r-backup-" in script:
                    return subprocess.CompletedProcess(argv, 0, "no\n")
                return subprocess.CompletedProcess(argv, 0, "yes\n" if self.old_imports else "no\n")
            if "monkey" in script or "aa start" in script:
                return subprocess.CompletedProcess(argv, 0, self.launch_output)
            return subprocess.CompletedProcess(argv, 0, "")
        raise AssertionError("unexpected mocked command: %r" % (argv,))


class MobileDeviceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mobile_device_test_", dir=str(TEST_TMP_ROOT))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = self.root / "toolchains" / "fake adb"
        self.bridge.parent.mkdir()
        self.bridge.write_text("mock\n", encoding="utf-8")
        self.apk = self.root / "own build files" / "D2R Mobile APK.apk"
        self.apk.parent.mkdir()
        self.apk.write_bytes(b"synthetic apk")
        self.hap = self.root / "own build files" / "D2R Mobile HAP.hap"
        self.hap.write_bytes(b"synthetic hap")

    def invoke(self, args, runner):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(mobile_device.subprocess, "run", side_effect=runner) as mocked_run, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            code = mobile_device.main(args)
        for call in runner.calls:
            self.assertNotIn("shell", call[1])
        return code, json.loads(stdout.getvalue()), stderr.getvalue(), mocked_run

    def install_args(self, target="android", serial="phone", extra=None):
        artifact = self.apk if target == "android" else self.hap
        args = [
            "install",
            "--target",
            target,
            "--serial",
            serial,
            "--artifact",
            str(artifact),
            "--bridge",
            str(self.bridge),
            "--json",
        ]
        return args + list(extra or [])

    def make_resources(self, name, target="android", files=None):
        resources = self.root / name
        layout = ("android", "data") if target == "android" else ("dm", "data")
        for component in layout:
            (resources / component).mkdir(parents=True, exist_ok=True)
        pack_dir = layout[0]
        (resources / pack_dir / "pack_info.txt").write_text(
            "PRODUCT=synthetic-product\nTARGET=%s\n" % target,
            encoding="utf-8",
        )
        for rel, body in (files or {}).items():
            path = resources / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        return resources

    def test_wrong_device_never_installs(self):
        runner = MockAdb(devices_output="tablet\tdevice\n")
        code, report, _, _ = self.invoke(self.install_args(), runner)
        self.assertEqual(code, 1)
        self.assertEqual(report["error"]["code"], "serial_not_found")
        self.assertFalse(report["installed"])
        self.assertFalse(any("install" in call[0] for call in runner.calls))
        self.assertFalse(any("push" in call[0] for call in runner.calls))

    def test_adb_exit_zero_without_literal_success_is_a_failure(self):
        runner = MockAdb(install_output="Failure [INSTALL_FAILED_UPDATE_INCOMPATIBLE]\n")
        code, report, _, _ = self.invoke(self.install_args(), runner)
        self.assertEqual(code, 1)
        self.assertEqual(report["error"]["code"], "install_not_verified")
        self.assertFalse(report["installed"])
        install_steps = [step for step in report["steps"] if step["name"] == "install_artifact"]
        self.assertFalse(install_steps[0]["verified"])
        self.assertFalse(any("push" in call[0] for call in runner.calls))

    def test_hdc_exit_zero_without_bundle_success_is_a_failure(self):
        runner = MockAdb(devices_output="harmony-tablet\n", install_output="install failed\n")
        args = self.install_args("ohos", serial="harmony-tablet")
        code, report, _, _ = self.invoke(args, runner)
        self.assertEqual(code, 1)
        self.assertEqual(report["error"]["code"], "install_not_verified")
        self.assertFalse(report["installed"])
        self.assertFalse(any("file" in call[0] for call in runner.calls))

    def test_unauthorized_offline_and_multiple_are_distinct(self):
        unauthorized = [mobile_device.DeviceRecord("a", "unauthorized")]
        offline = [mobile_device.DeviceRecord("a", "offline")]
        multiple = [mobile_device.DeviceRecord("a", "device"), mobile_device.DeviceRecord("b", "unauthorized")]
        self.assertEqual(mobile_device.classify_devices(unauthorized)[0], "unauthorized")
        self.assertEqual(mobile_device.classify_devices(offline)[0], "offline")
        state, issues = mobile_device.classify_devices(multiple)
        self.assertEqual(state, "multiple_devices")
        self.assertIn("unauthorized", issues)
        self.assertEqual(mobile_device.parse_devices("[Empty]\n", "ohos"), [])

    def test_android_remote_paths_are_quoted_staged_and_backed_up_without_destructive_commands(self):
        resources = self.make_resources(
            "complete import",
            files={
                "android/fixture.bin": b"android",
                "data/fixture.bin": b"data",
            },
        )
        remote = "/sdcard/Download/S2X/D2R Mobile; touch should-not-run '$HOME' 'quoted'"
        runner = MockAdb(old_imports=True, resource_root=resources)
        args = self.install_args(extra=["--resources", str(resources), "--remote-dir", remote])
        code, report, _, _ = self.invoke(args, runner)

        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "awaiting_import")
        self.assertTrue(report["installed"])
        self.assertEqual(report["transfer"]["import_state"], "awaiting_manual_app_import")
        self.assertTrue(report["transfer"]["old_import_backups_retained"])
        self.assertEqual(report["transfer"]["verification"]["verified_files"], 3)

        pushes = [call[0] for call in runner.calls if "push" in call[0]]
        self.assertEqual(len(pushes), 2)
        stage = report["transfer"]["stage"]
        self.assertEqual(posixpath.dirname(stage), posixpath.dirname(remote))
        for argv in pushes:
            self.assertTrue(argv[-1].startswith(stage + "/"))
            self.assertTrue(argv[-2].endswith(os.sep + "."))

        remote_scripts = [call[0][-1] for call in runner.calls if "shell" in call[0]]
        remote_script_tokens = [token for script in remote_scripts for token in shlex.split(script)]
        self.assertIn(remote, remote_script_tokens)
        self.assertIn(stage, remote_script_tokens)
        self.assertTrue(any("[ -e " in script and ".d2r-backup-" in script for script in remote_scripts))

        flat_tokens = []
        for argv, _ in runner.calls:
            flat_tokens.extend(argv)
            if "shell" in argv:
                flat_tokens.extend(shlex.split(argv[-1]))
        for forbidden in ("uninstall", "clear", "force-stop", "rm"):
            self.assertNotIn(forbidden, flat_tokens)

    def test_android_default_remote_is_an_absolute_sdcard_path(self):
        resources = self.make_resources(
            "default android remote",
            files={"android/pack_info.txt": b"PRODUCT=synthetic-product\nTARGET=android\n"},
        )
        runner = MockAdb(resource_root=resources)
        args = self.install_args(extra=["--resources", str(resources)])
        code, report, _, _ = self.invoke(args, runner)
        self.assertEqual(code, 0)
        self.assertEqual(report["transfer"]["remote_dir"], "/sdcard/Download/S2X/D2R Mobile")
        self.assertTrue(report["transfer"]["stage"].startswith("/sdcard/Download/S2X/."))
        for argv, _ in runner.calls:
            if "push" in argv:
                self.assertTrue(argv[-1].startswith("/sdcard/Download/S2X/.D2R Mobile.d2r-import-stage-"))

    def test_android_relative_remote_is_explicitly_rooted_under_sdcard(self):
        resources = self.make_resources("relative android remote")
        runner = MockAdb()
        args = self.install_args(
            extra=["--resources", str(resources), "--remote-dir", "Download/Chosen Import", "--dry-run"]
        )
        code, report, _, _ = self.invoke(args, runner)
        self.assertEqual(code, 0)
        self.assertEqual(report["steps"][1]["plan"]["remote_dir"], "/sdcard/Download/Chosen Import")
        self.assertEqual(runner.calls, [])

    def test_hdc_uses_non_destructive_file_copy_and_reports_awaiting_import(self):
        resources = self.make_resources(
            "harmony import",
            target="ohos",
            files={
                "dm/out/fixture.bin": b"dm",
                "data/build_manifest.psv": b"data",
            },
        )
        remote = "/storage/Users/currentUser/Download/D2R Mobile"
        runner = MockAdb(
            devices_output="harmony-tablet\n",
            install_output="install bundle successfully\n",
            resource_root=resources,
        )
        args = self.install_args(
            "ohos",
            serial="harmony-tablet",
            extra=["--resources", str(resources), "--remote-dir", remote],
        )
        code, report, _, _ = self.invoke(args, runner)

        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "awaiting_import")
        self.assertTrue(report["installed"])
        self.assertEqual(report["transfer"]["import_state"], "awaiting_manual_app_import")
        self.assertIn("not verified", report["transfer"]["note"])
        self.assertEqual(report["transfer"]["transfer"]["method"], "hdc_tar_archive")
        self.assertEqual(report["transfer"]["verification"]["verified_files"], 3)
        self.assertEqual(report["transfer"]["verification"]["sha256_tool"], "/bin/sha256sum")
        file_sends = [call[0] for call in runner.calls if "file" in call[0] and "send" in call[0]]
        self.assertEqual(len(file_sends), 1)
        self.assertFalse(any("push" in call[0] for call in runner.calls))
        self.assertRegex(file_sends[0][-1], r"^/data/local/tmp/\.d2r-mobile-transfer-[^/]+\.tar$")
        remote_scripts = [call[0][-1] for call in runner.calls if "shell" in call[0]]
        self.assertTrue(any(script.startswith("/bin/tar -xf ") for script in remote_scripts))
        self.assertTrue(any(script.startswith("if mkdir -p ") and "D2R_MKDIR_OK" in script for script in remote_scripts))
        self.assertTrue(any(script.startswith("if [ ! -e ") and "D2R_MV_OK" in script for script in remote_scripts))
        archive_cleanup = [script for script in remote_scripts if script.startswith("if rm -f ")]
        self.assertEqual(len(archive_cleanup), 1)
        self.assertIn("/data/local/tmp/.d2r-mobile-transfer-", archive_cleanup[0])
        self.assertIn(".tar", archive_cleanup[0])

    def test_hdc_tar_fallback_uses_explicit_directory_sends_and_still_hashes(self):
        resources = self.make_resources(
            "harmony fallback",
            target="ohos",
            files={"dm/out/a.bin": b"a", "data/b.bin": b"b"},
        )
        remote = "/storage/Users/currentUser/Download/D2R Mobile"
        runner = MockAdb(
            devices_output="harmony-tablet\n",
            install_output="install bundle successfully\n",
            resource_root=resources,
            tar_available=False,
        )
        args = self.install_args(
            "ohos",
            serial="harmony-tablet",
            extra=["--resources", str(resources), "--remote-dir", remote],
        )
        code, report, _, _ = self.invoke(args, runner)
        self.assertEqual(code, 0)
        self.assertEqual(report["transfer"]["transfer"]["method"], "hdc_explicit_directory_send_verify")
        sends = [call[0] for call in runner.calls if "file" in call[0] and "send" in call[0]]
        self.assertEqual(len(sends), 2)
        self.assertFalse(any(argv[-1].endswith(".tar") for argv in sends))
        self.assertEqual(report["transfer"]["verification"]["verified_files"], 3)

    def test_remote_hash_mismatch_or_missing_file_never_publishes(self):
        remote = "/storage/Users/currentUser/Download/D2R Mobile"
        for label, kwargs in (
            ("corrupt", {"corrupt_paths": {"dm/out/a.bin"}}),
            ("missing", {"omit_paths": {"dm/out/a.bin"}}),
        ):
            with self.subTest(label=label):
                resources = self.make_resources(
                    "harmony hash " + label,
                    target="ohos",
                    files={"dm/out/a.bin": b"correct", "data/b.bin": b"data"},
                )
                runner = MockAdb(
                    devices_output="harmony-tablet\n",
                    install_output="install bundle successfully\n",
                    resource_root=resources,
                    old_imports=True,
                    **kwargs,
                )
                args = self.install_args(
                    "ohos",
                    serial="harmony-tablet",
                    extra=["--resources", str(resources), "--remote-dir", remote],
                )
                code, report, _, _ = self.invoke(args, runner)
                self.assertEqual(code, 1)
                self.assertEqual(report["error"]["code"], "transfer_verification_failed")
                self.assertTrue(report["installed"])
                shell_scripts = [call[0][-1] for call in runner.calls if "shell" in call[0]]
                mv_scripts = [shlex.split(script) for script in shell_scripts if script.startswith("if [ ! -e ")]
                self.assertFalse(any(remote in tokens[1:] for tokens in mv_scripts))
                self.assertFalse(any(".d2r-backup-" in script for script in shell_scripts))

    def test_hdc_zero_exit_failed_publish_mv_restores_old_import(self):
        resources = self.make_resources(
            "hdc failed publish mv",
            target="ohos",
            files={"dm/out/a.bin": b"new dm", "data/b.bin": b"new data"},
        )
        remote = "/storage/Users/currentUser/Download/D2R Mobile"
        runner = MockAdb(
            devices_output="harmony-tablet\n",
            install_output="install bundle successfully\n",
            resource_root=resources,
            old_imports=True,
            fail_mv_once={remote},
        )
        args = self.install_args(
            "ohos",
            serial="harmony-tablet",
            extra=["--resources", str(resources), "--remote-dir", remote],
        )
        code, report, _, _ = self.invoke(args, runner)
        self.assertEqual(code, 1)
        self.assertEqual(report["error"]["code"], "remote_rename_failed")
        self.assertTrue(report["installed"])
        shell_scripts = [call[0][-1] for call in runner.calls if "shell" in call[0]]
        mv_scripts = [shlex.split(script) for script in shell_scripts if script.startswith("if [ ! -e ")]
        self.assertEqual(len(mv_scripts), 3)
        destinations = [tokens[tokens.index("mv") + 2] for tokens in mv_scripts]
        self.assertTrue(any(".d2r-backup-" in destination for destination in destinations))
        self.assertEqual(destinations.count(remote), 2)
        self.assertIn(remote, runner.created_paths)

    def test_hdc_zero_exit_failed_mkdir_never_publishes(self):
        resources = self.make_resources(
            "hdc failed mkdir",
            target="ohos",
            files={"dm/out/a.bin": b"dm", "data/b.bin": b"data"},
        )
        remote = "/storage/Users/currentUser/Download/D2R Mobile"
        runner = MockAdb(
            devices_output="harmony-tablet\n",
            install_output="install bundle successfully\n",
            resource_root=resources,
            fail_mkdir_contains={".d2r-import-stage-"},
        )
        args = self.install_args(
            "ohos",
            serial="harmony-tablet",
            extra=["--resources", str(resources), "--remote-dir", remote],
        )
        code, report, _, _ = self.invoke(args, runner)
        self.assertEqual(code, 1)
        self.assertEqual(report["error"]["code"], "remote_mkdir_failed")
        self.assertTrue(report["installed"])
        self.assertFalse(any("file" in call[0] and "send" in call[0] for call in runner.calls))
        self.assertFalse(any("mv" in script for call in runner.calls for script in call[0]))

    def test_resource_pack_target_mismatch_stops_before_install(self):
        resources = self.make_resources("wrong target pack")
        (resources / "android" / "pack_info.txt").write_text(
            "PRODUCT=synthetic-product\nTARGET=ohos\n",
            encoding="utf-8",
        )
        runner = MockAdb()
        args = self.install_args(extra=["--resources", str(resources)])
        code, report, _, _ = self.invoke(args, runner)
        self.assertEqual(code, 1)
        self.assertEqual(report["error"]["code"], "pack_target_mismatch")
        self.assertEqual(runner.calls, [])

    def test_harmony_relative_remote_dir_is_rejected_without_guessing(self):
        resources = self.make_resources("ohos relative remote", target="ohos")
        runner = MockAdb(devices_output="harmony-tablet\n", install_output="install bundle successfully\n")
        args = self.install_args(
            "ohos",
            serial="harmony-tablet",
            extra=["--resources", str(resources), "--remote-dir", "Download/D2R Mobile"],
        )
        code, report, _, _ = self.invoke(args, runner)
        self.assertEqual(code, 1)
        self.assertEqual(report["error"]["code"], "remote_dir_absolute_required")
        self.assertEqual(runner.calls, [])

    def test_harmony_resources_require_an_explicit_external_remote_directory(self):
        resources = self.make_resources("harmony import without remote", target="ohos")
        runner = MockAdb(devices_output="harmony-tablet\n", install_output="install bundle successfully\n")
        args = self.install_args("ohos", serial="harmony-tablet", extra=["--resources", str(resources)])
        code, report, _, _ = self.invoke(args, runner)
        self.assertEqual(code, 1)
        self.assertEqual(report["error"]["code"], "remote_dir_required")
        self.assertEqual(runner.calls, [])

    def test_remote_path_traversal_and_newlines_are_rejected_before_device_calls(self):
        resources = self.make_resources("bad remote import")
        for remote in (
            "Download/../app/files",
            "Download/import\nnext",
            "/",
            "/data/storage/el2/base/files",
            "/sdcard/Android/data/example/files",
        ):
            runner = MockAdb()
            args = self.install_args(extra=["--resources", str(resources), "--remote-dir", remote])
            code, report, _, _ = self.invoke(args, runner)
            self.assertEqual(code, 1)
            self.assertEqual(report["error"]["code"], "invalid_remote_dir")
            self.assertEqual(runner.calls, [])

    def test_launch_is_not_implicit(self):
        runner = MockAdb()
        code, report, _, _ = self.invoke(self.install_args(), runner)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["installed"])
        self.assertFalse(report["launch_requested"])
        self.assertFalse(report["launch_sent"])
        self.assertEqual(report["package"], "com.xiaoboluo.d2r.mate60")
        flat = [" ".join(call[0]) for call in runner.calls]
        self.assertFalse(any("monkey" in command for command in flat))
        self.assertFalse(any("aa start" in command for command in flat))

    def test_hdc_launch_exit_zero_without_success_text_is_reported_unverified(self):
        runner = MockAdb(
            devices_output="harmony-tablet\n",
            install_output="install bundle successfully\n",
            launch_output="command accepted\n",
        )
        args = self.install_args("ohos", serial="harmony-tablet", extra=["--launch"])
        code, report, _, _ = self.invoke(args, runner)
        self.assertEqual(code, 1)
        self.assertEqual(report["error"]["code"], "launch_not_verified")
        self.assertTrue(report["installed"])
        self.assertTrue(report["launch_requested"])
        self.assertTrue(report["launch_sent"])
        self.assertFalse(report["launch_verified"])
        self.assertEqual(report["launch_state"], "sent_unverified")
        launch_step = [step for step in report["steps"] if step["name"] == "launch"][0]
        self.assertEqual(launch_step["status"], "sent_unverified")
        self.assertNotIn("running", json.dumps(report).lower())

    def test_hdc_launch_success_text_marks_verified_without_running_claim(self):
        runner = MockAdb(
            devices_output="harmony-tablet\n",
            install_output="install bundle successfully\n",
            launch_output="[Info] start ability successfully.\n",
        )
        args = self.install_args("ohos", serial="harmony-tablet", extra=["--launch"])
        code, report, _, _ = self.invoke(args, runner)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "ok")
        self.assertTrue(report["installed"])
        self.assertTrue(report["launch_requested"])
        self.assertTrue(report["launch_sent"])
        self.assertTrue(report["launch_verified"])
        self.assertEqual(report["launch_state"], "verified_success")
        self.assertNotIn("running", json.dumps(report).lower())

    def test_dry_run_makes_no_subprocess_calls_or_filesystem_writes(self):
        resources = self.make_resources("dry import")
        before = sorted((str(path.relative_to(self.root)), path.stat().st_size) for path in self.root.rglob("*"))
        runner = MockAdb()
        args = self.install_args(
            extra=["--resources", str(resources), "--remote-dir", "Download/Unused in plan", "--dry-run"]
        )
        code, report, _, mocked_run = self.invoke(args, runner)
        after = sorted((str(path.relative_to(self.root)), path.stat().st_size) for path in self.root.rglob("*"))

        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "dry_run")
        self.assertFalse(report["installed"])
        self.assertEqual(before, after)
        self.assertEqual(runner.calls, [])
        mocked_run.assert_not_called()
        self.assertEqual(report["steps"][1]["plan"]["import_state"], "awaiting_manual_app_import")


if __name__ == "__main__":
    unittest.main()
