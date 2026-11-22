#!/usr/bin/env python3
"""Independent D2R Mobile cloud save service.

The HTTP server intentionally binds to loopback. Put it behind the reverse
proxy template in ``deploy/nginx-location.conf``; TLS and the public URL live
at that proxy, while account credentials stay in this service.

This module uses only the Python 3.9 standard library.
"""

import argparse
import base64
import binascii
import dataclasses
import datetime
import errno
import fcntl
import hashlib
import hmac
import ipaddress
import json
import os
import pathlib
import re
import shutil
import socket
import stat
import sys
import threading
import time
import unicodedata
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_QUOTA_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_SOCKET_TIMEOUT_SECONDS = 15.0
DEFAULT_MAX_WORKERS = 8

MAX_FILES = 2048
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 24 * 1024 * 1024
MAX_HTTP_BODY_BYTES = 48 * 1024 * 1024
MAX_STORED_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_PATH_UTF16_UNITS = 512
MAX_PATH_COMPONENTS = 8
MAX_DEVICE_UTF16_UNITS = 64
MAX_REVISION_LENGTH = 128
MAX_AUTH_FILE_BYTES = 1024 * 1024
MAX_CURRENT_INDEX_BYTES = 4 * 1024 * 1024
MAX_HISTORY_VERSIONS = 8192

HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REVISION_RE = re.compile(r"^r1-[0-9a-f]{32}$")


class ApiError(Exception):
    def __init__(self, status: int, error: str, extra: Optional[Mapping[str, object]] = None):
        super().__init__(error)
        self.status = status
        self.error = error
        self.extra = dict(extra or {})


class ConfigError(Exception):
    pass


class StoredDataError(Exception):
    pass


class ConflictError(Exception):
    def __init__(self, state: Mapping[str, object]):
        super().__init__("conflict")
        self.state = dict(state)


class QuotaExceededError(Exception):
    pass


class HistoryLimitError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Account:
    account_id: str
    token_sha256: str
    quota_bytes: int
    key: str


@dataclasses.dataclass(frozen=True)
class AuthConfig:
    accounts: Tuple[Account, ...]


@dataclasses.dataclass(frozen=True)
class FileEntry:
    path: str
    size: int
    sha256: str
    data: bytes
    encoded: str


@dataclasses.dataclass(frozen=True)
class PutRequest:
    base_revision: str
    device: str
    snapshot_sha256: str
    files: Tuple[FileEntry, ...]
    total_bytes: int


EMPTY_STATE = {
    "revision": "",
    "snapshot_sha256": "",
    "updated_at": "",
    "device": "",
    "file_count": 0,
    "total_bytes": 0,
}

STATE_FIELDS = {
    "revision",
    "snapshot_sha256",
    "updated_at",
    "device",
    "file_count",
    "total_bytes",
}
PUT_FIELDS = {"base_revision", "device", "snapshot_sha256", "files"}
FILE_FIELDS = {"path", "size", "sha256", "data"}


def _json_constant(value: str) -> None:
    raise ValueError("non-JSON constant: {}".format(value))


def _reject_duplicate_object_keys(pairs: Sequence[Tuple[str, object]]) -> Dict[str, object]:
    result: Dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def parse_json_bytes(raw: bytes) -> object:
    try:
        text = raw.decode("utf-8")
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ApiError(400, "invalid_request") from exc


def _require_exact_keys(value: object, fields: set, label: str) -> Mapping[str, object]:
    if type(value) is not dict:
        raise ApiError(400, "invalid_request")
    mapping = value  # type: Mapping[str, object]
    if set(mapping.keys()) != fields:
        raise ApiError(400, "invalid_request")
    return mapping


def _require_str(value: object, field: str, max_utf16: int, allow_empty: bool = False) -> str:
    if type(value) is not str:
        raise ApiError(400, "invalid_request")
    text = value  # type: str
    if not allow_empty and not text:
        raise ApiError(400, "invalid_request")
    try:
        units = len(text.encode("utf-16-le")) // 2
    except UnicodeEncodeError as exc:
        raise ApiError(400, "invalid_request") from exc
    if units > max_utf16 or any(unicodedata.category(ch) == "Cc" for ch in text):
        raise ApiError(400, "invalid_request")
    return text


