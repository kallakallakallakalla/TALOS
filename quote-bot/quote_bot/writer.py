"""Turn template drafts into natural, human-sounding mails via the LLM gateway (facts verified)."""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

SYSTEM = (
    "You are an experienced, friendly air freight forwarder writing e-mails. Rewrite the draft as a "
    "natural, concise human e-mail (max 140 words). Keep EVERY number, price, currency, code, date and "
    "reference exactly as written (no reformatting, no rounding). Keep prices and references on their "
    "own lines. No markdown, no placeholders, no invented facts. {extra}"
)


class Writer:
    def __init__(self, llm=None, enabled: bool | None = None):
        self.llm = llm
        if enabled is None:
            enabled = os.getenv("HUMANIZE", "1").strip().lower() not in ("0", "false", "no")
        self.enabled = enabled and llm is not None
        self.signature = os.getenv("REPLY_SIGNATURE", "Air Freight Desk").strip()
        self.tokens = 0

    def write(self, kind: str, draft: str, must_include: list[str], greeting: bool = True) -> str:
        """Return humanized text; fall back to the draft if the LLM drops a fact or fails."""
        if not self.enabled:
            return draft
        extra = f"This is a {kind}. Sign off with: {self.signature}."
        if not greeting:
            extra += " Do not write a greeting line; start directly with the request."
        try:
            text, tokens = self.llm.complete_text(SYSTEM.format(extra=extra), draft)
            self.tokens += tokens
        except Exception as exc:  # noqa: BLE001
            log.warning("humanize failed (%s): using template", exc)
            return draft
        text = (text or "").strip()
        missing = [m for m in must_include if m and str(m) not in text]
        if not text or missing:
            log.info("humanized %s dropped %s: using template", kind, missing)
            return draft
        log.info("humanized %s (%d tok)", kind, tokens)
        return text
