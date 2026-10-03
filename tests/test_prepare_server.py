"""Run with python3 -m unittest discover -s tests -v; no SSH or downloads."""
import argparse
import base64
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


spec = importlib.util.spec_from_file_location(
    'prepare_server', Path(__file__).resolve().parents[1] / 'scripts/prepare_server.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class RemoteFixture(unittest.TestCase):
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


class PreflightTests(RemoteFixture):
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


class SpaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='sshuf space ')
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def check(self, root, available='4096', needed=1024, df_status=0, home=None):
        calls = self.directory / 'df-args'
        # Run the generated shell; only the filesystem statistics are simulated.
        script = '''set -euo pipefail
df() {
    printf '%%s\\n' "$@" > %s
    printf 'Filesystem 1024-blocks Used Available Capacity Mounted on\\n'
    printf 'fixture 8192 4096 %%s 50%%%% /fixture\\n' %s
    return %d
}
root=%s
''' % (shlex.quote(str(calls)), shlex.quote(available), df_status,
       helper.remote_root_expression({'remote_root': root}))
        env = dict(os.environ)
        if home is not None:
            env['HOME'] = str(home)
        result = subprocess.run(['bash', '-s'], input=script + helper.space_check_script(needed),
                                text=True, capture_output=True, env=env, timeout=10)
        return result, calls.read_text().splitlines() if calls.exists() else []

    def test_absolute_and_home_relative_paths_use_nearest_existing_directory(self):
        for relative in (False, True):
            root = 'work space/new server' if relative else str(self.directory / 'work space/new server')
            with self.subTest(relative=relative):
                result, calls = self.check(root, home=self.directory)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(str(self.directory), calls)
                self.assertFalse((self.directory / 'work space').exists())

    def test_space_equal_to_budget_is_accepted(self):
        result, _ = self.check(str(self.directory), available='1024', needed=1024)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_space_failure_names_target_filesystem_and_amounts(self):
        target = self.directory / 'missing' / 'server'
        result, _ = self.check(str(target), available='511', needed=1024)
        self.assertNotEqual(result.returncode, 0)
        output = result.stdout + result.stderr
        for value in (str(target), str(self.directory), '511', '1024'):
            self.assertIn(value, output)
        self.assertFalse(target.parent.exists())

    def test_invalid_or_failed_df_cannot_pass_space_check(self):
        for available, status in [('garbage', 0), ('', 0), ('-1', 0), ('1.5', 0), ('999999', 7)]:
            with self.subTest(available=available, status=status):
                result, _ = self.check(str(self.directory / 'new'), available, df_status=status)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(str(self.directory), result.stdout + result.stderr)
                if status:
                    self.assertIn('df failed (exit %d)' % status, result.stderr)
                self.assertFalse((self.directory / 'new').exists())

    def test_non_directory_ancestor_is_rejected_without_df(self):
        ancestor = self.directory / 'file'
        ancestor.write_text('preserve')
        result, calls = self.check(str(ancestor / 'server'))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])
        self.assertEqual(ancestor.read_text(), 'preserve')

    def test_unwritable_ancestor_is_rejected(self):
        ancestor = self.directory / 'read only'
        ancestor.mkdir()
        ancestor.chmod(0o500)
        self.addCleanup(ancestor.chmod, 0o700)
        if os.access(ancestor, os.W_OK):
            self.skipTest('Current user bypasses directory permissions')
        result, calls = self.check(str(ancestor / 'server'))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])
        self.assertIn(str(ancestor), result.stdout + result.stderr)

    def test_cli_budget_counts_all_unpacked_components_and_largest_transfer(self):
        large = self.directory / 'server.tar.gz'
        small = self.directory / 'cli.tar.gz'
        large.write_bytes(b'x' * 1000)
        small.write_bytes(b'x' * 57)
        packages = [(large, 'unused', {'unpacked': 4096}), (small, 'unused', {'unpacked': 1024})]
        # 5 KiB extracted plus one transfer (1000 + 1354 bytes), rounded up.
        self.assertEqual(len(base64.encodebytes(large.read_bytes())), 1354)
        self.assertEqual(helper.required_space_kb(packages), 256 * 1024 + 8)
        self.assertEqual(helper.required_space_kb(packages[::-1]), helper.required_space_kb(packages))

    def test_remote_space_check_requires_execution_marker(self):
        config = {'remote_root': str(self.directory / 'server')}
        archive = self.directory / 'archive'
        archive.write_bytes(b'test')
        with patch.object(helper, 'ssh', return_value='Welcome to SSHD\n') as ssh:
            with self.assertRaises(helper.Failure):
                helper.check_space(config, 'normal', [(archive, 'unused', {'unpacked': 100})])
        self.assertIsInstance(ssh.call_args.args[2], str)
        self.assertNotIn('stream', ssh.call_args.kwargs)


