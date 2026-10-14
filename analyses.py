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

import re
import csv
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from dataclasses import dataclass, field

import csvjobs
import model
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
    dataset_id: str = ""


def _col_for_path(cols: dict, path: str) -> "Column":
    """Any column from the file at this path.

    Some analyses pick a file once and read several of its columns, naming
    the file rather than a field. The rows are the same whichever of its
    columns asks for them.
    """
    for c in cols.values():
        if c.storage_path == path:
            return c
    raise ValueError("no mapped column belongs to that file")


def rows_of(conn, column: "Column") -> list[dict]:
    """Every row of the file this column is in, from the database.

    Analyses used to download the file and read it with a CSV reader.
    That silently produced nothing on an xlsx export: the reader finds no
    delimiters in a zip, so every row comes back as one unparseable field
    and the analysis reports zeros rather than failing. A client's
    opportunity export arrived that way and the pricing analysis reported
    nought closed-won deals, which read as a finding about them.

    The rows have been in the database since the row store was built. No
    analysis has any reason to touch a file again, and reading from the
    database joins, filters and counts without downloading anything.
    """
    if not column.dataset_id:
        raise ValueError(
            f"the {column.canonical_key} mapping has no dataset behind it, "
            f"so its rows cannot be read")
    with conn.cursor() as cur:
        cur.execute("""
            select data from dataset_row
             where dataset_id = %s order by row_number
        """, (column.dataset_id,))
        rows = [r[0] for r in cur.fetchall()]
    conn.rollback()
    return rows


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

    The confirmed name, or the proposed one where a mapping was accepted
    automatically and never carried a confirmed name of its own. The
    screen was fixed to read both a day before this was; the service was
    not, so every auto-accepted field resolved to a column called nothing.
    Nothing failed: the analysis read a column name of null from every
    row, found nothing in it, and reported that the client's data was
    empty.

    Only settled mappings. A proposal nobody accepted is not a field the
    client has, and reading one would mean computing a figure from a column
    a person never agreed to.

    The export reference is looked up where it can be, and left empty where
    it cannot. It is used for reporting and nothing else, and requiring it
    made a confirmed mapping invisible the moment its file lost its link to
    the export line — which is exactly what replacing a file does. The
    screen went on showing the field as mapped while the analysis reported
    it missing, and both were reading the truth they had.
    """
    with conn.cursor() as cur:
        cur.execute("""
            select cf.key,
                   coalesce(m.confirmed_field_name, m.proposed_field_name),
                   a.storage_path, el.ref,
                   d.id::text
              from analysis_field_requirement r
              join engagement e on e.id = %s
                               and e.instrument_version_id = r.instrument_version_id
              join canonical_field cf on cf.id = r.canonical_field_id
              join field_mapping m on m.canonical_field_id = cf.id
                                  and m.engagement_id = e.id
                                  and m.status in ('confirmed','corrected','auto_accepted')
              join dataset d on d.id = m.dataset_id
              join artefact a on a.id = d.artefact_id and a.purged_at is null
              left join export_request_line erl on erl.id = a.export_request_line_id
              left join export_line el on el.id = erl.export_line_id
             where r.analysis_key = %s
        """, (engagement_id, analysis_key))
        found = {k: Column(k, name, path, ref or "", ds)
                 for k, name, path, ref, ds in cur.fetchall()}

    # A settled mapping with no column name is a broken record, not an
    # absent field, and the two must not look alike: reading from a column
    # called nothing returns nothing from every row, and the analysis then
    # says the client's data is empty.
    nameless = sorted(k for k, c in found.items() if not c.column_name)
    if nameless:
        raise ValueError(
            f"{', '.join(nameless)} {'is' if len(nameless) == 1 else 'are'} "
            f"recorded as mapped and no column is named. Open the field "
            f"mapping and choose the column again.")
    return found


def missing_fields(conn, engagement_id: str, analysis_key: str,
                   needed: list[str], cols: dict) -> str | None:
    """Why an analysis cannot run, told apart properly.

    A field can be absent for two reasons that look identical from here and
    need completely different work. Either nobody mapped it, or the
    engagement is pinned to an instrument version that never asked for it —
    and the second happens whenever the methodology gains a field after an
    engagement starts, which it has done four times.

    Saying "not mapped" in the second case sends somebody to a screen where
    the field is already mapped, or is not listed at all. Both are a waste
    of their afternoon.
    """
    absent = [k for k in needed if k not in cols]
    if not absent:
        return None

    with conn.cursor() as cur:
        cur.execute("""
            select cf.key
              from analysis_field_requirement r
              join engagement e on e.id = %s
                               and e.instrument_version_id = r.instrument_version_id
              join canonical_field cf on cf.id = r.canonical_field_id
             where r.analysis_key = %s and cf.key = any(%s)
        """, (engagement_id, analysis_key, absent))
        declared = {row[0] for row in cur.fetchall()}
        cur.execute("""
            select iv.label from engagement e
              join instrument_version iv on iv.id = e.instrument_version_id
             where e.id = %s""", (engagement_id,))
        version = cur.fetchone()[0]
    conn.rollback()

    unmapped = sorted(k for k in absent if k in declared)
    undeclared = sorted(k for k in absent if k not in declared)

    parts = []
    if unmapped:
        parts.append("this analysis needs fields that are not mapped: "
                     + ", ".join(unmapped))
    if undeclared:
        parts.append(
            f"this engagement runs on instrument {version}, which does not "
            f"ask for {', '.join(undeclared)}. The analysis was built against "
            f"a later version. Start a new engagement on the current "
            f"instrument, or the figures would be computed from fields this "
            f"one never required")
    return ". ".join(parts)


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


# Values that occupy the field without answering it. A loss reason of
# "Other" does not say why the deal was lost; it says the person closing it
# had somewhere to click that was not a reason. Counting these as the leading
# answer turns a tracking failure into a finding about the market, which is
# the opposite of true.
NON_ANSWERS = {
    "other", "others", "n/a", "na", "none", "no reason", "not applicable",
    "unknown", "unspecified", "not specified", "not set", "tbd",
    "to be determined", "misc", "miscellaneous", "default", "-", "--",
    "blank", "null", "nil", "no", "n a", "not recorded", "not stated",
    "undefined", "general", "no answer",
}


def is_a_non_answer(value: str) -> bool:
    return str(value).strip().lower().strip(".") in NON_ANSWERS


@dataclass
class Cut:
    """One dimension of the picture, and the records behind it.

    Three buckets, never two. A record can carry a real value, carry a value
    that is not an answer, or carry nothing. Collapsing the last two loses
    the difference between a field nobody fills in and a field everyone fills
    in with "Other" — and the second is the worse finding, because it looks
    like compliance.
    """
    name: str
    counts: Counter = field(default_factory=Counter)
    non_answers: Counter = field(default_factory=Counter)
    missing: int = 0

    def add(self, value: str | None) -> None:
        if value is None or str(value).strip() == "":
            self.missing += 1
        elif is_a_non_answer(value):
            self.non_answers[str(value).strip()] += 1
        else:
            self.counts[str(value).strip()] += 1

    @property
    def answered(self) -> int:
        return sum(self.counts.values())

    @property
    def not_answered(self) -> int:
        return sum(self.non_answers.values()) + self.missing

    def as_dict(self, total: int) -> dict:
        known = self.answered
        rows = []
        for value, n in self.counts.most_common():
            row = {"value": value, "count": n, "of": known}
            # A share computed from nine records is not a share.
            if known >= MIN_FOR_A_RATE:
                row["share"] = round(n / known, 4)
            rows.append(row)
        held = sum(self.non_answers.values())
        return {
            "dimension": self.name,
            "records_with_an_answer": known,
            "records_with_a_non_answer": held,
            "records_with_nothing": self.missing,
            "of": total,
            # What a consultant needs first: how much of this field is unusable.
            "unusable_share": (round((held + self.missing) / total, 4)
                               if total >= MIN_FOR_A_RATE else None),
            "non_answers_used": [{"value": v, "count": n}
                                 for v, n in self.non_answers.most_common()],
            "suppressed": known < MIN_FOR_A_RATE,
            "values": rows,
        }


def load_accounts(conn, cols: dict[str, Column]) -> dict[str, dict]:
    """A lookup from account to its firmographics, from the companies export.

    Held in memory because it is one row per customer, not one per deal: a
    client with twenty thousand accounts is a few megabytes. The deals file,
    which is the big one, is never held.
    """
    out: dict[str, dict] = {}
    if "account_id" not in cols:
        return out
    for row in rows_of(conn, cols["account_id"]):
        key = (row.get(cols["account_id"].column_name) or "").strip()
        if not key:
            continue
        out[key] = {
                name: row.get(cols[field].column_name)
                for name, field in (("industry", "account_industry"),
                                    ("country", "account_country"),
                                    ("employees", "account_employee_count"),
                                    ("revenue", "account_annual_revenue"),
                                    ("segment", "account_segment"))
                if field in cols
            }
    return out


def firmographic_cuts(cols: dict[str, Column]) -> tuple[dict[str, Cut], list[str]]:
    """The dimensions a firmographic picture is cut by, and those not mapped."""
    dims = [("industry", "account_industry"), ("country", "account_country"),
            ("employee band", "account_employee_count"),
            ("revenue band", "account_annual_revenue"),
            ("segment", "account_segment")]
    return ({n: Cut(n) for n, k in dims if k in cols},
            [n for n, k in dims if k not in cols])


def add_firmographics(cuts: dict[str, Cut], acct: dict) -> None:
    if "industry" in cuts:
        cuts["industry"].add(acct.get("industry"))
    if "country" in cuts:
        cuts["country"].add(acct.get("country"))
    if "employee band" in cuts:
        cuts["employee band"].add(_band(acct.get("employees"), EMPLOYEE_BANDS))
    if "revenue band" in cuts:
        cuts["revenue band"].add(_band(acct.get("revenue"), REVENUE_BANDS))
    if "segment" in cuts:
        cuts["segment"].add(acct.get("segment"))


def money(value) -> float | None:
    """A number, or None. An unparseable figure is not a zero."""
    if value is None:
        return None
    t = str(value).strip().replace(",", "").replace("$", "").replace("£", "")
    t = t.replace("€", "")
    if t in ("", "-"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


# A list of exact stage names cannot survive contact with a real CRM.
# "6 - Closed Won", "Closed Won (Implemented)", "Closed Lost to Competitor"
# are all ordinary and none of them matches a fixed string. So the word is
# looked for inside the stage rather than compared to it.
#
# Lost is checked first, because a stage can mention both — "Closed Lost,
# Won by Competitor" is not a win.
NOT_A_WIN = ("not won", "no decision", "never won", "not closed won")


def outcome(value) -> str | None:
    """Won, lost, or neither. Neither is open, and open is not a loss."""
    t = " ".join(str(value or "").strip().lower().split())
    if not t:
        return None
    if any(n in t for n in NOT_A_WIN):
        return "lost" if "lost" in t else None
    if "lost" in t:
        return "lost"
    if "won" in t or t == "win":
        return "won"
    return None


@dataclass
class Outcomes:
    """What the stage column actually contained.

    An analysis that classified nothing has not found that the client never
    won a deal. It has found that it cannot read their stage names, and the
    difference is the whole difference between a finding and a bug. So what
    was seen is kept, and reported when nothing matched.
    """
    seen: Counter = field(default_factory=Counter)

    def classify(self, value) -> str | None:
        result = outcome(value)
        self.seen[(str(value or "").strip() or "(empty)", result or "neither")] += 1
        return result

    @property
    def classified(self) -> int:
        return sum(n for (_v, r), n in self.seen.items() if r != "neither")

    def refuse_if_nothing_matched(self, column: str) -> None:
        if self.classified:
            return
        top = ", ".join(f"{v!r} ({n})" for (v, _r), n in self.seen.most_common(8))
        raise ValueError(
            f"none of the values in {column} could be read as won or lost, so "
            f"there is nothing to measure. What is in that column: {top}. "
            f"Either the wrong column is mapped, or this client records "
            f"outcomes in words that need adding to the methodology.")

    def as_dict(self) -> dict:
        return {"stage_values_seen": [
            {"value": v, "read_as": r, "deals": n}
            for (v, r), n in self.seen.most_common(20)]}


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
    problem = missing_fields(conn, engagement_id, "icp_reconciliation",
                             needed, cols)
    if problem:
        raise ValueError(problem)

    deals_path = cols["opportunity_status"].storage_path
    accounts_path = cols["account_id"].storage_path

    # The companies file first: a lookup from account to its firmographics.
    seen = Outcomes()
    heartbeat(0, 2)
    accounts = load_accounts(conn, cols)
    heartbeat(1, 2)

    # Then the deals, one at a time. The file is never held in memory.
    # Only cut by a dimension whose field is actually mapped. A cut reading
    # "0 with a value, 362 without" on an unmapped field says the client
    # leaves it blank, when the truth is that nobody looked. Those are
    # different findings and must not look alike.
    cuts, not_cut = firmographic_cuts(cols)
    deals = won_deals = no_account = unmatched = 0

    status_col = cols["opportunity_status"].column_name
    account_col = cols["opportunity_account_id"].column_name

    for row in rows_of(conn, _col_for_path(cols, deals_path)):
            deals += 1
            if seen.classify(row.get(status_col)) != "won":
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

            add_firmographics(cuts, acct)
    heartbeat(2, 2)

    seen.refuse_if_nothing_matched(cols["opportunity_status"].column_name)
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
        **seen.as_dict(),
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


def win_loss_and_churn_patterns(conn, engagement_id: str, heartbeat):
    """Who actually wins, who actually churns, and whether those agree.

    The win rate is won over won plus lost. Open deals are excluded and
    counted, because an open deal is not a loss and treating it as one
    flatters nobody.

    The loss reason distribution carries the share held by its single most
    common value, and nothing more. A reason holding most of the records is
    usually a default nobody changed rather than a pattern, and deciding
    which is a judgement about how the client's team works — not something
    this can tell from the values.
    """
    cols = resolve_columns(conn, engagement_id, "win_loss_and_churn_patterns")
    needed = ["opportunity_id", "opportunity_status", "opportunity_account_id",
              "loss_reason", "account_id", "account_industry",
              "account_employee_count"]
    problem = missing_fields(conn, engagement_id, "win_loss_and_churn_patterns", needed, cols)
    if problem:
        raise ValueError(problem)

    seen = Outcomes()
    heartbeat(0, 2)
    accounts = load_accounts(conn, cols)
    heartbeat(1, 2)

    won_cuts, not_cut = firmographic_cuts(cols)
    lost_cuts, _ = firmographic_cuts(cols)
    loss_reasons, win_reasons, competitors = Cut("loss reason"), Cut("win reason"), Cut("competitor named")

    deals = won = lost = open_deals = no_account = unmatched = 0
    status_col = cols["opportunity_status"].column_name
    account_col = cols["opportunity_account_id"].column_name

    for row in rows_of(conn, cols["opportunity_status"]):
            deals += 1
            result = seen.classify(row.get(status_col))
            if result is None:
                open_deals += 1
                continue
            if result == "won":
                won += 1
                if "win_reason" in cols:
                    win_reasons.add(row.get(cols["win_reason"].column_name))
            else:
                lost += 1
                loss_reasons.add(row.get(cols["loss_reason"].column_name))
                if "competitor_named" in cols:
                    competitors.add(row.get(cols["competitor_named"].column_name))

            key = (row.get(account_col) or "").strip()
            if not key:
                no_account += 1
                continue
            acct = accounts.get(key)
            if acct is None:
                unmatched += 1
                continue
            add_firmographics(won_cuts if result == "won" else lost_cuts, acct)
    heartbeat(2, 2)

    seen.refuse_if_nothing_matched(cols["opportunity_status"].column_name)
    decided = won + lost
    overall = round(won / decided, 4) if decided >= MIN_FOR_A_RATE else None

    # Where the win rate differs most from the overall rate. Reported with
    # both counts, because a cut of twelve records swinging twenty points is
    # arithmetic, not a finding.
    variance = []
    for name, cut in won_cuts.items():
        for value, n_won in cut.counts.items():
            n_lost = lost_cuts[name].counts.get(value, 0)
            total = n_won + n_lost
            if total < MIN_FOR_A_RATE:
                continue
            rate = n_won / total
            variance.append({
                "dimension": name, "value": value,
                "won": n_won, "lost": n_lost, "of": total,
                **seen.as_dict(),
        "win_rate": round(rate, 4),
                "points_from_overall": (round((rate - overall) * 100, 1)
                                        if overall is not None else None),
            })
    variance.sort(key=lambda v: abs(v["points_from_overall"] or 0), reverse=True)

    reasons = loss_reasons.as_dict(lost)
    out = {
        "analysis": "win_loss_and_churn_patterns",
        "population": {
            "deals_read": deals, "won": won, "lost": lost,
            "decided": decided, "still_open": open_deals,
            "described": decided - no_account - unmatched,
        },
        "win_rate": overall,
        "win_rate_of": decided,
        "widest_variance": variance[:3],
        "loss_reasons": reasons,
        # The first number a consultant needs. A client whose loss reasons are
        # two thirds "Other" has no loss reason data, whatever the field says,
        # and no pattern read from the remaining third is safe to generalise.
        "losses_with_no_usable_reason": loss_reasons.not_answered,
        "losses_with_no_usable_reason_share": (
            round(loss_reasons.not_answered / lost, 4)
            if lost >= MIN_FOR_A_RATE else None),
        "won_by": [c.as_dict(won) for c in won_cuts.values()],
        "lost_by": [c.as_dict(lost) for c in lost_cuts.values()],
        "not_cut_because_the_field_is_not_mapped": not_cut,
        "gaps": [g.as_dict() for g in (
            [Gap("decided deals with no account on the record", no_account, decided)] if no_account else [])
            + ([Gap("decided deals whose account is not in the companies export",
                    unmatched, decided)] if unmatched else [])],
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "Values like Other and N/A are counted as no reason given, "
                "not as a reason. Where most losses carry one, the client "
                "does not record why deals are lost, and the pattern in the "
                "remainder describes only the minority who filled it in.",
    }
    if "win_reason" in cols:
        out["win_reasons"] = win_reasons.as_dict(won)
    if "competitor_named" in cols:
        out["competitors"] = competitors.as_dict(lost)
    return decided, out


def price_discount_dispersion(conn, engagement_id: str, heartbeat):
    """What the client actually charges, against what they say they charge.

    Discount is list minus realised, over list, per closed-won deal. Only
    where both are present and list is above zero: a deal with no list price
    has no discount, and calling that nought per cent would report
    discipline the client has not got.

    A realised price above list is kept as an uplift rather than clamped to
    nought. It is rare, it is real, and hiding it would make the spread look
    tidier than it is.
    """
    cols = resolve_columns(conn, engagement_id, "price_discount_dispersion")
    needed = ["opportunity_id", "opportunity_status", "opportunity_list_price",
              "opportunity_realised_price"]
    problem = missing_fields(conn, engagement_id, "price_discount_dispersion", needed, cols)
    if problem:
        raise ValueError(problem)

    accounts = load_accounts(conn, cols) if "account_id" in cols else {}
    discounts: list[float] = []
    same_as_list = 0
    by_segment: dict[str, list[float]] = defaultdict(list)
    by_owner: dict[str, list[float]] = defaultdict(list)
    won = no_list = no_realised = zero_list = 0

    status_col = cols["opportunity_status"].column_name
    list_col = cols["opportunity_list_price"].column_name
    real_col = cols["opportunity_realised_price"].column_name

    seen = Outcomes()
    for row in rows_of(conn, cols["opportunity_status"]):
            if seen.classify(row.get(status_col)) != "won":
                continue
            won += 1
            lst, real = money(row.get(list_col)), money(row.get(real_col))
            if lst is None:
                no_list += 1
                continue
            if real is None:
                no_realised += 1
                continue
            if lst <= 0:
                zero_list += 1
                continue
            if lst == real:
                same_as_list += 1
            d = (lst - real) / lst
            discounts.append(d)
            if "opportunity_owner" in cols:
                by_owner[(row.get(cols["opportunity_owner"].column_name)
                          or "not recorded").strip()].append(d)
            if accounts and "account_segment" in cols:
                acct = accounts.get((row.get(cols["opportunity_account_id"].column_name) or "").strip())
                if acct:
                    by_segment[(acct.get("segment") or "not recorded")].append(d)
    heartbeat(1, 1)

    seen.refuse_if_nothing_matched(cols["opportunity_status"].column_name)
    def spread(values: list[float]) -> dict | None:
        if len(values) < MIN_FOR_A_RATE:
            return {"count": len(values), "suppressed": True}
        v = sorted(values)
        def at(q): return round(v[min(int(q * len(v)), len(v) - 1)], 4)
        return {
            "count": len(v),
            "no_discount": round(sum(1 for x in v if x <= 0) / len(v), 4),
            "lowest_tenth": at(0.10), "lower_quarter": at(0.25),
            "middle": at(0.50), "upper_quarter": at(0.75),
            "highest_tenth": at(0.90),
            "suppressed": False,
        }

    return won, {
        "analysis": "price_discount_dispersion",
        "population": {
            "closed_won": won, "priced": len(discounts),
            "no_list_price": no_list, "no_realised_price": no_realised,
            "list_price_of_zero_or_less": zero_list,
            "realised_equals_list": same_as_list,
        },
        # A client who prices every deal on its merits has no list price to
        # discount from. Reporting that as nought per cent discount would
        # describe pricing discipline they have not got, and it is the
        # commonest shape in the mid-market: custom pricing per deal, no
        # packaging to hold the line.
        "no_list_price_structure": (
            len(discounts) >= MIN_FOR_A_RATE
            and (same_as_list / len(discounts)) >= 0.95),
        **seen.as_dict(),
        "discount": spread(discounts),
        "by_segment": ({k: spread(v) for k, v in sorted(by_segment.items())}
                       if by_segment else None),
        "by_owner": ({k: spread(v) for k, v in sorted(by_owner.items())}
                     if by_owner else None),
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "Discount is list minus realised over list, on closed-won "
                "deals where both are recorded. Deals with no list price are "
                "counted, not treated as full price. Where the realised price "
                "equals the list price on nearly every deal, the client does "
                "not operate a list price, and that is a finding about "
                "packaging rather than a record of perfect discipline.",
    }


def a_date(value):
    """A date, or None. Several shapes, because clients export all of them."""
    t = (str(value) if value is not None else "").strip()
    if not t:
        return None
    t = t.replace("T", " ").split(" ")[0].split("+")[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def a_month(value) -> str | None:
    d = a_date(value)
    if d:
        return f"{d.year:04d}-{d.month:02d}"
    t = (str(value) if value is not None else "").strip()
    # Spend files often carry the month alone.
    if len(t) == 7 and t[4] in "-/" and t[:4].isdigit():
        return f"{t[:4]}-{t[5:7]}"
    return None


def same_channel(name) -> str:
    """Channels are typed by different people in different systems.

    Marketing writes "Paid Search" in the spend sheet and the CRM says
    "paid_search". They are the same channel and must not be reported as two.
    """
    return " ".join(str(name or "").strip().lower()
                    .replace("_", " ").replace("-", " ").split())


def stage_conversion_and_duration(conn, engagement_id: str, heartbeat):
    """How far deals get, and how long they sit.

    No stage order is assumed. The client's stages are their own, and a
    sequence inferred from the data would be a guess presented as structure.
    What is measured instead needs no order: of the deals that ever entered a
    stage, how many went on to be won, and how long they stayed.

    Deals that skip stages are counted. A pipeline where half the deals
    appear first in the final stage is not a pipeline, it is a record kept
    after the fact, and no conversion rate read from it means anything.
    """
    cols = resolve_columns(conn, engagement_id, "stage_conversion_and_duration")

    # Two routes to the same rows.
    #
    # A stage history export arrives as one row per deal per stage, which
    # is what this needs. Most clients have no such export: HubSpot writes
    # a date property per stage on the deal, and a Salesforce report is
    # usually built the same way. detect_pipeline reads those columns and
    # pivots them into deal_stage_entry, which holds exactly the rows the
    # export would have held.
    #
    # So where the pivot has run, it is used, and the export stops being
    # something to chase. Where neither exists, the refusal names both.
    pivoted = _pivot_available(conn, engagement_id)
    needed = ["opportunity_id", "opportunity_status"]
    if not pivoted:
        needed += ["stage_history_opportunity_id", "stage_history_to",
                   "stage_entered_at"]
    problem = missing_fields(conn, engagement_id, "stage_conversion_and_duration",
                             needed, cols)
    if problem:
        if not pivoted:
            problem += (". The stages can also be read from the deals export "
                        "itself, where it carries a date column per stage. "
                        "Run the pipeline detection and this will not need "
                        "the stage history export at all.")
        raise ValueError(problem)

    # The outcome of each deal, from the deals export.
    heartbeat(0, 2)
    outcomes: dict[str, str | None] = {}
    closed_on: dict[str, object] = {}
    for row in rows_of(conn, cols["opportunity_id"]):
            key = (row.get(cols["opportunity_id"].column_name) or "").strip()
            if not key:
                continue
            outcomes[key] = outcome(row.get(cols["opportunity_status"].column_name))
            if "opportunity_close_date" in cols:
                when = a_date(row.get(cols["opportunity_close_date"].column_name))
                if when:
                    closed_on[key] = when
    heartbeat(1, 2)

    # Then the history: every stage each deal entered, and when.
    history: dict[str, list[tuple[str, object]]] = defaultdict(list)
    rows = undated = orphan = unchanged = 0
    read_from = "the stage history export"

    if pivoted:
        read_from = "the deals export, pivoted"
        for deal, stage, when, _segment in _stage_rows_from_pivot(
                conn, engagement_id):
            rows += 1
            if deal not in outcomes:
                orphan += 1
                continue
            history[deal].append((stage, when))
    else:
        # A history row is written for any change to the opportunity, not
        # only a stage change. Salesforce records "5 - Submit to 5 - Submit"
        # when somebody edits the amount or the close date, and on the
        # first real history export two thousand five hundred of six
        # thousand rows were that. Counted as stage movements they invent
        # entries and destroy every duration.
        #
        # Where the export says which stage a row came from, a row that
        # did not move is dropped. Where it does not, they cannot be told
        # apart and the count below is nought.
        from_col = (cols["stage_history_from"].column_name
                    if "stage_history_from" in cols else None)
        for row in rows_of(conn, cols["stage_history_to"]):
            rows += 1
            deal = (row.get(cols["stage_history_opportunity_id"].column_name) or "").strip()
            stage = (row.get(cols["stage_history_to"].column_name) or "").strip()
            when = a_date(row.get(cols["stage_entered_at"].column_name))
            if not deal or not stage:
                continue
            if from_col and (row.get(from_col) or "").strip() == stage:
                unchanged += 1
                continue
            if deal not in outcomes:
                orphan += 1
                continue
            if when is None:
                undated += 1
                continue
            history[deal].append((stage, when))
    heartbeat(2, 2)

    entered: Counter = Counter()
    won_after: Counter = Counter()
    dwell: dict[str, list[int]] = defaultdict(list)
    transitions: Counter = Counter()
    first_stage: Counter = Counter()

    for deal, events in history.items():
        events.sort(key=lambda e: e[1])
        result = outcomes.get(deal)

        # The last stage a deal reached has nothing after it, so without this
        # it has no dwell time at all — and time stuck in the final stage is
        # usually the number worth having. A closed deal's close date ends
        # its last stage. An open one genuinely has no end yet, and is left
        # out rather than measured to today, which would grow every time the
        # analysis ran.
        if result is not None and deal in closed_on:
            closing = closed_on[deal]
            if closing >= events[-1][1]:
                events.append(("__closed__", closing))
        seen = set()
        first_stage[events[0][0]] += 1
        for i, (stage, when) in enumerate(events):
            if stage == "__closed__":
                continue
            if stage not in seen:
                seen.add(stage)
                entered[stage] += 1
                if result == "won":
                    won_after[stage] += 1
            if i + 1 < len(events):
                nxt = events[i + 1]
                if nxt[0] != "__closed__":
                    transitions[(stage, nxt[0])] += 1
                dwell[stage].append((nxt[1] - when).days)

    def days(values: list[int]) -> dict:
        if len(values) < MIN_FOR_A_RATE:
            return {"count": len(values), "suppressed": True}
        v = sorted(values)
        def at(q): return v[min(int(q * len(v)), len(v) - 1)]
        return {"count": len(v), "quickest_tenth": at(0.10),
                "middle": at(0.50), "slowest_tenth": at(0.90),
                "suppressed": False}

    stages = []
    for stage, n in entered.most_common():
        row = {"stage": stage, "deals_that_entered": n,
               "went_on_to_be_won": won_after[stage],
               "days_in_the_stage": days(dwell.get(stage, []))}
        if n >= MIN_FOR_A_RATE:
            row["won_after_entering"] = round(won_after[stage] / n, 4)
        row["suppressed"] = n < MIN_FOR_A_RATE
        stages.append(row)

    tracked = len(history)
    return tracked, {
        "analysis": "stage_conversion_and_duration",
        "stage_rows_read_from": read_from,
        "rows_that_did_not_change_the_stage": unchanged,
        "population": {
            "deals_in_the_export": len(outcomes),
            "deals_with_a_stage_history": tracked,
            "history_rows_read": rows,
            "history_rows_with_no_date": undated,
            "history_rows_for_a_deal_not_in_the_export": orphan,
        },
        "stages": stages,
        "where_deals_first_appear": [
            {"stage": s, "count": n, "of": tracked,
             "share": round(n / tracked, 4) if tracked >= MIN_FOR_A_RATE else None}
            for s, n in first_stage.most_common()],
        "most_common_transitions": [
            {"from": a, "to": b, "count": n}
            for (a, b), n in transitions.most_common(12)],
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "No stage order is assumed. Of the deals that ever entered a "
                "stage, how many were won and how long they stayed. Where "
                "deals first appear matters: a pipeline whose deals appear "
                "first in a late stage is a record kept after the fact.",
    }


def cost_per_qualified_opportunity(conn, engagement_id: str, heartbeat):
    """What a qualified opportunity costs, by channel.

    Spend and opportunities come from different systems and different
    people, so the channel names rarely match exactly. They are compared
    loosely — case, spacing and underscores ignored — and every channel that
    appears on only one side is reported. A channel with spend and no
    opportunities is either waste or a naming mismatch, and the client can
    tell which far faster than we can.
    """
    cols = resolve_columns(conn, engagement_id, "cost_per_qualified_opportunity")
    needed = ["opportunity_id", "channel_source", "qualified_at",
              "spend_amount", "spend_channel", "spend_month"]
    problem = missing_fields(conn, engagement_id, "cost_per_qualified_opportunity", needed, cols)
    if problem:
        raise ValueError(problem)

    heartbeat(0, 2)
    spend: dict[tuple[str, str], float] = defaultdict(float)
    spend_rows = spend_undated = spend_unpriced = 0
    for row in rows_of(conn, cols["spend_amount"]):
            spend_rows += 1
            month = a_month(row.get(cols["spend_month"].column_name))
            amount = money(row.get(cols["spend_amount"].column_name))
            channel = same_channel(row.get(cols["spend_channel"].column_name))
            if month is None:
                spend_undated += 1
                continue
            if amount is None:
                spend_unpriced += 1
                continue
            spend[(channel, month)] += amount
    heartbeat(1, 2)

    qualified: Counter = Counter()
    deals = no_qualified_date = no_channel = 0
    for row in rows_of(conn, cols["opportunity_id"]):
            deals += 1
            month = a_month(row.get(cols["qualified_at"].column_name))
            if month is None:
                no_qualified_date += 1
                continue
            channel = same_channel(row.get(cols["channel_source"].column_name))
            if not channel:
                no_channel += 1
                continue
            qualified[(channel, month)] += 1
    heartbeat(2, 2)

    by_channel: dict[str, dict] = defaultdict(lambda: {"spend": 0.0, "qualified": 0})
    for (ch, _m), amount in spend.items():
        by_channel[ch]["spend"] += amount
    for (ch, _m), n in qualified.items():
        by_channel[ch]["qualified"] += n

    channels = []
    for ch, v in sorted(by_channel.items()):
        row = {"channel": ch or "not recorded",
               "spend": round(v["spend"], 2),
               "qualified_opportunities": v["qualified"]}
        if v["qualified"] >= MIN_FOR_A_RATE and v["spend"] > 0:
            row["cost_per_qualified_opportunity"] = round(v["spend"] / v["qualified"], 2)
        else:
            row["suppressed"] = True
        channels.append(row)

    spend_only = sorted({c for c, _ in spend} - {c for c, _ in qualified})
    opps_only = sorted({c for c, _ in qualified} - {c for c, _ in spend})

    total_spend = sum(v["spend"] for v in by_channel.values())
    total_qualified = sum(v["qualified"] for v in by_channel.values())
    return total_qualified, {
        "analysis": "cost_per_qualified_opportunity",
        "population": {
            "spend_rows_read": spend_rows,
            "spend_rows_with_no_month": spend_undated,
            "spend_rows_with_no_amount": spend_unpriced,
            "deals_read": deals,
            "deals_with_no_qualified_date": no_qualified_date,
            "deals_with_no_channel": no_channel,
            "qualified_opportunities": total_qualified,
            "total_spend": round(total_spend, 2),
        },
        "overall_cost_per_qualified_opportunity": (
            round(total_spend / total_qualified, 2)
            if total_qualified >= MIN_FOR_A_RATE and total_spend > 0 else None),
        "by_channel": channels,
        "channels_with_spend_and_no_opportunities": spend_only,
        "channels_with_opportunities_and_no_spend": opps_only,
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "Channel names are compared loosely across the two systems. "
                "A channel appearing on one side only is either waste or a "
                "naming mismatch, and the client will know which.",
    }


def forecast_accuracy(conn, engagement_id: str, heartbeat):
    """Whether the forecast is worth anything.

    Reported signed, not as an absolute error. A team that misses by twenty
    per cent every quarter in the same direction has a correctable bias; one
    that misses by twenty either way has no forecast at all, and the two
    need different conversations.
    """
    cols = resolve_columns(conn, engagement_id, "forecast_accuracy")
    needed = ["forecast_period", "forecast_amount", "forecast_actual_amount"]
    problem = missing_fields(conn, engagement_id, "forecast_accuracy", needed, cols)
    if problem:
        raise ValueError(problem)

    periods: dict[str, dict] = {}
    rows = unusable = 0
    for row in rows_of(conn, cols["forecast_amount"]):
            rows += 1
            period = (row.get(cols["forecast_period"].column_name) or "").strip()
            forecast = money(row.get(cols["forecast_amount"].column_name))
            actual = money(row.get(cols["forecast_actual_amount"].column_name))
            if not period or forecast is None or actual is None or forecast <= 0:
                unusable += 1
                continue
            p = periods.setdefault(period, {"forecast": 0.0, "actual": 0.0,
                                            "submissions": 0})
            p["forecast"] += forecast
            p["actual"] += actual
            p["submissions"] += 1
    heartbeat(1, 1)

    out_periods, errors = [], []
    for period, p in sorted(periods.items()):
        err = (p["actual"] - p["forecast"]) / p["forecast"]
        errors.append(err)
        out_periods.append({
            "period": period, "forecast": round(p["forecast"], 2),
            "actual": round(p["actual"], 2),
            "submissions": p["submissions"],
            "error": round(err, 4),
            "direction": "over-forecast" if err < 0 else "under-forecast",
        })

    n = len(errors)
    bias = round(sum(errors) / n, 4) if n else None
    spread = round(sum(abs(e) for e in errors) / n, 4) if n else None
    return n, {
        "analysis": "forecast_accuracy",
        "population": {
            "rows_read": rows,
            "rows_with_no_period_or_no_figures": unusable,
            "periods": n,
        },
        "periods": out_periods,
        "average_error": bias,
        "average_miss_whichever_way": spread,
        "always_the_same_way": (n >= 3 and all(e < 0 for e in errors))
                               or (n >= 3 and all(e > 0 for e in errors)),
        "periods_too_few_to_judge": n < 3,
        "note": "Error is actual minus forecast over forecast, per period. "
                "A consistent direction is a correctable bias. A miss that "
                "goes either way is not a forecast.",
    }


def buying_group_composition(conn, engagement_id: str, heartbeat):
    """How many people are involved in a deal, and who.

    The number that matters is how many deals have exactly one named
    contact. A single-threaded deal is one resignation away from starting
    again, and a client whose deals are mostly single-threaded has a risk
    they can see in a sentence.
    """
    cols = resolve_columns(conn, engagement_id, "buying_group_composition")
    needed = ["opportunity_id", "opportunity_status", "contact_role",
              "contact_role_opportunity_id", "contact_role_contact_id"]
    problem = missing_fields(conn, engagement_id, "buying_group_composition", needed, cols)
    if problem:
        raise ValueError(problem)

    heartbeat(0, 3)
    outcomes = {}
    for row in rows_of(conn, cols["opportunity_id"]):
            key = (row.get(cols["opportunity_id"].column_name) or "").strip()
            if key:
                outcomes[key] = outcome(row.get(cols["opportunity_status"].column_name))
    heartbeat(1, 3)

    # Contacts, where that export is here, so roles can carry seniority.
    people = {}
    if "contact_id" in cols:
        for row in rows_of(conn, cols["contact_id"]):
                key = (row.get(cols["contact_id"].column_name) or "").strip()
                if key:
                    people[key] = {
                        n: row.get(cols[f].column_name)
                        for n, f in (("seniority", "contact_seniority"),
                                     ("function", "contact_function"),
                                     ("title", "contact_job_title"))
                        if f in cols}
    heartbeat(2, 3)

    per_deal: dict[str, set] = defaultdict(set)
    roles, seniorities, functions = Cut("role"), Cut("seniority"), Cut("function")
    role_rows = orphan = 0
    for row in rows_of(conn, cols["contact_role"]):
            role_rows += 1
            deal = (row.get(cols["contact_role_opportunity_id"].column_name) or "").strip()
            person = (row.get(cols["contact_role_contact_id"].column_name) or "").strip()
            if not deal or deal not in outcomes:
                orphan += 1
                continue
            per_deal[deal].add(person)
            roles.add(row.get(cols["contact_role"].column_name))
            who = people.get(person)
            if who:
                seniorities.add(who.get("seniority"))
                functions.add(who.get("function"))
    heartbeat(3, 3)

    sizes = Counter(len(v) for v in per_deal.values())
    with_roles = len(per_deal)
    decided = sum(1 for o in outcomes.values() if o is not None)
    single = sizes.get(1, 0)

    # Won deals against lost, by how many people were involved.
    by_size: dict[int, dict] = defaultdict(lambda: {"won": 0, "lost": 0})
    for deal, contacts in per_deal.items():
        r = outcomes.get(deal)
        if r:
            by_size[len(contacts)][r] += 1

    out = {
        "analysis": "buying_group_composition",
        "population": {
            "deals_in_the_export": len(outcomes),
            "deals_with_a_named_contact": with_roles,
            "deals_with_nobody_named": len(outcomes) - with_roles,
            "contact_role_rows_read": role_rows,
            "contact_role_rows_for_a_deal_not_in_the_export": orphan,
        },
        "single_threaded": single,
        "single_threaded_share": (round(single / with_roles, 4)
                                  if with_roles >= MIN_FOR_A_RATE else None),
        "people_per_deal": [
            {"people": k, "deals": v, "of": with_roles,
             "share": round(v / with_roles, 4) if with_roles >= MIN_FOR_A_RATE else None}
            for k, v in sorted(sizes.items())],
        "outcome_by_group_size": [
            {"people": k, "won": v["won"], "lost": v["lost"],
             "of": v["won"] + v["lost"],
             "win_rate": (round(v["won"] / (v["won"] + v["lost"]), 4)
                          if v["won"] + v["lost"] >= MIN_FOR_A_RATE else None)}
            for k, v in sorted(by_size.items())],
        "roles": roles.as_dict(role_rows),
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "A deal with one named contact is single-threaded. Deals with "
                "nobody named are counted separately: that is a recording "
                "failure, not a buying group of nought.",
    }
    if people:
        out["seniority"] = seniorities.as_dict(role_rows)
        out["function"] = functions.as_dict(role_rows)
    return with_roles, out


def handover_completeness_census(conn, engagement_id: str, heartbeat):
    """What actually crosses from sales to delivery, on every won deal.

    A census, not a sample. For each closed-won deal, which of the things a
    handover needs are on the record: who bought, who owns the account, what
    was discussed, and whether anything happened after the sale.

    Each is counted on its own. A deal with three of four is not
    three-quarters handed over; it is missing the fourth, and which one is
    missing is the finding.
    """
    cols = resolve_columns(conn, engagement_id, "handover_completeness_census")
    needed = ["opportunity_id", "opportunity_status", "opportunity_account_id",
              "contact_role", "contact_role_opportunity_id",
              "activity_id", "activity_opportunity_id", "activity_type"]
    problem = missing_fields(conn, engagement_id, "handover_completeness_census", needed, cols)
    if problem:
        raise ValueError(problem)

    seen = Outcomes()
    heartbeat(0, 4)
    won_deals: dict[str, str] = {}
    for row in rows_of(conn, cols["opportunity_id"]):
            if seen.classify(row.get(cols["opportunity_status"].column_name)) != "won":
                continue
            key = (row.get(cols["opportunity_id"].column_name) or "").strip()
            if key:
                won_deals[key] = (row.get(cols["opportunity_account_id"].column_name) or "").strip()
    heartbeat(1, 4)

    seen.refuse_if_nothing_matched(cols["opportunity_status"].column_name)
    has_role = set()
    for row in rows_of(conn, cols["contact_role"]):
            deal = (row.get(cols["contact_role_opportunity_id"].column_name) or "").strip()
            if deal in won_deals and (row.get(cols["contact_role"].column_name) or "").strip():
                has_role.add(deal)
    heartbeat(2, 4)

    has_activity = set()
    activity_types = Cut("activity type")
    for row in rows_of(conn, cols["activity_id"]):
            deal = (row.get(cols["activity_opportunity_id"].column_name) or "").strip()
            if deal in won_deals:
                has_activity.add(deal)
                activity_types.add(row.get(cols["activity_type"].column_name))
    heartbeat(3, 4)

    has_note = set()
    if "note_opportunity_id" in cols:
        for row in rows_of(conn, cols["note_opportunity_id"]):
                deal = (row.get(cols["note_opportunity_id"].column_name) or "").strip()
                if deal in won_deals:
                    has_note.add(deal)

    has_owner = set()
    if "account_owner" in cols and "account_id" in cols:
        owners = {}
        for row in rows_of(conn, cols["account_id"]):
                key = (row.get(cols["account_id"].column_name) or "").strip()
                val = (row.get(cols["account_owner"].column_name) or "").strip()
                if key and val and not is_a_non_answer(val):
                    owners[key] = val
        has_owner = {d for d, acct in won_deals.items() if acct in owners}
    heartbeat(4, 4)

    n = len(won_deals)
    checks = [("somebody named on the deal", has_role),
              ("something recorded against it", has_activity)]
    if "note_opportunity_id" in cols:
        checks.append(("a written record of the conversation", has_note))
    if has_owner or ("account_owner" in cols and "account_id" in cols):
        checks.append(("an owner on the account", has_owner))

    present = []
    for label, found in checks:
        present.append({
            "what": label, "deals_with_it": len(found), "of": n,
            "share": round(len(found) / n, 4) if n >= MIN_FOR_A_RATE else None,
        })
    complete = set(won_deals) 
    for _label, found in checks:
        complete &= found
    nothing = set(won_deals)
    for _label, found in checks:
        nothing -= found

    return n, {
        "analysis": "handover_completeness_census",
        "population": {"closed_won": n, "checks_made": len(checks)},
        **seen.as_dict(),
        "each_part": present,
        "deals_with_every_part": len(complete),
        "deals_with_every_part_share": (round(len(complete) / n, 4)
                                        if n >= MIN_FOR_A_RATE else None),
        "deals_with_no_part_at_all": len(nothing),
        "activity_types": activity_types.as_dict(len(has_activity)),
        "parts_not_checked_because_the_field_is_not_mapped": [
            label for label, field in
            (("a written record of the conversation", "note_opportunity_id"),
             ("an owner on the account", "account_owner"))
            if field not in cols],
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "A census of every closed-won deal. Each part is counted on "
                "its own: a deal with three of four is missing the fourth, "
                "and which one is missing is the finding.",
    }


def expansion_vs_new_business(conn, engagement_id: str, heartbeat):
    """How much of the growth comes from customers they already had.

    Deals are classified by the type the client records, not inferred. Where
    the type is missing or is a non-answer the deal is counted as
    unclassified, not assumed to be new business — which is the assumption
    that flatters a land-and-expand story.

    Value comes from the subscriptions, where the client records what a
    customer is actually worth and what was added. A deal amount is what was
    signed; annual recurring revenue is what was kept.
    """
    cols = resolve_columns(conn, engagement_id, "expansion_vs_new_business")
    needed = ["opportunity_id", "opportunity_status", "opportunity_type",
              "opportunity_account_id"]
    problem = missing_fields(conn, engagement_id, "expansion_vs_new_business", needed, cols)
    if problem:
        raise ValueError(problem)

    types = Cut("opportunity type")
    won = unclassified = read = 0
    by_type: Counter = Counter()
    accounts_won: dict[str, set] = defaultdict(set)

    seen = Outcomes()
    for row in rows_of(conn, cols["opportunity_id"]):
            read += 1
            if seen.classify(row.get(cols["opportunity_status"].column_name)) != "won":
                continue
            won += 1
            raw = row.get(cols["opportunity_type"].column_name)
            types.add(raw)
            if raw is None or str(raw).strip() == "" or is_a_non_answer(raw):
                unclassified += 1
                label = "not classified"
            else:
                label = str(raw).strip()
            by_type[label] += 1
            acct = (row.get(cols["opportunity_account_id"].column_name) or "").strip()
            if acct:
                accounts_won[label].add(acct)
    heartbeat(1, 2)

    seen.refuse_if_nothing_matched(cols["opportunity_status"].column_name)
    # What those customers are worth, from the subscriptions.
    recurring = expansion = 0.0
    subs = subs_unpriced = 0
    if "subscription_id" in cols and "arr_amount" in cols:
        for row in rows_of(conn, cols["subscription_id"]):
                subs += 1
                arr = money(row.get(cols["arr_amount"].column_name))
                if arr is None:
                    subs_unpriced += 1
                else:
                    recurring += arr
                if "expansion_amount" in cols:
                    add = money(row.get(cols["expansion_amount"].column_name))
                    if add is not None:
                        expansion += add
    heartbeat(2, 2)

    return won, {
        "analysis": "expansion_vs_new_business",
        "population": {
            "deals_read": read, "closed_won": won,
            "not_classified": unclassified,
            "subscriptions_read": subs,
            "subscriptions_with_no_recurring_revenue": subs_unpriced,
        },
        **seen.as_dict(),
        "won_by_type": types.as_dict(won),
        "accounts_by_type": [{"type": k, "accounts": len(v)}
                             for k, v in sorted(accounts_won.items())],
        "recurring_revenue": round(recurring, 2) if subs else None,
        "from_expansion": round(expansion, 2) if subs else None,
        "expansion_share_of_recurring_revenue": (
            round(expansion / recurring, 4)
            if subs and recurring > 0 else None),
        "value_not_measured_because_the_field_is_not_mapped":
            "arr_amount" not in cols,
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "Deals are classified by the type the client records. A deal "
                "with no type is counted as unclassified, not as new "
                "business. Value comes from recurring revenue, not deal "
                "amounts.",
    }


def renewal_timing_and_reason(conn, engagement_id: str, heartbeat):
    """Whether renewals are run, or just happen.

    How far ahead of the contract end a renewal was settled is the number
    that separates a renewal process from a renewal date. A renewal recorded
    on or after the end date was not managed, whatever its outcome.
    """
    cols = resolve_columns(conn, engagement_id, "renewal_timing_and_reason")
    needed = ["subscription_id", "renewal_date", "renewal_outcome"]
    problem = missing_fields(conn, engagement_id, "renewal_timing_and_reason", needed, cols)
    if problem:
        raise ValueError(problem)

    outcomes, reasons = Cut("renewal outcome"), Cut("reason for not renewing")
    lead_days: list[int] = []
    rows = no_dates = not_renewed = 0
    end_col = (cols["contract_end_date"].column_name
               if "contract_end_date" in cols else None)

    for row in rows_of(conn, cols["subscription_id"]):
            rows += 1
            result = row.get(cols["renewal_outcome"].column_name)
            outcomes.add(result)
            # A reason for not renewing only applies where it did not renew.
            # Counting a blank on a renewed subscription as a missing reason
            # made nine in ten renewals look like a recording failure.
            renewed = "renew" in str(result or "").lower() \
                and "not" not in str(result or "").lower() \
                and "non" not in str(result or "").lower()
            if "non_renewal_reason" in cols and not renewed:
                not_renewed += 1
                reasons.add(row.get(cols["non_renewal_reason"].column_name))
            if end_col:
                settled = a_date(row.get(cols["renewal_date"].column_name))
                ends = a_date(row.get(end_col))
                if settled and ends:
                    lead_days.append((ends - settled).days)
                else:
                    no_dates += 1
    heartbeat(1, 1)

    timing = None
    if len(lead_days) >= MIN_FOR_A_RATE:
        v = sorted(lead_days)
        def at(q): return v[min(int(q * len(v)), len(v) - 1)]
        timing = {
            "count": len(v),
            "days_ahead_middle": at(0.50),
            "days_ahead_earliest_tenth": at(0.90),
            "settled_on_or_after_the_end_date": sum(1 for x in v if x <= 0),
            "settled_on_or_after_the_end_date_share":
                round(sum(1 for x in v if x <= 0) / len(v), 4),
        }

    out = {
        "analysis": "renewal_timing_and_reason",
        "population": {"subscriptions_read": rows,
                       "with_no_usable_dates": no_dates},
        "outcomes": outcomes.as_dict(rows),
        "timing": timing,
        "timing_not_measured_because_the_field_is_not_mapped":
            end_col is None,
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "How far ahead of the contract end each renewal was settled. "
                "A renewal settled on or after the end date was not managed, "
                "whatever its outcome.",
    }
    if "non_renewal_reason" in cols:
        out["reasons_for_not_renewing"] = reasons.as_dict(not_renewed)
        out["did_not_renew"] = not_renewed
        out["non_renewals_with_no_usable_reason"] = reasons.not_answered
        out["non_renewals_with_no_usable_reason_share"] = (
            round(reasons.not_answered / not_renewed, 4)
            if not_renewed >= MIN_FOR_A_RATE else None)
    return rows, out


def a_moment(value):
    """A date and time, or None. Response time needs the hours."""
    t = (str(value) if value is not None else "").strip()
    if not t:
        return None
    t = t.replace("T", " ").split("+")[0].split(".")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
                "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def signal_to_first_contact(conn, engagement_id: str, heartbeat):
    """When somebody raised a hand, how long before a person answered.

    The signal is a form submission. Not a record being created: a bought
    list, a ZoomInfo import and a scraped contact all create records, and
    none of them is a buyer raising a hand. Measuring from record creation
    measures the import.

    Meetings in the activity log are counted but not treated as signals.
    A meeting the prospect booked through a scheduling link is as inbound
    as a form; one a rep booked is not, and nothing in an activity export
    distinguishes them.

    The response is the first activity of any kind against the same person
    after the signal. Direction is not needed and mostly is not there:
    both CRMs carry a direction field, both populate it only when the
    activity happened inside the tool, and reps logging by hand are offered
    the choice rather than made to take it.
    """
    cols = resolve_columns(conn, engagement_id, "signal_to_first_contact")
    needed = ["contact_id", "activity_id", "activity_contact_id", "activity_at"]
    problem = missing_fields(conn, engagement_id, "signal_to_first_contact", needed, cols)
    if problem:
        raise ValueError(problem)
    if "form_submission_at" not in cols or "form_submission_contact_id" not in cols:
        raise ValueError(
            "there is no record of anyone raising a hand: no form submissions "
            "are mapped. Response time cannot be measured without a signal, "
            "and that is a finding rather than a gap")

    # Everyone in the CRM, so the ones who never signalled can be counted.
    heartbeat(0, 3)
    people: set[str] = set()
    for row in rows_of(conn, cols["contact_id"]):
            key = (row.get(cols["contact_id"].column_name) or "").strip()
            if key:
                people.add(key)
    leads = 0
    if "lead_id" in cols:
        for row in rows_of(conn, cols["lead_id"]):
                key = (row.get(cols["lead_id"].column_name) or "").strip()
                if key:
                    people.add(key)
                    leads += 1
    heartbeat(1, 3)

    # The signals.
    first_signal: dict[str, object] = {}
    submissions = undated_signals = 0
    for row in rows_of(conn, cols["form_submission_at"]):
            submissions += 1
            who = (row.get(cols["form_submission_contact_id"].column_name) or "").strip()
            when = a_moment(row.get(cols["form_submission_at"].column_name))
            if not who:
                continue
            if when is None:
                undated_signals += 1
                continue
            if who not in first_signal or when < first_signal[who]:
                first_signal[who] = when
    heartbeat(2, 3)

    # The responses: any activity against that person, after the signal.
    first_reply: dict[str, object] = {}
    activities = meetings = 0
    meeting_col = cols["meeting_at"].column_name if "meeting_at" in cols else None
    for row in rows_of(conn, cols["activity_at"]):
            activities += 1
            if meeting_col and (row.get(meeting_col) or "").strip():
                meetings += 1
            who = (row.get(cols["activity_contact_id"].column_name) or "").strip()
            if who not in first_signal:
                continue
            when = a_moment(row.get(cols["activity_at"].column_name))
            if when is None or when < first_signal[who]:
                continue
            if who not in first_reply or when < first_reply[who]:
                first_reply[who] = when
    heartbeat(3, 3)

    hours = sorted((first_reply[w] - first_signal[w]).total_seconds() / 3600
                   for w in first_reply)
    signalled = len(first_signal)
    never_answered = signalled - len(hours)

    def within(h):
        return sum(1 for x in hours if x <= h)

    bands = None
    if signalled >= MIN_FOR_A_RATE:
        bands = [{"answered": "within " + label, "people": within(h),
                  "of": signalled, "share": round(within(h) / signalled, 4)}
                 for label, h in (("an hour", 1), ("a working day", 8),
                                  ("a day", 24), ("three days", 72),
                                  ("a week", 168))]
        bands.append({"answered": "not at all", "people": never_answered,
                      "of": signalled,
                      "share": round(never_answered / signalled, 4)})

    speed = None
    if len(hours) >= MIN_FOR_A_RATE:
        def at(q): return round(hours[min(int(q * len(hours)), len(hours) - 1)], 2)
        speed = {"answered": len(hours), "quickest_tenth_hours": at(0.10),
                 "middle_hours": at(0.50), "slowest_tenth_hours": at(0.90)}

    # Only people we can actually see in the CRM. A form submission from
    # somebody who is not in the contact or lead export is its own small
    # finding, counted below rather than swelling this one.
    signalled_and_known = {w for w in first_signal if w in people}
    never_signalled = len(people) - len(signalled_and_known)
    signals_from_nobody_on_file = signalled - len(signalled_and_known)
    return signalled, {
        "analysis": "signal_to_first_contact",
        "population": {
            "people_in_the_crm": len(people),
            "of_which_unconverted_leads": leads if "lead_id" in cols else None,
            "form_submissions_read": submissions,
            "form_submissions_with_no_date": undated_signals,
            "people_who_signalled": signalled,
            "activities_read": activities,
        },
        "answered_within": bands,
        "speed": speed,
        "never_answered": never_answered,
        # The other finding, and on most CRMs the louder one.
        "people_who_never_signalled": never_signalled,
        "people_who_never_signalled_share": (
            round(never_signalled / len(people), 4)
            if len(people) >= MIN_FOR_A_RATE else None),
        "signals_from_somebody_not_in_the_crm": signals_from_nobody_on_file,
        "meetings_recorded": meetings if meeting_col else None,
        "leads_not_counted_because_the_export_is_not_mapped":
            "lead_id" not in cols,
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "The signal is a form submission. A record being created is "
                "not a signal: imported lists create records and raise no "
                "hands. People in the CRM who never submitted a form are "
                "counted separately, and on most databases they are most of "
                "it — which is what every per-contact rate the client quotes "
                "is computed against.",
    }


def working_days_between(a, b) -> int:
    """Working days, because two days over a weekend is not two days late."""
    if a is None or b is None:
        return 0
    step = 1 if b >= a else -1
    days, cur = 0, a
    while cur != b:
        cur += timedelta(days=step)
        if cur.weekday() < 5:
            days += step
    return days


# A note of three words is a note in the export and nothing in practice.
# "Called." and "n/a" are the commonest, and counting them as coverage is
# how a client comes to believe their notes are good.
SUBSTANTIVE_WORDS = 10


def note_sufficiency(conn, engagement_id: str, heartbeat):
    """Whether the client's notes can carry an assessment at all.

    Four conditions, and the verdict is computed from all four. Three of
    them are counted: how many closed opportunities carry a substantive
    note, separately for won and lost because notes cluster on deals that
    went well, and how many notes were written within two working days of
    the meeting they describe.

    The fourth is read. A sample of thirty notes across at least three
    authors is labelled for what the buyer actually said — decision
    process, timeline, success criteria, agreed next step — and for whether
    the note follows any recognisable structure.

    The model never returns the verdict or the rates. It labels, and the
    arithmetic happens here where it can be checked. A near miss is a fail.
    """
    cols = resolve_columns(conn, engagement_id, "note_sufficiency")
    needed = ["note_id", "note_body", "note_author", "note_created_at",
              "note_opportunity_id", "meeting_at"]
    problem = missing_fields(conn, engagement_id, "note_sufficiency", needed, cols)
    if problem:
        raise ValueError(problem)

    model.check_configured()

    # Which opportunities closed, and which way.
    seen = Outcomes()
    heartbeat(0, 3)
    closed: dict[str, str] = {}
    if "opportunity_id" in cols and "opportunity_status" in cols:
        for row in rows_of(conn, cols["opportunity_id"]):
                result = seen.classify(row.get(cols["opportunity_status"].column_name))
                key = (row.get(cols["opportunity_id"].column_name) or "").strip()
                if key and result:
                    closed[key] = result
    seen.refuse_if_nothing_matched(cols["opportunity_status"].column_name)
    heartbeat(1, 3)

    notes: list[dict] = []
    with_note: dict[str, set] = {"won": set(), "lost": set()}
    by_author: dict[str, list[dict]] = defaultdict(list)
    read = thin = 0
    on_time = datable = 0

    for row in rows_of(conn, cols["note_body"]):
            read += 1
            body = (row.get(cols["note_body"].column_name) or "").strip()
            if len(body.split()) < SUBSTANTIVE_WORDS:
                thin += 1
                continue
            nid = (row.get(cols["note_id"].column_name) or "").strip()
            author = (row.get(cols["note_author"].column_name) or "").strip()
            deal = (row.get(cols["note_opportunity_id"].column_name) or "").strip()
            written = a_date(row.get(cols["note_created_at"].column_name))
            met = a_date(row.get(cols["meeting_at"].column_name))

            if deal in closed:
                with_note[closed[deal]].add(deal)
            if written and met:
                datable += 1
                if 0 <= working_days_between(met, written) <= 2:
                    on_time += 1
            if nid and body:
                note = {"id": nid, "text": body, "author": author or "unnamed"}
                notes.append(note)
                by_author[note["author"]].append(note)
    heartbeat(2, 3)

    won_total = sum(1 for v in closed.values() if v == "won")
    lost_total = sum(1 for v in closed.values() if v == "lost")

    def rate(part, whole):
        return round(part / whole, 4) if whole >= MIN_FOR_A_RATE else None

    coverage_won = rate(len(with_note["won"]), won_total)
    coverage_lost = rate(len(with_note["lost"]), lost_total)
    contemporaneity = rate(on_time, datable)

    # Thirty notes across at least three authors. Spread across authors
    # deliberately: thirty notes from the one person who writes properly
    # would describe that person, not the client.
    sample: list[dict] = []
    authors = sorted(by_author, key=lambda a: -len(by_author[a]))
    per_author = max(1, 30 // max(len(authors), 1))
    for a in authors:
        sample.extend(by_author[a][:per_author])
    # Top up from the authors who write most, so a client with four authors
    # gets thirty notes rather than twenty-eight.
    for a in authors:
        if len(sample) >= 30:
            break
        sample.extend(by_author[a][per_author:per_author + (30 - len(sample))])
    sample = sample[:30]

    LABELS = ["decision_process", "timeline", "success_criteria",
              "next_step", "follows_a_structure"]
    labelled, unread = ({}, [])
    if sample:
        labelled, unread = model.classify(
            sample,
            "These are notes written by sales people after customer "
            "conversations. For each, answer whether the note records the "
            "buyer's own stated position on their decision process, their "
            "timeline, their success criteria, and an agreed next step; and "
            "whether the note follows a recognisable structure such as "
            "headings or consistent sections rather than free prose.",
            LABELS, heartbeat=None)
    heartbeat(3, 3)

    n = len(labelled)
    content = {l: (round(sum(1 for v in labelled.values()
                            if v["labels"][l] == "yes") / n, 4)
                   if n >= MIN_FOR_A_RATE else None)
               for l in LABELS[:4]}

    # Structure is consistent when most notes have one and the authors agree.
    # Computed, not asked for.
    author_of = {x["id"]: x["author"] for x in sample}
    structured: dict[str, list] = defaultdict(list)
    for rid, v in labelled.items():
        structured[author_of[rid]].append(v)
    by_author_rate = {
        a: sum(1 for v in vs if v["labels"]["follows_a_structure"] == "yes") / len(vs)
        for a, vs in structured.items() if len(vs) >= 3}
    overall_structure = (round(sum(1 for v in labelled.values()
                                   if v["labels"]["follows_a_structure"] == "yes") / n, 4)
                         if n >= MIN_FOR_A_RATE else None)
    structure_consistent = (
        overall_structure is not None and overall_structure >= 0.70
        and len(by_author_rate) >= 2
        and (max(by_author_rate.values()) - min(by_author_rate.values())) <= 0.30)

    failed = []
    if coverage_won is None or coverage_won < 0.90:
        failed.append("coverage of closed-won opportunities")
    if not structure_consistent:
        failed.append("consistent structure across authors")
    for l, v in content.items():
        if v is None or v < 0.70:
            failed.append(l.replace("_", " "))
    if contemporaneity is None or contemporaneity < 0.80:
        failed.append("notes written within two working days")

    return read, {
        "analysis": "note_sufficiency",
        "population": {
            "notes_read": read,
            "notes_too_thin_to_count": thin,
            "closed_won": won_total, "closed_lost": lost_total,
            "notes_datable_against_a_meeting": datable,
        },
        "coverage_won": coverage_won,
        "coverage_won_of": won_total,
        "coverage_lost": coverage_lost,
        "coverage_lost_of": lost_total,
        "contemporaneity": contemporaneity,
        "contemporaneity_of": datable,
        "content_rates": content,
        "structure_rate": overall_structure,
        "structure_by_author": {f"Author {i}": round(v, 4)
                                for i, v in enumerate(by_author_rate.values(), 1)},
        "structure_consistent": structure_consistent,
        "sample": {"size": len(sample), "authors": len(by_author_rate),
                   "labelled": n, "unread": len(unread)},
        "verdict": "PASS" if not failed else "FAIL",
        "failed_on": failed,
        "model": model.MODEL,
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "The verdict is computed from all four conditions. A near "
                "miss is a fail. Notes under ten words are counted as too "
                "thin rather than as coverage: a note saying Called. is a "
                "note in the export and nothing in practice.",
    }


def discovery_consistency_classification(conn, engagement_id: str, heartbeat):
    """What the discovery conversations actually recorded, on every deal.

    A census rather than a sample, because the finding is a distribution
    and a sample of thirty cannot be cut by rep. Every record is read, and
    whether every record was read is reported: an analysis that quietly
    skipped a fifth of the notes would describe four fifths of the client.

    Five questions per record, each answered only where the record states
    it. A rep's assumption, an implication, or the model's own inference is
    a no, and the span of text behind each answer comes back with it so a
    person can check ten of them at step 2.4.

    Reps are named Rep 1, Rep 2 and so on. The finding is about the
    practice, not about who is worst at it, and a report that names people
    stops being read as an assessment.
    """
    cols = resolve_columns(conn, engagement_id,
                           "discovery_consistency_classification")
    needed = ["note_id", "note_body", "note_author", "note_created_at",
              "note_opportunity_id", "activity_owner"]
    problem = missing_fields(conn, engagement_id, "discovery_consistency_classification", needed, cols)
    if problem:
        raise ValueError(problem)

    model.check_configured()

    records: list[dict] = []
    owner_of: dict[str, str] = {}
    read = thin = 0
    for row in rows_of(conn, cols["note_body"]):
            read += 1
            body = (row.get(cols["note_body"].column_name) or "").strip()
            nid = (row.get(cols["note_id"].column_name) or "").strip()
            if not nid or len(body.split()) < SUBSTANTIVE_WORDS:
                thin += 1
                continue
            records.append({"id": nid, "text": body})
            owner_of[nid] = (row.get(cols["note_author"].column_name) or "").strip() \
                or "unnamed"

    # The questions as GEA-P13 asks them. A screen deriving "Does this
    # record show decision process?" from a key is asking a different and
    # vaguer question than the model was asked, and the person answering it
    # is not checking the same thing.
    QUESTIONS = {
        "decision_process": "Does the record state the buyer's own decision "
                            "process?",
        "timeline": "Does the record state the buyer's timeline?",
        "success_criteria": "Does the record state the buyer's own success "
                            "criteria?",
        "next_step": "Does the record state an agreed next step?",
        "others_involved": "Does the record state who else is involved in "
                           "the decision?",
    }
    LABELS = list(QUESTIONS)
    labelled, unread = model.classify(
        records,
        "These are records of discovery conversations written by sales "
        "people. For each, answer whether the record states the buyer's own "
        "decision process, the buyer's timeline, the buyer's success "
        "criteria, an agreed next step, and who else is involved in the "
        "decision. Answer yes only where the record says it. An assumption "
        "by the writer, an implication, or your own inference is a no.",
        LABELS, heartbeat=heartbeat)

    n = len(labelled)
    rates = {l: (round(sum(1 for v in labelled.values()
                           if v["labels"][l] == "yes") / n, 4)
                 if n >= MIN_FOR_A_RATE else None) for l in LABELS}

    all_five = sum(1 for v in labelled.values()
                   if all(v["labels"][l] == "yes" for l in LABELS))
    none_of_five = sum(1 for v in labelled.values()
                       if all(v["labels"][l] == "no" for l in LABELS))

    # By rep, anonymised, and only where there are enough records to say
    # anything. A rep with four notes is not a pattern.
    per_rep: dict[str, list] = defaultdict(list)
    for rid, v in labelled.items():
        per_rep[owner_of[rid]].append(v)
    reps = []
    for i, (_name, vs) in enumerate(
            sorted(per_rep.items(), key=lambda x: -len(x[1])), 1):
        if len(vs) < MIN_FOR_A_RATE:
            continue
        reps.append({"rep": f"Rep {i}", "records": len(vs),
                     **{l: round(sum(1 for v in vs if v["labels"][l] == "yes")
                                 / len(vs), 4) for l in LABELS}})

    # The ten a person reads at step 2.4. Spread across the distribution
    # rather than taken off the top, so the check covers the easy records
    # and the hard ones.
    by_score = sorted(labelled.items(),
                      key=lambda kv: sum(1 for l in LABELS
                                         if kv[1]["labels"][l] == "yes"))
    step = max(1, len(by_score) // 10)
    # The record's own text travels with it. Without it a person checking
    # the model can only read the span the model chose to show them, which
    # is not a check at all: they would be agreeing that the model's own
    # evidence supports the model's own answer. Ten records of client text,
    # held under the same rule as everything else raw.
    text_of = {r["id"]: r["text"] for r in records}
    to_check = [{"record_id": rid,
                 "record": text_of.get(rid, "")[:4000],
                 "labels": v["labels"], "evidence": v["evidence"],
                 "confidence": v["confidence"]}
                for rid, v in by_score[::step][:10]]

    census_complete = not unread and bool(records)
    return n, {
        "analysis": "discovery_consistency_classification",
        "population": {
            "records_read": read,
            "records_too_thin_to_classify": thin,
            "records_offered_to_the_model": len(records),
            "records_classified": n,
            "records_the_model_did_not_return": len(unread),
        },
        "census_complete": census_complete,
        "rates": rates,
        "recorded_all_five": all_five,
        "recorded_all_five_share": (round(all_five / n, 4)
                                    if n >= MIN_FOR_A_RATE else None),
        "recorded_none_of_the_five": none_of_five,
        "recorded_none_of_the_five_share": (round(none_of_five / n, 4)
                                            if n >= MIN_FOR_A_RATE else None),
        "by_rep": reps,
        "reps_too_few_records_to_report": sum(
            1 for vs in per_rep.values() if len(vs) < MIN_FOR_A_RATE),
        "to_check_by_hand": to_check,
        "questions": QUESTIONS,
        "low_confidence": sum(1 for v in labelled.values()
                              if v["confidence"] == "low"),
        "model": model.MODEL,
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "A census: every substantive record was offered to the "
                "model. Where census_complete is false some records came "
                "back unreadable and the rates describe only the rest. The "
                "ten records listed for checking are spread across the "
                "distribution, and reading them is what makes the rest "
                "trustworthy.",
    }


def adversarial_score_review(conn, engagement_id: str, heartbeat):
    """Argue against every score before the client sees it.

    This one reads no client export. It reads the scorecard Blend has just
    written, which makes it the only place in the assessment where the work
    is turned on itself.

    The two failures worth catching are named in GEA-P17: a four or five
    awarded on evidence that does not meet the anchor, and a zero or one
    that confuses an absence of the thing with an absence of evidence about
    the thing. The second is the one that gets a client angry, because it
    reads as an accusation when it is a gap in what they record.

    Nothing it produces is binding. Each challenge goes to a person, who
    defends it with a citation or concedes and moves the score.
    """
    model.check_configured()

    with conn.cursor() as cur:
        cur.execute("select * from gea_scorecard_for_review(%s)",
                    (engagement_id,))
        cols = [d.name for d in cur.description]
        scored = [dict(zip(cols, row)) for row in cur.fetchall()]
    conn.rollback()

    if not scored:
        raise ValueError(
            "nothing has been scored yet, so there is nothing to argue with")

    # One element per record, so the review argues with a score rather than
    # summarising a scorecard. Batched by the model layer.
    records = []
    for e in scored:
        reads = ("not evidenced" if e["not_evidenced"]
                 else str(e["value"]))
        cites = "; ".join(
            f"{c.get('what')}{': ' + c['note'] if c.get('note') else ''}"
            for c in (e["citations"] or [])) or "nothing cited"
        records.append({
            "id": e["element_ref"],
            "text": (
                f"ELEMENT {e['element_ref']}: {e['element_name']}\n"
                f"WHY IT IS ASSESSED: {e['why_assessed'] or ''}\n"
                f"SCORE GIVEN: {reads}\n"
                + (f"REASON RECORDED FOR NOT EVIDENCED: {e['ne_reason']}\n"
                   if e["not_evidenced"] and e["ne_reason"] else "")
                + (f"THE ANCHOR THIS SCORE CLAIMS: {e['anchor_claimed']}\n"
                   if e["anchor_claimed"] else "")
                + (f"THE ANCHOR BELOW IT: {e['anchor_below']}\n"
                   if e["anchor_below"] else "")
                + (f"THE ANCHOR ABOVE IT: {e['anchor_above']}\n"
                   if e["anchor_above"] else "")
                + (f"CAPPED AT: {e['cap_applied']}\n"
                   if e["cap_applied"] is not None else "")
                + (f"CONFIDENCE FLAGGED: {e['confidence']}\n"
                   if e["confidence"] else "")
                + f"WHAT THE SCORER WROTE: {e['scoring_note'] or 'nothing'}\n"
                + f"WHAT IT RESTS ON: {cites}"),
        })

    LABELS = ["evidence_does_not_meet_the_anchor",
              "absence_of_evidence_read_as_absence_of_the_thing",
              "citation_too_vague_to_retrieve",
              "should_be_a_limitation_not_a_score"]
    labelled, unread = model.classify(
        records,
        "Each record is one element of a completed assessment, with the "
        "score given, the anchor that score claims, the anchors either side "
        "of it, and what the score rests on.\n\n"
        "Argue against the score. Do not confirm it. Do not suggest wording "
        "for a report.\n\n"
        "Answer yes where the fault is present:\n"
        "- evidence_does_not_meet_the_anchor: what the score rests on does "
        "not satisfy what the claimed anchor requires.\n"
        "- absence_of_evidence_read_as_absence_of_the_thing: a low score "
        "treats missing records as proof the practice is missing. These are "
        "different, and only the first is evidenced.\n"
        "- citation_too_vague_to_retrieve: a colleague could not find the "
        "same evidence from what is cited.\n"
        "- should_be_a_limitation_not_a_score: confidence is low enough "
        "that publishing a number would overstate what is known.\n\n"
        "Your evidence quotation must come from the record, and it must be "
        "the part that shows the fault.",
        LABELS, heartbeat=heartbeat)

    challenges = []
    for ref, v in labelled.items():
        faults = [l for l in LABELS if v["labels"][l] == "yes"]
        if not faults:
            continue
        e = next(x for x in scored if x["element_ref"] == ref)
        reads = "not evidenced" if e["not_evidenced"] else str(e["value"])
        for fault in faults:
            challenges.append({
                "element_ref": ref,
                "weakness_type": fault,
                "challenge": (
                    f"{fault.replace('_', ' ').capitalize()}. "
                    f"{e['element_name']} reads {reads} and rests on "
                    f"{'nothing cited' if not e['citations'] else 'what is cited'}. "
                    f"The review points at: {v['evidence']}"),
                "confidence": v["confidence"],
            })

    unchallenged = len(scored) - len({c["element_ref"] for c in challenges})
    return len(challenges), {
        "analysis": "adversarial_score_review",
        "population": {
            "elements_scored": len(scored),
            "elements_reviewed": len(labelled),
            "elements_the_model_did_not_return": len(unread),
            "elements_with_nothing_argued": unchallenged,
        },
        "challenges": challenges,
        "review_complete": not unread,
        "by_fault": {l: sum(1 for c in challenges if c["weakness_type"] == l)
                     for l in LABELS},
        "model": model.MODEL,
        "note": "Nothing here is binding. Each challenge goes to a person "
                "who defends the score with its citation or concedes and "
                "changes it. An element with nothing argued is not thereby "
                "confirmed: it is an element this review found no fault in.",
    }


def synthesise_interview(conn, engagement_id: str, heartbeat, interview_id=None):
    """Read a conversation against the questions it was meant to cover.

    Forty minutes of talk, twenty to fifty questions, and a consultant who
    needs to find the part that bears on element 5.3. That is the whole job.

    It does not propose a score. Nothing here says what an element is worth,
    and the elements an interview serves are scored by a person who read it.
    The synthesis exists so they can find what they are looking for.

    Three outputs, and the second two are the ones worth having:

      What was said, per question, in the words that were said. A claim
      without the words is the model's summary, and a summary cannot be
      cited.

      What was never discussed. A guide has questions for a reason, and a
      question nobody asked is a gap in the evidence rather than a silence
      about the client. Consultants run out of time and skip the last ten
      minutes; this is what notices.

      Where the answer did not answer. Not a claim that somebody was hiding
      something — that is a judgement nobody should delegate — but a flag
      that the words given did not address what was asked, so a person can
      decide for themselves.
    """
    model.check_configured()

    with conn.cursor() as cur:
        cur.execute("""
            select i.id, i.interviewee_name, i.interviewee_role,
                   g.ref, g.function_name, a.storage_path, a.original_filename
              from interview i
              join interview_guide g on g.id = i.interview_guide_id
              left join artefact a on a.id = i.transcript_artefact_id
                                  and a.purged_at is null
             where i.engagement_id = %s
               and (%s::uuid is null or i.id = %s::uuid)
               and i.status = 'held'
               and i.synthesis_run_id is null
             order by i.held_at nulls last
             limit 1
        """, (engagement_id, interview_id, interview_id))
        row = cur.fetchone()
    conn.rollback()

    if row is None:
        raise ValueError(
            "there is no interview waiting to be read. One has to be marked "
            "held before its transcript can be synthesised")
    iid, who, role, guide_ref, function_name, path, filename = row
    if path is None:
        raise ValueError(
            f"the {function_name} interview has no transcript attached, so "
            f"there is nothing to read. Attach it and try again")

    with conn.cursor() as cur:
        cur.execute("select * from gea_interview_guide_questions(%s)", (iid,))
        questions = [{"id": r[0], "section": r[1], "text": r[2], "elements": r[3]}
                     for r in cur.fetchall()]
    conn.rollback()

    with storage.download(path).open("r", encoding="utf-8",
                                     errors="replace") as fh:
        transcript = fh.read()
    if len(transcript.split()) < 200:
        raise ValueError(
            f"that transcript is {len(transcript.split())} words, which is "
            f"too short to be a conversation. Check the right file is "
            f"attached")

    # The transcript goes with every batch of questions. It is the thing
    # being read, and a question judged against part of a conversation is
    # judged against the wrong thing.
    LABELS = ["was_asked", "was_answered", "the_answer_addressed_the_question"]
    records = [{"id": q["id"],
                "text": (f"QUESTION: {q['text']}\n"
                         + (f"SECTION: {q['section']}\n" if q["section"] else "")
                         + "\n--- THE TRANSCRIPT ---\n" + transcript[:60000])}
               for q in questions]

    labelled, unread = model.classify(
        records,
        f"Each record is one question from the {function_name} interview "
        f"guide, followed by the transcript of that conversation with "
        f"{who}{', ' + role if role else ''}.\n\n"
        "For each question, answer:\n"
        "- was_asked: the question, or something close enough to it, was put "
        "to the interviewee.\n"
        "- was_answered: they said something in response.\n"
        "- the_answer_addressed_the_question: what they said actually bears "
        "on what was asked. Answer no where they talked about something "
        "else, gave a generality, or deferred to somebody not present. This "
        "is not a claim that they were evasive; it is a flag for a person to "
        "judge.\n\n"
        "Your evidence quotation must be the interviewee's own words from "
        "the transcript, under fifteen words, and must be the part that "
        "answers the question where there is one. Where the question was "
        "never put, quote nothing and say so with an empty evidence string "
        "is not permitted — quote the nearest thing that was discussed "
        "instead.\n\n"
        "Never say what any of this is worth. You are not scoring anything.",
        LABELS, heartbeat=heartbeat)

    by_id = {q["id"]: q for q in questions}
    covered, never_asked, not_addressed = [], [], []
    for qid, v in labelled.items():
        q = by_id[qid]
        entry = {"question": q["text"], "elements": q["elements"],
                 "said": v["evidence"], "confidence": v["confidence"]}
        if v["labels"]["was_asked"] == "no":
            never_asked.append({"question": q["text"],
                                "elements": q["elements"]})
        elif v["labels"]["the_answer_addressed_the_question"] == "no":
            not_addressed.append(entry)
        elif v["labels"]["was_answered"] == "yes":
            covered.append(entry)

    # Which elements this conversation actually reached. An element whose
    # every question went unasked is not evidenced by this interview, and
    # saying so is the difference between a gap somebody can close and one
    # nobody notices until the scoring.
    reached, missed = set(), set()
    for q in questions:
        for e in q["elements"]:
            if any(x["question"] == q["text"] for x in covered):
                reached.add(e)
    for q in questions:
        for e in q["elements"]:
            if e not in reached:
                missed.add(e)

    return len(covered), {
        "analysis": "synthesise_interview",
        "interview": {"guide": guide_ref, "function": function_name,
                      "interviewee": who, "role": role,
                      "transcript": filename,
                      "words": len(transcript.split())},
        "population": {
            "questions_in_the_guide": len(questions),
            "questions_read": len(labelled),
            "questions_the_model_did_not_return": len(unread),
        },
        "what_was_said": covered,
        "never_asked": never_asked,
        "asked_but_not_addressed": not_addressed,
        "elements_this_conversation_reached": sorted(reached),
        "elements_it_did_not": sorted(missed),
        "complete": not unread,
        "model": model.MODEL,
        "note": "Nothing here is a score. It is what was said, against the "
                "questions the guide asks, in the words that were said. A "
                "question nobody put is a gap in the evidence, not a silence "
                "about the client. An answer flagged as not addressing the "
                "question is for a person to judge, not an accusation.",
    }


def detect_pipeline(conn, engagement_id: str, heartbeat):
    """Find the pipelines the client actually runs.

    Two sources, and the better one wins. A stage history export holds
    every transition a deal ever made, including into stages nobody uses
    any more. The deals export holds only the present: a stage column
    saying where each deal is today, and on some clients a date column per
    stage. Reading the deals export on a client who had replaced their
    pipeline found only the new one, because the old one does not appear
    in a file that describes now.

    Segments nest. A client splits by division and again by deal type
    inside it, and both are real questions. Choosing one throws the other
    away, so the split is a path and the outer level is the roll-up of
    what sits beneath it.

    And a pipeline has an era. This client replaced twelve stages with six
    partway through the window and is still moving deals across by hand.
    One definition per segment cannot say that, and a conversion rate
    spanning the change means nothing — so the stage sets are separated by
    when they were in use, and the changeover date is reported.
    """
    cols = resolve_columns(conn, engagement_id, "detect_pipeline")
    history = resolve_columns(conn, engagement_id, "stage_conversion_and_duration")

    # Every field that could split the client's business, outermost first.
    # A client with a division and a deal type gets both, nested.
    splits: list[tuple[str, Column]] = []
    for key in ("opportunity_type", "account_segment", "product_family"):
        c = cols.get(key) or _one_column(conn, engagement_id, key)
        if c and c.column_name:
            splits.append((key, c))

    from_history = all(k in history for k in
                       ("stage_history_opportunity_id", "stage_history_to",
                        "stage_entered_at"))
    heartbeat(0, 3)

    if from_history:
        events, source = _stages_from_history(conn, history), "the stage history export"
    else:
        events, source = _stages_from_deal_columns(conn, cols), \
            "the date columns on the deals export"
    if not events:
        raise ValueError(
            "no stage movement could be read. The stage history export has "
            "no usable rows, and the deals export carries no column pairing "
            "a stage name with a date.")

    heartbeat(1, 3)

    # Which era each stage belongs to.
    eras = _eras(events)

    # The segment each deal belongs to, read from the deals export.
    deal_segment = _deal_segments(conn, cols, splits)
    heartbeat(2, 3)

    report = _store_pipelines(conn, engagement_id, events, eras, deal_segment,
                              [k for k, _c in splits], source)
    return sum(len(v) for v in events.values()), {
        "analysis": "detect_pipeline",
        "read_from": source,
        "split_by": [k for k, _c in splits] or None,
        "eras": [{"era": e["era"], "from": str(e["from"]), "to": str(e["to"]),
                  "stages": sorted(e["stages"]), "label": e["label"],
                  "changed_over": (str(e["changed_over"])
                                   if e.get("changed_over") else None)}
                 for e in eras],
        "the_pipeline_was_replaced": len(eras) > 1,
        "pipelines": report,
        "note": "The stages are the client's own, read from their records "
                "rather than from a list. Where the stage set changed, the "
                "periods are kept apart: a conversion rate spanning a "
                "pipeline change describes neither pipeline.",
    }


def _stages_from_history(conn, cols) -> dict:
    """Every transition, from the stage history export."""
    id_col = cols["stage_history_opportunity_id"].column_name
    to_col = cols["stage_history_to"].column_name
    at_col = cols["stage_entered_at"].column_name
    from_col = (cols["stage_history_from"].column_name
                if "stage_history_from" in cols else None)

    out: dict[str, list] = defaultdict(list)
    for row in rows_of(conn, cols["stage_history_to"]):
        deal = (row.get(id_col) or "").strip()
        stage = (row.get(to_col) or "").strip()
        when = a_date(row.get(at_col))
        if not deal or not stage or when is None:
            continue
        # A row that changed no stage is an edit to something else.
        if from_col and (row.get(from_col) or "").strip() == stage:
            continue
        out[deal].append({"stage": stage, "when": when, "kind": "entered"})
    return out


def _stages_from_deal_columns(conn, cols) -> dict:
    """The date-per-stage columns some clients carry on the deal itself."""
    if "opportunity_status" not in cols or "opportunity_id" not in cols:
        return {}
    rows = rows_of(conn, cols["opportunity_status"])
    if not rows:
        return {}
    with conn.cursor() as cur:
        cur.execute("select field_name from dataset_field_profile where dataset_id = %s",
                    (cols["opportunity_status"].dataset_id,))
        headers = [r[0] for r in cur.fetchall()]
    conn.rollback()

    id_col = cols["opportunity_id"].column_name
    stage_col = cols["opportunity_status"].column_name
    seen = Counter((r.get(stage_col) or "").strip() for r in rows
                   if (r.get(stage_col) or "").strip())
    found, kinds = _stage_date_columns(seen, headers, rows, stage_col, id_col)
    if not found:
        return {}

    out: dict[str, list] = defaultdict(list)
    for r in rows:
        deal = (r.get(id_col) or "").strip()
        if not deal:
            continue
        for stage, col in found.items():
            when = a_date(r.get(col))
            if when:
                out[deal].append({"stage": stage, "when": when,
                                  "kind": kinds.get(stage, "entered")})
    return out


def _eras(events: dict) -> list[dict]:
    """Which stage set each stage belongs to.

    A client that replaces its pipeline does not switch overnight. The old
    stages and the new ones are both in use for months while deals are
    moved across by hand, so a month-by-month reading sees one muddled
    pipeline and never notices the change.

    What separates them is when each stage started. The stages of the old
    set have been in use since the beginning of the record; the new ones
    all appear within weeks of each other, long afterwards. So the first
    transition into each stage is the signal, and a gap of months between
    one group and the next is a pipeline that was replaced.

    Two sets at most are looked for, because a client that has changed its
    pipeline twice inside an assessment window has a finding larger than
    this analysis.
    """
    first_seen: dict[str, object] = {}
    last_seen: dict[str, object] = {}
    for moves in events.values():
        for m in moves:
            st, when = m["stage"], m["when"]
            if st not in first_seen or when < first_seen[st]:
                first_seen[st] = when
            if st not in last_seen or when > last_seen[st]:
                last_seen[st] = when
    if not first_seen:
        return []

    starts = sorted(first_seen.items(), key=lambda kv: kv[1])

    # A new pipeline arrives all at once.
    #
    # The first version looked for the widest gap between one stage
    # starting and the next, and split a real client in the wrong place: a
    # quiet six months in 2025 beat the week in 2026 when eleven stages
    # appeared together. A gap says nothing — a client can go months
    # without adding a stage. What says a pipeline was replaced is a
    # cluster of stages starting within days of each other, long after the
    # rest.
    #
    # So the boundary is the start of the largest such cluster, provided
    # something was already in use well before it.
    TOGETHER = timedelta(days=30)
    best_n, at = 0, None
    for i, (_st, when) in enumerate(starts):
        n = sum(1 for _s2, w2 in starts[i:] if w2 - when <= TOGETHER)
        # Something has to predate it, or this is simply the beginning of
        # the record.
        if i == 0 or when - starts[0][1] < timedelta(days=180):
            continue
        if n > best_n:
            best_n, at = n, when

    newer = {st for st, when in starts if at and when >= at}
    if at is None or best_n < 3 or len(newer) == len(starts):
        return [{"era": 1,
                 "from": min(first_seen.values()),
                 "to": max(last_seen.values()),
                 "stages": set(first_seen),
                 "changed_over": None,
                 "label": f"{len(first_seen)} stages, one set throughout"}]

    older = set(first_seen) - newer
    return [
        {"era": 1, "from": min(first_seen[s] for s in older),
         "to": max(last_seen[s] for s in older), "stages": older,
         "changed_over": at,
         "label": f"{len(older)} stages, the set in use until {at}"},
        {"era": 2, "from": at,
         "to": max(last_seen[s] for s in newer), "stages": newer,
         "changed_over": at,
         "label": f"{len(newer)} stages, the set introduced {at}"},
    ]

def _deal_segments(conn, cols, splits) -> dict[str, list[str]]:
    """Which segment each deal belongs to, at every level of the split."""
    if not splits or "opportunity_id" not in cols:
        return {}
    rows = rows_of(conn, cols["opportunity_id"])
    id_col = cols["opportunity_id"].column_name
    out = {}
    for r in rows:
        deal = (r.get(id_col) or "").strip()
        if not deal:
            continue
        out[deal] = [((r.get(c.column_name) or "").strip() or "not recorded")
                     for _k, c in splits]
    return out


def _store_pipelines(conn, engagement_id, events, eras, deal_segment,
                     split_keys, source) -> list[dict]:
    """One pipeline per segment path per era, and the rows beneath them."""
    era_of = {}
    for e in eras:
        for st in e["stages"]:
            era_of.setdefault(st, e["era"])

    # Every level of the split, so a division has its own picture as well
    # as each of its deal types.
    buckets: dict[tuple, list] = defaultdict(list)
    for deal, moves in events.items():
        path = deal_segment.get(deal, [])
        levels = [tuple(path[:n]) for n in range(len(path) + 1)] or [()]
        for m in moves:
            era = era_of.get(m["stage"], 1)
            for lv in levels:
                buckets[(lv, era)].append((deal, m))

    report = []
    with conn.cursor() as cur:
        cur.execute("delete from deal_stage_entry where engagement_id = %s",
                    (engagement_id,))
        cur.execute("delete from pipeline where engagement_id = %s",
                    (engagement_id,))
        for (path, era), items in sorted(
                buckets.items(), key=lambda kv: (len(kv[0][0]), kv[0])):
            label = " · ".join(path) if path else "all deals"
            deals = len({d for d, _m in items})
            e = next((x for x in eras if x["era"] == era), eras[0])
            cur.execute("""
                insert into pipeline (engagement_id, segment, segment_field,
                       segment_fields, segment_values, era, era_from, era_to,
                       era_label, deals, deals_with_any_stage_date)
                     values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) returning id
            """, (engagement_id, label,
                  split_keys[0] if split_keys else None,
                  split_keys[:len(path)], list(path), era,
                  e["from"], e["to"] if era < len(eras) else None,
                  e["label"], deals, deals))
            pid = cur.fetchone()[0]

            per_stage: dict[str, list] = defaultdict(list)
            for _d, m in items:
                per_stage[m["stage"]].append(m["when"])
            ordered = _order_stages(per_stage)
            for i, (stage, dates) in enumerate(ordered, 1):
                mid = sorted(dates)[len(dates) // 2]
                cur.execute("""
                    insert into pipeline_stage (pipeline_id, stage_name,
                           position, source_column, deals_entered,
                           median_entered, is_closed_won, is_closed_lost,
                           stage_number, date_kind)
                         values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, (pid, stage, i, source, len(dates), mid,
                      outcome(stage) == "won", outcome(stage) == "lost",
                      _stage_number(stage), "entered"))

            if not path:
                cur.executemany("""
                    insert into deal_stage_entry (engagement_id,
                           opportunity_id, segment, stage_name, entered_at,
                           era)
                         values (%s,%s,%s,%s,%s,%s)
                    on conflict do nothing
                """, [(engagement_id, d, label, m["stage"], m["when"], era)
                      for d, m in items])

            report.append({
                "segment": label, "depth": len(path), "era": era,
                "deals": deals, "entries": len(items),
                "stages": [{"stage": s, "position": i, "deals_entered": len(d)}
                           for i, (s, d) in enumerate(ordered, 1)],
            })
    conn.commit()
    return report


