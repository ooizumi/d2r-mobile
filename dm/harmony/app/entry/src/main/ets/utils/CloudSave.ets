// HarmonyOS cloud-save coordinator for D2R Mobile.
// This file owns protocol validation, startup reconciliation and the running-game upload worker.
// Native save locking and the atomic save-tree exchange stay in libdm.so.

import { common } from '@kit.AbilityKit';
import { util } from '@kit.ArkTS';
import { cryptoFramework } from '@kit.CryptoArchitectureKit';
import { fileIo, hash } from '@kit.CoreFileKit';
import { connection, http } from '@kit.NetworkKit';
import { hilog } from '@kit.PerformanceAnalysisKit';
import { BusinessError } from '@kit.BasicServicesKit';
import dm from 'libdm.so';

const TAG: string = 'XblCloudSave';
const DEFAULT_ENDPOINT: string = 'https://mythstribe.com/d2r-cloud/v1';
const DEFAULT_ORIGIN_HOST: string = 'mythstribe.com';
const DEFAULT_ORIGIN_IPV4: string = '120.53.45.109';
// Custom DNS rules belong to the application, including across manager replacement.
let cloudNetworkTail: Promise<void> = Promise.resolve();
const CONFIG_SCHEMA: number = 1;
const BASELINE_SCHEMA: number = 1;
const MAX_FILES: number = 2048;
const MAX_FILE_BYTES: number = 8 * 1024 * 1024;
const MAX_TOTAL_BYTES: number = 24 * 1024 * 1024;
const MAX_HTTP_BODY_BYTES: number = 48 * 1024 * 1024;
const MAX_PATH_UTF16: number = 512;
const MAX_PATH_COMPONENTS: number = 8;
const CONNECT_TIMEOUT_MS: number = 8000;
const SMALL_READ_TIMEOUT_MS: number = 10000;
const TRANSFER_READ_TIMEOUT_MS: number = 35000;
const UPLOAD_INTERVAL_MS: number = 30000;
const EMPTY_SHA256: string = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';

interface NetworkDiagnostic {
  at: number;
  method: string;
  path: string;
  elapsedMs: number;
  status: number;
  errorCode: number;
  route: string;
  remoteAddress: string;
  timing: http.PerformanceTiming | null;
}

interface CloudConfigFile {
  schema_version?: number;
  endpoint: string;
  token: string;
}

interface CloudConfig {
  endpoint: string;
  token: string;
}

interface BaselineFile {
  schema_version: number;
  scope: string;
  revision: string;
  snapshot_sha256: string;
}

interface ManifestFile {
  path: string;
  size: number;
  sha256: string;
}

interface UploadFile extends ManifestFile {
  data: string;
}

interface LocalSnapshot {
  root: string;
  files: ManifestFile[];
  snapshotSha256: string;
  totalBytes: number;
}

interface CloudStateWire {
  revision?: string;
  snapshot_sha256?: string;
  updated_at?: string;
  device?: string;
  file_count?: number;
  total_bytes?: number;
}

interface CloudState {
  revision: string;
  snapshotSha256: string;
  updatedAt: string;
  device: string;
  fileCount: number;
  totalBytes: number;
}

interface CloudSnapshotFileWire {
  path?: string;
  size?: number;
  sha256?: string;
  data?: string;
}

interface CloudSnapshotWire extends CloudStateWire {
  files?: CloudSnapshotFileWire[];
}

interface CloudConflictWire {
  error?: string;
  state?: CloudStateWire;
}

interface ValidatedRemoteFile extends ManifestFile {
  bytes: Uint8Array;
}

interface ValidatedRemoteSnapshot {
  state: CloudState;
  files: ValidatedRemoteFile[];
  snapshotSha256: string;
  totalBytes: number;
}

interface PutRequest {
  base_revision: string;
  device: string;
  snapshot_sha256: string;
  files: UploadFile[];
}

interface PutResult {
  conflict: boolean;
  state: CloudState;
}

interface HttpResult {
  status: number;
  body: string;
}

interface CloudHttpHeaders {
  Authorization: string;
  'Content-Type': string;
  'Accept': string;
  'User-Agent': string;
}

interface LoadedConfig {
  config: CloudConfig;
}

class RemoteChangedError extends Error {
  readonly state: CloudState;

  constructor(state: CloudState) {
    super('云端存档在恢复期间发生变化');
    this.state = state;
  }
}

export interface CloudSaveConflict {
  localFiles: number;
  localBytes: number;
  remoteFiles: number;
  remoteBytes: number;
  remoteUpdatedAt: string;
  remoteRevision: string;
  localSha256: string;
  remoteSha256: string;
}

export interface CloudSaveStartupResult {
  kind: string;
  message: string;
  endpoint: string;
  conflict: CloudSaveConflict | null;
}

function messageOf(error: Error): string {
  try {
    const value: string = error.message.trim();
    return value.length > 0 ? value : '未知错误';
  } catch (ignored) {
    return '未知错误';
  }
}

function pathExists(path: string): boolean {
  try {
    return fileIo.accessSync(path);
  } catch (error) {
    return false;
  }
}

function ensureCloudDirectory(filesRoot: string): string {
  const root: string = filesRoot + '/cloud-save';
  if (!pathExists(root)) {
    fileIo.mkdirSync(root, true);
  }
  const info: fileIo.Stat = fileIo.lstatSync(root);
  if (info.isSymbolicLink() || !info.isDirectory()) {
    throw new Error('files/cloud-save 不是普通目录');
  }
  return root;
}

function uniqueChild(parent: string, prefix: string): string {
  const stamp: string = Date.now().toString();
  for (let index: number = 0; index < 1000; index++) {
    const candidate: string = parent + '/' + prefix + stamp + '-' + index.toString();
    if (!pathExists(candidate)) {
      return candidate;
    }
  }
  throw new Error('无法分配云存档临时目录');
}

function removeTree(path: string): void {
  if (!pathExists(path)) {
    return;
  }
  const info: fileIo.Stat = fileIo.lstatSync(path);
  if (info.isDirectory() && !info.isSymbolicLink()) {
    for (const name of fileIo.listFileSync(path)) {
      removeTree(path + '/' + name);
    }
    fileIo.rmdirSync(path);
    return;
  }
  fileIo.unlinkSync(path);
}

function writeAllFd(fd: number, value: ArrayBuffer): void {
  let written: number = 0;
  while (written < value.byteLength) {
    const count: number = fileIo.writeSync(fd, value.slice(written), { offset: written });
    if (count <= 0) {
      throw new Error('写入文件时没有取得进展');
    }
    written += count;
  }
}

