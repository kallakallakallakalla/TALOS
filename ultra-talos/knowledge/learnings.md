# Learnings

One line each: date, mail id, what the next run must know. Newest last.

- 2026-11-21: Portal airline lookup needs the 2-letter code or the exact full name ("polaris" -> 404).
- 2026-11-21: A portal `rate_id` is valid about 15 minutes. Before booking, fetch a fresh rate if it expired (410).
- 2026-11-21: Booking confirmations come unthreaded from the airline ("Booking BK-... confirmed. Your reference: ...").
- 2026-11-21: Truckers answer about 30 s after a request, with a price, "Pickup time: N hours", a validity of about 15 min and a TRK- reference. Truckers are never booked.
- 2026-11-21: Bookings made minutes after the acceptance did not count. Book in the same run as the acceptance.
- 2026-11-21: Mail we sent is readable with GET /v1/mailbox/messages/{id}; the poll only lists mail sent to us.
- 2026-11-21, mail 24011: Listing the mailbox (GET /v1/mailbox/messages?since_id=) is limited to one poll per 5 s per team (429 with retry_after). Reading one mail by id is not limited. Poll rarely; `tools/api.py` retries the 429 itself.
- 2026-11-22, drill (no mail): Portal UI at https://start-hack.5u.ai/portal/ui takes weight, airline code/name, volume, destination, team key and origin; a “Solve the sum” challenge may require a human to clear it. Submit with “Get rate” (then “Show price”); read the result from the “Your rate” table above the form: rate_id, airline/lane, chargeable weight, total/currency, transit days and valid-until timestamp.
