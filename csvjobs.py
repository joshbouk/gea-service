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
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

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

    # Keeping every distinct value of a free-text column would hold the whole
    # file in memory. Past this the count becomes a floor and is reported as
    # such rather than as a precise figure.
    CAP = 50_000

    def observe(self, value: str) -> None:
        value = value.strip()
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
        parsed = parse_date(value)
        if parsed is not None:
            self.date_like += 1
            if self.earliest is None or parsed < self.earliest:
                self.earliest = parsed
            if self.latest is None or parsed > self.latest:
                self.latest = parsed


def parse_date(value: str) -> date | None:
    head = value[:32]
    for pattern in DATE_PATTERNS:
        try:
            return datetime.strptime(head, pattern).date()
        except ValueError:
            continue
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


def read_export(path: Path, on_progress=None) -> ParseResult:
    """Stream the file once, counting everything worth counting."""
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

        header = [h.strip() for h in header]
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

        for row in reader:
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
    }
