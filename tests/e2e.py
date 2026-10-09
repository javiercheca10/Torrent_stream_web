"""Run inside web container with the isolated test seeder present."""
import concurrent.futures
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import httpx

CONTROL_HEADERS = {
    'X-Internal-Token': os.environ['INTERNAL_API_TOKEN'],
    'X-Owner-Chat-Id': '999',
}
c = httpx.Client(base_url='http://127.0.0.1:8787', timeout=100, headers=CONTROL_HEADERS)
t = httpx.Client(base_url='http://torrserver:8090', timeout=20)
seed = httpx.Client(base_url='http://test-seeder:8000', timeout=30)


class Reference:
    def __len__(self):
        return int(seed.head('/fixture/main.mp4').headers['content-length'])

    def __getitem__(self, key):
        start, end, step = key.indices(len(self))
        result = bytearray()
        offset = 0
        with seed.stream('GET', '/fixture/main.mp4') as response:
            for chunk in response.iter_bytes(65536):
                lo, hi = max(0, start-offset), min(len(chunk), end-offset)
                if hi > lo:
                    result.extend(chunk[lo:hi])
                offset += len(chunk)
                if offset >= end:
                    break
        return bytes(result)


def upload(name='fixture.torrent'):
    r = c.post('/api/torrents', content=seed.get('/' + name).content,
               headers={'Content-Type': 'application/x-bittorrent'})
    assert r.status_code == 201, r.text
    return r.json()


def wait_deleted(s, timeout=20):
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if c.get('/api/sessions/' + s['id']).status_code == 404:
            assert not list(Path('/cache').iterdir()), list(Path('/cache').iterdir())
            assert not t.post('/torrents', json={'action': 'list'}).json()
            assert not t.post('/viewed', json={'action': 'list'}).json()
            assert json.loads(Path('/state/sessions.json').read_text()) == []
            print('PASS cleanup: API, disk pieces, viewed metadata, state empty', flush=True)
            return
        time.sleep(1)
    raise AssertionError('Session not deleted')


def range_check(s, start, end, reference):
    r = c.get('/api/sessions/' + s['id'] + '/play', headers={'Range': f'bytes={start}-{end}'})
    assert r.status_code == 206, (r.status_code, r.text[:200])
    assert r.headers['content-range'] == f'bytes {start}-{end}/{len(reference)}', r.headers
    assert int(r.headers['content-length']) == end-start+1
    assert r.content == reference[start:end+1], (start, end, hashlib.sha256(r.content).hexdigest())
    print('PASS Range byte-exact', start, end, flush=True)
    return r


