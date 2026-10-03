#!/usr/bin/env python3
"""Prepare official stable Cursor / VS Code servers without remote proxy access."""
import argparse
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlsplit


class Failure(Exception):
    pass


ACTIVE = None
LOG = None


def log(message):
    line = time.strftime('%Y-%m-%d %H:%M:%S ') + message
    print(line, flush=True)
    if LOG:
        with LOG.open('a') as stream:
            stream.write(line + '\n')


def stop():
    if ACTIVE is not None and ACTIVE.poll() is None:
        try:
            os.killpg(ACTIVE.pid, signal.SIGTERM)
            ACTIVE.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(ACTIVE.pid, signal.SIGKILL)
            ACTIVE.wait()
        except ProcessLookupError:
            pass


def cancelled(number, _frame):
    stop()
    raise SystemExit(128 + number)


def run(argv, data=None, stream=None, timeout=60):
    global ACTIVE
    env = {k: v for k, v in os.environ.items()
           if k.lower() not in ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy')}
    ACTIVE = subprocess.Popen(argv, stdin=stream or subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=env, start_new_session=True)
    try:
        out, err = ACTIVE.communicate(data, timeout=timeout)
        if ACTIVE.returncode:
            detail = '\n'.join(label + ': ' + value.decode('utf-8', 'replace')[-1600:]
                               for label, value in (('stderr', err), ('stdout', out)) if value)
            raise Failure('Command failed (%s, exit %s): %s' %
                          (Path(argv[0]).name, ACTIVE.returncode, detail))
        return out.decode('utf-8', 'replace')
    except subprocess.TimeoutExpired:
        stop()
        raise Failure('Command timed out; existing installations and cache are preserved')
    finally:
        ACTIVE = None


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def product(app, editor):
    root = Path(app).expanduser().absolute()
    choices = [root / 'Contents/Resources/app', root / 'resources/app', root]
    folder = next((p for p in choices if (p / 'product.json').is_file()), None)
    if folder is None:
        raise Failure('App path must point to the stable application or its resources/app folder')
    p = json.loads((folder / 'product.json').read_text())
    expected = 'cursor' if editor == 'cursor' else 'code'
    if p.get('applicationName') != expected or p.get('quality', 'stable') != 'stable':
        raise Failure('Only the official stable product is supported')
    commit = p.get('commit', '')
    download = p.get('realCommit', commit) if editor == 'cursor' else commit
    if not all(re.fullmatch('[0-9a-f]{40}', value or '') for value in (commit, download)):
        raise Failure('Unrecognized product build identifiers')
    return {'commit': commit, 'download': download}


def validate_config(c):
    required = {'editor', 'host', 'app', 'layout', 'transport', 'remote_root', 'proxy'}
    if not isinstance(c, dict) or set(c) != required or not all(isinstance(v, str) for v in c.values()):
        raise Failure('Configuration fields do not match this helper version')
    if c['editor'] not in ('cursor', 'vscode'):
        raise Failure('Unsupported editor')
    layouts = ('cursor',) if c['editor'] == 'cursor' else ('vscode-cli', 'vscode-legacy')
    if c['layout'] not in layouts or c['transport'] not in ('auto', 'bash-stdin'):
        raise Failure('Unsupported layout or transport; inspect Remote-SSH logs first')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]*', c['host']):
        raise Failure('Use an SSH config alias without whitespace or shell syntax')
    root = PurePosixPath(c['remote_root'])
    if str(root) in ('.', '/', '') or '..' in root.parts or '~' in root.parts or '\n' in str(root):
        raise Failure('remote-root must be an absolute directory or a path relative to remote HOME')
    proxy = c['proxy']
    if proxy:
        u = urlsplit(proxy)
        if (u.scheme != 'http' or u.hostname not in ('localhost', '127.0.0.1', '::1')
                or not u.port or u.username or u.password or u.path not in ('', '/')
                or u.query or u.fragment):
            raise Failure('Proxy must be a loopback HTTP URL with a port and no credentials')
    product(c['app'], c['editor'])


