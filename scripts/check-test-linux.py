#!/usr/bin/env python3
"""Actual Docker lifecycle regression checks for the trusted Linux test helper.

Run with Python3 and the same local Docker authority as test-linux.sh. No mocks:
two competing runners each create PostgreSQL, a workspace and a Garage fixture.
"""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

SPEC = importlib.util.spec_from_file_location('test_linux', Path(__file__).with_name('test-linux.py'))
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
HOLD = '''import os, pathlib, secrets, subprocess, time
root = pathlib.Path(os.environ['DOOP_TEST_WORKSPACE']).parent
subprocess.run(['docker', 'info'], check=True, stdout=subprocess.DEVNULL)
subprocess.run(['node', '--input-type=module', '-e',
    "import pg from 'pg'; const c=new pg.Client({connectionString:process.env.DOOP_TEST_DATABASE_URL}); await c.connect(); if((await c.query('select 1 as ok')).rows[0].ok!==1)process.exit(1); await c.end()"], cwd='/work', check=True)
parent = root / ('doop-private-storage-' + secrets.token_hex(6))
parent.mkdir(mode=0o700)
subprocess.run(['python3', '/work/scripts/private-storage-fixture.py', 'start',
                '--runtime-dir', str(parent / 'runtime')], check=True, stdout=subprocess.DEVNULL)
(root / 'lifecycle-ready').touch()
while True: time.sleep(1)
'''
STALL_KEY = None


class HoldingFixtures(MODULE.Fixtures):
    def create(self, key, kind, args=(), env=None):
        if key == 'runner':
            args = list(args)
            image = self.state['image']['id']
            index = args.index(image)
            args = args[:index] + ['--entrypoint', 'python3', image, '-c', HOLD]
        return super().create(key, kind, args, env)


class StallingFixtures(HoldingFixtures):
    def create(self, key, kind, args=(), env=None):
        if key == STALL_KEY:
            args = list(args)
            index = args.index(self.state['image']['id'])
            prefix = args[:index]
            entrypoint = prefix.index('--entrypoint')
            del prefix[entrypoint:entrypoint + 2]
            args = prefix + ['--entrypoint', 'python3', self.state['image']['id'],
                             '-c', 'import time; time.sleep(3600)']
        return super().create(key, kind, args, env)


def worker(stall=None):
    global STALL_KEY, HOLD
    # Keep the actual entrypoint's signal handling, cleanup and exit behavior.
    STALL_KEY = stall
    if stall:
        # Shorten only the regression's observation interval, never suite limits.
        MODULE.ATTACH_TIMEOUTS = {'probe': 2, 'rescue': 2}
        if stall == 'rescue':
            HOLD = HOLD.replace('while True: time.sleep(1)', '')
    MODULE.Fixtures = StallingFixtures if stall else HoldingFixtures
    sys.argv = [str(Path(__file__).with_name('test-linux.py'))]
    sys.exit(MODULE.main())


def wait_for(action, seconds=1200):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = action()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError('bounded lifecycle check timed out')


def cli(state, *args, check=True):
    return subprocess.run(['docker', '--host', state['endpoint'], *args], env=MODULE.clean_environment(),
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check)


def state_path(log):
    for line in log.read_text().splitlines():
        if line.startswith('Linux test recovery state: '):
            return Path(line.split(': ', 1)[1])
    return None


def ready(path):
    if not path:
        return False
    state = json.loads(path.read_text())
    runner = state['resources'].get('runner', {})
    return runner.get('phase') == 'created' and cli(state, 'exec', runner['id'], 'test', '-e',
        state['mountpoint'] + '/lifecycle-ready', check=False).returncode == 0


def recover(path, env=None):
    return subprocess.run([sys.executable, str(Path(__file__).with_name('test-linux.py')), '--cleanup', str(path)],
                          env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)


