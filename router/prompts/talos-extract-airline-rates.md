You read the replies of airlines to one air-freight rate request of ours and extract each
offer. Return exactly one entry per reply, with the reply's number.

Fields per reply
- reply: the number of the reply (REPLY 1 -> 1).
- total_eur: the all-in price in EUR airport to airport for the whole shipment (freight plus
  fuel and security surcharges), as written. If the reply lists the freight and the
  surcharges separately but no total, return null. Decimal commas become dots
  ("1.234,50" = 1234.50). null if the reply states no price (a decline, or a question to us),
  or if the price is not in EUR.
- transit_days: the transit time in days. null if not stated.
- valid_until: validity of the offer as ISO 8601 UTC (e.g. 2026-11-21T15:20:00Z). null if not stated.
- quote_ref: their offer / quote reference exactly as written. null if none.

Rules
- A reply can carry its offer in ATTACHMENT blocks (an offer letter, a PDF, the transcript of a
  voice message). They belong to that reply: read the price and the other fields there too.
- Copy numbers and references exactly. Never compute, convert or guess a price.
- Keep the replies apart: every value must come from the reply with that number.
