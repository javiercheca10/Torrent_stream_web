import asyncio
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import quote, urlsplit

import anyio
import bencodepy
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

log = logging.getLogger("torrent-stream")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
PUBLIC = os.getenv("PUBLIC_URL", "http://localhost:8787").rstrip("/")


def secret_from_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    path = os.getenv(f"{name}_FILE", "")
    if path:
        try:
            value = Path(path).read_text().strip()
        except OSError as exc:
            raise RuntimeError(f"No se pudo leer {name}_FILE") from exc
    return value


INTERNAL_API_TOKEN = secret_from_env("INTERNAL_API_TOKEN")
GRACE = int(os.getenv("GRACE_SECONDS", "900"))
TTL = int(os.getenv("TTL_SECONDS", "21600"))
PAUSED_TTL = int(os.getenv("PAUSED_TTL_SECONDS", "10800"))
NEVER_PLAYED_TTL = int(os.getenv("NEVER_PLAYED_TTL_SECONDS", "1800"))
SWEEP = float(os.getenv("SWEEP_SECONDS", "5"))
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "1"))
MAX_STREAMS = int(os.getenv("MAX_STREAMS", "4"))
DISK_LIMIT = int(os.getenv("DISK_LIMIT_BYTES", "64424509440"))
MIN_FREE = int(os.getenv("MIN_FREE_BYTES", "5368709120"))
BUFFER_TARGET = int(os.getenv("BUFFER_TARGET_BYTES", "536870912"))
BUFFER_TIMEOUT = int(os.getenv("BUFFER_TIMEOUT_SECONDS", "300"))
PREFETCH_CAP = int(os.getenv("PREFETCH_CAP_BYTES", "53687091200"))
CACHE = Path(os.getenv("CACHE_PATH", "/cache"))
STATE = Path(os.getenv("STATE_PATH", "/state"))
VIDEO = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".m2ts", ".webm", ".mpg", ".mpeg"}
sessions = {}
lock = asyncio.Lock()
client = None
ready = False


def validate_torrent(data):
    try:
        meta = bencodepy.decode(data)
        info = meta[b"info"]
        piece = info[b"piece length"]
        pieces = info[b"pieces"]
        files = info.get(b"files", [{b"length": info.get(b"length")}])
        lengths = [f[b"length"] for f in files]
        if not 16384 <= piece <= 64 * 1024 * 1024 or len(files) > 2000:
            raise ValueError()
        if any(not isinstance(n, int) or n < 0 for n in lengths):
            raise ValueError()
        total = sum(lengths)
        if not 0 < total <= 1024**4 or len(pieces) != math.ceil(total / piece) * 20:
            raise ValueError()
        return hashlib.sha1(bencodepy.encode(info)).hexdigest()
    except Exception as exc:
        raise HTTPException(400, "Torrent v1/hibrido invalido o limites de metadata excedidos") from exc


