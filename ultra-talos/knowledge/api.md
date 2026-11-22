# Platform API

- **Base URL:** `https://api.start-hack.5u.ai`
- **Auth:** header `X-API-Key: <HACKLOOP_API_KEY>` (or `Authorization: Bearer …`). The key is in `router/.env`.
- **Our identity:** `GET /v1/me` returns `{"team":"talos","display_name":"TALOS","mailbox":"talos@hackloop.mail"}`.
- **Full spec:** `openapi.json` in the repo root, refreshed from `/openapi.json` on 2026-11-21. The live docs are at `/docs`.
- **Leaderboard (HTML):** `https://api.start-hack.5u.ai/board`

## Mailbox

### Poll: `GET /v1/mailbox/messages?since_id=N&limit=1..200`

- Returns mail sent **to** us with `id > since_id`, oldest first. Keep `next_since_id` for the next call.
- Message ids are **global across all teams**, so ours are sparse (275, 379, 412…).
- Everything arrives here: customer requests, carrier bids, rate desk answers, booking confirmations, airline tariff mails and tracking questions.
- The docs say: "response time counts: poll every second or two." The current router polls every 10 s; the plan changes that to **2 s** ([ADR 04](decisions.md#04--poll-the-mailbox-every-2-s-webhook-later)). The mailbox has no stated rate limit.

Message shape:

```json
{"id":275,"from":"lindqvist-medtech@hackloop.mail","to":"talos@hackloop.mail",
 "subject":"…","body":"…","in_reply_to":null,"references":[],
 "received_at":"2026-11-21T15:00:59.986067+00:00","attachments":[]}
```

`references` lists the thread's ids from the first mail down to the parent. `attachments` lists `id, filename, content_type, size`. Download each with `GET /v1/mailbox/messages/{id}/attachments/{attachment_id}`.

`GET /v1/mailbox/messages/{id}` returns a single mail, **including mail we sent**. This is how we read our own outgoing quotes.

### Send: `POST /v1/mailbox/messages`

```json
{"to":"customer@hackloop.mail","subject":"Re: …","body":"…","in_reply_to":275,
 "attachments":[{"filename":"x.pdf","content_type":"application/pdf","content_base64":"…"}]}
```

- **A quote counts only as a reply in a thread a customer started, and only our first quote in a thread counts.** Set `in_reply_to`, and send exactly one quote.
- Allowed recipients: customers, the airlines and truckers in `carrier-list.csv`, `ratedesk@hackloop.mail` and `tracking@hackloop.mail`. Any other address returns 422.
- Attachments: at most 5 per mail, 10 MB each. Allowed types: pdf, xlsx, xls, csv, txt, png, jpg, docx.
- Limit: 60 mails per minute, then 429.

### Webhook (new; not in the original local openapi.json)

- `PUT /v1/mailbox/webhook` with `{"url":"https://…","secret":"optional, at least 16 chars"}` subscribes us. The `secret` in the response is shown only once.
- Each new mail arrives as `POST {"event":"mail.received","message":{…poll item…}}`. Headers: `X-Webhook-Event`, `X-Webhook-Id` (the mail id), `X-Webhook-Timestamp`, `X-Webhook-Signature`.
- Signature: `"sha256=" + hex(HMAC_SHA256(secret, timestamp + "." + raw_body))`. Reject a wrong signature or an old timestamp.
- We must answer 2xx within 5 s and do the work afterwards. Otherwise the server retries 3 times within about 15 s and then drops the push. The same push can arrive twice or out of order, so deduplicate by `message.id`. Keep a slow poll as a safety net.
- The URL must be public (a tunnel is fine). Redirects aren't followed.
- `GET /v1/mailbox/webhook` shows the status and the last 20 pushes. `DELETE` unsubscribes. `POST /v1/mailbox/webhook/test` sends a ping.

## Rate portal (airline prices)

### `GET /v1/portal/ports`

Returns every lane the portal can price: `{"lanes":[{"origin":"CGN","destination":"DEL","airlines":[{"code":"JP","name":"Juniper Cargo Lines"}…]}]}`. This call doesn't count against the rate limit.

- There are **114 lanes** in total, by origin: CGN 12, DUS 17, FRA 34, HAM 14, MUC 24, STR 13.
- The portal covers **15 airlines**: AU Aurelia, BX Borealis, FQ Fjordline, GN Gannet, IB Ibisair, JP Juniper, LM Lumen, MD Meridian, NV Nova Atlas, PL Polaris, RV Riverbend, SL Solace, TM Tamarind, UV Ultraviolet, XC Xenon.
- The 36 destinations are ATL BKK BOG BOM CAI DEL DFW DOH DXB EZE GRU HKG ICN JFK JNB KUL LAX LIM LOS MEL MEX MIA MNL NBO NRT ORD PEK PVG RUH SCL SGN SIN SYD TLV TPE YYZ.
- "Destinations missing here are priced by email to the airline."

### `GET /v1/portal/brackets?airline=FQ`

Returns `{"airline":"Fjordline Air Cargo","airline_code":"FQ","breaks_kg":[0,45,100,250,500,1000,2000]}`. This call doesn't count against the rate limit.

- Airline lookup takes the **code or the exact full name**. "polaris" returns 404; "PL" or "Polaris Air Logistics" works.
- For an airline that isn't on the portal, asking by full name returns a 404 that names the email address to use instead.

### `GET /v1/portal/rates?origin=DUS&destination=JFK&airline=FQ&weight_kg=1540&volume_cbm=5.9`

```json
{"rate_id":"R-37nHKFJKWL4u5D21","airline":"Fjordline Air Cargo","airline_code":"FQ",
 "origin":"DUS","destination":"JFK","weight_kg":"1540","chargeable_weight_kg":"1540",
 "total":"3696.00","currency":"EUR","transit_days":3,
 "valid_until":"2026-11-21T15:49:05+00:00","dataset_version":"v1"}
```

- `total` is freight + fuel + security, airport to airport. **Trucking is not included.**
- `chargeable_weight_kg` = `max(weight_kg, volume_cbm × 167)`.
- The portal prices only the weight we ask for. To apply the anti-weight-break check we query the next break weight ourselves.
- The currency may not be EUR. In that case `fx_eur_per_usd` gives the exchange rate, and we must convert.
- **Rate limit: 10 rate reads per minute** (header `X-RateLimit-Limit: 10`). Over the limit we get 429 with `Retry-After`. This is the main reason to have a local cache or DB.
- A `rate_id` is valid for about **15 minutes**.
- 503 means the portal is unavailable; the message says where to get the rate instead.
- For a destination the portal doesn't serve (e.g. ULN), the 404 lists the airlines to email.

### `POST /v1/portal/bookings`

Takes `{"rate_id":"R-…","team_reference":"<our CRM id>"}`.

| Status | Meaning |
|---|---|
| 201 | Booked; `booking_reference` = `BK-xxxxxxxx` |
| 200 | Retry with the same pair; returns the existing booking |
| 404 | Unknown `rate_id` |
| 409 | The `rate_id` is already booked under another reference |
| 410 | The rate expired; fetch a fresh rate and book that |
| 503 | The booking system is down; the message says how to book by email |

- Afterwards the airline sends a "Booking <ref> confirmed" mail.
- There is no status endpoint, so we keep track of bookings ourselves.
- Example: `team_reference` "1470" was booked as `BK-8RCF17DZ`.
- Current OpenAPI also defines `DELETE /v1/portal/bookings/{booking_reference}` to cancel a booking. It returns the booking reference, team reference and `status: "cancelled"`; repeated cancellation is idempotent.

## LLM gateway (OpenAI-compatible)

- `base_url = https://api.start-hack.5u.ai/v1/gateway`, and the `api_key` is the team key. Our own OpenAI key is registered once via `POST /v1/gateway/openai-key`.
- Endpoints: `/chat/completions`, `/responses` and `/embeddings`. `GET /v1/gateway/models` lists the available models, including gpt-4.1(-mini), gpt-4o(-mini), gpt-5.x, gpt-5.4-nano/mini, gpt-5.6-luna/sol/terra, gpt-6-luna/sol/astra, gpt-6.1/6.2-*, o3 and the embeddings models.
- **LLM cost is scored**, presumably measured through this gateway (see scoring.md). **Decided: `gpt-6-luna` for every call** ([ADR 14](decisions.md#14--gpt-6-luna-for-every-llm-call)). Note that `gpt-6.1-terra` exists on the gateway, but `gpt-6-terra` does not.

## Board

- `GET /v1/board` is public: ranking, rounds and bookings.
- `GET /v1/board/me` shows our verdict per request (`accepted`, `rejected`, `pending`, `open`, `no answer`, `voided`), the `reason` (e.g. `too_low`), and `booked`.
