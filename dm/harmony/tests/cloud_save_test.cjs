'use strict';

const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const sourceRoot = path.resolve(__dirname, '../../..');
const cloudSaveSource = path.join(sourceRoot,
  'dm/harmony/app/entry/src/main/ets/utils/CloudSave.ets');
const testRoot = process.env.CLOUD_SAVE_TEST_ROOT ?
  path.resolve(process.env.CLOUD_SAVE_TEST_ROOT) :
  path.join(path.dirname(sourceRoot), 'work/cloud-save/harmony-tests');
const typescriptPath = process.env.TYPESCRIPT_PATH ||
  '/Applications/DevEco-Studio.app/Contents/sdk/default/openharmony/ets/build-tools/ets-loader/node_modules/typescript';
const ts = require(typescriptPath);
const endpoint = 'https://cloud-save.test/v1';
const defaultEndpoint = 'https://mythstribe.com/d2r-cloud/v1';
const defaultOriginHost = 'mythstribe.com';
const defaultOriginIpv4 = '120.53.45.109';
const token = 'harmony-state-machine-test-token';
const emptySha256 = sha256('');

function sha256(value) {
  return crypto.createHash('sha256').update(value).digest('hex');
}

function snapshotDigest(files) {
  let canonical = '';
  for (const file of files) {
    canonical += `${file.path}\0${file.size}\0${file.sha256}\n`;
  }
  return sha256(canonical);
}

function comparePath(left, right) {
  return left < right ? -1 : left > right ? 1 : 0;
}

function blankState() {
  return {
    revision: '',
    snapshot_sha256: '',
    updated_at: '',
    device: '',
    file_count: 0,
    total_bytes: 0,
  };
}

class MemoryCloudServer {
  constructor() {
    this.current = blankState();
    this.versions = new Map();
    this.nextRevision = 1;
    this.requests = {
      state: 0,
      snapshot: 0,
      put: 0,
      unauthorized: 0,
      invalidPut: 0,
    };
    this.conflictOnPut = null;
    this.snapshotTransform = null;
  }

  setSnapshot(contents, options = {}) {
    const files = Object.entries(contents)
      .map(([filePath, value]) => {
        const bytes = Buffer.isBuffer(value) ? value : Buffer.from(String(value));
        return {
          path: filePath,
          size: bytes.length,
          sha256: sha256(bytes),
          data: bytes.toString('base64'),
        };
      })
      .sort((left, right) => comparePath(left.path, right.path));
    const state = {
      revision: `r${this.nextRevision++}`,
      snapshot_sha256: snapshotDigest(files),
      updated_at: options.updatedAt || `2026-11-21T10:00:0${this.nextRevision % 10}Z`,
      device: options.device || 'HarmonyOS',
      file_count: files.length,
      total_bytes: files.reduce((sum, file) => sum + file.size, 0),
    };
    const snapshot = { ...state, files };
    this.current = state;
    this.versions.set(state.revision, snapshot);
    return state;
  }

  dispatch(method, rawUrl, options) {
    if (options.header?.Authorization !== `Bearer ${token}`) {
      this.requests.unauthorized += 1;
      return { status: 401, body: '{"error":"unauthorized"}' };
    }
    const url = new URL(rawUrl);
    if (method === 'GET' && url.pathname.endsWith('/state')) {
      this.requests.state += 1;
      return { status: 200, body: JSON.stringify(this.current) };
    }
    if (method === 'GET' && url.pathname.endsWith('/snapshot')) {
      this.requests.snapshot += 1;
      const revision = url.searchParams.get('revision') || '';
      const stored = this.versions.get(revision);
      if (!stored) {
        return { status: 404, body: '{"error":"not_found"}' };
      }
      let snapshot = JSON.parse(JSON.stringify(stored));
      if (this.snapshotTransform !== null) {
        snapshot = this.snapshotTransform(snapshot);
      }
      return { status: 200, body: JSON.stringify(snapshot) };
    }
    if (method === 'PUT' && url.pathname.endsWith('/snapshot')) {
      this.requests.put += 1;
      if (this.conflictOnPut !== null) {
        const mutate = this.conflictOnPut;
        this.conflictOnPut = null;
        mutate();
      }
      const request = JSON.parse(options.extraData || '{}');
      const validation = this.validatePut(request);
      if (!validation.ok) {
        this.requests.invalidPut += 1;
        return { status: 400, body: JSON.stringify({ error: validation.error }) };
      }
      if (request.base_revision !== this.current.revision) {
        return {
          status: 409,
          body: JSON.stringify({ error: 'conflict', state: this.current }),
        };
      }
      if (this.current.snapshot_sha256 === request.snapshot_sha256) {
        return { status: 200, body: JSON.stringify(this.current) };
      }
      const state = this.setSnapshot(validation.contents, { device: request.device });
      return { status: 200, body: JSON.stringify(state) };
    }
    return { status: 404, body: '{"error":"not_found"}' };
  }

