"""Reading a client export, and describing what is in it.

Two jobs, both real. Everything else the service declares is still fixture
output.

    parse    what the file is: how many rows, which columns, what date range
             it covers, and how many rows could not be read
    profile  what each column holds: how often it is populated, how many
             distinct values, the most common one and its share

Written against the standard library rather than pandas. A four hundred
thousand row export is read as a stream, so memory stays flat and there is no
binary wheel to install on someone's laptop.

Three rules the methodology insists on and this code keeps:

    Unparseable rows are counted, never dropped. A row with the wrong number
    of fields is a finding about the client's export, not something to skip
    quietly so the totals look tidier.

    No imputation and no defaults. A column that is empty is empty. Nothing
    here fills a gap with a zero, a mean or a guess.

    Every rate carries its denominator. populated_rate is always paired with
    populated_count and the row count it was computed against.
"""
from __future__ import annotations

import csv
import io
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

# csv refuses a field over 128 KB by default. A CRM export blows through that
# routinely: a column of associated records concatenates every one of them into
# a single cell, and a notes column can hold a whole conversation. Raise it as
# far as this build allows, then cap it somewhere a genuinely corrupt file
# still fails rather than eating memory.
MAX_FIELD_BYTES = 64 * 1024 * 1024

_limit = MAX_FIELD_BYTES
while True:
    try:
        csv.field_size_limit(_limit)
        break
    except OverflowError:
        # On some builds this is a C long and smaller than it looks. Step
        # down until it is accepted.
        _limit //= 2
MAX_FIELD_BYTES = _limit


# A column is treated as free text when its values are long and mostly
# distinct. The thresholds are deliberately conservative: calling a picklist
# free text would misreport the client's discipline.
FREE_TEXT_MIN_MEAN_LENGTH = 40
FREE_TEXT_MIN_DISTINCT_RATIO = 0.6
MOSTLY_EMPTY_BELOW = 0.5
HIGH_CARDINALITY_ABOVE = 0.95

DATE_PATTERNS = (
    "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y",
    "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
)
_SLASH_DATE = re.compile(r"^\s*\d{1,2}[/-]\d{1,2}[/-]\d{4}")
_UNDECIDED = object()
NUMERIC = re.compile(r"^-?[\d,]*\.?\d+$")
BOOLEAN = {"true", "false", "yes", "no", "y", "n", "1", "0", "t", "f"}


class ParseError(RuntimeError):
    """The file could not be read at all. The run fails and says why."""


# ------------------------------------------------------------------ reading --