function writeAtomicText(path: string, textValue: string): void {
  const parent: string = path.substring(0, path.lastIndexOf('/'));
  if (!pathExists(parent)) {
    fileIo.mkdirSync(parent, true);
  }
  const body: Uint8Array = new util.TextEncoder().encodeInto(textValue);
  const temporary: string = uniqueChild(parent, '.state-');
  let file: fileIo.File | null = null;
  try {
    file = fileIo.openSync(temporary,
      fileIo.OpenMode.CREATE | fileIo.OpenMode.WRITE_ONLY |
      fileIo.OpenMode.NOFOLLOW | fileIo.OpenMode.SYNC);
    writeAllFd(file.fd, body.buffer);
    fileIo.fsyncSync(file.fd);
    fileIo.closeSync(file);
    file = null;
    if (fileIo.readTextSync(temporary) !== textValue) {
      throw new Error('状态临时文件校验失败');
    }
    fileIo.renameSync(temporary, path);
    const directory: fileIo.File = fileIo.openSync(parent,
      fileIo.OpenMode.READ_ONLY | fileIo.OpenMode.DIR | fileIo.OpenMode.NOFOLLOW);
    try {
      fileIo.fsyncSync(directory.fd);
    } finally {
      fileIo.closeSync(directory);
    }
  } catch (error) {
    if (file !== null) {
      try {
        fileIo.closeSync(file);
      } catch (ignored) {
      }
    }
    try {
      if (pathExists(temporary)) {
        fileIo.unlinkSync(temporary);
      }
    } catch (ignored) {
    }
    throw new Error(`原子写入失败：${messageOf(error as Error)}`);
  }
  if (fileIo.readTextSync(path) !== textValue) {
    throw new Error('原子写入后的内容校验失败');
  }
}

function isLowerHex64(value: string): boolean {
  if (value.length !== 64) {
    return false;
  }
  for (let index: number = 0; index < value.length; index++) {
    const code: number = value.charCodeAt(index);
    if (!((code >= 48 && code <= 57) || (code >= 97 && code <= 102))) {
      return false;
    }
  }
  return true;
}

function bytesToHex(bytes: Uint8Array): string {
  const digits: string = '0123456789abcdef';
  let out: string = '';
  for (let index: number = 0; index < bytes.length; index++) {
    const value: number = bytes[index];
    out += digits.charAt(value >> 4) + digits.charAt(value & 15);
  }
  return out;
}

function sha256Bytes(value: Uint8Array): string {
  const md: cryptoFramework.Md = cryptoFramework.createMd('SHA256');
  md.updateSync({ data: value });
  return bytesToHex(md.digestSync().data);
}

function normalizedRemoteHash(value: string): string {
  return value.length === 0 ? EMPTY_SHA256 : value;
}

function comparePath(left: string, right: string): number {
  if (left < right) {
    return -1;
  }
  if (left > right) {
    return 1;
  }
  return 0;
}

function validateRelativePath(value: string): void {
  if (value.length === 0 || value.length > MAX_PATH_UTF16 || value.startsWith('/') ||
    value.indexOf('\\') >= 0 || value.indexOf(':') >= 0) {
    throw new Error(`云存档路径非法：${value}`);
  }
  const parts: string[] = value.split('/');
  if (parts.length === 0 || parts.length > MAX_PATH_COMPONENTS) {
    throw new Error(`云存档路径层级过多：${value}`);
  }
  for (const part of parts) {
    if (part.length === 0 || part === '.' || part === '..' || part.startsWith('.') || part.startsWith('_')) {
      throw new Error(`云存档包含应保留在本地的路径：${value}`);
    }
    for (let index: number = 0; index < part.length; index++) {
      const code: number = part.charCodeAt(index);
      if (code < 32 || code === 127) {
        throw new Error(`云存档路径包含控制字符：${value}`);
      }
    }
  }
  if (parts.length > 1 && parts[0] === 'Settings') {
    throw new Error('云存档不应包含 Settings 目录');
  }
}

function validatePathSet(files: ManifestFile[]): void {
  const names: Set<string> = new Set<string>();
  const directories: Set<string> = new Set<string>();
  for (const file of files) {
    if (names.has(file.path)) {
      throw new Error(`云存档路径重复：${file.path}`);
    }
    const parts: string[] = file.path.split('/');
    let current: string = '';
    for (let index: number = 0; index < parts.length - 1; index++) {
      current = current.length === 0 ? parts[index] : current + '/' + parts[index];
      if (names.has(current)) {
        throw new Error(`云存档文件与目录冲突：${current}`);
      }
      directories.add(current);
    }
    if (directories.has(file.path)) {
      throw new Error(`云存档文件与目录冲突：${file.path}`);
    }
    names.add(file.path);
  }
}

function snapshotDigest(files: ManifestFile[]): string {
  let canonical: string = '';
  for (const file of files) {
    canonical += file.path + '\0' + file.size.toString() + '\0' + file.sha256 + '\n';
  }
  return sha256Bytes(new util.TextEncoder().encodeInto(canonical));
}

function readFileBytes(path: string, expectedSize: number): Uint8Array {
  const file: fileIo.File = fileIo.openSync(path, fileIo.OpenMode.READ_ONLY | fileIo.OpenMode.NOFOLLOW);
  const bytes: Uint8Array = new Uint8Array(expectedSize);
  try {
    let read: number = 0;
    while (read < expectedSize) {
      const chunk: ArrayBuffer = new ArrayBuffer(Math.min(1024 * 1024, expectedSize - read));
      const count: number = fileIo.readSync(file.fd, chunk, { offset: read });
      if (count <= 0) {
        break;
      }
      bytes.set(new Uint8Array(chunk, 0, count), read);
      read += count;
    }
    if (read !== expectedSize) {
      throw new Error(`读取文件不完整：${path}`);
    }
  return bytes;
  } finally {
    fileIo.closeSync(file);
  }
}

function isExcludedComponent(name: string): boolean {
  return name.startsWith('.') || name.startsWith('_');
}

