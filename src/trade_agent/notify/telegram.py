"""Cliente fino da Bot API do Telegram (``sendMessage`` e ``getUpdates`` com long polling).

O token faz parte da URL da API: mensagens de erro nunca incluem a URL.
"""

from dataclasses import dataclass
from typing import Any

import httpx

API_URL = "https://api.telegram.org"
MAX_MESSAGE = 4096


class TelegramError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Update:
    update_id: int
    chat_id: int | None
    text: str


def parse_updates(payload: list[dict[str, Any]]) -> list[Update]:
    updates: list[Update] = []
    for raw in payload:
        message = raw.get("message") or raw.get("edited_message") or {}
        chat = message.get("chat") or {}
        updates.append(
            Update(
                update_id=int(raw["update_id"]),
                chat_id=int(chat["id"]) if "id" in chat else None,
                text=str(message.get("text") or ""),
            )
        )
    return updates


class TelegramBot:
    def __init__(self, http: httpx.AsyncClient, token: str, *, base_url: str = API_URL) -> None:
        self._http = http
        self._url = f"{base_url}/bot{token}"

    async def _call(self, method: str, payload: dict[str, Any], wait_s: float) -> Any:
        try:
            response = await self._http.post(f"{self._url}/{method}", json=payload, timeout=wait_s)
        except httpx.HTTPError as exc:
            raise TelegramError(f"{method}: {type(exc).__name__}") from None
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code != 200 or not data.get("ok"):
            description = data.get("description", response.reason_phrase)
            raise TelegramError(f"{method}: HTTP {response.status_code} {description}")
        return data["result"]

    async def send_message(self, chat_id: int, text: str) -> None:
        body = text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 1] + "…"
        await self._call(
            "sendMessage",
            {"chat_id": chat_id, "text": body, "disable_web_page_preview": True},
            wait_s=15,
        )

    async def get_updates(self, offset: int | None, *, timeout_s: int = 30) -> list[Update]:
        payload: dict[str, Any] = {"timeout": timeout_s, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        return parse_updates(await self._call("getUpdates", payload, wait_s=timeout_s + 10))