  validatePut(request) {
    if (!request || typeof request.base_revision !== 'string' ||
        typeof request.snapshot_sha256 !== 'string' || !Array.isArray(request.files)) {
      return { ok: false, error: 'invalid_request' };
    }
    const contents = Object.create(null);
    const manifest = [];
    let totalBytes = 0;
    let previous = '';
    for (const row of request.files) {
      if (!row || typeof row.path !== 'string' || typeof row.size !== 'number' ||
          typeof row.sha256 !== 'string' || typeof row.data !== 'string') {
        return { ok: false, error: 'invalid_file' };
      }
      if (previous && comparePath(previous, row.path) >= 0) {
        return { ok: false, error: 'unsorted_files' };
      }
      previous = row.path;
      const bytes = Buffer.from(row.data, 'base64');
      if (bytes.length !== row.size || sha256(bytes) !== row.sha256) {
        return { ok: false, error: 'file_sha256' };
      }
      contents[row.path] = bytes;
      manifest.push({ path: row.path, size: row.size, sha256: row.sha256 });
      totalBytes += row.size;
    }
    if (snapshotDigest(manifest) !== request.snapshot_sha256) {
      return { ok: false, error: 'snapshot_sha256' };
    }
    if (this.current.total_bytes === totalBytes && manifest.length === this.current.file_count &&
        request.snapshot_sha256 === this.current.snapshot_sha256) {
      return { ok: true, contents };
    }
    return { ok: true, contents };
  }
}

function makeFileIo() {
  const constants = fs.constants;
  let vmFillBuffer = null;
  let vmReadBuffer = null;
  const openMode = {
    READ_ONLY: 1,
    WRITE_ONLY: 2,
    CREATE: 4,
    NOFOLLOW: 8,
    SYNC: 16,
    DIR: 32,
  };

  function asUint8Array(value) {
    if (Buffer.isBuffer(value)) {
      return value;
    }
    if (ArrayBuffer.isView(value)) {
      return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
    }
    if (Object.prototype.toString.call(value) === '[object ArrayBuffer]') {
      if (vmReadBuffer !== null) {
        return Buffer.from(vmReadBuffer(value));
      }
      return new Uint8Array(value);
    }
    throw new TypeError('fileIo buffer expected');
  }

  function stat(value) {
    return {
      size: value.size,
      isSymbolicLink() {
        return value.isSymbolicLink();
      },
      isDirectory() {
        return value.isDirectory();
      },
      isFile() {
        return value.isFile();
      },
    };
  }

  function descriptorNumber(descriptor) {
    return typeof descriptor === 'number' ? descriptor : descriptor.fd;
  }

  return {
    OpenMode: openMode,
    setVmHelpers(fillBuffer, readBuffer) {
      vmFillBuffer = fillBuffer;
      vmReadBuffer = readBuffer;
    },
    accessSync(filePath) {
      return fs.existsSync(filePath);
    },
    mkdirSync(directory, recursive) {
      fs.mkdirSync(directory, { recursive: recursive === true });
    },
    lstatSync(filePath) {
      return stat(fs.lstatSync(filePath));
    },
    listFileSync(directory) {
      return fs.readdirSync(directory);
    },
    openSync(filePath, mode) {
      let flags;
      if ((mode & openMode.DIR) !== 0) {
        flags = constants.O_RDONLY;
      } else if ((mode & openMode.WRITE_ONLY) !== 0) {
        flags = constants.O_WRONLY | ((mode & openMode.CREATE) !== 0 ? constants.O_CREAT : 0);
      } else {
        flags = constants.O_RDONLY;
      }
      flags |= (mode & openMode.NOFOLLOW) !== 0 ? (constants.O_NOFOLLOW || 0) : 0;
      flags |= (mode & openMode.SYNC) !== 0 ? constants.O_SYNC : 0;
      return { fd: fs.openSync(filePath, flags, 0o600) };
    },
    readSync(descriptor, buffer, options = {}) {
      const target = asUint8Array(buffer);
      const temporary = Buffer.alloc(target.byteLength);
      const count = fs.readSync(descriptorNumber(descriptor), temporary, 0,
        temporary.byteLength, options.offset ?? null);
      if (Object.prototype.toString.call(buffer) === '[object ArrayBuffer]' &&
          !(buffer instanceof ArrayBuffer) && vmFillBuffer !== null) {
        vmFillBuffer(buffer, Array.from(temporary.subarray(0, count)));
      } else {
        for (let index = 0; index < count; index++) {
          target[index] = temporary[index];
        }
      }
      return count;
    },
    writeSync(descriptor, value, options = {}) {
      const source = Buffer.from(asUint8Array(value));
      return fs.writeSync(descriptorNumber(descriptor), source, 0, source.byteLength,
        options.offset ?? null);
    },
    fsyncSync(descriptor) {
      fs.fsyncSync(descriptorNumber(descriptor));
    },
    closeSync(descriptor) {
      fs.closeSync(descriptor.fd);
    },
    readTextSync(filePath) {
      return fs.readFileSync(filePath, 'utf8');
    },
    renameSync(source, destination) {
      fs.renameSync(source, destination);
    },
    unlinkSync(filePath) {
      fs.unlinkSync(filePath);
    },
    rmdirSync(directory) {
      fs.rmdirSync(directory);
    },
  };
}

function copyTree(source, destination) {
  const sourceStat = fs.lstatSync(source);
  if (sourceStat.isSymbolicLink()) {
    fs.symlinkSync(fs.readlinkSync(source), destination);
    return;
  }
  if (sourceStat.isDirectory()) {
    fs.mkdirSync(destination);
    for (const name of fs.readdirSync(source)) {
      copyTree(path.join(source, name), path.join(destination, name));
    }
    return;
  }
  fs.copyFileSync(source, destination, fs.constants.COPYFILE_EXCL);
}