def sniff(path: Path) -> tuple[str, str]:
    """Work out the encoding and the delimiter, or say why not.

    Tried in order of likelihood for a CRM export. utf-8-sig comes first
    because Excel writes a byte order mark and a bare utf-8 read leaves it
    stuck to the first column name, where it silently breaks every mapping.
    """
    sample = path.read_bytes()[:64_000]
    encoding = None
    for candidate in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = sample.decode(candidate)
            encoding = candidate
            break
        except UnicodeDecodeError:
            continue
    if encoding is None:
        raise ParseError(
            "the file is not text in any encoding this service recognises. "
            "If it is a spreadsheet, export it as CSV and upload that.")

    # Sniffer needs whole lines; a truncated last line throws it off.
    text = text[:text.rfind("\n") + 1] or text
    try:
        dialect = csv.Sniffer().sniff(text, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        # Fall back to whichever candidate appears most in the header line.
        header = text.split("\n", 1)[0]
        delimiter = max(",;\t|", key=header.count)
        if header.count(delimiter) == 0:
            raise ParseError(
                "the file has no recognisable column separator in its first "
                "line. A single-column file cannot be profiled.") from None
    return encoding, delimiter


def count_lines(path: Path, encoding: str) -> int:
    """Physical lines, for a progress total that means something.

    Not the row count: a quoted field may contain newlines. The real row count
    comes out of the parse itself.
    """
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            total += chunk.count(b"\n")
    return total


# ------------------------------------------------------------------ parsing --

# A client export can contain bytes that Postgres will not store. The
# commonest is NUL (0x00), which Excel and several CRMs emit as padding
# inside string cells and which a text column cannot hold at all. Reading
# such a file succeeded all the way to the last row and then failed on the
# insert, which looked like a service fault and was a cell.
#
# Cleaning happens once here, where a value is first read, rather than at
# each of the places one is later written. The character carries no meaning
# in exported data and nothing is lost by dropping it.
_UNSTORABLE = str.maketrans("", "", "\x00")


def storable(value: str) -> str:
    """A value Postgres will accept, with nothing meaningful removed."""
    if value is None:
        return value
    return value.translate(_UNSTORABLE) if "\x00" in value else value


@dataclass
class ColumnStats:
    name: str
    populated: int = 0
    distinct: Counter = field(default_factory=Counter)
    distinct_overflow: bool = False
    total_length: int = 0
    date_like: int = 0
    numeric_like: int = 0
    boolean_like: int = 0
    earliest: date | None = None
    latest: date | None = None
    # A few date-shaped values as written, enough to tell a US column from
    # a British one. The question cannot be answered one value at a time.
    date_samples: list = field(default_factory=list)

    # Keeping every distinct value of a free-text column would hold the whole
    # file in memory. Past this the count becomes a floor and is reported as
    # such rather than as a precise figure.
    CAP = 50_000

    _direction: object = _UNDECIDED

    # How many real values to keep for someone deciding what this column is.
    # Distinct, so a column that reads "Closed Won" nine times out of ten
    # shows what else it can be rather than the same word repeated.
    SAMPLES = 10

    @property
    def day_first(self) -> bool | None:
        """Decided once from what has been seen, and cached."""
        if self._direction is _UNDECIDED:
            self._direction = which_way_round(self.date_samples)
        return self._direction

    def settle_dates(self) -> None:
        """Decide the direction, then read the samples again.

        The first values of a column are observed before enough of it has
        been seen to know which way round its dates are. Once the column
        is finished the decision is sound, and the earliest and latest are
        recomputed from the samples under it rather than left as whatever
        the first guess produced.
        """
        self._direction = which_way_round(self.date_samples)
        for v in self.date_samples:
            d = parse_date(v, self._direction)
            if d is None:
                continue
            if self.earliest is None or d < self.earliest:
                self.earliest = d
            if self.latest is None or d > self.latest:
                self.latest = d

    def observe(self, value: str) -> None:
        value = storable(value).strip()
        if value == "":
            return
        self.populated += 1
        self.total_length += len(value)
        if not self.distinct_overflow:
            self.distinct[value] += 1
            if len(self.distinct) > self.CAP:
                self.distinct_overflow = True

        lowered = value.lower()
        if lowered in BOOLEAN:
            self.boolean_like += 1
        if NUMERIC.match(value):
            self.numeric_like += 1
            return
        if len(self.date_samples) < 400 and _SLASH_DATE.match(value):
            self.date_samples.append(value)
        parsed = parse_date(value, self.day_first)
        if parsed is not None:
            self.date_like += 1
            if self.earliest is None or parsed < self.earliest:
                self.earliest = parsed
            if self.latest is None or parsed > self.latest:
                self.latest = parsed


def parse_date(value: str, day_first: bool | None = None) -> date | None:
    """A date, or None.

    10/1/2024 is the first of October to Salesforce and the tenth of
    January to a British CRM, and both parse. Trying one order before the
    other silently transposes every date whose day is twelve or less,
    which is about forty per cent of them, and nothing anywhere looks
    wrong: an opportunity simply moved in January instead of October.

    So the order is decided per column by which_way_round, from the whole
    column rather than one value, and passed in. Where nothing in the
    column settles it, the ISO forms still work and the ambiguous ones are
    read month first, because that is the commoner export.
    """
    head = value[:32]
    patterns = DATE_PATTERNS
    if day_first is None:
        day_first = False
    if day_first is not None:
        ambiguous = ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%m-%d-%Y")
        preferred = (("%d/%m/%Y", "%d-%m-%Y") if day_first
                     else ("%m/%d/%Y", "%m-%d-%Y"))
        patterns = (tuple(p for p in DATE_PATTERNS if p not in ambiguous
                          and "%H" not in p)
                    + preferred
                    + tuple(p for p in DATE_PATTERNS if "%H" in p))
    for pattern in patterns:
        try:
            return datetime.strptime(head, pattern).date()
        except ValueError:
            continue
    return None


def which_way_round(values) -> bool | None:
    """Whether a column's dates are day first, from the column itself.

    A single value cannot say. A column almost always can: one date with
    a first part above twelve settles it, because no month is the
    thirteenth. Returns None where every value is ambiguous, which is rare
    over more than a handful of rows and honest when it happens.
    """
    slash = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})[/-](\d{4})")
    day_first = month_first = 0
    for v in values:
        m = slash.match(str(v or ""))
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        if a > 12 and b <= 12:
            day_first += 1
        elif b > 12 and a <= 12:
            month_first += 1
    if day_first and not month_first:
        return True
    if month_first and not day_first:
        return False
    if day_first or month_first:
        # Both appear, so the column is inconsistent. The commoner wins
        # and the disagreement is worth nothing being confident about.
        return day_first > month_first
    return None