def configure(args):
    c = {key: getattr(args, key) for key in
         ('editor', 'host', 'app', 'layout', 'transport', 'remote_root', 'proxy')}
    c['app'] = str(Path(c['app']).expanduser().absolute())
    c['remote_root'] = c['remote_root'] or ('.cursor-server' if c['editor'] == 'cursor' else '.vscode-server')
    validate_config(c)
    if args.state_dir:
        state = Path(args.state_dir).expanduser().absolute()
    else:
        base = (Path.home() / 'Library/Application Support' if sys.platform == 'darwin'
                else Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))))
        key = hashlib.sha256((c['editor'] + '\0' + c['host']).encode()).hexdigest()[:16]
        state = base / 'cursor-ssh-updatefix' / key
    state = state.expanduser().resolve()
    source = Path(__file__).resolve()
    if source.parents[1] == state or source.parents[1] in state.parents:
        raise Failure('Runtime state must be outside the shareable skill folder')
    helper, config, hook = state / 'prepare_server.py', state / 'config.json', state / 'preconnect.sh'
    body = '#!/bin/sh\nexec %s %s prepare --config %s\n' % tuple(
        shlex.quote(str(p)) for p in (Path(sys.executable).absolute(), helper, config))
    files = {helper: source.read_bytes(),
             config: (json.dumps(c, indent=2) + '\n').encode(), hook: body.encode()}
    for path, content in files.items():
        if path.is_symlink():
            raise Failure('Runtime files must not be symlinks')
        if path.exists() and path.read_bytes() != content:
            raise Failure('Existing runtime configuration differs; review it instead of overwriting')
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path, content in files.items():
        if not path.exists():
            with path.open('xb') as stream:
                stream.write(content)
        path.chmod(0o600)
    hook.chmod(0o700)
    print(json.dumps({'remote.SSH.preconnect': {c['host']: str(hook)}}, indent=2))
    print('Merge this host entry into LOCAL user settings; do not replace existing entries.')
    print('Remote Server data directory: ' + c['remote_root'] +
          ('' if c['remote_root'].startswith('/') else ' (relative to remote HOME)'))
    print('Match remote.SSH.serverInstallPath for this host to this directory using the installed '
          'extension and connection log; some installers append the product directory name.')
    print('Runtime configuration, cache and log directory: ' + str(state))


def ssh(c, mode, script=None, stream=None, timeout=60):
    command = ['bash -s'] if mode == 'normal' else ['bash', '--login', '-c', 'bash']
    return run(['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
                '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
                '-o', 'ClearAllForwardings=yes', '-o', 'ForwardAgent=no',
                c['host']] + command,
               data=script.encode() if script is not None else None, stream=stream, timeout=timeout)


def probe(c):
    modes = ('normal', 'bash-stdin') if c['transport'] == 'auto' else ('bash-stdin',)
    script = "printf 'SSHUF_PLATFORM=%s/%s\\n' \"$(uname -s)\" \"$(uname -m)\"\n"
    for mode in modes:
        # Authentication/network errors must not silently turn into transport retries.
        out = ssh(c, mode, script)
        for line in out.splitlines():
            if line.startswith('SSHUF_PLATFORM='):
                platform = line.split('=', 1)[1]
                arch = {'Linux/x86_64': 'x64', 'Linux/aarch64': 'arm64'}.get(platform)
                if not arch:
                    raise Failure('Only Linux x64 / ARM64 remotes are supported')
                return mode, arch
    raise Failure('SSH returned no execution marker; verify the platform shell entry')


def artifacts(c, p, arch):
    if c['editor'] == 'cursor':
        url = 'https://downloads.cursor.com/production/%s/linux/%s/cursor-reh-linux-%s.tar.gz' % (p['download'], arch, arch)
        target = 'bin/linux-%s/%s' % (arch, p['commit'])
    else:
        url = 'https://update.code.visualstudio.com/commit:%s/server-linux-%s/stable' % (p['commit'], arch)
        target = ('cli/servers/Stable-%s/server' if c['layout'] == 'vscode-cli' else 'bin/%s') % p['commit']
    result = [{'kind': 'server', 'url': url, 'target': target}]
    if c['layout'] == 'vscode-cli':
        result.append({'kind': 'cli', 'url': 'https://update.code.visualstudio.com/commit:%s/cli-alpine-%s/stable' % (p['commit'], arch),
                       'target': 'code-' + p['commit']})
    return result


def remote_root_expression(c):
    root = c['remote_root']
    return shlex.quote(root) if root.startswith('/') else '"$HOME"/' + shlex.quote(root)