def persist():
    data = [{k: v for k, v in s.items() if k not in {"tasks", "bg"}} for s in sessions.values()]
    tmp = STATE / "sessions.tmp"
    with tmp.open("w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(STATE / "sessions.json")


def require_owner(s, request: Request):
    if request.headers.get("X-Owner-Chat-Id") != str(s["owner_chat_id"]):
        raise HTTPException(403, "Esta sesion pertenece a otro usuario")


async def api(action, hash=None):
    response = await client.post("/torrents", json={"action": action, **({"hash": hash} if hash else {})})
    response.raise_for_status()
    return response.json() if response.content else None


def disk_usage():
    used = 0
    for p in CACHE.rglob("*"):
        try:
            if p.is_file():
                used += p.stat().st_blocks * 512
        except FileNotFoundError:
            pass
    return used, shutil.disk_usage(CACHE).free


async def remove_hash(hash):
    if not re.fullmatch(r"[0-9a-f]{40}", hash):
        raise ValueError("Invalid cache hash")
    await api("rem", hash)
    response = await client.post("/viewed", json={"action": "rem", "hash": hash, "file_index": -1})
    response.raise_for_status()
    path = CACHE / hash
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        await asyncio.to_thread(shutil.rmtree, path)


def closing(s, reason):
    if s["state"] != "deleting":
        s["state"] = "deleting"
        s["reason"] = reason
        log.info("session_closing id=%s reason=%s streams=%s bg=%s", s["id"], reason, len(s["tasks"]), len(s["bg"]))
        persist()
    for task in list(s["tasks"]):
        task.cancel()
    for task in list(s["bg"]):
        task.cancel()


async def reap(s):
    if s["tasks"] or s["bg"]:
        return
    try:
        await remove_hash(s["hash"])
        del sessions[s["id"]]
        persist()
        log.info("session_deleted id=%s reason=%s", s["id"], s.get("reason"))
    except Exception:
        log.exception("cleanup_error id=%s; retry next sweep", s["id"])


def expiry(s, now):
    if now - s["created"] >= TTL:
        return "ttl"
    if not s["started"]:
        if now - s["created"] >= NEVER_PLAYED_TTL:
            return "never_played"
        return None
    if s.get("paused") and s.get("paused_at") and now - s["paused_at"] >= PAUSED_TTL:
        return "paused_ttl"
    if s.get("paused"):
        return None
    if s["started"] and not s["tasks"] and not s["bg"] and now - s["last_activity"] >= GRACE:
        return "disconnect_grace"
    return None


async def sweep():
    global ready
    while True:
        await asyncio.sleep(SWEEP)
        try:
            async with lock:
                torrents = await api("list")
                known = {s["hash"] for s in sessions.values()}
                orphans = {t["hash"] for t in torrents} - known
                orphans.update(p.name for p in CACHE.iterdir()
                               if re.fullmatch(r"[0-9a-f]{40}", p.name) and p.name not in known)
                for hash in orphans:
                    await remove_hash(hash)
                    log.info("orphan_removed hash=%s", hash)
                live = {t["hash"] for t in torrents}
                used, free = await asyncio.to_thread(disk_usage)
                for s in list(sessions.values()):
                    reason = expiry(s, time.time())
                    if used > DISK_LIMIT or free < MIN_FREE:
                        reason = "disk_pressure"
                    if s["hash"] not in live:
                        reason = "upstream_lost"
                    if reason:
                        closing(s, reason)
                    if s["state"] == "deleting":
                        await reap(s)
                    else:
                        await api("get", s["hash"])
                ready = True
        except Exception:
            ready = False
            log.exception("sweep_error; cleanup will retry")


@asynccontextmanager
async def lifespan(app):
    global client, ready
    if len(INTERNAL_API_TOKEN) < 32:
        raise RuntimeError("INTERNAL_API_TOKEN debe tener al menos 32 caracteres")
    STATE.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    client = httpx.AsyncClient(base_url=os.getenv("TORRSERVER_URL", "http://torrserver:8090"),
                               timeout=httpx.Timeout(15, connect=5), limits=httpx.Limits(max_connections=16),
                               headers={"Accept-Encoding": "identity"})
    try:
        old = json.loads((STATE / "sessions.json").read_text()) if (STATE / "sessions.json").exists() else []
    except (ValueError, OSError):
        log.exception("state_unreadable; reconcile API and cache instead")
        old = []
    policy = json.loads(Path("/policy.json").read_text())
    r = await client.post("/settings", json={"action": "get"})
    r.raise_for_status()
    settings = r.json()
    if any(settings.get(k) != v for k, v in policy.items()):
        settings.update(policy)
        r = await client.post("/settings", json={"action": "set", "sets": settings})
        r.raise_for_status()
    r = await client.post("/settings", json={"action": "get"})
    r.raise_for_status()
    if any(r.json().get(k) != v for k, v in policy.items()):
        raise RuntimeError("TorrServer policy mismatch")
    hashes = {t["hash"] for t in await api("list")}
    viewed = await client.post("/viewed", json={"action": "list"})
    viewed.raise_for_status()
    hashes.update(t["hash"] for t in (viewed.json() or []))
    hashes.update(s["hash"] for s in old if isinstance(s, dict) and "hash" in s)
    hashes.update(p.name for p in CACHE.iterdir() if re.fullmatch(r"[0-9a-f]{40}", p.name))
    for hash in hashes:
        await remove_hash(hash)
        log.info("startup_orphan_removed hash=%s", hash)
    sessions.clear()
    persist()
    ready = True
    worker = asyncio.create_task(sweep())
    log.info("ready public=%s grace=%s ttl=%s paused_ttl=%s buffer=%s prefetch_cap=%s",
             PUBLIC, GRACE, TTL, PAUSED_TTL, BUFFER_TARGET, PREFETCH_CAP)
    try:
        yield
    finally:
        ready = False
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        async with lock:
            for s in list(sessions.values()):
                closing(s, "shutdown")
                await reap(s)
        await client.aclose()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=list(
        filter(
            None,
            [
                urlsplit(PUBLIC).hostname,
                "web",
                "127.0.0.1",
                "localhost",
                "100.67.190.35",
                "server",
                "*.ts.net",
                *os.getenv("ALLOWED_HOSTS", "").split(","),
            ],
        )
    ),
)


