import httpx
from langfuse import observe

from app.config import Settings
from app.schemas import InputPayload

PLACEHOLDER_INPUTS: dict[str, InputPayload] = {
    "sample-1": InputPayload(
        id="sample-1",
        source="placeholder",
        text=(
            "Subject: Request for Quote - 500x Industrial Sensors\n\n"
            "Hi team, could you please send us a quote including lead time and "
            "unit pricing for 500 units of model X-200 delivered to Munich?"
        ),
        metadata={"from": "buyer@acme-mfg.de", "channel": "email"},
    ),
    "sample-2": InputPayload(
        id="sample-2",
        source="placeholder",
        text=(
            "Subject: Shipment Tracking #TRK-99412\n\n"
            "Your order #4510 has been dispatched via DHL Freight. "
            "Tracking number: TRK-99412, estimated arrival tomorrow."
        ),
        metadata={"from": "logistics@supplier.com", "channel": "email"},
    ),
    "sample-3": InputPayload(
        id="sample-3",
        source="placeholder",
        text=(
            "Subject: Question about ISO certification\n\n"
            "Hello, is the X-200 sensor certified for ATEX Zone 2 environments?"
        ),
        metadata={"from": "engineer@client.io", "channel": "email"},
    ),
}


class InputApiClient:
    """Fetches inputs from an upstream API, falling back to placeholder data for the MVP."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @observe(name="fetch-input")
    async def fetch_input(self, input_id: str | None = None) -> InputPayload:
        """Fetch an input item from the configured API or return a placeholder sample."""
        if not self._settings.input_api_url:
            return self._fetch_placeholder(input_id)

        headers: dict[str, str] = {}
        if self._settings.input_api_token:
            headers["Authorization"] = f"Bearer {self._settings.input_api_token}"

        params = {"id": input_id} if input_id else None
        async with httpx.AsyncClient(timeout=self._settings.input_api_timeout_seconds) as client:
            response = await client.get(
                self._settings.input_api_url,
                headers=headers,
                params=params,
            )
            response.raise_for_status()
            return InputPayload.model_validate(response.json())

    def _fetch_placeholder(self, input_id: str | None) -> InputPayload:
        if input_id and input_id in PLACEHOLDER_INPUTS:
            return PLACEHOLDER_INPUTS[input_id]
        if input_id:
            return InputPayload(
                id=input_id,
                source="placeholder",
                text=PLACEHOLDER_INPUTS["sample-1"].text,
                metadata={"requested_id": input_id, "fallback": True},
            )
        return PLACEHOLDER_INPUTS["sample-1"]