def prelude(c, p, arch, item):
    entry = 'cursor-server' if c['editor'] == 'cursor' else 'code-server'
    script = 'set -euo pipefail\numask 077\nunset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY\n'
    script += '[[ "$(uname -s)/$(uname -m)" = %s ]] || exit 2\n' % shlex.quote('Linux/' + ('aarch64' if arch == 'arm64' else 'x86_64'))
    script += 'root=%s\ntarget="$root"/%s\ncommit=%s\ndownload=%s\nentry=%s\n' % (
        remote_root_expression(c), shlex.quote(item['target']), shlex.quote(p['commit']), shlex.quote(p['download']), shlex.quote(entry))
    if item['kind'] == 'cli':
        script += '''ready() {
    [[ -x "$1" ]] || return 1
    timeout 30 "$1" --version 2>/dev/null | grep -Fq -- "$commit"
}
'''
    else:
        script += '''ready() {
    local folder="$1"
    [[ -x "$folder/node" && -x "$folder/bin/$entry" && -s "$folder/out/server-main.js" && -s "$folder/product.json" ]] || return 1
    timeout 30 "$folder/node" -e 'const p=require(process.argv[1]); if (![process.argv[2],process.argv[3]].includes(p.commit) && p.realCommit!==process.argv[3]) process.exit(1)' "$folder/product.json" "$commit" "$download" >/dev/null 2>&1 || return 1
    timeout 30 "$folder/bin/$entry" --version >/dev/null 2>&1
}
'''
    return script


def ready(c, p, arch, item, mode):
    # Match the publication boundary used by install(), including the CLI bundle.
    publish = item['target']
    if c['layout'] == 'vscode-cli' and item['kind'] == 'server':
        publish = posixpath.dirname(publish)
    check = 'publish="$root"/%s\n' % shlex.quote(publish)
    output = ssh(c, mode, prelude(c, p, arch, item) + check + '''
command -v timeout >/dev/null || { echo 'Missing remote timeout utility'; exit 2; }
if ready "$target"; then
    echo SSHUF_READY
elif [[ -e "$publish" || -L "$publish" ]]; then
    echo SSHUF_INCOMPLETE
else
    echo SSHUF_MISSING
fi
''', timeout=90)
    if 'SSHUF_READY' in output.splitlines():
        return True
    if 'SSHUF_INCOMPLETE' in output.splitlines():
        raise Failure('Incomplete target already exists for %s; inspect its directory and '
                      'related processes before backing it up and moving it aside. '
                      'Existing files are preserved.' % publish)
    if 'SSHUF_MISSING' in output.splitlines():
        return False
    raise Failure('Remote readiness check returned no marker')


def validate_archive(path, p, kind, editor):
    run(['gzip', '-t', str(path)], timeout=120)
    with tarfile.open(path, 'r:gz') as archive:
        members = archive.getmembers()
        if not members or len(members) > 100000:
            raise Failure('Unexpected archive entry count')
        entries = {}
        for m in members:
            name = posixpath.normpath(m.name)
            if name == '.':
                if not m.isdir():
                    raise Failure('Invalid archive root')
                continue
            if name.startswith('/') or '..' in PurePosixPath(m.name).parts or name in entries:
                raise Failure('Unsafe or duplicate archive path')
            if not (m.isfile() or m.isdir() or m.issym() or m.islnk()):
                raise Failure('Unsupported archive entry')
            entries[name] = m
        links = {n for n, m in entries.items() if m.issym() or m.islnk()}
        for name, m in entries.items():
            if any(str(parent) in links for parent in PurePosixPath(name).parents):
                raise Failure('Archive writes through a link')
            if name in links:
                destination = posixpath.normpath(posixpath.join(posixpath.dirname(name), m.linkname) if m.issym() else m.linkname)
                if m.linkname.startswith('/') or destination.startswith('../') or destination not in entries or destination in links:
                    raise Failure('Archive link escapes or has an unsupported target')
        total = sum(m.size for m in members)
        if total > 8 * 1024**3:
            raise Failure('Unexpected archive size')
        if kind == 'cli':
            if set(entries) != {'code'} or not entries['code'].isfile() or not entries['code'].mode & 0o111:
                raise Failure('Unknown CLI archive layout')
            return {'strip': 0, 'unpacked': total}
        roots = {PurePosixPath(n).parts[0] for n in entries}
        if len(roots) != 1:
            raise Failure('Unknown Server archive layout')
        prefix = next(iter(roots)) + '/'
        entry = 'cursor-server' if editor == 'cursor' else 'code-server'
        for name in ('node', 'bin/' + entry, 'out/server-main.js', 'product.json'):
            m = entries.get(prefix + name)
            if m is None or not m.isfile() or m.size == 0:
                raise Failure('Missing Server component: ' + name)
            if name in ('node', 'bin/' + entry) and not m.mode & 0o111:
                raise Failure('Server component is not executable')
        q = json.load(archive.extractfile(entries[prefix + 'product.json']))
        if q.get('commit') not in (p['commit'], p['download']) and q.get('realCommit') != p['download']:
            raise Failure('Archive version does not match the application')
        # All links must remain within the directory that survives strip-components.
        for name in links:
            m = entries[name]
            dest = posixpath.normpath(posixpath.join(posixpath.dirname(name), m.linkname) if m.issym() else m.linkname)
            if not dest.startswith(prefix):
                raise Failure('Link escapes the extracted Server root')
        return {'strip': 1, 'unpacked': total}


