package com.xiaoboluo.d2r;

import android.app.Activity;
import android.app.AlertDialog;
import android.system.ErrnoException;
import android.system.Os;
import android.system.OsConstants;
import android.text.InputType;
import android.widget.EditText;
import android.widget.Toast;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileDescriptor;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.FilterOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URI;
import java.net.URISyntaxException;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.nio.file.attribute.PosixFilePermission;
import java.nio.file.attribute.PosixFilePermissions;
import java.security.MessageDigest;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Date;
import java.util.EnumSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;

import javax.net.ssl.HttpsURLConnection;

/** Startup gate and background synchronizer for D2R Mobile cloud saves. */
final class CloudSave {
    static final String DEFAULT_ENDPOINT = "https://mythstribe.com/d2r-cloud/v1";
    private static final int CONNECT_TIMEOUT_MS = 15_000;
    private static final int READ_TIMEOUT_MS = 30_000;
    private static final int MAX_STATE_HTTP_BYTES = 256 * 1024;

    static native int nativeSnapshot(String filesRoot, String destination);
    static native int nativeInstall(String filesRoot, String stagedDirectory);

    private final Activity activity;
    private final File filesRoot;
    private final File cloudRoot;
    private final ScheduledExecutorService worker;
    private LaunchCallback launchCallback;
    private AlertDialog dialog;
    private File conflictSnapshot;
    private boolean gateRunning;
    private boolean launchReady;
    private boolean offlineForSession;
    private boolean backgroundPaused;
    private boolean gameRunning;
    private boolean shutdown;
    private volatile String setupEndpoint = DEFAULT_ENDPOINT;
    private ScheduledFuture<?> periodicSync;
    private long stoppedGeneration;

    interface LaunchCallback { void onReady(); }

    CloudSave(Activity activity, File filesRoot) {
        this.activity = activity;
        this.filesRoot = filesRoot.getAbsoluteFile();
        this.cloudRoot = new File(this.filesRoot, "cloud-save");
        this.worker = Executors.newSingleThreadScheduledExecutor(runnable -> {
            Thread thread = new Thread(runnable, "dm-cloud-save");
            thread.setDaemon(true);
            return thread;
        });
    }

    boolean isLaunchReady() {
        return launchReady;
    }

    /** Called only from the Activity main thread. Re-entry joins the existing one-flight gate. */
    void start(LaunchCallback callback) {
        launchCallback = callback;
        if (launchReady) {
            postReady();
            return;
        }
        if (gateRunning || shutdown) return;
        gateRunning = true;
        showConnectingDialog();
        worker.execute(this::evaluateStartup);
    }

    /** Background uploads are enabled only after a clean, explicit startup sync. */
    void onGameStarted() {
        gameRunning = true;
        if (launchReady && !offlineForSession && !backgroundPaused) schedulePeriodicSync();
    }

    /** Ask once for one final committed snapshot; onStop follows nativeFocus(1). */
    void onGameStopped() {
        gameRunning = false;
        final long generation = ++stoppedGeneration;
        if (!launchReady || offlineForSession || backgroundPaused) return;
        worker.schedule(() -> {
            if (generation == stoppedGeneration) backgroundSync(false);
        }, 3, TimeUnit.SECONDS);
    }

    void shutdown() {
        shutdown = true;
        trackBackgroundFuture(null);
        worker.shutdownNow();
        dismissDialog();
    }

    private void evaluateStartup() {
        File localSnapshot = null;
        try {
            Config config = loadPrivateConfig();
            if (config == null) {
                runOnUiThread(this::showTokenSetup);
                return;
            }
            localSnapshot = takeSnapshot();
            CloudSaveRules.Manifest local = CloudSaveRules.scan(localSnapshot);
            RemoteState remote = getState(config);
            Baseline baseline = Baseline.load(scopeFile(config));
            CloudSaveRules.SyncAction action = CloudSaveRules.decide(
                    local.sha256, remote.snapshotSha256, remote.revision,
                    baseline.present, baseline.revision, baseline.snapshotSha256);
            switch (action) {
                case ACK -> {
                    Baseline.save(scopeFile(config), remote.revision, remote.snapshotSha256);
                    finishReady(localSnapshot);
                }
                case UPLOAD -> {
                    RemoteState uploaded = uploadSnapshot(config, remote.revision, localSnapshot, local);
                    Baseline.save(scopeFile(config), uploaded.revision, uploaded.snapshotSha256);
                    finishReady(localSnapshot);
                }
                case DOWNLOAD -> {
                    InstallResult installed = downloadAndInstall(config, remote, localSnapshot);
                    Baseline.save(scopeFile(config), remote.revision, installed.state.snapshotSha256);
                    finishReady(localSnapshot);
                }
                case CONFLICT -> keepForConflict(localSnapshot, local, remote);
            }
        } catch (ConfigException error) {
            deleteSnapshot(localSnapshot);
            runOnUiThread(() -> showError("云存档配置无效：" + safeMessage(error), true));
        } catch (ConflictException error) {
            File snapshot = localSnapshot;
            try {
                CloudSaveRules.Manifest local = CloudSaveRules.scan(snapshot);
                keepForConflict(snapshot, local, error.state);
            } catch (IOException scanError) {
                deleteSnapshot(snapshot);
                runOnUiThread(() -> showError("无法校验本地存档：" + safeMessage(scanError), false));
            }
        } catch (AuthenticationException error) {
            deleteSnapshot(localSnapshot);
            runOnUiThread(() -> showError("云存档访问令牌无效或已失效。", true));
        } catch (IOException | RuntimeException error) {
            deleteSnapshot(localSnapshot);
            runOnUiThread(() -> showError("无法检查云存档：" + safeMessage(error), false));
        }
    }

