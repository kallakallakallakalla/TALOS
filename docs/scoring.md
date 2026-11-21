# Scoring (from the leaderboard page)

> Each accepted email earns up to 100 points: **45 for price accuracy, 40 for LLM cost and 15 for response time.**
> **+10 most human email:** a jury reads the accepted emails of each round with the team names hidden and picks one.
> **+10 tracking:** up to this many for a correct tracking answer on a booked shipment.
> Rejected and missed emails earn nothing. Voided rounds count for nobody. Cost points arrive once an hour, a few minutes after the full hour.

Structure of the game:

- A round is sent about every 6 minutes. **Each round closes 6 minutes after it is sent**, so a late answer counts as "no answer".
- Only the **first quote in a customer thread** counts.
- 14 teams are playing.

Standing at 15:35 UTC: TALOS was 12th with 220.62 points, 4 of 6 accepted and 0 bookings. The leader had 400 points, 6 of 6 accepted and 6 bookings. The top teams answer every round and book every accepted quote.

## What this means for the design

| Lever | Points | Design consequence |
|---|---|---|
| Accepted at all | gate | The price must be in range. Leaving out trucking = `too_low`; a big margin probably = `too_high`. |
| Price accuracy | 45 | Presumably closeness to a reference price (cheapest or fastest correct option). Probably requires choosing the truly cheapest or fastest option across portal, local DB and truckers, with anti-weight-break applied. **Decided: margin 0** ([ADR 09](decisions.md#09--margin-0)). |
| LLM cost | 40 | The first analysis suggested a minimal LLM. **Decided instead:** an LLM reads and writes every mail, on `gpt-6-luna`, with a classifier plus focused extractors ([ADR 12](decisions.md#12--an-llm-reads-every-mail-no-regex-on-mail-text)–[15](decisions.md#15--classifier-first-then-focused-extractors)). That's about 14 calls per request; Langfuse tracks the cost, and the model is set per task. |
| Response time | 15 | Use the webhook or a 1–2 s poll. The trucker round-trip (~30 s) is the bottleneck, so ask the truckers in parallel with the air pricing. |
| Human email | +10/round | Natural, friendly reply that answers the customer's extra questions (arrival date, drop-off time). **Decided:** written by the LLM composer with a human-tone prompt and a fact check ([ADR 13](decisions.md#13--an-llm-writes-every-mail-code-checks-the-facts)). |
| Tracking | +10 | Needs **bookings** first. Then answer tracking questions correctly (likely via `tracking@hackloop.mail`). |

Booking isn't listed as a points category, but the tracking points depend on it and the board counts bookings. Book as soon as a customer accepts.

Later observation (see the [HANDOFF.md](HANDOFF.md) backlog): bookings made long after the acceptance showed `booked: false` on `board/me`. Book immediately.