def get_archive(c, p, arch, item, cache):
    path = cache / ('%s-%s-linux-%s-%s.tar.gz' % (c['editor'], p['download'], arch, item['kind']))
    meta = path.with_suffix('.json')
    if path.exists():
        actual = digest(path)
        if not meta.exists() or json.loads(meta.read_text()).get('sha256') != actual:
            raise Failure('Cache checksum mismatch or missing metadata; move this cache pair aside and retry')
        info = validate_archive(path, p, item['kind'], c['editor'])
        log('CACHE_REUSED ' + item['kind'])
        return path, actual, info
    partial = path.with_suffix('.part')
    log('DOWNLOADING ' + item['kind'])
    run(['curl', '-q', '--fail', '--location', '--silent', '--show-error', '--proto', '=https',
         '--proto-redir', '=https', '--proxy', c['proxy'] or '', '--noproxy', '' if c['proxy'] else '*',
         '--connect-timeout', '15', '--max-time', '600', '--retry', '2', '--retry-max-time', '900',
         '--continue-at', '-', '--output', str(partial), item['url']], timeout=1200)
    try:
        info = validate_archive(partial, p, item['kind'], c['editor'])
    except (Failure, tarfile.TarError, ValueError):
        partial.unlink(missing_ok=True)
        raise
    actual = digest(partial)
    partial.replace(path)
    meta.write_text(json.dumps({'sha256': actual, 'commit': p['commit'], 'bytes': path.stat().st_size}) + '\n')
    return path, actual, info


