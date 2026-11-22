package com.xiaoboluo.d2r;

import java.io.File;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.StandardOpenOption;
import java.util.Arrays;
import java.util.List;

final class CloudSaveRulesTest {
    private static int checks;

    public static void main(String[] args) throws Exception {
        File root = Files.createTempDirectory("dm-cloud-rules-").toFile();
        try {
            snapshotFilterAndNestedExcludedRestore(root);
            emptySnapshotDecisionsAndCollisions(root);
            pathValidation();
            sizeAndDepthLimits(root);
            System.out.println("CloudSaveRulesTest: PASS (" + checks + " checks)");
        } finally {
            delete(root);
        }
    }

    private static void snapshotFilterAndNestedExcludedRestore(File root) throws Exception {
        File save = new File(root, "save");
        write(new File(save, "Hero/Hero.d2s"), "hero");
        write(new File(save, "Hero/ModernSharedStashSoftCoreV2.sw_kills"), "kills");
        write(new File(save, "sw_elysium.txt"), "Hero\n");
        write(new File(save, "Hero/.local/cache.bin"), "dot");
        write(new File(save, "Hero/_cloudtmp/pending.bin"), "underscore");
        write(new File(save, "Settings/Settings.json"), "settings");
        write(new File(save, "_cloudinbox/entry/import.sha256"), "inbox");
        write(new File(save, ".private/state"), "private");

        CloudSaveRules.Manifest manifest = CloudSaveRules.scan(save);
        check(manifest.files.size() == 3, "three synchronized regular files");
        check(manifest.files.get(0).path.equals("Hero/Hero.d2s"), "files sort by UTF-16 path");
        check(manifest.files.get(1).path.equals("Hero/ModernSharedStashSoftCoreV2.sw_kills"), "nested non-hidden side file included");
        check(manifest.files.get(2).path.equals("sw_elysium.txt"), "root Elysium side file included");
        CloudSaveRules.Entry checked = CloudSaveRules.entryFromData("Hero/Hero.d2s", "hero".getBytes(StandardCharsets.UTF_8));
        CloudSaveRules.verifyContent(checked, "hero".getBytes(StandardCharsets.UTF_8));
        check(checked.sha256.equals(manifest.files.get(0).sha256), "per-file SHA-256 matches snapshot file");
        expectIOException(() -> CloudSaveRules.verifyContent(checked, "Hero".getBytes(StandardCharsets.UTF_8)),
                "corrupt file content is rejected");

        File restore = new File(root, "restore");
        write(new File(restore, "Hero/Hero.d2s"), "cloud");
        CloudSaveRules.copyExcluded(save, restore);
        check(new File(restore, "Hero/.local/cache.bin").isFile(), "nested dot-prefixed file preserved");
        check(new File(restore, "Hero/_cloudtmp/pending.bin").isFile(), "nested underscore directory preserved");
        check(new File(restore, "Settings/Settings.json").isFile(), "top-level Settings preserved");
        check(new File(restore, "_cloudinbox/entry/import.sha256").isFile(), "top-level underscore tree preserved");
        check(new File(restore, ".private/state").isFile(), "top-level dot tree preserved");
        check(new String(Files.readAllBytes(new File(restore, "Hero/Hero.d2s").toPath()), StandardCharsets.UTF_8).equals("cloud"),
                "ordinary included file remains the cloud copy");
    }