class PreparationTests(RemoteFixture):
    def write_config(self):
        path = self.root / 'config.json'
        path.write_text(json.dumps(self.config))
        return argparse.Namespace(config=str(path), check_only=False)

    def test_custom_paths_are_read_only_for_all_layouts(self):
        for editor, layout in [('cursor', 'cursor'), ('vscode', 'vscode-cli'), ('vscode', 'vscode-legacy')]:
            for relative in (False, True):
                with self.subTest(layout=layout, relative=relative):
                    folder = self.root / 'work space' / layout
                    self.config.update(editor=editor, layout=layout,
                                       remote_root=str(folder.relative_to(self.root)) if relative else str(folder))
                    with patch.dict(os.environ, {'HOME': str(self.root)}):
                        for item in helper.artifacts(self.config, self.product, 'x64'):
                            self.assertFalse(helper.ready(self.config, self.product, 'x64', item, 'normal'))
                    self.assertFalse(folder.parent.exists())

    def test_check_only_never_downloads_checks_space_or_installs(self):
        args = self.write_config()
        args.check_only = True
        with patch.object(helper, 'probe', return_value=('normal', 'x64')), \
                patch.object(helper, 'get_archive') as download, \
                patch.object(helper, 'check_space') as space, \
                patch.object(helper, 'install') as install:
            self.assertEqual(helper.prepare(args), 10)
        download.assert_not_called()
        space.assert_not_called()
        install.assert_not_called()
        self.assertFalse(Path(self.config['remote_root']).exists())

    def test_space_failure_preserves_cache_and_existing_version_without_upload(self):
        args = self.write_config()
        old = Path(self.config['remote_root']) / 'old-version' / 'keep'
        old.parent.mkdir(parents=True)
        old.write_bytes(b'working server')
        cache = self.root / 'cache'
        cache.mkdir()
        archive = cache / 'server.tar.gz'
        archive.write_bytes(b'cached archive')
        meta = cache / 'server.tar.json'
        meta.write_text('{"sha256":"fixture"}')
        original = {file: file.read_bytes() for file in (old, archive, meta)}
        package = (archive, 'fixture', {'unpacked': 4096})
        with patch.object(helper, 'probe', return_value=('normal', 'x64')), \
                patch.object(helper, 'get_archive', return_value=package), \
                patch.object(helper, 'check_space', side_effect=helper.Failure('Insufficient disk space')), \
                patch.object(helper, 'install') as install:
            with self.assertRaisesRegex(helper.Failure, 'Insufficient disk space'):
                helper.prepare(args)
        install.assert_not_called()
        self.assertEqual({file: file.read_bytes() for file in original}, original)
        self.assertFalse(self.target().exists())

    def test_all_cli_archives_are_checked_together_before_first_upload(self):
        self.config.update(editor='vscode', layout='vscode-cli')
        app = Path(self.config['app']) / 'product.json'
        p = json.loads(app.read_text())
        p['applicationName'] = 'code'
        app.write_text(json.dumps(p))
        args = self.write_config()
        events = []
        packages = []

        def archive(_config, _product, _arch, item, cache):
            events.append('archive:' + item['kind'])
            path = cache / item['kind']
            path.write_bytes(b'fixture')
            package = (path, 'unused', {'unpacked': 1024})
            packages.append(package)
            return package

        def space(_config, _mode, batch):
            self.assertEqual(list(batch), packages)
            self.assertEqual(len(packages), 2)
            events.append('space')

        def install(_config, _product, _arch, item, *_args):
            events.append('install:' + item['kind'])

        with patch.object(helper, 'probe', return_value=('normal', 'x64')), \
                patch.object(helper, 'ready', side_effect=[False, False, True, True]), \
                patch.object(helper, 'get_archive', side_effect=archive), \
                patch.object(helper, 'check_space', side_effect=space), \
                patch.object(helper, 'install', side_effect=install):
            self.assertEqual(helper.prepare(args), 0)
        self.assertEqual(events, ['archive:server', 'archive:cli', 'space', 'install:server', 'install:cli'])

    def test_complete_installation_skips_download_space_check_and_upload(self):
        args = self.write_config()
        with patch.object(helper, 'probe', return_value=('normal', 'x64')), \
                patch.object(helper, 'ready', return_value=True), \
                patch.object(helper, 'get_archive') as download, \
                patch.object(helper, 'check_space') as space, \
                patch.object(helper, 'install') as install:
            self.assertEqual(helper.prepare(args), 0)
        download.assert_not_called()
        space.assert_not_called()
        install.assert_not_called()