def required_space_kb(packages):
    # Keep all published components, plus the largest package and its heredoc.
    unpacked = 0
    largest_upload = 0
    for path, _checksum, info in packages:
        size = path.stat().st_size
        encoded = 4 * ((size + 2) // 3) + (size + 56) // 57
        unpacked += info['unpacked']
        largest_upload = max(largest_upload, size + encoded)
    return (unpacked + largest_upload + 256 * 1024**2 + 1023) // 1024


def space_check_script(needed_kb):
    return 'needed_kb=%d\n' % needed_kb + r'''
check_dir="$root"
available_kb=unknown
space_fail() {
    printf 'Space check failed: %s; root=%s; checked=%s; available_kib=%s; required_kib=%s. Choose a writable directory with enough space; no automatic cleanup or HOME fallback.\n' \
        "$1" "$root" "$check_dir" "$available_kb" "$needed_kb" >&2
    exit 4
}
while [[ ! -e "$check_dir" ]]; do
    [[ ! -L "$check_dir" ]] || space_fail 'Dangling directory symlink'
    parent=$(dirname -- "$check_dir")
    [[ "$parent" != "$check_dir" ]] || space_fail 'No existing parent directory'
    check_dir="$parent"
done
[[ -d "$check_dir" && -w "$check_dir" && -x "$check_dir" ]] || space_fail 'Directory is not writable/searchable'
df_output=$(LC_ALL=C df -Pk "$check_dir") || space_fail "df failed (exit $?)"
available_kb=$(printf '%s\n' "$df_output" | awk 'END {print $4}')
[[ "$available_kb" =~ ^[0-9]{1,18}$ ]] || space_fail 'Invalid free-space value from df'
available_kb=$((10#$available_kb))
[[ "$available_kb" -ge "$needed_kb" ]] || space_fail 'Insufficient disk space'
printf 'SPACE root=%s; checked=%s; available_kib=%s; required_kib=%s\n' \
    "$root" "$check_dir" "$available_kb" "$needed_kb"
'''


def check_space(c, mode, packages):
    # A short, read-only request surfaces errors before sending the large stdin stream.
    script = 'set -euo pipefail\nroot=%s\n' % remote_root_expression(c)
    script += space_check_script(required_space_kb(packages)) + 'echo SSHUF_SPACE_OK\n'
    output = ssh(c, mode, script)
    if 'SSHUF_SPACE_OK' not in output.splitlines():
        raise Failure('Remote space check returned no marker')
    for line in output.splitlines():
        if line.startswith('SPACE '):
            log(line)


def install(c, p, arch, item, mode, path, checksum, info):
    # Stage on the destination filesystem. Never replace an existing incomplete target.
    head = prelude(c, p, arch, item) + '''
for tool in flock sha256sum base64 tar timeout; do command -v "$tool" >/dev/null || { echo "Missing $tool"; exit 2; }; done
mkdir -p "$root"
exec 9>"$root/.cursor-ssh-updatefix.lock"
flock -w 120 9 || { echo 'Another preparation is running'; exit 3; }
stage=""
native_target=""
native_lock=""
cleanup() {
    [[ -z "$stage" ]] || rm -rf -- "$stage"
    if [[ -n "$native_target" ]]; then
        if [[ "$native_target" -ef "$native_lock" ]]; then rm -f -- "$native_lock"; fi
        rm -f -- "$native_target"
    fi
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM
'''
    if c['editor'] == 'cursor':
        head += '''
lock_hash=$(printf '%s\\n' "$root" | md5sum | awk '{print $1}')
lock_parent="${XDG_RUNTIME_DIR:-/tmp}"
[[ -d "$lock_parent" && -w "$lock_parent" ]] || lock_parent=/tmp
native_lock="$lock_parent/cursor-remote-lock.$lock_hash"
native_target=$(mktemp "$native_lock.target.sshuf.XXXXXXXX") || { echo "Cannot create Cursor install lock in $lock_parent; check its permissions and free space" >&2; exit 3; }
printf 'owner_pid=%s\\nheartbeat=%s\\nnonce=sshuf-%s\\n' "$$" "$(date +%s)" "$$" > "$native_target" || { echo "Cannot write Cursor install lock in $lock_parent; check its permissions and free space" >&2; exit 3; }
ln "$native_target" "$native_lock" 2>/dev/null || { echo 'Cursor install lock exists; finish or cancel the other installation'; exit 3; }
'''
    elif c['layout'] == 'vscode-cli' and item['kind'] == 'server':
        head += '''
mkdir -p "$root/cli/servers/.locks"
exec 8>"$root/cli/servers/.locks/Stable-$commit"
flock -w 120 8 || { echo 'VS Code Server installation is busy'; exit 3; }
'''
    head += space_check_script(required_space_kb([(path, checksum, info)]))
    head += '''stage=$(mktemp -d "$root/.sshuf-stage.XXXXXXXX")
mkdir -m 700 "$stage/tmp"
export TMPDIR="$stage/tmp"
base64 -d > "$stage/archive.tar.gz" <<'SSHUF_ARCHIVE_END'
'''
    tail = "SSHUF_ARCHIVE_END\n[[ $(sha256sum \"$stage/archive.tar.gz\" | awk '{print $1}') = %s ]] || { echo 'Upload checksum mismatch'; exit 5; }\n" % shlex.quote(checksum)
    tail += 'mkdir "$stage/unpacked"\ntar -xzf "$stage/archive.tar.gz" -C "$stage/unpacked" --strip-components=%d --no-same-owner\n' % info['strip']
    staged = '"$stage/unpacked/code"' if item['kind'] == 'cli' else '"$stage/unpacked"'
    tail += 'ready %s || { echo "Runtime or version check failed"; exit 6; }\n' % staged
    # CLI considers the whole Stable-<commit> directory a cache entry; publish it atomically.
    tail += 'publish="$target"\n'
    if c['layout'] == 'vscode-cli' and item['kind'] == 'server':
        tail += 'publish="$(dirname "$target")"\nmkdir "$stage/bundle"\nmv "$stage/unpacked" "$stage/bundle/server"\n'
        staged = '"$stage/bundle"'
    tail += '''if ! ready "$target"; then
    [[ ! -e "$publish" && ! -L "$publish" ]] || { echo 'Incomplete target already exists; inspect it before moving it aside'; exit 7; }
    mkdir -p "$(dirname "$publish")"
'''
    tail += '    mv -Tn -- %s "$publish"\nfi\n' % staged
    tail += 'ready "$target" || { echo "Post-install check failed"; exit 8; }\necho SSHUF_INSTALLED\n'
    with tempfile.TemporaryFile() as stream:
        stream.write(head.encode())
        with path.open('rb') as source:
            for chunk in iter(lambda: source.read(57 * 16384), b''):
                stream.write(base64.encodebytes(chunk))
        stream.write(tail.encode())
        stream.seek(0)
        result = ssh(c, mode, stream=stream, timeout=900)
    if 'SSHUF_INSTALLED' not in result.splitlines():
        raise Failure('No installation completion marker')
    log('INSTALLED ' + item['kind'])


def prepare(args):
    global LOG
    config = Path(args.config).expanduser().absolute()
    c = json.loads(config.read_text())
    validate_config(c)
    state = config.parent
    LOG = state / 'prepare.log'
    cache = state / 'cache'
    cache.mkdir(mode=0o700, exist_ok=True)
    with (state / 'prepare.lock').open('a') as lock:
        deadline = time.monotonic() + 1200
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise Failure('Another preparation did not finish in time')
                time.sleep(1)
        p = product(c['app'], c['editor'])
        mode, arch = probe(c)
        log('CHECK %s %s linux-%s' % (c['editor'], p['commit'], arch))
        items = artifacts(c, p, arch)
        missing = [item for item in items if not ready(c, p, arch, item, mode)]
        if not missing:
            log('READY: download and upload skipped')
            return 0
        if args.check_only:
            log('MISSING: no remote changes made')
            return 10
        # Validate every archive before publishing any component.
        packages = [(item, get_archive(c, p, arch, item, cache)) for item in missing]
        check_space(c, mode, [package for _item, package in packages])
        for item, package in packages:
            install(c, p, arch, item, mode, *package)
        if not all(ready(c, p, arch, item, mode) for item in items):
            raise Failure('Remote verification failed after installation')
        log('READY: editor may continue connecting')
        return 0


def main():
    if sys.platform not in ('darwin', 'linux'):
        raise Failure('Only macOS / Linux clients are supported')
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('configure', help='Generate private runtime files; does not edit editor settings')
    init.add_argument('--editor', required=True, choices=('cursor', 'vscode'))
    init.add_argument('--host', required=True)
    init.add_argument('--app', required=True, help='Stable .app or resources/app directory, not a version-specific directory')
    init.add_argument('--layout', required=True, choices=('cursor', 'vscode-cli', 'vscode-legacy'))
    init.add_argument('--transport', default='auto', choices=('auto', 'bash-stdin'))
    init.add_argument('--remote-root', help='Server data directory from Remote-SSH logs; default is the product default')
    init.add_argument('--proxy', default='', help='Optional loopback HTTP proxy URL; no credentials')
    init.add_argument('--state-dir', help='Optional private runtime directory outside the skill folder')
    task = commands.add_parser('prepare', help='Prepare the current application build on the configured host')
    task.add_argument('--config', required=True)
    task.add_argument('--check-only', action='store_true', help='Read-only remote check; exit 10 means missing')
    args = parser.parse_args()
    for name in ('ssh', 'curl', 'gzip'):
        if not shutil.which(name):
            raise Failure('Missing local dependency: ' + name)
    return configure(args) if args.command == 'configure' else prepare(args)


if __name__ == '__main__':
    signal.signal(signal.SIGINT, cancelled)
    signal.signal(signal.SIGTERM, cancelled)
    try:
        sys.exit(main() or 0)
    except (Failure, OSError, ValueError, tarfile.TarError) as exc:
        log('ERROR: ' + str(exc))
        sys.exit(1)
