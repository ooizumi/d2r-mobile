# D2R Mobile cloud save service

This directory contains the independent cloud-save HTTP service. The server
keeps an isolated account tree under its data directory and never stores D2R
files in Joplin or uploads the DM/ROM.

The service uses only the Python 3.9 standard library. Bind it to loopback and
terminate HTTPS in the existing nginx instance.

## Setup

Create a non-login service account, install the source read-only under
`/opt/d2r-cloud-save`, and create private runtime directories:

```sh
sudo install -d -m 0700 -o d2r-cloud-save -g d2r-cloud-save /etc/d2r-cloud-save /var/lib/d2r-cloud-save
sudo install -m 0600 -o d2r-cloud-save -g d2r-cloud-save auth.json /etc/d2r-cloud-save/auth.json
sudo install -m 0644 d2r-cloud-save.service /etc/systemd/system/d2r-cloud-save.service
```

Generate each account token outside this source tree, place only its SHA-256 in
`auth.json`, and store the original token in the client's private config. The
following command prints a hash without echoing the token:

```sh
python3 -c 'import getpass,hashlib; print(hashlib.sha256(getpass.getpass("token: ").encode("utf-8")).hexdigest())'
```

`auth.json` rejects duplicate account IDs/token hashes, invalid SHA-256 values,
and group/world-readable permissions. `default_quota_bytes` defaults to 2 GiB.
An account can set its own `quota_bytes`. Runtime data directories are forced
to mode 0700 and JSON state files to mode 0600.

Run locally:

```sh
python3 dm/cloud_save/server.py \
  --host 127.0.0.1 \
  --port 8765 \
  --data-dir /var/lib/d2r-cloud-save \
  --auth-file /etc/d2r-cloud-save/auth.json \
  --socket-timeout 15 \
  --max-workers 8
```

Only literal loopback IP addresses (`127.0.0.1` or `::1`) are accepted. Add
`deploy/nginx-location.conf` to the existing HTTPS nginx server and connect
clients to `https://mythstribe.com/d2r-cloud/v1`.

The service allows at most eight concurrent request threads by default and
applies a 15-second socket read timeout to each connection. Additional
connections receive HTTP 503 immediately, and a stalled connection cannot hold
a worker indefinitely. The systemd template also caps the service at 384 MiB
memory and 50% of one CPU.

## Storage and recovery

Every accepted snapshot is immutable under `accounts/<hashed-id>/versions/`.
`current.json` is replaced atomically and lists every retained version newest
first. A snapshot is checked, fsynced, renamed into its immutable directory,
and only then published through the pointer. `pending.json` lets startup
complete a commit interrupted between those two steps. Staging directories
left before publication are discarded because they were never acknowledged.

Reads verify each stored file's size/SHA-256 and the snapshot digest. Writes use
the current revision as a compare-and-swap value. Quota usage includes all
retained history; exceeding it returns HTTP 507 and preserves existing data.
History is never pruned automatically. To keep `current.json` readable, a write
that would exceed 8192 retained versions or 4 MiB for the encoded history index
is also refused with HTTP 507 (`history_limit_exceeded`). Existing versions are
kept in either case.

## API

All `/v1/*` routes require `Authorization: Bearer <token>`. The service exposes:

* `GET /healthz`
* `GET /v1/state`
* `GET /v1/snapshot?revision=<revision>`
* `GET /v1/history`
* `PUT /v1/snapshot`

The wire format and validation limits are defined by
`docs/CLOUD_SAVE_PROTOCOL.md` in the source tree.