    private void keepForConflict(File snapshot, CloudSaveRules.Manifest local, RemoteState remote) {
        if (conflictSnapshot != null && !conflictSnapshot.equals(snapshot)) deleteSnapshot(conflictSnapshot);
        conflictSnapshot = snapshot;
        runOnUiThread(() -> showConflict(local, remote));
    }

    private void applyCloudChoice(File localSnapshot, String shownRevision, String shownHash) {
        try {
            Config config = requireConfig();
            RemoteState latest = getState(config);
            if (!latest.revision.equals(shownRevision) || !latest.snapshotSha256.equals(shownHash)) {
                CloudSaveRules.Manifest local = CloudSaveRules.scan(localSnapshot);
                keepForConflict(localSnapshot, local, latest);
                return;
            }
            if (latest.revision.isEmpty()) {
                throw new ConflictException(latest);
            }
            InstallResult result = downloadAndInstall(config, latest, localSnapshot);
            Baseline.save(scopeFile(config), result.state.revision, result.state.snapshotSha256);
            finishReady(localSnapshot);
        } catch (ConflictException error) {
            try {
                CloudSaveRules.Manifest local = CloudSaveRules.scan(localSnapshot);
                keepForConflict(localSnapshot, local, error.state);
            } catch (IOException scanError) {
                failGate("无法继续云存档冲突处理：" + safeMessage(scanError));
            }
        } catch (AuthenticationException error) {
            failGate("云存档访问令牌无效或已失效。");
        } catch (IOException | RuntimeException error) {
            failGate("下载云存档失败：" + safeMessage(error));
        }
    }

    private void applyLocalChoice(File localSnapshot, String shownRevision, String shownHash) {
        try {
            Config config = requireConfig();
            CloudSaveRules.Manifest local = CloudSaveRules.scan(localSnapshot);
            RemoteState latest = getState(config);
            if (!latest.revision.equals(shownRevision) || !latest.snapshotSha256.equals(shownHash)) {
                keepForConflict(localSnapshot, local, latest);
                return;
            }
            RemoteState uploaded = uploadSnapshot(config, latest.revision, localSnapshot, local);
            Baseline.save(scopeFile(config), uploaded.revision, uploaded.snapshotSha256);
            finishReady(localSnapshot);
        } catch (ConflictException error) {
            try {
                CloudSaveRules.Manifest local = CloudSaveRules.scan(localSnapshot);
                keepForConflict(localSnapshot, local, error.state);
            } catch (IOException scanError) {
                failGate("无法继续云存档冲突处理：" + safeMessage(scanError));
            }
        } catch (AuthenticationException error) {
            failGate("云存档访问令牌无效或已失效。");
        } catch (IOException | RuntimeException error) {
            failGate("上传本地存档失败：" + safeMessage(error));
        }
    }

    private void failGate(String message) {
        gateRunning = false;
        runOnUiThread(() -> showError(message, false));
    }

    private void finishReady(File snapshot) {
        deleteSnapshot(snapshot);
        if (snapshot == conflictSnapshot) {
            conflictSnapshot = null;
        }
        backgroundPaused = false;
        offlineForSession = false;
        gateRunning = false;
        launchReady = true;
        runOnUiThread(() -> {
            dismissDialog();
            postReady();
            if (gameRunning) schedulePeriodicSync();
        });
    }

    private void chooseOffline() {
        deleteSnapshot(conflictSnapshot);
        conflictSnapshot = null;
        backgroundPaused = true;
        offlineForSession = true;
        gateRunning = false;
        launchReady = true;
        trackBackgroundFuture(null);
        runOnUiThread(() -> {
            dismissDialog();
            postReady();
        });
    }

    private void postReady() {
        runOnUiThread(() -> {
            LaunchCallback callback = launchCallback;
            if (callback != null) callback.onReady();
        });
    }

    private void schedulePeriodicSync() {
        if (periodicSync != null || shutdown || offlineForSession || backgroundPaused) return;
        periodicSync = worker.scheduleWithFixedDelay(() -> backgroundSync(false), 30, 30, TimeUnit.SECONDS);
    }

    private void trackBackgroundFuture(ScheduledFuture<?> future) {
        if (periodicSync != null) periodicSync.cancel(false);
        periodicSync = future;
    }

