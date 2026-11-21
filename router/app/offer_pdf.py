"""The quote as a one-file PDF, for customers who ask for the offer as PDF.

ponytail: hand-written PDF (Helvetica, cp1252, plain text), no dependency; a layout
library only if the offer ever needs a logo or tables.
"""

import textwrap
import zlib


def _esc(line: str) -> str:
    return line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def offer_pdf(subject: str, body: str) -> bytes:
    """A4 pages of the quote mail's text, the subject as title."""
    lines = [f"\x00{subject}", ""]  # \x00 marks the bold title line
    for para in body.replace("\r", "").split("\n"):
        lines += textwrap.wrap(para, 95) or [""]
    pages = [lines[i : i + 60] for i in range(0, len(lines), 60)]

    objs = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"]  # fmt: skip
    kids = []
    for page in pages:
        ops = ["BT", "14 TL", "50 790 Td"]
        for line in page:
            font = "/F2 12" if line.startswith("\x00") else "/F1 10"
            ops.append(f"{font} Tf ({_esc(line.lstrip(chr(0)))}) Tj T*")
        ops.append("ET")
        stream = zlib.compress("\n".join(ops).encode("cp1252", "replace"))
        objs.append(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream")
        objs.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
                    b"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents %d 0 R >>" % len(objs))  # fmt: skip
        kids.append(f"{len(objs)} 0 R")
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>".encode()

    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for n, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)
