import pytest

from trade_agent.execution.ids import (
    BINANCE_CLIENT_ID,
    ClientIdParts,
    Leg,
    is_agent_id,
    make_client_id,
    new_decision_id,
    order_ids,
    parse_client_id,
)


def test_new_decision_id_is_hex_and_unique() -> None:
    ids = {new_decision_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(len(i) == 10 and int(i, 16) >= 0 for i in ids)


def test_make_and_parse_roundtrip() -> None:
    value = make_client_id("mod", "7f3a9c2b1d", Leg.TAKE_PROFIT, seq=3)
    assert value == "ta1-mod-7f3a9c2b1d-3-TP"
    assert BINANCE_CLIENT_ID.match(value)
    assert parse_client_id(value) == ClientIdParts("mod", "7f3a9c2b1d", 3, Leg.TAKE_PROFIT)
    assert is_agent_id(value)


def test_order_ids_are_valid_and_distinct() -> None:
    ids = order_ids("agressivo", "abcdef123456", seq=99)
    values = [ids.list_id, ids.entry_id, ids.take_profit_id, ids.stop_id, ids.exit_id]
    assert len(set(values)) == 5
    for value in values:
        assert BINANCE_CLIENT_ID.match(value)
        assert len(value) <= 36


@pytest.mark.parametrize(
    ("profile", "decision", "seq"),
    [
        ("Mod", "abcdef", 0),
        ("nome-com-hifen", "abcdef", 0),
        ("umnomemuitolongo", "abcdef", 0),
        ("mod", "xyz123", 0),
        ("mod", "abc", 0),
        ("mod", "abcdef", 100),
        ("mod", "abcdef", -1),
    ],
)
def test_make_client_id_rejects_invalid(profile: str, decision: str, seq: int) -> None:
    with pytest.raises(ValueError, match=r"inválid|intervalo"):
        make_client_id(profile, decision, Leg.LIST, seq)


@pytest.mark.parametrize(
    "value",
    ["web_abc123", "ta1-mod-abcdef-0-ZZ", "ta2-mod-abcdef-0-L", "ta1-mod-abcdef-100-L", ""],
)
def test_foreign_ids_are_not_agent_ids(value: str) -> None:
    assert parse_client_id(value) is None
    assert not is_agent_id(value)
