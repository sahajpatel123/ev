"""Synthetic fixture files for worlds. Built from bytes, no new dependencies.

- docx / xlsx: minimal Office Open XML packages written with zipfile
- pdf: a minimal PDF with one text line per page (Helvetica)
- png: a PIL image with the given text lines drawn on it
- media: an ffmpeg lavfi test source (testsrc + sine), so durations are exact
"""

from __future__ import annotations

import random
import subprocess
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

_CT = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'


def make_docx(path: Path, item: dict[str, Any]) -> None:
    paras = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{escape(str(p))}</w:t></w:r></w:p>'
        for p in item.get("paragraphs") or []
    )
    document = (
        f'{_CT}<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{paras}</w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", (
            f'{_CT}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'))
        zf.writestr("_rels/.rels", (
            f'{_CT}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="word/document.xml"/></Relationships>'))
        zf.writestr("word/document.xml", document)


def _col(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def make_xlsx(path: Path, item: dict[str, Any]) -> None:
    rows = item.get("rows") or []
    strings: list[str] = []
    xml_rows = []
    for r, row in enumerate(rows, start=1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{_col(c)}{r}"
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cells.append(f'<c r="{ref}"><v>{value}</v></c>')
            else:
                strings.append(str(value))
                cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
        xml_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    sheet = (f'{_CT}<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             f'<sheetData>{"".join(xml_rows)}</sheetData></worksheet>')
    shared = (f'{_CT}<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
              f'count="{len(strings)}" uniqueCount="{len(strings)}">'
              + "".join(f"<si><t>{escape(s)}</t></si>" for s in strings) + "</sst>")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", (
            f'{_CT}<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
            '<Override PartName="/xl/sharedStrings.xml" ContentType="application/'
            'vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/></Types>'))
        zf.writestr("_rels/.rels", (
            f'{_CT}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'))
        zf.writestr("xl/workbook.xml", (
            f'{_CT}<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'))
        zf.writestr("xl/_rels/workbook.xml.rels", (
            f'{_CT}<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/worksheet" Target="worksheets/sheet1.xml"/>'
            '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/sharedStrings" Target="sharedStrings.xml"/></Relationships>'))
        zf.writestr("xl/worksheets/sheet1.xml", sheet)
        zf.writestr("xl/sharedStrings.xml", shared)


def make_pdf(path: Path, item: dict[str, Any]) -> None:
    pages = [str(p) for p in item.get("pages") or ["(empty)"]]
    objects: list[bytes] = []
    kids = []
    font_id = 3 + 2 * len(pages)
    for i, text in enumerate(pages):
        page_id = 3 + 2 * i
        content_id = page_id + 1
        kids.append(f"{page_id} 0 R")
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 18 Tf 72 720 Td ({safe}) Tj ET".encode("latin-1", "replace")
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode())
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    header = [b"<< /Type /Catalog /Pages 2 0 R >>",
              f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>".encode()]
    all_objs = header + objects + [b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(all_objs, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(all_objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(all_objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    if item.get("encrypted"):
        out = out.replace(b"/Root 1 0 R", b"/Root 1 0 R /Encrypt << /Filter /Standard /V 1 /R 2 >>")
    path.write_bytes(bytes(out))


def make_png(path: Path, item: dict[str, Any]) -> None:
    from PIL import Image, ImageDraw, ImageFont

    width, height = int(item.get("width", 1200)), int(item.get("height", 400))
    dark = bool(item.get("dark"))
    img = Image.new("RGB", (width, height), (24, 24, 28) if dark else (250, 250, 250))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", int(item.get("size", 28)))
    except OSError:
        font = ImageFont.load_default()
    y = 30
    for line in item.get("lines") or []:
        draw.text((30, y), str(line), fill=(230, 230, 230) if dark else (20, 20, 20), font=font)
        y += int(item.get("size", 28)) + 14
    img.save(path, "PNG")


def make_media(path: Path, item: dict[str, Any]) -> None:
    duration = float(item.get("duration", 4))
    size = str(item.get("size", "640x360"))
    args = ["ffmpeg", "-v", "error", "-y"]
    if item.get("video", True):
        args += ["-f", "lavfi", "-i", f"testsrc=duration={duration}:size={size}:rate=25"]
    if item.get("audio", True):
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    if item.get("video", True):
        args += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if item.get("audio", True):
        args += ["-c:a", "aac"]
    args += ["-shortest", str(path)]
    subprocess.run(args, capture_output=True, check=True, timeout=120)


_FILLER_TOPICS = (
    ("What time is it in Lisbon?", "It is mid-afternoon in Lisbon right now."),
    ("Remind me to stretch every hour.", "I will remind you to stretch hourly."),
    ("What is the capital of Norway?", "The capital of Norway is Oslo."),
    ("Convert 12 kilometers to miles.", "12 kilometers is about 7.5 miles."),
    ("Name one synthetic crater lake.", "Lake Toba is a well-known crater lake."),
    ("How many days are in June?", "June has 30 days."),
    ("Spell the word fixture backwards.", "E-R-U-T-X-I-F."),
    ("What color is a synthetic fern?", "A synthetic fern is usually green plastic."),
    ("Count from one to five.", "One, two, three, four, five."),
    ("Define the word ledger briefly.", "A ledger is a record of transactions."),
    ("What is 9 times 8?", "9 times 8 is 72."),
    ("Name a synthetic alloy.", "Duralumin is a well-known alloy."),
    ("How tall is a giraffe?", "Adult giraffes stand about 5 meters tall."),
    ("What is the opposite of expand?", "The opposite of expand is contract."),
    ("Recite the NATO letter A.", "Alfa."),
    ("How many wheels on a tricycle?", "A tricycle has three wheels."),
)


def make_thread_turns(count: int, *, seed: int = 1, anchor_index: int = 0,
                      anchor_owner: str = "", anchor_evie: str = "") -> list[dict[str, Any]]:
    """Deterministic synthetic thread of ``count`` turns for scale probes.

    Every filler turn carries a unique ``ledger-code T<index>`` marker so a
    recall oracle can name exactly one turn. The anchor turn (default first)
    carries the probe's own question and answer; its keywords must be unique
    across the thread or the recall oracle cannot be exact.
    """
    if count < 1:
        raise ValueError("thread needs at least 1 turn")
    if not 0 <= anchor_index < count:
        raise ValueError("anchor_index out of range")
    rng = random.Random(seed)
    base = datetime(2026, 1, 1, 9, 0, 0, tzinfo=UTC)
    order = list(range(count))
    turns: list[dict[str, Any]] = []
    for index in order:
        at = (base + timedelta(minutes=7 * index)).isoformat()
        if index == anchor_index:
            owner, evie = anchor_owner, anchor_evie
        else:
            topic = _FILLER_TOPICS[rng.randrange(len(_FILLER_TOPICS))]
            owner = f"{topic[0]} (ledger-code T{index})"
            evie = f"{topic[1]} (ledger-code T{index})"
        turns.append({"owner": owner, "evie": evie, "kind": "typed", "at": at})
    return turns
