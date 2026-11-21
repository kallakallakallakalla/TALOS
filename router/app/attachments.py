"""Customer attachments as text for the LLM (ADR 42).

Files are read locally, without an LLM: pdf via `pdftotext` (poppler), xlsx/docx via their zip
XML, csv/txt as is. A file that gives no usable text (a scan, an image, a broken PDF) is marked
unreadable; the workflow reads it with one vision call only when the request is incomplete.
"""

import io
import logging
import subprocess
import zipfile
from xml.etree import ElementTree

from app.schemas import MailboxAttachment, MailboxMessage

logger = logging.getLogger(__name__)

MAX_CHARS = 3000  # ~750 tokens per file; packing lists and SLIs are far shorter
_MIN_CHARS = 50
_MIN_CLEAN_SHARE = 0.6
VISION_TYPES = ("pdf", "png", "jpg", "jpeg")


def _xml_text(node: ElementTree.Element) -> str:
    return "".join(node.itertext())


def xlsx_text(data: bytes) -> str:
    """Every sheet as tab-separated rows, with shared strings resolved."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        strings: list[str] = []  # files written with inline strings have no shared table
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            strings = [_xml_text(si) for si in root.findall("m:si", ns)]
        rows: list[str] = []
        for name in sorted(n for n in archive.namelist() if n.startswith("xl/worksheets/sheet")):
            for row in ElementTree.fromstring(archive.read(name)).iter(f"{{{ns['m']}}}row"):
                cells = []
                for cell in row.findall("m:c", ns):
                    value = cell.find("m:v", ns)
                    text = _xml_text(cell) if value is None else value.text or ""
                    cells.append(strings[int(text)] if cell.get("t") == "s" else text)
                rows.append("\t".join(cells))
    return "\n".join(rows)


def docx_text(data: bytes) -> str:
    """Paragraphs and table rows of a Word file, one per line."""
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        body = ElementTree.fromstring(archive.read("word/document.xml")).find(f"{w}body")
    lines: list[str] = []
    for block in body:
        if block.tag == f"{w}tbl":
            lines += ["\t".join(_xml_text(c) for c in r.iter(f"{w}tc")) for r in block.iter(f"{w}tr")]
        else:
            lines.append(_xml_text(block))
    return "\n".join(lines)


def pdf_text(data: bytes) -> str:
    return subprocess.run(
        ["pdftotext", "-layout", "-", "-"], input=data, capture_output=True, check=True, timeout=30
    ).stdout.decode("utf-8", errors="replace")


def suffix(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def to_text(filename: str, data: bytes) -> str:
    """Plain text of one file; "" for types we cannot read locally (images, xls)."""
    kind = suffix(filename)
    if kind == "pdf":
        return pdf_text(data)
    if kind == "xlsx":
        return xlsx_text(data)
    if kind == "docx":
        return docx_text(data)
    if kind in ("txt", "csv"):
        return data.decode("utf-8", errors="replace")
    return ""


def clean(text: str) -> str:
    """Squash blank runs and padding; cut to MAX_CHARS."""
    lines = [" ".join(line.split()) for line in text.splitlines()]
    text = "\n".join(line for line in lines if line).strip()
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + " …[truncated]"


def readable(text: str) -> bool:
    """Enough text, and mostly letters/digits/punctuation (not mojibake from a broken font)."""
    if len(text) < _MIN_CHARS:
        return False
    ok = sum(c.isascii() and (c.isprintable() or c.isspace()) or c.isalpha() for c in text)
    return ok / len(text) >= _MIN_CLEAN_SHARE


async def enrich(msg: MailboxMessage, mailbox) -> None:
    """Download and read every attachment not read yet. Never raises: a failed file stays
    unreadable and the mail goes on with its body."""
    for att in msg.attachments[:5]:
        if att.text or att.id is None:
            continue
        try:
            data = await mailbox.download_attachment(msg.id, att.id)
            set_text(att, to_text(att.filename, data))
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the mail
            logger.warning("Attachment %s of mail %s unreadable: %s", att.filename, msg.id, exc)


def set_text(att: MailboxAttachment, text: str, *, from_vision: bool = False) -> None:
    att.text = clean(text)
    att.readable = bool(att.text) if from_vision else readable(att.text)


def as_text(msg: MailboxMessage) -> str:
    """The attachment blocks appended to a mail for the LLM."""
    blocks = []
    for att in msg.attachments:
        if att.readable:
            blocks.append(f"ATTACHMENT {att.filename}:\n{att.text}")
        else:
            blocks.append(f"ATTACHMENT {att.filename}: (could not be read)")
    return "\n\n".join(blocks)


if __name__ == "__main__":  # self-check: python -m app.attachments
    assert readable("Total gross weight (kg): 292\nTotal volume (cbm): 1.92 pieces 5")
    assert not readable("   ")
    assert not readable("�" * 60 + "abc")
    assert clean("a  b\n\n\n c ") == "a b\nc"
    assert clean("x" * 4000).endswith("…[truncated]")
    print("ok")