function makeNativeMock() {
  const mock = {
    snapshots: 0,
    installs: 0,
    backups: [],
    cloudSnapshot(filesRoot, destination) {
      try {
        const save = path.join(filesRoot, 'save');
        if (fs.existsSync(save)) {
          copyTree(save, destination);
        } else {
          fs.mkdirSync(destination);
        }
        mock.snapshots += 1;
        return 0;
      } catch (error) {
        return -1;
      }
    },
    cloudInstall(filesRoot, stagedDirectory) {
      try {
        const save = path.join(filesRoot, 'save');
        const backup = path.join(filesRoot, 'cloud-save', `native-backup-${mock.installs + 1}`);
        if (fs.existsSync(save)) {
          fs.renameSync(save, backup);
          fs.renameSync(stagedDirectory, save);
          fs.renameSync(backup, stagedDirectory);
        } else {
          fs.renameSync(stagedDirectory, save);
          fs.mkdirSync(stagedDirectory);
        }
        mock.installs += 1;
        mock.backups.push(stagedDirectory);
        return 0;
      } catch (error) {
        return -1;
      }
    },
  };
  return mock;
}

function loadCloudSave() {
  const server = new MemoryCloudServer();
  const fileIo = makeFileIo();
  const native = makeNativeMock();
  const timers = new Map();
  const trace = [];
  const dnsRules = new Map();
  let nextTimer = 1;
  const arkUtil = {
    TextEncoder: class TextEncoderMock {
      encodeInto(value) {
        return new Uint8Array(Buffer.from(value, 'utf8'));
      }
    },
    TextDecoder: class TextDecoderMock {
      decodeToString(value) {
        return Buffer.from(value).toString('utf8');
      }
    },
    Base64Helper: class Base64HelperMock {
      encodeToStringSync(value) {
        return Buffer.from(value).toString('base64');
      }
      decodeSync(value) {
        return new Uint8Array(Buffer.from(value, 'base64'));
      }
    },
    Type: { BASIC: 0 },
  };
  const cryptoKit = {
    cryptoFramework: {
      createMd(algorithm) {
        assert.equal(algorithm, 'SHA256');
        const hash = crypto.createHash('sha256');
        return {
          updateSync(input) {
            hash.update(Buffer.from(input.data));
          },
          digestSync() {
            return { data: new Uint8Array(hash.digest()) };
          },
        };
      },
    },
  };
  const coreFileKit = {
    fileIo,
    hash: {
      async hash(filePath, algorithm) {
        assert.equal(algorithm, 'sha256');
        return sha256(await fs.promises.readFile(filePath));
      },
    },
  };
  function activeDnsRules() {
    const result = {};
    for (const host of Array.from(dnsRules.keys()).sort()) {
      result[host] = dnsRules.get(host).slice();
    }
    return result;
  }

  function record(type, fields = {}) {
    trace.push({ sequence: trace.length + 1, type, ...fields });
  }

  const connection = {
    beforeRemove: null,
    async addCustomDnsRule(host, addresses) {
      const copiedAddresses = Array.from(addresses);
      dnsRules.set(host, copiedAddresses);
      record('dns-add', { host, addresses: copiedAddresses, active: activeDnsRules() });
    },
    async removeCustomDnsRule(host) {
      record('dns-remove-start', { host, active: activeDnsRules() });
      if (typeof this.beforeRemove === 'function') {
        const hook = this.beforeRemove;
        this.beforeRemove = null;
        await hook(host);
      }
      dnsRules.delete(host);
      record('dns-remove', { host, active: activeDnsRules() });
    },
    activeRules() {
      return activeDnsRules();
    },
  };
  const networkKit = {
    connection,
    http: {
      RequestMethod: { GET: 'GET', PUT: 'PUT' },
      HttpDataType: { STRING: 0 },
      createHttp() {
        return {
          async request(rawUrl, options) {
            record('request', {
              method: options.method,
              url: rawUrl,
              headers: { ...options.header },
              hasBody: typeof options.extraData === 'string' && options.extraData.length > 0,
              active: activeDnsRules(),
            });
            const response = await server.dispatch(options.method, rawUrl, options);
            return {
              responseCode: response.status,
              resultType: 0,
              result: response.body,
            };
          },
          destroy() {
          },
        };
      },
    },
  };
  const mockRequire = (name) => {
    switch (name) {
      case '@kit.AbilityKit':
        return { common: {} };
      case '@kit.ArkTS':
        return { util: arkUtil };
      case '@kit.CryptoArchitectureKit':
        return cryptoKit;
      case '@kit.CoreFileKit':
        return coreFileKit;
      case '@kit.NetworkKit':
        return networkKit;
      case '@kit.PerformanceAnalysisKit':
        return { hilog: { warn() {}, info() {} } };
      case '@kit.BasicServicesKit':
        return {};
      case 'libdm.so':
        return { __esModule: true, default: native };
      default:
        throw new Error(`unexpected import: ${name}`);
    }
  };
  const transpiled = ts.transpileModule(fs.readFileSync(cloudSaveSource, 'utf8'), {
    fileName: 'CloudSave.ets',
    reportDiagnostics: true,
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2020,
    },
  });
  const diagnostics = transpiled.diagnostics || [];
  assert.equal(diagnostics.length, 0,
    diagnostics.map((item) => ts.flattenDiagnosticMessageText(item.messageText, '\n')).join('\n'));
  const module = { exports: {} };
  const sandbox = {
    module,
    exports: module.exports,
    require: mockRequire,
    __filename: cloudSaveSource,
    __dirname: path.dirname(cloudSaveSource),
    console,
    setInterval(callback, milliseconds) {
      const id = nextTimer++;
      timers.set(id, { callback, milliseconds });
      return id;
    },
    clearInterval(id) {
      timers.delete(id);
    },
    setTimeout,
    clearTimeout,
    Map,
    Set,
    Error,
    Number,
    Date,
  };
  vm.runInNewContext(transpiled.outputText, sandbox, { filename: cloudSaveSource });
  vm.runInContext(`
    globalThis.__cloudSaveFillBuffer = function (raw, values) {
      const target = new Uint8Array(raw);
      for (let index = 0; index < values.length; index++) target[index] = values[index];
    };
    globalThis.__cloudSaveReadBuffer = function (raw) {
      const source = new Uint8Array(raw);
      const result = new Array(source.length);
      for (let index = 0; index < source.length; index++) result[index] = source[index];
      return result;
    };
  `, sandbox);
  fileIo.setVmHelpers(sandbox.__cloudSaveFillBuffer, sandbox.__cloudSaveReadBuffer);
  return { exports: module.exports, server, native, fileIo, timers, trace, connection };
}

