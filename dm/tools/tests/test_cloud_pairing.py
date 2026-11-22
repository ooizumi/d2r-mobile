"""Pairing credentials never travel in exported source or command logging."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]

def module(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result

build = module('mobile_build')
install = module('mobile_install')

class PairingTests(unittest.TestCase):
    def test_pairing_is_injected_only_into_selected_private_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / 'pairing.json'
            cfg.write_text(json.dumps({'endpoint': 'https://example.com/cloud/v1', 'token': 'test-only-' * 5}))
            for target, relative in [('android', 'android/app/src/main/assets/cloud-save.json'),
                                     ('ohos', 'harmony/app/entry/src/main/resources/rawfile/cloud-save.json')]:
                tree = root / target
                build.stage_cloud_config(tree / 'dm', target, cfg)
                output = tree / 'dm' / relative
                self.assertEqual(json.loads(output.read_text()), json.loads(cfg.read_text()))
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                self.assertTrue(build.is_excluded_source('dm/' + relative))
                self.assertTrue(install.excluded_export_path('dm/' + relative, set(), tree, root / 'export.zip'))

    def test_bad_configuration_errors_do_not_echo_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / 'pairing.json'
            secret = 'never-print-this-credential-' * 2
            for endpoint in ['http://example.com/v1', 'https://user:pass@example.com/v1',
                             'https://example.com/v1?token=secret', 'https://example.com']:
                cfg.write_text(json.dumps({'endpoint': endpoint, 'token': secret}))
                with self.assertRaises(build.BuildError) as raised:
                    build.read_cloud_config(cfg)
                self.assertNotIn(secret, str(raised.exception))

if __name__ == '__main__':
    unittest.main()
