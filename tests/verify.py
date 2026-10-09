"""Host-side destructive test of ONLY the torrent-stream project, with no user sessions."""
import json
import os
import shlex
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
URL = os.getenv('PUBLIC_URL', 'http://localhost:8787')
COMPOSE = 'docker compose -f compose.yml -f tests/compose.test.yml --profile test '


def docker(command, env=''):
    print('\nRUN', env + ' ' + command, flush=True)
    subprocess.run(['sg', 'docker', '-c', env + ' ' + command], cwd=ROOT, check=True)


def test(mode):
    docker(COMPOSE + 'exec -T web python -m tests.e2e ' + mode)


def wait():
    for _ in range(90):
        try:
            with urllib.request.urlopen(URL + '/health', timeout=3) as r:
                if r.status == 200:
                    return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError('Health timeout')


if __name__ == '__main__':
    with urllib.request.urlopen(URL + '/api/status') as response:
        if json.load(response):
            raise SystemExit('Refusing: active sessions exist. End only your test sessions first.')
    try:
        docker(COMPOSE + 'up -d --build --wait web test-seeder')
        docker(COMPOSE + 'exec -T web python -m unittest tests.test_unit -v')
        test('basic')
        test('isolation')
        test('create')
        docker(COMPOSE + 'kill -s SIGKILL web')
        docker(COMPOSE + 'up -d --wait web')
        test('empty')
        test('create')
        docker(COMPOSE + 'restart torrserver')
        time.sleep(10)
        wait()
        test('empty')
        docker(COMPOSE + 'up -d --wait --force-recreate --no-deps web', 'GRACE_SECONDS=6 TTL_SECONDS=60 SWEEP_SECONDS=1')
        test('grace')
        docker(COMPOSE + 'up -d --wait --force-recreate --no-deps web', 'TTL_SECONDS=12 SWEEP_SECONDS=1')
        test('ttl')
        test('ttl_active')
        docker(COMPOSE + 'up -d --wait --force-recreate --no-deps web', 'DISK_LIMIT_BYTES=1048576 SWEEP_SECONDS=1')
        test('disk')
    finally:
        docker(COMPOSE + 'up -d --wait --force-recreate --no-deps web')
        docker(COMPOSE + 'rm -sf test-seeder')
    wait()
    print('\nPASS all integration tests; default production policy restored; test seeder removed')