async function scanSnapshotDirectory(root: string, relative: string, files: ManifestFile[]): Promise<number> {
  const directory: string = relative.length === 0 ? root : root + '/' + relative;
  const info: fileIo.Stat = fileIo.lstatSync(directory);
  if (info.isSymbolicLink() || !info.isDirectory()) {
    throw new Error(`云存档快照目录非法：${relative}`);
  }
  let totalBytes: number = 0;
  for (const name of fileIo.listFileSync(directory)) {
    const childRelative: string = relative.length === 0 ? name : relative + '/' + name;
    const childPath: string = root + '/' + childRelative;
    const childInfo: fileIo.Stat = fileIo.lstatSync(childPath);
    if (isExcludedComponent(name) || (relative.length === 0 && name === 'Settings' && childInfo.isDirectory())) {
      continue;
    }
    if (childInfo.isSymbolicLink()) {
      throw new Error(`云存档包含符号链接：${childRelative}`);
    }
    if (childInfo.isDirectory()) {
      if (childRelative.length > 0 && childRelative.split('/').length > MAX_PATH_COMPONENTS) {
        throw new Error(`云存档目录层级过多：${childRelative}`);
      }
      totalBytes += await scanSnapshotDirectory(root, childRelative, files);
      continue;
    }
    if (!childInfo.isFile()) {
      throw new Error(`云存档包含特殊文件：${childRelative}`);
    }
    validateRelativePath(childRelative);
    if (!Number.isSafeInteger(childInfo.size) || childInfo.size > MAX_FILE_BYTES) {
      throw new Error(`云存档文件超过 8 MiB：${childRelative}`);
    }
    if (files.length >= MAX_FILES) {
      throw new Error('云存档文件数超过 2048');
    }
    totalBytes += childInfo.size;
    if (totalBytes > MAX_TOTAL_BYTES) {
      throw new Error('云存档总大小超过 24 MiB');
    }
    const digest: string = await hash.hash(childPath, 'sha256');
    files.push({ path: childRelative, size: childInfo.size, sha256: digest.toLowerCase() });
  }
  return totalBytes;
}

async function verifyLocalSnapshot(root: string): Promise<LocalSnapshot> {
  const info: fileIo.Stat = fileIo.lstatSync(root);
  if (info.isSymbolicLink() || !info.isDirectory()) {
    throw new Error('Native 快照目标不是普通目录');
  }
  const files: ManifestFile[] = [];
  const totalBytes: number = await scanSnapshotDirectory(root, '', files);
  files.sort((left: ManifestFile, right: ManifestFile): number => comparePath(left.path, right.path));
  validatePathSet(files);
  return { root: root, files: files, snapshotSha256: snapshotDigest(files), totalBytes: totalBytes };
}

function validateState(wire: CloudStateWire): CloudState {
  if (wire.revision === undefined || wire.snapshot_sha256 === undefined || wire.updated_at === undefined ||
    wire.device === undefined || wire.file_count === undefined || wire.total_bytes === undefined) {
    throw new Error('云存档 state 缺少字段');
  }
  if (wire.snapshot_sha256.length > 0 && !isLowerHex64(wire.snapshot_sha256)) {
    throw new Error('云存档 state 的 snapshot_sha256 无效');
  }
  if (!Number.isSafeInteger(wire.file_count) || wire.file_count < 0 || wire.file_count > MAX_FILES ||
    !Number.isSafeInteger(wire.total_bytes) || wire.total_bytes < 0 || wire.total_bytes > MAX_TOTAL_BYTES) {
    throw new Error('云存档 state 的文件数或大小无效');
  }
  if (wire.revision.length === 0 && wire.snapshot_sha256.length !== 0) {
    throw new Error('未使用账号的云存档 state 不应包含快照哈希');
  }
  if ((wire.revision.length === 0) !== (wire.snapshot_sha256.length === 0)) {
    throw new Error('云存档 state 的 revision 与 snapshot_sha256 不一致');
  }
  return {
    revision: wire.revision,
    snapshotSha256: wire.snapshot_sha256,
    updatedAt: wire.updated_at,
    device: wire.device,
    fileCount: wire.file_count,
    totalBytes: wire.total_bytes
  };
}

function stateConflict(local: LocalSnapshot, remote: CloudState): CloudSaveConflict {
  return {
    localFiles: local.files.length,
    localBytes: local.totalBytes,
    remoteFiles: remote.fileCount,
    remoteBytes: remote.totalBytes,
    remoteUpdatedAt: remote.updatedAt,
    remoteRevision: remote.revision,
    localSha256: local.snapshotSha256,
    remoteSha256: normalizedRemoteHash(remote.snapshotSha256)
  };
}

function startup(kind: string, message: string, endpoint: string, conflict: CloudSaveConflict | null): CloudSaveStartupResult {
  return { kind: kind, message: message, endpoint: endpoint, conflict: conflict };
}

function isStandardBase64(value: string): boolean {
  if (value.length === 0) {
    return true;
  }
  if (value.length % 4 !== 0) {
    return false;
  }
  return /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value);
}

function validateRemoteFiles(wire: CloudSnapshotWire, expected: CloudState): ValidatedRemoteSnapshot {
  const returned: CloudState = validateState(wire);
  if (returned.revision !== expected.revision || returned.snapshotSha256 !== expected.snapshotSha256 ||
    returned.fileCount !== expected.fileCount || returned.totalBytes !== expected.totalBytes) {
    throw new Error('云存档 snapshot 响应与已确认 state 不一致');
  }
  const source: CloudSnapshotFileWire[] | undefined = wire.files;
  if (source === undefined || source.length !== expected.fileCount) {
    throw new Error('云存档 files 数量与 state 不一致');
  }
  const base64: util.Base64Helper = new util.Base64Helper();
  const files: ValidatedRemoteFile[] = [];
  let totalBytes: number = 0;
  let previous: string = '';
  for (const row of source) {
    if (row.path === undefined || row.size === undefined || row.sha256 === undefined || row.data === undefined) {
      throw new Error('云存档文件项缺少字段');
    }
    validateRelativePath(row.path);
    if (previous.length > 0 && comparePath(previous, row.path) >= 0) {
      throw new Error('云存档 files 未按 UTF-16 路径顺序排列');
    }
    previous = row.path;
    if (!Number.isSafeInteger(row.size) || row.size < 0 || row.size > MAX_FILE_BYTES ||
      !isLowerHex64(row.sha256) || !isStandardBase64(row.data)) {
      throw new Error(`云存档文件项无效：${row.path}`);
    }
    const bytes: Uint8Array = base64.decodeSync(row.data);
    if (bytes.length !== row.size) {
      throw new Error(`云存档文件大小不匹配：${row.path}`);
    }
    if (sha256Bytes(bytes) !== row.sha256) {
      throw new Error(`云存档文件哈希不匹配：${row.path}`);
    }
    totalBytes += row.size;
    if (totalBytes > MAX_TOTAL_BYTES) {
      throw new Error('云存档总大小超过 24 MiB');
    }
    files.push({ path: row.path, size: row.size, sha256: row.sha256, bytes: bytes });
  }
  validatePathSet(files);
  if (totalBytes !== expected.totalBytes || snapshotDigest(files) !== expected.snapshotSha256) {
    throw new Error('云存档 manifest 哈希或总大小不匹配');
  }
  return { state: expected, files: files, snapshotSha256: expected.snapshotSha256, totalBytes: totalBytes };
}