function writeFile(filePath, value) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
  fs.writeFileSync(filePath, value);
}

function makeEnvironment(name) {
  const root = path.join(testRoot, name);
  fs.rmSync(root, { recursive: true, force: true });
  const filesRoot = path.join(root, 'files');
  fs.mkdirSync(path.join(filesRoot, 'save'), { recursive: true });
  const platform = loadCloudSave();
  const context = {
    resourceManager: {
      async getRawFileContent() {
        throw new Error('no bundled cloud-save.json');
      },
    },
  };
  return {
    root,
    filesRoot,
    saveRoot: path.join(filesRoot, 'save'),
    context,
    ...platform,
  };
}

function configureToken(environment) {
  configureEndpoint(environment, endpoint);
}

function configureEndpoint(environment, configuredEndpoint) {
  writeFile(path.join(environment.filesRoot, 'cloud-save/config.json'),
    JSON.stringify({ schema_version: 1, endpoint: configuredEndpoint, token }));
}

function makeNetworkError(code, message) {
  const error = new Error(message);
  error.code = code;
  return error;
}

function traceOf(environment, type) {
  return environment.trace.filter((entry) => entry.type === type);
}

function requestTrace(environment, method = null) {
  const requests = traceOf(environment, 'request');
  return method === null ? requests : requests.filter((entry) => entry.method === method);
}

async function flushAsyncWork() {
  await new Promise((resolve) => setImmediate(resolve));
}

async function waitUntil(predicate, label) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (predicate()) {
      return;
    }
    await flushAsyncWork();
  }
  throw new Error(`timed out waiting for ${label}`);
}

function writeSave(environment, relative, value) {
  writeFile(path.join(environment.saveRoot, relative), value);
}

function readSave(environment, relative) {
  return fs.readFileSync(path.join(environment.saveRoot, relative), 'utf8');
}

function collectFiles(root) {
  const result = Object.create(null);
  function visit(directory, relative) {
    for (const name of fs.readdirSync(directory).sort()) {
      const absolute = path.join(directory, name);
      const child = relative ? `${relative}/${name}` : name;
      const info = fs.lstatSync(absolute);
      if (info.isDirectory()) {
        result[`${child}/`] = '<dir>';
        visit(absolute, child);
      } else if (info.isFile()) {
        result[child] = fs.readFileSync(absolute, 'utf8');
      } else {
        result[child] = '<special>';
      }
    }
  }
  visit(root, '');
  return result;
}

const tests = [];

function test(name, callback) {
  tests.push({ name, callback });
}

test('fresh empty cloud uploads a new local character', async () => {
  const environment = makeEnvironment('fresh-upload');
  configureToken(environment);
  writeSave(environment, 'Hero/Hero.d2s', 'fresh-character');

  const manager = new environment.exports.CloudSaveManager(
    environment.filesRoot, environment.context);
  const result = await manager.initializeForStartup();

  assert.equal(result.kind, 'ready', result.message);
  assert.equal(environment.server.current.file_count, 1);
  assert.equal(environment.server.current.total_bytes, Buffer.byteLength('fresh-character'));
  const snapshot = environment.server.versions.get(environment.server.current.revision);
  assert.equal(snapshot.files[0].path, 'Hero/Hero.d2s');
  assert.equal(Buffer.from(snapshot.files[0].data, 'base64').toString(), 'fresh-character');
  assert.equal(environment.native.installs, 0);
});

test('one-sided local deletion uploads an empty committed snapshot', async () => {
  const environment = makeEnvironment('local-delete');
  configureToken(environment);
  writeSave(environment, 'Hero/Hero.d2s', 'delete-me');

  const manager = new environment.exports.CloudSaveManager(
    environment.filesRoot, environment.context);
  assert.equal((await manager.initializeForStartup()).kind, 'ready');
  const firstRevision = environment.server.current.revision;
  fs.unlinkSync(path.join(environment.saveRoot, 'Hero/Hero.d2s'));

  const result = await manager.initializeForStartup();

  assert.equal(result.kind, 'ready', result.message);
  assert.notEqual(environment.server.current.revision, firstRevision);
  assert.equal(environment.server.current.file_count, 0);
  assert.equal(environment.server.current.total_bytes, 0);
  assert.equal(environment.server.current.snapshot_sha256, emptySha256);
  assert.equal(fs.existsSync(path.join(environment.saveRoot, 'Hero/Hero.d2s')), false);
  assert.equal(environment.native.installs, 0);
});

test('a one-sided cloud change downloads and retains a native backup', async () => {
  const environment = makeEnvironment('cloud-download');
  configureToken(environment);
  writeSave(environment, 'Hero/Hero.d2s', 'original');
  const manager = new environment.exports.CloudSaveManager(
    environment.filesRoot, environment.context);
  assert.equal((await manager.initializeForStartup()).kind, 'ready');

  environment.server.setSnapshot({ 'Hero/Hero.d2s': 'from-cloud' }, { device: 'Other' });
  const result = await manager.initializeForStartup();

  assert.equal(result.kind, 'ready', result.message);
  assert.equal(readSave(environment, 'Hero/Hero.d2s'), 'from-cloud');
  assert.equal(environment.native.installs, 1);
  assert.equal(environment.native.backups.length, 1);
  assert.equal(fs.readFileSync(path.join(environment.native.backups[0], 'Hero/Hero.d2s'), 'utf8'),
    'original');
});

