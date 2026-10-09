import argparse
import asyncio
import html
import json
import logging
import os
import re
import time
from pathlib import Path

import bencodepy
import httpx

log = logging.getLogger("torrent-stream.telegram")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

WEB = os.getenv("WEB_URL", "http://web:8787").rstrip("/")
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


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
ALLOWED_USERS = {int(x) for x in re.split(r"[\s,]+", os.getenv("TELEGRAM_ALLOWED_USERS", "")) if x}
POLL_SECONDS = int(os.getenv("TELEGRAM_POLL_SECONDS", "25"))
MAX_TORRENT_BYTES = int(os.getenv("BOT_MAX_TORRENT_BYTES", "1048576"))
API = f"https://api.telegram.org/bot{TOKEN}"
FILE = f"https://api.telegram.org/file/bot{TOKEN}"
OFFSET_FILE = Path("/state/telegram-offset.json")


def parse_torrent_name(data: bytes) -> str:
    try:
        meta = bencodepy.decode(data)
        return meta[b"info"].get(b"name", b"torrent").decode(errors="replace")
    except Exception:
        return "torrent"


def allowed(user_id: int | None) -> bool:
    return bool(user_id and user_id in ALLOWED_USERS)


def owner_headers(user_id: int) -> dict:
    return {"X-Owner-Chat-Id": str(user_id)}


def load_offset() -> int:
    try:
        return int(json.loads(OFFSET_FILE.read_text()).get("offset", 0))
    except (OSError, ValueError, json.JSONDecodeError):
        return 0


def save_offset(offset: int):
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OFFSET_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"offset": offset}))
    tmp.replace(OFFSET_FILE)


def report_task(task: asyncio.Task):
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        log.exception("telegram_update_error")


def fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n/1024:.0f} KiB"
    if n < 1024**3:
        return f"{n/1024**2:.1f} MiB"
    return f"{n/1024**3:.2f} GiB"


def session_keyboard(session_id: str, paused: bool = False):
    action = "resume" if paused else "pause"
    label = "▶ Reanudar" if paused else "⏸ Pausar"
    return {
        "inline_keyboard": [[
            {"text": label, "callback_data": f"{action}:{session_id}"},
            {"text": "✕ Terminar", "callback_data": f"del:{session_id}"},
        ]]
    }


def buffer_keyboard(session_id: str):
    return {
        "inline_keyboard": [[
            {"text": "Ver progreso", "callback_data": f"buf:{session_id}"},
            {"text": "✕ Cancelar", "callback_data": f"del:{session_id}"},
        ]]
    }


async def tg(client: httpx.AsyncClient, method: str, payload: dict | None = None, files=None):
    r = await client.post(f"{API}/{method}", json=payload, files=files)
    r.raise_for_status()
    return r.json()


