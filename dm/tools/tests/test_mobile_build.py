from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


# Respect the caller's temporary directory on macOS, Linux, and Windows.
TEST_TMP_ROOT = Path(tempfile.gettempdir())

MODULE_PATH = Path(__file__).resolve().parents[1] / "mobile_build.py"
SPEC = importlib.util.spec_from_file_location("mobile_build_under_test", MODULE_PATH)
assert SPEC and SPEC.loader
mobile_build = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mobile_build)


def touch(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def stub_texts(root: Path) -> None:
    touch(root / "dm_hud_exp.h", "static uint32_t k_exp[100];\n")
    touch(root / "dm_vault_tab.h", "static vt_row vt_rows[562];\ndm_derived_each(\"VTROW\", row, x);\n")
    touch(
        root / "dm_touch_native_art.c",
        "static struct { uint32_t id; int atlas, frame; } rows[429];\n"
        "static void ta_row_cb(char **f, int n, void *ctx) {}\n"
        "dm_derived_each(\"TAROW\", ta_row_cb, x);\n",
    )
    touch(root / "dm_bright_levels.inc", "{ 1, 0, 1, 0 },\n")


class Workspace:
    def __enter__(self) -> Path:
        self._temp = tempfile.TemporaryDirectory(prefix="mobile-build-test-", dir=TEST_TMP_ROOT)
        return Path(self._temp.name)

    def __exit__(self, *exc: object) -> None:
        self._temp.cleanup()


class PathGuardTests(unittest.TestCase):
    def test_workspace_in_source_is_rejected(self) -> None:
        with Workspace() as root:
            source = root / "source"
            source.mkdir()
            with self.assertRaisesRegex(mobile_build.BuildError, "outside"):
                mobile_build.ensure_external_workspace(source / "work", source)

    def test_source_manifest_rejects_traversal_and_outside_paths(self) -> None:
        for value in ("../secret", "dm/../secret", "/absolute", "other/file", ""):
            with self.subTest(value=value), self.assertRaises(mobile_build.BuildError):
                mobile_build.safe_source_relative(value)

    def test_generated_and_private_files_are_excluded(self) -> None:
        for rel in (
            "dm/host/dm_vault_tab.h",
            "dm/host/dm_hud_exp.h",
            "dm/host/dm_bright_levels.inc",
            "dm/ios/Sources/dm_touch_native_art.c",
            "dm/ios/Gen/assets/NativeHUD/v4/hud.json",
            "dm/ios/Sources/TouchArt/dm_native_skill_0.png",
            "dm/harmony/app/build-profile.json5",
        ):
            with self.subTest(rel=rel):
                self.assertTrue(mobile_build.is_excluded_source(rel))
        self.assertFalse(mobile_build.is_excluded_source("dm/ios/tools/native_art/generate.py"))


class DiscoveryTests(unittest.TestCase):
    def test_deveco_discovery_supports_paths_with_spaces(self) -> None:
        with Workspace() as root:
            contents = root / "DevEco Studio.app/Contents"
            files = {
                contents / "tools/node/bin/node": "",
                contents / "tools/hvigor/bin/hvigorw": "",
                contents / "tools/ohpm/bin/ohpm": "",
                contents / "tools/ohpm/resources/.ohpmrc": "registry=x\n",
                contents / "jbr/Contents/Home/bin/java": "",
                contents / "sdk/default/sdk-pkg.json": json.dumps(
                    {"data": {"apiVersion": "24", "displayName": "HarmonyOS 6.1.1", "platformVersion": "6.1.1"}}
                ),
            }
            for path, text in files.items():
                touch(path, text)
            resolved = mobile_build.discover_deveco(root / "DevEco Studio.app")
            tools = mobile_build.discover_deveco_tools(resolved)
            sdk = mobile_build.discover_harmony_sdk(None, resolved)
            info = mobile_build.read_harmony_sdk(sdk)
            self.assertEqual(resolved, contents)
            self.assertEqual(tools["hvigor"].name, "hvigorw")
            self.assertEqual(info["api"], 24)
            self.assertEqual(info["label"], "6.1.1(24)")

    def test_harmony_requested_api_is_never_silently_lowered(self) -> None:
        with Workspace() as root:
            sdk = root / "sdk/default"
            touch(
                sdk / "sdk-pkg.json",
                json.dumps({"data": {"apiVersion": "24", "displayName": "HarmonyOS 6.1.1", "platformVersion": "6.1.1"}}),
            )
            common = {
                "deveco": root / "deveco",
                "sdk": sdk,
            }
            args = SimpleNamespace(sdk_version="6.1.1(26)", build_profile=None)
            with mock.patch.object(mobile_build, "discover_deveco_tools", side_effect=mobile_build.BuildError("stop-after-sdk")):
                # The SDK check is covered directly so this test does not need a full DevEco layout.
                self.assertEqual(mobile_build.required_harmony_api(args.sdk_version), 26)
            self.assertGreater(26, mobile_build.read_harmony_sdk(sdk)["api"])


class BuildPlanTests(unittest.TestCase):
    def make_android_source(self, root: Path) -> Path:
        source = root / "source tree"
        touch(source / "dm/PRODUCT", "3.3.94012-r45\n")
        touch(
            source / "dm/android/app/build.gradle",
            "android {\n compileSdk 36\n ndkVersion '29.0.14206865'\n"
            " defaultConfig {\n  targetSdk 36\n  versionCode 20300\n"
            "  versionName project.findProperty('dmVersionName') ?: '2.3.0'\n }\n"
            " externalNativeBuild { cmake { version '3.31.6' } }\n}\n",
        )
        return source

    def test_android_plan_uses_gradle_native_build_without_prebuilt_fallback(self) -> None:
        with Workspace() as root:
            source = self.make_android_source(root)
            sdk = root / "Android SDK with spaces"
            for directory in (
                sdk / "platforms/android-36",
                sdk / "ndk/29.0.14206865",
                sdk / "cmake/3.31.6/bin",
            ):
                directory.mkdir(parents=True)
            gradle = root / "Gradle with spaces/bin/gradle"
            touch(gradle, "#!/bin/sh\n")
            os.chmod(gradle, 0o755)
            java_home = root / "Java Home"
            (java_home / "bin").mkdir(parents=True)
            gen = root / "generated stubs"
            stub_texts(gen)
            common = {
                "workspace": root / "private workspace",
                "source_root": source,
                "dm_root": source / "dm",
                "python": "python3",
                "gen_dir": gen,
                "game_dir": None,
                "romfs": None,
                "build_profile": None,
                "sdk": sdk,
                "deveco": None,
                "gradle": gradle,
                "java_home": java_home,
                "app_id": mobile_build.ANDROID_DEFAULT_APP_ID,
                "label": mobile_build.DEFAULT_LABEL,
                "product": "default",
            }
            args = SimpleNamespace(sdk_version=None)
            with mock.patch.object(mobile_build, "SOURCE_ROOT", source):
                args.build_mode = "debug"
                plan = mobile_build.android_plan(args, common)
            command = " ".join(plan["commands"][0])
            self.assertEqual(plan["native"]["mode"], "gradle-externalNativeBuild")
            self.assertFalse(plan["native"]["prebuilt_fallback"])
            self.assertIn(":app:assembleDebug", command)
            self.assertNotIn("dmPrebuiltJni", command)
            self.assertIn("dmAppId=com.xiaoboluo.d2r.mate60", command)
            self.assertIn("dmAppLabel=暗黑2", command)
            self.assertEqual(plan["product"], "default")
            self.assertEqual(plan["build_product"], "default")

    def test_dry_run_does_not_write_or_run_processes(self) -> None:
        with Workspace() as root:
            source = self.make_android_source(root)
            gen = root / "gen"
            stub_texts(gen)
            workspace = root / "workspace"
            real_source = mobile_build.SOURCE_ROOT
            try:
                mobile_build.SOURCE_ROOT = source
                command = [
                    "--target", "android", "--workspace", str(workspace), "--gen-dir", str(gen),
                    "--sdk", str(root / "sdk"), "--gradle", str(root / "gradle"), "--dry-run", "--json",
                ]
                output = io.StringIO()
                with mock.patch.object(mobile_build.subprocess, "run", side_effect=AssertionError("process started")):
                    with contextlib.redirect_stdout(output):
                        code = mobile_build.main(command)
            finally:
                mobile_build.SOURCE_ROOT = real_source
            self.assertEqual(code, 0)
            self.assertFalse(workspace.exists())
            plan = json.loads(output.getvalue())
            self.assertTrue(plan["dry_run"])
            self.assertEqual(plan["generated"]["action"], "reuse")

    def test_gen_dir_copy_is_exactly_four_files(self) -> None:
        with Workspace() as root:
            source = root / "gen-source"
            destination = root / "workspace/build/gen"
            stub_texts(source)
            touch(source / "derived.tsv", "game data")
            touch(source / "extra-secret.txt", "do not copy")
            hashes = mobile_build.copy_exact_generated(source, destination)
            self.assertEqual(set(hashes), set(mobile_build.GENERATED_FILES))
            self.assertEqual({path.name for path in destination.iterdir()}, set(mobile_build.GENERATED_FILES))

    def test_build_mode_defaults_to_debug_and_release_unsigned_is_explicit(self) -> None:
        defaults = mobile_build.make_parser().parse_args([
            "--target", "ohos", "--workspace", str(TEST_TMP_ROOT / "workspace-test"),
        ])
        self.assertEqual(defaults.build_mode, "debug")
        with Workspace() as root:
            output = root / "app/build/outputs/apk/release"
            output.mkdir(parents=True)
            unsigned = output / "app-release-unsigned.apk"
            touch(unsigned, "unsigned")
            # macOS may spell the same temporary tree /var or /private/var.
            artifact = mobile_build.select_android_artifact(root, "release")
            self.assertTrue(artifact.is_absolute())
            self.assertTrue(artifact.samefile(unsigned))
            self.assertEqual(mobile_build.artifact_signature_status(unsigned, "android", False), (False, "unsigned"))

    def test_runtime_product_and_build_product_are_distinct(self) -> None:
        with Workspace() as root:
            source = root / "source"
            touch(source / "dm/PRODUCT", "3.3.94012-r45\n")
            gen = root / "gen"
            stub_texts(gen)
            old_source = mobile_build.SOURCE_ROOT
            mobile_build.SOURCE_ROOT = source
            try:
                product = mobile_build.validate_common_args(SimpleNamespace(
                    workspace=root / "workspace", python="python3", gen_dir=gen, game_dir=None,
                    build_profile=None, target="ohos", sdk=None, deveco=None, gradle=None,
                    java_home=None, app_id=None, label="暗黑2",
                ))["product"]
            finally:
                mobile_build.SOURCE_ROOT = old_source
            self.assertEqual(product, "3.3.94012-r45")


class ArtifactTests(unittest.TestCase):
    def test_signed_and_unsigned_hap_status_is_explicit(self) -> None:
        with Workspace() as root:
            signed = root / "entry-default-signed.hap"
            unsigned = root / "entry-default-unsigned.hap"
            touch(signed, "signed")
            touch(unsigned, "unsigned")
            self.assertEqual(mobile_build.artifact_signature_status(signed, "ohos", True), (True, "signature_detected"))
            self.assertEqual(mobile_build.artifact_signature_status(unsigned, "ohos", True), (False, "unsigned"))

    def test_android_signature_is_detected_from_signature_entries(self) -> None:
        with Workspace() as root:
            signed = root / "app-debug.apk"
            with zipfile.ZipFile(signed, "w") as archive:
                archive.writestr("META-INF/CERT.RSA", b"signature")
            self.assertEqual(mobile_build.artifact_signature_status(signed, "android", False), (True, "signature_detected"))

    def test_artifact_selection_requires_the_exact_unsigned_name(self) -> None:
        with Workspace() as root:
            app = root / "app"
            outputs = app / "entry/build/default/outputs/default"
            outputs.mkdir(parents=True)
            touch(outputs / "unrelated-old.hap")
            with self.assertRaisesRegex(mobile_build.BuildError, "explicit output"):
                mobile_build.select_harmony_artifact(app, False)
            expected = outputs / "entry-default-unsigned.hap"
            touch(expected)
            self.assertEqual(mobile_build.select_harmony_artifact(app, False), expected)


if __name__ == "__main__":
    unittest.main()
