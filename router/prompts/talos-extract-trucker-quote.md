You read a trucking company's reply to our pickup request and extract their offer.

Our request was:
{{our_request}}

Fields
- price_eur: the all-in price in EUR for the pickup. If several prices are given, take
  the total all-in price. Decimal commas become dots ("222,36" = 222.36).
- pickup_hours: the pickup / lead time in hours (convert days x24). null if not stated.
- valid_until: validity of the offer as ISO 8601 UTC (e.g. 2026-11-21T15:20:00Z). null if not stated.
- quote_ref: their offer / quote reference exactly as written (e.g. "TRK-181").
- conditions: any condition or exclusion they state, in a few words; empty if none.

Rules
- Copy numbers and references exactly. Never compute or guess a price.
- If there is no price in the mail, return price_eur = null.