function validateBundledConfig(raw: string): CloudConfigFile {
  const parsed: CloudConfigFile = JSON.parse(raw) as CloudConfigFile;
  if (parsed.endpoint === undefined || parsed.token === undefined) {
    throw new Error('cloud-save.json 缺少 endpoint 或 token');
  }
  return parsed;
}

export class CloudSaveManager {
  private readonly filesRoot: string;
  private readonly context: common.UIAbilityContext;
  private readonly cloudRoot: string;
  private config: CloudConfig | null = null;
  private baseline: BaselineFile | null = null;
  private running: boolean = false;
  private uploadBusy: boolean = false;
  private uploadBlocked: boolean = false;
  private uploadTimer: number = -1;
  private disposed: boolean = false;
  private activeRequests: Set<http.HttpRequest> = new Set<http.HttpRequest>();
  private pendingConflictRevision: string = '';
  private pendingConflictRemoteHash: string = '';
  private pendingConflictLocalHash: string = '';

  constructor(filesRoot: string, context: common.UIAbilityContext) {
    this.filesRoot = filesRoot;
    this.context = context;
    this.cloudRoot = ensureCloudDirectory(filesRoot);
  }

  private configPath(): string {
    return this.cloudRoot + '/config.json';
  }

  private baselinePath(): string {
    return this.cloudRoot + '/baseline.json';
  }

  private normalizeEndpoint(value: string): string {
    let endpoint: string = value.trim();
    if (endpoint.length === 0) {
      endpoint = DEFAULT_ENDPOINT;
    }
    while (endpoint.endsWith('/')) {
      endpoint = endpoint.substring(0, endpoint.length - 1);
    }
    if (!endpoint.startsWith('https://') || endpoint.length === 'https://'.length ||
      endpoint.indexOf('?') >= 0 || endpoint.indexOf('#') >= 0 || endpoint.indexOf('@') >= 0) {
      throw new Error('云存档地址必须是 HTTPS 基础地址，且不能包含账号、查询或片段');
    }
    for (let index: number = 0; index < endpoint.length; index++) {
      const code: number = endpoint.charCodeAt(index);
      if (code <= 32 || code === 127) {
        throw new Error('云存档地址包含空白或控制字符');
      }
    }
    return endpoint;
  }

  private normalizeToken(value: string): string {
    const token: string = value.trim();
    if (token.length > 4096) {
      throw new Error('配对码过长');
    }
    for (let index: number = 0; index < token.length; index++) {
      const code: number = token.charCodeAt(index);
      if (code <= 32 || code === 127) {
        throw new Error('配对码不能包含空白或控制字符');
      }
    }
    return token;
  }

  private async loadConfig(): Promise<LoadedConfig> {
    const path: string = this.configPath();
    if (pathExists(path)) {
      const info: fileIo.Stat = fileIo.lstatSync(path);
      if (info.isSymbolicLink() || !info.isFile()) {
        throw new Error('files/cloud-save/config.json 不是普通文件');
      }
      const parsed: CloudConfigFile = JSON.parse(fileIo.readTextSync(path)) as CloudConfigFile;
      return {
        config: { endpoint: this.normalizeEndpoint(parsed.endpoint), token: this.normalizeToken(parsed.token) }
      };
    }
    try {
      const raw: Uint8Array = await this.context.resourceManager.getRawFileContent('cloud-save.json');
      const textValue: string = new util.TextDecoder().decodeToString(raw);
      const parsed: CloudConfigFile = validateBundledConfig(textValue);
      const config: CloudConfig = {
        endpoint: this.normalizeEndpoint(parsed.endpoint),
        token: this.normalizeToken(parsed.token)
      };
      if (config.token.length > 0) {
        this.writeConfig(config);
      }
      return { config: config };
    } catch (error) {
      return { config: { endpoint: DEFAULT_ENDPOINT, token: '' } };
    }
  }

  private writeConfig(config: CloudConfig): void {
    const stored: CloudConfigFile = {
      schema_version: CONFIG_SCHEMA,
      endpoint: config.endpoint,
      token: config.token
    };
    writeAtomicText(this.configPath(), JSON.stringify(stored));
  }

  private scopeFor(config: CloudConfig): string {
    return sha256Bytes(new util.TextEncoder().encodeInto(config.endpoint + '\0' + config.token));
  }

  private loadBaseline(scope: string): BaselineFile | null {
    const path: string = this.baselinePath();
    if (!pathExists(path)) {
      return null;
    }
    try {
      const parsed: BaselineFile = JSON.parse(fileIo.readTextSync(path)) as BaselineFile;
      if (parsed.schema_version !== BASELINE_SCHEMA || parsed.scope !== scope ||
        parsed.revision === undefined || parsed.snapshot_sha256 === undefined) {
        return null;
      }
      if (parsed.revision.length === 0 && parsed.snapshot_sha256.length !== 0) {
        return null;
      }
      return parsed;
    } catch (error) {
      return null;
    }
  }

  private saveBaseline(config: CloudConfig, state: CloudState): void {
    const baseline: BaselineFile = {
      schema_version: BASELINE_SCHEMA,
      scope: this.scopeFor(config),
      revision: state.revision,
      snapshot_sha256: state.snapshotSha256
    };
    writeAtomicText(this.baselinePath(), JSON.stringify(baseline));
    this.baseline = baseline;
  }

  private conflictResult(local: LocalSnapshot, remote: CloudState, message: string): CloudSaveStartupResult {
    this.pendingConflictRevision = remote.revision;
    this.pendingConflictRemoteHash = normalizedRemoteHash(remote.snapshotSha256);
    this.pendingConflictLocalHash = local.snapshotSha256;
    return startup('conflict', message, (this.config as CloudConfig).endpoint, stateConflict(local, remote));
  }

  private headers(): CloudHttpHeaders {
    const config: CloudConfig = this.config as CloudConfig;
    return {
      Authorization: 'Bearer ' + config.token,
      'Content-Type': 'application/json',
      'Accept': 'application/json',
      'User-Agent': 'D2RMobile-Cloud/1'
    };
  }

