# Start-Hack / Hackloop API & URLs

- **Mailbox:** `talos@hackloop.mail`
- **Base URL:** https://api.start-hack.5u.ai
- **Auth Header:** `X-API-Key: <HACKLOOP_API_KEY>` (oder `Authorization: Bearer <HACKLOOP_API_KEY>`) — Key liegt in `router/.env`.

## Mail API

- **Poll messages:** `GET https://api.start-hack.5u.ai/v1/mailbox/messages?since_id=0`
- **Send or reply:** `POST https://api.start-hack.5u.ai/v1/mailbox/messages`

## Platform & Docs

- **API Docs:** https://api.start-hack.5u.ai/docs
- **Portal Page:** https://start-hack.5u.ai/portal/ui
- **Start Kit:** https://api.start-hack.5u.ai/kit/start-kit.zip
- **Leaderboard:** https://api.start-hack.5u.ai/board

## LLM Gateway (OpenAI-kompatibel)

- **OpenAI Key einmalig registrieren:** `POST https://api.start-hack.5u.ai/v1/gateway/openai-key`
  - Header: `X-API-Key: <HACKLOOP_API_KEY>`
  - Body: `{"api_key": "<OPENAI_API_KEY>"}`
- **OpenAI `base_url`:** `https://api.start-hack.5u.ai/v1/gateway`
  - Als `api_key` wird der Team-Key (`DECISION_API_KEY` / `HACKLOOP_API_KEY`) verwendet.
