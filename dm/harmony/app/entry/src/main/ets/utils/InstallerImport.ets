// Fresh-install resource import used by the local installer helper.
//
// The helper writes a request and a ZIP into the application cache while the app is stopped. This
// module verifies and publishes that request before the normal runtime-pack preparation and boot.
// It only accepts a completely fresh runtime layout and never removes an existing save or user file.

import { common } from '@kit.AbilityKit';
import { fileIo, hash } from '@kit.CoreFileKit';
import { zlib } from '@kit.BasicServicesKit';
import { hilog } from '@kit.PerformanceAnalysisKit';

const TAG: string = 'XblInstall';
const SCHEMA_VERSION: number = 1;
const MAX_REQUEST_BYTES: number = 64 * 1024;
const MAX_RESULT_BYTES: number = 64 * 1024;
const MAX_MANIFEST_BYTES: number = 32 * 1024 * 1024;
const EXPECTED_SHADER_COUNT: number = 6931;
const EXPECTED_OHOS_UI_FILES: number = 18;
const REQUIRED_DATA_FILES: string[] = [
  '00-00000001.data',
  '00-00000002.data',
  '00-00000003.data',
  '00-00000004.data',
  '00-00000005.data',
  '00-00000006.data',
  '01-00000001.data',
  '01-00000002.data',
  '02-00000001.data',
  '02-00000002.data',
  '03-00000001.data',
  '03-00000002.data',
  '04-00000001.data',
  '04-00000002.data',
  '05-00000001.data',
  '05-00000002.data',
  '17-00000001.data',
  '17-00000002.data'
];

export type InstallerProgress = (message: string) => void;

export interface InstallerImportResult {
  outcome: string;
  error: string;
}

interface InstallerRequest {
  schema_version?: number;
  token?: string;
  archive?: string;
  archive_sha256?: string;
  archive_bytes?: number;
  manifest_sha256?: string;
}

interface ParsedInstallerRequest {
  token: string;
  archive: string;
  archiveSha256: string;
  archiveBytes: number;
  manifestSha256: string;
}

interface ManifestFile {
  path?: string;
  sha256?: string;
  size?: number;
}

interface ImportStagingManifest {
  path?: string;
  layout?: string[];
  mobile_device_resources?: string;
}

interface PackManifest {
  path?: string;
  product?: string;
  target?: string;
  shader_xgs?: number;
  ui_art_files?: number;
}

interface DataManifest {
  path?: string;
  archives?: number;
  config?: string;
}

interface StagingManifest {
  schema_version?: number;
  status?: string;
  target?: string;
  product?: string;
  runtime_root?: string;
  import_staging?: ImportStagingManifest;
  pack?: PackManifest;
  data?: DataManifest;
  file_count?: number;
  total_bytes?: number;
  files?: ManifestFile[];
}

interface StoredImportResult {
  schema_version?: number;
  token?: string;
  status?: string;
  files?: number;
  bytes?: number;
  error?: string;
}

interface VerifiedManifest {
  files: ManifestFile[];
  fileCount: number;
  totalBytes: number;
}

interface RequestPaths {
  request: string;
  archive: string;
  result: string;
}

function reportProgress(progress: InstallerProgress, message: string): void {
  try {
    progress(message);
  } catch (error) {
    hilog.warn(0xD2E0, TAG, 'progress callback failed');
  }
}

function errorText(error: Error): string {
  try {
    const message: string = error.message.trim();
    return message.length > 0 ? message : '未知错误';
  } catch (ignored) {
    return '未知错误';
  }
}

function failure(text: string): InstallerImportResult {
  return { outcome: 'failed', error: text };
}

function success(): InstallerImportResult {
  return { outcome: 'imported', error: '' };
}

function unchanged(): InstallerImportResult {
  return { outcome: 'none', error: '' };
}

function pathExists(path: string): boolean {
  try {
    return fileIo.accessSync(path);
  } catch (error) {
    return false;
  }
}

