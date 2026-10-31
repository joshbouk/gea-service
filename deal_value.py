"""What each deal is worth.

Counting deals answers half the question. A client whose conferences
produce two opportunities from fifty-six has a problem; whether those
two were worth four hundred thousand or four thousand decides how big a
problem, and which other channel should have had the money. Every
analysis that groups deals was counting them and stopping there.

So value is read once, per deal, and handed to whatever is grouping.
Each group then carries both: how many, and how much. A reader who sees
only one of those will reach for the other.

Where a client records no value, every analysis still runs and the
value simply is not there. The field is optional everywhere it is used
for exactly that reason: a figure nobody records is a finding about the
client, not a reason to produce nothing.
"""
from __future__ import annotations

from collections import defaultdict


# How many of a thing it takes before a total is worth reading. A mean
# over three deals describes those three deals.
ENOUGH_TO_MEAN = 5


def clean(value) -> float | None:
    """A number from whatever the export put in the cell.

    Currency symbols, thousands separators and a trailing minus are all
    things a CRM exports and none of them are the figure. An empty cell
    is not nothing; it is unknown, and the two are different when a
    reader asks what the total covers.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    negative = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "").replace("$", "").replace("£", "")
    s = s.replace("€", "").strip()
    if s.endswith("-"):
        negative, s = True, s[:-1]
    try:
        n = float(s)
    except ValueError:
        return None
    return -n if negative else n


def by_deal(conn, cols, rows_of) -> dict[str, float]:
    """What each deal is worth, by its id.

    Reads the value column on the deals export once. Returns nothing
    where no value field is mapped, which every caller treats as "this
    client does not record it" rather than as an error.
    """
    field = next((k for k in ("opportunity_amount", "arr_amount")
                  if k in cols), None)
    if field is None or "opportunity_id" not in cols:
        return {}
    id_col = cols["opportunity_id"].column_name
    value_col = cols[field].column_name
    out: dict[str, float] = {}
    for row in rows_of(conn, cols[field]):
        deal = (row.get(id_col) or "").strip()
        if not deal:
            continue
        amount = clean(row.get(value_col))
        if amount is not None:
            out[deal] = amount
    return out


class Worth:
    """How many, and how much, for each of several groups.

    Kept together because a group's count and its value are two halves
    of one answer and separating them invites a reader to quote
    whichever suits. Deals with no recorded value are counted
    separately, so a total can say what it covers.
    """

    def __init__(self, what: str = "deals"):
        self.what = what
        self.count: dict[str, int] = defaultdict(int)
        self.total: dict[str, float] = defaultdict(float)
        self.priced: dict[str, int] = defaultdict(int)

    def add(self, group: str, amount: float | None) -> None:
        group = (group or "not recorded").strip() or "not recorded"
        self.count[group] += 1
        if amount is not None:
            self.total[group] += amount
            self.priced[group] += 1

    @property
    def deals(self) -> int:
        return sum(self.count.values())

    @property
    def value(self) -> float:
        return sum(self.total.values())

    def rows(self, biggest_first: bool = True) -> list[dict]:
        """One row per group, ordered by what a reader would look at first.

        By value where there is any, because the question behind every
        one of these is where the money is. By count otherwise.
        """
        has_value = any(self.priced.values())
        groups = sorted(
            self.count,
            key=(lambda g: -self.total[g]) if (has_value and biggest_first)
            else (lambda g: -self.count[g]))
        out = []
        for g in groups:
            n, priced = self.count[g], self.priced[g]
            row = {
                self.what: g,
                "count": n,
                "share": round(n / self.deals, 4) if self.deals else None,
            }
            if has_value:
                row["value"] = round(self.total[g], 2)
                row["share_of_value"] = (round(self.total[g] / self.value, 4)
                                         if self.value else None)
                # What the value covers. A total over nine of forty
                # deals is a real number about nine deals.
                row["priced"] = priced
                row["mean"] = (round(self.total[g] / priced, 2)
                               if priced >= ENOUGH_TO_MEAN else None)
            out.append(row)
        return out

    def as_dict(self, label: str) -> dict:
        has_value = any(self.priced.values())
        out = {label: self.rows()}
        if has_value:
            out[f"{label}_total_value"] = round(self.value, 2)
            unpriced = self.deals - sum(self.priced.values())
            if unpriced:
                out[f"{label}_with_no_value_recorded"] = unpriced
        return out
