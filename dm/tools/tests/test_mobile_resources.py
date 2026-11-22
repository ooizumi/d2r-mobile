#!/usr/bin/env python3
"""Synthetic tests for mobile_resources; they need neither ROM content nor a device."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mobile_resources as tool


class MobileResourcesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="mobile-resources-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def make_game(self, name: str = "update", mutate: bytes = b"") -> tuple[Path, dict[str, str], dict[str, int]]:
        update = self.root / name
        exefs = update / "ExeFS"
        data = update / "RomFS" / "data"
        exefs.mkdir(parents=True)
        data.mkdir(parents=True)
        expected_exefs: dict[str, str] = {}
        for index, exe_name in enumerate(("main", "sdk", "subsdk0")):
            payload = ("synthetic-%s-%d" % (exe_name, index)).encode("ascii")
            (exefs / exe_name).write_bytes(payload)
            expected_exefs[exe_name] = hashlib.sha256(payload).hexdigest()
        sizes = {name: 8 for name in tool.REQUIRED_ARCHIVES}
        for index, archive_name in enumerate(tool.REQUIRED_ARCHIVES):
            payload = (mutate + index.to_bytes(1, "little") + archive_name.encode("ascii"))[:8].ljust(8, b"x")
            if index == 0:
                payload = payload[:4]
                (data / (archive_name + ".holes")).write_text("size 8\n4 4\n", encoding="ascii")
            (data / archive_name).write_bytes(payload)
        (data / ".build.config").write_text("synthetic owned config\n", encoding="utf-8")
        (data / "build_manifest.psv").write_text("synthetic manifest\n", encoding="utf-8")
        (data / "extra-not-selected.data").write_bytes(b"must not be copied")
        return update, expected_exefs, sizes

    def inspect(self, update: Path, expected_exefs: dict[str, str], sizes: dict[str, int]) -> tool.GameInfo:
        return tool.inspect_game_update(update, expected_exefs=expected_exefs, archive_sizes=sizes)

    def invoke_main(self, argv: list[str]) -> tuple[int, dict[str, object]]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = tool.main(argv)
        return code, json.loads(stdout.getvalue())

    def test_upstream_archive_and_executable_contracts_match(self) -> None:
        host = (tool.SOURCE_DM / "host" / "romfs_validate.c").read_text(encoding="utf-8")
        need_block = re.search(r"static const char \*need\[\] = \{(.*?)\};", host, re.S)
        size_block = re.search(r"static const long long size\[\] = \{(.*?)\};", host, re.S)
        self.assertIsNotNone(need_block)
        self.assertIsNotNone(size_block)
        names = tuple(re.findall(r'"([^"]+\.data)"', need_block.group(1)))
        sizes = tuple(int(value) for value in re.findall(r"\d+", size_block.group(1)))
        self.assertEqual(names, tool.REQUIRED_ARCHIVES)
        self.assertEqual(len(sizes), 18)
        self.assertEqual(sizes, tuple(tool.ARCHIVE_RAW_SIZES[name] for name in names))
        extract = (tool.SOURCE_DM / "tools" / "extract_game.sh").read_text(encoding="utf-8")
        for name, digest in tool.EXPECTED_EXEFS_SHA256.items():
            self.assertRegex(extract, r"(?m)^%s %s$" % (digest, re.escape(name)))
        self.assertIn('grep -q "^Title ID: *' + tool.TITLE_ID + '"', extract)

    def test_source_snapshot_excludes_ignored_files_and_keeps_original_untouched(self) -> None:
        repo = self.root / "repo"
        dm = repo / "dm"
        source = dm / "tools" / "artifact.py"
        ignored = dm / "generated.bin"
        source.parent.mkdir(parents=True)
        source.write_text("print('tracked')\n", encoding="utf-8")
        ignored.write_bytes(b"ignored source-tree output")
        (repo / ".gitignore").write_text("dm/generated.bin\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "add", ".gitignore", "dm/tools/artifact.py"], check=True)
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        entries, fingerprint = tool._source_inventory(dm, repo_root=repo)
        self.assertEqual([entry.relative.as_posix() for entry in entries], ["tools/artifact.py"])
        snapshot_dm = tool._ensure_source_snapshot(self.root / "workspace", entries, fingerprint)
        self.assertTrue((snapshot_dm / "tools/artifact.py").is_file())
        self.assertFalse((snapshot_dm / "generated.bin").exists())
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)
        source.write_text("print('changed')\n", encoding="utf-8")
        _, changed = tool._source_inventory(dm, repo_root=repo)
        self.assertNotEqual(changed, fingerprint)

    def test_source_export_manifest_fallback_filters_generated_and_private_files(self) -> None:
        source_root = self.root / "source-export"
        dm = source_root / "dm"
        source = dm / "tools" / "artifact.py"
        generated = dm / "ios" / "Sources" / "TouchArt" / "dm_native_skill_0.png"
        profile = dm / "harmony" / "app" / "build-profile.json5"
        source.parent.mkdir(parents=True)
        generated.parent.mkdir(parents=True)
        profile.parent.mkdir(parents=True)
        source.write_text("print('exported')\n", encoding="utf-8")
        generated.write_bytes(b"must be excluded")
        profile.write_text('{"signingConfigs":[]}\n', encoding="utf-8")
        root_manifest = source_root / "SOURCE_EXPORT_MANIFEST.txt"
        root_manifest.write_text(
            "# complete source export\n"
            "README.md\n"
            "docs/MOBILE_INSTALL.zh-CN.md\n"
            "dm/tools/artifact.py\n"
            "dm/ios/Sources/TouchArt/dm_native_skill_0.png\n"
            "dm/harmony/app/build-profile.json5\n",
            encoding="utf-8",
        )
        dm_manifest = dm / "SOURCE_EXPORT_MANIFEST.txt"
        dm_manifest.write_text(
            "dm/tools/artifact.py\n"
            "dm/ios/Sources/TouchArt/dm_native_skill_0.png\n"
            "dm/harmony/app/build-profile.json5\n",
            encoding="utf-8",
        )
        self.assertEqual(tool._snapshot_mode(source_root), "manifest")
        self.assertEqual(tool._source_manifest_path(source_root), dm_manifest.resolve())
        entries, fingerprint = tool._source_inventory(dm, repo_root=source_root)
        self.assertEqual([entry.relative.as_posix() for entry in entries], ["tools/artifact.py"])
        self.assertEqual(len(fingerprint), 64)
        snapshot = tool._ensure_source_snapshot(self.root / "manifest-workspace", entries, fingerprint)
        self.assertTrue((snapshot / "tools/artifact.py").is_file())
        self.assertFalse((snapshot / "ios/Sources/TouchArt/dm_native_skill_0.png").exists())
        self.assertFalse((snapshot / "harmony/app/build-profile.json5").exists())
        dm_manifest.rename(self.root / "dm-manifest.saved")
        mixed_entries, _ = tool._source_inventory(dm, repo_root=source_root)
        self.assertEqual([entry.relative.as_posix() for entry in mixed_entries], ["tools/artifact.py"])

    def test_dry_run_game_dir_has_no_side_effects_and_no_generation(self) -> None:
        update, expected_exefs, sizes = self.make_game()
        workspace = self.root / "workspace-does-not-exist"
        with mock.patch.object(tool, "EXPECTED_EXEFS_SHA256", expected_exefs), mock.patch.object(tool, "ARCHIVE_RAW_SIZES", sizes), mock.patch.object(tool, "_source_fingerprint", return_value=([], "a" * 64)), mock.patch.object(tool, "_run_child", side_effect=AssertionError("child launched during dry-run")):
            code, report = self.invoke_main(["--target", "android", "--workspace", str(workspace), "--game-dir", str(update), "--dry-run", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "planned")
        self.assertFalse(report["launch_extraction"])
        self.assertFalse(report["launch_generation"])
        self.assertFalse(report["write_performed"])
        self.assertEqual(report["game_dir"], str(update.resolve()))
        self.assertFalse(workspace.exists())

    def test_dry_run_packages_does_not_extract_or_create_workspace(self) -> None:
        base = self.root / "base.nsp"
        update = self.root / "update.xci"
        keys = self.root / "prod.keys"
        base.write_bytes(b"synthetic base")
        update.write_bytes(b"synthetic update")
        keys.write_text("not read during dry-run\n", encoding="utf-8")
        workspace = self.root / "rom-workspace-does-not-exist"
        with mock.patch.object(tool, "_source_fingerprint", return_value=([], "b" * 64)), mock.patch.object(tool, "_resolve_extraction_runtime", return_value=("bash", Path("/bin/true"))), mock.patch.object(tool, "_run_child", side_effect=AssertionError("extractor launched during dry-run")):
            code, report = self.invoke_main([
                "--target", "ohos", "--workspace", str(workspace),
                "--rom", str(base), "--rom", str(update), "--keys", str(keys),
                "--dry-run", "--json",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "planned")
        self.assertTrue(report["launch_extraction"])
        self.assertFalse(report["launch_generation"])
        self.assertFalse(report["write_performed"])
        self.assertFalse(workspace.exists())

    def test_target_option_mismatch_is_structured_json_error(self) -> None:
        code, report = self.invoke_main([
            "--target", "ohos", "--workspace", str(self.root / "workspace"),
            "--game-dir", str(self.root / "missing-update"), "--astc-pack", str(self.root / "pack"),
            "--dry-run", "--json",
        ])
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["error"]["code"], "target_option_mismatch")

    def test_path_safety_rejects_source_workspace_and_output_overlap(self) -> None:
        for path in (tool.SOURCE_DM / "unsafe-workspace", tool.SOURCE_DM.parent / "generated"):
            with self.subTest(workspace=path), self.assertRaises(tool.ToolError) as caught:
                tool._workspace_plan(path, tool.SOURCE_DM, [])
            self.assertEqual(caught.exception.code, "unsafe_workspace")
        workspace = self.root / "workspace"
        with self.assertRaises(tool.ToolError) as caught:
            tool._output_disjoint(self.root / "outside", workspace, [])
        self.assertEqual(caught.exception.code, "unsafe_output")
        with self.assertRaises(tool.ToolError) as caught:
            tool._output_disjoint(workspace / "generated", workspace, [workspace])
        self.assertEqual(caught.exception.code, "unsafe_output_overlap")

    def test_symbolic_link_inputs_are_rejected(self) -> None:
        real = self.root / "real.bin"
        real.write_bytes(b"input")
        link = self.root / "link.bin"
        try:
            link.symlink_to(real)
        except OSError as exc:
            self.skipTest("symlink creation unavailable: %s" % exc)
        with self.assertRaises(tool.ToolError) as caught:
            tool._safe_regular_file(link, "test input")
        self.assertEqual(caught.exception.code, "unsafe_input_symlink")

    def test_child_environment_strips_inherited_dm_state(self) -> None:
        snapshot = self.root / "snapshot"
        cache = self.root / "cache"
        tmp = self.root / "tmp"
        snapshot.mkdir()
        with mock.patch.dict(os.environ, {
            "DM_PC_DATA": "/stale/assets",
            "DM_ANDROID_SHADOW_SIDE": "1",
            "DM_REQUIRE_PC_ART": "1",
            "DM_GEN_ASSETS": "/stale/gen",
            "GLSLC": "/custom/glslc",
            "PATH": "/kept/path",
        }, clear=False):
            env = tool._child_env(snapshot, cache, tmp)
        self.assertEqual(env["DM_ROOT"], str(cache))
        self.assertEqual(env["PATH"], "/kept/path")
        self.assertEqual(env["GLSLC"], "/custom/glslc")
        self.assertNotIn("DM_PC_DATA", env)
        self.assertNotIn("DM_ANDROID_SHADOW_SIDE", env)
        self.assertNotIn("DM_REQUIRE_PC_ART", env)
        self.assertNotIn("DM_GEN_ASSETS", env)

    def test_generation_uses_private_mutable_source_copy(self) -> None:
        base = self.root / "base-source"
        (base / "tools").mkdir(parents=True)
        (base / "tools" / "d2rm_android.py").write_text("# synthetic\n", encoding="utf-8")
        private = tool._fresh_generation_source(base, self.root / "cache")
        self.assertNotEqual(private, base)
        (private / "tools" / "d2rm_android.py").write_text("# mutated\n", encoding="utf-8")
        self.assertEqual((base / "tools" / "d2rm_android.py").read_text(encoding="utf-8"), "# synthetic\n")

    def test_resource_staging_selects_exactly_required_files(self) -> None:
        update, expected_exefs, sizes = self.make_game()
        game = self.inspect(update, expected_exefs, sizes)
        rows = {row["name"]: row for row in game.identity["resource_archives"]}
        destination = self.root / "staged-data"
        tool.stage_data_resources(game, destination, rows)
        expected = set(tool.REQUIRED_ARCHIVES) | {tool.REQUIRED_ARCHIVES[0] + ".holes", ".build.config"}
        self.assertEqual({path.name for path in destination.iterdir()}, expected)
        self.assertNotIn("extra-not-selected.data", {path.name for path in destination.iterdir()})
        self.assertEqual((destination / (tool.REQUIRED_ARCHIVES[0] + ".holes")).read_text(encoding="ascii"), "size 8\n4 4\n")
        for name in tool.REQUIRED_ARCHIVES:
            self.assertEqual(tool.sha256_file(destination / name)[0], game.archive_sha256[name])
        self.assertEqual(tool.sha256_file(destination / ".build.config")[0], game.config_sha256)

    def test_build_key_invalidates_for_archive_and_target_changes(self) -> None:
        first, exefs_one, sizes_one = self.make_game("first", mutate=b"a")
        second, exefs_two, sizes_two = self.make_game("second", mutate=b"b")
        game_one = self.inspect(first, exefs_one, sizes_one)
        game_two = self.inspect(second, exefs_two, sizes_two)
        self.assertNotEqual(game_one.identity_digest, game_two.identity_digest)

        def key(game: tool.GameInfo, target: str) -> str:
            return tool._digest_value({
                "target": target,
                "source_fingerprint": "c" * 64,
                "game_identity_sha256": game.identity_digest,
                "warm_seed_sha256": None,
                "astc_pack": None,
            })

        android_one = key(game_one, "android")
        self.assertNotEqual(android_one, key(game_one, "ohos"))
        self.assertNotEqual(android_one, key(game_two, "android"))

    def test_manifest_contract_locates_runtime_and_complete_import_parent(self) -> None:
        staging = self.root / "staging"
        (staging / "android").mkdir(parents=True)
        (staging / "data").mkdir()
        (staging / "android" / "pack_info.txt").write_text("PRODUCT=synthetic\nTARGET=android\n", encoding="utf-8")
        (staging / "data" / "sample.data").write_bytes(b"resource")
        files = tool._tree_files(staging)
        manifest = {
            "schema_version": 1,
            "status": "complete",
            "target": "android",
            "product": "synthetic",
            "runtime_root": "android",
            "import_staging": {"path": ".", "layout": ["android", "data"], "mobile_device_resources": "."},
            "pack": {"path": "android"},
            "data": {"path": "data"},
            "files": files,
        }
        path = staging / tool.MANIFEST_NAME
        tool._write_json_atomic(path, manifest)
        readback = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(readback["runtime_root"], "android")
        self.assertEqual(readback["import_staging"]["layout"], ["android", "data"])
        self.assertEqual([row["path"] for row in readback["files"]], ["android/pack_info.txt", "data/sample.data"])
        self.assertEqual({row["sha256"] for row in readback["files"]}, {
            hashlib.sha256(b"PRODUCT=synthetic\nTARGET=android\n").hexdigest(),
            hashlib.sha256(b"resource").hexdigest(),
        })
        self.assertFalse(any(path.name.startswith(".resource-manifest.json.tmp-") for path in staging.iterdir()))

    def test_reused_result_returns_canonical_game_dir_for_build_handoff(self) -> None:
        final = self.root / "workspace" / "mobile-resources" / "android" / ("f" * 64)
        (final / "android").mkdir(parents=True)
        (final / "data").mkdir()
        game_dir = self.root / "owned-update"
        game_dir.mkdir()
        manifest = {
            "schema_version": 1,
            "status": "complete",
            "target": "android",
            "product": "synthetic",
            "build_key": "f" * 64,
            "source": {"fingerprint": "a" * 64, "mode": "manifest"},
            "game_identity": {"sha256": "b" * 64},
            "files": [],
        }
        tool._write_json_atomic(final / tool.MANIFEST_NAME, manifest)
        result = tool._existing_compatible(final, {
            "build_key": "f" * 64,
            "target": "android",
            "product": "synthetic",
            "source_fingerprint": "a" * 64,
            "game_identity_digest": "b" * 64,
            "game_dir": str(game_dir.resolve()),
            "cache_dir": str(self.root / "cache"),
            "pack_name": "android",
        })
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "reused")
        self.assertEqual(result["game_dir"], str(game_dir.resolve()))
        self.assertEqual(result["source_mode"], "manifest")


if __name__ == "__main__":
    unittest.main(verbosity=2)
