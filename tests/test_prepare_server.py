"""Run with python3 -m unittest discover -s tests -v; no SSH or downloads."""
import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'prepare_server', Path(__file__).resolve().parents[1] / 'scripts/prepare_server.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.product = {'commit': 'a' * 40, 'download': 'b' * 40}
        self.config = {
            'editor': 'cursor', 'host': 'test-host', 'app': str(self.root / 'app'),
            'layout': 'cursor', 'transport': 'auto',
            'remote_root': str(self.root / 'remote'), 'proxy': '',
        }
        app = Path(self.config['app'])
        app.mkdir()
        (app / 'product.json').write_text(json.dumps({
            'applicationName': 'cursor', 'commit': self.product['commit'],
            'realCommit': self.product['download'], 'quality': 'stable',
        }))
        self.item = helper.artifacts(self.config, self.product, 'x64')[0]
        self.addCleanup(setattr, helper, 'LOG', helper.LOG)
        self.ssh_patch = patch.object(helper, 'ssh', side_effect=self.local_shell)
        self.ssh_patch.start()
        self.addCleanup(self.ssh_patch.stop)

    @staticmethod
    def local_shell(config, mode, script, **kwargs):
        # Execute the actual generated check against isolated local fixtures.
        # Stub platform/timeout so this also runs on a macOS developer machine.
        prefix = '''
uname() { case "$1" in -s) echo Linux;; -m) echo x86_64;; *) return 1;; esac; }
timeout() { shift; "$@"; }
'''
        return subprocess.run(['bash', '-s'], input=prefix + script, text=True,
                              capture_output=True, check=True, timeout=10).stdout

    def target(self):
        return Path(self.config['remote_root']) / self.item['target']

    def ready(self):
        return helper.ready(self.config, self.product, 'x64', self.item, 'normal')

    def test_absent_target_is_missing(self):
        self.assertFalse(self.ready())
        self.assertFalse(Path(self.config['remote_root']).exists())

    def test_empty_directory_is_preserved_and_rejected(self):
        target = self.target()
        target.mkdir(parents=True)
        with self.assertRaises(helper.Failure):
            self.ready()
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])

    def test_incomplete_files_are_preserved(self):
        target = self.target()
        target.mkdir(parents=True)
        marker = target / 'partial-download'
        marker.write_bytes(b'preserve me')
        with self.assertRaises(helper.Failure):
            self.ready()
        self.assertEqual(marker.read_bytes(), b'preserve me')

    def test_dangling_symlink_is_preserved_and_rejected(self):
        target = self.target()
        target.parent.mkdir(parents=True)
        target.symlink_to(self.root / 'missing')
        with self.assertRaises(helper.Failure):
            self.ready()
        self.assertTrue(target.is_symlink())

    def test_existing_cli_bundle_without_server_is_rejected(self):
        self.config.update(editor='vscode', layout='vscode-cli')
        self.item = helper.artifacts(self.config, self.product, 'x64')[0]
        self.target().parent.mkdir(parents=True)
        with self.assertRaises(helper.Failure):
            self.ready()
        self.assertTrue(self.target().parent.is_dir())

    def test_ready_cli_is_reused(self):
        self.config.update(editor='vscode', layout='vscode-cli')
        self.item = helper.artifacts(self.config, self.product, 'x64')[1]
        target = self.target()
        target.parent.mkdir(parents=True)
        target.write_text('#!/bin/sh\necho ' + self.product['commit'] + '\n')
        target.chmod(0o700)
        self.assertTrue(self.ready())

    def test_prepare_rejects_before_download_or_install(self):
        self.target().mkdir(parents=True)
        config = self.root / 'config.json'
        config.write_text(json.dumps(self.config))
        with patch.object(helper, 'probe', return_value=('normal', 'x64')), \
                patch.object(helper, 'get_archive') as download, \
                patch.object(helper, 'install') as install:
            for check_only in (True, False):
                with self.subTest(check_only=check_only):
                    with self.assertRaises(helper.Failure):
                        helper.prepare(argparse.Namespace(config=str(config), check_only=check_only))
            download.assert_not_called()
            install.assert_not_called()


if __name__ == '__main__':
    unittest.main()