@dataclass
class ParseResult:
    row_count: int
    column_count: int
    malformed_rows: int
    malformed_examples: list[dict]
    columns: list[ColumnStats]
    encoding: str
    delimiter: str
    primary_date_field: str | None
    date_min: date | None
    date_max: date | None


# What a file actually is, read from its first bytes rather than its name.
#
# A spreadsheet renamed .csv is common, and so is a .xlsx uploaded as the
# export it is. Reading either as text produces one enormous column of
# compressed rubbish, a mapping screen offering it as a candidate, and a
# failure several steps later that names none of this.
MAGIC = [
    (b"PK\x03\x04", "xlsx"),      # any zip: xlsx, and also docx and pptx
    (b"\xd0\xcf\x11\xe0", "xls"),  # the old binary Excel format
    (b"%PDF", "pdf"),
]


def what_is_this(path: Path) -> str:
    head = path.read_bytes()[:8]
    for magic, kind in MAGIC:
        if head.startswith(magic):
            return kind

    # Salesforce's "Export to Excel" writes an HTML table and calls it
    # .xls. Excel opens it, so nobody notices, and a reader that trusts
    # the extension finds markup where it expected rows. A client's
    # opportunity history arrived this way: six thousand transitions in a
    # file nothing here could read.
    start = path.read_bytes()[:2048].lstrip().lower()
    if start.startswith((b"<html", b"<!doctype html", b"<head", b"<table",
                         b"<?xml")) or b"<table" in start:
        return "html"
    return "text"


def read_workbook(path: Path, on_progress=None) -> ParseResult:
    """Read the first worksheet of an xlsx, row by row.

    Read-only and streamed, because a client's opportunity export runs to
    tens of thousands of rows and loading one into memory to count it is
    a waste of a laptop.
    """
    import xlsx

    try:
        rows_iter = xlsx.rows(path, on_progress)
        raw_header = next(rows_iter)
    except xlsx.NotAWorkbook as exc:
        raise ParseError(str(exc)) from None
    except StopIteration:
        raise ParseError("the workbook's first sheet is empty.") from None
    except Exception as exc:
        raise ParseError(
            f"that file could not be read as a workbook: {exc}") from None

    header = [storable(h).strip() for h in raw_header]
    while header and header[-1] == "":
        header.pop()
    if not any(header):
        raise ParseError(
            "the first row of the sheet is blank, so there are no column "
            "names. Export it without a title row above the headings.")

    seen: Counter = Counter()
    names: list[str] = []
    for i, name in enumerate(header):
        name = name or f"(unnamed column {i + 1})"
        seen[name] += 1
        names.append(name if seen[name] == 1 else f"{name} ({seen[name]})")

    columns = [ColumnStats(n) for n in names]
    by_name = {c.name: c for c in columns}
    rows = malformed = 0
    malformed_examples: list[dict] = []

    for values in rows_iter:
        if not any(v for v in values):
            continue
        rows += 1
        if len(values) > len(names):
            malformed += 1
            if len(malformed_examples) < 5:
                malformed_examples.append(
                    {"row": rows + 1, "found": len(values),
                     "expected": len(names)})
        for name, value in zip(names, values):
            by_name[name].observe(value)

    for c in columns:
        c.settle_dates()

    if rows == 0:
        raise ParseError("the sheet has column names and no rows.")

    # Same rule as the text path: the date-like column covering the most
    # rows, and nothing invented where none does.
    dated = [c for c in columns
             if c.populated > 0 and c.date_like / c.populated > 0.9
             and c.date_like > 0]
    primary = max(dated, key=lambda c: c.date_like, default=None)

    return ParseResult(
        row_count=rows,
        column_count=len(names),
        malformed_rows=malformed,
        malformed_examples=malformed_examples,
        columns=columns,
        encoding="xlsx",
        delimiter="",
        primary_date_field=primary.name if primary else None,
        date_min=primary.earliest if primary else None,
        date_max=primary.latest if primary else None,
    )