class ConfigureTests(RemoteFixture):
    def configure(self, root=None):
        args = argparse.Namespace(**dict(self.config, state_dir=str(self.root / 'private runtime')))
        if root is not None:
            args.remote_root = root
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            helper.configure(args)
        return out.getvalue()

    def test_repeated_configuration_is_idempotent_and_explains_path_sync(self):
        root = str(self.root / 'work space' / '.cursor-server')
        output = self.configure(root)
        state = self.root / 'private runtime'
        before = {p.name: p.read_bytes() for p in state.iterdir()}
        self.assertEqual(self.configure(root), output)
        self.assertEqual({p.name: p.read_bytes() for p in state.iterdir()}, before)
        self.assertIn(root, output)
        self.assertIn('remote.SSH.serverInstallPath', output)
        self.assertEqual(json.loads((state / 'config.json').read_text())['remote_root'], root)

    def test_changed_root_does_not_overwrite_existing_configuration(self):
        self.configure()
        state = self.root / 'private runtime'
        before = {p.name: p.read_bytes() for p in state.iterdir()}
        with self.assertRaises(helper.Failure):
            self.configure(str(self.root / 'different'))
        self.assertEqual({p.name: p.read_bytes() for p in state.iterdir()}, before)

    def test_script_only_refresh_and_rollback_preserve_config_and_hook(self):
        self.configure()
        state = self.root / 'private runtime'
        runtime = state / 'prepare_server.py'
        original = runtime.read_bytes()
        previous = original + b'\n# Previous private runtime revision.\n'
        runtime.write_bytes(previous)
        backup = self.root / 'runtime-backup.py'
        shutil.copy2(runtime, backup)
        unchanged = {p: p.read_bytes() for p in (state / 'config.json', state / 'preconnect.sh')}
        with self.assertRaises(helper.Failure):
            self.configure()
        self.assertEqual(runtime.read_bytes(), previous)
        shutil.copy2(Path(helper.__file__), runtime)
        self.configure()
        self.assertEqual(runtime.read_bytes(), original)
        self.assertEqual({p: p.read_bytes() for p in unchanged}, unchanged)
        shutil.copy2(backup, runtime)
        self.assertEqual(runtime.read_bytes(), previous)
        self.assertEqual({p: p.read_bytes() for p in unchanged}, unchanged)


class CommandFailureTests(unittest.TestCase):
    def test_nonzero_exit_retains_status_stdout_and_stderr(self):
        with self.assertRaises(helper.Failure) as error:
            helper.run(['bash', '-c', 'echo diagnostic-out; echo diagnostic-err >&2; exit 4'])
        for value in ('4', 'diagnostic-out', 'diagnostic-err'):
            self.assertIn(value, str(error.exception))


