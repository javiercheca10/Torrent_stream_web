"""Synthetic public-domain test patterns; isolated local BitTorrent seed + tracker."""
import http.server
import os
import socket
import struct
import subprocess
import time
from pathlib import Path

import libtorrent as lt

root = Path('/data')
media = root / 'fixture'
media.mkdir()
for name, duration, quality in [('main.mp4', '100', '2'), ('small.mp4', '2', '8')]:
    codec = ['-c:v', 'mpeg4', '-q:v', quality] if name == 'main.mp4' else ['-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '24']
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=960x540:rate=30', '-t', duration, *codec,
                    '-movflags', '+faststart', str(media / name)], check=True)
fs = lt.file_storage()
lt.add_files(fs, str(media))
t = lt.create_torrent(fs, 256 * 1024, flags=lt.create_torrent.v1_only)
t.set_priv(True)
t.add_tracker('http://test-seeder:8000/announce')
lt.set_piece_hashes(t, str(root))
torrent = lt.bencode(t.generate())
(root / 'fixture.torrent').write_bytes(torrent)
session = lt.session({'listen_interfaces': '0.0.0.0:6881', 'enable_dht': False,
                      'enable_lsd': False, 'enable_upnp': False, 'enable_natpmp': False,
                      'upload_rate_limit': 4 * 1024 * 1024})
params = lt.add_torrent_params()
params.ti = lt.torrent_info(torrent)
params.save_path = str(root)
params.flags |= lt.torrent_flags.seed_mode
handle = session.add_torrent(params)
for number in (2, 3):
    variant = t.generate()
    variant[b'info'][b'source'] = f'test-{number}'.encode()
    encoded = lt.bencode(variant)
    (root / f'fixture{number}.torrent').write_bytes(encoded)
    extra = lt.add_torrent_params()
    extra.ti = lt.torrent_info(encoded)
    extra.save_path = str(root)
    extra.flags |= lt.torrent_flags.seed_mode
    session.add_torrent(extra)
peer = socket.inet_aton(socket.gethostbyname(socket.gethostname())) + struct.pack('!H', 6881)


class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/announce'):
            body = lt.bencode({b'interval': 2, b'complete': 1, b'incomplete': 0, b'peers': peer})
        elif self.path == '/health':
            body = b'ok'
        else:
            return super().do_GET()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


os.chdir(root)
print('Fixture ready', handle.info_hash(), [(p.name, p.stat().st_size) for p in media.iterdir()], flush=True)
http.server.ThreadingHTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
