"""Analyses that read the client's own exports.

Everything before this described files. These read them, and the difference
matters: a description can be wrong and look fine, while a figure that goes
into a report has to be defensible to a finance director who did not want to
hear it.

Three rules from the runbook, enforced here rather than hoped for.

  Never fill a gap. Where a value is missing, the missing value is the
  finding. Nothing is imputed, averaged in, or quietly dropped.

  Every rate states its denominator. A percentage without the record count
  behind it cannot be defended in a readout, so no rate is emitted without
  the count it was computed from.

  Suppress thin cuts. Below ten records a rate is noise wearing a number's
  clothes. The count is reported and the rate is not.

The client's rows stay in storage. They are streamed, counted and discarded;
only the findings reach the database.
"""
from __future__ import annotations

import csv
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import csvjobs
import storage

VERSION = "analysis-1"

# Below this, a rate is not reported. The count still is: "three of these and
# we cannot tell you a rate" is a more honest sentence than a rate computed
# from three.
MIN_FOR_A_RATE = 10


@dataclass
class Column:
    """Where one canonical field actually lives."""
    canonical_key: str
    column_name: str
    storage_path: str
    export_ref: str


@dataclass
class Gap:
    """Something the analysis could not do, and what it cost."""
    what: str
    count: int
    of: int

    def as_dict(self) -> dict:
        return {"what": self.what, "count": self.count, "of": self.of}


def resolve_columns(conn, engagement_id: str, analysis_key: str) -> dict[str, Column]:
    """Which column of which file holds each field this analysis needs.

    Only confirmed mappings. A proposal nobody accepted is not a field the
    client has, and reading one would mean computing a figure from a column
    a person never agreed to.
    """
    with conn.cursor() as cur:
        cur.execute("""
            select cf.key, m.confirmed_field_name, a.storage_path, el.ref
              from analysis_field_requirement r
              join engagement e on e.id = %s
                               and e.instrument_version_id = r.instrument_version_id
              join canonical_field cf on cf.id = r.canonical_field_id
              join field_mapping m on m.canonical_field_id = cf.id
                                  and m.engagement_id = e.id
                                  and m.status in ('confirmed','corrected','auto_accepted')
              join dataset d on d.id = m.dataset_id
              join artefact a on a.id = d.artefact_id and a.purged_at is null
              join export_request_line erl on erl.id = a.export_request_line_id
              join export_line el on el.id = erl.export_line_id
             where r.analysis_key = %s
        """, (engagement_id, analysis_key))
        return {k: Column(k, name, path, ref)
                for k, name, path, ref in cur.fetchall()}


def _band(value: str | None, edges: list[tuple[int, str]]) -> str | None:
    """Put a number in a band, or return None when there is no number.

    None is not a band. A record with no employee count is not a small
    company; it is a record with no employee count, and it is counted as
    such.
    """
    n = csvjobs.as_number(value) if hasattr(csvjobs, "as_number") else None
    if n is None:
        try:
            n = float(str(value).replace(",", "").strip())
        except (TypeError, ValueError):
            return None
    for edge, label in edges:
        if n < edge:
            return label
    return edges[-1][1]


EMPLOYEE_BANDS = [(10, "1 to 9"), (50, "10 to 49"), (250, "50 to 249"),
                  (1000, "250 to 999"), (5000, "1,000 to 4,999"),
                  (10 ** 12, "5,000 and above")]
REVENUE_BANDS = [(1_000_000, "under 1M"), (10_000_000, "1M to 10M"),
                 (50_000_000, "10M to 50M"), (250_000_000, "50M to 250M"),
                 (10 ** 15, "250M and above")]


@dataclass
class Cut:
    """One dimension of the picture, and the records behind it."""
    name: str
    counts: Counter = field(default_factory=Counter)
    missing: int = 0

    def add(self, value: str | None) -> None:
        if value is None or str(value).strip() == "":
            self.missing += 1
        else:
            self.counts[str(value).strip()] += 1

    def as_dict(self, total: int) -> dict:
        known = sum(self.counts.values())
        rows = []
        for value, n in self.counts.most_common():
            row = {"value": value, "count": n, "of": known}
            # A share computed from nine records is not a share.
            if known >= MIN_FOR_A_RATE:
                row["share"] = round(n / known, 4)
            rows.append(row)
        return {
            "dimension": self.name,
            "records_with_a_value": known,
            "records_without_one": self.missing,
            "of": total,
            "suppressed": known < MIN_FOR_A_RATE,
            "values": rows,
        }


