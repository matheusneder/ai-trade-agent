# Contract tests against external APIs

Tests that call **live third-party services** to catch a change of contract before it breaks
the agent. They are not integration tests: the integration suite (`tests/integration`) runs
the agent against simulated services and is deterministic; these depend on the network and
on someone else's API, so they live apart, one folder per API.

| Folder | API | Why a contract test |
| --- | --- | --- |
| `binance_copy_trading/` | The Spot Copy Trading endpoints of Binance's website (`www.binance.com/bapi/...`) that the lead tracker reads (D-052; doc 01, §2.6) | Undocumented and with no guarantee: Binance can change a field, a unit, the order of the ranking or the error envelope at any time, and only a live call shows it |

## Running

```bash
uv run pytest tests/contract                     # all of them
uv run pytest tests/contract -m contract         # the same, by marker
uv run pytest tests/contract/binance_copy_trading -k ranking
```

The default `uv run pytest` deselects them (`conftest.py` here), and CI does not run them:
they run when the run names this folder, a file or a test in it, or selects `-m contract`.
A machine the API refuses (HTTP 403, 418, 429 or 451, e.g. a US address) or with no
connection skips them, with the reason: there is nothing to check from there. The Binance
suite makes about 25 requests, half a second apart, with the agent's own client and headers.

## When one fails

The failing test names what changed (a missing field, another unit, another order...). Fix
the parser or the assumption it checks (`src/trade_agent/leads/source.py`, `ranking.py`,
`consensus.py`), then the unit tests of `tests/unit/leads` with the new shape, and run these
again. Until then the agent keeps working safely: an answer out of the expected shape is a
failed reading, and without a fresh reading the `lead_consensus` profile opens no entry
(runbook, §2.8).