def read_html_table(path: Path, on_progress=None) -> ParseResult:
    """Read the largest table out of an HTML document.

    Salesforce reports exported "to Excel" are HTML, and so are several
    other systems' exports. The markup is plain: one table, a header row
    of th cells, a body of tr and td. No parser library is needed and
    none is carried.

    The largest table, because a report often wraps its data in a layout
    table and the data is the one with the most rows.
    """
    import html as _html

    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "iso-8859-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("iso-8859-1", "replace")

    tables = re.findall(r"<table[^>]*>(.*?)</table>", text, re.S | re.I)
    if not tables:
        raise ParseError(
            "that file is HTML with no table in it, so there are no rows "
            "to read.")
    body = max(tables, key=lambda t: t.count("<tr"))

    cell = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S | re.I)
    tag = re.compile(r"<[^>]+>")

    def read_row(chunk: str) -> list[str]:
        return [storable(_html.unescape(tag.sub("", c))).strip()
                for c in cell.findall(chunk)]

    raw_rows = re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S | re.I)
    if not raw_rows:
        raise ParseError("that table has no rows in it.")

    header = read_row(raw_rows[0])
    while header and header[-1] == "":
        header.pop()
    if not any(header):
        raise ParseError(
            "the first row of that table is blank, so there are no column "
            "names.")

    seen: Counter = Counter()
    names: list[str] = []
    for i, name in enumerate(header):
        name = name or f"(unnamed column {i + 1})"
        seen[name] += 1
        names.append(name if seen[name] == 1 else f"{name} ({seen[name]})")

    columns = [ColumnStats(n) for n in names]
    by_name = {c.name: c for c in columns}
    rows = malformed = 0
    malformed_examples: list[dict] = []

    for chunk in raw_rows[1:]:
        values = read_row(chunk)
        if not any(values):
            continue
        # A Salesforce report ends with grand-total rows that have fewer
        # cells than the header and no record in them. They are not data.
        if len(values) < max(2, len(names) // 2):
            continue
        rows += 1
        if len(values) > len(names):
            malformed += 1
            if len(malformed_examples) < 5:
                malformed_examples.append(
                    {"row": rows + 1, "found": len(values),
                     "expected": len(names)})
        for name, value in zip(names, values):
            by_name[name].observe(value)
        if on_progress and rows % 2000 == 0:
            on_progress(rows, 0)

    for c in columns:
        c.settle_dates()

    if rows == 0:
        raise ParseError("that table has column names and no rows.")

    dated = [c for c in columns
             if c.populated > 0 and c.date_like / c.populated > 0.9
             and c.date_like > 0]
    primary = max(dated, key=lambda c: c.date_like, default=None)

    return ParseResult(
        row_count=rows, column_count=len(names), malformed_rows=malformed,
        malformed_examples=malformed_examples, columns=columns,
        encoding="html", delimiter="",
        primary_date_field=primary.name if primary else None,
        date_min=primary.earliest if primary else None,
        date_max=primary.latest if primary else None,
    )


def read_export(path: Path, on_progress=None) -> ParseResult:
    """Stream the file once, counting everything worth counting."""
    kind = what_is_this(path)
    if kind == "xlsx":
        return read_workbook(path, on_progress)
    if kind == "xls":
        raise ParseError(
            "this is an Excel file in the old binary format, which the "
            "service cannot read. Open it in Excel and save it as .xlsx or "
            "as CSV.")
    if kind == "html":
        return read_html_table(path, on_progress)
    if kind == "pdf":
        raise ParseError(
            "this is a PDF. An export has to be a spreadsheet or a CSV; a "
            "PDF of a report cannot be read field by field.")

    encoding, delimiter = sniff(path)
    lines = count_lines(path, encoding)

    malformed = 0
    malformed_examples: list[dict] = []
    rows = 0

    with path.open("r", encoding=encoding, newline="") as handle:
        reader = csv.reader(handle, delimiter=delimiter)
        try:
            header = next(reader)
        except StopIteration:
            raise ParseError("the file is empty.") from None

        header = [storable(h).strip() for h in header]
        if not any(header):
            raise ParseError("the first line is blank, so there are no column names.")

        # Duplicate and unnamed columns are real and have to survive, because
        # a mapping screen has to show them as they are.
        seen: Counter = Counter()
        names: list[str] = []
        for i, name in enumerate(header):
            name = name or f"(unnamed column {i + 1})"
            seen[name] += 1
            names.append(name if seen[name] == 1 else f"{name} ({seen[name]})")

        columns = [ColumnStats(n) for n in names]
        width = len(names)

        while True:
            try:
                row = next(reader)
            except StopIteration:
                break
            except csv.Error as exc:
                # A field longer than the cap, or a quote that never closes.
                # Either way the file cannot be read past this point, and
                # saying where is more use than the library's own wording.
                raise ParseError(
                    f"the file could not be read past line {reader.line_num:,}: "
                    f"{exc}. A single value longer than "
                    f"{MAX_FIELD_BYTES // (1024 * 1024)} MB, or a quotation "
                    f"mark that is never closed, will do this."
                ) from None
            rows += 1
            if len(row) != width:
                malformed += 1
                if len(malformed_examples) < 10:
                    malformed_examples.append({
                        "line": reader.line_num,
                        "fields_found": len(row),
                        "fields_expected": width,
                    })
                # Counted, not dropped. Values that line up are still observed.
            for i in range(min(len(row), width)):
                columns[i].observe(row[i])

            if on_progress and rows % 1000 == 0:
                on_progress(rows, max(lines - 1, rows))

        if on_progress:
            on_progress(rows, rows)

    # The primary date field is the date-like column covering the most rows.
    # Nothing is invented: if no column parses as dates, this stays empty.
    for c in columns:
        c.settle_dates()

    dated = [c for c in columns
             if c.populated > 0 and c.date_like / c.populated > 0.9 and c.date_like > 0]
    primary = max(dated, key=lambda c: c.date_like, default=None)

    return ParseResult(
        row_count=rows,
        column_count=width,
        malformed_rows=malformed,
        malformed_examples=malformed_examples,
        columns=columns,
        encoding=encoding,
        delimiter=delimiter,
        primary_date_field=primary.name if primary else None,
        date_min=primary.earliest if primary else None,
        date_max=primary.latest if primary else None,
    )


# ---------------------------------------------------------------- profiling --

def profile_column(c: ColumnStats, rows: int) -> dict:
    """One column, described. Every rate carries the count behind it."""
    populated_rate = (c.populated / rows) if rows else 0.0
    distinct = None if c.distinct_overflow else len(c.distinct)
    top_value, top_count = (c.distinct.most_common(1) or [(None, 0)])[0]
    top_share = (top_count / c.populated) if c.populated else None

    mean_length = (c.total_length / c.populated) if c.populated else 0
    distinct_ratio = (distinct / c.populated) if distinct and c.populated else 0
    is_free_text = (mean_length >= FREE_TEXT_MIN_MEAN_LENGTH
                    and distinct_ratio >= FREE_TEXT_MIN_DISTINCT_RATIO)

    if c.populated == 0:
        inferred = "empty"
    elif c.numeric_like / c.populated > 0.95:
        inferred = "number"
    elif c.date_like / c.populated > 0.9:
        inferred = "date"
    elif c.boolean_like / c.populated > 0.95:
        inferred = "boolean"
    elif is_free_text:
        inferred = "free text"
    else:
        inferred = "text"

    flags = []
    if c.populated == 0:
        flags.append("always empty")
    elif populated_rate < MOSTLY_EMPTY_BELOW:
        flags.append("mostly empty")
    if distinct == 1:
        flags.append("one value throughout")
    if top_share is not None and top_share > 0.9 and distinct and distinct > 1:
        flags.append("dominated by one value")
    if distinct_ratio > HIGH_CARDINALITY_ABOVE and c.populated > 20 and not is_free_text:
        flags.append("almost every row differs")
    if c.distinct_overflow:
        flags.append("too many distinct values to count exactly")

    return {
        "field_name": c.name,
        "inferred_type": inferred,
        "populated_count": c.populated,
        "populated_rate": round(populated_rate, 6),
        "distinct_count": distinct,
        "top_value": (top_value[:500] if top_value else None),
        "top_value_share": (round(top_share, 6) if top_share is not None else None),
        "is_free_text": is_free_text,
        "flags": flags,
        # Longest-first would surface the outliers; most common first shows
        # what the column usually holds, which is what identifies it.
        "sample_values": [v for v, _ in c.distinct.most_common(ColumnStats.SAMPLES)],
    }


def rows_of(path: Path, result: ParseResult):
    """Read the file again, yielding each row as a dict.

    A second pass rather than keeping the rows from the first. The first
    pass exists to count and profile without holding anything, and making
    it accumulate rows as well would mean a large export sitting in memory
    twice over. Reading a file twice is cheap; running out of memory on a
    client's export is not.
    """
    names = [c.name for c in result.columns]

    # Dates are written as the client wrote them, normalised to ISO using
    # the direction decided for each column. Downstream there is then
    # nothing left to guess: a stored date means one thing.
    direction = {c.name: c.day_first for c in result.columns
                 if c.date_like and c.populated
                 and c.date_like / c.populated > 0.5}

    def fixed(name: str, value: str) -> str:
        if name not in direction or not value:
            return value
        d = parse_date(value, direction[name])
        return d.isoformat() if d else value
    if result.encoding == "html":
        import html as _html
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "utf-8", "iso-8859-1"):
            try:
                text = raw.decode(enc); break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("iso-8859-1", "replace")
        tables = re.findall(r"<table[^>]*>(.*?)</table>", text, re.S | re.I)
        body = max(tables, key=lambda t: t.count("<tr"))
        cell = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S | re.I)
        tag = re.compile(r"<[^>]+>")
        chunks = re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S | re.I)
        for chunk in chunks[1:]:
            values = [storable(_html.unescape(tag.sub("", c))).strip()
                      for c in cell.findall(chunk)]
            if not any(values) or len(values) < max(2, len(names) // 2):
                continue
            yield {n: fixed(n, v) for n, v in zip(names, values) if v != ""}
        return

    if result.encoding == "xlsx":
        import xlsx
        stream = xlsx.rows(path)
        next(stream, None)                       # the header
        for values in stream:
            if not any(values):
                continue
            yield {n: fixed(n, storable(v).strip())
                   for n, v in zip(names, values) if v != ""}
        return

    with path.open("r", encoding=result.encoding, newline="") as handle:
        reader = csv.reader(handle, delimiter=result.delimiter)
        next(reader, None)
        for values in reader:
            if not any(values):
                continue
            yield {n: fixed(n, storable(v).strip())
                   for n, v in zip(names, values) if v.strip() != ""}