def icp_reconciliation(conn, engagement_id: str, heartbeat) -> tuple[int, dict]:
    """Who actually buys, from the client's own closed-won records.

    This reports what the data says. Whether it matches the ICP the client
    believes in is the consultant's call at step 2.2, against the ICP
    document — a thing no export contains.
    """
    cols = resolve_columns(conn, engagement_id, "icp_reconciliation")

    # The six without which there is no analysis at all. The optional ones —
    # segment, product family — sharpen the picture and their absence is
    # reported rather than guessed at.
    needed = ["opportunity_account_id", "opportunity_status", "account_id",
              "account_industry", "account_country", "account_employee_count",
              "account_annual_revenue"]
    absent = [k for k in needed if k not in cols]
    if absent:
        raise ValueError(
            "this analysis needs fields that are not mapped: "
            + ", ".join(absent))

    deals_path = cols["opportunity_status"].storage_path
    accounts_path = cols["account_id"].storage_path

    # The companies file first: a lookup from account to its firmographics.
    heartbeat(0, 2)
    accounts: dict[str, dict] = {}
    with storage.download(accounts_path).open("r", encoding="utf-8-sig",
                                              newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            key = (row.get(cols["account_id"].column_name) or "").strip()
            if not key:
                continue
            accounts[key] = {
                "industry": row.get(cols["account_industry"].column_name),
                "country": row.get(cols["account_country"].column_name),
                "employees": row.get(cols["account_employee_count"].column_name),
                "revenue": row.get(cols["account_annual_revenue"].column_name),
                "segment": (row.get(cols["account_segment"].column_name)
                            if "account_segment" in cols else None),
            }
    heartbeat(1, 2)

    # Then the deals, one at a time. The file is never held in memory.
    won = ("won", "closed won", "closedwon", "closed-won")

    # Only cut by a dimension whose field is actually mapped. A cut reading
    # "0 with a value, 362 without" on an unmapped field says the client
    # leaves it blank, when the truth is that nobody looked. Those are
    # different findings and must not look alike.
    dimensions = [("industry", "account_industry"),
                  ("country", "account_country"),
                  ("employee band", "account_employee_count"),
                  ("revenue band", "account_annual_revenue"),
                  ("segment", "account_segment")]
    cuts = {name: Cut(name) for name, key in dimensions if key in cols}
    not_cut = [name for name, key in dimensions if key not in cols]
    deals = won_deals = no_account = unmatched = 0

    status_col = cols["opportunity_status"].column_name
    account_col = cols["opportunity_account_id"].column_name

    with storage.download(deals_path).open("r", encoding="utf-8-sig",
                                           newline="") as fh:
        for row in csv.DictReader(fh):
            deals += 1
            if (row.get(status_col) or "").strip().lower() not in won:
                continue
            won_deals += 1

            key = (row.get(account_col) or "").strip()
            if not key:
                no_account += 1
                continue
            acct = accounts.get(key)
            if acct is None:
                unmatched += 1
                continue

            if "industry" in cuts:
                cuts["industry"].add(acct["industry"])
            if "country" in cuts:
                cuts["country"].add(acct["country"])
            if "employee band" in cuts:
                cuts["employee band"].add(_band(acct["employees"], EMPLOYEE_BANDS))
            if "revenue band" in cuts:
                cuts["revenue band"].add(_band(acct["revenue"], REVENUE_BANDS))
            if "segment" in cuts:
                cuts["segment"].add(acct["segment"])
    heartbeat(2, 2)

    described = won_deals - no_account - unmatched
    gaps = []
    if no_account:
        gaps.append(Gap("closed-won deals with no account on the record",
                        no_account, won_deals))
    if unmatched:
        gaps.append(Gap("closed-won deals whose account is not in the "
                        "companies export", unmatched, won_deals))

    return described, {
        "analysis": "icp_reconciliation",
        "population": {
            "deals_read": deals,
            "closed_won": won_deals,
            "described": described,
            "accounts_in_the_companies_export": len(accounts),
        },
        "cuts": [c.as_dict(described) for c in cuts.values()],
        "not_cut_because_the_field_is_not_mapped": not_cut,
        "gaps": [g.as_dict() for g in gaps],
        "bands": {
            "employees": [b for _, b in EMPLOYEE_BANDS],
            "revenue": [b for _, b in REVENUE_BANDS],
        },
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "Who actually buys, from the client's own closed-won records. "
                "Whether this matches the ICP they believe in is a judgement "
                "against their ICP document, not something any export can "
                "answer.",
    }


# The analyses that read real data. Everything not here is still a fixture,
# and the database refuses to accept a fixture as evidence.
BUILT = {
    "icp_reconciliation": icp_reconciliation,
}
