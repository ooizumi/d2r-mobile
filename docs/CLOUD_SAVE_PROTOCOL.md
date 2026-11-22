# D2R Mobile cloud save protocol v1

Independent service, never Joplin storage. The default endpoint is
`https://mythstribe.com/d2r-cloud/v1`. All API calls require `Authorization:
Bearer <private token>`. The token selects an isolated account; tokens must not
appear in source, logs, URLs or public exports. HTTPS certificate verification
is mandatory. Clients do not follow redirects with credentials.

## Snapshot

The synchronized tree is `files/save` (not `saves`, `dm` or ROM data). Include
regular files recursively except any path component starting with `.` or `_`,
and the top-level `Settings` directory. Excluded local entries survive restores.
Reject symlinks and special files. Maximum 2048 files, 8 MiB per file, 24 MiB
decoded total, 48 MiB HTTP body. Paths are relative slash-separated strings,
maximum 512 UTF-16 code units, at most 8 components. Reject empty, `.` / `..`,
backslash, colon, control characters and absolute paths. Reject duplicate paths
and file/directory prefix collisions. Unicode names are supported.

Each file: `{ "path": "Hero/Hero.d2s", "size": 123,
"sha256": "lowercase hex", "data": "standard padded base64" }`.
Sort by path in UTF-16 code unit order (Java compareTo / JS default sort).
Snapshot SHA-256 is over UTF-8 encoding of the concatenation, for each file:
`path + "\0" + decimal_size + "\0" + sha256 + "\n"`.
The empty snapshot digest is SHA-256 of zero bytes. Timestamps never decide
conflicts. Persist and verify every file hash and the snapshot hash.

## HTTP API

* `GET /state`: `{revision, snapshot_sha256, updated_at, device, file_count,
  total_bytes}`. An unused account has revision/hash/updated_at/device `""`,
  counts zero. Revisions are opaque strings.
* `GET /snapshot?revision=<revision>`: state fields plus `files`. Reads an
  immutable version, including previous versions; 404 if absent.
* `PUT /snapshot`: `{base_revision, device, snapshot_sha256, files}`. `device`
  is a short non-sensitive label (Android/HarmonyOS). Empty base means no remote
  version. Under one account lock, validate and compare base with current; 409
  returns `{error:"conflict",state:<current state>}` on mismatch. Identical
  content may return the current state without creating another version.
  Successful response is state. Publish immutable checked snapshot before
  atomically replacing current pointer; fsync files and parent directories.
* `GET /history`: `{versions:[<state>,...]}` newest first. Old versions remain
  available; quota exhaustion refuses writes (507) rather than deleting data.
* `GET /healthz` (outside `/v1`, no auth): minimal health status, no paths/tokens.

Auth failures return 401. Invalid/oversize requests return 400/413. Errors
contain no credentials or save bytes. Server logs only method/status.

## Client rules

Private config `{endpoint,token}` is loaded from `files/cloud-save/config.json`
or optional bundled `cloud-save.json` (Android assets / Harmony rawfile), saved
privately. Missing config offers setup and offline start. Never commit a real
config into source. Changing endpoint/token resets baseline. Baseline is an
atomic private JSON file `{revision,snapshot_sha256}` scoped to config.

Before starting native game, compare a verified local snapshot, remote state,
and last synchronized baseline. Equal hashes acknowledge sync. One-sided
changes propagate in their direction. First sync with files on both sides and
different hashes, or both sides changed since baseline, requires explicit
`云存档覆盖本地` or `本地存档覆盖云端`; show counts/time and explain backups.
A fresh empty device may download cloud; a new empty cloud may receive local.
A previously populated cloud disappearing is a conflict. No timestamp winner.
Offline/error offers retry and explicit offline start without changing baseline.
Conflict may also be deferred with offline start; deferral must not silently
upload the local version later. Recheck remote revision after user choice; a
409 must return to conflict choice, never force overwrite.

The main agent provides native APIs:
Android `CloudSave.nativeSnapshot(String filesRoot, String destination)` and
`CloudSave.nativeInstall(String filesRoot, String stagedDirectory)`;
Harmony `dm.cloudSnapshot(filesRoot, destination)` and
`dm.cloudInstall(filesRoot, stagedDirectory)`. Return 0 or negative errno.
Mac uses `dm_mac_cloud_boot(root, configPath)` before mounting saves, with the
same native `dm_fs_cloud_snapshot` / `dm_fs_cloud_install` transactions. Its
serial worker uploads every 30 seconds, on app deactivation and during graceful
quit. HTTP sessions explicitly disable the separate iCloud coordinator, including
when the player chooses offline.

Snapshot copies committed `filesRoot/save` under the game's save lock into a
nonexistent destination (empty if no save). Install only before any save mount:
atomically exchanges staged full tree with save; the previous local tree stays
at stagedDirectory as a recoverable backup. Staged tree must preserve all local
excluded entries. Verify written files before install. No startup until install
and required state persistence complete; failed state persistence is recoverable
by comparing content next launch. Never prune restore backups automatically.

While game runs, at most one worker uploads a native committed snapshot every
30 seconds (and on background when possible). Never download into a running
game. Only upload with an acknowledged baseline and CAS; unknown remote changes
or conflict pauses uploads until next startup choice. Keep pending local saves
on errors and retry. Native snapshot lock covers only copying, never networking.

Builds/tests and private config live under `work/cloud-save`, not active device
builds. Do not touch the Harmony tablet/phone, graphics core or other outputs.
