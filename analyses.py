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
from datetime import datetime
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


def load_accounts(cols: dict[str, Column]) -> dict[str, dict]:
    """A lookup from account to its firmographics, from the companies export.

    Held in memory because it is one row per customer, not one per deal: a
    client with twenty thousand accounts is a few megabytes. The deals file,
    which is the big one, is never held.
    """
    out: dict[str, dict] = {}
    if "account_id" not in cols:
        return out
    with storage.download(cols["account_id"].storage_path).open(
            "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
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


WON = ("won", "closed won", "closedwon", "closed-won")
LOST = ("lost", "closed lost", "closedlost", "closed-lost", "closed/lost")


def outcome(value) -> str | None:
    """Won, lost, or neither. Neither is open, and open is not a loss."""
    t = (str(value) if value is not None else "").strip().lower()
    if t in WON:
        return "won"
    if t in LOST:
        return "lost"
    return None


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
    accounts = load_accounts(cols)
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

    with storage.download(deals_path).open("r", encoding="utf-8-sig",
                                           newline="") as fh:
        for row in csv.DictReader(fh):
            deals += 1
            if outcome(row.get(status_col)) != "won":
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
    absent = [k for k in needed if k not in cols]
    if absent:
        raise ValueError("this analysis needs fields that are not mapped: "
                         + ", ".join(absent))

    heartbeat(0, 2)
    accounts = load_accounts(cols)
    heartbeat(1, 2)

    won_cuts, not_cut = firmographic_cuts(cols)
    lost_cuts, _ = firmographic_cuts(cols)
    loss_reasons, win_reasons, competitors = Cut("loss reason"), Cut("win reason"), Cut("competitor named")

    deals = won = lost = open_deals = no_account = unmatched = 0
    status_col = cols["opportunity_status"].column_name
    account_col = cols["opportunity_account_id"].column_name

    with storage.download(cols["opportunity_status"].storage_path).open(
            "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            deals += 1
            result = outcome(row.get(status_col))
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
    absent = [k for k in needed if k not in cols]
    if absent:
        raise ValueError("this analysis needs fields that are not mapped: "
                         + ", ".join(absent))

    accounts = load_accounts(cols) if "account_id" in cols else {}
    discounts: list[float] = []
    same_as_list = 0
    by_segment: dict[str, list[float]] = defaultdict(list)
    by_owner: dict[str, list[float]] = defaultdict(list)
    won = no_list = no_realised = zero_list = 0

    status_col = cols["opportunity_status"].column_name
    list_col = cols["opportunity_list_price"].column_name
    real_col = cols["opportunity_realised_price"].column_name

    with storage.download(cols["opportunity_status"].storage_path).open(
            "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            if outcome(row.get(status_col)) != "won":
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
    needed = ["opportunity_id", "opportunity_status",
              "stage_history_opportunity_id", "stage_history_to",
              "stage_entered_at"]
    absent = [k for k in needed if k not in cols]
    if absent:
        raise ValueError("this analysis needs fields that are not mapped: "
                         + ", ".join(absent))

    # The outcome of each deal, from the deals export.
    heartbeat(0, 2)
    outcomes: dict[str, str | None] = {}
    closed_on: dict[str, object] = {}
    with storage.download(cols["opportunity_id"].storage_path).open(
            "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
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
    rows = undated = orphan = 0
    with storage.download(cols["stage_history_to"].storage_path).open(
            "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            rows += 1
            deal = (row.get(cols["stage_history_opportunity_id"].column_name) or "").strip()
            stage = (row.get(cols["stage_history_to"].column_name) or "").strip()
            when = a_date(row.get(cols["stage_entered_at"].column_name))
            if not deal or not stage:
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
    absent = [k for k in needed if k not in cols]
    if absent:
        raise ValueError("this analysis needs fields that are not mapped: "
                         + ", ".join(absent))

    heartbeat(0, 2)
    spend: dict[tuple[str, str], float] = defaultdict(float)
    spend_rows = spend_undated = spend_unpriced = 0
    with storage.download(cols["spend_amount"].storage_path).open(
            "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
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
    with storage.download(cols["opportunity_id"].storage_path).open(
            "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
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
    absent = [k for k in needed if k not in cols]
    if absent:
        raise ValueError("this analysis needs fields that are not mapped: "
                         + ", ".join(absent))

    periods: dict[str, dict] = {}
    rows = unusable = 0
    with storage.download(cols["forecast_amount"].storage_path).open(
            "r", encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
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


# The analyses that read real data. Everything not here is still a fixture,
# and the database refuses to accept a fixture as evidence.
BUILT = {
    "icp_reconciliation": icp_reconciliation,
    "win_loss_and_churn_patterns": win_loss_and_churn_patterns,
    "price_discount_dispersion": price_discount_dispersion,
    "stage_conversion_and_duration": stage_conversion_and_duration,
    "cost_per_qualified_opportunity": cost_per_qualified_opportunity,
    "forecast_accuracy": forecast_accuracy,
}