test('independent local and cloud edits force an explicit conflict choice', async () => {
  const environment = makeEnvironment('both-changed');
  configureToken(environment);
  writeSave(environment, 'Hero/Hero.d2s', 'baseline');
  const manager = new environment.exports.CloudSaveManager(
    environment.filesRoot, environment.context);
  const initialResult = await manager.initializeForStartup();
  assert.equal(initialResult.kind, 'ready', initialResult.message);
  const putsBeforeConflict = environment.server.requests.put;

  writeSave(environment, 'Hero/Hero.d2s', 'local-new');
  environment.server.setSnapshot({ 'Hero/Hero.d2s': 'cloud-new' }, { device: 'Other' });
  const result = await manager.initializeForStartup();

  assert.equal(result.kind, 'conflict');
  assert.ok(result.conflict);
  assert.equal(readSave(environment, 'Hero/Hero.d2s'), 'local-new');
  assert.equal(environment.server.requests.put, putsBeforeConflict);
  assert.equal(environment.native.installs, 0);
});

test('a remote change after the conflict prompt is not silently overwritten', async () => {
  const environment = makeEnvironment('choice-race');
  configureToken(environment);
  writeSave(environment, 'Hero/Hero.d2s', 'baseline');
  const manager = new environment.exports.CloudSaveManager(
    environment.filesRoot, environment.context);
  const initialResult = await manager.initializeForStartup();
  assert.equal(initialResult.kind, 'ready', initialResult.message);
  writeSave(environment, 'Hero/Hero.d2s', 'local-choice');
  environment.server.setSnapshot({ 'Hero/Hero.d2s': 'cloud-choice-v1' }, { device: 'Other' });
  assert.equal((await manager.initializeForStartup()).kind, 'conflict');

  environment.server.setSnapshot({ 'Hero/Hero.d2s': 'cloud-choice-v2' }, { device: 'Other' });
  const result = await manager.resolveConflict('cloud');

  assert.equal(result.kind, 'conflict');
  assert.equal(readSave(environment, 'Hero/Hero.d2s'), 'local-choice');
  assert.equal(environment.native.installs, 0);
  assert.equal(environment.server.requests.put, 1);
});

test('offline startup blocks background uploads', async () => {
  const environment = makeEnvironment('offline');
  configureToken(environment);
  writeSave(environment, 'Hero/Hero.d2s', 'baseline');
  const manager = new environment.exports.CloudSaveManager(
    environment.filesRoot, environment.context);
  const initialResult = await manager.initializeForStartup();
  assert.equal(initialResult.kind, 'ready', initialResult.message);

  manager.startOffline();
  const putsBefore = environment.server.requests.put;
  const statesBefore = environment.server.requests.state;
  writeSave(environment, 'Hero/Hero.d2s', 'offline-change');
  await manager.uploadCommittedSnapshot();

  assert.equal(environment.server.requests.put, putsBefore);
  assert.equal(environment.server.requests.state, statesBefore);
  const remote = environment.server.versions.get(environment.server.current.revision);
  assert.equal(Buffer.from(remote.files[0].data, 'base64').toString(), 'baseline');
});

test('nested exclusions and Settings survive repeated restores exactly once', async () => {
  const environment = makeEnvironment('excluded-preserve');
  configureToken(environment);
  writeSave(environment, '_cloudinbox/mine.pack', 'private-inbox');
  writeSave(environment, '_root-private.pack', 'root-private');
  writeSave(environment, 'Hero/.local', 'hero-local');
  writeSave(environment, 'Settings/settings.json', '{"hello":"local"}');
  environment.server.setSnapshot({ 'Hero/Hero.d2s': 'cloud-v1' });

  const manager = new environment.exports.CloudSaveManager(
    environment.filesRoot, environment.context);
  const initialResult = await manager.initializeForStartup();
  assert.equal(initialResult.kind, 'ready', initialResult.message);
  let files = collectFiles(environment.saveRoot);
  assert.equal(files['_cloudinbox/mine.pack'], 'private-inbox');
  assert.equal(files['_root-private.pack'], 'root-private');
  assert.equal(files['Hero/.local'], 'hero-local');
  assert.equal(files['Settings/settings.json'], '{"hello":"local"}');
  assert.equal(files['Hero/Hero.d2s'], 'cloud-v1');

  environment.server.setSnapshot({ 'Hero/Hero.d2s': 'cloud-v2' }, { device: 'Other' });
  assert.equal((await manager.initializeForStartup()).kind, 'ready');
  files = collectFiles(environment.saveRoot);
  assert.equal(files['_cloudinbox/mine.pack'], 'private-inbox');
  assert.equal(files['_root-private.pack'], 'root-private');
  assert.equal(files['Hero/.local'], 'hero-local');
  assert.equal(files['Settings/settings.json'], '{"hello":"local"}');
  assert.equal(files['Hero/Hero.d2s'], 'cloud-v2');
  assert.equal(environment.native.installs, 2);
  assert.equal(Object.values(files).filter((value) => value === 'private-inbox').length, 1);
});