def _require_int(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise ApiError(400, "invalid_request")
    return value


def _utf16_key(value: str) -> bytes:
    try:
        return value.encode("utf-16-be")
    except UnicodeEncodeError as exc:
        raise ApiError(400, "invalid_request") from exc


def _validate_path(value: object) -> Tuple[str, Tuple[str, ...]]:
    path = _require_str(value, "path", MAX_PATH_UTF16_UNITS)
    if path.startswith("/") or "\\" in path or ":" in path:
        raise ApiError(400, "invalid_request")
    if any(unicodedata.category(ch) == "Cc" for ch in path):
        raise ApiError(400, "invalid_request")
    parts = tuple(path.split("/"))
    if (
        not parts
        or len(parts) > MAX_PATH_COMPONENTS
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise ApiError(400, "invalid_request")
    return path, parts


def _validate_sha(value: object) -> str:
    if type(value) is not str or HEX_SHA256_RE.fullmatch(value) is None:
        raise ApiError(400, "invalid_request")
    return value


def validate_files(value: object) -> Tuple[Tuple[FileEntry, ...], int, str]:
    if type(value) is not list:
        raise ApiError(400, "invalid_request")
    files_raw = value  # type: List[object]
    if len(files_raw) > MAX_FILES:
        raise ApiError(400, "invalid_request")

    entries: List[FileEntry] = []
    seen_paths = set()
    path_parts: Dict[str, Tuple[str, ...]] = {}
    total_bytes = 0

    for raw_file in files_raw:
        file_obj = _require_exact_keys(raw_file, FILE_FIELDS, "file")
        path, parts = _validate_path(file_obj["path"])
        if path in seen_paths:
            raise ApiError(400, "invalid_request")
        seen_paths.add(path)
        path_parts[path] = parts

        size = _require_int(file_obj["size"], 0, MAX_FILE_BYTES)
        digest = _validate_sha(file_obj["sha256"])
        encoded = file_obj["data"]
        if type(encoded) is not str:
            raise ApiError(400, "invalid_request")
        try:
            decoded = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error, ValueError) as exc:
            raise ApiError(400, "invalid_request") from exc
        if base64.b64encode(decoded).decode("ascii") != encoded:
            raise ApiError(400, "invalid_request")
        if len(decoded) != size or hashlib.sha256(decoded).hexdigest() != digest:
            raise ApiError(400, "invalid_request")

        total_bytes += size
        if total_bytes > MAX_SNAPSHOT_BYTES:
            raise ApiError(400, "invalid_request")
        entries.append(FileEntry(path, size, digest, decoded, encoded))

    for path, parts in path_parts.items():
        for index in range(1, len(parts)):
            ancestor = "/".join(parts[:index])
            if ancestor in path_parts:
                raise ApiError(400, "invalid_request")

    entries.sort(key=lambda entry: _utf16_key(entry.path))
    hasher = hashlib.sha256()
    for entry in entries:
        hasher.update(entry.path.encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(str(entry.size).encode("ascii"))
        hasher.update(b"\x00")
        hasher.update(entry.sha256.encode("ascii"))
        hasher.update(b"\n")
    return tuple(entries), total_bytes, hasher.hexdigest()


def parse_put_request(raw: bytes) -> PutRequest:
    value = parse_json_bytes(raw)
    request_obj = _require_exact_keys(value, PUT_FIELDS, "snapshot request")
    base_revision = _require_str(
        request_obj["base_revision"], "base_revision", MAX_REVISION_LENGTH, allow_empty=True
    )
    device = _require_str(request_obj["device"], "device", MAX_DEVICE_UTF16_UNITS)
    if device != device.strip():
        raise ApiError(400, "invalid_request")
    requested_digest = _validate_sha(request_obj["snapshot_sha256"])
    files, total_bytes, actual_digest = validate_files(request_obj["files"])
    if requested_digest != actual_digest:
        raise ApiError(400, "invalid_request")
    return PutRequest(base_revision, device, requested_digest, files, total_bytes)


def _validate_state(value: object) -> Dict[str, object]:
    state_obj = _require_exact_keys(value, STATE_FIELDS, "state")
    revision = state_obj["revision"]
    snapshot_sha = state_obj["snapshot_sha256"]
    updated_at = state_obj["updated_at"]
    device = state_obj["device"]
    file_count = _require_int(state_obj["file_count"], 0, MAX_FILES)
    total_bytes = _require_int(state_obj["total_bytes"], 0, MAX_SNAPSHOT_BYTES)

    if type(revision) is not str or (revision and REVISION_RE.fullmatch(revision) is None):
        raise ApiError(400, "invalid_request")
    if snapshot_sha == "":
        if revision or updated_at != "" or device != "" or file_count != 0 or total_bytes != 0:
            raise ApiError(400, "invalid_request")
    else:
        _validate_sha(snapshot_sha)
        if not revision:
            raise ApiError(400, "invalid_request")
        _require_str(updated_at, "updated_at", 64)
        _require_str(device, "device", MAX_DEVICE_UTF16_UNITS)
    return {
        "revision": revision,
        "snapshot_sha256": snapshot_sha,
        "updated_at": updated_at,
        "device": device,
        "file_count": file_count,
        "total_bytes": total_bytes,
    }


def _empty_state() -> Dict[str, object]:
    return dict(EMPTY_STATE)


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _serialized_json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _ensure_history_index_capacity(revision: str, versions: Sequence[Mapping[str, object]]) -> None:
    if len(versions) > MAX_HISTORY_VERSIONS:
        raise HistoryLimitError()
    value = {
        "schema": 1,
        "current_revision": revision,
        "versions": versions,
    }
    if len(_serialized_json(value)) > MAX_CURRENT_INDEX_BYTES:
        raise HistoryLimitError()


def _account_key(account_id: str) -> str:
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()


def load_auth_config(path: pathlib.Path, quota_override: Optional[int] = None) -> AuthConfig:
    try:
        file_stat = path.lstat()
    except OSError as exc:
        raise ConfigError("cannot stat auth file") from exc
    if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
        raise ConfigError("auth file must be a regular file")
    if stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise ConfigError("auth file must not be accessible by group or others")
    if file_stat.st_size > MAX_AUTH_FILE_BYTES:
        raise ConfigError("auth file is too large")

    try:
        with path.open("rb") as auth_file:
            raw = auth_file.read(MAX_AUTH_FILE_BYTES + 1)
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=_json_constant,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ConfigError("invalid auth file") from exc
    if type(value) is not dict or not set(value).issubset({"accounts", "default_quota_bytes"}):
        raise ConfigError("invalid auth file")
    raw_accounts = value.get("accounts")
    if type(raw_accounts) is not list or not raw_accounts:
        raise ConfigError("invalid auth file")

    default_quota = value.get("default_quota_bytes", DEFAULT_QUOTA_BYTES)
    if quota_override is not None:
        default_quota = quota_override
    if type(default_quota) is not int or default_quota <= 0:
        raise ConfigError("invalid default quota")

    accounts: List[Account] = []
    seen_ids = set()
    seen_hashes = set()
    for raw_account in raw_accounts:
        if type(raw_account) is not dict or not set(raw_account).issubset(
            {"id", "token_sha256", "quota_bytes"}
        ):
            raise ConfigError("invalid account")
        if "id" not in raw_account or "token_sha256" not in raw_account:
            raise ConfigError("invalid account")
        account_id = raw_account["id"]
        token_hash = raw_account["token_sha256"]
        if type(account_id) is not str or not account_id or len(account_id) > 128:
            raise ConfigError("invalid account")
        if any(unicodedata.category(ch) == "Cc" for ch in account_id):
            raise ConfigError("invalid account")
        if type(token_hash) is not str or HEX_SHA256_RE.fullmatch(token_hash) is None:
            raise ConfigError("invalid account token hash")
        quota = raw_account.get("quota_bytes", default_quota)
        if type(quota) is not int or quota <= 0:
            raise ConfigError("invalid account quota")
        if account_id in seen_ids or token_hash in seen_hashes:
            raise ConfigError("duplicate account identity")
        seen_ids.add(account_id)
        seen_hashes.add(token_hash)
        accounts.append(Account(account_id, token_hash, quota, _account_key(account_id)))
    return AuthConfig(tuple(accounts))


def _fsync_dir(path: pathlib.Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    fd = os.open(str(path), flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_private_dir(path: pathlib.Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    file_stat = path.lstat()
    if not stat.S_ISDIR(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
        raise StoredDataError("private path is not a directory")
    os.chmod(str(path), 0o700)


def _write_fd_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        view = view[written:]


def _atomic_write_json(path: pathlib.Path, value: object) -> None:
    data = _serialized_json(value)
    temporary = path.parent / (".tmp-" + uuid.uuid4().hex)
    fd = -1
    try:
        fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(fd, 0o600)
        _write_fd_all(fd, data)
        os.fsync(fd)
        os.close(fd)
        fd = -1
        os.replace(str(temporary), str(path))
        os.chmod(str(path), 0o600)
        _fsync_dir(path.parent)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_private_bytes(path: pathlib.Path, maximum: int) -> bytes:
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise StoredDataError("cannot open private file") from exc
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode) or stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise StoredDataError("private file permissions are invalid")
        if file_stat.st_size > maximum:
            raise StoredDataError("private file is too large")
        chunks: List[bytes] = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum:
            raise StoredDataError("private file is too large")
        return data
    finally:
        os.close(fd)


def _read_private_json(path: pathlib.Path, maximum: int) -> object:
    raw = _read_private_bytes(path, maximum)
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_object_keys,
            parse_constant=_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise StoredDataError("invalid private JSON") from exc


class StateStore:
    def __init__(self, data_dir: pathlib.Path, accounts: Mapping[str, Account]):
        self.data_dir = pathlib.Path(data_dir).absolute()
        self.accounts_root = self.data_dir / "accounts"
        self.accounts = dict(accounts)
        self._locks: Dict[str, threading.RLock] = {
            key: threading.RLock() for key in self.accounts
        }
        _ensure_private_dir(self.data_dir)
        _ensure_private_dir(self.accounts_root)

    def recover_all(self) -> None:
        for account in self.accounts.values():
            self._recover(account)

    def _account_dir(self, account: Account) -> pathlib.Path:
        path = self.accounts_root / account.key
        _ensure_private_dir(path)
        return path

    def _versions_dir(self, account: Account) -> pathlib.Path:
        path = self._account_dir(account) / "versions"
        _ensure_private_dir(path)
        return path

    def _current_path(self, account: Account) -> pathlib.Path:
        return self._account_dir(account) / "current.json"

    def _pending_path(self, account: Account) -> pathlib.Path:
        return self._account_dir(account) / "pending.json"

    def _recover(self, account: Account) -> None:
        with self._locks[account.key]:
            self._recover_locked(account)

    def _recover_locked(self, account: Account) -> None:
        account_dir = self._account_dir(account)
        pending_path = self._pending_path(account)
        pending_exists = pending_path.exists()
        if pending_exists:
            pending_obj = _read_private_json(pending_path, 64 * 1024)
            if type(pending_obj) is not dict or set(pending_obj) != {
                "schema",
                "prior_revision",
                "state",
            }:
                raise StoredDataError("invalid pending transaction")
            if pending_obj["schema"] != 1 or type(pending_obj["prior_revision"]) is not str:
                raise StoredDataError("invalid pending transaction")
            pending_state = _validate_state(pending_obj["state"])
            revision = pending_state["revision"]
            version_path = self._versions_dir(account) / revision
            if version_path.is_dir():
                self._read_verified_manifest(account, pending_state)
                current = self._read_current(account)
                if all(state["revision"] != revision for state in current["versions"]):
                    versions = [pending_state] + current["versions"]
                    self._write_current(account, revision, versions)
                pending_path.unlink()
                _fsync_dir(account_dir)
            else:
                pending_path.unlink()
                _fsync_dir(account_dir)

        for staging in account_dir.glob(".staging-*"):
            if staging.is_dir() and not staging.is_symlink():
                shutil.rmtree(staging)
        for temporary in account_dir.glob(".tmp-*"):
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _read_current(self, account: Account) -> Dict[str, object]:
        path = self._current_path(account)
        if not path.exists():
            return {"schema": 1, "current_revision": "", "versions": []}
        value = _read_private_json(path, MAX_CURRENT_INDEX_BYTES)
        if type(value) is not dict or set(value) != {"schema", "current_revision", "versions"}:
            raise StoredDataError("invalid current pointer")
        if value["schema"] != 1 or type(value["current_revision"]) is not str:
            raise StoredDataError("invalid current pointer")
        raw_versions = value["versions"]
        if type(raw_versions) is not list:
            raise StoredDataError("invalid current pointer")
        versions = [_validate_state(raw_state) for raw_state in raw_versions]
        seen_revisions = set()
        for state in versions:
            if state["revision"] in seen_revisions:
                raise StoredDataError("duplicate historical revision")
            seen_revisions.add(state["revision"])
        current_revision = value["current_revision"]
        if versions and versions[0]["revision"] != current_revision:
            raise StoredDataError("current revision does not match history")
        if not versions and current_revision:
            raise StoredDataError("current revision has no history")
        return {
            "schema": 1,
            "current_revision": current_revision,
            "versions": versions,
        }

    def _write_current(
        self, account: Account, current_revision: str, versions: List[Dict[str, object]]
    ) -> None:
        _ensure_history_index_capacity(current_revision, versions)
        _atomic_write_json(
            self._current_path(account),
            {
                "schema": 1,
                "current_revision": current_revision,
                "versions": versions,
            },
        )

    def _manifest_path(self, account: Account, state: Mapping[str, object]) -> pathlib.Path:
        revision = state["revision"]
        if type(revision) is not str or REVISION_RE.fullmatch(revision) is None:
            raise StoredDataError("invalid revision")
        return self._versions_dir(account) / revision / "snapshot.json"

    def _history_usage_locked(self, account: Account, versions: Sequence[Mapping[str, object]]) -> int:
        total = 0
        for state in versions:
            path = self._manifest_path(account, state)
            try:
                file_stat = path.lstat()
            except OSError as exc:
                raise StoredDataError("historical snapshot is missing") from exc
            if not stat.S_ISREG(file_stat.st_mode) or stat.S_ISLNK(file_stat.st_mode):
                raise StoredDataError("historical snapshot is invalid")
            total += file_stat.st_size
        return total

    def _read_verified_manifest(
        self, account: Account, expected_state: Mapping[str, object]
    ) -> List[FileEntry]:
        path = self._manifest_path(account, expected_state)
        value = _read_private_json(path, MAX_STORED_MANIFEST_BYTES)
        if type(value) is not dict or set(value) != {"schema", "state", "files"}:
            raise StoredDataError("invalid snapshot manifest")
        if value["schema"] != 1:
            raise StoredDataError("invalid snapshot manifest")
        state = _validate_state(value["state"])
        if state != dict(expected_state):
            raise StoredDataError("snapshot state mismatch")
        try:
            files, total_bytes, digest = validate_files(value["files"])
        except ApiError as exc:
            raise StoredDataError("invalid snapshot manifest") from exc
        if (
            digest != state["snapshot_sha256"]
            or total_bytes != state["total_bytes"]
            or len(files) != state["file_count"]
        ):
            raise StoredDataError("snapshot manifest checksum mismatch")
        return list(files)

    def get_state(self, account: Account) -> Dict[str, object]:
        with self._locks[account.key]:
            self._recover_locked(account)
            current = self._read_current(account)
            versions = current["versions"]
            return dict(versions[0]) if versions else _empty_state()

    def get_history(self, account: Account) -> List[Dict[str, object]]:
        with self._locks[account.key]:
            self._recover_locked(account)
            return [dict(state) for state in self._read_current(account)["versions"]]

    def get_snapshot(self, account: Account, revision: str) -> Dict[str, object]:
        if type(revision) is not str or not revision or len(revision) > MAX_REVISION_LENGTH:
            raise ApiError(400, "invalid_request")
        with self._locks[account.key]:
            self._recover_locked(account)
            versions = self._read_current(account)["versions"]
            state = next((state for state in versions if state["revision"] == revision), None)
            if state is None:
                raise ApiError(404, "not_found")
            files = self._read_verified_manifest(account, state)
            result = dict(state)
            result["files"] = [
                {
                    "path": entry.path,
                    "size": entry.size,
                    "sha256": entry.sha256,
                    "data": entry.encoded,
                }
                for entry in files
            ]
            return result

    def _write_manifest(
        self, staging: pathlib.Path, state: Mapping[str, object], files: Sequence[FileEntry]
    ) -> int:
        manifest = {
            "schema": 1,
            "state": dict(state),
            "files": [
                {
                    "path": entry.path,
                    "size": entry.size,
                    "sha256": entry.sha256,
                    "data": entry.encoded,
                }
                for entry in files
            ],
        }
        data = (json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        path = staging / "snapshot.json"
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
            _write_fd_all(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        _fsync_dir(staging)
        return len(data)

    def put(self, account: Account, raw_request: bytes) -> Dict[str, object]:
        request = parse_put_request(raw_request)
        with self._locks[account.key]:
            self._recover_locked(account)
            current = self._read_current(account)
            versions = current["versions"]
            current_state = dict(versions[0]) if versions else _empty_state()
            if request.base_revision != current_state["revision"]:
                raise ConflictError(current_state)
            if current_state["snapshot_sha256"] == request.snapshot_sha256:
                return current_state

            account_dir = self._account_dir(account)
            versions_dir = self._versions_dir(account)
            revision = "r1-" + uuid.uuid4().hex
            state = {
                "revision": revision,
                "snapshot_sha256": request.snapshot_sha256,
                "updated_at": _utc_now(),
                "device": request.device,
                "file_count": len(request.files),
                "total_bytes": request.total_bytes,
            }
            _ensure_history_index_capacity(revision, [state] + versions)
            staging = account_dir / (".staging-" + uuid.uuid4().hex)
            staging.mkdir(mode=0o700)
            os.chmod(str(staging), 0o700)
            pending_path = self._pending_path(account)
            published = False
            manifest_size = 0
            try:
                manifest_size = self._write_manifest(staging, state, request.files)
                usage = self._history_usage_locked(account, versions)
                if usage + manifest_size > account.quota_bytes:
                    raise QuotaExceededError()
                _atomic_write_json(
                    pending_path,
                    {
                        "schema": 1,
                        "prior_revision": current_state["revision"],
                        "state": state,
                    },
                )
                destination = versions_dir / revision
                os.replace(str(staging), str(destination))
                published = True
                _fsync_dir(versions_dir)
                self._write_current(account, revision, [state] + versions)
            except Exception:
                if not published:
                    try:
                        pending_path.unlink()
                    except FileNotFoundError:
                        pass
                raise
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
            try:
                pending_path.unlink()
                _fsync_dir(account_dir)
            except FileNotFoundError:
                pass
            return state


class CloudSaveHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, handler_class, socket_timeout, max_workers):
        if max_workers <= 0:
            raise ValueError("max_workers must be positive")
        if socket_timeout <= 0:
            raise ValueError("socket_timeout must be positive")
        self.socket_timeout = float(socket_timeout)
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        super().__init__(server_address, handler_class)

    def get_request(self):
        request, client_address = super().get_request()
        request.settimeout(self.socket_timeout)
        return request, client_address

    def process_request(self, request: socket.socket, client_address: Tuple[str, int]) -> None:
        if not self._worker_slots.acquire(blocking=False):
            self._reject_busy(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request: socket.socket, client_address: Tuple[str, int]) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()

    def _reject_busy(self, request: socket.socket) -> None:
        body = b'{"error":"server_busy"}'
        response = (
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Type: application/json; charset=utf-8\r\n"
            + b"Content-Length: "
            + str(len(body)).encode("ascii")
            + b"\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
            + body
        )
        try:
            request.settimeout(1.0)
            request.sendall(response)
            request.shutdown(socket.SHUT_WR)
            # Drain bytes already queued by the client so closing the socket
            # with unread request data does not turn the 503 into a TCP RST.
            deadline = time.monotonic() + 0.05
            remaining = 64 * 1024
            while remaining > 0:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    break
                request.settimeout(min(0.05, wait))
                chunk = request.recv(min(4096, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            pass
        finally:
            try:
                request.close()
            except OSError:
                pass
        sys.stderr.write("CONNECT 503\n")
        sys.stderr.flush()

    def handle_error(self, request: socket.socket, client_address: Tuple[str, int]) -> None:
        # Request paths and exception details can contain private data. The
        # response status is logged by the handler; do not print tracebacks.
        return


def _json_response(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def make_handler(store: StateStore, accounts: Sequence[Account]):
    account_by_key = {account.key: account for account in accounts}

    class Handler(BaseHTTPRequestHandler):
        server_version = "D2RCloudSave/1"
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            # Deliberately omit the request path and headers: either can contain
            # credentials or save-related information.
            sys.stderr.write("{} {}\n".format(self.command, getattr(self, "last_status", "")))
            sys.stderr.flush()

        def _send_json(self, status: int, value: object, headers: Optional[Mapping[str, str]] = None) -> None:
            self.last_status = status
            body = _json_response(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            if headers:
                for key, header_value in headers.items():
                    self.send_header(key, header_value)
            self.end_headers()
            self.close_connection = True
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_error(self, status: int, error: str, extra: Optional[Mapping[str, object]] = None) -> None:
            value: Dict[str, object] = {"error": error}
            if extra:
                value.update(extra)
            headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
            self._send_json(status, value, headers)

        def _authenticate(self) -> Account:
            authorization = self.headers.get("Authorization", "")
            if not authorization.startswith("Bearer "):
                raise ApiError(401, "unauthorized")
            token = authorization[len("Bearer ") :]
            if not token or len(token) > 1024 or any(ch.isspace() for ch in token):
                raise ApiError(401, "unauthorized")
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            matched_key = None
            for account in accounts:
                if hmac.compare_digest(token_hash, account.token_sha256):
                    matched_key = account.key
            account = account_by_key.get(matched_key) if matched_key else None
            if account is None:
                raise ApiError(401, "unauthorized")
            return account

        def _read_body(self) -> bytes:
            if self.headers.get("Transfer-Encoding"):
                raise ApiError(400, "invalid_request")
            if self.headers.get("Content-Encoding"):
                raise ApiError(400, "invalid_request")
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                raise ApiError(400, "invalid_request")
            raw_length = self.headers.get("Content-Length")
            if raw_length is None or not raw_length.isdigit():
                raise ApiError(400, "invalid_request")
            length = int(raw_length)
            if length > MAX_HTTP_BODY_BYTES:
                raise ApiError(413, "request_too_large")
            chunks: List[bytes] = []
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ApiError(400, "invalid_request")
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        def _dispatch(self) -> None:
            try:
                parsed = urlsplit(self.path)
                if parsed.path == "/healthz":
                    if self.command != "GET":
                        raise ApiError(405, "method_not_allowed")
                    if parsed.query:
                        raise ApiError(400, "invalid_request")
                    self._send_json(200, {"status": "ok"})
                    return
                if not parsed.path.startswith("/v1/"):
                    raise ApiError(404, "not_found")

                account = self._authenticate()
                if parsed.path == "/v1/state":
                    if self.command != "GET" or parsed.query:
                        raise ApiError(405 if self.command != "GET" else 400, "invalid_request")
                    self._send_json(200, store.get_state(account))
                    return
                if parsed.path == "/v1/history":
                    if self.command != "GET" or parsed.query:
                        raise ApiError(405 if self.command != "GET" else 400, "invalid_request")
                    self._send_json(200, {"versions": store.get_history(account)})
                    return
                if parsed.path == "/v1/snapshot" and self.command == "GET":
                    query = parse_qs(parsed.query, keep_blank_values=True)
                    if set(query) != {"revision"} or len(query["revision"]) != 1:
                        raise ApiError(400, "invalid_request")
                    self._send_json(200, store.get_snapshot(account, query["revision"][0]))
                    return
                if parsed.path == "/v1/snapshot" and self.command == "PUT":
                    if parsed.query:
                        raise ApiError(400, "invalid_request")
                    state = store.put(account, self._read_body())
                    self._send_json(200, state)
                    return
                if parsed.path in ("/v1/state", "/v1/snapshot", "/v1/history"):
                    raise ApiError(405, "method_not_allowed")
                raise ApiError(404, "not_found")
            except ConflictError as exc:
                self._send_error(409, "conflict", {"state": exc.state})
            except QuotaExceededError:
                self._send_error(507, "quota_exceeded")
            except HistoryLimitError:
                self._send_error(507, "history_limit_exceeded")
            except StoredDataError:
                self._send_error(500, "stored_data_error")
            except ApiError as exc:
                self._send_error(exc.status, exc.error, exc.extra)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True
            except Exception:
                self._send_error(500, "internal_error")

        def do_GET(self) -> None:
            self._dispatch()

        def do_PUT(self) -> None:
            self._dispatch()

        def do_POST(self) -> None:
            self._dispatch()

        def do_DELETE(self) -> None:
            self._dispatch()

        def do_PATCH(self) -> None:
            self._dispatch()

    return Handler


def create_server(
    host: str,
    port: int,
    data_dir: pathlib.Path,
    accounts: Sequence[Account],
    socket_timeout: float = DEFAULT_SOCKET_TIMEOUT_SECONDS,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> CloudSaveHTTPServer:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("host must be a loopback IP address") from exc
    if not address.is_loopback:
        raise ValueError("host must be a loopback IP address")

    account_mapping = {account.key: account for account in accounts}
    store = StateStore(data_dir, account_mapping)
    store.recover_all()

    server_class = CloudSaveHTTPServer
    if address.version == 6:
        class CloudSaveHTTPServerV6(CloudSaveHTTPServer):
            address_family = socket.AF_INET6

        server_class = CloudSaveHTTPServerV6
    httpd = server_class(
        (host, port),
        make_handler(store, accounts),
        socket_timeout=socket_timeout,
        max_workers=max_workers,
    )
    httpd.store = store  # type: ignore[attr-defined]
    return httpd


def _acquire_process_lock(data_dir: pathlib.Path):
    _ensure_private_dir(data_dir)
    lock_path = data_dir / "server.lock"
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    os.fchmod(fd, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise ConfigError("data directory is already in use")
    return fd


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="D2R Mobile cloud save service")
    parser.add_argument("--host", default=DEFAULT_HOST, help="loopback bind address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data-dir", type=pathlib.Path, required=True)
    parser.add_argument("--auth-file", type=pathlib.Path, required=True)
    parser.add_argument(
        "--quota-bytes",
        type=int,
        default=None,
        help="override the auth config default quota (default: 2 GiB)",
    )
    parser.add_argument(
        "--socket-timeout",
        type=float,
        default=DEFAULT_SOCKET_TIMEOUT_SECONDS,
        help="per-socket read timeout in seconds (default: 15)",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=DEFAULT_MAX_WORKERS,
        help="maximum concurrent HTTP connections (default: 8)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.port < 0 or args.port > 65535:
        print("port must be between 0 and 65535", file=sys.stderr)
        return 2
    if args.quota_bytes is not None and args.quota_bytes <= 0:
        print("quota must be positive", file=sys.stderr)
        return 2
    if args.socket_timeout <= 0 or args.max_workers <= 0:
        print("socket timeout and max workers must be positive", file=sys.stderr)
        return 2
    try:
        config = load_auth_config(args.auth_file, args.quota_bytes)
        lock_fd = _acquire_process_lock(args.data_dir)
        httpd = create_server(
            args.host,
            args.port,
            args.data_dir,
            config.accounts,
            socket_timeout=args.socket_timeout,
            max_workers=args.max_workers,
        )
    except (ConfigError, OSError, ValueError) as exc:
        print("startup error: {}".format(type(exc).__name__), file=sys.stderr)
        return 1

    try:
        host, port = httpd.server_address[:2]
        print("d2r cloud save listening on {}:{}".format(host, port), flush=True)
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        os.close(lock_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