async def send_message(client: httpx.AsyncClient, chat_id: int, text: str, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await tg(client, "sendMessage", payload)


async def edit_message(client: httpx.AsyncClient, chat_id: int, message_id: int, text: str, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return await tg(client, "editMessageText", payload)


async def edit_keyboard(client: httpx.AsyncClient, chat_id: int, message_id: int, reply_markup):
    return await tg(client, "editMessageReplyMarkup", {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": reply_markup,
    })


async def answer_callback(client: httpx.AsyncClient, callback_id: str, text: str, show_alert=False):
    return await tg(client, "answerCallbackQuery", {"callback_query_id": callback_id, "text": text, "show_alert": show_alert})


async def wait_for_buffer(client: httpx.AsyncClient, session_id: str, user_id: int, timeout: int = 600) -> dict:
    start = time.time()
    while time.time() - start < timeout:
        r = await client.get(f"/api/sessions/{session_id}", headers=owner_headers(user_id))
        if r.status_code == 200:
            s = r.json()
            if s.get("buffer_ready"):
                return s
        await asyncio.sleep(5)
    r = await client.get(f"/api/sessions/{session_id}", headers=owner_headers(user_id))
    return r.json() if r.status_code == 200 else {}


def _msg_buffer_wait(session: dict, title: str, file_path: str, file_size: int) -> str:
    return (
        f"🎞 <b>{html.escape(str(session.get('title') or title))}</b>\n"
        f"<code>{html.escape(str(file_path))}</code> · {fmt_size(file_size)}\n\n"
        f"La sala está preparando el buffer. Te avisaré cuando esté listo para ver sin prisas."
    )


def _msg_ready(session: dict, title: str, file_path: str, file_size: int, play_url: str) -> str:
    return (
        f"🎞 <b>{html.escape(str(session.get('title') or title))}</b>\n"
        f"<code>{html.escape(str(file_path))}</code> · {fmt_size(file_size)}\n\n"
        f"✅ <b>Todo listo para la tele</b>\n"
        f"En VLC o Kodi: <b>Abrir flujo de red</b> y pega este enlace:\n"
        f"<code>{html.escape(play_url)}</code>\n\n"
        f"\nLa tele debe estar en la misma WiFi. Puedes pausar o terminar la sesión desde los botones."
    )


def _msg_not_ready(session: dict, title: str, file_path: str, file_size: int, play_url: str) -> str:
    return (
        f"🎞 <b>{html.escape(str(session.get('title') or title))}</b>\n"
        f"<code>{html.escape(str(file_path))}</code> · {fmt_size(file_size)}\n\n"
        f"⏳ <b>El buffer necesita un poco más de tiempo</b>\n"
        f"Si decides empezar ya, en VLC o Kodi abre un flujo de red y pega:\n"
        f"<code>{html.escape(play_url)}</code>\n\n"
        f"\nLa tele debe estar en la misma WiFi. Si hay cortes, espera unos minutos y vuelve a abrir el enlace."
    )


async def process_torrent(client: httpx.AsyncClient, web: httpx.AsyncClient, chat_id: int, user_id: int, data: bytes, filename: str):
    if not allowed(user_id):
        await send_message(client, chat_id, "⛔ Usuario no autorizado.")
        return
    if len(data) > MAX_TORRENT_BYTES:
        await send_message(client, chat_id, "⛔ El torrent supera el limite de 1 MiB.")
        return
    title = parse_torrent_name(data)
    try:
        r = await web.post("/api/torrents", content=data, headers={"Content-Type": "application/x-bittorrent", **owner_headers(user_id)})
        r.raise_for_status()
    except httpx.HTTPStatusError as exc:
        try:
            detail = exc.response.json().get("detail", "Error desconocido")
        except ValueError:
            detail = "Error desconocido"
        await send_message(client, chat_id, f"❌ Error al crear sesion:\n<code>{html.escape(str(detail))}</code>")
        return
    session = r.json()
    session_id = session["id"]
    files = session.get("files", [])
    selected = next((f for f in files if f["id"] == session.get("selected")), files[0] if files else {})
    file_size = selected.get("length", 0)
    file_path = selected.get("path", filename)
    msg = _msg_buffer_wait(session, title, file_path, file_size)
    sent = await send_message(client, chat_id, msg, buffer_keyboard(session_id))
    message_id = sent["result"]["message_id"]
    ready_session = await wait_for_buffer(web, session_id, user_id)
    play_url = ready_session.get("play_url", session["play_url"])
    buffer_ready = ready_session.get("buffer_ready", False)
    if buffer_ready:
        final = _msg_ready(session, title, file_path, file_size, play_url)
    else:
        final = _msg_not_ready(session, title, file_path, file_size, play_url)
    await edit_message(client, chat_id, message_id, final, session_keyboard(session_id, ready_session.get("paused", False)))


async def process_callback(client: httpx.AsyncClient, web: httpx.AsyncClient, callback: dict):
    data = callback.get("data", "")
    callback_id = callback["id"]
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    user_id = callback.get("from", {}).get("id")
    if not allowed(user_id):
        await answer_callback(client, callback_id, "Usuario no autorizado", show_alert=True)
        return
    if not chat_id or message.get("chat", {}).get("type") != "private":
        await answer_callback(client, callback_id, "Usa el bot en un chat privado", show_alert=True)
        return
    if data.startswith("del:"):
        session_id = data.split(":", 1)[1]
        r = await web.delete(f"/api/sessions/{session_id}", headers=owner_headers(user_id))
        if r.status_code in {200, 202}:
            await answer_callback(client, callback_id, "Sesion eliminada")
            await send_message(client, chat_id, "🗑 Sesion eliminada.")
        else:
            await answer_callback(client, callback_id, "Sesion no encontrada o no permitida", show_alert=True)
        return
    if data.startswith("pause:"):
        session_id = data.split(":", 1)[1]
        r = await web.post(f"/api/sessions/{session_id}/pause", headers=owner_headers(user_id))
        if r.status_code == 200:
            paused_until = r.json().get("paused_until")
            from datetime import datetime
            limit = datetime.fromtimestamp(paused_until).strftime("%H:%M") if paused_until else "desconocida"
            await edit_keyboard(client, chat_id, message_id, session_keyboard(session_id, paused=True))
            await answer_callback(client, callback_id, "Sesion pausada")
            await send_message(client, chat_id, f"⏸ Sesión pausada. Se borrará automáticamente a las <b>{limit}</b>.")
        else:
            await answer_callback(client, callback_id, "No se pudo pausar", show_alert=True)
        return
    if data.startswith("resume:"):
        session_id = data.split(":", 1)[1]
        r = await web.post(f"/api/sessions/{session_id}/resume", headers=owner_headers(user_id))
        if r.status_code == 200:
            await edit_keyboard(client, chat_id, message_id, session_keyboard(session_id))
            await answer_callback(client, callback_id, "Sesion reanudada")
            await send_message(client, chat_id, "▶ Sesión reanudada. El buffer se prepara de nuevo.")
        else:
            await answer_callback(client, callback_id, "No se pudo reanudar", show_alert=True)
        return
    if data.startswith("buf:"):
        session_id = data.split(":", 1)[1]
        await answer_callback(client, callback_id, "Esperando buffer...")
        r = await web.get(f"/api/sessions/{session_id}", headers=owner_headers(user_id))
        if r.status_code != 200:
            await send_message(client, chat_id, "❌ Sesion no encontrada o caducada.")
            return
        session = r.json()
        if session.get("buffer_ready"):
            play_url = session["play_url"]
            files = session.get("files", [])
            selected = next((f for f in files if f["id"] == session.get("selected")), files[0] if files else {})
            final = _msg_ready(session, session.get("title", "Video"), selected.get("path", ""), selected.get("length", 0), play_url)
            await send_message(client, chat_id, final, session_keyboard(session_id, session.get("paused", False)))
        else:
            buf = session.get("buffered_bytes", 0)
            tgt = session.get("buffer_target", 1)
            pct = min(100, buf / max(tgt, 1) * 100)
            await send_message(client, chat_id, f"⏳ Buffer: {fmt_size(buf)} / {fmt_size(tgt)} ({pct:.0f}%)\nEspera unos segundos y pulsa de nuevo.")
        return


async def handle_update(client: httpx.AsyncClient, web: httpx.AsyncClient, update: dict):
    if "message" in update:
        message = update["message"]
        chat = message.get("chat", {})
        if chat.get("type") != "private":
            return
        user = message.get("from", {}).get("id")
        if not allowed(user):
            await send_message(client, chat["id"], "⛔ Usuario no autorizado.")
            return
        doc = message.get("document")
        if not doc:
            text = message.get("text", "").strip().lower()
            if text in {"/start", "/ayuda", "/help"}:
                await send_message(client, chat["id"], "<b>Bienvenido a Cine en casa</b>\n\nEnvíame un archivo <code>.torrent</code> y prepararé la película para la tele. Cuando esté lista, recibirás un enlace para VLC o Kodi.\n\nPuedes consultar el progreso en la página de la sala y controlar todo desde este chat.")
            else:
                await send_message(client, chat["id"], "Envía un archivo <b>.torrent</b> para preparar una película. Usa /ayuda si necesitas una guía.")
            return
        name = doc.get("file_name", "archivo.torrent")
        if not name.lower().endswith(".torrent"):
            await send_message(client, chat["id"], "⚠️ Envía un archivo <b>.torrent</b>.")
            return
        if doc.get("file_size", 0) > MAX_TORRENT_BYTES:
            await send_message(client, chat["id"], "⛔ El torrent supera el limite de 1 MiB.")
            return
        f = await tg(client, "getFile", {"file_id": doc["file_id"]})
        path = f["result"]["file_path"]
        async with client.stream("GET", f"{FILE}/{path}") as response:
            response.raise_for_status()
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > MAX_TORRENT_BYTES:
                    await send_message(client, chat["id"], "⛔ El torrent supera el limite de 1 MiB.")
                    return
        await process_torrent(client, web, chat["id"], user, bytes(data), name)
        return
    if "callback_query" in update:
        await process_callback(client, web, update["callback_query"])


async def run_bot():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN no configurado")
    if not ALLOWED_USERS:
        raise SystemExit("TELEGRAM_ALLOWED_USERS vacio")
    if len(INTERNAL_API_TOKEN) < 32:
        raise SystemExit("INTERNAL_API_TOKEN debe tener al menos 32 caracteres")
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10)) as client, httpx.AsyncClient(base_url=WEB, timeout=60, headers={"X-Internal-Token": INTERNAL_API_TOKEN}) as web:
        await web.get("/health")
        await tg(client, "getMe")
        offset = load_offset()
        tasks = set()
        log.info("telegram_bot_ready web=%s allowed_users=%s", WEB, len(ALLOWED_USERS))
        try:
            while True:
                try:
                    payload = {"timeout": POLL_SECONDS, "offset": offset, "allowed_updates": ["message", "callback_query"]}
                    updates = (await tg(client, "getUpdates", payload)).get("result", [])
                    for update in updates:
                        offset = update["update_id"] + 1
                        save_offset(offset)
                        task = asyncio.create_task(handle_update(client, web, update), name=f"telegram-update-{offset}")
                        tasks.add(task)
                        task.add_done_callback(tasks.discard)
                        task.add_done_callback(report_task)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("telegram_poll_error")
                    await asyncio.sleep(3)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def healthcheck():
    if not WEB:
        return 1
    async with httpx.AsyncClient(base_url=WEB, timeout=10) as web:
        r = await web.get("/health")
        r.raise_for_status()
    return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    if args.healthcheck:
        raise SystemExit(asyncio.run(healthcheck()))
    raise SystemExit(asyncio.run(run_bot()))


if __name__ == "__main__":
    main()
