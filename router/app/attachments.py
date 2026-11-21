"""Customer attachments as text for the LLM (ADR 42, ADR 43).

Text is read locally, without an LLM: pdf via `pdftotext` (poppler), docx/xlsx/pptx via their
zip XML, csv/txt as is. One vision call per file reads what text extraction cannot see: an
image file, a PDF that contains images or has no usable text, and the pictures embedded in an
Office file. Everything is read once, when the mail comes in, and stored with the mail.
"""

import base64
import io
import logging
import subprocess
import tempfile
import zipfile
from collections.abc import Awaitable, Callable
from xml.etree import ElementTree

from app.schemas import MailboxAttachment, MailboxMessage

logger = logging.getLogger(__name__)

MAX_CHARS = 3000  # ~750 tokens per file; packing lists and SLIs are far shorter
_MIN_CHARS = 50
_MIN_CLEAN_SHARE = 0.6
_IMAGE_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
_MAX_EMBEDDED_IMAGES = 4  # ponytail: pictures per Office file sent to vision; logos come first
_MIN_IMAGE_BYTES = 5_000  # smaller pictures are icons and logos, not documents


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


def pptx_text(data: bytes) -> str:
    """The text of every slide, one paragraph per line, slides in order."""
    a = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        slides = [n for n in archive.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
        slides.sort(key=lambda n: int("".join(filter(str.isdigit, n)) or 0))
        lines: list[str] = []
        for name in slides:
            root = ElementTree.fromstring(archive.read(name))
            lines += [_xml_text(p) for p in root.iter(f"{a}p")]
    return "\n".join(lines)


def pdf_has_images(data: bytes) -> bool:
    with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
        tmp.write(data)
        tmp.flush()
        listing = subprocess.run(
            ["pdfimages", "-list", tmp.name], capture_output=True, text=True, timeout=30
        ).stdout
    return len(listing.strip().splitlines()) > 2  # two header lines, then one line per image


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
    if kind == "pptx":
        return pptx_text(data)
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


def _embedded_images(data: bytes) -> list[tuple[str, bytes]]:
    """Pictures inside a docx/xlsx/pptx (they live under */media/ in the zip)."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        found = [
            (_IMAGE_MIME[suffix(n)], archive.read(n))
            for n in sorted(archive.namelist())
            if "/media/" in n and suffix(n) in _IMAGE_MIME
        ]
    return [(mime, raw) for mime, raw in found if len(raw) >= _MIN_IMAGE_BYTES][:_MAX_EMBEDDED_IMAGES]


def _image_part(mime: str, raw: bytes) -> dict:
    url = f"data:{mime};base64,{base64.b64encode(raw).decode()}"
    return {"type": "image_url", "image_url": {"url": url}}


def vision_parts(filename: str, data: bytes, text: str) -> list[dict]:
    """What a vision call must see of this file; [] when the local text is all there is."""
    kind = suffix(filename)
    if kind in _IMAGE_MIME:
        return [_image_part(_IMAGE_MIME[kind], data)]
    if kind == "pdf" and (not readable(text) or pdf_has_images(data)):
        b64 = base64.b64encode(data).decode()
        return [{"type": "file", "file": {"filename": filename, "file_data": f"data:application/pdf;base64,{b64}"}}]
    if kind in ("docx", "xlsx", "pptx"):
        return [_image_part(mime, raw) for mime, raw in _embedded_images(data)]
    return []


def vision_replaces(filename: str) -> bool:
    """A PDF or an image is shown whole, so vision reads all of it; an Office file only
    shows its pictures, so their text is added to the local text."""
    return suffix(filename) in _IMAGE_MIME or suffix(filename) == "pdf"


Vision = Callable[[str, list[dict]], Awaitable[str]]


async def enrich(msg: MailboxMessage, mailbox, vision: Vision | None = None) -> None:
    """Read every attachment not read yet: local text first, then one vision call if the
    file holds something text extraction cannot see. Never raises: a failed file stays
    unreadable and the mail goes on with its body."""
    for att in msg.attachments[:5]:
        if att.text or att.vision_tried or att.id is None:
            continue
        try:
            data = await mailbox.download_attachment(msg.id, att.id)
            text = to_text(att.filename, data)
            set_text(att, text)
            parts = vision_parts(att.filename, data, att.text) if vision else []
            if parts:
                att.vision_tried = True
                seen = clean(await vision(att.filename, parts))
                if vision_replaces(att.filename):
                    att.text = seen or att.text
                elif seen:
                    att.text = clean(f"{att.text}\nPICTURES IN THE FILE:\n{seen}")
                att.readable = bool(att.text) and (bool(seen) or readable(att.text))
        except Exception as exc:  # noqa: BLE001 - one bad file must not stop the mail
            logger.warning("Attachment %s of mail %s unreadable: %s", att.filename, msg.id, exc)


def set_text(att: MailboxAttachment, text: str) -> None:
    att.text = clean(text)
    att.readable = readable(att.text)


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