  private async request(method: http.RequestMethod, path: string, body: string,
    readTimeout: number): Promise<HttpResult> {
    // DNS overrides are application-scoped. Serialize requests, and remove our rule on every exit.
    const previous: Promise<void> = cloudNetworkTail;
    let unlock: () => void = () => {};
    cloudNetworkTail = new Promise<void>((resolve) => { unlock = resolve; });
    await previous;
    const startedAt: number = Date.now();
    let originRule: boolean = false;
    try {
      if (this.disposed) { throw new Error('云存档会话已结束'); }
      const config: CloudConfig = this.config as CloudConfig;
      if (config.endpoint === DEFAULT_ENDPOINT) {
        // This is the configured service's own origin. The HTTPS URL, SNI, hostname and CA validation
        // remain unchanged; no IP URL, certificate bypass or global/system DNS change is used.
        try {
          await connection.addCustomDnsRule(DEFAULT_ORIGIN_HOST, [DEFAULT_ORIGIN_IPV4]);
          originRule = true;
        } catch (ignored) { }
      }
      try {
        return await this.requestOnce(method, path, body, readTimeout, originRule ? 'origin' : 'public');
      } catch (error) {
        const code: number = (error as BusinessError).code ?? 0;
        // Only retry reads. A PUT may already have committed even when its response was lost.
        if (!originRule || this.disposed || method !== http.RequestMethod.GET ||
          [2300005, 2300006, 2300007, 2300016, 2300028, 2300035, 2300052, 2300055, 2300056].indexOf(code) < 0) {
          throw error as Error;
        }
        await connection.removeCustomDnsRule(DEFAULT_ORIGIN_HOST);
        originRule = false;
        return await this.requestOnce(method, path, body, readTimeout, 'public');
      }
    } catch (error) {
      const code: number = (error as BusinessError).code ?? 0;
      throw new Error(`云存档连接失败（${((Date.now() - startedAt) / 1000).toFixed(1)} 秒，错误 ${code}）：${messageOf(error as Error)}`);
    } finally {
      if (originRule) { try { await connection.removeCustomDnsRule(DEFAULT_ORIGIN_HOST); } catch (ignored) { } }
      unlock();
    }
  }

  private async requestOnce(method: http.RequestMethod, path: string, body: string,
    readTimeout: number, route: string): Promise<HttpResult> {
    if (this.disposed) { throw new Error('云存档会话已结束'); }
    const config: CloudConfig = this.config as CloudConfig;
    const request: http.HttpRequest = http.createHttp();
    const startedAt: number = Date.now();
    this.activeRequests.add(request);
    try {
      const options: http.HttpRequestOptions = {
        method: method,
        header: this.headers(),
        expectDataType: http.HttpDataType.STRING,
        usingCache: false,
        connectTimeout: CONNECT_TIMEOUT_MS,
        readTimeout: readTimeout,
        maxLimit: MAX_HTTP_BODY_BYTES,
        maxRedirects: 0
      };
      if (body.length > 0) {
        if (new util.TextEncoder().encodeInto(body).length > MAX_HTTP_BODY_BYTES) {
          throw new Error('云存档 HTTP 请求超过 48 MiB');
        }
        options.extraData = body;
      }
      const response: http.HttpResponse = await request.request(config.endpoint + path, options);
      this.recordNetwork({ at: startedAt, method: method, path: path, elapsedMs: Date.now() - startedAt,
        status: response.responseCode as number, errorCode: 0, route: route,
        remoteAddress: response.connectionExtraInfo?.remoteAddress ?? '', timing: response.performanceTiming ?? null });
      if (this.disposed) { throw new Error('云存档会话已结束'); }
      if (response.resultType !== http.HttpDataType.STRING || typeof response.result !== 'string') {
        throw new Error('云存档服务没有返回文本响应');
      }
      const responseBody: string = response.result as string;
      if (new util.TextEncoder().encodeInto(responseBody).length > MAX_HTTP_BODY_BYTES) {
        throw new Error('云存档 HTTP 响应超过 48 MiB');
      }
      return { status: response.responseCode as number, body: responseBody };
    } catch (error) {
      const code: number = (error as BusinessError).code ?? 0;
      const elapsed: number = Date.now() - startedAt;
      this.recordNetwork({ at: startedAt, method: method, path: path, elapsedMs: elapsed,
        status: 0, errorCode: code, route: route, remoteAddress: '', timing: null });
      throw error as Error;
    } finally {
      this.activeRequests.delete(request);
      try { request.destroy(); } catch (ignored) { }
    }
  }

  private recordNetwork(sample: NetworkDiagnostic): void {
    // Keep only timing/status, never authorization headers, response bodies or save contents.
    const summary: string = JSON.stringify(sample);
    try { hilog.info(0xD2E0, TAG, 'request %{public}s', summary); } catch (ignored) { }
    try { writeAtomicText(this.cloudRoot + '/network-last.json', summary); } catch (ignored) { }
  }

  private requireStatus(result: HttpResult, expected: number): void {
    if (result.status === expected) {
      return;
    }
    if (result.status >= 300 && result.status < 400) {
      throw new Error('云存档服务尝试重定向；已拒绝携带认证继续');
    }
    if (result.status === 401) {
      throw new Error('云存档认证失败（401）');
    }
    if (result.status === 413) {
      throw new Error('云存档超过服务端大小限制（413）');
    }
    if (result.status === 507) {
      throw new Error('云存档服务空间不足（507）');
    }
    throw new Error(`云存档服务返回 HTTP ${result.status}`);
  }

  private async getState(): Promise<CloudState> {
    const result: HttpResult = await this.request(http.RequestMethod.GET, '/state', '', SMALL_READ_TIMEOUT_MS);
    this.requireStatus(result, 200);
    return validateState(JSON.parse(result.body) as CloudStateWire);
  }

  private async getSnapshot(revision: string): Promise<CloudSnapshotWire> {
    const result: HttpResult = await this.request(http.RequestMethod.GET,
      '/snapshot?revision=' + encodeURIComponent(revision), '', TRANSFER_READ_TIMEOUT_MS);
    this.requireStatus(result, 200);
    return JSON.parse(result.body) as CloudSnapshotWire;
  }

  private async snapshotLocal(): Promise<LocalSnapshot> {
    const destination: string = uniqueChild(this.cloudRoot, 'snapshot-');
    const rc: number = dm.cloudSnapshot(this.filesRoot, destination);
    if (rc !== 0) {
      try {
        removeTree(destination);
      } catch (ignored) {
      }
      throw new Error(`无法建立本地存档快照（${rc}）`);
    }
    if (!pathExists(destination)) {
      fileIo.mkdirSync(destination, false);
    }
    return await verifyLocalSnapshot(destination);
  }