function isHex(value: string, minimum: number, maximum: number): boolean {
  if (value.length < minimum || value.length > maximum) {
    return false;
  }
  for (let index: number = 0; index < value.length; index++) {
    const code: number = value.charCodeAt(index);
    const digit: boolean = code >= 48 && code <= 57;
    const lower: boolean = code >= 97 && code <= 102;
    const upper: boolean = code >= 65 && code <= 70;
    if (!digit && !lower && !upper) {
      return false;
    }
  }
  return true;
}

function isSafeManifestPath(value: string): boolean {
  if (value.length === 0 || value.indexOf('\\') >= 0 || value.indexOf('\0') >= 0 || value.startsWith('/')) {
    return false;
  }
  const parts: string[] = value.split('/');
  if (parts.length < 2 || (parts[0] !== 'dm' && parts[0] !== 'data')) {
    return false;
  }
  for (const part of parts) {
    if (part.length === 0 || part === '.' || part === '..') {
      return false;
    }
  }
  return true;
}

function isRegularFile(path: string): boolean {
  const info: fileIo.Stat = fileIo.lstatSync(path);
  return info.isFile() && !info.isSymbolicLink();
}

function ensureRegularFile(path: string, label: string, maximumBytes: number): number {
  if (!pathExists(path)) {
    throw new Error(`${label}不存在`);
  }
  const info: fileIo.Stat = fileIo.lstatSync(path);
  if (info.isSymbolicLink() || !info.isFile()) {
    throw new Error(`${label}不是普通文件`);
  }
  if (info.size <= 0 || info.size > maximumBytes) {
    throw new Error(`${label}大小异常`);
  }
  return info.size;
}

function ensureDirectory(path: string, label: string): void {
  const info: fileIo.Stat = fileIo.lstatSync(path);
  if (info.isSymbolicLink() || !info.isDirectory()) {
    throw new Error(`${label}不是普通目录`);
  }
}

function ensureAbsentOrEmptyDirectory(path: string, label: string): void {
  if (!pathExists(path)) {
    return;
  }
  ensureDirectory(path, label);
  if (fileIo.listFileSync(path).length !== 0) {
    throw new Error(`${label}不是空的，已拒绝覆盖`);
  }
}

function validateFreshTargets(filesDir: string): void {
  const dmDir: string = filesDir + '/dm';
  if (pathExists(dmDir)) {
    ensureDirectory(dmDir, 'files/dm');
    const names: string[] = fileIo.listFileSync(dmDir);
    if (names.length > 1 || (names.length === 1 && names[0] !== 'boot.log')) {
      throw new Error('files/dm 中已有运行文件，只允许保留 bootstrap 的 boot.log');
    }
    if (names.length === 1 && !isRegularFile(dmDir + '/boot.log')) {
      throw new Error('files/dm/boot.log 不是普通文件');
    }
  }

  ensureAbsentOrEmptyDirectory(filesDir + '/romfs/data', 'files/romfs/data');
  ensureAbsentOrEmptyDirectory(filesDir + '/save', 'files/save');
  ensureAbsentOrEmptyDirectory(filesDir + '/saves', 'files/saves');
  ensureAbsentOrEmptyDirectory(filesDir + '/temp', 'files/temp');

  if (pathExists(filesDir + '/runtimeconfig')) {
    throw new Error('检测到已有 runtimeconfig，已拒绝执行全新安装导入');
  }
  if (pathExists(filesDir + '/cache')) {
    throw new Error('检测到已有运行时 cache，已拒绝执行全新安装导入');
  }
}

function validateFreshInstall(filesDir: string, token: string): void {
  validateFreshTargets(filesDir);
  const stage: string = filesDir + '/.d2r-import-' + token;
  if (pathExists(stage)) {
    throw new Error(`暂存目录已存在：${stage}`);
  }
  const bootstrap: string = filesDir + '/d2r-first-boot-' + token;
  if (pathExists(bootstrap)) {
    throw new Error(`bootstrap 保留文件已存在：${bootstrap}`);
  }
}