test('missing token returns setup without starting network traffic', async () => {
  const environment = makeEnvironment('setup');
  writeSave(environment, 'Hero/Hero.d2s', 'untouched');
  const manager = new environment.exports.CloudSaveManager(
    environment.filesRoot, environment.context);

  const result = await manager.initializeForStartup();

  assert.equal(result.kind, 'setup');
  assert.equal(readSave(environment, 'Hero/Hero.d2s'), 'untouched');
  assert.deepEqual(environment.server.requests,
    { state: 0, snapshot: 0, put: 0, unauthorized: 0, invalidPut: 0 });
});

test('download checksum failure leaves the local tree unchanged', async () => {
  const environment = makeEnvironment('checksum-failure');
  configureToken(environment);
  writeSave(environment, '_local-only.pack', 'keep-me');
  writeSave(environment, 'Settings/settings.json', '{"keep":true}');
  const before = collectFiles(environment.saveRoot);
  environment.server.setSnapshot({ 'Hero/Hero.d2s': 'cloud-corrupt' });
  environment.server.snapshotTransform = (snapshot) => {
    snapshot.files[0].sha256 = '0'.repeat(64);
    return snapshot;
  };
  const manager = new environment.exports.CloudSaveManager(
    environment.filesRoot, environment.context);

  const result = await manager.initializeForStartup();

  assert.equal(result.kind, 'network');
  assert.match(result.message, /哈希不匹配/);
  assert.deepEqual(collectFiles(environment.saveRoot), before);
  assert.equal(environment.native.installs, 0);
  assert.equal(environment.server.requests.put, 0);
});

test('a background 409 pauses all further background uploads', async () => {
  const environment = makeEnvironment('background-409');
  configureToken(environment);
  writeSave(environment, 'Hero/Hero.d2s', 'baseline');
  const manager = new environment.exports.CloudSaveManager(
    environment.filesRoot, environment.context);
  assert.equal((await manager.initializeForStartup()).kind, 'ready');
  writeSave(environment, 'Hero/Hero.d2s', 'local-after-conflict');
  environment.server.conflictOnPut = () => {
    environment.server.setSnapshot({ 'Hero/Hero.d2s': 'other-device-winner' },
      { device: 'Other' });
  };

  await manager.uploadCommittedSnapshot();
  assert.equal(environment.server.requests.put, 2);
  assert.equal(environment.native.installs, 0);
  const putCountAfterConflict = environment.server.requests.put;
  const stateCountAfterConflict = environment.server.requests.state;

  await manager.uploadCommittedSnapshot();
  assert.equal(environment.server.requests.put, putCountAfterConflict);
  assert.equal(environment.server.requests.state, stateCountAfterConflict);
  assert.equal(readSave(environment, 'Hero/Hero.d2s'), 'local-after-conflict');
  const remote = environment.server.versions.get(environment.server.current.revision);
  assert.equal(Buffer.from(remote.files[0].data, 'base64').toString(), 'other-device-winner');
});

test('connection timeout records diagnostics without changing real saves or exposing credentials', async () => {
  const environment = makeEnvironment('connection-timeout');
  configureToken(environment);
  writeSave(environment, 'Hero/Hero.d2s', 'unchanged-on-timeout');
  environment.server.dispatch = () => {
    const error = new Error('Operation timeout');
    error.code = 2300028;
    throw error;
  };
  const manager = new environment.exports.CloudSaveManager(environment.filesRoot, environment.context);
  const result = await manager.initializeForStartup();
  assert.equal(result.kind, 'network');
  assert.match(result.message, /2300028/);
  assert.equal(readSave(environment, 'Hero/Hero.d2s'), 'unchanged-on-timeout');
  assert.equal(environment.native.installs, 0);
  const raw = fs.readFileSync(path.join(environment.filesRoot, 'cloud-save/network-last.json'), 'utf8');
  const diagnostic = JSON.parse(raw);
  assert.equal(diagnostic.errorCode, 2300028);
  assert.equal(diagnostic.status, 0);
  assert.equal(diagnostic.path, '/state');
  assert.doesNotMatch(raw, /Authorization|Bearer|token|unchanged-on-timeout/);
});

test('only the default endpoint gets the origin DNS route while HTTPS, host and auth stay unchanged', async () => {
  const environment = makeEnvironment('origin-default-route');
  configureEndpoint(environment, defaultEndpoint);
  const manager = new environment.exports.CloudSaveManager(
    environment.filesRoot, environment.context);

  const result = await manager.initializeForStartup();

  assert.equal(result.kind, 'ready', result.message);
  const dnsAdds = traceOf(environment, 'dns-add');
  assert.equal(dnsAdds.length, 1);
  assert.equal(dnsAdds[0].host, defaultOriginHost);
  assert.deepEqual(dnsAdds[0].addresses, [defaultOriginIpv4]);
  const requests = requestTrace(environment, 'GET');
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, `${defaultEndpoint}/state`);
  assert.deepEqual(requests[0].active, { [defaultOriginHost]: [defaultOriginIpv4] });
  assert.deepEqual(requests[0].headers, {
    Authorization: `Bearer ${token}`,
    'Content-Type': 'application/json',
    Accept: 'application/json',
    'User-Agent': 'D2RMobile-Cloud/1',
  });
  assert.deepEqual(environment.connection.activeRules(), {});

  const custom = makeEnvironment('origin-custom-route');
  const sameOriginCustomEndpoint = 'https://mythstribe.com/d2r-cloud/v2';
  configureEndpoint(custom, sameOriginCustomEndpoint);
  const customManager = new custom.exports.CloudSaveManager(custom.filesRoot, custom.context);
  const customResult = await customManager.initializeForStartup();
  assert.equal(customResult.kind, 'ready', customResult.message);
  assert.equal(traceOf(custom, 'dns-add').length, 0);
  assert.equal(traceOf(custom, 'dns-remove').length, 0);
  const customRequests = requestTrace(custom, 'GET');
  assert.equal(customRequests.length, 1);
  assert.equal(customRequests[0].url, `${sameOriginCustomEndpoint}/state`);
  assert.deepEqual(customRequests[0].active, {});
});