  private async readUploadFiles(snapshot: LocalSnapshot): Promise<UploadFile[]> {
    const base64: util.Base64Helper = new util.Base64Helper();
    const files: UploadFile[] = [];
    for (const file of snapshot.files) {
      const bytes: Uint8Array = readFileBytes(snapshot.root + '/' + file.path, file.size);
      if (sha256Bytes(bytes) !== file.sha256) {
        throw new Error(`本地快照在读取时发生变化：${file.path}`);
      }
      files.push({
        path: file.path,
        size: file.size,
        sha256: file.sha256,
        data: base64.encodeToStringSync(bytes, util.Type.BASIC)
      });
    }
    return files;
  }

  private parseConflictState(body: string): CloudState {
    const conflict: CloudConflictWire = JSON.parse(body) as CloudConflictWire;
    if (conflict.state === undefined) {
      throw new Error('云存档 409 响应缺少当前 state');
    }
    return validateState(conflict.state);
  }

  private async putSnapshot(snapshot: LocalSnapshot, baseRevision: string): Promise<PutResult> {
    const files: UploadFile[] = await this.readUploadFiles(snapshot);
    const body: PutRequest = {
      base_revision: baseRevision,
      device: 'HarmonyOS',
      snapshot_sha256: snapshot.snapshotSha256,
      files: files
    };
    const result: HttpResult = await this.request(http.RequestMethod.PUT, '/snapshot',
      JSON.stringify(body), TRANSFER_READ_TIMEOUT_MS);
    if (result.status === 409) {
      return { conflict: true, state: this.parseConflictState(result.body) };
    }
    this.requireStatus(result, 200);
    const state: CloudState = validateState(JSON.parse(result.body) as CloudStateWire);
    if (normalizedRemoteHash(state.snapshotSha256) !== snapshot.snapshotSha256) {
      throw new Error('云存档上传后返回的快照哈希不一致');
    }
    return { conflict: false, state: state };
  }

  private ensureStageDirectory(path: string): void {
    if (pathExists(path)) {
      throw new Error(`恢复暂存目录已存在：${path}`);
    }
    fileIo.mkdirSync(path, false);
  }

  private writeValidatedFile(root: string, relative: string, bytes: Uint8Array): void {
    const parts: string[] = relative.split('/');
    let directory: string = root;
    for (let index: number = 0; index < parts.length - 1; index++) {
      directory += '/' + parts[index];
      if (!pathExists(directory)) {
        fileIo.mkdirSync(directory, false);
      }
      const info: fileIo.Stat = fileIo.lstatSync(directory);
      if (info.isSymbolicLink() || !info.isDirectory()) {
        throw new Error(`恢复路径前缀不是目录：${relative}`);
      }
    }
    const target: string = root + '/' + relative;
    if (pathExists(target)) {
      throw new Error(`恢复文件目标已存在：${relative}`);
    }
    const file: fileIo.File = fileIo.openSync(target,
      fileIo.OpenMode.CREATE | fileIo.OpenMode.WRITE_ONLY |
      fileIo.OpenMode.NOFOLLOW | fileIo.OpenMode.SYNC);
    try {
      writeAllFd(file.fd, bytes.buffer);
      fileIo.fsyncSync(file.fd);
    } finally {
      fileIo.closeSync(file);
    }
  }

  private mergeExcludedFile(source: string, destination: string): void {
    if (pathExists(destination)) {
      throw new Error(`无法保留本地额外文件，工作区已有同名项：${destination}`);
    }
    const info: fileIo.Stat = fileIo.lstatSync(source);
    if (info.isSymbolicLink() || !info.isFile() || info.size > Number.MAX_SAFE_INTEGER) {
      throw new Error(`被排除项不是可安全保留的普通文件：${source}`);
    }
    const bytes: Uint8Array = readFileBytes(source, info.size);
    const file: fileIo.File = fileIo.openSync(destination,
      fileIo.OpenMode.CREATE | fileIo.OpenMode.WRITE_ONLY |
      fileIo.OpenMode.NOFOLLOW | fileIo.OpenMode.SYNC);
    try {
      writeAllFd(file.fd, bytes.buffer);
      fileIo.fsyncSync(file.fd);
    } finally {
      fileIo.closeSync(file);
    }
  }

  private mergeExcludedTree(sourceRoot: string, sourceRelative: string, destinationRoot: string): void {
    const source: string = sourceRoot + '/' + sourceRelative;
    const destination: string = destinationRoot + '/' + sourceRelative;
    const info: fileIo.Stat = fileIo.lstatSync(source);
    if (info.isSymbolicLink()) {
      throw new Error(`无法在恢复时安全保留符号链接：${sourceRelative}`);
    }
    if (info.isFile()) {
      this.mergeExcludedFile(source, destination);
      return;
    }
    if (!info.isDirectory()) {
      throw new Error(`无法在恢复时保留特殊文件：${sourceRelative}`);
    }
    if (!pathExists(destination)) {
      fileIo.mkdirSync(destination, false);
    }
    const destinationInfo: fileIo.Stat = fileIo.lstatSync(destination);
    if (destinationInfo.isSymbolicLink() || !destinationInfo.isDirectory()) {
      throw new Error(`无法保留本地额外目录，工作区已有同名文件：${sourceRelative}`);
    }
    for (const name of fileIo.listFileSync(source)) {
      const childRelative: string = sourceRelative.length === 0 ? name : sourceRelative + '/' + name;
      this.mergeExcludedTree(sourceRoot, childRelative, destinationRoot);
    }
  }

  private preserveExcluded(localRoot: string, stagedRoot: string): void {
    const save: string = localRoot + '/save';
    if (!pathExists(save)) {
      return;
    }
    const saveInfo: fileIo.Stat = fileIo.lstatSync(save);
    if (saveInfo.isSymbolicLink() || !saveInfo.isDirectory()) {
      throw new Error('files/save 不是普通目录，无法保留被排除项');
    }
    for (const name of fileIo.listFileSync(save)) {
      const info: fileIo.Stat = fileIo.lstatSync(save + '/' + name);
      if (name === 'Settings' && info.isDirectory()) {
        this.mergeExcludedTree(save, name, stagedRoot);
      }
    }
    this.preserveExcludedDescendants(save, '', stagedRoot);
  }

