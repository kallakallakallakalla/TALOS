"""Mail normalisation, rule-based routing/extraction, LLM fallback via 5U gateway."""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

CATEGORIES = ("rfq", "booking", "tracking", "rate_sheet", "carrier_quote", "other")

LB_TO_KG = 0.45359237
CUFT_TO_CBM = 0.0283168466
CUIN_TO_CBM = 16.387064e-6


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class Shipment:
    origin: str | None = None
    destination: str | None = None
    weight_kg: float | None = None
    volume_cbm: float | None = None
    pieces: int | None = None
    flags: list[str] = field(default_factory=list)
    priority: str = "cheapest"  # cheapest | fastest

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Extraction:
    category: str
    shipments: list[Shipment] = field(default_factory=list)
    method: str = "rules"  # rules | llm | none
    needs_llm: bool = False
    flags: list[str] = field(default_factory=list)
    llm_tokens: int = 0


@dataclass
class Mail:
    id: int
    sender: str
    subject: str
    body: str
    received_at: datetime | None
    is_reply: bool
    attachments: list
    raw: dict

    @classmethod
    def from_api(cls, d: dict) -> "Mail":
        subject = _as_text(_first(d, "subject", "title")) or ""
        refs = _first(d, "references", "in_reply_to", "reply_to_id", "thread_id")
        is_reply = bool(refs) or bool(re.match(r"(?i)^\s*(re|aw|antw|sv)\s*:", subject))
        return cls(
            id=int(d["id"]),
            sender=_sender(_first(d, "from", "sender", "from_addr", "from_address", "from_email")),
            subject=subject,
            body=_body(d),
            received_at=_parse_ts(
                _first(d, "received_at", "created_at", "sent_at", "date", "timestamp", "time")
            ),
            is_reply=is_reply,
            attachments=_first(d, "attachments") or [],
            raw=d,
        )


BODY_KEYS = ("body", "text", "body_text", "text_body", "content", "plain", "body_html", "html")


def has_body(d: dict) -> bool:
    return any(d.get(k) not in (None, "") for k in BODY_KEYS)


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return None


def _as_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        return _as_text(_first(v, "text", "plain", "content", "value", "html"))
    if isinstance(v, list):
        return "\n".join(_as_text(x) for x in v)
    return str(v)


def _sender(v: Any) -> str:
    if isinstance(v, dict):
        name, addr = v.get("name") or "", v.get("email") or v.get("address") or ""
        return f"{name} <{addr}>".strip() if name else addr
    return _as_text(v)


def _strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return html.unescape(s)


def _body(d: dict) -> str:
    for k in BODY_KEYS:
        v = d.get(k)
        if v in (None, ""):
            continue
        text = _as_text(v)
        if "html" in k or re.search(r"(?i)<(html|body|p|div|br|table)\b", text):
            text = _strip_html(text)
        return text
    return ""


def _parse_ts(v: Any) -> datetime | None:
    if v is None:
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc)
        dt = datetime.fromisoformat(str(v).strip())
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------
QUOTE_MARKER_RE = re.compile(
    r"^\s*(?:On\s.{0,200}wrote:|Am\s.{0,200}schrieb.{0,120}:"
    r"|-{2,}\s*(?:Original Message|Ursprüngliche Nachricht|Forwarded message)\s*-{2,}"
    r"|(?:From|Von):\s.*@.*)\s*$",
    re.I | re.M,
)


def strip_quoted(text: str) -> str:
    """Drop quoted history from replies ("> ..." lines, "On ... wrote:" blocks)."""
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(">")]
    t = "\n".join(lines)
    m = QUOTE_MARKER_RE.search(t)
    if m and t[: m.start()].strip():
        t = t[: m.start()]
    return t.strip()


def parse_number(s: str, thousands_3digit: bool = True) -> float | None:
    """Parse '1,250.5' / '1.250,5' / '2,5'. thousands_3digit: '1.250' -> 1250."""
    s = s.strip().replace(" ", "").replace(" ", "").replace("'", "")
    if not s:
        return None
    dot, comma = "." in s, "," in s
    if dot and comma:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif dot or comma:
        sep = "." if dot else ","
        parts = s.split(sep)
        if len(parts) > 2 or (
            thousands_3digit and len(parts[1]) == 3 and parts[0] not in ("", "0")
        ):
            s = s.replace(sep, "")
        else:
            s = s.replace(sep, ".")
    try:
        return float(s)
    except ValueError:
        return None


