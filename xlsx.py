"""Reading an xlsx with nothing but the standard library.

openpyxl would do this in ten lines. It is also a dependency, and the
machine this runs on could not reach the package index — which is a
corporate network doing its job rather than a fault, and which will
happen again on the next laptop.

An xlsx is a zip of XML. The parts that matter are small:

  xl/workbook.xml          names the sheets and their relationship ids
  xl/_rels/workbook.xml.rels   maps those ids to file paths
  xl/sharedStrings.xml     every repeated string, by index
  xl/styles.xml            which formats are dates
  xl/worksheets/sheetN.xml the cells

Everything is streamed. A client's opportunity export runs to tens of
thousands of rows and there is no reason to hold one in memory.

Three things that look like details and are not:

  A row skips empty cells, so the cell reference is the only way to know
  which column a value belongs to. Reading them in order puts every value
  after a gap into the wrong column, and nothing downstream would notice.

  A date is a number. Excel stores 45,000 and a format code; whether it is
  a date is in styles.xml, not in the cell. Without that, every date in
  the export reads as a five-digit integer.

  The 1900 leap year bug is real. Excel believes 1900 was a leap year, so
  serial numbers after the 59th are one day out unless corrected.
"""
from __future__ import annotations

import re
import zipfile
from datetime import date, datetime, timedelta
from xml.etree import ElementTree as ET

NS = {
    "m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "p": "http://schemas.openxmlformats.org/package/2006/relationships",
}

# The built-in format ids Excel reserves for dates and times.
BUILTIN_DATES = set(range(14, 23)) | set(range(45, 48)) | {27, 30, 36, 50, 57}
_DATEY = re.compile(r"[dmyhs]", re.I)
_NOT_A_FORMAT = re.compile(r'"[^"]*"|\[[^\]]*\]|\\.')
_CELL_REF = re.compile(r"([A-Z]+)(\d+)")

EPOCH = datetime(1899, 12, 30)


class NotAWorkbook(RuntimeError):
    pass


def _column_index(ref: str) -> int:
    """AA3 is column 27. Letters are base 26 with no zero."""
    m = _CELL_REF.match(ref or "")
    if not m:
        return -1
    n = 0
    for ch in m.group(1):
        n = n * 26 + (ord(ch) - 64)
    return n - 1


def _date_formats(z: zipfile.ZipFile) -> set[int]:
    """Which style indexes mean the number in the cell is a date."""
    try:
        root = ET.fromstring(z.read("xl/styles.xml"))
    except KeyError:
        return set()

    datey_fmt_ids = set(BUILTIN_DATES)
    for fmt in root.iterfind(".//m:numFmts/m:numFmt", NS):
        code = fmt.get("formatCode") or ""
        # Quoted text and bracketed sections are not format characters, and
        # a currency symbol such as [$-409] is full of letters that look
        # like a date if read naively.
        bare = _NOT_A_FORMAT.sub("", code)
        if _DATEY.search(bare):
            datey_fmt_ids.add(int(fmt.get("numFmtId")))

    out = set()
    xfs = root.find("m:cellXfs", NS)
    if xfs is None:
        return out
    for i, xf in enumerate(xfs.iterfind("m:xf", NS)):
        if int(xf.get("numFmtId", 0)) in datey_fmt_ids:
            out.add(i)
    return out


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    try:
        data = z.read("xl/sharedStrings.xml")
    except KeyError:
        return []
    out = []
    for si in ET.fromstring(data).iterfind("m:si", NS):
        # A string can be split into runs by formatting, and the runs have
        # to be joined or half the word is lost.
        out.append("".join(t.text or "" for t in si.iter(
            "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")))
    return out


