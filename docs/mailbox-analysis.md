# Mailbox analysis (rounds 1–6, 2026-11-21 15:00–15:35 UTC)

Mail started arriving at 15:00 UTC (16:00 CET). By 15:35 UTC we had received **24 mails**:

- 6 customer requests (one per round)
- 6 customer verdict replies
- 12 trucker quotes

We haven't received any booking confirmations, tariff mails, tracking questions or attachments yet.

## Rounds

| Round | Mail | Customer | Request | Our answer (time to answer) | Verdict |
|---|---|---|---|---|---|
| 1 | 275 | lindqvist-medtech | **Fastest** to ORD; **pickup** 51447 Bergisch Gladbach; ready 11-25; 12 pcs, 676 kg, 5.79 cbm | CGN–ORD Aurelia, 1000 kg chargeable, €2494.80, 2 days. **Trucking explicitly excluded** (1:41) | rejected, `too_low` |
| 2 | 431 | sonnberg-roesterei | **Fastest** to JFK; **pickup** 61018 Bad Homburg; ready 11-25; 6 pcs, 1233 kg, 10.78 cbm; asks for earliest arrival | FRA–JFK Polaris, €4491.28, 1800.26 kg chargeable, 4 days. **Trucking excluded** (2:39) | rejected, `too_low` |
| 3 | 741 | auer-maschinenbau | **Fastest** DUS→JFK; customer delivers to DUS; ready 11-27; 1 pc, 1540 kg, 5.9 cbm | Fjordline €3696.00, 3 days, R-jyMH42RKnZOqllTT (0:28) | accepted → asked to book → **not booked** |
| 4 | 947 | rheintal-elektronik | **Cheapest** to ORD; **pickup** 51896 Bergisch Gladbach; ready 11-28; 6 pcs, 679 kg, 4.19 cbm | CGN–ORD Xenon €1483.43 + Elbkante pickup €115.98 = **€1599.41** (0:35) | accepted → **not booked** |
| 5 | 1233 | weidner-spielwaren | **Fastest** STR→EZE; delivers to STR; ready 11-26; 12 pcs, 1228 kg, 5.37 cbm; asks latest drop-off and arrival | Fjordline €4408.52, 4 days (0:04) | accepted → **not booked** |
| 6 | 1470 | kessler-feinmechanik | **Cheapest** DUS→KUL; delivers to DUS; ready 11-26; 12 pcs, 1549 kg, 8.26 cbm | Fjordline €3949.95, 5 days (0:03) | accepted → **booked by hand: BK-8RCF17DZ** (the airline confirmed it in mail 1623) |
| 7 | 1638 | rheintal-elektronik | **Fastest** MUC→LAX; delivers to MUC; ready 11-29; **2 lots** (618 kg / 0.74 cbm + 380 kg / 1.65 cbm) shipped as one consignment; asks for earliest departure and arrival | answered by the old process (mail 1674) | accepted (mail 1701) |

Rounds 8–13 and everything after 16:36 CET are summarised in the [HANDOFF.md](HANDOFF.md) backlog. That covers multi-lot requests, dangerous-goods rejections, tracking questions, the reply loop and late bookings. They are not part of the current plan.

Patterns in the customer requests:

- The cargo is always given as pieces, gross kg and cbm, plus a ready date, and the currency is always EUR.
- The customer asks for either **"fastest"** or **"cheapest"**.
- The origin is given either as a **postal code with "please arrange trucking"** or as a German airport with **"we deliver to X ourselves"**.
- The destination is given as an IATA code plus the city name.
- Some customers ask extra questions: earliest departure or arrival, latest drop-off time. A good ("human") answer addresses these.
- **None of the six requests was missing data.** We haven't seen an incomplete request yet, so the clarifying-question path is unproven (but cheap to keep).

Customer verdict replies use fixed wording:

- "Thank you for your offer. That works for us, please go ahead and book it." means accepted, and we must **book**.
- "That seems too low to be right, so we will not go ahead with it." means rejected as too low. We assume there is a "too high" variant.

## Trucker quotes

We send one request per trucker that serves the zone. Our request contains: from (postal code and city), to (airport), date, pieces, kg, cbm, and a request for an all-in EUR price.

The replies arrive **about 30 s later**. They are free text, but every reply contains:

```text
All-in price … : 222.36 EUR
Pickup time: 13 hours
Offer valid until: 21 Nov 2026 15:20 UTC     (about 15 min)
Quote reference: TRK-181
No booking is required.
```

Each trucker's pickup time stays the same between requests, while its price depends on the weight:

| Trucker | Round 1 (676 kg) | Round 4 (679 kg / 4.19 cbm) | Pickup time |
|---|---|---|---|
| Elbkante | €134.69 | €115.98 | 29 h (slowest, cheapest) |
| Westfalenroute | €220.03 | €187.97 | 11 h (fastest) |
| Bodenseefracht | €269.71 | €226.96 | 12 h |
| Maintal | €222.36 | €192.97 | 13 h |

The best trucker depends on the request: **cheapest → lowest price; fastest → shortest pickup time**. Truckers need no booking.

The format is regular enough that a regex would work. **Decided anyway:** mail text is always read by an LLM, here the `talos-extract-trucker-quote` extractor ([ADR 12](decisions.md#12--an-llm-reads-every-mail-no-regex-on-mail-text)).

## Our outgoing mail (who sent it?)

| Ids | Signature | Style |
|---|---|---|
| 377, 643 | "Alex, Talos Air Freight \| Munich" | LLM prose, airport-to-airport only |
| 405–408 | "TALOS" | trucker requests |
| 803, 959, 1082, 1275, 1477 | "Air Freight Desk" | template with Quote reference = rate_id, "Reply to this mail to confirm the booking." |
| 1030 (+1033, 1035, 1038) | "TALOS Team" | **duplicate** trucker requests, sent 6 s after 959 etc. |

None of these were sent by `router/` in this repo; it only classifies and stores mail in `router/data/router.db`. Several team members or bots are using the same key. The "Air Freight Desk" bot got 4 of 4 quotes accepted, but it never books.

## The current classifier on real data

These are the labels `router/data/router.db` assigned, compared with the truth:

- Trucker quotes were labelled "Answer" 7 times and **"RFQ" 5 times**. That is wrong: a trucker quote is a reply to our own request.
- Customer verdicts were labelled "Answer" or **"update"**. The label doesn't tell us whether the customer accepted, rejected, or asked us to book.
- Only the 6 customer requests were labelled correctly (RFQ).

Conclusion of the analysis: metadata (sender and thread) is the strongest signal. **Decided:** the metadata becomes a *context lookup* that is passed to the classifier, which stays the first LLM step with new categories ([ADR 15](decisions.md#15--classifier-first-then-focused-extractors), [16](decisions.md#16--context-lookup-no-llm-before-the-classifier)). These were the original metadata rules:

- Sender in the trucker list → trucker quote.
- Sender in the airline list → airline mail (rate, tariff or booking confirmation).
- `ratedesk@` / `tracking@` → desk reply.
- Unknown sender with no `in_reply_to` → new customer request.
- Customer reply in a thread where we sent a quote → verdict (accept or reject by keyword).