test('a GET transport timeout removes the origin route before one public retry', async () => {
  const environment = makeEnvironment('origin-get-fallback');
  configureEndpoint(environment, defaultEndpoint);
  const originalDispatch = environment.server.dispatch.bind(environment.server);
  let attempts = 0;
  environment.server.dispatch = async (method, rawUrl, options) => {
    attempts += 1;
    if (attempts === 1) {
      throw makeNetworkError(2300028, 'Operation timeout');
    }
    return originalDispatch(method, rawUrl, options);
  };
  const manager = new environment.exports.CloudSaveManager(
    environment.filesRoot, environment.context);
  let releaseRemoval;
  const removalGate = new Promise((resolve) => {
    releaseRemoval = resolve;
  });
  environment.connection.beforeRemove = async (host) => {
    assert.equal(host, defaultOriginHost);
    await removalGate;
  };

  const startupPromise = manager.initializeForStartup();
  await waitUntil(() => traceOf(environment, 'dns-remove-start').length === 1,
    'origin DNS removal before GET fallback');
  assert.equal(requestTrace(environment, 'GET').length, 1);
  releaseRemoval();
  const result = await startupPromise;

  assert.equal(result.kind, 'ready', result.message);
  const requests = requestTrace(environment, 'GET');
  assert.equal(requests.length, 2);
  assert.deepEqual(requests.map((entry) => entry.url), [
    `${defaultEndpoint}/state`, `${defaultEndpoint}/state`,
  ]);
  assert.deepEqual(requests[0].active, { [defaultOriginHost]: [defaultOriginIpv4] });
  assert.deepEqual(requests[1].active, {});
  assert.deepEqual(requests[1].headers, requests[0].headers);
  assert.equal(environment.server.requests.state, 1);
  const dns = environment.trace.filter((entry) => entry.type === 'dns-add' || entry.type === 'dns-remove');
  assert.deepEqual(dns.map((entry) => entry.type), ['dns-add', 'dns-remove']);
  assert.ok(dns[0].sequence < dns[1].sequence);
  assert.ok(dns[1].sequence < requests[1].sequence);
  assert.deepEqual(environment.connection.activeRules(), {});
});

test('a failed public GET retry ends after the second transport attempt', async () => {
  const environment = makeEnvironment('origin-public-retry-failure');
  configureEndpoint(environment, defaultEndpoint);
  let attempts = 0;
  environment.server.dispatch = () => {
    attempts += 1;
    throw makeNetworkError(attempts === 1 ? 2300028 : 2300016,
      attempts === 1 ? 'origin timeout' : 'public connection failure');
  };
  const manager = new environment.exports.CloudSaveManager(
    environment.filesRoot, environment.context);

  const result = await manager.initializeForStartup();

  assert.equal(result.kind, 'network');
  assert.match(result.message, /2300016/);
  const requests = requestTrace(environment, 'GET');
  assert.equal(requests.length, 2);
  assert.deepEqual(requests.map((entry) => entry.active), [
    { [defaultOriginHost]: [defaultOriginIpv4] },
    {},
  ]);
  assert.equal(traceOf(environment, 'dns-add').length, 1);
  assert.equal(traceOf(environment, 'dns-remove').length, 1);
  assert.deepEqual(environment.connection.activeRules(), {});
});

test('a PUT whose committed response is lost is never automatically resent', async () => {
  const environment = makeEnvironment('origin-put-lost-response');
  configureEndpoint(environment, defaultEndpoint);
  writeSave(environment, 'Hero/Hero.d2s', 'committed-once');
  const originalDispatch = environment.server.dispatch.bind(environment.server);
  environment.server.dispatch = (method, rawUrl, options) => {
    const response = originalDispatch(method, rawUrl, options);
    if (method === 'PUT') {
      throw makeNetworkError(2300028, 'Operation timeout after commit');
    }
    return response;
  };
  const manager = new environment.exports.CloudSaveManager(
    environment.filesRoot, environment.context);

  const result = await manager.initializeForStartup();

  assert.equal(result.kind, 'network');
  assert.match(result.message, /2300028/);
  const puts = requestTrace(environment, 'PUT');
  assert.equal(puts.length, 1);
  assert.equal(environment.server.requests.put, 1);
  assert.deepEqual(puts[0].active, { [defaultOriginHost]: [defaultOriginIpv4] });
  assert.equal(readSave(environment, 'Hero/Hero.d2s'), 'committed-once');
  const remote = environment.server.versions.get(environment.server.current.revision);
  assert.equal(Buffer.from(remote.files[0].data, 'base64').toString(), 'committed-once');
  assert.deepEqual(environment.connection.activeRules(), {});
});

test('an origin HTTP 401 response is returned once without a public transport retry', async () => {
  const environment = makeEnvironment('origin-http-401');
  configureEndpoint(environment, defaultEndpoint);
  environment.server.dispatch = () => ({
    status: 401,
    body: '{"error":"unauthorized"}',
  });
  const manager = new environment.exports.CloudSaveManager(
    environment.filesRoot, environment.context);

  const result = await manager.initializeForStartup();

  assert.equal(result.kind, 'network');
  assert.match(result.message, /401/);
  assert.equal(requestTrace(environment, 'GET').length, 1);
  assert.equal(traceOf(environment, 'dns-add').length, 1);
  assert.equal(traceOf(environment, 'dns-remove').length, 1);
  assert.deepEqual(environment.connection.activeRules(), {});
});