def check():
    logs = Path(tempfile.mkdtemp(prefix='doop-linux-check-'))
    workers = []
    paths = []
    print('Lifecycle check logs: ' + str(logs), flush=True)
    try:
        for index in range(2):
            log = logs / ('worker-' + str(index) + '.log')
            with log.open('w') as stream:
                process = subprocess.Popen([sys.executable, __file__, '--worker'], stdout=stream, stderr=subprocess.STDOUT)
            workers.append(process)
            paths.append(wait_for(lambda: state_path(log)))
        def both_ready():
            if any(process.poll() is not None for process in workers):
                raise AssertionError('fixture worker exited before readiness; inspect ' + str(logs))
            return all(ready(path) for path in paths)
        wait_for(both_ready)
        states = [json.loads(path.read_text()) for path in paths]
        assert states[0]['owner'] != states[1]['owner']
        assert states[0]['postgres_port'] != states[1]['postgres_port']
        assert states[0]['mountpoint'] != states[1]['mountpoint']
        for key in ['runner', 'postgres', 'workspace', 'pgdata']:
            assert states[0]['resources'][key]['id'] != states[1]['resources'][key]['id']
        busy = recover(paths[0])
        (logs / 'live-owner-refusal.log').write_text(busy.stdout)
        assert busy.returncode != 0 and 'live owner' in busy.stdout and ready(paths[0])

        # A copied state naming the wrong authority cannot mutate live resources.
        wrong = Path(tempfile.mkdtemp(prefix='doop-linux-wrong-')) / 'state.json'
        wrong_state = {**states[0], 'daemon': 'deliberately-wrong-daemon'}
        wrong.write_text(json.dumps(wrong_state))
        wrong.chmod(0o600)
        refused = recover(wrong)
        (logs / 'wrong-daemon-refusal.log').write_text(refused.stdout)
        assert refused.returncode != 0 and not json.loads(wrong.read_text())['complete'] and ready(paths[0])

        # The actual in-image wrapper must reject a different daemon ID.
        probe = MODULE.Fixtures()
        try:
            probe.create('probe', 'container', ['--network=host', '--mount',
                'type=bind,src=' + probe.state['socket'] + ',dst=/var/run/docker.sock,readonly',
                '--env', 'DOOP_TEST_DAEMON_ID=deliberately-wrong-daemon', '--entrypoint', 'docker',
                states[0]['image']['id'], 'info'])
            assert probe.attach('probe') != 0
        finally:
            probe.cleanup()
            probe.close()

        # Deliberately stall actual owned containers, through main's setup and
        # signal-ignoring cleanup paths. The unaffected two owners stay alive.
        for key in ['probe', 'rescue']:
            log = logs / ('stalled-' + key + '.log')
            with log.open('w') as stream:
                child = subprocess.Popen([sys.executable, __file__, '--stall-' + key], stdout=stream, stderr=subprocess.STDOUT)
            workers.append(child)
            path = wait_for(lambda: state_path(log))
            paths.append(path)

            def stalled_running():
                if child.poll() is not None:
                    raise AssertionError('stalled worker exited before observation; inspect ' + str(log))
                state = json.loads(path.read_text())
                item = state['resources'].get(key, {})
                if item.get('phase') != 'created':
                    return False
                result = cli(state, 'inspect', '--format', '{{.State.Running}}', item['id'], check=False)
                return result.returncode == 0 and result.stdout.strip() == 'true'

            wait_for(stalled_running)
            if key == 'rescue':
                child.send_signal(signal.SIGTERM)
                child.send_signal(signal.SIGINT)
                time.sleep(0.1)
                assert child.poll() is None  # cleanup ignores these, deadline still works
            started = time.monotonic()
            assert child.wait(timeout=20) != 0
            state = json.loads(path.read_text())
            assert time.monotonic() - started < 20
            assert state['resources'][key]['phase'] == 'removed'
            assert cli(state, 'container', 'inspect', state['resources'][key]['id'], check=False).returncode != 0
            assert key + ' attachment exceeded its deadline' in log.read_text()
            assert all(ready(original) for original in paths[:2])
            if key == 'probe':
                assert state['complete']
            else:
                assert not state['complete']
                assert state['resources']['workspace']['phase'] == 'created'
                assert state['image']['phase'] == 'created'
                assert state['resources']['runner']['phase'] == 'removed'
                assert recover(path).returncode == 0
                assert json.loads(path.read_text())['complete']

        # Graceful interruption removes only run 0; run 1 remains productive.
        workers[0].send_signal(signal.SIGTERM)
        assert workers[0].wait(timeout=120) != 0
        assert json.loads(paths[0].read_text())['complete'] and ready(paths[1])

        # Kill the host, leaving a real runner + Garage. Two recovery claimants
        # race on its stable lock, with a deliberately broken ambient endpoint.
        workers[1].kill()
        workers[1].wait(timeout=10)
        env = {**os.environ, 'DOCKER_HOST': 'unix:///deliberately-absent.sock'}
        claims = []
        for index in range(2):
            stream = (logs / ('recovery-' + str(index) + '.log')).open('w')
            child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('test-linux.py')),
                                      '--cleanup', str(paths[1])], env=env, stdout=stream, stderr=subprocess.STDOUT)
            claims.append((child, stream))
        codes = []
        for child, stream in claims:
            codes.append(child.wait(timeout=120))
            stream.close()
        assert 0 in codes and json.loads(paths[1].read_text())['complete']
        for state in states:
            for key in ['runner', 'postgres']:
                assert cli(state, 'container', 'inspect', state['resources'][key]['id'], check=False).returncode != 0
            for key in ['workspace', 'pgdata']:
                assert cli(state, 'volume', 'inspect', state['resources'][key]['id'], check=False).returncode != 0
            assert cli(state, 'image', 'inspect', state['image']['name'], check=False).returncode != 0

        # A synthetic pending intent with no creator is intentionally retained.
        # It is never sent to Docker; absence cannot prove a real create settled.
        pending = MODULE.Fixtures()
        pending.state['resources']['runner'] = {'kind': 'container', 'name': 'dtl-' + pending.state['owner'] + '-runner', 'phase': 'pending'}
        pending.save()
        pending.close()
        refused = recover(pending.path)
        (logs / 'pending-absent-refusal.log').write_text(refused.stdout)
        assert refused.returncode != 0 and json.loads(pending.path.read_text())['resources']['runner']['phase'] == 'pending'
        print('PASS concurrent isolation, live-owner refusal, graceful/hard recovery, daemon authority, stalled setup/rescue deadlines and pending retention', flush=True)
        (logs / 'result.json').write_text(json.dumps({'status': 'PASS', 'states': [str(path) for path in paths],
            'wrong_daemon_state': str(wrong), 'synthetic_pending_state': str(pending.path), 'recovery_codes': codes}, indent=2))
    finally:
        for process in workers:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
        for path in paths:
            if not json.loads(path.read_text())['complete']:
                result = recover(path)
                print(result.stdout, end='')
                if result.returncode:
                    print('Retain unresolved recovery state: ' + str(path), file=sys.stderr)


if __name__ == '__main__':
    if sys.argv[1:] == ['--worker']:
        worker()
    elif sys.argv[1:] in [['--stall-probe'], ['--stall-rescue']]:
        worker(sys.argv[1].removeprefix('--stall-'))
    else:
        check()
