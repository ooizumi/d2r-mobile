package com.xiaoboluo.d2r;

import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.DirectoryStream;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.BasicFileAttributes;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/** Protocol rules shared by the Android client and host-side tests. */
final class CloudSaveRules {
    static final int MAX_FILES = 2048;
    static final long MAX_FILE_BYTES = 8L * 1024 * 1024;
    static final long MAX_TOTAL_BYTES = 24L * 1024 * 1024;
    static final long MAX_HTTP_BYTES = 48L * 1024 * 1024;
    static final int MAX_PATH_UTF16 = 512;
    static final int MAX_PATH_COMPONENTS = 8;
    static final String EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

    enum SyncAction { ACK, UPLOAD, DOWNLOAD, CONFLICT }

    static final class Entry {
        final String path;
        final long size;
        final String sha256;

        Entry(String path, long size, String sha256) {
            this.path = path;
            this.size = size;
            this.sha256 = sha256;
        }
    }

    static final class Manifest {
        final List<Entry> files;
        final String sha256;
        final long totalBytes;

        Manifest(List<Entry> files, String sha256, long totalBytes) {
            this.files = Collections.unmodifiableList(new ArrayList<>(files));
            this.sha256 = sha256;
            this.totalBytes = totalBytes;
        }
    }

    private CloudSaveRules() {}

    static Manifest scan(File snapshotRoot) throws IOException {
        if (!snapshotRoot.isDirectory()) throw new IOException("snapshot is not a directory");
        List<Entry> files = new ArrayList<>();
        scanDirectory(snapshotRoot.toPath(), "", files);
        files.sort(Comparator.comparing(entry -> entry.path));
        validateEntries(files);
        long total = 0;
        for (Entry entry : files) total += entry.size;
        if (total > MAX_TOTAL_BYTES) throw new IOException("snapshot exceeds decoded size limit");
        return new Manifest(files, snapshotDigest(files), total);
    }

