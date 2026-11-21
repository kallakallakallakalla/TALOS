# TALOS docs

TALOS acts as an **air-freight forwarder** in a scored hackathon simulation ("Hackloop" / START Hack, API at `https://api.start-hack.5u.ai`). Simulated customers email us requests for quotes. We price each one by combining a **trucker pickup** (from the customer's postal code to a German airport) with **air freight** (from that airport to the destination). We reply with one all-in price in EUR and **book** the shipment once the customer accepts.

## Start here

| File | Contents |
|---|---|
| **[HANDOFF.md](HANDOFF.md)** | **For the implementer:** read order, current state, rules, traps, open items, session timeline, later backlog |
| **[workflow.md](workflow.md)** | **The agreed implementation plan:** architecture, flow, state machine, loop guards, data model, config, module specs, build order, cutover |
| **[implementation.md](implementation.md)** | **What was built (steps 0–8), deviations from the plan, test and replay results, what is still open** |
| [decisions.md](decisions.md) | All 36 architecture and product decisions, with context and who decided |
| [prompts.md](prompts.md) | v1 drafts of all system prompts (classifier, extractors, composer) |
| [platform-api.md](platform-api.md) | Every API endpoint, limit and error, the webhook, booking, the LLM gateway |
| [data.md](data.md) | Start kit: carrier list, pickup zones, the 10 airline tariff files, pricing formula |
| [mailbox-analysis.md](mailbox-analysis.md) | The mails of rounds 1–7, what was sent, how it was scored |
| [scoring.md](scoring.md) | How points are earned and what that means for the design |
| [../vocab.md](../vocab.md) | Logistics vocabulary |

## Key findings from the exploration (2026-11-21, 16:35 CET)

These are what the exploration found. Where a later decision changed how we act on a finding, the decision is linked.

1. **The team wasn't booking.** Booking is a separate API call (`POST /v1/portal/bookings` with the `rate_id`). Four customers had asked us to book and nothing happened. Round 6 was booked by hand during the session (`BK-8RCF17DZ`); since then, bookings may only happen in code ([ADR 07](decisions.md#07--book-automatically-on-acceptance-api-for-portal-airlines-email-for-tariff-airlines), [ADR 24](decisions.md#24--no-manual-bookings-booking-belongs-in-code)).
2. **Both early rejections ("too_low") were quotes without trucking.** All-in means trucker pickup + air freight (+ fuel + security).
3. **An older version on another computer sends with the same team key.** This repo replaces it at a round boundary ([ADR 02](decisions.md#02--this-repo-replaces-the-old-process-on-the-other-computer)).
4. **LLM cost is 40 of 100 points per mail.** We still decided to have an LLM read and write every mail ([ADR 12](decisions.md#12--an-llm-reads-every-mail-no-regex-on-mail-text)–[15](decisions.md#15--classifier-first-then-focused-extractors)), on `gpt-6-luna`. Langfuse shows the cost per call, and the model is set per task.
5. **The old classifier mislabels mail without context.** It labelled 5 trucker quotes "RFQ" and customer acceptances "update". The fix is a context lookup (sender role + thread) passed to a classifier with new categories ([ADR 15](decisions.md#15--classifier-first-then-focused-extractors), [16](decisions.md#16--context-lookup-no-llm-before-the-classifier)).
6. **There is no truck-only business:** every request is an air export from Germany ([ADR 01](decisions.md#01--scope-air-export-with-optional-trucker-pickup-no-truck-only-routes)).
7. **Truckers serve postal zones, not airports.** A trucker's `serves` column lists 2-digit postal prefixes, and `pickup_zones.csv` maps each prefix to exactly one airport ([ADR 05](decisions.md#05--ask-only-the-truckers-that-serve-the-postal-zone)).
8. **Airline data is split into groups that don't overlap:** 15 airlines on the portal, 10 only in the tariff files (the local DB), 4 email-only for exotic destinations ([ADR 06](decisions.md#06--portal-api-and-local-tariff-db-are-equal-price-sources)).
9. **There is a webhook push** (`PUT /v1/mailbox/webhook`), found by comparing the old `openapi.json` with the live one. We poll every 2 s for now ([ADR 04](decisions.md#04--poll-the-mailbox-every-2-s-webhook-later)).
10. **Timing:** a round arrives about every 6 minutes and closes 6 minutes after it is sent. Truckers answer after about 30 s. Rates and trucker offers are valid for about 15 minutes. **The 10/min rate limit applies only to `/portal/rates`.**