def main(mode):
    if mode == 'empty':
    assert c.get('/api/status').json() == []
        assert not list(Path('/cache').iterdir())
        assert not t.post('/torrents', json={'action': 'list'}).json()
        assert not t.post('/viewed', json={'action': 'list'}).json()
        print('PASS reconciliation: no sessions, torrents, cache or viewed records')
        return
    s = upload()
    print('PASS upload + metadata', s['id'], s['files'], flush=True)
    base = '/api/sessions/' + s['id']
    reference = Reference()
    assert s['selected'] == max(s['files'], key=lambda f: f['length'])['id']
    if mode == 'create':
        range_check(s, 0, 65535, reference)
        orphan = Path('/cache') / ('f' * 40)
        orphan.mkdir(exist_ok=True)
        (orphan / 'piece').write_bytes(b'orphan test')
        print('PASS staged session and disk orphan for crash/restart')
        return
    if mode == 'stream':
        expected = hashlib.sha256()
        with seed.stream('GET', '/fixture/main.mp4') as response:
            for chunk in response.iter_bytes(65536):
                expected.update(chunk)
        actual = hashlib.sha256()
        count = 0
        started = time.monotonic()
        with c.stream('GET', base + '/play') as response:
            assert response.status_code == 200
            for chunk in response.iter_bytes(65536):
                count += len(chunk)
                actual.update(chunk)
        assert count == len(reference) and actual.digest() == expected.digest()
        assert c.get(base).status_code == 200
        print('PASS full main video SHA256', count, actual.hexdigest(), 'seconds', round(time.monotonic()-started, 2), flush=True)
        assert c.delete(base).status_code in (200, 202)
        wait_deleted(s)
        return
    if mode == 'disk':
        wait_deleted(s, 20)
        print('PASS disk pressure terminates and cleans session')
        return
    if mode == 'ttl_active':
        with c.stream('GET', base + '/play') as stream:
            chunks = stream.iter_bytes(65536)
            next(chunks)
            time.sleep(7)
            assert c.post(base + '/heartbeat').status_code == 200
            wait_deleted(s, 20)
        print('PASS hard TTL cancels active reader despite heartbeat')
        return
    if mode == 'ttl':
        wait_deleted(s, int(os.getenv('TTL_SECONDS', '21600')) + 15)
        print('PASS never-played TTL')
        return
    if mode == 'grace':
        range_check(s, 0, 65535, reference)
        grace = int(os.getenv('GRACE_SECONDS', '180'))
        for _ in range(3):
            time.sleep(grace / 2)
            assert c.post(base + '/heartbeat').status_code == 200
        assert c.get(base).status_code == 200
        print('PASS cooperative heartbeat keeps session beyond grace')
        wait_deleted(s, grace + 15)
        print('PASS disconnect grace', grace)
        return
    if mode == 'disconnect':
        with c.stream('GET', base + '/play') as stream:
            chunks = stream.iter_bytes(65536)
            next(chunks)
        grace = int(os.getenv('GRACE_SECONDS', '180'))
        time.sleep(grace - 10)
        assert c.get(base).status_code == 200
        wait_deleted(s, 30)
        print('PASS actual disconnect grace', grace)
        return
    if mode == 'isolation':
        other = upload('fixture2.torrent')
        r = c.post('/api/torrents', content=seed.get('/fixture3.torrent').content,
                   headers={'Content-Type': 'application/x-bittorrent'})
        assert r.status_code == 503
        range_check(other, 0, 65535, reference)
        with c.stream('GET', base + '/play') as first, c.stream('GET', base + '/play') as second:
            one, two = first.iter_bytes(65536), second.iter_bytes(65536)
            next(one)
            next(two)
            first.close()
            time.sleep(1)
            assert c.get(base).json()['active_streams'] >= 1
            assert list(Path('/cache').iterdir())
            assert c.delete(base).status_code in (200, 202)
        deadline = time.monotonic() + 20
        while c.get(base).status_code != 404 and time.monotonic() < deadline:
            time.sleep(1)
        assert c.get(base).status_code == 404
        range_check(other, 1048576, 1114111, reference)
        assert c.delete('/api/sessions/' + other['id']).status_code in (200, 202)
        wait_deleted(other)
        print('PASS two sessions isolated, session limit, one disconnect preserves other reader')
        return
    assert c.post('/api/torrents', content=seed.get('/fixture.torrent').content,
                  headers={'Content-Type': 'application/x-bittorrent'}).status_code == 409
    print('PASS duplicate rejected')
    range_check(s, 0, 65535, reference)
    used = sum(p.stat().st_blocks*512 for p in Path('/cache').rglob('*') if p.is_file())
    assert 0 < used < len(reference), (used, len(reference))
    print('PASS real disk pieces and streaming before complete download', used, '/', len(reference), flush=True)
    range_check(s, len(reference)-131072, len(reference)-65537, reference)
    range_check(s, 1048576, 1114111, reference)
    r = c.head(base + '/play')
    assert r.status_code == 200 and int(r.headers['content-length']) == len(reference) and not r.content, r
    r = c.get(base + '/play', headers={'Range': 'bytes=-1024'})
    assert r.status_code == 206 and r.content == reference[-1024:]
    r = c.get(base + '/play', headers={'Range': f'bytes={len(reference)+100}-'})
    assert r.status_code == 416 and r.headers['content-range'] == f'bytes */{len(reference)}', r.headers
    print('PASS HEAD, suffix Range, 416')
    with concurrent.futures.ThreadPoolExecutor() as pool:
        futures = [pool.submit(range_check, s, n, n+65535, reference) for n in (2000000, 4000000)]
        for future in futures:
            future.result()
    assert c.get(base).status_code == 200
    print('PASS simultaneous seeks and EOF does not delete session')
    small = min(s['files'], key=lambda f: f['length'])
    assert c.post(base + '/select', json={'index': small['id']}).status_code == 200
    r = c.get(base + '/play')
    assert r.status_code == 200 and r.content == seed.get('/fixture/small.mp4').content
    print('PASS multi-video selection and full HTTP 200 byte-exact')
    assert c.get(base).status_code == 200
    assert c.delete(base).status_code in (200, 202)
    wait_deleted(s)
    s = upload()
    base = '/api/sessions/' + s['id']
    with c.stream('GET', base + '/play') as stream:
        assert stream.status_code == 200
        chunks = stream.iter_bytes(65536)
        next(chunks)
        assert c.post(base + '/select', json={'index': small['id']}).status_code == 409
        assert c.delete(base).status_code in (200, 202)
    wait_deleted(s)
    print('PASS delete during active stream, selection race blocked')
    assert c.post('/api/torrents', content=b'invalid', headers={'Content-Type': 'application/x-bittorrent'}).status_code == 400
    assert c.post('/api/torrents', content=b'x'*(1024*1024+1), headers={'Content-Type': 'application/x-bittorrent'}).status_code == 413
    assert c.post('/api/torrents', content=b'x', headers={'Origin': 'http://evil.example', 'Content-Type': 'application/x-bittorrent'}).status_code == 403
    assert c.get('/health', headers={'Host': 'evil.example'}).status_code == 400
    print('PASS invalid upload, upload limit, cross-origin and Host protection')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'basic')