    private void backgroundSync(boolean urgent) {
        if (shutdown || offlineForSession || backgroundPaused || !launchReady) return;
        File snapshot = null;
        try {
            Config config = loadPrivateConfig();
            if (config == null) return;
            Baseline baseline = Baseline.load(scopeFile(config));
            if (!baseline.present) return;
            snapshot = takeSnapshot();
            CloudSaveRules.Manifest local = CloudSaveRules.scan(snapshot);
            if (local.sha256.equals(baseline.snapshotSha256)
                    || (CloudSaveRules.isEmptyHash(local.sha256) && CloudSaveRules.isEmptyHash(baseline.snapshotSha256))) return;
            RemoteState uploaded = uploadSnapshot(config, baseline.revision, snapshot, local);
            Baseline.save(scopeFile(config), uploaded.revision, uploaded.snapshotSha256);
        } catch (ConflictException error) {
            // Unknown remote changes pause all uploads until the next startup presents a conflict.
            backgroundPaused = true;
            trackBackgroundFuture(null);
        } catch (AuthenticationException error) {
            backgroundPaused = true;
            trackBackgroundFuture(null);
        } catch (IOException | RuntimeException error) {
            // Keep committed local saves; the next periodic attempt retries from the acknowledged baseline.
        } finally {
            deleteSnapshot(snapshot);
        }
    }

    private Config requireConfig() throws IOException {
        Config config = loadPrivateConfig();
        if (config == null) throw new ConfigException("缺少访问令牌");
        return config;
    }

    private Config loadPrivateConfig() throws IOException {
        File privateConfig = new File(cloudRoot, "config.json");
        if (privateConfig.isFile()) {
            Config config = Config.parse(readSmallFile(privateConfig), privateConfig.getAbsolutePath());
            setupEndpoint = config.endpoint;
            return config.token.isEmpty() ? null : config;
        }
        try (InputStream input = activity.getAssets().open("cloud-save.json")) {
            Config config = Config.parse(readLimited(input, MAX_STATE_HTTP_BYTES), "assets/cloud-save.json");
            setupEndpoint = config.endpoint;
            if (config.token.isEmpty()) return null;
            saveConfig(config);
            return config;
        } catch (java.io.FileNotFoundException missing) {
            return null;
        }
    }

    private void saveConfig(Config config) throws IOException {
        ensureCloudRoot();
        JSONObject json = new JSONObject();
        try {
            json.put("endpoint", config.endpoint);
            json.put("token", config.token);
        } catch (JSONException error) {
            throw new IOException("cannot encode configuration", error);
        }
        atomicWrite(new File(cloudRoot, "config.json"), (json.toString() + "\n").getBytes(StandardCharsets.UTF_8), true);
    }

    private File scopeFile(Config config) throws IOException {
        MessageDigest digest;
        try {
            digest = MessageDigest.getInstance("SHA-256");
        } catch (java.security.NoSuchAlgorithmException error) {
            throw new IOException("SHA-256 unavailable", error);
        }
        digest.update(config.endpoint.getBytes(StandardCharsets.UTF_8));
        digest.update((byte) 0);
        digest.update(config.token.getBytes(StandardCharsets.UTF_8));
        return new File(cloudRoot, "baseline-" + CloudSaveRules.hex(digest.digest()) + ".json");
    }

    private File takeSnapshot() throws IOException {
        ensureCloudRoot();
        File destination = unique(cloudRoot, "snapshot-");
        int result;
        try {
            result = nativeSnapshot(filesRoot.getAbsolutePath(), destination.getAbsolutePath());
        } catch (UnsatisfiedLinkError error) {
            throw new IOException("当前 libdm 不支持云存档快照", error);
        }
        if (result != 0) throw new IOException("本地存档快照失败（errno " + (-result) + "）");
        if (!destination.isDirectory()) throw new IOException("本地存档快照目录不存在");
        return destination;
    }

    private RemoteState getState(Config config) throws IOException {
        JSONObject json = requestJson(config, "GET", "/state", null, MAX_STATE_HTTP_BYTES);
        return RemoteState.parse(json);
    }

    private RemoteState uploadSnapshot(Config config, String baseRevision, File snapshot,
                                       CloudSaveRules.Manifest manifest) throws IOException {
        File body = unique(cloudRoot, "upload-");
        try {
            writeUploadBody(body, baseRevision, snapshot, manifest);
            long length = body.length();
            if (length > CloudSaveRules.MAX_HTTP_BYTES) throw new IOException("上传请求超过 HTTP 大小上限");
            HttpsURLConnection connection = connect(config, "PUT", "/snapshot");
            connection.setDoOutput(true);
            connection.setFixedLengthStreamingMode(length);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            try (OutputStream output = connection.getOutputStream()) { Files.copy(body.toPath(), output); }
            JSONObject response = readJsonResponse(connection, CloudSaveRules.MAX_HTTP_BYTES);
            RemoteState uploaded = RemoteState.parse(response);
            if (!uploaded.snapshotSha256.equals(manifest.sha256)
                    && !(CloudSaveRules.isEmptyHash(uploaded.snapshotSha256) && CloudSaveRules.isEmptyHash(manifest.sha256))) {
                throw new IOException("服务端确认的存档摘要不一致");
            }
            return uploaded;
        } finally {
            deleteSnapshot(body);
        }
    }