function requestPaths(context: common.UIAbilityContext): RequestPaths {
  const cacheDir: string = context.getApplicationContext().cacheDir;
  return {
    request: cacheDir + '/d2r-installer-request.json',
    archive: '',
    result: cacheDir + '/d2r-installer-result.json'
  };
}

function parseRequest(path: string, applicationCacheDir: string): ParsedInstallerRequest {
  ensureRegularFile(path, '安装请求', MAX_REQUEST_BYTES);
  const request = JSON.parse(fileIo.readTextSync(path)) as InstallerRequest;
  if (request.schema_version !== SCHEMA_VERSION) {
    throw new Error('安装请求 schema_version 不匹配');
  }
  if (request.token === undefined || !isHex(request.token, 16, 128)) {
    throw new Error('安装请求 token 无效');
  }
  if (request.manifest_sha256 === undefined || !isHex(request.manifest_sha256, 64, 64)) {
    throw new Error('安装请求 manifest_sha256 无效');
  }
  const archiveName: string = 'd2r-installer-' + request.token + '.zip';
  if (request.archive !== archiveName) {
    throw new Error('安装请求 archive 不是本应用缓存中的指定 ZIP');
  }
  const archivePath: string = applicationCacheDir + '/' + archiveName;
  if (request.archive_sha256 !== undefined && !isHex(request.archive_sha256, 64, 64)) {
    throw new Error('安装请求 archive_sha256 无效');
  }
  if (request.archive_bytes !== undefined &&
    (!Number.isSafeInteger(request.archive_bytes) || request.archive_bytes <= 0)) {
    throw new Error('安装请求 archive_bytes 无效');
  }
  return {
    token: request.token as string,
    archive: archivePath,
    archiveSha256: request.archive_sha256 ?? '',
    archiveBytes: request.archive_bytes ?? -1,
    manifestSha256: request.manifest_sha256 as string
  };
}

function readStoredResult(path: string, token: string): StoredImportResult | undefined {
  if (!pathExists(path)) {
    return undefined;
  }
  ensureRegularFile(path, '安装结果', MAX_RESULT_BYTES);
  const stored = JSON.parse(fileIo.readTextSync(path)) as StoredImportResult;
  if (stored.schema_version !== SCHEMA_VERSION || stored.token !== token) {
    return undefined;
  }
  if (stored.status !== 'imported' && stored.status !== 'failed') {
    throw new Error('同 token 的安装结果状态无效');
  }
  return stored;
}

function writeResult(path: string, token: string, status: string, files: number, bytes: number,
  error: string): void {
  const result: StoredImportResult = {
    schema_version: SCHEMA_VERSION,
    token: token,
    status: status,
    files: files,
    bytes: bytes
  };
  if (status === 'failed') {
    result.error = error;
  }
  const temporary: string = path + '.tmp-' + token;
  if (pathExists(temporary)) {
    fileIo.unlinkSync(temporary);
  }
  const file: fileIo.File = fileIo.openSync(temporary,
    fileIo.OpenMode.CREATE | fileIo.OpenMode.TRUNC | fileIo.OpenMode.WRITE_ONLY);
  try {
    fileIo.writeSync(file.fd, JSON.stringify(result));
  } finally {
    fileIo.closeSync(file);
  }
  try {
    fileIo.renameSync(temporary, path);
  } catch (error) {
    // Some HarmonyOS filesystems report EEXIST for rename-replace. Keep the persisted result
    // authoritative for the new token after the only possible fallback.
    if (pathExists(path)) {
      fileIo.unlinkSync(path);
    }
    fileIo.renameSync(temporary, path);
  }
}