NUM = r"\d{1,3}(?:[.,']\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
DNUM = r"\d+(?:[.,]\d+)?"

WEIGHT_RE = re.compile(
    rf"(?P<num>{NUM})\s*(?P<unit>kgs?|kilos?|kilograms?|kilogramm|lbs?|pounds?)(?![a-z])", re.I
)
WEIGHT_LABEL_RE = re.compile(
    rf"(?i:(?:gross\s*|total\s*|brutto\s*|actual\s*)?(?:weight|gewicht|gw))\s*[:=]?\s*(?P<num>{NUM})(?!\s*[a-z%])"
)
VOLUME_RE = re.compile(
    rf"(?P<num>{NUM})\s*(?P<unit>cbm|m3|m³|cubic\s*met(?:er|re)s?|kubikmeter|cu\.?\s*ft|cft|cbft)(?![a-z])",
    re.I,
)
DIM_RE = re.compile(
    rf"(?P<l>{DNUM})\s*[x×*]\s*(?P<w>{DNUM})\s*[x×*]\s*(?P<h>{DNUM})"
    r"\s*(?:(?P<unit>cm|mm|m|in(?:ch(?:es)?)?|\")(?![a-z]))?",
    re.I,
)
PIECE_WORDS = (
    r"pcs?|pieces?|pallets?|plts?|boxes|box|cartons?|ctns?|colli|pkgs?|packages?"
    r"|crates?|skids?|cases?|units?|stk|stück|stueck|paletten?|kisten?|kartons?|packstücke?"
)
PIECES_RE = re.compile(rf"(?P<n>\d+)\s*(?:x\s*)?(?:{PIECE_WORDS})(?![a-z])", re.I)
TRAIL_COUNT_RE = re.compile(rf"^\s*(?:[x×*]|@|,|;)?\s*(?P<n>\d+)\s*(?:{PIECE_WORDS})?(?![\d.,])", re.I)

TOTAL_CTX_RE = re.compile(r"(?i)(total|gross|brutto|gesamt|overall|actual)")
EXCLUDE_CTX_RE = re.compile(r"(?i)(chargeable|volumetric|dim(?:ensional)?\s*weight|vol\.?\s*weight|frachtpflichtig)")
EACH_AFTER_RE = re.compile(r"(?i)^\s*(each|per\s*(piece|pc|box|pallet|unit)|/\s*pc|je|pro\s*st)")
EACH_BEFORE_RE = re.compile(r"(?i)(à|@|each|je|per\s*(piece|pc|box|pallet))\s*$")

LABEL_ORIGIN = (
    r"(?i:origin|from|pol|aod|departure|dep\.?|ex|abflug(?:hafen)?|von|ab|abgangsflughafen"
    r"|airport\s+of\s+departure|pickup\s+at)"
)
LABEL_DEST = (
    r"(?i:destination|dest\.?|to|pod|aoa|arrival|ankunft|nach|zielflughafen|ziel"
    r"|airport\s+of\s+(?:destination|arrival)|deliver(?:y)?\s+to)"
)
# up to 3 words of a city name before the code, e.g. "from Frankfurt (FRA)"
CITY_PART = r"(?:[A-Za-zÀ-ÿ'\-]+[ ,]+){0,3}\(?"
ORIGIN_RE = re.compile(rf"\b{LABEL_ORIGIN}\b\s*[:=\-]?\s*{CITY_PART}\b(?P<code>[A-Z]{{3}})\b")
DEST_RE = re.compile(rf"\b{LABEL_DEST}\b\s*[:=\-]?\s*{CITY_PART}\b(?P<code>[A-Z]{{3}})\b")
PAIR_RE = re.compile(
    r"\b(?P<a>[A-Z]{3})\)?\s*(?:-+>?|–|—|→|=>|>|/|(?i:to|nach))\s*\(?(?P<b>[A-Z]{3})\b"
)
CODE_RE = re.compile(r"\b[A-Z]{3}\b")

