import httpx
import pytest

from app.config import Settings
from app.portal_client import PortalClient, PortalError, RateExpired

SETTINGS = Settings(langfuse_enabled=False, hackloop_api_key="team-key")


def _rate_json(params: httpx.QueryParams, total: str) -> dict:
    weight = float(params["weight_kg"])
    return {
        "rate_id": f"R-{params['airline']}{int(weight)}xxxxxxxx",
        "airline": "Xenon Cargo Airlines", "airline_code": params["airline"],
        "origin": params["origin"], "destination": params["destination"],
        "weight_kg": params["weight_kg"],
        "chargeable_weight_kg": str(max(weight, float(params["volume_cbm"]) * 167)),
        "total": total, "currency": "EUR", "transit_days": 3,
        "valid_until": "2026-11-21T15:34:18+00:00",
    }  # fmt: skip


class _FakeTime:
    """A clock that only moves when the client sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _client(handler, fake_time: _FakeTime | None = None, **settings) -> PortalClient:
    fake_time = fake_time or _FakeTime()
    return PortalClient(
        SETTINGS.model_copy(update=settings),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        sleep=fake_time.sleep,
        clock=fake_time.clock,
    )


@pytest.mark.anyio
async def test_limiter_holds_back_the_eleventh_rate_read() -> None:
    fake_time = _FakeTime()
    portal = _client(lambda r: httpx.Response(200, json=_rate_json(r.url.params, "100.00")), fake_time)
    for _ in range(10):
        await portal.rate("CGN", "ORD", "XC", 679, 4.19)
    assert fake_time.sleeps == []
    await portal.rate("CGN", "ORD", "XC", 679, 4.19)
    assert fake_time.sleeps == [60.0]


@pytest.mark.anyio
async def test_rate_waits_retry_after_on_429_and_gives_up_after_three_retries() -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json=_rate_json(request.url.params, "1483.43"))

    fake_time = _FakeTime()
    option = await _client(flaky, fake_time).rate("CGN", "ORD", "XC", 679, 4.19)
    assert (option.total_eur, option.billed_kg, option.rate_id[:2]) == (1483.43, 699.73, "R-")
    assert fake_time.sleeps == [7.0]

    always_429 = _client(lambda r: httpx.Response(429, headers={"Retry-After": "1"}))
    with pytest.raises(PortalError):
        await always_429.rate("CGN", "ORD", "XC", 679, 4.19)


@pytest.mark.anyio
async def test_booking_410_raises_rate_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if b"R-expired" in request.content:
            return httpx.Response(410, json={"detail": "rate expired"})
        return httpx.Response(201, json={"booking_reference": "BK-TEST1234", "team_reference": "947"})

    portal = _client(handler)
    with pytest.raises(RateExpired):
        await portal.book("R-expired", "947")
    assert (await portal.book("R-fresh", "947"))["booking_reference"] == "BK-TEST1234"


@pytest.mark.anyio
async def test_portal_options_takes_the_next_break_when_it_is_cheaper() -> None:
    rate_reads: list[tuple[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if request.url.path.endswith("/ports"):
            return httpx.Response(200, json={"lanes": [{
                "origin": "CGN", "destination": "ORD",
                "airlines": [{"code": "AU", "name": "Aurelia"}, {"code": "XC", "name": "Xenon"}],
            }]})  # fmt: skip
        if request.url.path.endswith("/brackets"):
            return httpx.Response(200, json={"breaks_kg": [0, 45, 100, 250, 500, 1000, 2000]})
        weight = float(params["weight_kg"])
        rate_reads.append((params["airline"], weight))
        # AU: billing 1000 kg is cheaper than the real 966.93 kg; XC: it is not.
        totals = {("AU", 676.0): "2600.00", ("AU", 1000.0): "2494.80",
                  ("XC", 676.0): "2700.00", ("XC", 1000.0): "2800.00"}  # fmt: skip
        return httpx.Response(200, json=_rate_json(params, totals[(params["airline"], weight)]))

    portal = _client(handler)
    options = {o.airline_code: o for o in await portal.portal_options("CGN", "ORD", 676, 5.79)}
    assert (options["AU"].total_eur, options["AU"].billed_kg) == (2494.80, 1000.0)
    assert (options["XC"].total_eur, options["XC"].billed_kg) == (2700.00, 966.93)
    assert len(rate_reads) == 4

    # 250 kg: the next break (500) is twice the weight, so it is not worth a rate read
    rate_reads.clear()
    assert await portal.portal_options("FRA", "ORD", 250, 1.0) == []  # lane not on the portal
    assert rate_reads == []