function validateManifestMetadata(manifest: StagingManifest, files: ManifestFile[]): void {
  if (manifest.schema_version !== SCHEMA_VERSION || manifest.status !== 'complete' || manifest.target !== 'ohos') {
    throw new Error('资源清单不是完整的 ohos 安装清单');
  }
  if (manifest.runtime_root !== 'dm' || manifest.import_staging === undefined || manifest.pack === undefined ||
    manifest.data === undefined) {
    throw new Error('资源清单缺少 dm/data 布局');
  }
  const layout: string[] | undefined = manifest.import_staging.layout;
  if (manifest.import_staging.path !== '.' || layout === undefined || layout.length !== 2 ||
    layout[0] !== 'dm' || layout[1] !== 'data') {
    throw new Error('资源清单 import_staging.layout 不是 dm/data');
  }
  if (manifest.pack.path !== 'dm' || manifest.pack.target !== 'ohos' || manifest.pack.product !== manifest.product) {
    throw new Error('资源清单 pack 目标与产品不一致');
  }
  if (manifest.pack.shader_xgs !== EXPECTED_SHADER_COUNT ||
    manifest.pack.ui_art_files !== EXPECTED_OHOS_UI_FILES) {
    throw new Error('资源清单运行组件数量不符合 HarmonyOS 安装要求');
  }
  if (manifest.data.path !== 'data' || manifest.data.archives !== REQUIRED_DATA_FILES.length ||
    (manifest.data.config !== '.build.config' && manifest.data.config !== 'build.config')) {
    throw new Error('资源清单 data 元数据不完整');
  }
  if (manifest.file_count !== files.length || manifest.total_bytes === undefined ||
    !Number.isSafeInteger(manifest.total_bytes) || manifest.total_bytes <= 0) {
    throw new Error('资源清单文件数量或总大小无效');
  }
}

function validateManifestEntries(manifest: StagingManifest): VerifiedManifest {
  const files: ManifestFile[] | undefined = manifest.files;
  if (files === undefined) {
    throw new Error('资源清单缺少 files');
  }
  validateManifestMetadata(manifest, files);
  const seen: Set<string> = new Set<string>();
  let totalBytes: number = 0;
  let shaderCount: number = 0;
  let uiArtCount: number = 0;
  let hasOut: boolean = false;
  let hasOverlay: boolean = false;
  let hasDerived: boolean = false;
  let hasPackInfo: boolean = false;
  let configCount: number = 0;

  for (const row of files) {
    if (row.path === undefined || !isSafeManifestPath(row.path)) {
      throw new Error('资源清单包含不安全的文件路径');
    }
    if (row.sha256 === undefined || !isHex(row.sha256, 64, 64)) {
      throw new Error(`资源清单文件哈希无效：${row.path}`);
    }
    if (row.size === undefined || !Number.isSafeInteger(row.size) || row.size < 0) {
      throw new Error(`资源清单文件大小无效：${row.path}`);
    }
    if (seen.has(row.path)) {
      throw new Error(`资源清单文件重复：${row.path}`);
    }
    seen.add(row.path);
    totalBytes += row.size;

    if (row.path.startsWith('dm/shaders/') && row.path.endsWith('.xgs')) {
      shaderCount++;
    }
    const uiPrefix: string = 'dm/ui-art/';
    if (row.path.startsWith(uiPrefix) && row.path.substring(uiPrefix.length).indexOf('/') < 0) {
      uiArtCount++;
    }
    if (row.path.startsWith('dm/out/')) {
      hasOut = true;
    }
    if (row.path.startsWith('dm/overlay/')) {
      hasOverlay = true;
    }
    if (row.path === 'dm/derived.tsv') {
      hasDerived = true;
    }
    if (row.path === 'dm/pack_info.txt') {
      hasPackInfo = true;
    }
    if (row.path === 'data/.build.config' || row.path === 'data/build.config') {
      configCount++;
    }
  }

  if (totalBytes !== manifest.total_bytes) {
    throw new Error('资源清单 total_bytes 与文件明细不一致');
  }
  if (shaderCount !== EXPECTED_SHADER_COUNT || uiArtCount !== EXPECTED_OHOS_UI_FILES || !hasOut ||
    !hasOverlay || !hasDerived || !hasPackInfo) {
    throw new Error('资源清单缺少必需运行组件');
  }
  const configName: string | undefined = manifest.data === undefined ? undefined : manifest.data.config;
  if (configCount !== 1 || configName === undefined || !seen.has('data/' + configName)) {
    throw new Error('资源清单缺少唯一 build.config');
  }
  for (const name of REQUIRED_DATA_FILES) {
    if (!seen.has('data/' + name)) {
      throw new Error(`资源清单缺少数据档案：${name}`);
    }
  }
  return { files: files, fileCount: files.length, totalBytes: manifest.total_bytes };
}