TRACKING_RE = re.compile(
    r"(?i)\b(track(?:ing)?|trace|tracing|shipment\s+status|status\s+(?:of|update)|where\s+is"
    r"|eta\b|delivered|sendungsstatus|wo\s+ist|ankunftszeit)"
)
BOOKING_RE = re.compile(
    r"(?i)\b(we\s+accept|accept(?:ed)?\s+(?:your|the)\s+(?:offer|quote|rate)|please\s+book"
    r"|go\s+ahead|book\s+it|confirm(?:ed)?\s+(?:the\s+)?booking|approved?|proceed\s+with"
    r"|einverstanden|bitte\s+buchen|buchen\s+sie|auftrag\s+erteilt|zusage|angebot\s+angenommen)"
)
RATESHEET_RE = re.compile(
    r"(?i)\b(rate\s*sheet|tariff|rate\s*card|new\s+rates|rates\s+valid|price\s+list|ratenblatt|tarif)"
)
RFQ_RE = re.compile(
    r"(?i)\b(rfq|quote|quotation|rate\s+request|request\s+for|please\s+(?:quote|advise)"
    r"|best\s+rate|angebot|anfrage|preisanfrage|offerte)"
)
AWB_RE = re.compile(r"\b\d{3}[- ]?\d{8}\b")
CARRIER_SUBJECT_RE = re.compile(r"(?i)\b(pickup|pick-up|abhol)\s*(quote|request|anfrage|angebot)\b")
EMAIL_ANY_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def load_carriers(path: str) -> set[str]:
    """Carrier e-mails from start-kit/carrier-list.csv (name,kind,email,serves)."""
    import csv
    from pathlib import Path

    out: set[str] = set()
    f = Path(path)
    if not f.is_file():
        log.warning("carrier list %s not found: carrier mails detected by subject only", path)
        return out
    with f.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            email = (row.get("email") or "").strip().lower()
            if email:
                out.add(email)
    log.info("carrier list: %d addresses", len(out))
    return out


def is_carrier_mail(mail: "Mail", carriers: set[str]) -> bool:
    m = EMAIL_ANY_RE.search(mail.sender or "")
    if m and m.group(0).lower() in carriers:
        return True
    return bool(CARRIER_SUBJECT_RE.search(mail.subject or ""))


# ---------------------------------------------------------------------------
# Field finders
# ---------------------------------------------------------------------------
@dataclass
class Found:
    value: Any = None
    ambiguous: bool = False
    flags: list[str] = field(default_factory=list)