    private static void emptySnapshotDecisionsAndCollisions(File root) throws Exception {
        File empty = new File(root, "empty-save");
        check(empty.mkdirs(), "create empty snapshot");
        String emptyHash = CloudSaveRules.scan(empty).sha256;
        check(emptyHash.equals(CloudSaveRules.EMPTY_SHA256), "empty snapshot has SHA-256 of zero bytes");
        String remoteHash = "1111111111111111111111111111111111111111111111111111111111111111";
        String localHash = "2222222222222222222222222222222222222222222222222222222222222222";
        String baselineHash = "3333333333333333333333333333333333333333333333333333333333333333";

        check(CloudSaveRules.decide(emptyHash, remoteHash, "r1", false, "", "") == CloudSaveRules.SyncAction.DOWNLOAD,
                "fresh empty device downloads populated cloud");
        check(CloudSaveRules.decide(localHash, "", "", false, "", "") == CloudSaveRules.SyncAction.UPLOAD,
                "fresh local files upload to unused cloud");
        check(CloudSaveRules.decide(emptyHash, "", "", false, "", "") == CloudSaveRules.SyncAction.ACK,
                "fresh empty device and unused cloud acknowledge");
        check(CloudSaveRules.decide(localHash, "", "", true, "", "") == CloudSaveRules.SyncAction.UPLOAD,
                "acknowledged empty baseline still uploads newly created local saves");
        check(CloudSaveRules.decide(localHash, emptyHash, "r-empty", true, "r-empty", emptyHash) == CloudSaveRules.SyncAction.UPLOAD,
                "local files upload over an explicitly published empty snapshot");
        check(CloudSaveRules.decide(emptyHash, remoteHash, "r2", true, "r1", emptyHash) == CloudSaveRules.SyncAction.DOWNLOAD,
                "unchanged empty baseline downloads one-sided remote change");
        check(CloudSaveRules.decide(localHash, emptyHash, "r1", true, "r1", emptyHash) == CloudSaveRules.SyncAction.UPLOAD,
                "empty published baseline uploads one-sided local change");
        check(CloudSaveRules.decide(emptyHash, "", "", true, "r-empty", emptyHash) == CloudSaveRules.SyncAction.CONFLICT,
                "published cloud disappearing remains a conflict even for empty content");
        check(CloudSaveRules.decide(localHash, remoteHash, "r2", true, "r1", baselineHash) == CloudSaveRules.SyncAction.CONFLICT,
                "both sides changed conflict without timestamp winner");
        check(CloudSaveRules.decide(localHash, remoteHash, "r2", false, "", "") == CloudSaveRules.SyncAction.CONFLICT,
                "first sync with files on both sides conflicts");
        check(CloudSaveRules.isEmptyHash("") && CloudSaveRules.isEmptyHash(emptyHash), "unused and published empty hashes normalize");

        File preserveSave = new File(root, "preserve-save");
        write(new File(preserveSave, "Hero/_guard"), "local-only file");
        File cloudStage = new File(root, "cloud-stage");
        write(new File(cloudStage, "Hero/_guard/remote.bin"), "invalid cloud path");
        expectIOException(() -> CloudSaveRules.copyExcluded(preserveSave, cloudStage),
                "restore rejects cloud path blocked by preserved local ancestor");

        List<CloudSaveRules.Entry> collision = Arrays.asList(
                new CloudSaveRules.Entry("Hero", 0, CloudSaveRules.EMPTY_SHA256),
                new CloudSaveRules.Entry("Hero/Hero.d2s", 0, CloudSaveRules.EMPTY_SHA256));
        expectIOException(() -> CloudSaveRules.validateEntries(collision), "file/directory prefix collision rejected");
    }

    private static void pathValidation() {
        expectIOException(() -> CloudSaveRules.validateCloudPath("Settings/Settings.json"), "Settings is local-only");
        expectIOException(() -> CloudSaveRules.validateCloudPath("Hero/.local/cache"), "dot component is local-only");
        expectIOException(() -> CloudSaveRules.validateCloudPath("Hero/_cloudtmp/cache"), "underscore component is local-only");
        expectIOException(() -> CloudSaveRules.validateCloudPath("Hero\\Hero.d2s"), "backslash path rejected");
        expectIOException(() -> CloudSaveRules.validateCloudPath("Hero/../Hero.d2s"), "dot-dot path rejected");
        expectIOException(() -> CloudSaveRules.validateCloudPath("a/b/c/d/e/f/g/h/i"), "path component limit enforced");
        char[] longPath = new char[CloudSaveRules.MAX_PATH_UTF16 + 1];
        Arrays.fill(longPath, 'x');
        expectIOException(() -> CloudSaveRules.validatePath(new String(longPath)), "UTF-16 path length limit enforced");
    }

    private static void sizeAndDepthLimits(File root) throws Exception {
        File oversized = new File(root, "oversized");
        check(oversized.mkdirs(), "create oversized snapshot");
        File file = new File(oversized, "too-large.d2s");
        try (FileChannel channel = FileChannel.open(file.toPath(), StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE)) {
            channel.position(CloudSaveRules.MAX_FILE_BYTES);
            channel.write(ByteBuffer.wrap(new byte[]{0}));
        }
        expectIOException(() -> CloudSaveRules.scan(oversized), "per-file size limit enforced");
    }

    private static void write(File file, String text) throws IOException {
        File parent = file.getParentFile();
        if (parent != null) check(parent.isDirectory() || parent.mkdirs(), "create parent directory");
        Files.write(file.toPath(), text.getBytes(StandardCharsets.UTF_8));
    }

    private static void expectIOException(IoRunnable runnable, String label) {
        try {
            runnable.run();
            throw new AssertionError("expected IOException: " + label);
        } catch (IOException expected) {
            check(true, label);
        }
    }

    private static void check(boolean value, String label) {
        checks++;
        if (!value) throw new AssertionError(label);
    }

    private static void delete(File file) throws IOException {
        if (!file.exists()) return;
        File[] children = file.listFiles();
        if (children != null) for (File child : children) delete(child);
        if (!file.delete()) throw new IOException("delete " + file);
    }

    private interface IoRunnable { void run() throws IOException; }
}