def _stage_number(stage: str):
    m = re.match(r"\s*(\d{1,2})\s*[-–—.:)]", stage or "")
    return int(m.group(1)) if m else None


def _order_stages(per_stage: dict) -> list:
    """The order the stages run in: the client's numbers, or the dates."""
    by_date = sorted(per_stage.items(),
                     key=lambda kv: sorted(kv[1])[len(kv[1]) // 2])
    numbered = [s for s in per_stage if _stage_number(s) is not None]
    if len(numbered) >= max(2, len(per_stage) // 2):
        return sorted(per_stage.items(),
                      key=lambda kv: (_stage_number(kv[0])
                                      if _stage_number(kv[0]) is not None
                                      else 99,
                                      sorted(kv[1])[len(kv[1]) // 2]))
    return by_date


def _stage_date_columns(seen, headers, rows, stage_col, id_col):
    """Columns pairing a stage name, or its number, with a date."""
    DATE_WORDS = ("date", "entered", "enter", "moved", "reached", "at",
                  "time", "timestamp", "when", "exit", "exited", "left")
    found, kinds = {}, {}
    for stage in seen:
        flat = _flat(stage)
        best, extra_best, kind = None, 999, "entered"
        for h in headers:
            if h in (stage_col, id_col) or not flat:
                continue
            hf = _flat(h)
            if flat not in hf:
                continue
            rest = hf.replace(flat, " ")
            if not any(w in rest for w in DATE_WORDS):
                continue
            if len(rest.split()) < extra_best:
                best, extra_best = h, len(rest.split())
                kind = "exited" if any(w in rest for w in
                                       ("exit", "exited", "left")) else "entered"
        n = _stage_number(stage)
        if best is None and n is not None:
            for h in headers:
                hf = _flat(h)
                if "stage" not in hf or not re.search(rf"\b{n}\b", hf):
                    continue
                rest = re.sub(rf"\bstage\b|\b{n}\b", " ", hf)
                if not any(w in rest for w in DATE_WORDS):
                    continue
                if len(rest.split()) < extra_best:
                    best, extra_best = h, len(rest.split())
                    kind = "exited" if "exit" in rest else "entered"
        if best:
            found[stage], kinds[stage] = best, kind
    return found, kinds



def _col_for_path(cols: dict, path: str) -> "Column":
    """Any column from the file at this path.

    Some analyses pick a file once and read several of its columns, naming
    the file rather than a field. The rows are the same whichever of its
    columns asks for them.
    """
    for c in cols.values():
        if c.storage_path == path:
            return c
    raise ValueError("no mapped column belongs to that file")


def rows_of(conn, column: "Column") -> list[dict]:
    """Every row of the file this column is in, from the database.

    Analyses used to download the file and read it with a CSV reader.
    That silently produced nothing on an xlsx export: the reader finds no
    delimiters in a zip, so every row comes back as one unparseable field
    and the analysis reports zeros rather than failing. A client's
    opportunity export arrived that way and the pricing analysis reported
    nought closed-won deals, which read as a finding about them.

    The rows have been in the database since the row store was built. No
    analysis has any reason to touch a file again, and reading from the
    database joins, filters and counts without downloading anything.
    """
    if not column.dataset_id:
        raise ValueError(
            f"the {column.canonical_key} mapping has no dataset behind it, "
            f"so its rows cannot be read")
    with conn.cursor() as cur:
        cur.execute("""
            select data from dataset_row
             where dataset_id = %s order by row_number
        """, (column.dataset_id,))
        rows = [r[0] for r in cur.fetchall()]
    conn.rollback()
    return rows


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

    The confirmed name, or the proposed one where a mapping was accepted
    automatically and never carried a confirmed name of its own. The
    screen was fixed to read both a day before this was; the service was
    not, so every auto-accepted field resolved to a column called nothing.
    Nothing failed: the analysis read a column name of null from every
    row, found nothing in it, and reported that the client's data was
    empty.

    Only settled mappings. A proposal nobody accepted is not a field the
    client has, and reading one would mean computing a figure from a column
    a person never agreed to.

    The export reference is looked up where it can be, and left empty where
    it cannot. It is used for reporting and nothing else, and requiring it
    made a confirmed mapping invisible the moment its file lost its link to
    the export line — which is exactly what replacing a file does. The
    screen went on showing the field as mapped while the analysis reported
    it missing, and both were reading the truth they had.
    """
    with conn.cursor() as cur:
        cur.execute("""
            select cf.key,
                   coalesce(m.confirmed_field_name, m.proposed_field_name),
                   a.storage_path, el.ref,
                   d.id::text
              from analysis_field_requirement r
              join engagement e on e.id = %s
                               and e.instrument_version_id = r.instrument_version_id
              join canonical_field cf on cf.id = r.canonical_field_id
              join field_mapping m on m.canonical_field_id = cf.id
                                  and m.engagement_id = e.id
                                  and m.status in ('confirmed','corrected','auto_accepted')
              join dataset d on d.id = m.dataset_id
              join artefact a on a.id = d.artefact_id and a.purged_at is null
              left join export_request_line erl on erl.id = a.export_request_line_id
              left join export_line el on el.id = erl.export_line_id
             where r.analysis_key = %s
        """, (engagement_id, analysis_key))
        found = {k: Column(k, name, path, ref or "", ds)
                 for k, name, path, ref, ds in cur.fetchall()}

    # A settled mapping with no column name is a broken record, not an
    # absent field, and the two must not look alike: reading from a column
    # called nothing returns nothing from every row, and the analysis then
    # says the client's data is empty.
    nameless = sorted(k for k, c in found.items() if not c.column_name)
    if nameless:
        raise ValueError(
            f"{', '.join(nameless)} {'is' if len(nameless) == 1 else 'are'} "
            f"recorded as mapped and no column is named. Open the field "
            f"mapping and choose the column again.")
    return found


def missing_fields(conn, engagement_id: str, analysis_key: str,
                   needed: list[str], cols: dict) -> str | None:
    """Why an analysis cannot run, told apart properly.

    A field can be absent for two reasons that look identical from here and
    need completely different work. Either nobody mapped it, or the
    engagement is pinned to an instrument version that never asked for it —
    and the second happens whenever the methodology gains a field after an
    engagement starts, which it has done four times.

    Saying "not mapped" in the second case sends somebody to a screen where
    the field is already mapped, or is not listed at all. Both are a waste
    of their afternoon.
    """
    absent = [k for k in needed if k not in cols]
    if not absent:
        return None

    with conn.cursor() as cur:
        cur.execute("""
            select cf.key
              from analysis_field_requirement r
              join engagement e on e.id = %s
                               and e.instrument_version_id = r.instrument_version_id
              join canonical_field cf on cf.id = r.canonical_field_id
             where r.analysis_key = %s and cf.key = any(%s)
        """, (engagement_id, analysis_key, absent))
        declared = {row[0] for row in cur.fetchall()}
        cur.execute("""
            select iv.label from engagement e
              join instrument_version iv on iv.id = e.instrument_version_id
             where e.id = %s""", (engagement_id,))
        version = cur.fetchone()[0]
    conn.rollback()

    unmapped = sorted(k for k in absent if k in declared)
    undeclared = sorted(k for k in absent if k not in declared)

    parts = []
    if unmapped:
        parts.append("this analysis needs fields that are not mapped: "
                     + ", ".join(unmapped))
    if undeclared:
        parts.append(
            f"this engagement runs on instrument {version}, which does not "
            f"ask for {', '.join(undeclared)}. The analysis was built against "
            f"a later version. Start a new engagement on the current "
            f"instrument, or the figures would be computed from fields this "
            f"one never required")
    return ". ".join(parts)


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


# Values that occupy the field without answering it. A loss reason of
# "Other" does not say why the deal was lost; it says the person closing it
# had somewhere to click that was not a reason. Counting these as the leading
# answer turns a tracking failure into a finding about the market, which is
# the opposite of true.
NON_ANSWERS = {
    "other", "others", "n/a", "na", "none", "no reason", "not applicable",
    "unknown", "unspecified", "not specified", "not set", "tbd",
    "to be determined", "misc", "miscellaneous", "default", "-", "--",
    "blank", "null", "nil", "no", "n a", "not recorded", "not stated",
    "undefined", "general", "no answer",
}


def is_a_non_answer(value: str) -> bool:
    return str(value).strip().lower().strip(".") in NON_ANSWERS


@dataclass
class Cut:
    """One dimension of the picture, and the records behind it.

    Three buckets, never two. A record can carry a real value, carry a value
    that is not an answer, or carry nothing. Collapsing the last two loses
    the difference between a field nobody fills in and a field everyone fills
    in with "Other" — and the second is the worse finding, because it looks
    like compliance.
    """
    name: str
    counts: Counter = field(default_factory=Counter)
    non_answers: Counter = field(default_factory=Counter)
    missing: int = 0

    def add(self, value: str | None) -> None:
        if value is None or str(value).strip() == "":
            self.missing += 1
        elif is_a_non_answer(value):
            self.non_answers[str(value).strip()] += 1
        else:
            self.counts[str(value).strip()] += 1

    @property
    def answered(self) -> int:
        return sum(self.counts.values())

    @property
    def not_answered(self) -> int:
        return sum(self.non_answers.values()) + self.missing

    def as_dict(self, total: int) -> dict:
        known = self.answered
        rows = []
        for value, n in self.counts.most_common():
            row = {"value": value, "count": n, "of": known}
            # A share computed from nine records is not a share.
            if known >= MIN_FOR_A_RATE:
                row["share"] = round(n / known, 4)
            rows.append(row)
        held = sum(self.non_answers.values())
        return {
            "dimension": self.name,
            "records_with_an_answer": known,
            "records_with_a_non_answer": held,
            "records_with_nothing": self.missing,
            "of": total,
            # What a consultant needs first: how much of this field is unusable.
            "unusable_share": (round((held + self.missing) / total, 4)
                               if total >= MIN_FOR_A_RATE else None),
            "non_answers_used": [{"value": v, "count": n}
                                 for v, n in self.non_answers.most_common()],
            "suppressed": known < MIN_FOR_A_RATE,
            "values": rows,
        }


def load_accounts(conn, cols: dict[str, Column]) -> dict[str, dict]:
    """A lookup from account to its firmographics, from the companies export.

    Held in memory because it is one row per customer, not one per deal: a
    client with twenty thousand accounts is a few megabytes. The deals file,
    which is the big one, is never held.
    """
    out: dict[str, dict] = {}
    if "account_id" not in cols:
        return out
    for row in rows_of(conn, cols["account_id"]):
        key = (row.get(cols["account_id"].column_name) or "").strip()
        if not key:
            continue
        out[key] = {
                name: row.get(cols[field].column_name)
                for name, field in (("industry", "account_industry"),
                                    ("country", "account_country"),
                                    ("employees", "account_employee_count"),
                                    ("revenue", "account_annual_revenue"),
                                    ("segment", "account_segment"))
                if field in cols
            }
    return out


def firmographic_cuts(cols: dict[str, Column]) -> tuple[dict[str, Cut], list[str]]:
    """The dimensions a firmographic picture is cut by, and those not mapped."""
    dims = [("industry", "account_industry"), ("country", "account_country"),
            ("employee band", "account_employee_count"),
            ("revenue band", "account_annual_revenue"),
            ("segment", "account_segment")]
    return ({n: Cut(n) for n, k in dims if k in cols},
            [n for n, k in dims if k not in cols])


def add_firmographics(cuts: dict[str, Cut], acct: dict) -> None:
    if "industry" in cuts:
        cuts["industry"].add(acct.get("industry"))
    if "country" in cuts:
        cuts["country"].add(acct.get("country"))
    if "employee band" in cuts:
        cuts["employee band"].add(_band(acct.get("employees"), EMPLOYEE_BANDS))
    if "revenue band" in cuts:
        cuts["revenue band"].add(_band(acct.get("revenue"), REVENUE_BANDS))
    if "segment" in cuts:
        cuts["segment"].add(acct.get("segment"))


def money(value) -> float | None:
    """A number, or None. An unparseable figure is not a zero."""
    if value is None:
        return None
    t = str(value).strip().replace(",", "").replace("$", "").replace("£", "")
    t = t.replace("€", "")
    if t in ("", "-"):
        return None
    try:
        return float(t)
    except ValueError:
        return None


# A list of exact stage names cannot survive contact with a real CRM.
# "6 - Closed Won", "Closed Won (Implemented)", "Closed Lost to Competitor"
# are all ordinary and none of them matches a fixed string. So the word is
# looked for inside the stage rather than compared to it.
#
# Lost is checked first, because a stage can mention both — "Closed Lost,
# Won by Competitor" is not a win.
NOT_A_WIN = ("not won", "no decision", "never won", "not closed won")


def outcome(value) -> str | None:
    """Won, lost, or neither. Neither is open, and open is not a loss."""
    t = " ".join(str(value or "").strip().lower().split())
    if not t:
        return None
    if any(n in t for n in NOT_A_WIN):
        return "lost" if "lost" in t else None
    if "lost" in t:
        return "lost"
    if "won" in t or t == "win":
        return "won"
    return None


@dataclass
class Outcomes:
    """What the stage column actually contained.

    An analysis that classified nothing has not found that the client never
    won a deal. It has found that it cannot read their stage names, and the
    difference is the whole difference between a finding and a bug. So what
    was seen is kept, and reported when nothing matched.
    """
    seen: Counter = field(default_factory=Counter)

    def classify(self, value) -> str | None:
        result = outcome(value)
        self.seen[(str(value or "").strip() or "(empty)", result or "neither")] += 1
        return result

    @property
    def classified(self) -> int:
        return sum(n for (_v, r), n in self.seen.items() if r != "neither")

    def refuse_if_nothing_matched(self, column: str) -> None:
        if self.classified:
            return
        top = ", ".join(f"{v!r} ({n})" for (v, _r), n in self.seen.most_common(8))
        raise ValueError(
            f"none of the values in {column} could be read as won or lost, so "
            f"there is nothing to measure. What is in that column: {top}. "
            f"Either the wrong column is mapped, or this client records "
            f"outcomes in words that need adding to the methodology.")

    def as_dict(self) -> dict:
        return {"stage_values_seen": [
            {"value": v, "read_as": r, "deals": n}
            for (v, r), n in self.seen.most_common(20)]}


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
    problem = missing_fields(conn, engagement_id, "icp_reconciliation",
                             needed, cols)
    if problem:
        raise ValueError(problem)

    deals_path = cols["opportunity_status"].storage_path
    accounts_path = cols["account_id"].storage_path

    # The companies file first: a lookup from account to its firmographics.
    seen = Outcomes()
    heartbeat(0, 2)
    accounts = load_accounts(conn, cols)
    heartbeat(1, 2)

    # Then the deals, one at a time. The file is never held in memory.
    # Only cut by a dimension whose field is actually mapped. A cut reading
    # "0 with a value, 362 without" on an unmapped field says the client
    # leaves it blank, when the truth is that nobody looked. Those are
    # different findings and must not look alike.
    cuts, not_cut = firmographic_cuts(cols)
    deals = won_deals = no_account = unmatched = 0

    status_col = cols["opportunity_status"].column_name
    account_col = cols["opportunity_account_id"].column_name

    for row in rows_of(conn, _col_for_path(cols, deals_path)):
            deals += 1
            if seen.classify(row.get(status_col)) != "won":
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

            add_firmographics(cuts, acct)
    heartbeat(2, 2)

    seen.refuse_if_nothing_matched(cols["opportunity_status"].column_name)
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
        **seen.as_dict(),
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


def win_loss_and_churn_patterns(conn, engagement_id: str, heartbeat):
    """Who actually wins, who actually churns, and whether those agree.

    The win rate is won over won plus lost. Open deals are excluded and
    counted, because an open deal is not a loss and treating it as one
    flatters nobody.

    The loss reason distribution carries the share held by its single most
    common value, and nothing more. A reason holding most of the records is
    usually a default nobody changed rather than a pattern, and deciding
    which is a judgement about how the client's team works — not something
    this can tell from the values.
    """
    cols = resolve_columns(conn, engagement_id, "win_loss_and_churn_patterns")
    needed = ["opportunity_id", "opportunity_status", "opportunity_account_id",
              "loss_reason", "account_id", "account_industry",
              "account_employee_count"]
    problem = missing_fields(conn, engagement_id, "win_loss_and_churn_patterns", needed, cols)
    if problem:
        raise ValueError(problem)

    seen = Outcomes()
    heartbeat(0, 2)
    accounts = load_accounts(conn, cols)
    heartbeat(1, 2)

    won_cuts, not_cut = firmographic_cuts(cols)
    lost_cuts, _ = firmographic_cuts(cols)
    loss_reasons, win_reasons, competitors = Cut("loss reason"), Cut("win reason"), Cut("competitor named")

    deals = won = lost = open_deals = no_account = unmatched = 0
    status_col = cols["opportunity_status"].column_name
    account_col = cols["opportunity_account_id"].column_name

    for row in rows_of(conn, cols["opportunity_status"]):
            deals += 1
            result = seen.classify(row.get(status_col))
            if result is None:
                open_deals += 1
                continue
            if result == "won":
                won += 1
                if "win_reason" in cols:
                    win_reasons.add(row.get(cols["win_reason"].column_name))
            else:
                lost += 1
                loss_reasons.add(row.get(cols["loss_reason"].column_name))
                if "competitor_named" in cols:
                    competitors.add(row.get(cols["competitor_named"].column_name))

            key = (row.get(account_col) or "").strip()
            if not key:
                no_account += 1
                continue
            acct = accounts.get(key)
            if acct is None:
                unmatched += 1
                continue
            add_firmographics(won_cuts if result == "won" else lost_cuts, acct)
    heartbeat(2, 2)

    seen.refuse_if_nothing_matched(cols["opportunity_status"].column_name)
    decided = won + lost
    overall = round(won / decided, 4) if decided >= MIN_FOR_A_RATE else None

    # Where the win rate differs most from the overall rate. Reported with
    # both counts, because a cut of twelve records swinging twenty points is
    # arithmetic, not a finding.
    variance = []
    for name, cut in won_cuts.items():
        for value, n_won in cut.counts.items():
            n_lost = lost_cuts[name].counts.get(value, 0)
            total = n_won + n_lost
            if total < MIN_FOR_A_RATE:
                continue
            rate = n_won / total
            variance.append({
                "dimension": name, "value": value,
                "won": n_won, "lost": n_lost, "of": total,
                **seen.as_dict(),
        "win_rate": round(rate, 4),
                "points_from_overall": (round((rate - overall) * 100, 1)
                                        if overall is not None else None),
            })
    variance.sort(key=lambda v: abs(v["points_from_overall"] or 0), reverse=True)

    reasons = loss_reasons.as_dict(lost)
    out = {
        "analysis": "win_loss_and_churn_patterns",
        "population": {
            "deals_read": deals, "won": won, "lost": lost,
            "decided": decided, "still_open": open_deals,
            "described": decided - no_account - unmatched,
        },
        "win_rate": overall,
        "win_rate_of": decided,
        "widest_variance": variance[:3],
        "loss_reasons": reasons,
        # The first number a consultant needs. A client whose loss reasons are
        # two thirds "Other" has no loss reason data, whatever the field says,
        # and no pattern read from the remaining third is safe to generalise.
        "losses_with_no_usable_reason": loss_reasons.not_answered,
        "losses_with_no_usable_reason_share": (
            round(loss_reasons.not_answered / lost, 4)
            if lost >= MIN_FOR_A_RATE else None),
        "won_by": [c.as_dict(won) for c in won_cuts.values()],
        "lost_by": [c.as_dict(lost) for c in lost_cuts.values()],
        "not_cut_because_the_field_is_not_mapped": not_cut,
        "gaps": [g.as_dict() for g in (
            [Gap("decided deals with no account on the record", no_account, decided)] if no_account else [])
            + ([Gap("decided deals whose account is not in the companies export",
                    unmatched, decided)] if unmatched else [])],
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "Values like Other and N/A are counted as no reason given, "
                "not as a reason. Where most losses carry one, the client "
                "does not record why deals are lost, and the pattern in the "
                "remainder describes only the minority who filled it in.",
    }
    if "win_reason" in cols:
        out["win_reasons"] = win_reasons.as_dict(won)
    if "competitor_named" in cols:
        out["competitors"] = competitors.as_dict(lost)
    return decided, out


def price_discount_dispersion(conn, engagement_id: str, heartbeat):
    """What the client actually charges, against what they say they charge.

    Discount is list minus realised, over list, per closed-won deal. Only
    where both are present and list is above zero: a deal with no list price
    has no discount, and calling that nought per cent would report
    discipline the client has not got.

    A realised price above list is kept as an uplift rather than clamped to
    nought. It is rare, it is real, and hiding it would make the spread look
    tidier than it is.
    """
    cols = resolve_columns(conn, engagement_id, "price_discount_dispersion")
    needed = ["opportunity_id", "opportunity_status", "opportunity_list_price",
              "opportunity_realised_price"]
    problem = missing_fields(conn, engagement_id, "price_discount_dispersion", needed, cols)
    if problem:
        raise ValueError(problem)

    accounts = load_accounts(conn, cols) if "account_id" in cols else {}
    discounts: list[float] = []
    same_as_list = 0
    by_segment: dict[str, list[float]] = defaultdict(list)
    by_owner: dict[str, list[float]] = defaultdict(list)
    won = no_list = no_realised = zero_list = 0

    status_col = cols["opportunity_status"].column_name
    list_col = cols["opportunity_list_price"].column_name
    real_col = cols["opportunity_realised_price"].column_name

    seen = Outcomes()
    for row in rows_of(conn, cols["opportunity_status"]):
            if seen.classify(row.get(status_col)) != "won":
                continue
            won += 1
            lst, real = money(row.get(list_col)), money(row.get(real_col))
            if lst is None:
                no_list += 1
                continue
            if real is None:
                no_realised += 1
                continue
            if lst <= 0:
                zero_list += 1
                continue
            if lst == real:
                same_as_list += 1
            d = (lst - real) / lst
            discounts.append(d)
            if "opportunity_owner" in cols:
                by_owner[(row.get(cols["opportunity_owner"].column_name)
                          or "not recorded").strip()].append(d)
            if accounts and "account_segment" in cols:
                acct = accounts.get((row.get(cols["opportunity_account_id"].column_name) or "").strip())
                if acct:
                    by_segment[(acct.get("segment") or "not recorded")].append(d)
    heartbeat(1, 1)

    seen.refuse_if_nothing_matched(cols["opportunity_status"].column_name)
    def spread(values: list[float]) -> dict | None:
        if len(values) < MIN_FOR_A_RATE:
            return {"count": len(values), "suppressed": True}
        v = sorted(values)
        def at(q): return round(v[min(int(q * len(v)), len(v) - 1)], 4)
        return {
            "count": len(v),
            "no_discount": round(sum(1 for x in v if x <= 0) / len(v), 4),
            "lowest_tenth": at(0.10), "lower_quarter": at(0.25),
            "middle": at(0.50), "upper_quarter": at(0.75),
            "highest_tenth": at(0.90),
            "suppressed": False,
        }

    return won, {
        "analysis": "price_discount_dispersion",
        "population": {
            "closed_won": won, "priced": len(discounts),
            "no_list_price": no_list, "no_realised_price": no_realised,
            "list_price_of_zero_or_less": zero_list,
            "realised_equals_list": same_as_list,
        },
        # A client who prices every deal on its merits has no list price to
        # discount from. Reporting that as nought per cent discount would
        # describe pricing discipline they have not got, and it is the
        # commonest shape in the mid-market: custom pricing per deal, no
        # packaging to hold the line.
        "no_list_price_structure": (
            len(discounts) >= MIN_FOR_A_RATE
            and (same_as_list / len(discounts)) >= 0.95),
        **seen.as_dict(),
        "discount": spread(discounts),
        "by_segment": ({k: spread(v) for k, v in sorted(by_segment.items())}
                       if by_segment else None),
        "by_owner": ({k: spread(v) for k, v in sorted(by_owner.items())}
                     if by_owner else None),
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "Discount is list minus realised over list, on closed-won "
                "deals where both are recorded. Deals with no list price are "
                "counted, not treated as full price. Where the realised price "
                "equals the list price on nearly every deal, the client does "
                "not operate a list price, and that is a finding about "
                "packaging rather than a record of perfect discipline.",
    }


def a_date(value):
    """A date, or None. Several shapes, because clients export all of them."""
    t = (str(value) if value is not None else "").strip()
    if not t:
        return None
    t = t.replace("T", " ").split(" ")[0].split("+")[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def a_month(value) -> str | None:
    d = a_date(value)
    if d:
        return f"{d.year:04d}-{d.month:02d}"
    t = (str(value) if value is not None else "").strip()
    # Spend files often carry the month alone.
    if len(t) == 7 and t[4] in "-/" and t[:4].isdigit():
        return f"{t[:4]}-{t[5:7]}"
    return None


def same_channel(name) -> str:
    """Channels are typed by different people in different systems.

    Marketing writes "Paid Search" in the spend sheet and the CRM says
    "paid_search". They are the same channel and must not be reported as two.
    """
    return " ".join(str(name or "").strip().lower()
                    .replace("_", " ").replace("-", " ").split())


def stage_conversion_and_duration(conn, engagement_id: str, heartbeat):
    """How far deals get, and how long they sit.

    No stage order is assumed. The client's stages are their own, and a
    sequence inferred from the data would be a guess presented as structure.
    What is measured instead needs no order: of the deals that ever entered a
    stage, how many went on to be won, and how long they stayed.

    Deals that skip stages are counted. A pipeline where half the deals
    appear first in the final stage is not a pipeline, it is a record kept
    after the fact, and no conversion rate read from it means anything.
    """
    cols = resolve_columns(conn, engagement_id, "stage_conversion_and_duration")

    # Two routes to the same rows.
    #
    # A stage history export arrives as one row per deal per stage, which
    # is what this needs. Most clients have no such export: HubSpot writes
    # a date property per stage on the deal, and a Salesforce report is
    # usually built the same way. detect_pipeline reads those columns and
    # pivots them into deal_stage_entry, which holds exactly the rows the
    # export would have held.
    #
    # So where the pivot has run, it is used, and the export stops being
    # something to chase. Where neither exists, the refusal names both.
    pivoted = _pivot_available(conn, engagement_id)
    needed = ["opportunity_id", "opportunity_status"]
    if not pivoted:
        needed += ["stage_history_opportunity_id", "stage_history_to",
                   "stage_entered_at"]
    problem = missing_fields(conn, engagement_id, "stage_conversion_and_duration",
                             needed, cols)
    if problem:
        if not pivoted:
            problem += (". The stages can also be read from the deals export "
                        "itself, where it carries a date column per stage. "
                        "Run the pipeline detection and this will not need "
                        "the stage history export at all.")
        raise ValueError(problem)

    # The outcome of each deal, from the deals export.
    heartbeat(0, 2)
    outcomes: dict[str, str | None] = {}
    closed_on: dict[str, object] = {}
    for row in rows_of(conn, cols["opportunity_id"]):
            key = (row.get(cols["opportunity_id"].column_name) or "").strip()
            if not key:
                continue
            outcomes[key] = outcome(row.get(cols["opportunity_status"].column_name))
            if "opportunity_close_date" in cols:
                when = a_date(row.get(cols["opportunity_close_date"].column_name))
                if when:
                    closed_on[key] = when
    heartbeat(1, 2)

    # Then the history: every stage each deal entered, and when.
    history: dict[str, list[tuple[str, object]]] = defaultdict(list)
    rows = undated = orphan = unchanged = 0
    read_from = "the stage history export"

    if pivoted:
        read_from = "the deals export, pivoted"
        for deal, stage, when, _segment in _stage_rows_from_pivot(
                conn, engagement_id):
            rows += 1
            if deal not in outcomes:
                orphan += 1
                continue
            history[deal].append((stage, when))
    else:
        # A history row is written for any change to the opportunity, not
        # only a stage change. Salesforce records "5 - Submit to 5 - Submit"
        # when somebody edits the amount or the close date, and on the
        # first real history export two thousand five hundred of six
        # thousand rows were that. Counted as stage movements they invent
        # entries and destroy every duration.
        #
        # Where the export says which stage a row came from, a row that
        # did not move is dropped. Where it does not, they cannot be told
        # apart and the count below is nought.
        from_col = (cols["stage_history_from"].column_name
                    if "stage_history_from" in cols else None)
        for row in rows_of(conn, cols["stage_history_to"]):
            rows += 1
            deal = (row.get(cols["stage_history_opportunity_id"].column_name) or "").strip()
            stage = (row.get(cols["stage_history_to"].column_name) or "").strip()
            when = a_date(row.get(cols["stage_entered_at"].column_name))
            if not deal or not stage:
                continue
            if from_col and (row.get(from_col) or "").strip() == stage:
                unchanged += 1
                continue
            if deal not in outcomes:
                orphan += 1
                continue
            if when is None:
                undated += 1
                continue
            history[deal].append((stage, when))
    heartbeat(2, 2)

    entered: Counter = Counter()
    won_after: Counter = Counter()
    dwell: dict[str, list[int]] = defaultdict(list)
    transitions: Counter = Counter()
    first_stage: Counter = Counter()

    for deal, events in history.items():
        events.sort(key=lambda e: e[1])
        result = outcomes.get(deal)

        # The last stage a deal reached has nothing after it, so without this
        # it has no dwell time at all — and time stuck in the final stage is
        # usually the number worth having. A closed deal's close date ends
        # its last stage. An open one genuinely has no end yet, and is left
        # out rather than measured to today, which would grow every time the
        # analysis ran.
        if result is not None and deal in closed_on:
            closing = closed_on[deal]
            if closing >= events[-1][1]:
                events.append(("__closed__", closing))
        seen = set()
        first_stage[events[0][0]] += 1
        for i, (stage, when) in enumerate(events):
            if stage == "__closed__":
                continue
            if stage not in seen:
                seen.add(stage)
                entered[stage] += 1
                if result == "won":
                    won_after[stage] += 1
            if i + 1 < len(events):
                nxt = events[i + 1]
                if nxt[0] != "__closed__":
                    transitions[(stage, nxt[0])] += 1
                dwell[stage].append((nxt[1] - when).days)

    def days(values: list[int]) -> dict:
        if len(values) < MIN_FOR_A_RATE:
            return {"count": len(values), "suppressed": True}
        v = sorted(values)
        def at(q): return v[min(int(q * len(v)), len(v) - 1)]
        return {"count": len(v), "quickest_tenth": at(0.10),
                "middle": at(0.50), "slowest_tenth": at(0.90),
                "suppressed": False}

    stages = []
    for stage, n in entered.most_common():
        row = {"stage": stage, "deals_that_entered": n,
               "went_on_to_be_won": won_after[stage],
               "days_in_the_stage": days(dwell.get(stage, []))}
        if n >= MIN_FOR_A_RATE:
            row["won_after_entering"] = round(won_after[stage] / n, 4)
        row["suppressed"] = n < MIN_FOR_A_RATE
        stages.append(row)

    tracked = len(history)
    return tracked, {
        "analysis": "stage_conversion_and_duration",
        "stage_rows_read_from": read_from,
        "rows_that_did_not_change_the_stage": unchanged,
        "population": {
            "deals_in_the_export": len(outcomes),
            "deals_with_a_stage_history": tracked,
            "history_rows_read": rows,
            "history_rows_with_no_date": undated,
            "history_rows_for_a_deal_not_in_the_export": orphan,
        },
        "stages": stages,
        "where_deals_first_appear": [
            {"stage": s, "count": n, "of": tracked,
             "share": round(n / tracked, 4) if tracked >= MIN_FOR_A_RATE else None}
            for s, n in first_stage.most_common()],
        "most_common_transitions": [
            {"from": a, "to": b, "count": n}
            for (a, b), n in transitions.most_common(12)],
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "No stage order is assumed. Of the deals that ever entered a "
                "stage, how many were won and how long they stayed. Where "
                "deals first appear matters: a pipeline whose deals appear "
                "first in a late stage is a record kept after the fact.",
    }


def cost_per_qualified_opportunity(conn, engagement_id: str, heartbeat):
    """What a qualified opportunity costs, by channel.

    Spend and opportunities come from different systems and different
    people, so the channel names rarely match exactly. They are compared
    loosely — case, spacing and underscores ignored — and every channel that
    appears on only one side is reported. A channel with spend and no
    opportunities is either waste or a naming mismatch, and the client can
    tell which far faster than we can.
    """
    cols = resolve_columns(conn, engagement_id, "cost_per_qualified_opportunity")
    needed = ["opportunity_id", "channel_source", "qualified_at",
              "spend_amount", "spend_channel", "spend_month"]
    problem = missing_fields(conn, engagement_id, "cost_per_qualified_opportunity", needed, cols)
    if problem:
        raise ValueError(problem)

    heartbeat(0, 2)
    spend: dict[tuple[str, str], float] = defaultdict(float)
    spend_rows = spend_undated = spend_unpriced = 0
    for row in rows_of(conn, cols["spend_amount"]):
            spend_rows += 1
            month = a_month(row.get(cols["spend_month"].column_name))
            amount = money(row.get(cols["spend_amount"].column_name))
            channel = same_channel(row.get(cols["spend_channel"].column_name))
            if month is None:
                spend_undated += 1
                continue
            if amount is None:
                spend_unpriced += 1
                continue
            spend[(channel, month)] += amount
    heartbeat(1, 2)

    qualified: Counter = Counter()
    deals = no_qualified_date = no_channel = 0
    for row in rows_of(conn, cols["opportunity_id"]):
            deals += 1
            month = a_month(row.get(cols["qualified_at"].column_name))
            if month is None:
                no_qualified_date += 1
                continue
            channel = same_channel(row.get(cols["channel_source"].column_name))
            if not channel:
                no_channel += 1
                continue
            qualified[(channel, month)] += 1
    heartbeat(2, 2)

    by_channel: dict[str, dict] = defaultdict(lambda: {"spend": 0.0, "qualified": 0})
    for (ch, _m), amount in spend.items():
        by_channel[ch]["spend"] += amount
    for (ch, _m), n in qualified.items():
        by_channel[ch]["qualified"] += n

    channels = []
    for ch, v in sorted(by_channel.items()):
        row = {"channel": ch or "not recorded",
               "spend": round(v["spend"], 2),
               "qualified_opportunities": v["qualified"]}
        if v["qualified"] >= MIN_FOR_A_RATE and v["spend"] > 0:
            row["cost_per_qualified_opportunity"] = round(v["spend"] / v["qualified"], 2)
        else:
            row["suppressed"] = True
        channels.append(row)

    spend_only = sorted({c for c, _ in spend} - {c for c, _ in qualified})
    opps_only = sorted({c for c, _ in qualified} - {c for c, _ in spend})

    total_spend = sum(v["spend"] for v in by_channel.values())
    total_qualified = sum(v["qualified"] for v in by_channel.values())
    return total_qualified, {
        "analysis": "cost_per_qualified_opportunity",
        "population": {
            "spend_rows_read": spend_rows,
            "spend_rows_with_no_month": spend_undated,
            "spend_rows_with_no_amount": spend_unpriced,
            "deals_read": deals,
            "deals_with_no_qualified_date": no_qualified_date,
            "deals_with_no_channel": no_channel,
            "qualified_opportunities": total_qualified,
            "total_spend": round(total_spend, 2),
        },
        "overall_cost_per_qualified_opportunity": (
            round(total_spend / total_qualified, 2)
            if total_qualified >= MIN_FOR_A_RATE and total_spend > 0 else None),
        "by_channel": channels,
        "channels_with_spend_and_no_opportunities": spend_only,
        "channels_with_opportunities_and_no_spend": opps_only,
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "Channel names are compared loosely across the two systems. "
                "A channel appearing on one side only is either waste or a "
                "naming mismatch, and the client will know which.",
    }


def forecast_accuracy(conn, engagement_id: str, heartbeat):
    """Whether the forecast is worth anything.

    Reported signed, not as an absolute error. A team that misses by twenty
    per cent every quarter in the same direction has a correctable bias; one
    that misses by twenty either way has no forecast at all, and the two
    need different conversations.
    """
    cols = resolve_columns(conn, engagement_id, "forecast_accuracy")
    needed = ["forecast_period", "forecast_amount", "forecast_actual_amount"]
    problem = missing_fields(conn, engagement_id, "forecast_accuracy", needed, cols)
    if problem:
        raise ValueError(problem)

    periods: dict[str, dict] = {}
    rows = unusable = 0
    for row in rows_of(conn, cols["forecast_amount"]):
            rows += 1
            period = (row.get(cols["forecast_period"].column_name) or "").strip()
            forecast = money(row.get(cols["forecast_amount"].column_name))
            actual = money(row.get(cols["forecast_actual_amount"].column_name))
            if not period or forecast is None or actual is None or forecast <= 0:
                unusable += 1
                continue
            p = periods.setdefault(period, {"forecast": 0.0, "actual": 0.0,
                                            "submissions": 0})
            p["forecast"] += forecast
            p["actual"] += actual
            p["submissions"] += 1
    heartbeat(1, 1)

    out_periods, errors = [], []
    for period, p in sorted(periods.items()):
        err = (p["actual"] - p["forecast"]) / p["forecast"]
        errors.append(err)
        out_periods.append({
            "period": period, "forecast": round(p["forecast"], 2),
            "actual": round(p["actual"], 2),
            "submissions": p["submissions"],
            "error": round(err, 4),
            "direction": "over-forecast" if err < 0 else "under-forecast",
        })

    n = len(errors)
    bias = round(sum(errors) / n, 4) if n else None
    spread = round(sum(abs(e) for e in errors) / n, 4) if n else None
    return n, {
        "analysis": "forecast_accuracy",
        "population": {
            "rows_read": rows,
            "rows_with_no_period_or_no_figures": unusable,
            "periods": n,
        },
        "periods": out_periods,
        "average_error": bias,
        "average_miss_whichever_way": spread,
        "always_the_same_way": (n >= 3 and all(e < 0 for e in errors))
                               or (n >= 3 and all(e > 0 for e in errors)),
        "periods_too_few_to_judge": n < 3,
        "note": "Error is actual minus forecast over forecast, per period. "
                "A consistent direction is a correctable bias. A miss that "
                "goes either way is not a forecast.",
    }


def buying_group_composition(conn, engagement_id: str, heartbeat):
    """How many people are involved in a deal, and who.

    The number that matters is how many deals have exactly one named
    contact. A single-threaded deal is one resignation away from starting
    again, and a client whose deals are mostly single-threaded has a risk
    they can see in a sentence.
    """
    cols = resolve_columns(conn, engagement_id, "buying_group_composition")
    needed = ["opportunity_id", "opportunity_status", "contact_role",
              "contact_role_opportunity_id", "contact_role_contact_id"]
    problem = missing_fields(conn, engagement_id, "buying_group_composition", needed, cols)
    if problem:
        raise ValueError(problem)

    heartbeat(0, 3)
    outcomes = {}
    for row in rows_of(conn, cols["opportunity_id"]):
            key = (row.get(cols["opportunity_id"].column_name) or "").strip()
            if key:
                outcomes[key] = outcome(row.get(cols["opportunity_status"].column_name))
    heartbeat(1, 3)

    # Contacts, where that export is here, so roles can carry seniority.
    people = {}
    if "contact_id" in cols:
        for row in rows_of(conn, cols["contact_id"]):
                key = (row.get(cols["contact_id"].column_name) or "").strip()
                if key:
                    people[key] = {
                        n: row.get(cols[f].column_name)
                        for n, f in (("seniority", "contact_seniority"),
                                     ("function", "contact_function"),
                                     ("title", "contact_job_title"))
                        if f in cols}
    heartbeat(2, 3)

    per_deal: dict[str, set] = defaultdict(set)
    roles, seniorities, functions = Cut("role"), Cut("seniority"), Cut("function")
    role_rows = orphan = 0
    for row in rows_of(conn, cols["contact_role"]):
            role_rows += 1
            deal = (row.get(cols["contact_role_opportunity_id"].column_name) or "").strip()
            person = (row.get(cols["contact_role_contact_id"].column_name) or "").strip()
            if not deal or deal not in outcomes:
                orphan += 1
                continue
            per_deal[deal].add(person)
            roles.add(row.get(cols["contact_role"].column_name))
            who = people.get(person)
            if who:
                seniorities.add(who.get("seniority"))
                functions.add(who.get("function"))
    heartbeat(3, 3)

    sizes = Counter(len(v) for v in per_deal.values())
    with_roles = len(per_deal)
    decided = sum(1 for o in outcomes.values() if o is not None)
    single = sizes.get(1, 0)

    # Won deals against lost, by how many people were involved.
    by_size: dict[int, dict] = defaultdict(lambda: {"won": 0, "lost": 0})
    for deal, contacts in per_deal.items():
        r = outcomes.get(deal)
        if r:
            by_size[len(contacts)][r] += 1

    out = {
        "analysis": "buying_group_composition",
        "population": {
            "deals_in_the_export": len(outcomes),
            "deals_with_a_named_contact": with_roles,
            "deals_with_nobody_named": len(outcomes) - with_roles,
            "contact_role_rows_read": role_rows,
            "contact_role_rows_for_a_deal_not_in_the_export": orphan,
        },
        "single_threaded": single,
        "single_threaded_share": (round(single / with_roles, 4)
                                  if with_roles >= MIN_FOR_A_RATE else None),
        "people_per_deal": [
            {"people": k, "deals": v, "of": with_roles,
             "share": round(v / with_roles, 4) if with_roles >= MIN_FOR_A_RATE else None}
            for k, v in sorted(sizes.items())],
        "outcome_by_group_size": [
            {"people": k, "won": v["won"], "lost": v["lost"],
             "of": v["won"] + v["lost"],
             "win_rate": (round(v["won"] / (v["won"] + v["lost"]), 4)
                          if v["won"] + v["lost"] >= MIN_FOR_A_RATE else None)}
            for k, v in sorted(by_size.items())],
        "roles": roles.as_dict(role_rows),
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "A deal with one named contact is single-threaded. Deals with "
                "nobody named are counted separately: that is a recording "
                "failure, not a buying group of nought.",
    }
    if people:
        out["seniority"] = seniorities.as_dict(role_rows)
        out["function"] = functions.as_dict(role_rows)
    return with_roles, out


def handover_completeness_census(conn, engagement_id: str, heartbeat):
    """What actually crosses from sales to delivery, on every won deal.

    A census, not a sample. For each closed-won deal, which of the things a
    handover needs are on the record: who bought, who owns the account, what
    was discussed, and whether anything happened after the sale.

    Each is counted on its own. A deal with three of four is not
    three-quarters handed over; it is missing the fourth, and which one is
    missing is the finding.
    """
    cols = resolve_columns(conn, engagement_id, "handover_completeness_census")
    needed = ["opportunity_id", "opportunity_status", "opportunity_account_id",
              "contact_role", "contact_role_opportunity_id",
              "activity_id", "activity_opportunity_id", "activity_type"]
    problem = missing_fields(conn, engagement_id, "handover_completeness_census", needed, cols)
    if problem:
        raise ValueError(problem)

    seen = Outcomes()
    heartbeat(0, 4)
    won_deals: dict[str, str] = {}
    for row in rows_of(conn, cols["opportunity_id"]):
            if seen.classify(row.get(cols["opportunity_status"].column_name)) != "won":
                continue
            key = (row.get(cols["opportunity_id"].column_name) or "").strip()
            if key:
                won_deals[key] = (row.get(cols["opportunity_account_id"].column_name) or "").strip()
    heartbeat(1, 4)

    seen.refuse_if_nothing_matched(cols["opportunity_status"].column_name)
    has_role = set()
    for row in rows_of(conn, cols["contact_role"]):
            deal = (row.get(cols["contact_role_opportunity_id"].column_name) or "").strip()
            if deal in won_deals and (row.get(cols["contact_role"].column_name) or "").strip():
                has_role.add(deal)
    heartbeat(2, 4)

    has_activity = set()
    activity_types = Cut("activity type")
    for row in rows_of(conn, cols["activity_id"]):
            deal = (row.get(cols["activity_opportunity_id"].column_name) or "").strip()
            if deal in won_deals:
                has_activity.add(deal)
                activity_types.add(row.get(cols["activity_type"].column_name))
    heartbeat(3, 4)

    has_note = set()
    if "note_opportunity_id" in cols:
        for row in rows_of(conn, cols["note_opportunity_id"]):
                deal = (row.get(cols["note_opportunity_id"].column_name) or "").strip()
                if deal in won_deals:
                    has_note.add(deal)

    has_owner = set()
    if "account_owner" in cols and "account_id" in cols:
        owners = {}
        for row in rows_of(conn, cols["account_id"]):
                key = (row.get(cols["account_id"].column_name) or "").strip()
                val = (row.get(cols["account_owner"].column_name) or "").strip()
                if key and val and not is_a_non_answer(val):
                    owners[key] = val
        has_owner = {d for d, acct in won_deals.items() if acct in owners}
    heartbeat(4, 4)

    n = len(won_deals)
    checks = [("somebody named on the deal", has_role),
              ("something recorded against it", has_activity)]
    if "note_opportunity_id" in cols:
        checks.append(("a written record of the conversation", has_note))
    if has_owner or ("account_owner" in cols and "account_id" in cols):
        checks.append(("an owner on the account", has_owner))

    present = []
    for label, found in checks:
        present.append({
            "what": label, "deals_with_it": len(found), "of": n,
            "share": round(len(found) / n, 4) if n >= MIN_FOR_A_RATE else None,
        })
    complete = set(won_deals) 
    for _label, found in checks:
        complete &= found
    nothing = set(won_deals)
    for _label, found in checks:
        nothing -= found

    return n, {
        "analysis": "handover_completeness_census",
        "population": {"closed_won": n, "checks_made": len(checks)},
        **seen.as_dict(),
        "each_part": present,
        "deals_with_every_part": len(complete),
        "deals_with_every_part_share": (round(len(complete) / n, 4)
                                        if n >= MIN_FOR_A_RATE else None),
        "deals_with_no_part_at_all": len(nothing),
        "activity_types": activity_types.as_dict(len(has_activity)),
        "parts_not_checked_because_the_field_is_not_mapped": [
            label for label, field in
            (("a written record of the conversation", "note_opportunity_id"),
             ("an owner on the account", "account_owner"))
            if field not in cols],
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "A census of every closed-won deal. Each part is counted on "
                "its own: a deal with three of four is missing the fourth, "
                "and which one is missing is the finding.",
    }


def expansion_vs_new_business(conn, engagement_id: str, heartbeat):
    """How much of the growth comes from customers they already had.

    Deals are classified by the type the client records, not inferred. Where
    the type is missing or is a non-answer the deal is counted as
    unclassified, not assumed to be new business — which is the assumption
    that flatters a land-and-expand story.

    Value comes from the subscriptions, where the client records what a
    customer is actually worth and what was added. A deal amount is what was
    signed; annual recurring revenue is what was kept.
    """
    cols = resolve_columns(conn, engagement_id, "expansion_vs_new_business")
    needed = ["opportunity_id", "opportunity_status", "opportunity_type",
              "opportunity_account_id"]
    problem = missing_fields(conn, engagement_id, "expansion_vs_new_business", needed, cols)
    if problem:
        raise ValueError(problem)

    types = Cut("opportunity type")
    won = unclassified = read = 0
    by_type: Counter = Counter()
    accounts_won: dict[str, set] = defaultdict(set)

    seen = Outcomes()
    for row in rows_of(conn, cols["opportunity_id"]):
            read += 1
            if seen.classify(row.get(cols["opportunity_status"].column_name)) != "won":
                continue
            won += 1
            raw = row.get(cols["opportunity_type"].column_name)
            types.add(raw)
            if raw is None or str(raw).strip() == "" or is_a_non_answer(raw):
                unclassified += 1
                label = "not classified"
            else:
                label = str(raw).strip()
            by_type[label] += 1
            acct = (row.get(cols["opportunity_account_id"].column_name) or "").strip()
            if acct:
                accounts_won[label].add(acct)
    heartbeat(1, 2)

    seen.refuse_if_nothing_matched(cols["opportunity_status"].column_name)
    # What those customers are worth, from the subscriptions.
    recurring = expansion = 0.0
    subs = subs_unpriced = 0
    if "subscription_id" in cols and "arr_amount" in cols:
        for row in rows_of(conn, cols["subscription_id"]):
                subs += 1
                arr = money(row.get(cols["arr_amount"].column_name))
                if arr is None:
                    subs_unpriced += 1
                else:
                    recurring += arr
                if "expansion_amount" in cols:
                    add = money(row.get(cols["expansion_amount"].column_name))
                    if add is not None:
                        expansion += add
    heartbeat(2, 2)

    return won, {
        "analysis": "expansion_vs_new_business",
        "population": {
            "deals_read": read, "closed_won": won,
            "not_classified": unclassified,
            "subscriptions_read": subs,
            "subscriptions_with_no_recurring_revenue": subs_unpriced,
        },
        **seen.as_dict(),
        "won_by_type": types.as_dict(won),
        "accounts_by_type": [{"type": k, "accounts": len(v)}
                             for k, v in sorted(accounts_won.items())],
        "recurring_revenue": round(recurring, 2) if subs else None,
        "from_expansion": round(expansion, 2) if subs else None,
        "expansion_share_of_recurring_revenue": (
            round(expansion / recurring, 4)
            if subs and recurring > 0 else None),
        "value_not_measured_because_the_field_is_not_mapped":
            "arr_amount" not in cols,
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "Deals are classified by the type the client records. A deal "
                "with no type is counted as unclassified, not as new "
                "business. Value comes from recurring revenue, not deal "
                "amounts.",
    }


def renewal_timing_and_reason(conn, engagement_id: str, heartbeat):
    """Whether renewals are run, or just happen.

    How far ahead of the contract end a renewal was settled is the number
    that separates a renewal process from a renewal date. A renewal recorded
    on or after the end date was not managed, whatever its outcome.
    """
    cols = resolve_columns(conn, engagement_id, "renewal_timing_and_reason")
    needed = ["subscription_id", "renewal_date", "renewal_outcome"]
    problem = missing_fields(conn, engagement_id, "renewal_timing_and_reason", needed, cols)
    if problem:
        raise ValueError(problem)

    outcomes, reasons = Cut("renewal outcome"), Cut("reason for not renewing")
    lead_days: list[int] = []
    rows = no_dates = not_renewed = 0
    end_col = (cols["contract_end_date"].column_name
               if "contract_end_date" in cols else None)

    for row in rows_of(conn, cols["subscription_id"]):
            rows += 1
            result = row.get(cols["renewal_outcome"].column_name)
            outcomes.add(result)
            # A reason for not renewing only applies where it did not renew.
            # Counting a blank on a renewed subscription as a missing reason
            # made nine in ten renewals look like a recording failure.
            renewed = "renew" in str(result or "").lower() \
                and "not" not in str(result or "").lower() \
                and "non" not in str(result or "").lower()
            if "non_renewal_reason" in cols and not renewed:
                not_renewed += 1
                reasons.add(row.get(cols["non_renewal_reason"].column_name))
            if end_col:
                settled = a_date(row.get(cols["renewal_date"].column_name))
                ends = a_date(row.get(end_col))
                if settled and ends:
                    lead_days.append((ends - settled).days)
                else:
                    no_dates += 1
    heartbeat(1, 1)

    timing = None
    if len(lead_days) >= MIN_FOR_A_RATE:
        v = sorted(lead_days)
        def at(q): return v[min(int(q * len(v)), len(v) - 1)]
        timing = {
            "count": len(v),
            "days_ahead_middle": at(0.50),
            "days_ahead_earliest_tenth": at(0.90),
            "settled_on_or_after_the_end_date": sum(1 for x in v if x <= 0),
            "settled_on_or_after_the_end_date_share":
                round(sum(1 for x in v if x <= 0) / len(v), 4),
        }

    out = {
        "analysis": "renewal_timing_and_reason",
        "population": {"subscriptions_read": rows,
                       "with_no_usable_dates": no_dates},
        "outcomes": outcomes.as_dict(rows),
        "timing": timing,
        "timing_not_measured_because_the_field_is_not_mapped":
            end_col is None,
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "How far ahead of the contract end each renewal was settled. "
                "A renewal settled on or after the end date was not managed, "
                "whatever its outcome.",
    }
    if "non_renewal_reason" in cols:
        out["reasons_for_not_renewing"] = reasons.as_dict(not_renewed)
        out["did_not_renew"] = not_renewed
        out["non_renewals_with_no_usable_reason"] = reasons.not_answered
        out["non_renewals_with_no_usable_reason_share"] = (
            round(reasons.not_answered / not_renewed, 4)
            if not_renewed >= MIN_FOR_A_RATE else None)
    return rows, out


def a_moment(value):
    """A date and time, or None. Response time needs the hours."""
    t = (str(value) if value is not None else "").strip()
    if not t:
        return None
    t = t.replace("T", " ").split("+")[0].split(".")[0].strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
                "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            return datetime.strptime(t, fmt)
        except ValueError:
            continue
    return None


def signal_to_first_contact(conn, engagement_id: str, heartbeat):
    """When somebody raised a hand, how long before a person answered.

    The signal is a form submission. Not a record being created: a bought
    list, a ZoomInfo import and a scraped contact all create records, and
    none of them is a buyer raising a hand. Measuring from record creation
    measures the import.

    Meetings in the activity log are counted but not treated as signals.
    A meeting the prospect booked through a scheduling link is as inbound
    as a form; one a rep booked is not, and nothing in an activity export
    distinguishes them.

    The response is the first activity of any kind against the same person
    after the signal. Direction is not needed and mostly is not there:
    both CRMs carry a direction field, both populate it only when the
    activity happened inside the tool, and reps logging by hand are offered
    the choice rather than made to take it.
    """
    cols = resolve_columns(conn, engagement_id, "signal_to_first_contact")
    needed = ["contact_id", "activity_id", "activity_contact_id", "activity_at"]
    problem = missing_fields(conn, engagement_id, "signal_to_first_contact", needed, cols)
    if problem:
        raise ValueError(problem)
    if "form_submission_at" not in cols or "form_submission_contact_id" not in cols:
        raise ValueError(
            "there is no record of anyone raising a hand: no form submissions "
            "are mapped. Response time cannot be measured without a signal, "
            "and that is a finding rather than a gap")

    # Everyone in the CRM, so the ones who never signalled can be counted.
    heartbeat(0, 3)
    people: set[str] = set()
    for row in rows_of(conn, cols["contact_id"]):
            key = (row.get(cols["contact_id"].column_name) or "").strip()
            if key:
                people.add(key)
    leads = 0
    if "lead_id" in cols:
        for row in rows_of(conn, cols["lead_id"]):
                key = (row.get(cols["lead_id"].column_name) or "").strip()
                if key:
                    people.add(key)
                    leads += 1
    heartbeat(1, 3)

    # The signals.
    first_signal: dict[str, object] = {}
    submissions = undated_signals = 0
    for row in rows_of(conn, cols["form_submission_at"]):
            submissions += 1
            who = (row.get(cols["form_submission_contact_id"].column_name) or "").strip()
            when = a_moment(row.get(cols["form_submission_at"].column_name))
            if not who:
                continue
            if when is None:
                undated_signals += 1
                continue
            if who not in first_signal or when < first_signal[who]:
                first_signal[who] = when
    heartbeat(2, 3)

    # The responses: any activity against that person, after the signal.
    first_reply: dict[str, object] = {}
    activities = meetings = 0
    meeting_col = cols["meeting_at"].column_name if "meeting_at" in cols else None
    for row in rows_of(conn, cols["activity_at"]):
            activities += 1
            if meeting_col and (row.get(meeting_col) or "").strip():
                meetings += 1
            who = (row.get(cols["activity_contact_id"].column_name) or "").strip()
            if who not in first_signal:
                continue
            when = a_moment(row.get(cols["activity_at"].column_name))
            if when is None or when < first_signal[who]:
                continue
            if who not in first_reply or when < first_reply[who]:
                first_reply[who] = when
    heartbeat(3, 3)

    hours = sorted((first_reply[w] - first_signal[w]).total_seconds() / 3600
                   for w in first_reply)
    signalled = len(first_signal)
    never_answered = signalled - len(hours)

    def within(h):
        return sum(1 for x in hours if x <= h)

    bands = None
    if signalled >= MIN_FOR_A_RATE:
        bands = [{"answered": "within " + label, "people": within(h),
                  "of": signalled, "share": round(within(h) / signalled, 4)}
                 for label, h in (("an hour", 1), ("a working day", 8),
                                  ("a day", 24), ("three days", 72),
                                  ("a week", 168))]
        bands.append({"answered": "not at all", "people": never_answered,
                      "of": signalled,
                      "share": round(never_answered / signalled, 4)})

    speed = None
    if len(hours) >= MIN_FOR_A_RATE:
        def at(q): return round(hours[min(int(q * len(hours)), len(hours) - 1)], 2)
        speed = {"answered": len(hours), "quickest_tenth_hours": at(0.10),
                 "middle_hours": at(0.50), "slowest_tenth_hours": at(0.90)}

    # Only people we can actually see in the CRM. A form submission from
    # somebody who is not in the contact or lead export is its own small
    # finding, counted below rather than swelling this one.
    signalled_and_known = {w for w in first_signal if w in people}
    never_signalled = len(people) - len(signalled_and_known)
    signals_from_nobody_on_file = signalled - len(signalled_and_known)
    return signalled, {
        "analysis": "signal_to_first_contact",
        "population": {
            "people_in_the_crm": len(people),
            "of_which_unconverted_leads": leads if "lead_id" in cols else None,
            "form_submissions_read": submissions,
            "form_submissions_with_no_date": undated_signals,
            "people_who_signalled": signalled,
            "activities_read": activities,
        },
        "answered_within": bands,
        "speed": speed,
        "never_answered": never_answered,
        # The other finding, and on most CRMs the louder one.
        "people_who_never_signalled": never_signalled,
        "people_who_never_signalled_share": (
            round(never_signalled / len(people), 4)
            if len(people) >= MIN_FOR_A_RATE else None),
        "signals_from_somebody_not_in_the_crm": signals_from_nobody_on_file,
        "meetings_recorded": meetings if meeting_col else None,
        "leads_not_counted_because_the_export_is_not_mapped":
            "lead_id" not in cols,
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "The signal is a form submission. A record being created is "
                "not a signal: imported lists create records and raise no "
                "hands. People in the CRM who never submitted a form are "
                "counted separately, and on most databases they are most of "
                "it — which is what every per-contact rate the client quotes "
                "is computed against.",
    }


def working_days_between(a, b) -> int:
    """Working days, because two days over a weekend is not two days late."""
    if a is None or b is None:
        return 0
    step = 1 if b >= a else -1
    days, cur = 0, a
    while cur != b:
        cur += timedelta(days=step)
        if cur.weekday() < 5:
            days += step
    return days


# A note of three words is a note in the export and nothing in practice.
# "Called." and "n/a" are the commonest, and counting them as coverage is
# how a client comes to believe their notes are good.
SUBSTANTIVE_WORDS = 10


def note_sufficiency(conn, engagement_id: str, heartbeat):
    """Whether the client's notes can carry an assessment at all.

    Four conditions, and the verdict is computed from all four. Three of
    them are counted: how many closed opportunities carry a substantive
    note, separately for won and lost because notes cluster on deals that
    went well, and how many notes were written within two working days of
    the meeting they describe.

    The fourth is read. A sample of thirty notes across at least three
    authors is labelled for what the buyer actually said — decision
    process, timeline, success criteria, agreed next step — and for whether
    the note follows any recognisable structure.

    The model never returns the verdict or the rates. It labels, and the
    arithmetic happens here where it can be checked. A near miss is a fail.
    """
    cols = resolve_columns(conn, engagement_id, "note_sufficiency")
    needed = ["note_id", "note_body", "note_author", "note_created_at",
              "note_opportunity_id", "meeting_at"]
    problem = missing_fields(conn, engagement_id, "note_sufficiency", needed, cols)
    if problem:
        raise ValueError(problem)

    model.check_configured()

    # Which opportunities closed, and which way.
    seen = Outcomes()
    heartbeat(0, 3)
    closed: dict[str, str] = {}
    if "opportunity_id" in cols and "opportunity_status" in cols:
        for row in rows_of(conn, cols["opportunity_id"]):
                result = seen.classify(row.get(cols["opportunity_status"].column_name))
                key = (row.get(cols["opportunity_id"].column_name) or "").strip()
                if key and result:
                    closed[key] = result
    seen.refuse_if_nothing_matched(cols["opportunity_status"].column_name)
    heartbeat(1, 3)

    notes: list[dict] = []
    with_note: dict[str, set] = {"won": set(), "lost": set()}
    by_author: dict[str, list[dict]] = defaultdict(list)
    read = thin = 0
    on_time = datable = 0

    for row in rows_of(conn, cols["note_body"]):
            read += 1
            body = (row.get(cols["note_body"].column_name) or "").strip()
            if len(body.split()) < SUBSTANTIVE_WORDS:
                thin += 1
                continue
            nid = (row.get(cols["note_id"].column_name) or "").strip()
            author = (row.get(cols["note_author"].column_name) or "").strip()
            deal = (row.get(cols["note_opportunity_id"].column_name) or "").strip()
            written = a_date(row.get(cols["note_created_at"].column_name))
            met = a_date(row.get(cols["meeting_at"].column_name))

            if deal in closed:
                with_note[closed[deal]].add(deal)
            if written and met:
                datable += 1
                if 0 <= working_days_between(met, written) <= 2:
                    on_time += 1
            if nid and body:
                note = {"id": nid, "text": body, "author": author or "unnamed"}
                notes.append(note)
                by_author[note["author"]].append(note)
    heartbeat(2, 3)

    won_total = sum(1 for v in closed.values() if v == "won")
    lost_total = sum(1 for v in closed.values() if v == "lost")

    def rate(part, whole):
        return round(part / whole, 4) if whole >= MIN_FOR_A_RATE else None

    coverage_won = rate(len(with_note["won"]), won_total)
    coverage_lost = rate(len(with_note["lost"]), lost_total)
    contemporaneity = rate(on_time, datable)

    # Thirty notes across at least three authors. Spread across authors
    # deliberately: thirty notes from the one person who writes properly
    # would describe that person, not the client.
    sample: list[dict] = []
    authors = sorted(by_author, key=lambda a: -len(by_author[a]))
    per_author = max(1, 30 // max(len(authors), 1))
    for a in authors:
        sample.extend(by_author[a][:per_author])
    # Top up from the authors who write most, so a client with four authors
    # gets thirty notes rather than twenty-eight.
    for a in authors:
        if len(sample) >= 30:
            break
        sample.extend(by_author[a][per_author:per_author + (30 - len(sample))])
    sample = sample[:30]

    LABELS = ["decision_process", "timeline", "success_criteria",
              "next_step", "follows_a_structure"]
    labelled, unread = ({}, [])
    if sample:
        labelled, unread = model.classify(
            sample,
            "These are notes written by sales people after customer "
            "conversations. For each, answer whether the note records the "
            "buyer's own stated position on their decision process, their "
            "timeline, their success criteria, and an agreed next step; and "
            "whether the note follows a recognisable structure such as "
            "headings or consistent sections rather than free prose.",
            LABELS, heartbeat=None)
    heartbeat(3, 3)

    n = len(labelled)
    content = {l: (round(sum(1 for v in labelled.values()
                            if v["labels"][l] == "yes") / n, 4)
                   if n >= MIN_FOR_A_RATE else None)
               for l in LABELS[:4]}

    # Structure is consistent when most notes have one and the authors agree.
    # Computed, not asked for.
    author_of = {x["id"]: x["author"] for x in sample}
    structured: dict[str, list] = defaultdict(list)
    for rid, v in labelled.items():
        structured[author_of[rid]].append(v)
    by_author_rate = {
        a: sum(1 for v in vs if v["labels"]["follows_a_structure"] == "yes") / len(vs)
        for a, vs in structured.items() if len(vs) >= 3}
    overall_structure = (round(sum(1 for v in labelled.values()
                                   if v["labels"]["follows_a_structure"] == "yes") / n, 4)
                         if n >= MIN_FOR_A_RATE else None)
    structure_consistent = (
        overall_structure is not None and overall_structure >= 0.70
        and len(by_author_rate) >= 2
        and (max(by_author_rate.values()) - min(by_author_rate.values())) <= 0.30)

    failed = []
    if coverage_won is None or coverage_won < 0.90:
        failed.append("coverage of closed-won opportunities")
    if not structure_consistent:
        failed.append("consistent structure across authors")
    for l, v in content.items():
        if v is None or v < 0.70:
            failed.append(l.replace("_", " "))
    if contemporaneity is None or contemporaneity < 0.80:
        failed.append("notes written within two working days")

    return read, {
        "analysis": "note_sufficiency",
        "population": {
            "notes_read": read,
            "notes_too_thin_to_count": thin,
            "closed_won": won_total, "closed_lost": lost_total,
            "notes_datable_against_a_meeting": datable,
        },
        "coverage_won": coverage_won,
        "coverage_won_of": won_total,
        "coverage_lost": coverage_lost,
        "coverage_lost_of": lost_total,
        "contemporaneity": contemporaneity,
        "contemporaneity_of": datable,
        "content_rates": content,
        "structure_rate": overall_structure,
        "structure_by_author": {f"Author {i}": round(v, 4)
                                for i, v in enumerate(by_author_rate.values(), 1)},
        "structure_consistent": structure_consistent,
        "sample": {"size": len(sample), "authors": len(by_author_rate),
                   "labelled": n, "unread": len(unread)},
        "verdict": "PASS" if not failed else "FAIL",
        "failed_on": failed,
        "model": model.MODEL,
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "The verdict is computed from all four conditions. A near "
                "miss is a fail. Notes under ten words are counted as too "
                "thin rather than as coverage: a note saying Called. is a "
                "note in the export and nothing in practice.",
    }


def discovery_consistency_classification(conn, engagement_id: str, heartbeat):
    """What the discovery conversations actually recorded, on every deal.

    A census rather than a sample, because the finding is a distribution
    and a sample of thirty cannot be cut by rep. Every record is read, and
    whether every record was read is reported: an analysis that quietly
    skipped a fifth of the notes would describe four fifths of the client.

    Five questions per record, each answered only where the record states
    it. A rep's assumption, an implication, or the model's own inference is
    a no, and the span of text behind each answer comes back with it so a
    person can check ten of them at step 2.4.

    Reps are named Rep 1, Rep 2 and so on. The finding is about the
    practice, not about who is worst at it, and a report that names people
    stops being read as an assessment.
    """
    cols = resolve_columns(conn, engagement_id,
                           "discovery_consistency_classification")
    needed = ["note_id", "note_body", "note_author", "note_created_at",
              "note_opportunity_id", "activity_owner"]
    problem = missing_fields(conn, engagement_id, "discovery_consistency_classification", needed, cols)
    if problem:
        raise ValueError(problem)

    model.check_configured()

    records: list[dict] = []
    owner_of: dict[str, str] = {}
    read = thin = 0
    for row in rows_of(conn, cols["note_body"]):
            read += 1
            body = (row.get(cols["note_body"].column_name) or "").strip()
            nid = (row.get(cols["note_id"].column_name) or "").strip()
            if not nid or len(body.split()) < SUBSTANTIVE_WORDS:
                thin += 1
                continue
            records.append({"id": nid, "text": body})
            owner_of[nid] = (row.get(cols["note_author"].column_name) or "").strip() \
                or "unnamed"

    # The questions as GEA-P13 asks them. A screen deriving "Does this
    # record show decision process?" from a key is asking a different and
    # vaguer question than the model was asked, and the person answering it
    # is not checking the same thing.
    QUESTIONS = {
        "decision_process": "Does the record state the buyer's own decision "
                            "process?",
        "timeline": "Does the record state the buyer's timeline?",
        "success_criteria": "Does the record state the buyer's own success "
                            "criteria?",
        "next_step": "Does the record state an agreed next step?",
        "others_involved": "Does the record state who else is involved in "
                           "the decision?",
    }
    LABELS = list(QUESTIONS)
    labelled, unread = model.classify(
        records,
        "These are records of discovery conversations written by sales "
        "people. For each, answer whether the record states the buyer's own "
        "decision process, the buyer's timeline, the buyer's success "
        "criteria, an agreed next step, and who else is involved in the "
        "decision. Answer yes only where the record says it. An assumption "
        "by the writer, an implication, or your own inference is a no.",
        LABELS, heartbeat=heartbeat)

    n = len(labelled)
    rates = {l: (round(sum(1 for v in labelled.values()
                           if v["labels"][l] == "yes") / n, 4)
                 if n >= MIN_FOR_A_RATE else None) for l in LABELS}

    all_five = sum(1 for v in labelled.values()
                   if all(v["labels"][l] == "yes" for l in LABELS))
    none_of_five = sum(1 for v in labelled.values()
                       if all(v["labels"][l] == "no" for l in LABELS))

    # By rep, anonymised, and only where there are enough records to say
    # anything. A rep with four notes is not a pattern.
    per_rep: dict[str, list] = defaultdict(list)
    for rid, v in labelled.items():
        per_rep[owner_of[rid]].append(v)
    reps = []
    for i, (_name, vs) in enumerate(
            sorted(per_rep.items(), key=lambda x: -len(x[1])), 1):
        if len(vs) < MIN_FOR_A_RATE:
            continue
        reps.append({"rep": f"Rep {i}", "records": len(vs),
                     **{l: round(sum(1 for v in vs if v["labels"][l] == "yes")
                                 / len(vs), 4) for l in LABELS}})

    # The ten a person reads at step 2.4. Spread across the distribution
    # rather than taken off the top, so the check covers the easy records
    # and the hard ones.
    by_score = sorted(labelled.items(),
                      key=lambda kv: sum(1 for l in LABELS
                                         if kv[1]["labels"][l] == "yes"))
    step = max(1, len(by_score) // 10)
    # The record's own text travels with it. Without it a person checking
    # the model can only read the span the model chose to show them, which
    # is not a check at all: they would be agreeing that the model's own
    # evidence supports the model's own answer. Ten records of client text,
    # held under the same rule as everything else raw.
    text_of = {r["id"]: r["text"] for r in records}
    to_check = [{"record_id": rid,
                 "record": text_of.get(rid, "")[:4000],
                 "labels": v["labels"], "evidence": v["evidence"],
                 "confidence": v["confidence"]}
                for rid, v in by_score[::step][:10]]

    census_complete = not unread and bool(records)
    return n, {
        "analysis": "discovery_consistency_classification",
        "population": {
            "records_read": read,
            "records_too_thin_to_classify": thin,
            "records_offered_to_the_model": len(records),
            "records_classified": n,
            "records_the_model_did_not_return": len(unread),
        },
        "census_complete": census_complete,
        "rates": rates,
        "recorded_all_five": all_five,
        "recorded_all_five_share": (round(all_five / n, 4)
                                    if n >= MIN_FOR_A_RATE else None),
        "recorded_none_of_the_five": none_of_five,
        "recorded_none_of_the_five_share": (round(none_of_five / n, 4)
                                            if n >= MIN_FOR_A_RATE else None),
        "by_rep": reps,
        "reps_too_few_records_to_report": sum(
            1 for vs in per_rep.values() if len(vs) < MIN_FOR_A_RATE),
        "to_check_by_hand": to_check,
        "questions": QUESTIONS,
        "low_confidence": sum(1 for v in labelled.values()
                              if v["confidence"] == "low"),
        "model": model.MODEL,
        "rates_suppressed_below": MIN_FOR_A_RATE,
        "note": "A census: every substantive record was offered to the "
                "model. Where census_complete is false some records came "
                "back unreadable and the rates describe only the rest. The "
                "ten records listed for checking are spread across the "
                "distribution, and reading them is what makes the rest "
                "trustworthy.",
    }


def adversarial_score_review(conn, engagement_id: str, heartbeat):
    """Argue against every score before the client sees it.

    This one reads no client export. It reads the scorecard Blend has just
    written, which makes it the only place in the assessment where the work
    is turned on itself.

    The two failures worth catching are named in GEA-P17: a four or five
    awarded on evidence that does not meet the anchor, and a zero or one
    that confuses an absence of the thing with an absence of evidence about
    the thing. The second is the one that gets a client angry, because it
    reads as an accusation when it is a gap in what they record.

    Nothing it produces is binding. Each challenge goes to a person, who
    defends it with a citation or concedes and moves the score.
    """
    model.check_configured()

    with conn.cursor() as cur:
        cur.execute("select * from gea_scorecard_for_review(%s)",
                    (engagement_id,))
        cols = [d.name for d in cur.description]
        scored = [dict(zip(cols, row)) for row in cur.fetchall()]
    conn.rollback()

    if not scored:
        raise ValueError(
            "nothing has been scored yet, so there is nothing to argue with")

    # One element per record, so the review argues with a score rather than
    # summarising a scorecard. Batched by the model layer.
    records = []
    for e in scored:
        reads = ("not evidenced" if e["not_evidenced"]
                 else str(e["value"]))
        cites = "; ".join(
            f"{c.get('what')}{': ' + c['note'] if c.get('note') else ''}"
            for c in (e["citations"] or [])) or "nothing cited"
        records.append({
            "id": e["element_ref"],
            "text": (
                f"ELEMENT {e['element_ref']}: {e['element_name']}\n"
                f"WHY IT IS ASSESSED: {e['why_assessed'] or ''}\n"
                f"SCORE GIVEN: {reads}\n"
                + (f"REASON RECORDED FOR NOT EVIDENCED: {e['ne_reason']}\n"
                   if e["not_evidenced"] and e["ne_reason"] else "")
                + (f"THE ANCHOR THIS SCORE CLAIMS: {e['anchor_claimed']}\n"
                   if e["anchor_claimed"] else "")
                + (f"THE ANCHOR BELOW IT: {e['anchor_below']}\n"
                   if e["anchor_below"] else "")
                + (f"THE ANCHOR ABOVE IT: {e['anchor_above']}\n"
                   if e["anchor_above"] else "")
                + (f"CAPPED AT: {e['cap_applied']}\n"
                   if e["cap_applied"] is not None else "")
                + (f"CONFIDENCE FLAGGED: {e['confidence']}\n"
                   if e["confidence"] else "")
                + f"WHAT THE SCORER WROTE: {e['scoring_note'] or 'nothing'}\n"
                + f"WHAT IT RESTS ON: {cites}"),
        })

    LABELS = ["evidence_does_not_meet_the_anchor",
              "absence_of_evidence_read_as_absence_of_the_thing",
              "citation_too_vague_to_retrieve",
              "should_be_a_limitation_not_a_score"]
    labelled, unread = model.classify(
        records,
        "Each record is one element of a completed assessment, with the "
        "score given, the anchor that score claims, the anchors either side "
        "of it, and what the score rests on.\n\n"
        "Argue against the score. Do not confirm it. Do not suggest wording "
        "for a report.\n\n"
        "Answer yes where the fault is present:\n"
        "- evidence_does_not_meet_the_anchor: what the score rests on does "
        "not satisfy what the claimed anchor requires.\n"
        "- absence_of_evidence_read_as_absence_of_the_thing: a low score "
        "treats missing records as proof the practice is missing. These are "
        "different, and only the first is evidenced.\n"
        "- citation_too_vague_to_retrieve: a colleague could not find the "
        "same evidence from what is cited.\n"
        "- should_be_a_limitation_not_a_score: confidence is low enough "
        "that publishing a number would overstate what is known.\n\n"
        "Your evidence quotation must come from the record, and it must be "
        "the part that shows the fault.",
        LABELS, heartbeat=heartbeat)

    challenges = []
    for ref, v in labelled.items():
        faults = [l for l in LABELS if v["labels"][l] == "yes"]
        if not faults:
            continue
        e = next(x for x in scored if x["element_ref"] == ref)
        reads = "not evidenced" if e["not_evidenced"] else str(e["value"])
        for fault in faults:
            challenges.append({
                "element_ref": ref,
                "weakness_type": fault,
                "challenge": (
                    f"{fault.replace('_', ' ').capitalize()}. "
                    f"{e['element_name']} reads {reads} and rests on "
                    f"{'nothing cited' if not e['citations'] else 'what is cited'}. "
                    f"The review points at: {v['evidence']}"),
                "confidence": v["confidence"],
            })

    unchallenged = len(scored) - len({c["element_ref"] for c in challenges})
    return len(challenges), {
        "analysis": "adversarial_score_review",
        "population": {
            "elements_scored": len(scored),
            "elements_reviewed": len(labelled),
            "elements_the_model_did_not_return": len(unread),
            "elements_with_nothing_argued": unchallenged,
        },
        "challenges": challenges,
        "review_complete": not unread,
        "by_fault": {l: sum(1 for c in challenges if c["weakness_type"] == l)
                     for l in LABELS},
        "model": model.MODEL,
        "note": "Nothing here is binding. Each challenge goes to a person "
                "who defends the score with its citation or concedes and "
                "changes it. An element with nothing argued is not thereby "
                "confirmed: it is an element this review found no fault in.",
    }


def synthesise_interview(conn, engagement_id: str, heartbeat, interview_id=None):
    """Read a conversation against the questions it was meant to cover.

    Forty minutes of talk, twenty to fifty questions, and a consultant who
    needs to find the part that bears on element 5.3. That is the whole job.

    It does not propose a score. Nothing here says what an element is worth,
    and the elements an interview serves are scored by a person who read it.
    The synthesis exists so they can find what they are looking for.

    Three outputs, and the second two are the ones worth having:

      What was said, per question, in the words that were said. A claim
      without the words is the model's summary, and a summary cannot be
      cited.

      What was never discussed. A guide has questions for a reason, and a
      question nobody asked is a gap in the evidence rather than a silence
      about the client. Consultants run out of time and skip the last ten
      minutes; this is what notices.

      Where the answer did not answer. Not a claim that somebody was hiding
      something — that is a judgement nobody should delegate — but a flag
      that the words given did not address what was asked, so a person can
      decide for themselves.
    """
    model.check_configured()

    with conn.cursor() as cur:
        cur.execute("""
            select i.id, i.interviewee_name, i.interviewee_role,
                   g.ref, g.function_name, a.storage_path, a.original_filename
              from interview i
              join interview_guide g on g.id = i.interview_guide_id
              left join artefact a on a.id = i.transcript_artefact_id
                                  and a.purged_at is null
             where i.engagement_id = %s
               and (%s::uuid is null or i.id = %s::uuid)
               and i.status = 'held'
               and i.synthesis_run_id is null
             order by i.held_at nulls last
             limit 1
        """, (engagement_id, interview_id, interview_id))
        row = cur.fetchone()
    conn.rollback()

    if row is None:
        raise ValueError(
            "there is no interview waiting to be read. One has to be marked "
            "held before its transcript can be synthesised")
    iid, who, role, guide_ref, function_name, path, filename = row
    if path is None:
        raise ValueError(
            f"the {function_name} interview has no transcript attached, so "
            f"there is nothing to read. Attach it and try again")

    with conn.cursor() as cur:
        cur.execute("select * from gea_interview_guide_questions(%s)", (iid,))
        questions = [{"id": r[0], "section": r[1], "text": r[2], "elements": r[3]}
                     for r in cur.fetchall()]
    conn.rollback()

    with storage.download(path).open("r", encoding="utf-8",
                                     errors="replace") as fh:
        transcript = fh.read()
    if len(transcript.split()) < 200:
        raise ValueError(
            f"that transcript is {len(transcript.split())} words, which is "
            f"too short to be a conversation. Check the right file is "
            f"attached")

    # The transcript goes with every batch of questions. It is the thing
    # being read, and a question judged against part of a conversation is
    # judged against the wrong thing.
    LABELS = ["was_asked", "was_answered", "the_answer_addressed_the_question"]
    records = [{"id": q["id"],
                "text": (f"QUESTION: {q['text']}\n"
                         + (f"SECTION: {q['section']}\n" if q["section"] else "")
                         + "\n--- THE TRANSCRIPT ---\n" + transcript[:60000])}
               for q in questions]

    labelled, unread = model.classify(
        records,
        f"Each record is one question from the {function_name} interview "
        f"guide, followed by the transcript of that conversation with "
        f"{who}{', ' + role if role else ''}.\n\n"
        "For each question, answer:\n"
        "- was_asked: the question, or something close enough to it, was put "
        "to the interviewee.\n"
        "- was_answered: they said something in response.\n"
        "- the_answer_addressed_the_question: what they said actually bears "
        "on what was asked. Answer no where they talked about something "
        "else, gave a generality, or deferred to somebody not present. This "
        "is not a claim that they were evasive; it is a flag for a person to "
        "judge.\n\n"
        "Your evidence quotation must be the interviewee's own words from "
        "the transcript, under fifteen words, and must be the part that "
        "answers the question where there is one. Where the question was "
        "never put, quote nothing and say so with an empty evidence string "
        "is not permitted — quote the nearest thing that was discussed "
        "instead.\n\n"
        "Never say what any of this is worth. You are not scoring anything.",
        LABELS, heartbeat=heartbeat)

    by_id = {q["id"]: q for q in questions}
    covered, never_asked, not_addressed = [], [], []
    for qid, v in labelled.items():
        q = by_id[qid]
        entry = {"question": q["text"], "elements": q["elements"],
                 "said": v["evidence"], "confidence": v["confidence"]}
        if v["labels"]["was_asked"] == "no":
            never_asked.append({"question": q["text"],
                                "elements": q["elements"]})
        elif v["labels"]["the_answer_addressed_the_question"] == "no":
            not_addressed.append(entry)
        elif v["labels"]["was_answered"] == "yes":
            covered.append(entry)

    # Which elements this conversation actually reached. An element whose
    # every question went unasked is not evidenced by this interview, and
    # saying so is the difference between a gap somebody can close and one
    # nobody notices until the scoring.
    reached, missed = set(), set()
    for q in questions:
        for e in q["elements"]:
            if any(x["question"] == q["text"] for x in covered):
                reached.add(e)
    for q in questions:
        for e in q["elements"]:
            if e not in reached:
                missed.add(e)

    return len(covered), {
        "analysis": "synthesise_interview",
        "interview": {"guide": guide_ref, "function": function_name,
                      "interviewee": who, "role": role,
                      "transcript": filename,
                      "words": len(transcript.split())},
        "population": {
            "questions_in_the_guide": len(questions),
            "questions_read": len(labelled),
            "questions_the_model_did_not_return": len(unread),
        },
        "what_was_said": covered,
        "never_asked": never_asked,
        "asked_but_not_addressed": not_addressed,
        "elements_this_conversation_reached": sorted(reached),
        "elements_it_did_not": sorted(missed),
        "complete": not unread,
        "model": model.MODEL,
        "note": "Nothing here is a score. It is what was said, against the "
                "questions the guide asks, in the words that were said. A "
                "question nobody put is a gap in the evidence, not a silence "
                "about the client. An answer flagged as not addressing the "
                "question is for a person to judge, not an accusation.",
    }


def _pivot_available(conn, engagement_id: str) -> bool:
    """Whether the client's pipeline has been read from their deals export."""
    with conn.cursor() as cur:
        cur.execute("select count(*) from deal_stage_entry where engagement_id = %s",
                    (engagement_id,))
        n = cur.fetchone()[0]
    conn.rollback()
    return n > 0


def _stage_rows_from_pivot(conn, engagement_id: str):
    """The pivoted rows, in the shape the stage history export would be."""
    with conn.cursor() as cur:
        cur.execute("""
            select opportunity_id, stage_name, entered_at, segment
              from deal_stage_entry
             where engagement_id = %s
             order by opportunity_id, entered_at
        """, (engagement_id,))
        rows = cur.fetchall()
    conn.rollback()
    return rows


def _one_column(conn, engagement_id: str, key: str):
    """One canonical field's column, whichever analysis declares it.

    resolve_columns answers for an analysis. The segment a client splits
    their pipeline by is not part of any analysis's field list, so it is
    looked up on its own.
    """
    with conn.cursor() as cur:
        cur.execute("""
            select cf.key,
                   coalesce(m.confirmed_field_name, m.proposed_field_name),
                   a.storage_path,
                   coalesce(el.ref, ''), d.id::text
              from field_mapping m
              join canonical_field cf on cf.id = m.canonical_field_id
              join dataset d on d.id = m.dataset_id
              join artefact a on a.id = d.artefact_id and a.purged_at is null
              left join export_request_line erl on erl.id = a.export_request_line_id
              left join export_line el on el.id = erl.export_line_id
             where m.engagement_id = %s and cf.key = %s
               and m.status in ('confirmed','corrected','auto_accepted')
             limit 1
        """, (engagement_id, key))
        row = cur.fetchone()
    conn.rollback()
    return Column(*row) if row else None


def _flat(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).split())


# What each analysis actually reads.
#
# Three analyses have now been built needing a field they never declared, and
# each time the symptom was a figure quietly coming out as nought rather than
# an error. The requirements were authored from the prompt text, which says
# what to analyse; this says what to read, and the two are not the same.
#
# gea_check_declared() compares these against the instrument, so the next one
# is caught before anybody reads its output.
NEEDS = {
    "icp_reconciliation": [
        "opportunity_account_id", "opportunity_status", "account_id",
        "account_industry", "account_country", "account_employee_count",
        "account_annual_revenue"],
    "win_loss_and_churn_patterns": [
        "opportunity_id", "opportunity_status", "opportunity_account_id",
        "loss_reason", "account_id", "account_industry",
        "account_employee_count"],
    "price_discount_dispersion": [
        "opportunity_id", "opportunity_status", "opportunity_list_price",
        "opportunity_realised_price"],
    "stage_conversion_and_duration": [
        "opportunity_id", "opportunity_status", "stage_history_opportunity_id",
        "stage_history_to", "stage_entered_at",
        # Read where the client has it, and not required: it is what lets a
        # row that changed no stage be told apart from one that did, and a
        # history export without it is still usable.
        "stage_history_from"],
    "cost_per_qualified_opportunity": [
        "opportunity_id", "channel_source", "qualified_at", "spend_amount",
        "spend_channel", "spend_month"],
    "forecast_accuracy": [
        "forecast_period", "forecast_amount", "forecast_actual_amount"],
    "buying_group_composition": [
        "opportunity_id", "opportunity_status", "contact_role",
        "contact_role_opportunity_id", "contact_role_contact_id"],
    "handover_completeness_census": [
        "opportunity_id", "opportunity_status", "opportunity_account_id",
        "contact_role", "contact_role_opportunity_id", "activity_id",
        "activity_opportunity_id", "activity_type"],
    "expansion_vs_new_business": [
        "opportunity_id", "opportunity_status", "opportunity_type",
        "opportunity_account_id"],
    "renewal_timing_and_reason": [
        "subscription_id", "renewal_date", "renewal_outcome"],
    "signal_to_first_contact": [
        "contact_id", "activity_id", "activity_contact_id", "activity_at"],
    "note_sufficiency": [
        "note_id", "note_body", "note_author", "note_created_at",
        "note_opportunity_id", "meeting_at", "opportunity_id",
        "opportunity_status"],
    "discovery_consistency_classification": [
        "note_id", "note_body", "note_author", "note_created_at",
        "note_opportunity_id", "activity_owner"],
}


def check_declared(conn) -> list[str]:
    """Every field an analysis reads must be one the instrument requires.

    Run at startup. A field the implementation reads but the instrument does
    not require is invisible to capability, never mapped, and comes back
    empty — so the analysis computes a figure from nothing and reports it
    without complaint.
    """
    problems = []
    with conn.cursor() as cur:
        cur.execute("""
            select r.analysis_key, cf.key
              from analysis_field_requirement r
              join canonical_field cf on cf.id = r.canonical_field_id
              join instrument_version iv on iv.id = r.instrument_version_id
             where iv.status = 'active'
        """)
        declared: dict[str, set] = defaultdict(set)
        for key, field in cur.fetchall():
            declared[key].add(field)
    for key, needs in NEEDS.items():
        missing = sorted(set(needs) - declared.get(key, set()))
        if missing:
            problems.append(
                f"{key} reads {', '.join(missing)}, which the instrument does "
                f"not require. It will never be mapped, and the analysis will "
                f"compute from nothing.")
    return problems


# The analyses that read real data. Everything not here is still a fixture,
# and the database refuses to accept a fixture as evidence.
BUILT = {
    "icp_reconciliation": icp_reconciliation,
    "win_loss_and_churn_patterns": win_loss_and_churn_patterns,
    "price_discount_dispersion": price_discount_dispersion,
    "stage_conversion_and_duration": stage_conversion_and_duration,
    "cost_per_qualified_opportunity": cost_per_qualified_opportunity,
    "forecast_accuracy": forecast_accuracy,
    "buying_group_composition": buying_group_composition,
    "handover_completeness_census": handover_completeness_census,
    "expansion_vs_new_business": expansion_vs_new_business,
    "renewal_timing_and_reason": renewal_timing_and_reason,
    "signal_to_first_contact": signal_to_first_contact,
    "note_sufficiency": note_sufficiency,
    "discovery_consistency_classification": discovery_consistency_classification,
    "adversarial_score_review": adversarial_score_review,
    "synthesise_interview": synthesise_interview,
    "detect_pipeline": detect_pipeline,
}
