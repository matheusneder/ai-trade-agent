import json

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from trade_agent.notify.notifier import LogNotifier, TelegramNotifier
from trade_agent.notify.telegram import MAX_MESSAGE, TelegramBot, TelegramError, parse_updates
from trade_agent.persistence.store import Severity

API = "https://tg.example/botTOKEN123"


def _bot(http: httpx.AsyncClient) -> TelegramBot:
    return TelegramBot(http, "TOKEN123", base_url="https://tg.example")


def test_parse_updates() -> None:
    updates = parse_updates(
        [
            {"update_id": 1, "message": {"chat": {"id": 42}, "text": "/status"}},
            {"update_id": 2, "edited_message": {"chat": {"id": 42}, "text": "/pause"}},
            {"update_id": 3, "channel_post": {}},
        ]
    )
    assert [(u.update_id, u.chat_id, u.text) for u in updates] == [
        (1, 42, "/status"),
        (2, 42, "/pause"),
        (3, None, ""),
    ]


async def test_send_message_and_get_updates() -> None:
    with respx.mock() as router:
        send = router.post(f"{API}/sendMessage").respond(200, json={"ok": True, "result": {}})
        updates = router.post(f"{API}/getUpdates").respond(
            200, json={"ok": True, "result": [{"update_id": 7, "message": {"chat": {"id": 1}}}]}
        )
        async with httpx.AsyncClient() as http:
            bot = _bot(http)
            await bot.send_message(42, "x" * (MAX_MESSAGE + 10))
            assert (await bot.get_updates(None, timeout_s=1))[0].update_id == 7
            await bot.get_updates(8, timeout_s=1)
    body = json.loads(send.calls[0].request.content)
    assert body["chat_id"] == 42 and len(body["text"]) == MAX_MESSAGE
    assert body["text"].endswith("…") and body["disable_web_page_preview"] is True
    first, second = (json.loads(c.request.content) for c in updates.calls)
    assert "offset" not in first and second["offset"] == 8
    assert first["allowed_updates"] == ["message"]


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            httpx.Response(401, json={"ok": False, "description": "Unauthorized"}),
            "401 Unauthorized",
        ),
        (httpx.Response(200, json={"ok": False}), "HTTP 200"),
        (httpx.Response(502, text="bad gateway"), "HTTP 502"),
    ],
)
async def test_api_errors_never_leak_the_token(response: httpx.Response, message: str) -> None:
    with respx.mock() as router:
        router.post(f"{API}/sendMessage").mock(return_value=response)
        async with httpx.AsyncClient() as http:
            with pytest.raises(TelegramError, match=message) as info:
                await _bot(http).send_message(1, "oi")
    assert "TOKEN123" not in str(info.value)


async def test_transport_errors_never_leak_the_token() -> None:
    with respx.mock() as router:
        router.post(f"{API}/getUpdates").mock(side_effect=httpx.ConnectError("sem rede"))
        async with httpx.AsyncClient() as http:
            with pytest.raises(TelegramError, match="getUpdates: ConnectError") as info:
                await _bot(http).get_updates(None)
    assert "TOKEN123" not in str(info.value)


async def test_notifiers() -> None:
    with capture_logs() as logs:
        await LogNotifier().notify(Severity.HIGH, "alerta")
    assert logs[0]["text"] == "alerta"
    with respx.mock() as router:
        send = router.post(f"{API}/sendMessage").mock(
            side_effect=[
                httpx.Response(200, json={"ok": True, "result": {}}),
                httpx.Response(500, json={"ok": False, "description": "falhou"}),
            ]
        )
        async with httpx.AsyncClient() as http:
            notifier = TelegramNotifier(_bot(http), 42, min_severity=Severity.HIGH)
            await notifier.notify(Severity.INFO, "ignorado")  # abaixo do mínimo
            await notifier.notify(Severity.CRITICAL, "urgente")
            with capture_logs() as logs:
                await notifier.notify(Severity.HIGH, "não chega")  # falha não propaga
    assert len(send.calls) == 2
    assert json.loads(send.calls[0].request.content)["text"] == "🚨 urgente"
    assert logs[0]["event"] == "alert.telegram_failed"
