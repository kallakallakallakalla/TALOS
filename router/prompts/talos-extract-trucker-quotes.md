You read the replies of trucking companies to one pickup request of ours and extract each
offer. Return exactly one entry per reply, with the reply's number.

Fields per reply
- reply: the number of the reply (REPLY 1 -> 1).
- price_eur: the all-in price in EUR for the pickup. Decimal commas become dots
  ("222,36" = 222.36). null if the reply states no price (a decline, or a question to us).
- pickup_hours: the pickup / lead time in hours (days x 24). null if not stated.
- valid_until: validity of the offer as ISO 8601 UTC (e.g. 2026-11-21T15:20:00Z). null if not stated.
- quote_ref: their offer / quote reference exactly as written (e.g. "TRK-181"). null if none.
- conditions: every other limit or extra the trucker states, one short item each, in their
  words: pickup windows or weekdays, how the pickup time is counted ("from order to
  collection"), waiting time or other charges not in the price, the chargeable weight they
  used, anything else that could matter for choosing a trucker. Never drop a stated fact.
  Leave out greetings, the offer reference and "no booking needed". Empty list if none.

Rules
- A reply can carry its offer in ATTACHMENT blocks (an offer letter, a PDF, the transcript of a
  voice message). They belong to that reply: read the price and the other fields there too.
- Copy numbers and references exactly. Never compute, convert or guess a price.
- Keep the replies apart: every value must come from the reply with that number.
