import ast
import base64
import concurrent.futures
import dataclasses
import hashlib
import http.client
import json
import os
import pathlib
import socket
import stat
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.parse import quote

from dm.cloud_save import server


TOKEN = "unit-test-token-0123456789abcdef"
ACCOUNT_ID = "test-account"


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def snapshot_digest(files):
    hasher = hashlib.sha256()
    ordered = sorted(files, key=lambda item: item["path"].encode("utf-16-be"))
    for entry in ordered:
        hasher.update(entry["path"].encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(str(entry["size"]).encode("ascii"))
        hasher.update(b"\0")
        hasher.update(entry["sha256"].encode("ascii"))
        hasher.update(b"\n")
    return hasher.hexdigest()


def file_record(path, data):
    return {
        "path": path,
        "size": len(data),
        "sha256": sha256_bytes(data),
        "data": base64.b64encode(data).decode("ascii"),
    }


def make_request(base_revision, files, device="Android", digest=None):
    records = [file_record(path, data) for path, data in files]
    records.sort(key=lambda item: item["path"].encode("utf-16-be"))
    return {
        "base_revision": base_revision,
        "device": device,
        "snapshot_sha256": snapshot_digest(records) if digest is None else digest,
        "files": records,
    }


class ServiceHarness(unittest.TestCase):
    quota_bytes = 16 * 1024 * 1024

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(prefix="d2r-cloud-save-test-")
        self.data_dir = pathlib.Path(self.tempdir.name) / "data"
        self.account = server.Account(
            ACCOUNT_ID,
            sha256_bytes(TOKEN.encode("utf-8")),
            self.quota_bytes,
            server._account_key(ACCOUNT_ID),
        )
        self.httpd = None
        self.thread = None
        self.start_server(self.account)

    def tearDown(self):
        self.stop_server()
        self.tempdir.cleanup()

    def start_server(self, account, socket_timeout=server.DEFAULT_SOCKET_TIMEOUT_SECONDS, max_workers=8):
        self.account = account
        self.httpd = server.create_server(
            "127.0.0.1",
            0,
            self.data_dir,
            [account],
            socket_timeout=socket_timeout,
            max_workers=max_workers,
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop_server(self):
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            if self.thread is not None:
                self.thread.join(timeout=5)
        self.httpd = None
        self.thread = None

    def restart_server(self, quota_bytes):
        self.stop_server()
        self.start_server(dataclasses.replace(self.account, quota_bytes=quota_bytes))

    def request(self, method, path, value=None, token=TOKEN, raw=None, headers=None, content_length=None):
        request_headers = dict(headers or {})
        if token is not None and "Authorization" not in request_headers:
            request_headers["Authorization"] = "Bearer " + token
        body = raw
        if value is not None:
            body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if body is not None and "Content-Type" not in request_headers:
            request_headers["Content-Type"] = "application/json"
        if content_length is not None:
            request_headers["Content-Length"] = str(content_length)

        connection = http.client.HTTPConnection(*self.httpd.server_address[:2], timeout=10)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            response_body = response.read()
            parsed = json.loads(response_body.decode("utf-8")) if response_body else None
            return response.status, dict(response.getheaders()), parsed, response_body
        finally:
            connection.close()


class ProtocolTests(ServiceHarness):
    def test_health_auth_lifecycle_and_stale_conflict(self):
        status, _, value, _ = self.request("GET", "/healthz", token=None)
        self.assertEqual((status, value), (200, {"status": "ok"}))

        status, _, value, raw = self.request("GET", "/v1/state", token="wrong-token")
        self.assertEqual(status, 401)
        self.assertEqual(value, {"error": "unauthorized"})
        self.assertNotIn(b"wrong-token", raw)

        status, _, state, _ = self.request("GET", "/v1/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["revision"], "")
        self.assertEqual(state["snapshot_sha256"], "")

        initial = make_request(
            "",
            [("Hero/Hero.d2s", b"hero-one"), ("地图/角色.d2s", b"unicode")],
        )
        status, _, first_state, _ = self.request("PUT", "/v1/snapshot", initial)
        self.assertEqual(status, 200)
        self.assertEqual(first_state["file_count"], 2)
        self.assertEqual(first_state["total_bytes"], 15)
        self.assertTrue(first_state["revision"].startswith("r1-"))

        revision = first_state["revision"]
        status, _, remote, _ = self.request(
            "GET", "/v1/snapshot?revision=" + quote(revision, safe="")
        )
        self.assertEqual(status, 200)
        self.assertEqual(remote["files"], initial["files"])

        status, _, history, _ = self.request("GET", "/v1/history")
        self.assertEqual(status, 200)
        self.assertEqual(history["versions"], [first_state])

        status, _, stale, _ = self.request(
            "PUT",
            "/v1/snapshot",
            make_request("obsolete-revision", [("Hero/Hero.d2s", b"new")]),
        )
        self.assertEqual(status, 409)
        self.assertEqual(stale["error"], "conflict")
        self.assertEqual(stale["state"], first_state)

        equivalent = dict(initial)
        equivalent["base_revision"] = revision
        status, _, unchanged, _ = self.request("PUT", "/v1/snapshot", equivalent)
        self.assertEqual(status, 200)
        self.assertEqual(unchanged, first_state)
        status, _, history, _ = self.request("GET", "/v1/history")
        self.assertEqual(len(history["versions"]), 1)

        changed = make_request(revision, [("Hero/Hero.d2s", b"hero-two")], device="HarmonyOS")
        status, _, second_state, _ = self.request("PUT", "/v1/snapshot", changed)
        self.assertEqual(status, 200)
        self.assertNotEqual(second_state["revision"], revision)
        status, _, history, _ = self.request("GET", "/v1/history")
        self.assertEqual([item["revision"] for item in history["versions"]], [second_state["revision"], revision])

    def test_rejects_malicious_and_malformed_snapshots(self):
        valid_data = b"payload"
        valid_record = file_record("Hero.d2s", valid_data)
        valid = make_request("", [("Hero.d2s", valid_data)])

        def put_value(value):
            return self.request("PUT", "/v1/snapshot", value)

        bad_path_records = []
        for bad_path in (
            "/Hero.d2s",
            "Hero\\Hero.d2s",
            "Hero:Hero.d2s",
            "Hero/\u0001file.d2s",
            "a/b/c/d/e/f/g/h/i",
        ):
            record = dict(valid_record)
            record["path"] = bad_path
            bad_path_records.append(record)
        for record in bad_path_records:
            value = dict(valid)
            value["files"] = [record]
            with self.subTest(path=record["path"]):
                self.assertEqual(put_value(value)[0], 400)

        prefix_records = [
            file_record("Hero", b"a"),
            file_record("Hero/Hero.d2s", b"b"),
        ]
        prefix_value = make_request("", [])
        prefix_value["files"] = prefix_records
        prefix_value["snapshot_sha256"] = snapshot_digest(prefix_records)
        self.assertEqual(put_value(prefix_value)[0], 400)

        duplicate_records = [valid_record, dict(valid_record)]
        duplicate_value = dict(valid)
        duplicate_value["files"] = duplicate_records
        duplicate_value["snapshot_sha256"] = snapshot_digest(duplicate_records)
        self.assertEqual(put_value(duplicate_value)[0], 400)

        mutations = []
        value = dict(valid)
        value["unexpected"] = True
        mutations.append(value)
        value = dict(valid)
        value["snapshot_sha256"] = "A" * 64
        mutations.append(value)
        value = json.loads(json.dumps(valid))
        value["files"][0]["size"] = True
        mutations.append(value)
        value = json.loads(json.dumps(valid))
        value["files"][0]["size"] += 1
        mutations.append(value)
        value = json.loads(json.dumps(valid))
        value["files"][0]["data"] = "not-base64!"
        mutations.append(value)
        value = json.loads(json.dumps(valid))
        value["files"][0]["sha256"] = "0" * 64
        mutations.append(value)
        for invalid in mutations:
            with self.subTest(invalid=invalid):
                self.assertEqual(put_value(invalid)[0], 400)

        duplicate_json = (
            b'{"base_revision":"","base_revision":"","device":"Android",'
            b'"snapshot_sha256":"' + b"0" * 64 + b'","files":[]}'
        )
        status, _, value, _ = self.request("PUT", "/v1/snapshot", raw=duplicate_json)
        self.assertEqual((status, value), (400, {"error": "invalid_request"}))

        status, _, value, _ = self.request(
            "PUT", "/v1/snapshot", value=valid, headers={"Content-Type": "text/plain"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(value, {"error": "invalid_request"})

        status, _, value, _ = self.request(
            "PUT",
            "/v1/snapshot",
            raw=b"",
            content_length=server.MAX_HTTP_BODY_BYTES + 1,
        )
        self.assertEqual((status, value), (413, {"error": "request_too_large"}))

        status, _, state, _ = self.request("GET", "/v1/state")
        self.assertEqual((status, state["revision"]), (200, ""))

    def test_concurrent_cas_allows_exactly_one_writer(self):
        initial = make_request("", [("Hero.d2s", b"initial")])
        status, _, first_state, _ = self.request("PUT", "/v1/snapshot", initial)
        self.assertEqual(status, 200)
        base = first_state["revision"]
        requests = [
            make_request(base, [("Hero.d2s", b"writer-a")]),
            make_request(base, [("Hero.d2s", b"writer-b")]),
        ]

        barrier = threading.Barrier(2)

        def send(payload):
            barrier.wait(timeout=5)
            return self.request("PUT", "/v1/snapshot", payload)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(send, requests))
        self.assertEqual(sorted(result[0] for result in results), [200, 409])

        status, _, state, _ = self.request("GET", "/v1/state")
        self.assertEqual(status, 200)
        self.assertIn(state["snapshot_sha256"], [item["snapshot_sha256"] for item in requests])
        status, _, history, _ = self.request("GET", "/v1/history")
        self.assertEqual(len(history["versions"]), 2)

    def test_quota_refuses_new_history_without_losing_current(self):
        first = make_request("", [("Hero.d2s", b"a" * 128)])
        status, _, first_state, _ = self.request("PUT", "/v1/snapshot", first)
        self.assertEqual(status, 200)

        first_manifest = (
            self.data_dir
            / "accounts"
            / self.account.key
            / "versions"
            / first_state["revision"]
            / "snapshot.json"
        )
        self.restart_server(first_manifest.stat().st_size * 2 - 1)

        second = make_request(first_state["revision"], [("Hero.d2s", b"b" * 128)])
        status, _, value, _ = self.request("PUT", "/v1/snapshot", second)
        self.assertEqual((status, value), (507, {"error": "quota_exceeded"}))

        status, _, state, _ = self.request("GET", "/v1/state")
        self.assertEqual((status, state), (200, first_state))
        status, _, history, _ = self.request("GET", "/v1/history")
        self.assertEqual(len(history["versions"]), 1)

    def test_history_limit_returns_507_and_keeps_existing_history(self):
        first = make_request("", [("Hero.d2s", b"first")])
        status, _, first_state, _ = self.request("PUT", "/v1/snapshot", first)
        self.assertEqual(status, 200)

        second = make_request(first_state["revision"], [("Hero.d2s", b"second")])
        with mock.patch.object(server, "MAX_HISTORY_VERSIONS", 1):
            status, _, value, _ = self.request("PUT", "/v1/snapshot", second)
        self.assertEqual((status, value), (507, {"error": "history_limit_exceeded"}))

        status, _, state, _ = self.request("GET", "/v1/state")
        self.assertEqual((status, state), (200, first_state))
        status, _, history, _ = self.request("GET", "/v1/history")
        self.assertEqual(len(history["versions"]), 1)

    def test_stored_manifest_corruption_is_detected_and_permissions_are_private(self):
        payload = make_request("", [("Hero.d2s", b"checked-data")])
        status, _, state, _ = self.request("PUT", "/v1/snapshot", payload)
        self.assertEqual(status, 200)

        account_dir = self.data_dir / "accounts" / self.account.key
        version_dir = account_dir / "versions" / state["revision"]
        manifest_path = version_dir / "snapshot.json"
        for path in (
            self.data_dir,
            account_dir,
            account_dir / "versions",
            version_dir,
        ):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700, str(path))
        for path in (account_dir / "current.json", manifest_path):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, str(path))

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["data"] = base64.b64encode(b"tampered").decode("ascii")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        os.chmod(str(manifest_path), 0o600)
        status, _, value, _ = self.request(
            "GET", "/v1/snapshot?revision=" + quote(state["revision"], safe="")
        )
        self.assertEqual((status, value), (500, {"error": "stored_data_error"}))

    def test_python_39_grammar(self):
        source_path = pathlib.Path(server.__file__)
        ast.parse(source_path.read_text(encoding="utf-8"), feature_version=(3, 9))

    def test_worker_limit_rejects_slow_connections(self):
        self.stop_server()
        self.start_server(self.account, socket_timeout=0.3, max_workers=2)
        slow_connections = []
        try:
            for _ in range(2):
                connection = socket.create_connection(self.httpd.server_address[:2], timeout=1)
                connection.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\n")
                slow_connections.append(connection)

            deadline = time.monotonic() + 2
            while self.httpd._worker_slots._value != 0 and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(self.httpd._worker_slots._value, 0)

            busy = socket.create_connection(self.httpd.server_address[:2], timeout=1)
            busy.sendall(b"GET /healthz HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            response = bytearray()
            try:
                while True:
                    header_end = response.find(b"\r\n\r\n")
                    if header_end >= 0:
                        content_length = 0
                        for line in response[:header_end].split(b"\r\n")[1:]:
                            if line.lower().startswith(b"content-length:"):
                                content_length = int(line.split(b":", 1)[1].strip())
                        if len(response) >= header_end + 4 + content_length:
                            break
                    chunk = busy.recv(4096)
                    if not chunk:
                        break
                    response.extend(chunk)
            except ConnectionResetError:
                # Some TCP stacks emit RST after the complete response when
                # the server closes a connection that was saturated. The
                # bytes already received are still valid HTTP.
                pass
            busy.close()
            self.assertTrue(response.startswith(b"HTTP/1.1 503 Service Unavailable"))
            self.assertIn(b'{"error":"server_busy"}', response)
        finally:
            for connection in slow_connections:
                connection.close()


class RecoveryTests(unittest.TestCase):
    def test_interrupted_commit_is_recovered_and_abandoned_staging_is_removed(self):
        with tempfile.TemporaryDirectory(prefix="d2r-cloud-save-recovery-") as temp:
            data_dir = pathlib.Path(temp) / "data"
            account = server.Account(
                ACCOUNT_ID,
                sha256_bytes(TOKEN.encode("utf-8")),
                16 * 1024 * 1024,
                server._account_key(ACCOUNT_ID),
            )
            accounts = {account.key: account}
            store = server.StateStore(data_dir, accounts)
            first_raw = json.dumps(
                make_request("", [("Hero.d2s", b"first")]), separators=(",", ":")
            ).encode("utf-8")
            first = store.put(account, first_raw)
            second_request = make_request(first["revision"], [("Hero.d2s", b"second")])
            second_raw = json.dumps(second_request, separators=(",", ":")).encode("utf-8")

            with mock.patch.object(store, "_write_current", side_effect=OSError("simulated crash")):
                with self.assertRaises(OSError):
                    store.put(account, second_raw)

            account_dir = data_dir / "accounts" / account.key
            self.assertTrue((account_dir / "pending.json").exists())
            unrecovered_current = store._read_current(account)
            self.assertEqual(unrecovered_current["current_revision"], first["revision"])
            self.assertEqual(len(unrecovered_current["versions"]), 1)
            abandoned = account_dir / ".staging-abandoned"
            abandoned.mkdir(mode=0o700)
            (abandoned / "partial").write_bytes(b"partial")

            recovered_store = server.StateStore(data_dir, accounts)
            recovered_store.recover_all()
            state = recovered_store.get_state(account)
            history = recovered_store.get_history(account)
            self.assertEqual(state["snapshot_sha256"], second_request["snapshot_sha256"])
            self.assertEqual(len(history), 2)
            self.assertFalse((account_dir / "pending.json").exists())
            self.assertFalse(abandoned.exists())
            snapshot = recovered_store.get_snapshot(account, state["revision"])
            self.assertEqual(snapshot["files"], second_request["files"])

    def test_directory_fsync_failure_after_publish_preserves_old_and_recovers_new(self):
        with tempfile.TemporaryDirectory(prefix="d2r-cloud-save-fsync-") as temp:
            data_dir = pathlib.Path(temp) / "data"
            account = server.Account(
                ACCOUNT_ID,
                sha256_bytes(TOKEN.encode("utf-8")),
                16 * 1024 * 1024,
                server._account_key(ACCOUNT_ID),
            )
            accounts = {account.key: account}
            store = server.StateStore(data_dir, accounts)
            first_raw = json.dumps(
                make_request("", [("Hero.d2s", b"first")]), separators=(",", ":")
            ).encode("utf-8")
            first = store.put(account, first_raw)
            second_request = make_request(first["revision"], [("Hero.d2s", b"second")])
            second_raw = json.dumps(second_request, separators=(",", ":")).encode("utf-8")

            account_dir = data_dir / "accounts" / account.key
            versions_dir = account_dir / "versions"
            original_fsync_dir = server._fsync_dir

            def fail_versions_fsync(path):
                if pathlib.Path(path) == versions_dir:
                    raise OSError("simulated directory fsync failure")
                return original_fsync_dir(path)

            with mock.patch.object(server, "_fsync_dir", side_effect=fail_versions_fsync):
                with self.assertRaises(OSError):
                    store.put(account, second_raw)

            self.assertTrue((account_dir / "pending.json").exists())
            unrecovered_current = store._read_current(account)
            self.assertEqual(unrecovered_current["current_revision"], first["revision"])
            self.assertEqual(len(unrecovered_current["versions"]), 1)

            recovered_store = server.StateStore(data_dir, accounts)
            recovered_store.recover_all()
            recovered_state = recovered_store.get_state(account)
            recovered_history = recovered_store.get_history(account)
            self.assertEqual(recovered_state["snapshot_sha256"], second_request["snapshot_sha256"])
            self.assertEqual(
                [item["revision"] for item in recovered_history],
                [recovered_state["revision"], first["revision"]],
            )
            self.assertEqual(
                recovered_store.get_snapshot(account, first["revision"])["files"],
                json.loads(first_raw)["files"],
            )

    def test_history_version_limit_returns_507_and_keeps_existing_history(self):
        with tempfile.TemporaryDirectory(prefix="d2r-cloud-save-history-") as temp:
            data_dir = pathlib.Path(temp) / "data"
            account = server.Account(
                ACCOUNT_ID,
                sha256_bytes(TOKEN.encode("utf-8")),
                16 * 1024 * 1024,
                server._account_key(ACCOUNT_ID),
            )
            store = server.StateStore(data_dir, {account.key: account})
            first = store.put(
                account,
                json.dumps(make_request("", [("Hero.d2s", b"first")]), separators=(",", ":")).encode(
                    "utf-8"
                ),
            )
            second = json.dumps(
                make_request(first["revision"], [("Hero.d2s", b"second")]),
                separators=(",", ":"),
            ).encode("utf-8")
            with mock.patch.object(server, "MAX_HISTORY_VERSIONS", 1):
                with self.assertRaises(server.HistoryLimitError):
                    store.put(account, second)
            self.assertEqual(store.get_state(account), first)
            self.assertEqual(len(store.get_history(account)), 1)

    def test_history_index_byte_limit_returns_507_before_publish(self):
        with tempfile.TemporaryDirectory(prefix="d2r-cloud-save-history-bytes-") as temp:
            data_dir = pathlib.Path(temp) / "data"
            account = server.Account(
                ACCOUNT_ID,
                sha256_bytes(TOKEN.encode("utf-8")),
                16 * 1024 * 1024,
                server._account_key(ACCOUNT_ID),
            )
            store = server.StateStore(data_dir, {account.key: account})
            first = store.put(
                account,
                json.dumps(make_request("", [("Hero.d2s", b"first")]), separators=(",", ":")).encode(
                    "utf-8"
                ),
            )
            second = json.dumps(
                make_request(first["revision"], [("Hero.d2s", b"second")]),
                separators=(",", ":"),
            ).encode("utf-8")
            current = store._read_current(account)
            current_size = len(server._serialized_json(current))
            with mock.patch.object(server, "MAX_CURRENT_INDEX_BYTES", current_size):
                with self.assertRaises(server.HistoryLimitError):
                    store.put(account, second)
            self.assertEqual(store.get_state(account), first)
            self.assertEqual(len(store.get_history(account)), 1)


class AuthConfigTests(unittest.TestCase):
    def _write_auth(self, directory, value, mode=0o600):
        path = pathlib.Path(directory) / "auth.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(str(path), mode)
        return path

    def test_auth_config_loading_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            token_hash = sha256_bytes(TOKEN.encode("utf-8"))
            path = self._write_auth(
                temp,
                {
                    "default_quota_bytes": 1234,
                    "accounts": [{"id": ACCOUNT_ID, "token_sha256": token_hash}],
                },
            )
            config = server.load_auth_config(path)
            self.assertEqual(config.accounts[0].quota_bytes, 1234)
            override = server.load_auth_config(path, 5678)
            self.assertEqual(override.accounts[0].quota_bytes, 5678)

            os.chmod(str(path), 0o644)
            with self.assertRaises(server.ConfigError):
                server.load_auth_config(path)

    def test_auth_config_rejects_duplicate_identity_and_invalid_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            token_hash = sha256_bytes(TOKEN.encode("utf-8"))
            for accounts in (
                [
                    {"id": ACCOUNT_ID, "token_sha256": token_hash},
                    {"id": ACCOUNT_ID, "token_sha256": "1" * 64},
                ],
                [
                    {"id": "one", "token_sha256": token_hash},
                    {"id": "two", "token_sha256": token_hash},
                ],
                [{"id": ACCOUNT_ID, "token_sha256": "REPLACE_ME"}],
            ):
                with self.subTest(accounts=accounts):
                    path = self._write_auth(temp, {"accounts": accounts})
                    with self.assertRaises(server.ConfigError):
                        server.load_auth_config(path)


if __name__ == "__main__":
    unittest.main()