  private preserveExcludedDescendants(sourceRoot: string, relative: string, stagedRoot: string): void {
    const directory: string = relative.length === 0 ? sourceRoot : sourceRoot + '/' + relative;
    for (const name of fileIo.listFileSync(directory)) {
      const childRelative: string = relative.length === 0 ? name : relative + '/' + name;
      const child: string = sourceRoot + '/' + childRelative;
      const info: fileIo.Stat = fileIo.lstatSync(child);
      if (relative.length === 0 && name === 'Settings') {
        continue;
      }
      if (isExcludedComponent(name)) {
        this.mergeExcludedTree(sourceRoot, childRelative, stagedRoot);
        continue;
      }
      if (info.isSymbolicLink()) {
        throw new Error(`本地存档包含符号链接：${childRelative}`);
      }
      if (info.isDirectory()) {
        this.preserveExcludedDescendants(sourceRoot, childRelative, stagedRoot);
      }
    }
  }

  private async verifyRestoredStage(stage: string, remote: ValidatedRemoteSnapshot): Promise<void> {
    const expected: Map<string, string> = new Map<string, string>();
    for (const file of remote.files) {
      expected.set(file.path, file.sha256);
    }
    for (const file of remote.files) {
      const path: string = stage + '/' + file.path;
      const info: fileIo.Stat = fileIo.lstatSync(path);
      if (info.isSymbolicLink() || !info.isFile() || info.size !== file.size) {
        throw new Error(`恢复文件落盘校验失败：${file.path}`);
      }
      const digest: string = await hash.hash(path, 'sha256');
      if (digest.toLowerCase() !== file.sha256) {
        throw new Error(`恢复文件哈希校验失败：${file.path}`);
      }
    }
  }

  private async installRemote(config: CloudConfig, state: CloudState): Promise<void> {
    const wire: CloudSnapshotWire = await this.getSnapshot(state.revision);
    const remote: ValidatedRemoteSnapshot = validateRemoteFiles(wire, state);
    const stage: string = uniqueChild(this.cloudRoot, 'restore-');
    this.ensureStageDirectory(stage);
    try {
      for (const file of remote.files) {
        this.writeValidatedFile(stage, file.path, file.bytes);
      }
      await this.verifyRestoredStage(stage, remote);
      this.preserveExcluded(this.filesRoot, stage);
      const latest: CloudState = await this.getState();
      if (latest.revision !== state.revision ||
        normalizedRemoteHash(latest.snapshotSha256) !== normalizedRemoteHash(state.snapshotSha256)) {
        throw new RemoteChangedError(latest);
      }
      const rc: number = dm.cloudInstall(this.filesRoot, stage);
      if (rc !== 0) {
        throw new Error(`无法安装云存档（${rc}）；${stage} 已保留`);
      }
      this.saveBaseline(config, state);
    } catch (error) {
      if (error instanceof RemoteChangedError) {
        throw error;
      }
      throw new Error(`${messageOf(error as Error)}；恢复暂存/备份保留在 ${stage}`);
    }
  }

  private async acknowledge(config: CloudConfig, state: CloudState): Promise<CloudSaveStartupResult> {
    if (this.disposed) { throw new Error('云存档会话已结束'); }
    this.saveBaseline(config, state);
    this.running = true;
    this.uploadBlocked = false;
    this.pendingConflictRevision = '';
    this.pendingConflictRemoteHash = '';
    this.pendingConflictLocalHash = '';
    this.startUploadTimer();
    return startup('ready', '', config.endpoint, null);
  }

  private startUploadTimer(): void {
    if (this.disposed) { return; }
    if (this.uploadTimer >= 0) {
      clearInterval(this.uploadTimer);
    }
    this.uploadTimer = setInterval(() => {
      this.uploadCommittedSnapshot();
    }, UPLOAD_INTERVAL_MS);
  }

  async initializeForStartup(): Promise<CloudSaveStartupResult> {
    this.running = false;
    this.uploadBlocked = false;
    try {
      const loaded: LoadedConfig = await this.loadConfig();
      this.config = loaded.config;
      if (this.config.token.length === 0) {
        return startup('setup', '请设置配对信息，或选择离线启动', this.config.endpoint, null);
      }
      const scope: string = this.scopeFor(this.config);
      this.baseline = this.loadBaseline(scope);

      const local: LocalSnapshot = await this.snapshotLocal();
      try {
        const remote: CloudState = await this.getState();
        const localHash: string = local.snapshotSha256;
        const remoteHash: string = normalizedRemoteHash(remote.snapshotSha256);
        const baselineHash: string = this.baseline === null ? EMPTY_SHA256 :
          normalizedRemoteHash(this.baseline.snapshot_sha256);
        const localChanged: boolean = this.baseline !== null && localHash !== baselineHash;
        const remoteChanged: boolean = this.baseline !== null && remoteHash !== baselineHash;
        const remoteDisappeared: boolean = this.baseline !== null &&
          this.baseline.snapshot_sha256.length > 0 && remote.revision.length === 0;

        if (!remoteDisappeared && localHash === remoteHash) {
          return await this.acknowledge(this.config, remote);
        }
        if (remoteDisappeared || (this.baseline !== null && localChanged && remoteChanged)) {
          return this.conflictResult(local, remote, '云端与本地都有独立变化，请明确选择覆盖方向');
        }
        if (this.baseline === null && localHash !== EMPTY_SHA256 && remoteHash !== EMPTY_SHA256) {
          return this.conflictResult(local, remote, '云端与本地已有不同存档，请明确选择覆盖方向');
        }
        if (this.baseline !== null && localChanged && !remoteChanged) {
          const put: PutResult = await this.putSnapshot(local, remote.revision);
          if (put.conflict) {
            return this.conflictResult(local, put.state, '上传前云端已变化，请重新选择覆盖方向');
          }
          return await this.acknowledge(this.config, put.state);
        }
        if (this.baseline !== null && remoteChanged && !localChanged) {
          try {
            await this.installRemote(this.config, remote);
          } catch (error) {
            if (error instanceof RemoteChangedError) {
              return this.conflictResult(local, error.state, '恢复期间云端再次变化，请重新选择覆盖方向');
            }
            throw new Error(messageOf(error as Error));
          }
          this.running = true;
          this.startUploadTimer();
          return startup('ready', '已下载云端存档', this.config.endpoint, null);
        }
        if (localHash !== EMPTY_SHA256 && remoteHash === EMPTY_SHA256) {
          const put: PutResult = await this.putSnapshot(local, remote.revision);
          if (put.conflict) {
            return this.conflictResult(local, put.state, '上传前云端已变化，请重新选择覆盖方向');
          }
          return await this.acknowledge(this.config, put.state);
        }
        if (localHash === EMPTY_SHA256 && remoteHash !== EMPTY_SHA256) {
          try {
            await this.installRemote(this.config, remote);
          } catch (error) {
            if (error instanceof RemoteChangedError) {
              return this.conflictResult(local, error.state, '恢复期间云端再次变化，请重新选择覆盖方向');
            }
            throw new Error(messageOf(error as Error));
          }
          this.running = true;
          this.startUploadTimer();
          return startup('ready', '已下载云端存档', this.config.endpoint, null);
        }
        return await this.acknowledge(this.config, remote);
      } finally {
        try {
          removeTree(local.root);
        } catch (error) {
          hilog.warn(0xD2E0, TAG, 'snapshot cleanup failed');
        }
      }
    } catch (error) {
      return startup('network', messageOf(error as Error), this.config === null ? DEFAULT_ENDPOINT :
        this.config.endpoint, null);
    }
  }