    private static void writeUploadBody(File body, String baseRevision, File snapshot,
                                        CloudSaveRules.Manifest manifest) throws IOException {
        try (OutputStream output = new FileOutputStream(body)) {
            output.write("{\"base_revision\":".getBytes(StandardCharsets.UTF_8));
            output.write(JSONObject.quote(baseRevision).getBytes(StandardCharsets.UTF_8));
            output.write(",\"device\":\"Android\",\"snapshot_sha256\":".getBytes(StandardCharsets.UTF_8));
            output.write(JSONObject.quote(manifest.sha256).getBytes(StandardCharsets.UTF_8));
            output.write(",\"files\":[".getBytes(StandardCharsets.UTF_8));
            for (int i = 0; i < manifest.files.size(); i++) {
                if (i != 0) output.write(',');
                CloudSaveRules.Entry entry = manifest.files.get(i);
                output.write("{\"path\":".getBytes(StandardCharsets.UTF_8));
                output.write(JSONObject.quote(entry.path).getBytes(StandardCharsets.UTF_8));
                output.write((",\"size\":" + entry.size + ",\"sha256\":").getBytes(StandardCharsets.UTF_8));
                output.write(JSONObject.quote(entry.sha256).getBytes(StandardCharsets.UTF_8));
                output.write(",\"data\":\"".getBytes(StandardCharsets.UTF_8));
                Path source = snapshot.toPath().resolve(entry.path).normalize();
                if (!source.startsWith(snapshot.toPath().normalize())) throw new IOException("save path escaped snapshot");
                try (OutputStream encoded = Base64.getEncoder().wrap(new NonClosingOutputStream(output))) {
                    Files.copy(source, encoded);
                }
                output.write("\"}".getBytes(StandardCharsets.UTF_8));
            }
            output.write("]}".getBytes(StandardCharsets.UTF_8));
        }
    }

    private InstallResult downloadAndInstall(Config config, RemoteState expected, File localSnapshot) throws IOException {
        String suffix = "/snapshot?revision=" + percentEncode(expected.revision);
        JSONObject json = requestJson(config, "GET", suffix, null, CloudSaveRules.MAX_HTTP_BYTES);
        RemoteSnapshot remote = parseRemoteSnapshot(json);
        if (!remote.state.revision.equals(expected.revision) || !remote.state.snapshotSha256.equals(expected.snapshotSha256)) {
            throw new ConflictException(remote.state);
        }
        if (!remote.manifest.sha256.equals(remote.state.snapshotSha256)) throw new IOException("云存档快照摘要不匹配");
        File stage = unique(cloudRoot, "restore-");
        boolean installing = false;
        try {
            if (!stage.mkdirs()) throw new IOException("无法创建恢复目录");
            writeRemoteFiles(remote, stage);
            CloudSaveRules.copyExcluded(localSnapshot, stage);
            CloudSaveRules.verifyTree(stage, remote.manifest);
            installing = true;
            int result;
            try {
                result = nativeInstall(filesRoot.getAbsolutePath(), stage.getAbsolutePath());
            } catch (UnsatisfiedLinkError error) {
                throw new IOException("当前 libdm 不支持云存档安装", error);
            }
            if (result != 0) throw new IOException("本地存档安装失败（errno " + (-result) + "）");
            return new InstallResult(remote.state, stage);
        } catch (IOException | RuntimeException error) {
            // After nativeInstall was entered, the exchange may already have happened even if fsync failed.
            if (!installing) deleteTree(stage);
            throw error;
        }
    }

    private RemoteSnapshot parseRemoteSnapshot(JSONObject json) throws IOException {
        RemoteState state = RemoteState.parse(json);
        JSONArray array;
        try {
            array = json.getJSONArray("files");
        } catch (JSONException error) {
            throw new IOException("云存档缺少 files", error);
        }
        if (array.length() > CloudSaveRules.MAX_FILES) throw new IOException("too many cloud save files");
        List<RemoteFile> files = new ArrayList<>();
        long total = 0;
        String previousPath = null;
        for (int i = 0; i < array.length(); i++) {
            JSONObject item;
            try {
                item = array.getJSONObject(i);
                String path = item.getString("path");
                long size = item.getLong("size");
                String sha = item.getString("sha256");
                CloudSaveRules.validateCloudPath(path);
                if (previousPath != null && previousPath.compareTo(path) >= 0) throw new IOException("cloud files are not sorted by UTF-16 path");
                previousPath = path;
                if (!CloudSaveRules.validSha256(sha)) throw new IOException("invalid file SHA-256");
                if (size < 0 || size > CloudSaveRules.MAX_FILE_BYTES) throw new IOException("invalid file size");
                String data = item.getString("data");
                long encodedSize = ((size + 2) / 3) * 4;
                if (data.length() != encodedSize) throw new IOException("base64 size mismatch: " + path);
                byte[] decoded = decodePaddedBase64(data, path);
                CloudSaveRules.Entry entry = new CloudSaveRules.Entry(path, size, sha);
                CloudSaveRules.verifyContent(entry, decoded);
                total += size;
                if (total > CloudSaveRules.MAX_TOTAL_BYTES) throw new IOException("snapshot exceeds decoded size limit");
                files.add(new RemoteFile(entry, decoded));
            } catch (JSONException error) {
                throw new IOException("invalid cloud snapshot file", error);
            }
        }
        if (files.size() > CloudSaveRules.MAX_FILES) throw new IOException("too many cloud save files");
        List<CloudSaveRules.Entry> entries = new ArrayList<>();
        for (RemoteFile file : files) entries.add(file.entry);
        CloudSaveRules.validateEntries(entries);
        String digest = CloudSaveRules.snapshotDigest(entries);
        CloudSaveRules.Manifest manifest = new CloudSaveRules.Manifest(entries, digest, total);
        if (state.fileCount != manifest.files.size() || state.totalBytes != manifest.totalBytes) {
            throw new IOException("cloud state counts do not match snapshot");
        }
        return new RemoteSnapshot(state, manifest, files);
    }