async function verifyDirectory(root: string, relativeDir: string, expected: Map<string, ManifestFile>,
  seen: Set<string>, progress: InstallerProgress, totalFiles: number, totalBytes: number): Promise<number> {
  const absoluteDir: string = root + '/' + relativeDir;
  ensureDirectory(absoluteDir, relativeDir);
  let verified: number = 0;
  for (const name of fileIo.listFileSync(absoluteDir)) {
    if (name.length === 0 || name === '.' || name === '..' || name.indexOf('/') >= 0 || name.indexOf('\\') >= 0) {
      throw new Error(`暂存目录包含非法文件名：${relativeDir}/${name}`);
    }
    const relative: string = relativeDir + '/' + name;
    const absolute: string = root + '/' + relative;
    const info: fileIo.Stat = fileIo.lstatSync(absolute);
    if (info.isSymbolicLink()) {
      throw new Error(`暂存目录包含符号链接：${relative}`);
    }
    if (info.isDirectory()) {
      verified += await verifyDirectory(root, relative, expected, seen, progress, totalFiles, totalBytes);
      continue;
    }
    if (!info.isFile()) {
      throw new Error(`暂存目录包含非普通文件：${relative}`);
    }
    const wanted: ManifestFile | undefined = expected.get(relative);
    if (wanted === undefined) {
      throw new Error(`暂存目录包含清单外文件：${relative}`);
    }
    if (wanted.size === undefined || info.size !== wanted.size) {
      throw new Error(`文件大小不匹配：${relative}`);
    }
    const digest: string = await hash.hash(absolute, 'sha256');
    if (wanted.sha256 === undefined || digest.toLowerCase() !== wanted.sha256.toLowerCase()) {
      throw new Error(`文件哈希不匹配：${relative}`);
    }
    if (seen.has(relative)) {
      throw new Error(`暂存文件重复：${relative}`);
    }
    seen.add(relative);
    verified++;
    if (seen.size === totalFiles || seen.size % 25 === 0) {
      reportProgress(progress,
        `正在校验安装文件：${seen.size} / ${totalFiles}（${Math.floor(totalBytes / 1048576)} MB）`);
    }
  }
  return verified;
}

async function verifyStage(stage: string, manifestPath: string, requestSha: string,
  progress: InstallerProgress): Promise<VerifiedManifest> {
  ensureRegularFile(manifestPath, '资源清单', MAX_MANIFEST_BYTES);
  reportProgress(progress, '正在核对资源清单…');
  const manifestDigest: string = await hash.hash(manifestPath, 'sha256');
  if (manifestDigest.toLowerCase() !== requestSha.toLowerCase()) {
    throw new Error('resource-manifest.json 的 SHA-256 与安装请求不一致');
  }
  const manifest = JSON.parse(fileIo.readTextSync(manifestPath)) as StagingManifest;
  const verified: VerifiedManifest = validateManifestEntries(manifest);
  const expected: Map<string, ManifestFile> = new Map<string, ManifestFile>();
  for (const row of verified.files) {
    if (row.path !== undefined) {
      expected.set(row.path, row);
    }
  }

  const top: string[] = fileIo.listFileSync(stage);
  if (top.length !== 3 || top.indexOf('dm') < 0 || top.indexOf('data') < 0 ||
    top.indexOf('resource-manifest.json') < 0) {
    throw new Error('暂存目录顶层必须且只能包含 dm、data、resource-manifest.json');
  }
  const seen: Set<string> = new Set<string>();
  await verifyDirectory(stage, 'dm', expected, seen, progress, verified.fileCount, verified.totalBytes);
  await verifyDirectory(stage, 'data', expected, seen, progress, verified.fileCount, verified.totalBytes);
  if (seen.size !== verified.fileCount) {
    throw new Error(`资源清单声明 ${verified.fileCount} 个文件，实际只验证到 ${seen.size} 个`);
  }
  return verified;
}

