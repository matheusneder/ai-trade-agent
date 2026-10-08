"""External heartbeat (e.g. Healthchecks.io): without pings, the external service alerts
that the agent stopped. The URL contains the check's secret identifier and is never logged."""

import httpx
import structlog

log = structlog.get_logger(__name__)


class Heartbeat:
    def __init__(self, http: httpx.AsyncClient, url: str) -> None:
        self._http = http
        self._url = url

    async def ping(self) -> bool:
        """Sends the ping; failures are recorded (without the URL) and never propagated."""
        try:
            response = await self._http.get(self._url, timeout=10)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log.warning("heartbeat.failed", status=exc.response.status_code)
            return False
        except httpx.HTTPError as exc:
            log.warning("heartbeat.failed", error=type(exc).__name__)
            return False
        log.debug("heartbeat.ok", status=response.status_code)
        return True