def _first_sheet_path(z: zipfile.ZipFile) -> str:
    """The first sheet as the workbook orders it, not as the zip does.

    sheet1.xml is usually right and is not always: a workbook whose first
    tab was deleted and recreated can have the visible first sheet stored
    as sheet3.xml, and reading the wrong one silently reads the wrong data.
    """
    try:
        wb = ET.fromstring(z.read("xl/workbook.xml"))
        rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    except KeyError:
        return "xl/worksheets/sheet1.xml"

    by_id = {rel.get("Id"): rel.get("Target")
             for rel in rels.iterfind("p:Relationship", NS)}
    rid_attr = f"{{{NS['r']}}}id"
    for sheet in wb.iterfind(".//m:sheets/m:sheet", NS):
        target = by_id.get(sheet.get(rid_attr))
        if not target:
            continue
        target = target.lstrip("/")
        return target if target.startswith("xl/") else f"xl/{target}"
    return "xl/worksheets/sheet1.xml"


def sheet_names(path) -> list[str]:
    with zipfile.ZipFile(path) as z:
        try:
            wb = ET.fromstring(z.read("xl/workbook.xml"))
        except KeyError:
            return []
        return [s.get("name") for s in wb.iterfind(".//m:sheets/m:sheet", NS)]


def _value(cell, strings, date_styles) -> str:
    kind = cell.get("t")
    style = cell.get("s")

    if kind == "inlineStr":
        return "".join(t.text or "" for t in cell.iter(
            "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t"))

    v = cell.find("m:v", NS)
    if v is None or v.text is None:
        return ""
    raw = v.text

    if kind == "s":
        try:
            return strings[int(raw)]
        except (ValueError, IndexError):
            return ""
    if kind == "e":
        return raw            # #N/A and friends are worth keeping as found
    if kind == "b":
        return "true" if raw == "1" else "false"
    if kind == "str":
        return raw

    # A number. Whether it is a date lives in the style, not the cell.
    if style is not None and int(style) in date_styles:
        try:
            serial = float(raw)
        except ValueError:
            return raw
        # Excel believes 1900 was a leap year. Everything after 28 February
        # 1900 is one day out unless this is undone.
        if serial < 60:
            serial += 1
        when = EPOCH + timedelta(days=serial)
        if when.time() == datetime.min.time():
            return when.date().isoformat()
        return when.isoformat(sep=" ")

    if raw.endswith(".0"):
        return raw[:-2]
    try:
        f = float(raw)
        if f.is_integer():
            return str(int(f))
    except ValueError:
        pass
    return raw


def rows(path, on_progress=None):
    """Yield each row of the first sheet as a list of strings.

    A row is padded to the width of the header, and a value lands in the
    column its cell reference names rather than the position it appears in.
    """
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if "xl/workbook.xml" not in names:
            raise NotAWorkbook(
                "that is a zip file but not a spreadsheet. An export has to "
                "be a workbook or a CSV.")

        strings = _shared_strings(z)
        date_styles = _date_formats(z)
        sheet = _first_sheet_path(z)
        if sheet not in names:
            sheet = next((n for n in sorted(names)
                          if n.startswith("xl/worksheets/sheet")), None)
            if sheet is None:
                raise NotAWorkbook("that workbook has no worksheets in it.")

        width = 0
        count = 0
        with z.open(sheet) as handle:
            for _event, el in ET.iterparse(handle, events=("end",)):
                if not el.tag.endswith("}row"):
                    continue
                cells: dict[int, str] = {}
                for c in el:
                    if not c.tag.endswith("}c"):
                        continue
                    i = _column_index(c.get("r", ""))
                    if i < 0:
                        i = len(cells)
                    text = _value(c, strings, date_styles)
                    if text != "":
                        cells[i] = text
                el.clear()

                if not cells:
                    # A row of empty cells still occupies a row in the file
                    # and is not a row of data.
                    if width:
                        count += 1
                    continue
                highest = max(cells) + 1
                width = max(width, highest)
                yield [cells.get(i, "") for i in range(width)]
                count += 1
                if on_progress and count % 2000 == 0:
                    on_progress(count, 0)