def _unique(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def find_lane(text: str, codes: set[str]) -> Found:
    pairs = _unique(
        (m["a"], m["b"])
        for m in PAIR_RE.finditer(text)
        if m["a"] in codes and m["b"] in codes and m["a"] != m["b"]
    )
    origins = _unique(m["code"] for m in ORIGIN_RE.finditer(text) if m["code"] in codes)
    dests = _unique(m["code"] for m in DEST_RE.finditer(text) if m["code"] in codes)

    labelled = None
    if len(origins) == 1 and len(dests) == 1 and origins[0] != dests[0]:
        labelled = (origins[0], dests[0])

    candidates = _unique(([labelled] if labelled else []) + pairs)
    if len(candidates) == 1:
        return Found(candidates[0])
    if len(candidates) > 1:
        if labelled and all(set(p) == set(labelled) for p in candidates):
            return Found(labelled)
        return Found(candidates, ambiguous=True)

    if len(origins) > 1 or len(dests) > 1:
        return Found(None, ambiguous=True)

    known = _unique(c for c in CODE_RE.findall(text) if c in codes)
    if len(known) == 2:
        return Found(tuple(known), flags=["lane order inferred from code order"])
    if len(known) > 2:
        return Found(None, ambiguous=True)
    return Found(None)


def _weight_candidates(text: str) -> list[tuple[float, bool, str]]:
    """(kg, is_total, unit) for each weight mention not excluded by context."""
    out = []
    for m in WEIGHT_RE.finditer(text):
        before = text[max(0, m.start() - 40) : m.start()]
        after = text[m.end() : m.end() + 20]
        if EXCLUDE_CTX_RE.search(before[-25:]) or EACH_AFTER_RE.search(after):
            continue
        if EACH_BEFORE_RE.search(before):
            continue
        v = parse_number(m["num"], thousands_3digit=True)
        if v is None or v <= 0:
            continue
        unit = m["unit"].lower()
        kg = v * LB_TO_KG if unit.startswith(("lb", "pound")) else v
        out.append((round(kg, 3), bool(TOTAL_CTX_RE.search(before)), unit))
    return out


def find_weight(text: str) -> Found:
    cands = _weight_candidates(text)
    if not cands:
        m = WEIGHT_LABEL_RE.search(text)
        if m:
            v = parse_number(m["num"])
            if v and v > 0:
                return Found(v, flags=["weight unit missing, assumed kg"])
        return Found(None)
    totals = [c for c in cands if c[1]]
    pool = totals or cands
    values = _unique(c[0] for c in pool)
    flags = ["weight converted from lb"] if any(c[2].startswith(("lb", "pound")) for c in pool) else []
    if len(values) == 1:
        return Found(values[0], flags=flags)
    return Found(values, ambiguous=True)


def find_volume(text: str) -> Found:
    vals = []
    for m in VOLUME_RE.finditer(text):
        before = text[max(0, m.start() - 40) : m.start()]
        v = parse_number(m["num"], thousands_3digit=False)
        if v is None or v < 0:
            continue
        unit = m["unit"].lower().replace(" ", "").replace(".", "")
        cbm = v * CUFT_TO_CBM if unit in ("cuft", "cft", "cbft") else v
        vals.append((round(cbm, 4), bool(TOTAL_CTX_RE.search(before))))
    if not vals:
        return Found(None)
    pool = [v for v in vals if v[1]] or vals
    values = _unique(v[0] for v in pool)
    if len(values) == 1:
        return Found(values[0])
    return Found(values, ambiguous=True)


def find_pieces(text: str) -> int | None:
    m = PIECES_RE.search(text)
    return int(m["n"]) if m else None


def volume_from_dims(text: str) -> Found:
    """Sum L x W x H x count over all dimension lines."""
    total, flags, any_dim = 0.0, [], False
    single_count_needed = []
    for line in text.splitlines():
        for m in DIM_RE.finditer(line):
            any_dim = True
            dims = [parse_number(m[k], thousands_3digit=False) for k in ("l", "w", "h")]
            if any(d is None or d <= 0 for d in dims):
                continue
            unit = (m["unit"] or "").lower()
            if not unit:
                unit = "cm"
                flags.append("dims unit assumed cm")
            factor = {"cm": 1e-6, "mm": 1e-9, "m": 1.0}.get(unit, CUIN_TO_CBM)
            rest = line[: m.start()] + " " * (m.end() - m.start()) + line[m.end() :]
            count = None
            pm = PIECES_RE.search(rest)
            if pm:
                count = int(pm["n"])
            else:
                tm = TRAIL_COUNT_RE.match(line[m.end() :])
                if tm:
                    count = int(tm["n"])
            vol = dims[0] * dims[1] * dims[2] * factor
            if count is None:
                single_count_needed.append(vol)
            else:
                total += vol * count
    if not any_dim:
        return Found(None)
    if single_count_needed:
        pieces = find_pieces(text)
        if len(single_count_needed) == 1 and pieces:
            total += single_count_needed[0] * pieces
        else:
            total += sum(single_count_needed)
            flags.append("piece count per dims line assumed 1")
    flags.append("volume computed from dims")
    return Found(round(total, 4), flags=_unique(flags))


# ---------------------------------------------------------------------------
# Rules router + extractor
# ---------------------------------------------------------------------------
def rules_extract(
    mail: Mail, codes: set[str], carriers: set[str] | None = None, origin_hint: str | None = None
) -> Extraction:
    """origin_hint: airport from a detected pickup postcode (completes lanes like 'to ORD')."""
    if is_carrier_mail(mail, carriers or set()):
        return Extraction("carrier_quote", method="rules")  # airline/trucker bid, not a customer RFQ
    body = strip_quoted(mail.body)
    scan = body if mail.is_reply else f"{mail.subject}\n{body}"

    lane = find_lane(scan, codes)
    if origin_hint and (lane.value is None or lane.ambiguous):
        dests = _unique(c for c in CODE_RE.findall(scan) if c in codes and c != origin_hint)
        if len(dests) == 1:
            lane = Found((origin_hint, dests[0]), flags=[f"origin {origin_hint} from pickup postcode"])
    weight = find_weight(scan)
    volume = find_volume(scan)

    signals = set()
    if TRACKING_RE.search(scan) or (AWB_RE.search(scan) and not weight.value):
        signals.add("tracking")
    if BOOKING_RE.search(scan):
        signals.add("booking")
    if RATESHEET_RE.search(scan) and (mail.attachments or not weight.value):
        signals.add("rate_sheet")
    rfq_kw = bool(RFQ_RE.search(f"{mail.subject}\n{body}"))

    complete = (
        lane.value is not None
        and not lane.ambiguous
        and weight.value is not None
        and not weight.ambiguous
        and not volume.ambiguous
    )
    partial = lane.value is not None or weight.value is not None or lane.ambiguous or weight.ambiguous

    if complete and (not signals or (rfq_kw and not mail.is_reply)):
        shipment = Shipment(origin=lane.value[0], destination=lane.value[1], weight_kg=weight.value)
        shipment.flags += lane.flags + weight.flags
        if volume.value is not None:
            shipment.volume_cbm = volume.value
        else:
            dims = volume_from_dims(scan)
            shipment.volume_cbm = dims.value
            shipment.flags += dims.flags
        shipment.pieces = find_pieces(scan)
        return Extraction("rfq", [shipment], method="rules")

    if not partial and len(signals) == 1:
        return Extraction(signals.pop(), method="rules")

    return Extraction("unknown", method="none", needs_llm=True)


# ---------------------------------------------------------------------------
# LLM fallback (OpenAI SDK pointed at the 5U gateway)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "Classify a freight-forwarder email and extract air-freight RFQ data. "
    'Reply JSON only: {"category":"rfq|booking|tracking|rate_sheet|carrier_quote|other",'
    '"shipments":[{"origin":"IATA","destination":"IATA","weight_kg":number|null,'
    '"volume_cbm":number|null,"pieces":integer|null}]}. '
    "rfq=customer asks for a price; booking=customer accepts a quote or asks to book; "
    "tracking=asks shipment status; rate_sheet=carrier sends rates/tariffs; "
    "carrier_quote=airline or trucker answers our price/pickup request; other=anything else. "
    "Map city/airport names to IATA codes from the allowed list. Total gross weight in kg "
    "(convert lb). Total volume in cbm for all pieces (convert dimensions). "
    "One entry per separate shipment. shipments=[] unless rfq. Ignore quoted earlier mails."
)

