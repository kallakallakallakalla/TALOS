"""Client for the airline rate portal: lanes, weight breaks, rates and bookings.

Its JSON responses are read directly; no LLM is involved.
"""

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from langfuse import observe

from app import pricing
from app.config import Settings
from app.schemas import AirOption

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 60.0
_MAX_429_RETRIES = 3
# ponytail: the next weight break is only priced when it is at most 50% above the chargeable
# weight. In every start-kit tariff a rate drops by less than 35% per break, so a break further
# away cannot be cheaper, and each skipped read saves one of the 10 reads per minute.
_NEXT_BREAK_MAX_FACTOR = 1.5


class PortalError(Exception):
    """The portal answered with an error we cannot act on."""


class PortalDown(PortalError):
    """503 that did not go away after one retry, or "contact the airline directly" (ADR 47)."""


def _says_contact_airline(response: httpx.Response) -> bool:
    return "airline directly" in response.text.lower()


class RateExpired(PortalError):
    """Booking returned 410: the rate_id is older than ~15 minutes. Fetch a fresh rate."""


class PortalClient:
    """Rate portal API. `rate()` is throttled to the shared 10 reads per minute."""

    def __init__(
        self,
        settings: Settings,
        http_client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._http_client = http_client
        self._sleep = sleep
        self._clock = clock
        self._rate_calls: deque[float] = deque()
        self._rate_lock = asyncio.Lock()
        self._lanes: dict[tuple[str, str], list[dict[str, str]]] | None = None
        self._brackets: dict[str, list[float]] = {}

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self._settings.hackloop_api_key:
            raise ValueError("HACKLOOP_API_KEY is required to call the portal API.")
        url = f"{self._settings.hackloop_base_url.rstrip('/')}{path}"
        headers = {"X-API-Key": self._settings.hackloop_api_key}
        if self._http_client is not None:
            return await self._http_client.request(method, url, headers=headers, **kwargs)
        async with httpx.AsyncClient(timeout=self._settings.portal_timeout_seconds) as client:
            return await client.request(method, url, headers=headers, **kwargs)

    async def lanes(self) -> dict[tuple[str, str], list[dict[str, str]]]:
        """`(origin, destination) -> airlines` for every lane the portal prices. Cached."""
        if self._lanes is None:
            cache = Path(self._settings.portal_lanes_cache_path)
            response = await self._request("GET", "/v1/portal/ports")
            if response.status_code == 503 and not _says_contact_airline(response):
                await self._sleep(self._settings.portal_unavailable_retry_seconds)  # ADR 47
                response = await self._request("GET", "/v1/portal/ports")
            if response.status_code == 200:
                raw = response.json()["lanes"]
                cache.write_text(json.dumps(raw))
            elif cache.exists():  # the lane list never changes; the last copy is good
                logger.warning("Portal lanes unavailable (%s); using %s", response.status_code, cache)
                raw = json.loads(cache.read_text())
            elif response.status_code == 503:
                raise PortalDown(f"ports: {response.text[:200]}")
            else:
                response.raise_for_status()
            self._lanes = {(lane["origin"], lane["destination"]): lane["airlines"] for lane in raw}
        return self._lanes

    async def brackets(self, airline_code: str) -> list[float]:
        """Weight breaks of an airline, ascending. Cached; not rate limited."""
        if airline_code not in self._brackets:
            response = await self._request(
                "GET", "/v1/portal/brackets", params={"airline": airline_code}
            )
            response.raise_for_status()
            self._brackets[airline_code] = sorted(float(b) for b in response.json()["breaks_kg"])
        return self._brackets[airline_code]

    async def _take_rate_slot(self) -> None:
        """Wait until one more rate read fits into the last minute."""
        async with self._rate_lock:
            limit = max(1, self._settings.portal_rate_limit_per_minute)
            while True:
                now = self._clock()
                while self._rate_calls and now - self._rate_calls[0] >= _WINDOW_SECONDS:
                    self._rate_calls.popleft()
                if len(self._rate_calls) < limit:
                    self._rate_calls.append(now)
                    return
                await self._sleep(self._rate_calls[0] + _WINDOW_SECONDS - now)

    @observe(name="portal.rate")
    async def rate(
        self, origin: str, destination: str, airline_code: str, weight_kg: float, volume_cbm: float
    ) -> AirOption:
        """One priced rate: freight + fuel + security, airport to airport, valid ~15 minutes."""
        params = {
            "origin": origin,
            "destination": destination,
            "airline": airline_code,
            "weight_kg": weight_kg,
            "volume_cbm": volume_cbm,
        }
        retries_429, retried_503 = 0, False
        while True:
            await self._take_rate_slot()
            response = await self._request("GET", "/v1/portal/rates", params=params)
            if response.status_code == 503 and not retried_503 and not _says_contact_airline(response):
                # The rate API is down for a while (the portal page has the same data): wait
                # once, then ask again (ADR 47).
                retried_503 = True
                await self._sleep(self._settings.portal_unavailable_retry_seconds)
            elif response.status_code == 429 and retries_429 < _MAX_429_RETRIES:
                retries_429 += 1
                await self._sleep(float(response.headers.get("Retry-After", "5")))
            else:
                break
        if response.status_code == 503:
            raise PortalDown(f"rate {origin}-{destination} {airline_code}: {response.text[:200]}")
        if response.status_code != 200:
            raise PortalError(f"rate {origin}-{destination} {airline_code}: {response.status_code} {response.text[:200]}")

        data = response.json()
        total = float(data["total"])
        if data.get("currency", "EUR") != "EUR":
            fx = data.get("fx_eur_per_usd")
            if data["currency"] != "USD" or not fx:
                raise PortalError(f"rate in {data['currency']} without an exchange rate")
            total = round(total * float(fx), 2)
        return AirOption(
            source="portal",
            airline=data["airline"],
            airline_code=data["airline_code"],
            origin=data["origin"],
            destination=data["destination"],
            billed_kg=float(data["chargeable_weight_kg"]),
            total_eur=total,
            transit_days=int(data["transit_days"]),
            rate_id=data["rate_id"],
            valid_until=data.get("valid_until"),
            weight_kg=weight_kg,
            volume_cbm=volume_cbm,
        )

    @observe(name="portal.book")
    async def book(self, rate_id: str, team_reference: str) -> dict[str, Any]:
        """Book a rate. Safe to repeat with the same pair (the portal returns the booking)."""
        response = await self._request(
            "POST", "/v1/portal/bookings", json={"rate_id": rate_id, "team_reference": team_reference}
        )
        if response.status_code == 410:
            raise RateExpired(rate_id)
        if response.status_code not in (200, 201):
            raise PortalError(f"booking {rate_id}: {response.status_code} {response.text[:200]}")
        return response.json()

    async def _airline_option(
        self, origin: str, destination: str, code: str, weight_kg: float, volume_cbm: float
    ) -> AirOption | None:
        """Best rate of one airline: the real weight, or the next weight break if cheaper."""
        try:
            best = await self.rate(origin, destination, code, weight_kg, volume_cbm)
            chargeable = pricing.chargeable_kg(weight_kg, volume_cbm)
            higher = [b for b in await self.brackets(code) if b > chargeable]
            if higher and higher[0] <= chargeable * _NEXT_BREAK_MAX_FACTOR:
                at_break = await self.rate(origin, destination, code, higher[0], volume_cbm)
                best = min(best, at_break, key=lambda option: option.total_eur)
            return best
        except PortalDown:
            raise
        except (PortalError, httpx.HTTPError) as exc:
            logger.warning("No portal rate from %s for %s-%s: %s", code, origin, destination, exc)
            return None

    async def portal_options(
        self, origin: str, destination: str, weight_kg: float, volume_cbm: float
    ) -> list[AirOption]:
        """Every portal airline on the lane, each at its cheapest billable weight.
        Raises PortalDown when the portal is down and gave no price at all (ADR 47)."""
        airlines = (await self.lanes()).get((origin, destination), [])
        results = await asyncio.gather(
            *(self._airline_option(origin, destination, a["code"], weight_kg, volume_cbm) for a in airlines),
            return_exceptions=True,
        )
        options = [r for r in results if isinstance(r, AirOption)]
        down = [r for r in results if isinstance(r, PortalDown)]
        for other in (r for r in results if isinstance(r, BaseException) and not isinstance(r, PortalDown)):
            raise other
        if down and not options:
            raise down[0]
        return options
