from __future__ import annotations

import json
import logging
import mimetypes
import uuid
from datetime import datetime
from pathlib import Path
from urllib import parse, request


log = logging.getLogger("prenotami")


def chunk_message(text: str, max_length: int = 4000) -> list[str]:
    """Split long Telegram messages without breaking lines when possible."""
    chunks: list[str] = []
    remaining = text

    while len(remaining) > max_length:
        split_at = remaining.rfind("\n", 0, max_length)
        if split_at <= 0:
            split_at = max_length
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")

    if remaining:
        chunks.append(remaining)

    return chunks


def write_notification_log(log_path: Path, subject: str, body: str) -> None:
    clean_body = body.replace("\\\\n", "\n").replace("\\n", "\n")
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(f"\n{'=' * 60}\n")
        handle.write(f"TIME: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        handle.write(f"SUBJECT: {subject}\n")
        handle.write(f"BODY:\n{clean_body}\n")
        handle.write(f"{'=' * 60}\n")


class TelegramClient:
    def __init__(self, bot_token: str, chat_id: str, offset_file: Path):
        self.bot_token = bot_token
        self.chat_id = str(chat_id)
        self.offset_file = offset_file
        self.offset = self._load_offset()

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def _load_offset(self) -> int:
        try:
            return int(self.offset_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return 0

    def _save_offset(self, offset: int) -> None:
        self.offset = offset
        self.offset_file.write_text(str(offset), encoding="utf-8")

    def _api_url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/{method}"

    def _post_form(self, method: str, payload: dict[str, object]) -> dict:
        data = parse.urlencode({k: v for k, v in payload.items() if v is not None}).encode("utf-8")
        req = request.Request(self._api_url(method), data=data, method="POST")
        with request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))

    def _post_multipart(
        self,
        method: str,
        fields: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
    ) -> dict:
        boundary = uuid.uuid4().hex
        body = bytearray()

        for name, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
            body.extend(value.encode("utf-8"))
            body.extend(b"\r\n")

        for field_name, (filename, content, content_type) in files.items():
            body.extend(f"--{boundary}\r\n".encode("utf-8"))
            body.extend(
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'.encode("utf-8")
            )
            body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
            body.extend(content)
            body.extend(b"\r\n")

        body.extend(f"--{boundary}--\r\n".encode("utf-8"))

        req = request.Request(self._api_url(method), data=bytes(body), method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        with request.urlopen(req, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))

    def send_message(self, text: str) -> None:
        if not self.enabled:
            log.warning("Telegram not configured; message skipped.")
            return

        try:
            for chunk in chunk_message(text):
                self._post_form(
                    "sendMessage",
                    {
                        "chat_id": self.chat_id,
                        "text": chunk,
                        "disable_web_page_preview": "true",
                    },
                )
        except Exception as exc:
            log.warning(f"Telegram sendMessage failed: {exc}")

    def send_photo(self, photo_path: Path, caption: str = "") -> None:
        if not self.enabled:
            log.warning("Telegram not configured; photo skipped.")
            return
        if not photo_path.exists():
            log.warning(f"Telegram photo skipped; file missing: {photo_path}")
            return

        try:
            mime_type = mimetypes.guess_type(photo_path.name)[0] or "application/octet-stream"
            payload = {
                "chat_id": self.chat_id,
                "caption": caption[:1024],
            }
            files = {
                "photo": (photo_path.name, photo_path.read_bytes(), mime_type),
            }
            self._post_multipart("sendPhoto", payload, files)
        except Exception as exc:
            log.warning(f"Telegram sendPhoto failed: {exc}")

    def get_updates(self, timeout: int = 0) -> list[dict]:
        if not self.enabled:
            return []

        try:
            response = self._post_form(
                "getUpdates",
                {
                    "offset": self.offset + 1,
                    "timeout": max(timeout, 0),
                    "allowed_updates": json.dumps(["message"]),
                },
            )
        except Exception as exc:
            log.warning(f"Telegram getUpdates failed: {exc}")
            return []

        if not response.get("ok"):
            log.warning(f"Telegram getUpdates returned error: {response}")
            return []

        updates = response.get("result", [])
        for update in updates:
            update_id = int(update.get("update_id", 0))
            if update_id > self.offset:
                self._save_offset(update_id)
        return updates