    private void writeRemoteFiles(RemoteSnapshot remote, File stage) throws IOException {
        Path root = stage.toPath().normalize();
        for (RemoteFile file : remote.files) {
            Path destination = root.resolve(file.entry.path).normalize();
            if (!destination.startsWith(root)) throw new IOException("cloud path escaped restore directory");
            Files.createDirectories(destination.getParent());
            Files.write(destination, file.data, StandardOpenOption.CREATE_NEW, StandardOpenOption.WRITE);
        }
    }

    private JSONObject requestJson(Config config, String method, String suffix, byte[] body, long limit) throws IOException {
        HttpsURLConnection connection = connect(config, method, suffix);
        if (body != null) {
            connection.setDoOutput(true);
            connection.setFixedLengthStreamingMode(body.length);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            try (OutputStream output = connection.getOutputStream()) { output.write(body); }
        }
        return readJsonResponse(connection, limit);
    }

    private HttpsURLConnection connect(Config config, String method, String suffix) throws IOException {
        URL url = new URL(config.endpoint + suffix);
        HttpURLConnection raw = (HttpURLConnection) url.openConnection();
        if (!(raw instanceof HttpsURLConnection)) {
            raw.disconnect();
            throw new IOException("云存档端点必须使用 HTTPS");
        }
        HttpsURLConnection connection = (HttpsURLConnection) raw;
        // Keep the platform trust manager and hostname verifier; never install a permissive fallback.
        connection.setSSLSocketFactory(HttpsURLConnection.getDefaultSSLSocketFactory());
        connection.setHostnameVerifier(HttpsURLConnection.getDefaultHostnameVerifier());
        connection.setInstanceFollowRedirects(false);
        connection.setUseCaches(false);
        connection.setConnectTimeout(CONNECT_TIMEOUT_MS);
        connection.setReadTimeout(READ_TIMEOUT_MS);
        connection.setRequestMethod(method);
        connection.setRequestProperty("User-Agent", "D2RMobile-Cloud/1");
        connection.setRequestProperty("Authorization", "Bearer " + config.token);
        connection.setRequestProperty("Accept", "application/json");
        connection.setRequestProperty("Accept-Encoding", "identity");
        connection.setRequestProperty("Cache-Control", "no-store");
        return connection;
    }

    private JSONObject readJsonResponse(HttpsURLConnection connection, long limit) throws IOException {
        int status;
        try {
            status = connection.getResponseCode();
        } catch (IOException error) {
            connection.disconnect();
            throw error;
        }
        if (status >= 300 && status < 400) {
            connection.disconnect();
            throw new IOException("云存档服务返回重定向，已拒绝携带凭据继续访问");
        }
        long declaredLength = connection.getContentLengthLong();
        if (declaredLength > limit) {
            connection.disconnect();
            throw new IOException("HTTP body exceeds limit");
        }
        InputStream stream = status >= 400 ? connection.getErrorStream() : connection.getInputStream();
        byte[] bytes;
        try {
            bytes = stream == null ? new byte[0] : readLimited(stream, limit);
        } finally {
            if (stream != null) stream.close();
            connection.disconnect();
        }
        JSONObject json = null;
        if (bytes.length != 0) {
            try {
                json = new JSONObject(new String(bytes, StandardCharsets.UTF_8));
            } catch (JSONException error) {
                if (status < 400) throw new IOException("云存档服务返回了无效 JSON", error);
            }
        }
        if (status == 401) throw new AuthenticationException();
        if (status == 409 && json != null) {
            try {
                throw new ConflictException(RemoteState.parse(json.getJSONObject("state")));
            } catch (JSONException error) {
                throw new IOException("云存档冲突响应无效", error);
            }
        }
        if (status < 200 || status >= 300) throw new HttpStatusException(status, json);
        if (json == null) throw new IOException("云存档服务返回空响应");
        return json;
    }