def is_trusted_tv_client(request: Request) -> bool:
    supplied = request.headers.get("X-Internal-Token", "")
    if INTERNAL_API_TOKEN and secrets.compare_digest(supplied, INTERNAL_API_TOKEN):
        return True
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback or ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


@app.middleware("http")
async def protection(request, call_next):
    path = request.url.path
    public_api = path == "/api/status" or path.endswith("/play")
    tv_api = path.startswith("/api/tv/")
    if tv_api:
        if not is_trusted_tv_client(request):
            return JSONResponse({"detail": "No autorizado"}, status_code=401)
    elif path.startswith("/api/") and not public_api:
        supplied = request.headers.get("X-Internal-Token", "")
        if not secrets.compare_digest(supplied, INTERNAL_API_TOKEN):
            return JSONResponse({"detail": "No autorizado"}, status_code=401)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        origin = request.headers.get("origin")
        if (origin and origin != PUBLIC) or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "Origen no permitido"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(httpx.HTTPError)
async def upstream_error(request, exc):
    log.error("upstream_error %s", exc)
    return JSONResponse({"detail": "TorrServer no disponible; reintenta"}, status_code=502)


def get_session(id):
    if id not in sessions:
        raise HTTPException(404, "Sesion no encontrada o caducada")
    return sessions[id]


def public(s, base_url=None):
    base = (base_url or PUBLIC).rstrip("/")
    return {**{k: v for k, v in s.items() if k not in {"tasks", "hash", "bg", "owner_chat_id", "play_token"}},
            "active_streams": len(s["tasks"]),
            "paused": s.get("paused", False),
            "paused_until": (s["paused_at"] + PAUSED_TTL) if s.get("paused") and s.get("paused_at") else None,
            "expires_at": s["created"] + TTL,
            "buffer_ready": s.get("buffered_bytes", 0) >= s.get("buffer_target", 1),
            "prefetched_bytes": s.get("prefetched_bytes", 0),
            "play_url": f"{base}/api/sessions/{s['id']}/play?token={s['play_token']}"}


def status(s):
    return {
        "title": s.get("title", "Metadata"),
        "state": s["state"],
        "created": s["created"],
        "expires_at": s["created"] + TTL,
        "active_streams": len(s["tasks"]),
        "paused": s.get("paused", False),
        "buffered_bytes": s.get("buffered_bytes", 0),
        "buffer_target": s.get("buffer_target", 0),
        "buffer_ready": s.get("buffered_bytes", 0) >= s.get("buffer_target", 1),
    }