    private static void scanDirectory(Path directory, String prefix, List<Entry> out) throws IOException {
        List<Path> children = new ArrayList<>();
        try (DirectoryStream<Path> stream = Files.newDirectoryStream(directory)) {
            for (Path child : stream) children.add(child);
        }
        children.sort((a, b) -> a.getFileName().toString().compareTo(b.getFileName().toString()));
        for (Path child : children) {
            String name = child.getFileName().toString();
            String relative = prefix.isEmpty() ? name : prefix + "/" + name;
            BasicFileAttributes attrs = Files.readAttributes(child, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
            if (attrs.isSymbolicLink() || (!attrs.isDirectory() && !attrs.isRegularFile())) {
                throw new IOException("unsupported save entry: " + relative);
            }
            if (excludedComponent(name) || (prefix.isEmpty() && name.equals("Settings"))) continue;
            if (attrs.isDirectory()) {
                scanDirectory(child, relative, out);
                continue;
            }
            if (attrs.size() < 0 || attrs.size() > MAX_FILE_BYTES) throw new IOException("save file exceeds size limit: " + relative);
            if (out.size() >= MAX_FILES) throw new IOException("snapshot exceeds file-count limit");
            out.add(new Entry(relative, attrs.size(), sha256(child)));
        }
    }

    static void validateEntries(List<Entry> entries) throws IOException {
        if (entries.size() > MAX_FILES) throw new IOException("too many files");
        Set<String> seen = new HashSet<>();
        long total = 0;
        for (Entry entry : entries) {
            validatePath(entry.path);
            if (!seen.add(entry.path)) throw new IOException("duplicate path: " + entry.path);
            if (entry.size < 0 || entry.size > MAX_FILE_BYTES) throw new IOException("invalid size: " + entry.path);
            if (!validSha256(entry.sha256)) throw new IOException("invalid SHA-256: " + entry.path);
            total += entry.size;
            if (total > MAX_TOTAL_BYTES) throw new IOException("snapshot exceeds decoded size limit");
        }
        for (Entry entry : entries) {
            String path = entry.path;
            for (int slash = path.indexOf('/'); slash >= 0; slash = path.indexOf('/', slash + 1)) {
                String ancestor = path.substring(0, slash);
                if (seen.contains(ancestor)) {
                    throw new IOException("file/directory prefix collision: " + ancestor + " and " + entry.path);
                }
            }
        }
    }

    static void validateCloudPath(String path) throws IOException {
        validatePath(path);
        String[] components = path.split("/", -1);
        if (components[0].equals("Settings")) throw new IOException("Settings must stay local");
        for (String component : components) {
            if (excludedComponent(component)) throw new IOException("excluded local path in cloud snapshot");
        }
    }

    static Entry entryFromData(String path, byte[] data) throws IOException {
        validateCloudPath(path);
        if (data.length > MAX_FILE_BYTES) throw new IOException("save file exceeds size limit");
        return new Entry(path, data.length, hex(sha256Digest().digest(data)));
    }

    static void verifyContent(Entry entry, byte[] data) throws IOException {
        if (data.length != entry.size || !hex(sha256Digest().digest(data)).equals(entry.sha256)) {
            throw new IOException("file content verification failed: " + entry.path);
        }
    }

    static void validatePath(String path) throws IOException {
        if (path == null || path.isEmpty() || path.length() > MAX_PATH_UTF16 || path.startsWith("/") || path.endsWith("/")) {
            throw new IOException("invalid save path");
        }
        String[] components = path.split("/", -1);
        if (components.length == 0 || components.length > MAX_PATH_COMPONENTS) throw new IOException("invalid save path depth");
        for (String component : components) {
            if (component.isEmpty() || component.equals(".") || component.equals("..")) throw new IOException("invalid save path component");
            if (component.indexOf('\\') >= 0 || component.indexOf(':') >= 0) throw new IOException("invalid save path character");
            for (int i = 0; i < component.length(); i++) {
                if (Character.isISOControl(component.charAt(i))) throw new IOException("control character in save path");
            }
        }
    }

    static boolean excludedComponent(String name) {
        return !name.isEmpty() && (name.charAt(0) == '.' || name.charAt(0) == '_');
    }

    static String snapshotDigest(List<Entry> entries) throws IOException {
        MessageDigest digest = sha256Digest();
        List<Entry> sorted = new ArrayList<>(entries);
        sorted.sort(Comparator.comparing(entry -> entry.path));
        for (Entry entry : sorted) {
            digest.update(entry.path.getBytes(StandardCharsets.UTF_8));
            digest.update((byte) 0);
            digest.update(Long.toString(entry.size).getBytes(StandardCharsets.US_ASCII));
            digest.update((byte) 0);
            digest.update(entry.sha256.getBytes(StandardCharsets.US_ASCII));
            digest.update((byte) '\n');
        }
        return hex(digest.digest());
    }

    static SyncAction decide(String localHash, String remoteHash, String remoteRevision,
                             boolean baselinePresent, String baselineRevision, String baselineHash) {
        boolean localEmpty = isEmptyHash(localHash);
        boolean remoteEmpty = isEmptyHash(remoteHash);
        // The remote account disappearing after any published version is a conflict, even when
        // the last version was an empty snapshot.
        if (baselinePresent && baselineRevision != null && !baselineRevision.isEmpty()
                && (remoteRevision == null || remoteRevision.isEmpty())) return SyncAction.CONFLICT;
        if (equalsHash(localHash, remoteHash)) return SyncAction.ACK;
        if (!baselinePresent) {
            if (localEmpty) return SyncAction.DOWNLOAD;
            if (remoteEmpty) return SyncAction.UPLOAD;
            return SyncAction.CONFLICT;
        }
        boolean localChanged = !equalsHash(localHash, baselineHash);
        boolean remoteChanged = !equalsHash(remoteHash, baselineHash);
        if (!localChanged) return SyncAction.DOWNLOAD;
        if (!remoteChanged) return SyncAction.UPLOAD;
        return SyncAction.CONFLICT;
    }

    static void copyExcluded(File sourceRoot, File destinationRoot) throws IOException {
        if (!sourceRoot.isDirectory()) throw new IOException("snapshot is not a directory");
        if (!destinationRoot.isDirectory() && !destinationRoot.mkdirs()) throw new IOException("cannot create restore directory");
        copyExcludedChildren(sourceRoot.toPath(), destinationRoot.toPath(), true);
    }

    private static void copyExcludedChildren(Path sourceDirectory, Path destinationDirectory, boolean topLevel) throws IOException {
        List<Path> children = new ArrayList<>();
        try (DirectoryStream<Path> stream = Files.newDirectoryStream(sourceDirectory)) {
            for (Path child : stream) children.add(child);
        }
        children.sort((a, b) -> a.getFileName().toString().compareTo(b.getFileName().toString()));
        for (Path child : children) {
            String name = child.getFileName().toString();
            Path destination = destinationDirectory.resolve(name);
            BasicFileAttributes attrs = Files.readAttributes(child, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
            if (attrs.isSymbolicLink() || (!attrs.isDirectory() && !attrs.isRegularFile())) {
                throw new IOException("unsupported save entry");
            }
            boolean excluded = excludedComponent(name) || (topLevel && name.equals("Settings"));
            if (excluded) {
                if (Files.exists(destination, LinkOption.NOFOLLOW_LINKS)) {
                    throw new IOException("cloud path collides with preserved local entry: " + destination.getFileName());
                }
                copyEntry(child, destination);
            } else if (attrs.isDirectory()) {
                if (Files.exists(destination, LinkOption.NOFOLLOW_LINKS) && !Files.isDirectory(destination, LinkOption.NOFOLLOW_LINKS)) {
                    throw new IOException("preserved local child is blocked by cloud file");
                }
                copyExcludedChildren(child, destination, false);
            }
        }
    }

    private static void copyEntry(Path source, Path destination) throws IOException {
        BasicFileAttributes attrs = Files.readAttributes(source, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
        if (attrs.isSymbolicLink() || (!attrs.isDirectory() && !attrs.isRegularFile())) {
            throw new IOException("unsupported excluded save entry");
        }
        if (attrs.isDirectory()) {
            Files.createDirectories(destination);
            List<Path> children = new ArrayList<>();
            try (DirectoryStream<Path> stream = Files.newDirectoryStream(source)) {
                for (Path child : stream) children.add(child);
            }
            children.sort((a, b) -> a.getFileName().toString().compareTo(b.getFileName().toString()));
            for (Path child : children) copyEntry(child, destination.resolve(child.getFileName().toString()));
        } else {
            Files.createDirectories(destination.getParent());
            Files.copy(source, destination, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.COPY_ATTRIBUTES);
        }
    }

    static void verifyTree(File root, Manifest manifest) throws IOException {
        for (Entry entry : manifest.files) {
            Path path = root.toPath().resolve(entry.path).normalize();
            if (!path.startsWith(root.toPath().normalize())) throw new IOException("path escaped restore tree");
            BasicFileAttributes attrs = Files.readAttributes(path, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
            if (!attrs.isRegularFile() || attrs.size() != entry.size || !sha256(path).equals(entry.sha256)) {
                throw new IOException("restore verification failed: " + entry.path);
            }
        }
    }

    static String sha256(Path path) throws IOException {
        MessageDigest digest = sha256Digest();
        byte[] buffer = new byte[1 << 20];
        try (InputStream input = Files.newInputStream(path)) {
            for (int count; (count = input.read(buffer)) > 0; ) digest.update(buffer, 0, count);
        }
        return hex(digest.digest());
    }

    static boolean validSha256(String value) {
        if (value == null || value.length() != 64) return false;
        for (int i = 0; i < value.length(); i++) {
            char c = value.charAt(i);
            if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
        }
        return true;
    }

    static String hex(byte[] bytes) {
        StringBuilder out = new StringBuilder(bytes.length * 2);
        for (byte value : bytes) out.append(String.format(Locale.ROOT, "%02x", value & 0xff));
        return out.toString();
    }

    private static boolean equalsHash(String left, String right) {
        String a = left == null ? "" : left;
        String b = right == null ? "" : right;
        if (isEmptyHash(a) && isEmptyHash(b)) return true;
        return a.equals(b);
    }

    static boolean isEmptyHash(String hash) {
        return hash == null || hash.isEmpty() || EMPTY_SHA256.equals(hash);
    }

    private static MessageDigest sha256Digest() throws IOException {
        try {
            return MessageDigest.getInstance("SHA-256");
        } catch (NoSuchAlgorithmException error) {
            throw new IOException("SHA-256 unavailable", error);
        }
    }
}