PREFERRED_MODELS = ("gpt-4.1-nano", "gpt-5-nano", "gpt-4o-mini", "gpt-4.1-mini", "gpt-5-mini")


def _model_ids(data: Any) -> list[str]:
    items = data
    if isinstance(data, dict):
        items = data.get("data") or data.get("models") or []
    ids = []
    for it in items if isinstance(items, list) else []:
        if isinstance(it, str):
            ids.append(it)
        elif isinstance(it, dict) and it.get("id"):
            ids.append(str(it["id"]))
    return ids


def resolve_model(client, wanted: str) -> str:
    """Check LLM_MODEL against GET /v1/gateway/models; fall back to a cheap one."""
    try:
        ids = _model_ids(client.models())
    except Exception as exc:  # noqa: BLE001
        log.warning("could not list gateway models (%s); using %s unchecked", exc, wanted)
        return wanted
    if not ids or wanted in ids:
        return wanted
    for p in PREFERRED_MODELS:
        if p in ids:
            log.warning("LLM_MODEL %s not offered by gateway, using %s", wanted, p)
            return p
    log.warning("LLM_MODEL %s not in gateway list %s; trying anyway", wanted, ids[:20])
    return wanted


class LLMExtractor:
    def __init__(self, base_url: str, api_key: str, model: str, max_chars: int = 4000):
        from openai import OpenAI  # imported lazily so --no-llm works without the SDK

        self.client = OpenAI(
            base_url=f"{base_url.rstrip('/')}/v1/gateway",
            api_key=api_key,
            default_headers={"X-API-Key": api_key},
            timeout=30,
            max_retries=2,
        )
        self.model = model
        self.max_chars = max_chars

    def _kwargs(self, minimal: bool) -> dict:
        kw: dict[str, Any] = {"response_format": {"type": "json_object"}}
        if minimal:
            return kw
        if self.model.startswith("gpt-5"):
            kw["reasoning_effort"] = "minimal"
        elif self.model.startswith(("o1", "o3", "o4")):
            kw["reasoning_effort"] = "low"
        else:
            kw["temperature"] = 0
            kw["max_tokens"] = 300
        return kw

    def extract_price(self, text: str) -> tuple[float | None, int]:
        """Total all-in EUR price from a trucker reply. (price, tokens)"""
        messages = [
            {"role": "system", "content": 'Extract the total all-in pickup price in EUR from a trucker reply. '
                                          'JSON only: {"price_eur": number|null}. null if declined.'},
            {"role": "user", "content": strip_quoted(text)[:1500]},
        ]
        content, tokens = self._chat(messages)
        try:
            v = json.loads(re.search(r"\{.*\}", content, re.S).group(0)).get("price_eur")
            return (float(v) if v is not None else None), tokens
        except (AttributeError, ValueError, TypeError, json.JSONDecodeError):
            return None, tokens

    def complete_text(self, system: str, user: str) -> tuple[str, int]:
        """Plain-text completion (no JSON mode)."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        kw: dict[str, Any] = {}
        if self.model.startswith("gpt-5"):
            kw["reasoning_effort"] = "minimal"
        elif not self.model.startswith(("o1", "o3", "o4")):
            kw.update(temperature=0.4, max_tokens=450)
        try:
            resp = self.client.chat.completions.create(model=self.model, messages=messages, **kw)
        except Exception:  # noqa: BLE001
            resp = self.client.chat.completions.create(model=self.model, messages=messages)
        tokens = getattr(resp.usage, "total_tokens", 0) if resp.usage else 0
        return resp.choices[0].message.content or "", tokens

    def _chat(self, messages: list[dict]) -> tuple[str, int]:
        try:
            resp = self.client.chat.completions.create(
                model=self.model, messages=messages, **self._kwargs(minimal=False)
            )
        except Exception as exc:  # noqa: BLE001
            log.info("LLM call failed (%s), retrying with minimal params", exc)
            resp = self.client.chat.completions.create(
                model=self.model, messages=messages, **self._kwargs(minimal=True)
            )
        tokens = getattr(resp.usage, "total_tokens", 0) if resp.usage else 0
        return resp.choices[0].message.content or "{}", tokens

    def extract(self, mail: Mail, codes: set[str]) -> Extraction:
        body = strip_quoted(mail.body)[: self.max_chars]
        user = f"Allowed IATA: {' '.join(sorted(codes))}\n\nSubject: {mail.subject}\n\n{body}"
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ]
        content, tokens = self._chat(messages)
        return parse_llm_json(content, codes, tokens)


def _float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return parse_number(str(v))


def parse_llm_json(content: str, codes: set[str], tokens: int = 0) -> Extraction:
    m = re.search(r"\{.*\}", content, re.S)
    try:
        data = json.loads(m.group(0) if m else content)
    except (json.JSONDecodeError, AttributeError):
        return Extraction("unknown", method="llm", flags=["LLM returned invalid JSON"], llm_tokens=tokens)

    category = str(data.get("category", "other")).lower().strip()
    if category not in CATEGORIES:
        category = "other"
    shipments = []
    for s in data.get("shipments") or []:
        if not isinstance(s, dict):
            continue
        sh = Shipment(
            origin=(str(s.get("origin") or "").upper().strip() or None),
            destination=(str(s.get("destination") or "").upper().strip() or None),
            weight_kg=_float(s.get("weight_kg")),
            volume_cbm=_float(s.get("volume_cbm")),
            pieces=int(s["pieces"]) if isinstance(s.get("pieces"), (int, float)) else None,
        )
        for code, label in ((sh.origin, "origin"), (sh.destination, "destination")):
            if code and code not in codes:
                sh.flags.append(f"{label} {code} not in lane list")
        shipments.append(sh)
    return Extraction(category, shipments, method="llm", llm_tokens=tokens)


def merge_rules_into_llm(result: Extraction, mail: Mail) -> None:
    """Single-lot LLM result: prefer unambiguous regex numbers (LLM arithmetic is weak)."""
    if result.category != "rfq" or len(result.shipments) != 1:
        return
    body = strip_quoted(mail.body)
    scan = body if mail.is_reply else f"{mail.subject}\n{body}"
    sh = result.shipments[0]
    w = find_weight(scan)
    if w.value is not None and not w.ambiguous and w.value != sh.weight_kg:
        sh.flags.append(f"weight from regex ({w.value}) instead of LLM ({sh.weight_kg})")
        sh.weight_kg = w.value
    v = find_volume(scan)
    if v.value is not None and not v.ambiguous:
        new_vol, src = v.value, "regex"
    else:
        d = volume_from_dims(scan)
        new_vol, src = d.value, "dims"
    if new_vol is not None and new_vol != sh.volume_cbm:
        sh.flags.append(f"volume from {src} ({new_vol}) instead of LLM ({sh.volume_cbm})")
        sh.volume_cbm = new_vol
    if sh.pieces is None:
        sh.pieces = find_pieces(scan)