function removeOwnedDirectory(path: string, label: string): void {
  if (!pathExists(path)) {
    return;
  }
  try {
    fileIo.rmdirSync(path);
  } catch (error) {
    hilog.warn(0xD2E0, TAG, 'cleanup %{public}s failed: %{public}s', label, errorText(error as Error));
  }
}

function restoreBootstrap(filesDir: string, token: string): string {
  const dmDir: string = filesDir + '/dm';
  const bootstrap: string = filesDir + '/d2r-first-boot-' + token;
  if (!pathExists(bootstrap)) {
    return '';
  }
  try {
    if (!pathExists(dmDir)) {
      fileIo.mkdirSync(dmDir, false);
    }
    fileIo.renameSync(bootstrap, dmDir + '/boot.log');
    return '';
  } catch (error) {
    return `；bootstrap 恢复失败：${errorText(error as Error)}`;
  }
}

function publishStage(stage: string, filesDir: string, token: string): void {
  const stageDm: string = stage + '/dm';
  const stageData: string = stage + '/data';
  const filesDm: string = filesDir + '/dm';
  const romfsDir: string = filesDir + '/romfs';
  const dataDir: string = romfsDir + '/data';
  const bootstrap: string = filesDir + '/d2r-first-boot-' + token;

  validateFreshTargets(filesDir);
  let bootstrapMoved: boolean = false;
  let dmPublished: boolean = false;
  try {
    if (pathExists(filesDm)) {
      ensureDirectory(filesDm, 'files/dm');
      const names: string[] = fileIo.listFileSync(filesDm);
      if (names.length === 1 && names[0] === 'boot.log') {
        fileIo.renameSync(filesDm + '/boot.log', bootstrap);
        bootstrapMoved = true;
      }
      if (fileIo.listFileSync(filesDm).length !== 0) {
        throw new Error('发布前 files/dm 出现了额外文件');
      }
      fileIo.rmdirSync(filesDm);
    }
    fileIo.renameSync(stageDm, filesDm);
    dmPublished = true;

    if (pathExists(dataDir)) {
      ensureAbsentOrEmptyDirectory(dataDir, 'files/romfs/data');
      fileIo.rmdirSync(dataDir);
    }
    if (!pathExists(romfsDir)) {
      fileIo.mkdirSync(romfsDir, false);
    } else {
      ensureDirectory(romfsDir, 'files/romfs');
    }
    fileIo.renameSync(stageData, dataDir);
  } catch (error) {
    let rollbackNote: string = '';
    if (dmPublished && pathExists(filesDm)) {
      try {
        fileIo.renameSync(filesDm, stageDm);
        dmPublished = false;
      } catch (rollbackError) {
        rollbackNote += `；保留新 dm 到暂存目录失败：${errorText(rollbackError as Error)}`;
      }
    }
    if (bootstrapMoved) {
      rollbackNote += restoreBootstrap(filesDir, token);
    }
    throw new Error(`发布 dm/data 失败：${errorText(error as Error)}${rollbackNote}`);
  }
}

function cleanupSuccess(context: common.UIAbilityContext, stage: string, paths: RequestPaths): void {
  removeOwnedDirectory(stage, 'staging directory');
  try {
    if (pathExists(paths.archive)) {
      fileIo.unlinkSync(paths.archive);
    }
  } catch (error) {
    hilog.warn(0xD2E0, TAG, 'archive cleanup failed: %{public}s', errorText(error as Error));
  }
  try {
    if (pathExists(paths.request)) {
      fileIo.unlinkSync(paths.request);
    }
  } catch (error) {
    hilog.warn(0xD2E0, TAG, 'request cleanup failed: %{public}s', errorText(error as Error));
  }
}