@app.get("/")
async def index():
    return FileResponse("/app/app/index.html")


@app.api_route("/health", methods=["GET", "HEAD"])
async def health():
    r = await client.get("/echo", timeout=3)
    r.raise_for_status()
    return JSONResponse({"ready": ready, "torrserver": r.text}, status_code=200 if ready else 503)


@app.post("/api/torrents", status_code=201)
async def upload(request: Request):
    if request.headers.get("content-type", "").split(";")[0] not in {"application/x-bittorrent", "application/octet-stream"}:
        raise HTTPException(415, "Envia el archivo .torrent como cuerpo binario")
    data = bytearray()
    async with asyncio.timeout(30):
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > 1024 * 1024:
                raise HTTPException(413, "Limite .torrent: 1 MiB")
    hash = validate_torrent(bytes(data))
    try:
        owner_chat_id = int(request.headers["X-Owner-Chat-Id"])
        if owner_chat_id <= 0:
            raise ValueError()
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, "Falta el propietario de la sesion") from exc
    async with lock:
        if not ready:
            raise HTTPException(503, "Servicio reconciliando")
        if any(s["hash"] == hash for s in sessions.values()):
            raise HTTPException(409, "Este torrent ya tiene una sesion; usa la existente")
        used, free = await asyncio.to_thread(disk_usage)
        if len(sessions) >= MAX_SESSIONS or used >= DISK_LIMIT or free < MIN_FREE:
            raise HTTPException(503, "Limite de sesiones o disco alcanzado")
        id = uuid.uuid4().hex
        now = time.time()
        s = dict(id=id, hash=hash, created=now, last_activity=now, started=False,
                 state="metadata", files=[], selected=None, tasks=set(), bg=set(),
                 paused=False, paused_at=None, buffered_bytes=0, prefetched_bytes=0,
                 owner_chat_id=owner_chat_id, play_token=secrets.token_urlsafe(32))
        sessions[id] = s
        persist()
        try:
            r = await client.post("/torrent/upload", files={"file": ("upload.torrent", bytes(data), "application/x-bittorrent")})
            r.raise_for_status()
            t = r.json()
            if not isinstance(t, dict) or t.get("hash") != hash:
                raise HTTPException(400, "TorrServer rechazo el torrent")
            for _ in range(20):
                t = await api("get", hash)
                if t.get("file_stats"):
                    break
                await asyncio.sleep(0.25)
            s["files"] = [f for f in t.get("file_stats", []) if Path(f["path"]).suffix.lower() in VIDEO]
            if not s["files"]:
                raise HTTPException(422, "No hay archivos de video compatibles")
            s["selected"] = max(s["files"], key=lambda f: f["length"])["id"]
            s["title"] = t.get("title", "Video")
            s["state"] = "ready"
            s["buffer_target"] = min(BUFFER_TARGET, max(s["files"], key=lambda f: f["length"])["length"])
            persist()
            task = asyncio.create_task(warm(s), name="warmup")
            s["bg"].add(task)
            task.add_done_callback(lambda finished: s["bg"].discard(finished))
            log.info("torrent_added metadata_ready id=%s files=%s", id, len(s["files"]))
            return public(s)
        except BaseException:
            with anyio.CancelScope(shield=True):
                closing(s, "upload_failed")
                await reap(s)
            raise


@app.get("/api/status")
async def listing():
    async with lock:
        return [status(s) for s in sessions.values()]


@app.get("/api/tv/sessions")
async def tv_listing(request: Request):
    base = str(request.base_url).rstrip("/")
    async with lock:
        return [public(s, base_url=base) for s in sessions.values() if s["state"] != "deleting"]