    private File unique(File parent, String prefix) throws IOException {
        ensureCloudRoot();
        String stamp = new SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US).format(new Date());
        for (int attempt = 0; attempt < 100; attempt++) {
            File candidate = new File(parent, prefix + stamp + "-" + UUID.randomUUID());
            if (!candidate.exists()) return candidate;
        }
        throw new IOException("无法分配云存档临时目录");
    }

    private void ensureCloudRoot() throws IOException {
        if (Files.isSymbolicLink(cloudRoot.toPath())) throw new IOException("云存档目录不能是符号链接");
        if (!cloudRoot.isDirectory() && !cloudRoot.mkdirs()) throw new IOException("无法创建云存档目录");
    }

    private static void atomicWrite(File destination, byte[] bytes, boolean privateFile) throws IOException {
        File parent = destination.getParentFile();
        if (parent == null || (!parent.isDirectory() && !parent.mkdirs())) throw new IOException("cannot create state directory");
        File temporary = new File(parent, destination.getName() + ".tmp-" + UUID.randomUUID());
        boolean moved = false;
        try {
            try (FileOutputStream output = new FileOutputStream(temporary)) {
                output.write(bytes);
                output.flush();
                output.getFD().sync();
            }
            if (privateFile) setPrivate(temporary.toPath());
            try {
                Files.move(temporary.toPath(), destination.toPath(), StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            } catch (java.nio.file.AtomicMoveNotSupportedException error) {
                throw new IOException("文件系统不支持原子状态更新", error);
            }
            moved = true;
            setPrivate(destination.toPath());
            fsyncDirectory(parent);
        } finally {
            if (!moved) Files.deleteIfExists(temporary.toPath());
        }
    }

    private static void fsyncDirectory(File directory) throws IOException {
        FileDescriptor descriptor = null;
        try {
            descriptor = Os.open(directory.getAbsolutePath(), OsConstants.O_RDONLY | OsConstants.O_CLOEXEC, 0);
            Os.fsync(descriptor);
        } catch (ErrnoException error) {
            throw new IOException("无法持久化原子状态目录", error);
        } finally {
            if (descriptor != null) {
                try {
                    Os.close(descriptor);
                } catch (ErrnoException ignored) {}
            }
        }
    }

    private static void setPrivate(Path path) {
        try {
            Set<PosixFilePermission> permissions = EnumSet.of(PosixFilePermission.OWNER_READ, PosixFilePermission.OWNER_WRITE);
            Files.setPosixFilePermissions(path, permissions);
        } catch (IOException | UnsupportedOperationException ignored) {
            // Android app-private storage still prevents other apps from reading this file.
        }
    }

    private void deleteSnapshot(File snapshot) {
        if (snapshot == null) return;
        try {
            Path root = cloudRoot.getCanonicalFile().toPath();
            Path target = snapshot.getCanonicalFile().toPath();
            if (target.startsWith(root) && !target.equals(root)) deleteTree(snapshot);
        } catch (IOException ignored) {}
    }

    private static void deleteTree(File file) throws IOException {
        if (!file.exists()) return;
        if (Files.isSymbolicLink(file.toPath())) throw new IOException("refusing to delete symlink");
        if (file.isDirectory()) {
            File[] children = file.listFiles();
            if (children == null) throw new IOException("cannot list temporary directory");
            for (File child : children) deleteTree(child);
        }
        if (!file.delete()) throw new IOException("cannot delete temporary file");
    }

    private static byte[] readSmallFile(File file) throws IOException {
        try (InputStream input = new FileInputStream(file)) { return readLimited(input, MAX_STATE_HTTP_BYTES); }
    }

    private static byte[] readLimited(InputStream input, long limit) throws IOException {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        byte[] buffer = new byte[1 << 16];
        long total = 0;
        for (int count; (count = input.read(buffer)) > 0; ) {
            total += count;
            if (total > limit) throw new IOException("HTTP body exceeds limit");
            output.write(buffer, 0, count);
        }
        return output.toByteArray();
    }

    private static byte[] decodePaddedBase64(String value, String path) throws IOException {
        if (value.length() % 4 != 0) throw new IOException("base64 padding mismatch: " + path);
        int padding = value.endsWith("==") ? 2 : value.endsWith("=") ? 1 : 0;
        for (int i = 0; i < value.length() - padding; i++) {
            char c = value.charAt(i);
            if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '+' || c == '/')) {
                throw new IOException("invalid base64: " + path);
            }
        }
        byte[] decoded = Base64.getDecoder().decode(value);
        if (!Base64.getEncoder().encodeToString(decoded).equals(value)) throw new IOException("non-canonical base64: " + path);
        return decoded;
    }

    private static String percentEncode(String value) {
        StringBuilder out = new StringBuilder();
        for (byte element : value.getBytes(StandardCharsets.UTF_8)) {
            int b = element & 0xff;
            if ((b >= 'a' && b <= 'z') || (b >= 'A' && b <= 'Z') || (b >= '0' && b <= '9') || b == '-' || b == '.' || b == '_' || b == '~') {
                out.append((char) b);
            } else {
                out.append('%').append(String.format(Locale.ROOT, "%02X", b));
            }
        }
        return out.toString();
    }

    private static String safeMessage(Throwable error) {
        String message = error.getMessage();
        if (message == null || message.isEmpty()) return error.getClass().getSimpleName();
        message = message.replace('\n', ' ').replace('\r', ' ');
        return message.length() > 180 ? message.substring(0, 180) : message;
    }

    private void showConnectingDialog() {
        runOnUiThread(() -> {
            if (!canShow()) return;
            dismissDialog();
            dialog = new AlertDialog.Builder(activity)
                    .setTitle("检查云存档")
                    .setMessage("正在校验本地与云端存档，请稍候…")
                    .setCancelable(false)
                    .create();
            dialog.show();
        });
    }

    private void showTokenSetup() {
        if (!canShow()) return;
        dismissDialog();
        EditText input = new EditText(activity);
        input.setSingleLine(true);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD);
        input.setHint("访问令牌（密码）");
        AlertDialog.Builder builder = new AlertDialog.Builder(activity)
                .setTitle("设置云存档")
                .setMessage("端点：" + setupEndpoint + "\n\n请输入云存档访问令牌。令牌只保存在本机 App 私有目录，不会显示在日志或链接中。")
                .setView(input)
                .setCancelable(false)
                .setPositiveButton("保存并连接", null)
                .setNegativeButton("离线启动", (d, which) -> chooseOffline());
        dialog = builder.create();
        dialog.setOnShowListener(ignored -> dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
            String token = input.getText().toString().trim();
            if (token.isEmpty()) {
                input.setError("请输入访问令牌");
                return;
            }
            try {
                saveConfig(new Config(setupEndpoint, token));
                dismissDialog();
                gateRunning = true;
                showConnectingDialog();
                worker.execute(this::evaluateStartup);
            } catch (IOException error) {
                input.setError("保存配置失败：" + safeMessage(error));
            }
        }));
        dialog.show();
    }

    private void showError(String message, boolean authFailure) {
        gateRunning = false;
        if (!canShow()) return;
        dismissDialog();
        AlertDialog.Builder builder = new AlertDialog.Builder(activity)
                .setTitle(authFailure ? "云存档认证失败" : "云存档暂时不可用")
                .setMessage(message + "\n\n可以重试，或本次明确离线启动。离线启动不会改变同步基线，也不会在后台上传存档。")
                .setCancelable(false)
                .setPositiveButton("重试", (d, which) -> retryGate())
                .setNegativeButton("离线启动", (d, which) -> chooseOffline());
        if (authFailure) builder.setNeutralButton("设置访问令牌", (d, which) -> showTokenSetup());
        dialog = builder.create();
        dialog.show();
    }

    private void retryGate() {
        if (gateRunning) return;
        gateRunning = true;
        showConnectingDialog();
        worker.execute(this::evaluateStartup);
    }

    private void showConflict(CloudSaveRules.Manifest local, RemoteState remote) {
        if (!canShow()) return;
        dismissDialog();
        String update = printable(remote.updatedAt, "未知");
        String device = printable(remote.device, "未知设备");
        String message = "本地存档：" + local.files.size() + " 个文件，" + formatBytes(local.totalBytes) + "。\n"
                + "云端存档：" + remote.fileCount + " 个文件，" + formatBytes(remote.totalBytes)
                + "；更新于 " + update + "，设备 " + device + "。\n\n"
                + "两端存档不一致，系统不会按时间自动选择。云存档覆盖本地时，旧本地存档会完整保留在本机 cloud-save/restore-* 目录；"
                + "本地覆盖云端时，服务端旧版本仍保留在历史中。";
        String shownRevision = remote.revision;
        String shownHash = remote.snapshotSha256;
        AlertDialog.Builder builder = new AlertDialog.Builder(activity)
                .setTitle("发现云存档冲突")
                .setMessage(message)
                .setCancelable(false)
                .setPositiveButton("云存档覆盖本地", (d, which) -> {
                    showConnectingDialog();
                    gateRunning = true;
                    File snapshot = conflictSnapshot;
                    worker.execute(() -> applyCloudChoice(snapshot, shownRevision, shownHash));
                })
                .setNegativeButton("本地存档覆盖云端", (d, which) -> {
                    showConnectingDialog();
                    gateRunning = true;
                    File snapshot = conflictSnapshot;
                    worker.execute(() -> applyLocalChoice(snapshot, shownRevision, shownHash));
                })
                .setNeutralButton("暂时离线", (d, which) -> chooseOffline());
        dialog = builder.create();
        dialog.show();
    }

    private void dismissDialog() {
        AlertDialog current = dialog;
        dialog = null;
        if (current != null && current.isShowing()) current.dismiss();
    }

    private boolean canShow() {
        return !shutdown && !activity.isFinishing() && !activity.isDestroyed();
    }

    private void runOnUiThread(Runnable runnable) {
        activity.runOnUiThread(() -> { if (!shutdown && !activity.isFinishing() && !activity.isDestroyed()) runnable.run(); });
    }

    private static String printable(String value, String fallback) {
        if (value == null || value.isEmpty()) return fallback;
        StringBuilder out = new StringBuilder();
        for (int i = 0; i < value.length() && out.length() < 80; i++) {
            char c = value.charAt(i);
            if (!Character.isISOControl(c)) out.append(c);
        }
        return out.length() == 0 ? fallback : out.toString();
    }

    private static String formatBytes(long bytes) {
        return String.format(Locale.CHINA, "%.1f MiB", bytes / 1048576.0);
    }

    private static final class NonClosingOutputStream extends FilterOutputStream {
        NonClosingOutputStream(OutputStream output) { super(output); }
        @Override public void close() throws IOException { flush(); }
    }

    private static final class Config {
        final String endpoint;
        final String token;

        Config(String endpoint, String token) {
            this.endpoint = endpoint;
            this.token = token;
        }

        static Config parse(byte[] bytes, String source) throws ConfigException {
            try {
                JSONObject json = new JSONObject(new String(bytes, StandardCharsets.UTF_8));
                String endpoint = json.optString("endpoint", DEFAULT_ENDPOINT).trim();
                String token = json.optString("token", "").trim();
                if (endpoint.isEmpty()) endpoint = DEFAULT_ENDPOINT;
                if (!endpoint.endsWith("/")) endpoint += "/";
                endpoint = endpoint.substring(0, endpoint.length() - 1);
                validateEndpoint(endpoint, source);
                for (int i = 0; i < token.length(); i++) {
                    if (Character.isISOControl(token.charAt(i))) throw new ConfigException(source + " 的 token 包含非法字符");
                }
                return new Config(endpoint, token);
            } catch (JSONException error) {
                throw new ConfigException("无法解析 " + source);
            }
        }

        private static void validateEndpoint(String endpoint, String source) throws ConfigException {
            try {
                URI uri = new URI(endpoint);
                if (!"https".equalsIgnoreCase(uri.getScheme()) || uri.getHost() == null || uri.getHost().isEmpty()
                        || uri.getRawUserInfo() != null || uri.getRawQuery() != null || uri.getRawFragment() != null) {
                    throw new ConfigException(source + " 的 endpoint 必须是 HTTPS 地址");
                }
            } catch (URISyntaxException error) {
                throw new ConfigException(source + " 的 endpoint 无效");
            }
        }
    }

    private static final class Baseline {
        final boolean present;
        final String revision;
        final String snapshotSha256;

        Baseline(boolean present, String revision, String snapshotSha256) {
            this.present = present;
            this.revision = revision == null ? "" : revision;
            this.snapshotSha256 = snapshotSha256 == null ? "" : snapshotSha256;
        }

        static Baseline load(File file) throws IOException {
            if (!file.isFile()) return new Baseline(false, "", "");
            try {
                JSONObject json = new JSONObject(new String(readSmallFile(file), StandardCharsets.UTF_8));
                String revision = json.optString("revision", "");
                String hash = json.optString("snapshot_sha256", "");
                if (!hash.isEmpty() && !CloudSaveRules.validSha256(hash)) return new Baseline(false, "", "");
                return new Baseline(true, revision, hash);
            } catch (JSONException error) {
                // A torn or old baseline is recoverable by comparing verified content next launch.
                return new Baseline(false, "", "");
            }
        }

        static void save(File file, String revision, String snapshotSha256) throws IOException {
            JSONObject json = new JSONObject();
            try {
                json.put("acknowledged", true);
                json.put("revision", revision == null ? "" : revision);
                json.put("snapshot_sha256", snapshotSha256 == null ? "" : snapshotSha256);
            } catch (JSONException error) {
                throw new IOException("cannot encode baseline", error);
            }
            atomicWrite(file, (json.toString() + "\n").getBytes(StandardCharsets.UTF_8), true);
        }
    }

    private static final class RemoteState {
        final String revision;
        final String snapshotSha256;
        final String updatedAt;
        final String device;
        final int fileCount;
        final long totalBytes;

        RemoteState(String revision, String snapshotSha256, String updatedAt, String device, int fileCount, long totalBytes) {
            this.revision = revision;
            this.snapshotSha256 = snapshotSha256;
            this.updatedAt = updatedAt;
            this.device = device;
            this.fileCount = fileCount;
            this.totalBytes = totalBytes;
        }

        static RemoteState parse(JSONObject json) throws IOException {
            String revision = json.optString("revision", "");
            String hash = json.optString("snapshot_sha256", "");
            int count = json.optInt("file_count", 0);
            long total = json.optLong("total_bytes", 0);
            boolean unused = revision.isEmpty();
            boolean hashValid = CloudSaveRules.isEmptyHash(hash) || CloudSaveRules.validSha256(hash);
            if (!hashValid || (unused && (!hash.isEmpty() || count != 0 || total != 0))
                    || (!unused && !CloudSaveRules.validSha256(hash)) || count < 0 || count > CloudSaveRules.MAX_FILES
                    || total < 0 || total > CloudSaveRules.MAX_TOTAL_BYTES || (count == 0 && total != 0)) {
                throw new IOException("invalid cloud state");
            }
            return new RemoteState(revision, hash, json.optString("updated_at", ""),
                    json.optString("device", ""), count, total);
        }
    }

    private static final class RemoteSnapshot {
        final RemoteState state;
        final CloudSaveRules.Manifest manifest;
        final List<RemoteFile> files;

        RemoteSnapshot(RemoteState state, CloudSaveRules.Manifest manifest, List<RemoteFile> files) {
            this.state = state;
            this.manifest = manifest;
            this.files = files;
        }
    }

    private static final class RemoteFile {
        final CloudSaveRules.Entry entry;
        final byte[] data;

        RemoteFile(CloudSaveRules.Entry entry, byte[] data) {
            this.entry = entry;
            this.data = data;
        }
    }

    private static final class InstallResult {
        final RemoteState state;
        final File preservedRestore;

        InstallResult(RemoteState state, File preservedRestore) {
            this.state = state;
            this.preservedRestore = preservedRestore;
        }
    }

    private static class ConfigException extends IOException {
        private static final long serialVersionUID = 1L;
        ConfigException(String message) { super(message); }
    }

    private static final class AuthenticationException extends IOException {
        private static final long serialVersionUID = 1L;
    }

    private static final class ConflictException extends IOException {
        private static final long serialVersionUID = 1L;
        final transient RemoteState state;
        ConflictException(RemoteState state) { this.state = state; }
    }

    private static final class HttpStatusException extends IOException {
        private static final long serialVersionUID = 1L;
        HttpStatusException(int status, JSONObject body) {
            super("云存档服务返回 HTTP " + status + statusDetail(body));
        }

        private static String statusDetail(JSONObject body) {
            if (body == null) return "";
            String error = body.optString("error", "");
            return error.isEmpty() ? "" : "（" + printable(error, "") + "）";
        }
    }
}
