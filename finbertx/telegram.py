from __future__ import annotations

from urllib.parse import urlencode

from finbertx.network import ApiError, request_json


class TelegramBot:
    def __init__(self, token: str, output_chat_id: str):
        self.token = token
        self.output_chat_id = output_chat_id
        self.offset = 0
        self.base_url = f"https://api.telegram.org/bot{token}"

    def send(self, text: str, chat_id: str | None = None) -> None:
        response = request_json(
            f"{self.base_url}/sendMessage",
            method="POST",
            payload={
                "chat_id": chat_id or self.output_chat_id,
                "text": text[:4000],
                "disable_web_page_preview": True,
            },
        ).data
        if not isinstance(response, dict) or not response.get("ok"):
            raise ApiError("Telegram rejected a message.")

    def updates(self) -> list[dict]:
        query = urlencode(
            {
                "offset": self.offset,
                "timeout": 0,
                "allowed_updates": '["message"]',
            }
        )
        response = request_json(f"{self.base_url}/getUpdates?{query}").data
        if not isinstance(response, dict) or not response.get("ok"):
            raise ApiError("Telegram could not fetch bot updates.")
        rows = response.get("result", [])
        if not isinstance(rows, list):
            return []
        if rows:
            self.offset = int(rows[-1].get("update_id", self.offset)) + 1
        return rows