function persistFailure(paths: RequestPaths, token: string, files: number, bytes: number, message: string): string {
  try {
    writeResult(paths.result, token, 'failed', files, bytes, message);
    return message;
  } catch (error) {
    return `${message}；安装失败结果也无法写入：${errorText(error as Error)}`;
  }
}

/**
 * Run the helper-driven fresh install import. A missing request is an ordinary no-op.
 */
export async function runFreshInstallerImport(context: common.UIAbilityContext,
  progress: InstallerProgress): Promise<InstallerImportResult> {
  if (!context.applicationInfo.debug) {
    return unchanged();
  }
  const paths: RequestPaths = requestPaths(context);
  if (!pathExists(paths.request)) {
    return unchanged();
  }

  let request: ParsedInstallerRequest;
  try {
    request = parseRequest(paths.request, context.getApplicationContext().cacheDir);
  } catch (error) {
    const message: string = `安装请求不完整或无效：${errorText(error as Error)}`;
    reportProgress(progress, message);
    return failure(message);
  }
  const token: string = request.token;
  const manifestSha: string = request.manifestSha256;
  const archive: string = request.archive;
  paths.archive = archive;

  try {
    const stored: StoredImportResult | undefined = readStoredResult(paths.result, token);
    if (stored !== undefined && stored.status === 'imported') {
      reportProgress(progress, '安装已完成，正在清理剩余安装文件…');
      cleanupSuccess(context, context.filesDir + '/.d2r-import-' + token, paths);
      reportProgress(progress, '安装结果已确认，正在继续启动…');
      return success();
    }
    if (stored !== undefined && stored.status === 'failed') {
      const storedError: string = stored.error ?? '上一次同 token 安装失败';
      reportProgress(progress, storedError);
      return failure(storedError);
    }

    reportProgress(progress, '正在检查全新安装状态…');
    validateFreshInstall(context.filesDir, token);
    const archiveBytes: number = ensureRegularFile(archive, '安装 ZIP', Number.MAX_SAFE_INTEGER);
    if (request.archiveBytes >= 0 && archiveBytes !== request.archiveBytes) {
      throw new Error(`安装 ZIP 大小不匹配：请求 ${request.archiveBytes}，实际 ${archiveBytes}`);
    }
    if (request.archiveSha256.length > 0) {
      reportProgress(progress, '正在校验安装 ZIP 的 SHA-256…');
      const archiveDigest: string = await hash.hash(archive, 'sha256');
      if (archiveDigest.toLowerCase() !== request.archiveSha256.toLowerCase()) {
        throw new Error('安装 ZIP 的 SHA-256 与请求不一致');
      }
    }

    const stage: string = context.filesDir + '/.d2r-import-' + token;
    reportProgress(progress, '正在解压游戏资源（约 6.3 GB）…');
    fileIo.mkdirSync(stage, false);
    await zlib.decompressFile(archive, stage);

    const verified: VerifiedManifest = await verifyStage(stage, stage + '/resource-manifest.json', manifestSha, progress);
    reportProgress(progress, '正在重新确认目标目录…');
    validateFreshTargets(context.filesDir);
    reportProgress(progress, '正在发布 dm 和 romfs/data…');
    publishStage(stage, context.filesDir, token);

    reportProgress(progress, '正在写入安装完成结果…');
    writeResult(paths.result, token, 'imported', verified.fileCount, verified.totalBytes, '');
    cleanupSuccess(context, stage, paths);
    reportProgress(progress, '导入完成，正在启动…');
    return success();
  } catch (error) {
    const message: string = persistFailure(paths, token, 0, 0,
      `导入失败：${errorText(error as Error)}`);
    reportProgress(progress, message);
    hilog.error(0xD2E0, TAG, 'fresh installer import failed: %{public}s', message);
    return failure(message);
  }
}