@app.post("/api/tv/sessions/{id}/pause")
async def tv_pause_session(id: str):
    async with lock:
        s = get_session(id)
        if s["state"] == "deleting" or expiry(s, time.time()) == "ttl":
            raise HTTPException(410, "Sesion terminada")
        if not s.get("paused"):
            s["paused"] = True
            s["paused_at"] = time.time()
            s["last_activity"] = time.time()
            for task in list(s["tasks"]) + list(s["bg"]):
                task.cancel()
            persist()
            log.info("session_paused_tv id=%s", id)
        return {"ok": True, "paused_until": s["paused_at"] + PAUSED_TTL}


@app.post("/api/tv/sessions/{id}/resume")
async def tv_resume_session(id: str, request: Request):
    base = str(request.base_url).rstrip("/")
    async with lock:
        s = get_session(id)
        if s["state"] == "deleting" or expiry(s, time.time()) == "ttl":
            raise HTTPException(410, "Sesion terminada")
        for task in list(s["bg"]):
            task.cancel()
        s["paused"] = False
        s["paused_at"] = None
        s["buffered_bytes"] = 0
        s["prefetched_bytes"] = 0
        task = asyncio.create_task(warm(s), name="warmup")
        s["bg"].add(task)
        task.add_done_callback(lambda finished: s["bg"].discard(finished))
        persist()
        return public(s, base_url=base)


@app.post("/api/tv/sessions/{id}/select")
async def tv_select_file(id: str, request: Request):
    body = await request.json()
    base = str(request.base_url).rstrip("/")
    async with lock:
        s = get_session(id)
        if s["tasks"] or s["state"] == "deleting":
            raise HTTPException(409, "Deten la reproduccion antes de cambiar de archivo")
        target_idx = body.get("index")
        selected_file = next((f for f in s["files"] if f["id"] == target_idx), None)
        if not selected_file:
            raise HTTPException(400, "Indice de video invalido")
        for task in list(s["bg"]):
            task.cancel()
        s["selected"] = target_idx
        s["buffered_bytes"] = 0
        s["prefetched_bytes"] = 0
        s["buffer_target"] = min(BUFFER_TARGET, selected_file["length"])
        if not s.get("paused"):
            task = asyncio.create_task(warm(s), name="warmup")
            s["bg"].add(task)
            task.add_done_callback(lambda finished: s["bg"].discard(finished))
        persist()
        return public(s, base_url=base)


@app.get("/api/sessions/{id}")
async def detail(id: str, request: Request):
    async with lock:
        s = get_session(id)
        require_owner(s, request)
        t = await api("get", s["hash"]) if s["state"] != "deleting" else {}
        return {**public(s), "stats": t}


@app.post("/api/sessions/{id}/select")
async def select(id: str, request: Request):
    body = await request.json()
    async with lock:
        s = get_session(id)
        require_owner(s, request)
        if s["tasks"] or s["bg"] or s["state"] == "deleting":
            raise HTTPException(409, "Deten la reproduccion antes de cambiar de archivo")
        if body.get("index") not in [f["id"] for f in s["files"]]:
            raise HTTPException(400, "Indice de video invalido")
        s["selected"] = body["index"]
        persist()
        return public(s)


@app.post("/api/sessions/{id}/heartbeat")
async def heartbeat(id: str, request: Request):
    async with lock:
        s = get_session(id)
        require_owner(s, request)
        if s["state"] == "deleting" or expiry(s, time.time()) == "ttl":
            raise HTTPException(410, "Sesion terminada")
        s["started"] = True
        s["last_activity"] = time.time()
        return {"ok": True}


@app.post("/api/sessions/{id}/pause")
async def pause_session(id: str, request: Request):
    async with lock:
        s = get_session(id)
        require_owner(s, request)
        if s["state"] == "deleting" or expiry(s, time.time()) == "ttl":
            raise HTTPException(410, "Sesion terminada")
        if not s.get("paused"):
            s["paused"] = True
            s["paused_at"] = time.time()
            s["last_activity"] = time.time()
            for task in list(s["tasks"]) + list(s["bg"]):
                task.cancel()
            persist()
            log.info("session_paused id=%s", id)
        return {"ok": True, "paused_until": s["paused_at"] + PAUSED_TTL}