@unittest.skipUnless(sys.platform.startswith('linux'), 'Actual heredoc temp files require Linux /proc and GNU tools')
class LinuxUploadTests(RemoteFixture):
    def setUp(self):
        super().setUp()
        for tool in ('flock', 'timeout', 'sha256sum', 'base64', 'tar', 'readlink', 'stat'):
            if not shutil.which(tool):
                self.skipTest('Missing Linux tool: ' + tool)
        self.config.update(editor='vscode', layout='vscode-legacy', remote_root=str(self.root / 'work space/server'))
        self.item = helper.artifacts(self.config, self.product, 'x64')[0]
        content = self.root / 'payload'
        (content / 'bin').mkdir(parents=True)
        (content / 'out').mkdir()
        for name in ('node', 'bin/code-server'):
            path = content / name
            path.write_text('#!/bin/sh\nexit 0\n')
            path.chmod(0o700)
        (content / 'out/server-main.js').write_bytes(os.urandom(256 * 1024))
        (content / 'product.json').write_text(json.dumps({'commit': self.product['commit']}))
        self.archive = self.root / 'server.tar.gz'
        with tarfile.open(self.archive, 'w:gz') as archive:
            archive.add(content, arcname='server')
        self.info = {'strip': 1, 'unpacked': sum(p.stat().st_size for p in content.rglob('*') if p.is_file())}
        wrappers = self.root / 'commands'
        wrappers.mkdir()
        self.record = self.root / 'heredoc-record'
        wrapper = wrappers / 'base64'
        wrapper.write_text('''#!/bin/sh
if [ "$1" = '-d' ]; then
    printf '%%s\\n' "$TMPDIR" >> %s
    readlink /proc/$$/fd/0 >> %s
    stat -c '%%a' "$TMPDIR" >> %s
fi
exec %s "$@"
''' % tuple(shlex.quote(str(p)) for p in (self.record, self.record, self.record, shutil.which('base64'))))
        wrapper.chmod(0o700)
        self.env = dict(os.environ, PATH=str(wrappers) + os.pathsep + os.environ['PATH'])
        self.ssh_patch.stop()
        self.ssh_patch = patch.object(helper, 'ssh', side_effect=self.execute_upload)
        self.ssh_patch.start()
        self.addCleanup(self.ssh_patch.stop)

    def execute_upload(self, config, mode, script=None, stream=None, **_kwargs):
        data = stream.read() if stream else script.encode()
        prefix = b'uname() { case "$1" in -s) echo Linux;; -m) echo x86_64;; esac; }\n'
        result = subprocess.run(['bash', '-s'], input=prefix + data, capture_output=True,
                                env=self.env, timeout=30)
        if result.returncode:
            raise helper.Failure((result.stdout + result.stderr).decode())
        return result.stdout.decode()

    def assert_temp_cleaned(self):
        tmpdir, descriptor, permission = self.record.read_text().splitlines()
        self.assertTrue(tmpdir.startswith(self.config['remote_root'] + '/.sshuf-stage.'))
        self.assertTrue(descriptor.startswith(tmpdir + '/'), descriptor)
        self.assertEqual(permission, '700')
        self.assertFalse(Path(tmpdir).exists())
        self.assertEqual(list(Path(self.config['remote_root']).glob('.sshuf-stage.*')), [])

    def test_large_upload_uses_work_disk_and_cleans_staging_on_success(self):
        helper.install(self.config, self.product, 'x64', self.item, 'normal',
                       self.archive, helper.digest(self.archive), self.info)
        self.assert_temp_cleaned()
        self.assertTrue((self.target() / 'out/server-main.js').is_file())

    def test_large_upload_failure_cleans_staging_and_preserves_existing_files(self):
        old = Path(self.config['remote_root']) / 'old-version'
        old.parent.mkdir(parents=True)
        old.write_bytes(b'keep')
        with self.assertRaisesRegex(helper.Failure, 'checksum'):
            helper.install(self.config, self.product, 'x64', self.item, 'normal',
                           self.archive, '0' * 64, self.info)
        self.assert_temp_cleaned()
        self.assertEqual(old.read_bytes(), b'keep')
        self.assertFalse(self.target().exists())


if __name__ == '__main__':
    unittest.main()
