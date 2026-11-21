"""Download mail attachments and turn them into plain text (txt/csv/pdf/xlsx/docx/images)."""

from __future__ import annotations

import base64
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class AttachmentText:
    filename: str
    content_type: str
    text: str
    note: str = ""  # why text is empty/partial
    llm_tokens: int = 0


def _ext(filename: str, content_type: str) -> str:
    name = (filename or "").lower()
    if "." in name:
        return name.rsplit(".", 1)[1]
    ct = (content_type or "").lower()
    for key, ext in (("pdf", "pdf"), ("spreadsheetml", "xlsx"), ("ms-excel", "xls"), ("csv", "csv"),
                     ("wordprocessingml", "docx"), ("png", "png"), ("jpeg", "jpg"), ("jpg", "jpg"),
                     ("text", "txt")):
        if key in ct:
            return ext
    return ""


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def pdf_text(data: bytes) -> tuple[str, str]:
    try:
        import pdfplumber  # better table layout if installed

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            return "\n".join(p.extract_text() or "" for p in pdf.pages), ""
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        log.info("pdfplumber failed: %s", exc)
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n".join(p.extract_text() or "" for p in reader.pages), ""
    except ImportError:
        return "", "pypdf not installed (pip install pypdf)"
    except Exception as exc:  # noqa: BLE001
        return "", f"pdf read failed: {exc}"


def xlsx_text(data: bytes) -> tuple[str, str]:
    try:
        import openpyxl
    except ImportError:
        return "", "openpyxl not installed (pip install openpyxl)"
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001
        return "", f"xlsx read failed: {exc}"
    lines = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            cells = ["" if v is None else (v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else str(v)) for v in row]
            while cells and cells[-1] == "":
                cells.pop()
            if any(cells):
                lines.append(" | ".join(cells))
    return "\n".join(lines), ""


def docx_text(data: bytes) -> tuple[str, str]:
    try:
        x = zipfile.ZipFile(io.BytesIO(data)).read("word/document.xml").decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return "", f"docx read failed: {exc}"

    def clean(s: str) -> str:
        return re.sub(r"<[^>]+>", "", s).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()

    out = []
    pos = 0
    body = x
    for m in re.finditer(r"<w:tbl>.*?</w:tbl>", body, re.S):  # paragraphs before table, then rows
        out += [clean(p) for p in re.findall(r"<w:p[ >].*?</w:p>", body[pos:m.start()], re.S)]
        for tr in re.findall(r"<w:tr[ >].*?</w:tr>", m.group(0), re.S):
            cells = [clean(c) for c in re.findall(r"<w:tc>.*?</w:tc>", tr, re.S)]
            out.append(" | ".join(cells))
        pos = m.end()
    out += [clean(p) for p in re.findall(r"<w:p[ >].*?</w:p>", body[pos:], re.S)]
    return "\n".join(line for line in out if line), ""


def to_text(data: bytes, filename: str, content_type: str = "", llm: Any = None) -> AttachmentText:
    ext = _ext(filename, content_type)
    res = AttachmentText(filename, content_type, "")
    if ext in ("txt", "csv", "tsv", "md", "json", "eml", "html", "htm"):
        res.text = _decode(data)
        if ext in ("html", "htm"):
            res.text = re.sub(r"<[^>]+>", " ", res.text)
    elif ext == "pdf":
        res.text, res.note = pdf_text(data)
    elif ext in ("xlsx", "xlsm"):
        res.text, res.note = xlsx_text(data)
    elif ext == "docx":
        res.text, res.note = docx_text(data)
    elif ext in ("png", "jpg", "jpeg", "gif", "webp"):
        if llm is None:
            res.note = "image: LLM disabled, not read"
        else:
            mime = "image/png" if ext == "png" else "image/jpeg"
            try:
                res.text, res.llm_tokens = llm.transcribe_image(base64.b64encode(data).decode(), mime)
            except Exception as exc:  # noqa: BLE001
                res.note = f"image transcription failed: {exc}"
    else:
        res.note = f"unsupported attachment type {ext or content_type!r}"
    if res.note:
        log.info("attachment %s: %s", filename, res.note)
    return res


def read_attachments(client: Any, mail: Any, llm: Any = None, max_bytes: int = 10_000_000) -> list[AttachmentText]:
    """Download and convert every attachment of a mail (GET .../attachments/{id})."""
    out: list[AttachmentText] = []
    for a in mail.attachments or []:
        if not isinstance(a, dict):
            continue
        aid = a.get("id") or a.get("attachment_id")
        name = str(a.get("filename") or a.get("name") or f"attachment-{aid}")
        ctype = str(a.get("content_type") or "")
        if aid is None:
            continue
        if (a.get("size") or 0) > max_bytes:
            out.append(AttachmentText(name, ctype, "", note="too large"))
            continue
        try:
            data = client.attachment(mail.id, aid)
        except Exception as exc:  # noqa: BLE001
            out.append(AttachmentText(name, ctype, "", note=f"download failed: {exc}"))
            continue
        out.append(to_text(data if isinstance(data, bytes) else str(data).encode(), name, ctype, llm))
    return out