@app.post("/api/sessions/{id}/resume")
async def resume_session(id: str, request: Request):
    async with lock:
        s = get_session(id)
        require_owner(s, request)
        if s["state"] == "deleting" or expiry(s, time.time()) == "ttl":
            raise HTTPException(410, "Sesion terminada")
        for task in list(s["bg"]):
            task.cancel()
        s["paused"] = False
        s["paused_at"] = None
        s["buffered_bytes"] = 0
        s["prefetched_bytes"] = 0
        task = asyncio.create_task(warm(s), name="warmup")
        s["bg"].add(task)
        task.add_done_callback(lambda finished: s["bg"].discard(finished))
        persist()
        return public(s)


@app.delete("/api/sessions/{id}")
@app.post("/api/sessions/{id}/end")
async def delete(id: str, request: Request):
    async with lock:
        s = get_session(id)
        require_owner(s, request)
        closing(s, "explicit_end")
        await reap(s)
        return JSONResponse({"state": "deleting" if id in sessions else "deleted"}, status_code=202 if id in sessions else 200)


async def release(s, task, upstream):
    with anyio.CancelScope(shield=True):
        try:
            if upstream is not None:
                await upstream.aclose()
        finally:
            async with lock:
                s["tasks"].discard(task)
                s["last_activity"] = time.time()
                log.info("connection_closed id=%s role=%s remaining=%s; EOF is not movie end", s["id"], task.get_name(), len(s["tasks"]))


async def warm(s):
    """Fill buffer to BUFFER_TARGET (plus file tail for MKV/MP4 index), then prefetch up to PREFETCH_CAP when idle."""
    selected_file = next((f for f in s["files"] if f["id"] == s["selected"]), max(s["files"], key=lambda f: f["length"]))
    file_len = selected_file["length"]
    buf_target = s.get("buffer_target", BUFFER_TARGET)
    prefetch_limit = min(PREFETCH_CAP, file_len) if file_len > buf_target else 0
    current_pos = s.get("buffered_bytes", 0)
    deadline = time.monotonic() + BUFFER_TIMEOUT
    try:
        # Phase 1: fill initial buffer with retry up to BUFFER_TIMEOUT
        while current_pos < buf_target:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or s["state"] == "deleting" or s.get("paused"):
                raise TimeoutError()
            async with asyncio.timeout(remaining):
                async with client.stream("GET", "/stream", params={"link": s["hash"], "index": s["selected"], "play": ""},
                                         headers={"Range": f"bytes={current_pos}-{buf_target-1}"}, timeout=max(10, remaining - 2)) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_raw(65536):
                        s["buffered_bytes"] += len(chunk)
                        current_pos = s["buffered_bytes"]
                        if s["tasks"]:
                            break
            if s["tasks"]:
                break
        # Pre-warm last 4 MiB of large files so MKV Cues / MP4 moov index at EOF load instantly
        if file_len > buf_target and not s["tasks"]:
            tail_start = max(buf_target, file_len - 4 * 1024 * 1024)
            with suppress(httpx.HTTPError, TimeoutError):
                async with asyncio.timeout(45):
                    async with client.stream("GET", "/stream", params={"link": s["hash"], "index": s["selected"], "play": ""},
                                             headers={"Range": f"bytes={tail_start}-{file_len-1}"}, timeout=40) as response:
                        if response.status_code < 400:
                            async for _ in response.aiter_raw(65536):
                                if s["tasks"]:
                                    break
        log.info("buffer_ready id=%s bytes=%s", s["id"], s["buffered_bytes"])
    except (httpx.HTTPError, TimeoutError):
        log.warning("buffer_timeout id=%s buffered=%s; player may still start when peers respond", s["id"], s["buffered_bytes"])
        return
    # Phase 2: prefetch in 64 MiB blocks only while no active player stream is open (avoids HDD/cache thrashing)
    chunk_window = 64 * 1024 * 1024
    try:
        while current_pos < prefetch_limit:
            if asyncio.current_task().cancelled() or s["state"] == "deleting" or s.get("paused"):
                break
            if s["started"] and not s["tasks"] and time.time() - s["last_activity"] >= GRACE:
                break
            if s["tasks"]:
                await asyncio.sleep(2)
                continue
            end = min(current_pos + chunk_window, prefetch_limit) - 1
            try:
                async with client.stream("GET", "/stream", params={"link": s["hash"], "index": s["selected"], "play": ""},
                                         headers={"Range": f"bytes={current_pos}-{end}"}, timeout=120) as response:
                    if response.status_code >= 400:
                        await asyncio.sleep(3)
                        continue
                    async for chunk in response.aiter_raw(65536):
                        s["prefetched_bytes"] += len(chunk)
                        current_pos += len(chunk)
                        if s["tasks"] or s["state"] == "deleting" or s.get("paused"):
                            break
            except (httpx.HTTPError, TimeoutError):
                await asyncio.sleep(3)
        log.info("prefetch_done id=%s prefetched=%s", s["id"], s["prefetched_bytes"])
    except asyncio.CancelledError:
        log.info("prefetch_stopped id=%s prefetched=%s", s["id"], s["prefetched_bytes"])


