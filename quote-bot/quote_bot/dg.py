"""Dangerous goods (DG) detection in RFQ text and attachments.

Customers may write "non-dangerous" while the attached packing list shows a UN number.
Rule: any DG evidence anywhere (body or attachment) -> ask for DG documents before quoting,
unless the documents are already attached.
"""

from __future__ import annotations

import re

# statements that say "no DG" -> removed before scanning, so "non-dangerous" is not a hit
NEGATED = re.compile(
    r"(?i)\b(?:non[-\s]?(?:dangerous|hazardous|haz|dg|dgr|regulated|restricted)"
    r"|not\s+(?:classified\s+as\s+)?(?:dangerous|hazardous|dg|restricted|regulated)(?:\s+goods)?"
    r"|no\s+(?:dangerous|hazardous)\s+(?:goods|materials?|cargo)|no\s+dg\b|no\s+dgr\b"
    r"|non[-\s]?(?:flammable|toxic|corrosive|explosive)|not\s+(?:flammable|toxic|corrosive|explosive)"
    r"|(?:no|without|keine)\s+lithium(?:[-\s](?:ion|metal))?\s*(?:batter(?:y|ies)|cells?|akkus?)"
    r"|kein(?:e|)\s+gefahrgut|nicht\s+gefährlich|nicht\s+als\s+gefahrgut)"
)
POSITIVE = [
    (re.compile(r"\bUN\s?-?\s?(\d{4})\b"), "UN number"),
    (re.compile(r"(?i)\bdangerous\s+goods?\b"), "dangerous goods"),
    (re.compile(r"(?i)\bhazardous\s+(?:goods|materials?|cargo|substances?)\b|\bhazmat\b"), "hazardous"),
    (re.compile(r"(?i)\b(?:dg|haz(?:ard)?)\s*class\s*[1-9](?:\.\d)?\b|\bclass\s*[1-9](?:\.\d)?\s*(?:dg|dangerous|hazard|flammable|corrosive|toxic|explosive)"
                r"|\bclass\s*[1-9](?:\.\d)?\b(?=[^\n]{0,40}\b(?:packing\s+group|PG\s*I{1,3}\b|UN\s?\d{4}))"), "hazard class"),
    (re.compile(r"(?i)\bpacking\s+group\s*(?:I{1,3}|[1-3])\b|\bPG\s*I{1,3}\b"), "packing group"),
    (re.compile(r"(?i)\bflammable\b|\bentzündlich\b|\bentzuendlich\b"), "flammable"),
    (re.compile(r"(?i)\b(?:corrosive|toxic|oxidi[sz]ing|explosive|radioactive|infectious)\b"), "hazard property"),
    (re.compile(r"(?i)\blithium[-\s](?:ion|metal)?\s*(?:batter(?:y|ies)|cells?)\b|\bUN\s?348[01]\b|\bUN\s?309[01]\b"), "lithium batteries"),
    (re.compile(r"(?i)\bgefahrgut\b|\bgefahrstoff\b"), "Gefahrgut"),
    (re.compile(r"(?i)\b(?:IATA\s+)?DGR\b|\bADR\b|\bIMDG\b"), "DG regulation"),
    (re.compile(r"(?i)\bproper\s+shipping\s+name\b"), "proper shipping name"),
    (re.compile(r"(?i)\b(?:dry\s+ice|aerosols?|magnetized\s+material)\b"), "special DG item"),
]
# documents that settle the DG question (if attached / pasted, we can quote directly)
DOC_RE = re.compile(
    r"(?i)shipper'?s\s+declaration\s+for\s+dangerous\s+goods|dangerous\s+goods\s+declaration|\bDGD\b"
    r"|safety\s+data\s+sheet|\bM?SDS\b|sicherheitsdatenblatt"
)


def dg_evidence(text: str) -> list[str]:
    """Distinct DG hits (with the matched words), [] if none."""
    scan = NEGATED.sub(" ", text or "")
    hits: list[str] = []
    for rx, label in POSITIVE:
        m = rx.search(scan)
        if m:
            hits.append(f"{label}: {m.group(0).strip()[:40]}")
    return hits


def has_dg_documents(texts: list[str], filenames: list[str]) -> bool:
    if any(DOC_RE.search(f or "") for f in filenames):
        return True
    return any(DOC_RE.search(t or "") and re.search(r"\bUN\s?\d{4}\b", t or "") for t in texts)


def dg_request_body(evidence: list[str], signature: str) -> str:
    found = "; ".join(evidence[:4])
    return (
        "Dear customer,\n\n"
        "thank you for your request. Your shipment contains dangerous goods "
        f"({found}). Before we can quote and book it, please send us:\n\n"
        "- Shipper's Declaration for Dangerous Goods (DGD)\n"
        "- Safety data sheet (MSDS/SDS)\n"
        "- UN number, proper shipping name, hazard class and packing group\n"
        "- Net quantity per package and packaging type\n\n"
        "As soon as we have the documents we will send you our offer.\n\n"
        f"Best regards,\n{signature}"
    )