test('an origin HTTP 409 PUT conflict is not retried and still reports the conflict', async () => {
  const environment = makeEnvironment('origin-http-409');
  configureEndpoint(environment, defaultEndpoint);
  writeSave(environment, 'Hero/Hero.d2s', 'local-winner');
  environment.server.conflictOnPut = () => {
    environment.server.setSnapshot({ 'Hero/Hero.d2s': 'remote-winner' }, { device: 'Other' });
  };
  const manager = new environment.exports.CloudSaveManager(
    environment.filesRoot, environment.context);

  const result = await manager.initializeForStartup();

  assert.equal(result.kind, 'conflict');
  assert.equal(result.conflict.remoteRevision, environment.server.current.revision);
  assert.equal(requestTrace(environment, 'PUT').length, 1);
  assert.equal(environment.server.requests.put, 1);
  assert.equal(traceOf(environment, 'dns-add').length, 2);
  assert.equal(traceOf(environment, 'dns-remove').length, 2);
  assert.ok(requestTrace(environment).every((entry) =>
    JSON.stringify(entry.active) === JSON.stringify({ [defaultOriginHost]: [defaultOriginIpv4] })));
  assert.deepEqual(environment.connection.activeRules(), {});
});

test('a certificate error on the origin route is not retried through public DNS', async () => {
  const environment = makeEnvironment('origin-certificate-error');
  configureEndpoint(environment, defaultEndpoint);
  let attempts = 0;
  environment.server.dispatch = () => {
    attempts += 1;
    if (attempts === 1) {
      throw makeNetworkError(2300060, 'certificate verify failed');
    }
    return { status: 200, body: JSON.stringify(blankState()) };
  };
  const manager = new environment.exports.CloudSaveManager(
    environment.filesRoot, environment.context);

  const result = await manager.initializeForStartup();

  assert.equal(result.kind, 'network');
  assert.match(result.message, /2300060/);
  assert.equal(requestTrace(environment, 'GET').length, 1);
  assert.equal(traceOf(environment, 'dns-add').length, 1);
  assert.equal(traceOf(environment, 'dns-remove').length, 1);
  assert.deepEqual(environment.connection.activeRules(), {});

  const recovered = await manager.getState();
  assert.equal(recovered.revision, '');
  assert.equal(requestTrace(environment, 'GET').length, 2);
  assert.equal(traceOf(environment, 'dns-add').length, 2);
  assert.equal(traceOf(environment, 'dns-remove').length, 2);
  assert.deepEqual(environment.connection.activeRules(), {});
});

for (const useSecondManager of [false, true]) {
test(`concurrent reads across ${useSecondManager ? 'two managers' : 'one manager'} serialize DNS ownership and cleanup`, async () => {
  const environment = makeEnvironment(`origin-serialized-reads-${useSecondManager}`);
  configureEndpoint(environment, defaultEndpoint);
  let stateCalls = 0;
  let releaseFirstRequest;
  const firstResponse = new Promise((resolve) => {
    releaseFirstRequest = resolve;
  });
  environment.server.dispatch = async (method, rawUrl) => {
    const url = new URL(rawUrl);
    assert.equal(method, 'GET');
    assert.match(url.pathname, /\/state$/);
    stateCalls += 1;
    if (stateCalls === 1) {
      return await firstResponse;
    }
    return { status: 200, body: JSON.stringify(blankState()) };
  };
  const manager = new environment.exports.CloudSaveManager(
    environment.filesRoot, environment.context);

  const startupPromise = manager.initializeForStartup();
  await waitUntil(() => stateCalls === 1, 'first serialized state request');
  const secondManager = useSecondManager ? new environment.exports.CloudSaveManager(
    environment.filesRoot, environment.context) : manager;
  const secondRead = useSecondManager ? secondManager.initializeForStartup() : manager.getState();
  await flushAsyncWork();
  assert.equal(requestTrace(environment, 'GET').length, 1);
  assert.equal(traceOf(environment, 'dns-add').length, 1);

  releaseFirstRequest({ status: 200, body: JSON.stringify(blankState()) });
  const [startupResult, secondState] = await Promise.all([startupPromise, secondRead]);

  assert.equal(startupResult.kind, 'ready', startupResult.message);
  if (useSecondManager) {
    assert.equal(secondState.kind, 'ready', secondState.message);
  } else {
    assert.equal(secondState.revision, '');
  }
  assert.equal(stateCalls, 2);
  const sequence = environment.trace
    .filter((entry) => ['dns-add', 'dns-remove', 'request'].includes(entry.type))
    .map((entry) => entry.type);
  assert.deepEqual(sequence, [
    'dns-add', 'request', 'dns-remove',
    'dns-add', 'request', 'dns-remove',
  ]);
  assert.deepEqual(environment.connection.activeRules(), {});
});
}

async function main() {
  fs.mkdirSync(testRoot, { recursive: true });
  let failed = 0;
  for (const entry of tests) {
    try {
      await entry.callback();
      process.stdout.write(`PASS ${entry.name}\n`);
    } catch (error) {
      failed += 1;
      process.stderr.write(`FAIL ${entry.name}\n${error.stack || error}\n`);
    }
  }
  process.stdout.write(`\n${tests.length - failed}/${tests.length} tests passed\n`);
  if (failed !== 0) {
    process.exitCode = 1;
  }
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
