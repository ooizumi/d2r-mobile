#!/usr/bin/env python3
"""Synthetic tests for the HarmonyOS debug-sandbox resource handoff."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
import mobile_ohos_import as tool  # noqa: E402


class TempRoot:
    def __enter__(self) -> Path:
        self._temp = tempfile.TemporaryDirectory(prefix="mobile-ohos-import-test-")
        return Path(self._temp.name)

    def __exit__(self, *exc: object) -> None:
        self._temp.cleanup()


class FakeHdc:
    def __init__(self, *, preflight: str = "D2R_RUNTIME_OK\n", running: bool = False) -> None:
        self.preflight = preflight
        self.running = running
        self.calls: list[list[str]] = []
        self.remote_bytes: dict[str, bytes] = {}
        self.hash_override: str | None = None

    @staticmethod
    def completed(argv: list[str], code: int, output: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, code, output)

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        self.assert_shell_false(kwargs)
        if "file" in argv and "send" in argv:
            bundle = argv.index("-b")
            local = Path(argv[bundle + 2])
            remote = argv[bundle + 3]
            self.remote_bytes[remote] = local.read_bytes()
            return self.completed(argv, 0, "send ok\n")
        if "shell" in argv:
            script = argv[-1]
            if script.startswith("pidof "):
                return self.completed(argv, 0, "25344\n") if self.running else self.completed(argv, 1)
            if "/bin/sha256sum" in script and "D2R_HASH_OK" not in script:
                return self.completed(argv, 0, "/bin/sha256sum\n")
            if "D2R_HASH_OK" in script:
                match = re.search(r"(\./data/storage/el2/base/cache/[^ ']+)", script)
                if not match or match.group(1) not in self.remote_bytes:
                    return self.completed(argv, 0, "D2R_HASH_FAILED\n")
                digest = self.hash_override or hashlib.sha256(self.remote_bytes[match.group(1)]).hexdigest()
                return self.completed(argv, 0, "%s  %s\nD2R_HASH_OK\n" % (digest, match.group(1)))
            if "D2R_RUNTIME_OK" in script:
                return self.completed(argv, 0, self.preflight)
        raise AssertionError("unexpected HDC command: %r" % (argv,))

    @staticmethod
    def assert_shell_false(kwargs: dict[str, object]) -> None:
        assert "shell" not in kwargs


class MobileOhosImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mobile-ohos-import-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bridge = self.root / "toolchains" / "hdc"
        self.bridge.parent.mkdir()
        self.bridge.write_text("mock\n", encoding="utf-8")

    def resources(self, *, files: dict[str, bytes] | None = None, extra_manifest: dict[str, object] | None = None) -> Path:
        root = self.root / "complete parent"
        (root / "dm").mkdir(parents=True)
        (root / "data").mkdir()
        bodies = files or {"dm/resource.bin": b"dm bytes", "data/sample.data": b"data bytes"}
        for relative, body in bodies.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        rows = [
            {"path": path.relative_to(root).as_posix(), "size": path.stat().st_size,
             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(path for path in root.rglob("*") if path.is_file())
        ]
        manifest: dict[str, object] = {
            "schema_version": 1, "status": "complete", "target": "ohos",
            "file_count": len(rows), "total_bytes": sum(int(row["size"]) for row in rows),
            "files": rows,
        }
        manifest.update(extra_manifest or {})
        (root / "resource-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return root

    def invoke(self, resources: Path, extra: list[str] | None = None, fake: FakeHdc | None = None) -> tuple[int, dict[str, object], FakeHdc, mock.Mock]:
        runner = fake or FakeHdc()
        argv = [
            "--resources", str(resources), "--serial", "tablet-serial",
            "--bridge", str(self.bridge), "--json",
        ] + list(extra or [])
        stdout = io.StringIO()
        with mock.patch.object(tool.subprocess, "run", side_effect=runner) as mocked, contextlib.redirect_stdout(stdout):
            code = tool.main(argv)
        return code, json.loads(stdout.getvalue()), runner, mocked

    def test_uses_bundle_scoped_hdc_syntax_and_zip_stored_handoff(self) -> None:
        resources = self.resources()
        code, report, runner, _ = self.invoke(resources)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "awaiting_app_import")
        self.assertFalse(report["imported"])
        sends = [call for call in runner.calls if "file" in call and "send" in call]
        self.assertEqual(len(sends), 2)
        for argv in sends:
            self.assertEqual(argv[:8], [str(self.bridge.resolve()), "-t", "tablet-serial", "file", "send", "-b", "com.xiao.boluo", argv[7]])
            self.assertTrue(argv[-1].startswith("./data/storage/el2/base/cache/"))
        preflight = [call for call in runner.calls if "shell" in call and "-b" in call and "D2R_RUNTIME_OK" in call[-1]][0]
        self.assertEqual(preflight[-3:-1], ["sh", "-c"])

        archive_path = next(path for path in runner.remote_bytes if path.endswith(".zip"))
        with zipfile.ZipFile(io.BytesIO(runner.remote_bytes[archive_path])) as archive:
            self.assertEqual(archive.getinfo("dm/resource.bin").compress_type, zipfile.ZIP_STORED)
            self.assertEqual(archive.namelist()[0], "resource-manifest.json")
            self.assertIn("dm/resource.bin", archive.namelist())
            self.assertIn("data/sample.data", archive.namelist())
        request: dict[str, object] = json.loads(runner.remote_bytes["./data/storage/el2/base/cache/d2r-installer-request.json"])
        self.assertEqual(request["schema_version"], 1)
        self.assertRegex(str(request["token"]), r"^[0-9a-f]{32}$")
        self.assertEqual(request["archive"], Path(archive_path).name)
        self.assertEqual(request["manifest_sha256"], report["manifest_sha256"])

    def test_dry_run_makes_zero_device_calls_or_writes(self) -> None:
        resources = self.resources()
        before = sorted((path.relative_to(self.root).as_posix(), path.stat().st_size) for path in self.root.rglob("*"))
        stdout = io.StringIO()
        argv = ["--resources", str(resources), "--serial", "tablet-serial", "--bridge", str(self.bridge), "--dry-run", "--json"]
        with mock.patch.object(tool.subprocess, "run", side_effect=AssertionError("device call")), contextlib.redirect_stdout(stdout):
            code = tool.main(argv)
        after = sorted((path.relative_to(self.root).as_posix(), path.stat().st_size) for path in self.root.rglob("*"))
        report = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "planned")
        self.assertEqual(before, after)

    def test_existing_boot_runtime_is_blocked_before_any_transfer(self) -> None:
        resources = self.resources()
        runner = FakeHdc(preflight="D2R_BLOCK_DM_ENTRY:files/dm/boot.log\n")
        code, report, runner, _ = self.invoke(resources, fake=runner)
        self.assertEqual(code, 1)
        self.assertEqual(report["error"]["code"], "runtime_not_fresh")
        self.assertFalse(any("file" in call and "send" in call for call in runner.calls))

    def test_preflight_allows_only_an_empty_regular_romfs_data_directory(self) -> None:
        script = tool._preflight_script("entry")
        self.assertIn("echo D2R_RUNTIME_OK", script)
        self.assertNotIn("D2R_RUNTIME_OK\\n", script)
        self.assertIn("if [ ! -d \"$romfs_data\" ] || [ -L \"$romfs_data\" ]", script)
        self.assertIn('for entry in "$romfs_data"/* "$romfs_data"/.[!.]*', script)
        resources = self.resources()
        code, report, _, _ = self.invoke(resources)
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "awaiting_app_import")

    def test_pending_request_and_saves_block_without_mutation(self) -> None:
        resources = self.resources()
        for output in ("D2R_BLOCK_REQUEST_EXISTS\n", "D2R_BLOCK_SAVES:files/save\n"):
            with self.subTest(output=output):
                runner = FakeHdc(preflight=output)
                code, report, runner, _ = self.invoke(resources, fake=runner)
                self.assertEqual(code, 1)
                self.assertFalse(any("file" in call and "send" in call for call in runner.calls))
                self.assertEqual(report["error"]["code"], "pending_request" if "REQUEST" in output else "runtime_not_fresh")

    def test_running_app_is_refused_before_preflight_or_transfer(self) -> None:
        resources = self.resources()
        runner = FakeHdc(running=True)
        code, report, runner, _ = self.invoke(resources, fake=runner)
        self.assertEqual(code, 1)
        self.assertEqual(report["error"]["code"], "app_running")
        self.assertEqual(len(runner.calls), 1)
        self.assertNotIn("-b", runner.calls[0])

    def test_remote_archive_sha_mismatch_is_a_zero_exit_failure_and_sends_no_request(self) -> None:
        resources = self.resources()
        runner = FakeHdc()
        runner.hash_override = "0" * 64
        code, report, runner, _ = self.invoke(resources, fake=runner)
        self.assertEqual(code, 1)
        self.assertEqual(report["error"]["code"], "archive_verify_failed")
        sends = [call for call in runner.calls if "file" in call and "send" in call]
        self.assertEqual(len(sends), 1)
        self.assertTrue(sends[0][-1].endswith(".zip"))
        self.assertIn("local_staging", report["recovery"])

    def test_local_manifest_digest_mismatch_stops_before_process_or_device_access(self) -> None:
        resources = self.resources()
        manifest_path = resources / "resource-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with mock.patch.object(tool.subprocess, "run", side_effect=AssertionError("device call")) as mocked:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                code = tool.main(["--resources", str(resources), "--serial", "tablet-serial", "--bridge", str(self.bridge), "--json"])
        report = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(report["error"]["code"], "manifest_mismatch")
        mocked.assert_not_called()

    def test_temp_root_must_stay_outside_source_checkout(self) -> None:
        source = self.root / "source"
        source.mkdir()
        with mock.patch.object(tool, "SOURCE_ROOT", source), mock.patch.object(tool.tempfile, "gettempdir", return_value=str(source / "tmp")):
            with self.assertRaisesRegex(tool.ToolError, "inside the source checkout"):
                tool._temp_root()


if __name__ == "__main__":
    unittest.main()