  async saveConfig(endpoint: string, token: string): Promise<CloudSaveStartupResult> {
    try {
      const config: CloudConfig = {
        endpoint: this.normalizeEndpoint(endpoint),
        token: this.normalizeToken(token)
      };
      if (config.token.length === 0) {
        throw new Error('配对码不能为空');
      }
      const previous: CloudConfig | null = this.config;
      const previousScope: string = previous === null ? '' : this.scopeFor(previous);
      this.writeConfig(config);
      if (this.scopeFor(config) !== previousScope) {
        this.baseline = null;
      }
      this.config = config;
      return await this.initializeForStartup();
    } catch (error) {
      return startup('setup', messageOf(error as Error), endpoint, null);
    }
  }

  startOffline(): void {
    this.running = true;
    this.uploadBlocked = true;
  }

  async resolveConflict(choice: string): Promise<CloudSaveStartupResult> {
    if (this.config === null || this.config.token.length === 0) {
      return startup('setup', '云存档配对信息已失效，请重新设置', DEFAULT_ENDPOINT, null);
    }
    const config: CloudConfig = this.config;
    let local: LocalSnapshot | null = null;
    try {
      local = await this.snapshotLocal();
      const remote: CloudState = await this.getState();
      if (remote.revision !== this.pendingConflictRevision ||
        normalizedRemoteHash(remote.snapshotSha256) !== this.pendingConflictRemoteHash ||
        local.snapshotSha256 !== this.pendingConflictLocalHash) {
        return this.conflictResult(local, remote, '冲突页面显示后存档已变化，请重新选择覆盖方向');
      }
      if (choice === 'cloud') {
        if (remote.revision.length === 0) {
          return this.conflictResult(local, remote, '云端版本已消失，请重新选择');
        }
        try {
          await this.installRemote(config, remote);
        } catch (error) {
          if (error instanceof RemoteChangedError) {
            return this.conflictResult(local, error.state, '恢复期间云端再次变化，请重新选择覆盖方向');
          }
          throw new Error(messageOf(error as Error));
        }
        this.running = true;
        this.uploadBlocked = false;
        this.startUploadTimer();
        return startup('ready', '云端存档已覆盖本地', config.endpoint, null);
      }
      if (choice === 'local') {
        const put: PutResult = await this.putSnapshot(local, remote.revision);
        if (put.conflict) {
          return this.conflictResult(local, put.state, '覆盖前云端再次变化，请重新选择');
        }
        return await this.acknowledge(config, put.state);
      }
      return this.conflictResult(local, remote, '未知的冲突选择');
    } catch (error) {
      return startup('network', messageOf(error as Error), config.endpoint, null);
    } finally {
      if (local !== null) {
        try {
          removeTree(local.root);
        } catch (ignored) {
        }
      }
    }
  }

  async uploadCommittedSnapshot(): Promise<void> {
    if (this.disposed || !this.running || this.uploadBusy || this.uploadBlocked || this.config === null || this.baseline === null) {
      return;
    }
    this.uploadBusy = true;
    let local: LocalSnapshot | null = null;
    try {
      local = await this.snapshotLocal();
      if (local.snapshotSha256 === normalizedRemoteHash(this.baseline.snapshot_sha256)) {
        return;
      }
      const remote: CloudState = await this.getState();
      const remoteHash: string = normalizedRemoteHash(remote.snapshotSha256);
      if (remote.revision !== this.baseline.revision || remoteHash !== normalizedRemoteHash(this.baseline.snapshot_sha256)) {
        this.uploadBlocked = true;
        return;
      }
      if (local.snapshotSha256 === remoteHash) {
        this.saveBaseline(this.config, remote);
        return;
      }
      const put: PutResult = await this.putSnapshot(local, remote.revision);
      if (put.conflict) {
        this.uploadBlocked = true;
        return;
      }
      this.saveBaseline(this.config, put.state);
    } catch (error) {
      hilog.warn(0xD2E0, TAG, 'background upload failed: %{public}s', messageOf(error as Error));
    } finally {
      if (local !== null) {
        try {
          removeTree(local.root);
        } catch (ignored) {
        }
      }
      this.uploadBusy = false;
    }
  }

  dispose(): void {
    this.disposed = true;
    this.running = false;
    this.uploadBlocked = true;
    if (this.uploadTimer >= 0) { clearInterval(this.uploadTimer); this.uploadTimer = -1; }
    this.activeRequests.forEach((request: http.HttpRequest) => {
      try { request.destroy(); } catch (ignored) { }
    });
    this.activeRequests.clear();
  }
}

let activeManager: CloudSaveManager | null = null;
let activeFilesRoot: string = '';

export function getCloudSaveManager(filesRoot: string, context: common.UIAbilityContext): CloudSaveManager {
  if (activeManager === null || activeFilesRoot !== filesRoot) {
    if (activeManager !== null) { activeManager.dispose(); }
    activeManager = new CloudSaveManager(filesRoot, context);
    activeFilesRoot = filesRoot;
  }
  return activeManager;
}

export function cloudSaveOnBackground(filesRoot: string): void {
  if (activeManager === null || activeFilesRoot !== filesRoot) {
    return;
  }
  activeManager.uploadCommittedSnapshot();
}

export function cloudSaveOnDestroy(filesRoot: string): void {
  if (activeManager !== null && activeFilesRoot === filesRoot) {
    activeManager.dispose();
    activeManager = null;
    activeFilesRoot = '';
  }
}
