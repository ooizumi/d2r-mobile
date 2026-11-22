#!/usr/bin/env python3
"""Inspect/download cloud history, or explicitly republish a chosen revision.

No credentials are printed. Use a private pairing file outside the source tree.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud_save.server import validate_files, MAX_HTTP_BODY_BYTES
from mobile_build import read_cloud_config


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def request(config, path, payload=None):
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(config['endpoint'] + path, data=body,
        headers={'Authorization': 'Bearer ' + config['token'], 'Content-Type': 'application/json',
                 'User-Agent': 'D2RMobile-Cloud/1'},
        method='GET' if body is None else 'PUT')
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=30) as response:
            data = response.read(MAX_HTTP_BODY_BYTES + 1)
            if len(data) > MAX_HTTP_BODY_BYTES:
                raise ValueError('response exceeds snapshot limit')
            return json.loads(data)
    except urllib.error.HTTPError as exc:
        raise ValueError('cloud request failed: HTTP ' + str(exc.code)) from None
    except urllib.error.URLError:
        raise ValueError('cloud connection failed; credentials were not redirected') from None


def snapshot(config, revision):
    value = request(config, '/snapshot?revision=' + urllib.parse.quote(revision, safe=''))
    entries, total, digest = validate_files(value['files'])
    if value.get('revision') != revision or value.get('snapshot_sha256') != digest:
        raise ValueError('snapshot verification failed')
    if value.get('total_bytes') != total or value.get('file_count') != len(entries):
        raise ValueError('snapshot metadata mismatch')
    return value, entries


def download(config, revision, output):
    output = output.resolve()
    source = Path(__file__).resolve().parents[2]
    if output == source or source in output.parents:
        raise ValueError('save exports must stay outside the source checkout')
    if output.exists():
        raise ValueError('output already exists; choose a new backup directory')
    value, entries = snapshot(config, revision)
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = output.with_name('.' + output.name + '.partial-' + uuid.uuid4().hex)
    stage.mkdir(mode=0o700)
    try:
        for entry in entries:
            path = stage / entry.path
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with path.open('xb') as stream:
                os.chmod(path, 0o600)
                stream.write(entry.data)
                stream.flush()
                os.fsync(stream.fileno())
        # Verify bytes after writing before publishing the recovery export.
        import hashlib
        for entry in entries:
            data = (stage / entry.path).read_bytes()
            if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
                raise ValueError('export verification failed')
        for directory, _, _ in os.walk(stage, topdown=False):
            fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        if output.exists():
            raise ValueError('output appeared during export')
        os.rename(stage, output)
        fd = os.open(output.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return {'directory': str(output), 'revision': revision, 'file_count': len(entries)}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('history')
    get = sub.add_parser('download')
    get.add_argument('--revision', required=True)
    get.add_argument('--output', required=True, type=Path)
    restore = sub.add_parser('restore-cloud', help='explicitly make a retained revision current; keeps all versions')
    restore.add_argument('--revision', required=True)
    restore.add_argument('--expected-current', required=True)
    args = parser.parse_args()
    try:
        config = read_cloud_config(args.config)
        if args.command == 'history':
            result = request(config, '/history')
        elif args.command == 'download':
            result = download(config, args.revision, args.output)
        else:
            value, _ = snapshot(config, args.revision)
            result = request(config, '/snapshot', {'base_revision': args.expected_current,
                'device': 'Recovery', 'snapshot_sha256': value['snapshot_sha256'], 'files': value['files']})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # Only our bounded, credential-free messages are safe for stdout.
        message = str(exc) if type(exc) is ValueError else 'operation failed; no credentials logged'
        print(json.dumps({'error': message}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
