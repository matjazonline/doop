#!/usr/bin/env python3
"""Run trusted repository tests with isolated, recoverable Docker fixtures."""
import fcntl
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
LABEL = 'io.doop.test-linux'
ATTACH_TIMEOUTS = {'probe': 30, 'rescue': 120}
POSTGRES = 'postgres:18.6-alpine@sha256:123b17998ccaf47e53b502e8c6a1e79afd8f579c6f3eec83c563da026f957395'
RESCUE = '''import pathlib, subprocess, sys
subprocess.run(['docker', 'info'], check=True, stdout=subprocess.DEVNULL)
for state in pathlib.Path(sys.argv[1]).glob('doop-private-storage-*/runtime/state.json'):
    subprocess.run(['python3', '/work/scripts/private-storage-fixture.py', 'stop',
                    '--runtime-dir', str(state.parent)], check=True)
'''


def clean_environment():
    env = os.environ.copy()
    for key in ['DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH']:
        env.pop(key, None)
    return env


def protected(path, directory=False):
    info = path.lstat()
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('state must be owned by this user and protected from other users')


class Fixtures:
    def __init__(self, recovery=None):
        self.cli = shutil.which('docker')
        if not self.cli:
            raise RuntimeError('Docker CLI is required')
        self.client = None
        self.lock = None
        if recovery:
            self.path = Path(recovery).absolute()
            protected(self.path.parent, directory=True)
            protected(self.path)
        else:
            self.path = Path(tempfile.mkdtemp(prefix='doop-linux-')) / 'state.json'
        descriptor = os.open(self.path.parent / 'owner.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        self.lock = os.fdopen(descriptor, 'a+')
        protected(self.path.parent / 'owner.lock')
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            raise RuntimeError('this state has a live owner; recovery refused') from None
        if recovery:
            self.state = json.loads(self.path.read_text())
            if self.state.get('version') != 1 or not re.fullmatch('[0-9a-f]{12}', self.state.get('owner', '')):
                raise RuntimeError('invalid ownership state')
        else:
            endpoint = os.environ.get('DOCKER_HOST')
            if os.environ.get('DOCKER_CONTEXT') or not endpoint:
                context = os.environ.get('DOCKER_CONTEXT')
                if not context:
                    context = subprocess.check_output([self.cli, 'context', 'show'], text=True).strip()
                endpoint = subprocess.check_output([self.cli, 'context', 'inspect', context,
                                                    '--format', '{{.Endpoints.docker.Host}}'], text=True).strip()
            owner = secrets.token_hex(6)
            self.state = {'version': 1, 'owner': owner, 'endpoint': endpoint,
                          'socket': os.environ.get('DOOP_TEST_DOCKER_SOCKET', '/var/run/docker.sock'),
                          'image': {'name': 'doop-test-linux:' + owner, 'phase': 'not_started'},
                          'resources': {}, 'complete': False}
            self.validate_endpoint()
            self.state['daemon'] = self.raw('info', '--format', '{{.ID}}')
            if not self.state['daemon']:
                raise RuntimeError('Docker daemon identity is required')
            self.save()
        self.validate_endpoint()
        print('Linux test recovery state: ' + str(self.path), flush=True)

    def validate_endpoint(self):
        if not self.state['endpoint'].startswith('unix:///') or not self.state['socket'].startswith('/'):
            raise RuntimeError('only a local Unix Docker endpoint/socket is supported')
        if any(c in self.state['socket'] for c in ',\n\r'):
            raise RuntimeError('invalid daemon socket path')

    def save(self):
        with tempfile.NamedTemporaryFile(mode='w', dir=self.path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(self.state, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        descriptor = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def raw(self, *args, timeout=30, creator=False, env=None, visible=False):
        result = subprocess.run([self.cli, '--host', self.state['endpoint'], *args],
                                env=env if env is not None else clean_environment(), text=True,
                                stdout=None if visible else subprocess.PIPE, stderr=None if visible else subprocess.PIPE,
                                timeout=timeout, pass_fds=(self.lock.fileno(),) if creator else ())
        if result.returncode:
            raise RuntimeError('Docker operation failed: ' + args[0])
        return '' if visible else result.stdout.strip()

    def guard(self):
        if self.raw('info', '--format', '{{.ID}}') != self.state['daemon']:
            raise RuntimeError('Docker authority changed; recovery state retained')

    def docker(self, *args, **kwargs):
        self.guard()
        return self.raw(*args, **kwargs)

    def observed(self, key):
        item = self.state['resources'][key]
        kind, name = item['kind'], item['name']
        if kind == 'container':
            candidates = self.docker('ps', '--all', '--quiet', '--no-trunc', '--filter', 'name=^/' + name + '$').splitlines()
        else:
            candidates = [value for value in self.docker('volume', 'ls', '--quiet', '--filter', 'name=' + name).splitlines() if value == name]
        if not candidates:
            return None
        if len(candidates) != 1:
            raise RuntimeError('ambiguous resource ownership')
        value = json.loads(self.docker(kind, 'inspect', candidates[0]))[0]
        labels = value.get('Config', {}).get('Labels', {}) if kind == 'container' else value.get('Labels', {})
        identity = value['Id'] if kind == 'container' else value['Name']
        actual_name = value['Name'].removeprefix('/')
        if labels.get(LABEL) != self.state['owner'] or actual_name != name or (item.get('id') and item['id'] != identity):
            raise RuntimeError('resource ownership mismatch; recovery state retained')
        return value

    def create(self, key, kind, args=(), env=None):
        item = {'name': 'dtl-' + self.state['owner'] + '-' + key, 'kind': kind, 'phase': 'pending'}
        self.state['resources'][key] = item
        self.save()
        options = ['--label', LABEL + '=' + self.state['owner']]
        if kind == 'container':
            options += ['--name', item['name']]
        self.docker(kind, 'create', *options, *args, *([item['name']] if kind == 'volume' else []),
                    env=env, creator=True)
        value = self.observed(key)
        if value is None:
            raise RuntimeError('creation outcome unconfirmed; recovery state retained')
        item['id'] = value['Id'] if kind == 'container' else value['Name']
        if key == 'workspace':
            mountpoint = value['Mountpoint']
            if not mountpoint.startswith('/') or any(c in mountpoint for c in ',\n\r'):
                raise RuntimeError('unsupported Docker volume mountpoint')
            self.state['mountpoint'] = mountpoint
        item['phase'] = 'created'
        self.save()
        return item['id']

    def remove(self, key):
        item = self.state['resources'].get(key)
        if not item or item['phase'] == 'removed':
            return
        value = self.observed(key)
        if value is None and item['phase'] == 'pending':
            raise RuntimeError('pending creation outcome unconfirmed; recovery state retained')
        if value is not None:
            identity = value['Id'] if item['kind'] == 'container' else value['Name']
            self.docker(item['kind'], 'rm', *(['--force'] if item['kind'] == 'container' else []), identity)
        if self.observed(key) is not None:
            raise RuntimeError('resource removal unconfirmed')
        item['phase'] = 'removed'
        self.save()

    def image(self):
        item = self.state['image']
        candidates = self.docker('image', 'ls', '--quiet', '--no-trunc', '--filter', 'reference=' + item['name']).splitlines()
        if not candidates:
            return None
        value = json.loads(self.docker('image', 'inspect', item['name']))[0]
        if value['Config'].get('Labels', {}).get(LABEL) != self.state['owner'] or (item.get('id') and item['id'] != value['Id']):
            raise RuntimeError('image ownership mismatch')
        return value['Id']

    def shared_args(self):
        return ['--network=host', '--env', 'DOOP_TEST_DAEMON_ID=' + self.state['daemon'],
                '--mount', 'type=bind,src=' + self.state['socket'] + ',dst=/var/run/docker.sock,readonly',
                '--mount', 'type=volume,src=' + self.state['resources']['workspace']['name'] + ',dst=' + self.state['mountpoint']]

    def stop_client(self):
        if self.client:
            if self.client.poll() is None:
                self.client.terminate()
            try:
                self.client.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.client.kill()
                self.client.wait(timeout=5)
            self.client = None

    def attach(self, key):
        value = self.observed(key)
        if value is None:
            raise RuntimeError('known container is missing')
        self.guard()
        self.client = subprocess.Popen([self.cli, '--host', self.state['endpoint'], 'start', '--attach', value['Id']],
                                       env=clean_environment())
        try:
            code = self.client.wait(timeout=ATTACH_TIMEOUTS.get(key))
        except subprocess.TimeoutExpired:
            reason = key + ' attachment exceeded its deadline'
            try:
                self.stop_client()
                self.remove(key)
            except Exception:
                raise RuntimeError(reason + '; container cleanup unconfirmed; recovery state retained') from None
            raise RuntimeError(reason + '; owned container removal confirmed') from None
        self.client = None
        return code

    def run(self, args):
        item = self.state['image']
        item['phase'] = 'pending'
        self.save()
        self.docker('build', '--file', 'tests/linux.Dockerfile', '--tag', item['name'],
                    '--label', LABEL + '=' + self.state['owner'], str(ROOT), timeout=1200, creator=True, visible=True)
        item['id'] = self.image()
        if not item['id']:
            raise RuntimeError('built image is missing')
        item['phase'] = 'created'
        self.save()
        self.create('workspace', 'volume')
        self.create('probe', 'container', [*self.shared_args(), '--entrypoint', 'docker', item['id'], 'info'])
        if self.attach('probe'):
            raise RuntimeError('runner socket authority probe failed')
        self.remove('probe')
        # Pull before durable create intent: a slow registry cannot leave a
        # container create client running after its bounded response window.
        self.docker('pull', POSTGRES, timeout=120, visible=True)
        self.create('pgdata', 'volume')
        password = secrets.token_hex(24)
        env = clean_environment()
        env['POSTGRES_PASSWORD'] = password
        pg = self.create('postgres', 'container', ['--env', 'POSTGRES_PASSWORD', '--publish', '127.0.0.1::5432',
                         '--mount', 'type=volume,src=' + self.state['resources']['pgdata']['name'] + ',dst=/var/lib/postgresql', POSTGRES], env=env)
        self.docker('start', pg)
        deadline = time.monotonic() + 30
        while True:
            try:
                self.docker('exec', pg, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres')
                break
            except RuntimeError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('disposable PostgreSQL did not become ready') from None
                time.sleep(0.1)
        ports = json.loads(self.docker('inspect', '--format', '{{json (index .NetworkSettings.Ports "5432/tcp")}}', pg))
        if len(ports) != 1 or ports[0]['HostIp'] != '127.0.0.1':
            raise RuntimeError('PostgreSQL must publish only on daemon loopback')
        self.state['postgres_port'] = ports[0]['HostPort']
        self.save()
        env = clean_environment()
        env['DOOP_TEST_DATABASE_URL'] = 'postgresql://postgres:' + password + '@127.0.0.1:' + ports[0]['HostPort'] + '/postgres'
        self.create('runner', 'container', [*self.shared_args(), '--cpus=2', '--memory=7g', '--shm-size=1g',
                    '--env', 'DOOP_TEST_DATABASE_URL', '--env', 'TMPDIR=/tmp/doop-test-linux',
                    '--env', 'DOOP_TEST_WORKSPACE=' + self.state['mountpoint'] + '/repo', item['id'], *args], env=env)
        return self.attach('runner')

    def cleanup(self):
        self.guard()
        if self.state['complete']:
            return
        # Fence every test producer before looking at its Garage ownership files.
        self.remove('runner')
        self.stop_client()
        self.remove('probe')
        self.remove('rescue')
        workspace = self.state['resources'].get('workspace')
        if workspace and workspace['phase'] != 'removed':
            value = self.observed('workspace')
            if value is None:
                if workspace['phase'] == 'pending':
                    raise RuntimeError('workspace creation outcome unconfirmed')
            else:
                self.state['mountpoint'] = value['Mountpoint']
                image = self.image()
                if image is None:
                    raise RuntimeError('recovery image missing; workspace retained')
                self.create('rescue', 'container', [*self.shared_args(), '--entrypoint', 'python3', image,
                            '-c', RESCUE, self.state['mountpoint']])
                if self.attach('rescue'):
                    self.remove('rescue')
                    raise RuntimeError('private storage cleanup uncertain; workspace retained')
                self.remove('rescue')
                # An unrecognized surviving binder is never ours to delete.
                for container in self.docker('ps', '--all', '--quiet', '--no-trunc').splitlines():
                    mounts = json.loads(self.docker('inspect', '--format', '{{json .Mounts}}', container))
                    if any(mount['Source'] == self.state['mountpoint'] or mount['Source'].startswith(self.state['mountpoint'] + '/') for mount in mounts):
                        raise RuntimeError('workspace still has a container mount; recovery retained')
        self.remove('postgres')
        self.remove('pgdata')
        self.remove('workspace')
        item = self.state['image']
        if item['phase'] not in ['not_started', 'removed']:
            image = self.image()
            if image is None and item['phase'] == 'pending':
                raise RuntimeError('image creation outcome unconfirmed; recovery state retained')
            if image:
                self.docker('image', 'rm', item['name'])
            if self.image() is not None:
                raise RuntimeError('image removal unconfirmed')
            item['phase'] = 'removed'
        self.state['complete'] = True
        self.save()
        # Keep a tiny nonsecret completion receipt and the stable lock inode:
        # unlinking a lock would let a waiter and a new opener own different locks.
        print('Linux test fixtures removed; completion receipt: ' + str(self.path), flush=True)

    def close(self):
        if self.lock:
            self.lock.close()


def interrupted(_signal, _frame):
    raise InterruptedError('Linux test interrupted')


def main():
    fixture = None
    code = 1
    args = sys.argv[1:]
    if args[:1] == ['--cleanup'] and len(args) != 2:
        print('usage: test-linux.sh --cleanup /path/to/state.json', file=sys.stderr)
        return 1
    signal.signal(signal.SIGINT, interrupted)
    signal.signal(signal.SIGTERM, interrupted)
    try:
        recovery = args[1] if args[:1] == ['--cleanup'] else None
        fixture = Fixtures(recovery)
        code = 0 if recovery else fixture.run(args)
    except (Exception, KeyboardInterrupt) as error:
        # Never print subprocess environments, inspect data, or credentials.
        print('Linux test run failed: ' + (str(error) if isinstance(error, (RuntimeError, InterruptedError)) else type(error).__name__), file=sys.stderr)
    finally:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        if fixture:
            try:
                fixture.cleanup()
            except Exception as error:
                code = 1
                reason = str(error) if isinstance(error, RuntimeError) else type(error).__name__
                print('Linux test cleanup uncertain; retain ' + str(fixture.path) + ' (' + reason + ')', file=sys.stderr)
            finally:
                fixture.close()
    return code


if __name__ == '__main__':
    sys.exit(main())