class Stream(StreamingResponse):
    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            await release(self.session, self.owner, self.upstream)


@app.api_route("/api/sessions/{id}/play", methods=["GET", "HEAD"])
async def play(id: str, request: Request):
    task = asyncio.current_task()
    async with lock:
        s = get_session(id)
        if not secrets.compare_digest(request.query_params.get("token", ""), s["play_token"]):
            raise HTTPException(404, "Sesion no encontrada o caducada")
        if s["state"] != "ready" or s.get("paused") or expiry(s, time.time()):
            raise HTTPException(410, "Sesion terminada")
        if s.get("buffered_bytes", 0) < s.get("buffer_target", 1) and s["bg"]:
            retry = max(5, min(30, int((s["buffer_target"] - s.get("buffered_bytes", 0)) / 1024 / 1024)))
            return JSONResponse(
                {"detail": "Buffer preparando", "buffered_bytes": s.get("buffered_bytes", 0),
                 "buffer_target": s["buffer_target"], "buffer_ready": False},
                status_code=425, headers={"Retry-After": str(retry)})
        if len(s["tasks"]) >= MAX_STREAMS:
            raise HTTPException(429, "Demasiados streams simultaneos")
        s["tasks"].add(task)
        s["started"] = True
        s["last_activity"] = time.time()
        file = next(f for f in s["files"] if f["id"] == s["selected"])
        hash, index = s["hash"], s["selected"]
    upstream = None
    try:
        headers = {k: request.headers[k] for k in ("range", "if-range", "if-none-match", "if-modified-since") if k in request.headers}
        req = client.build_request(request.method, "/stream/" + quote(Path(file["path"]).name, safe=""),
                                   params={"link": hash, "index": index, "play": ""}, headers=headers,
                                   timeout=httpx.Timeout(300, connect=10))
        upstream = await client.send(req, stream=True)
        allowed = {"content-type", "content-length", "content-range", "accept-ranges", "etag", "last-modified"}
        headers = {k: v for k, v in upstream.headers.items() if k in allowed}
        response = Stream(upstream.aiter_raw(65536), status_code=upstream.status_code, headers=headers)
        response.session, response.owner, response.upstream = s, task, upstream
        log.info("play_started id=%s status=%s range=%s", id, upstream.status_code, request.headers.get("range"))
        return response
    except BaseException:
        await release(s, task, upstream)
        raise
