"""What the evidence adds up to.

An engagement produces forty-five pipelines and a dozen analyses cut
several ways. A consultant cannot read that out and a client would not
listen. The report needs four or five sentences, each traceable to a
specific cut.

Three rules decide what is worth saying, and they are the whole of this
module.

A segment earns a sentence by being different. Three divisions behaving
alike is one sentence — "the three divisions win at much the same rate" —
not three.

A difference is only worth reporting where the smaller side is large
enough to mean something. On the first real client a segment held a single
deal, and a hundred per cent of one deal is not a finding.

Nothing is compared across a pipeline change. A client that replaced its
stages has two populations and a figure spanning them describes neither.

Everything here is a proposal. A consultant dismisses what the data cannot
see — "the East division only sells to regulated utilities" explains a gap
no analysis could know about — and the reason stays on the record, because
the next assessment will raise it again.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime

# A difference smaller than this is not worth a client's attention, however
# many records sit behind it. Twelve points between two divisions is a
# conversation; three points is noise wearing a number.
WORTH_SAYING = 0.12

# And the smaller side has to be big enough to carry a rate at all.
ENOUGH_TO_COMPARE = 25


def _plain(value):
    """Anything json cannot write, as something it can.

    An analysis output read back from the database carries Decimals and
    dates, and a finding's detail is whatever the analysis put there. A
    sentence failing to save because one number arrived as a Decimal is
    not a failure worth having.
    """
    try:
        return float(value)
    except (TypeError, ValueError):
        return str(value)


@dataclass
class Question:
    """Something the data raised that only a person can settle.

    The analyses that look hardest at a client's data are the ones that
    find things no export explains: a field populated on an eighth of the
    records, a deal value in six columns and none of them called Amount,
    a division missing from a quarter of the pipeline. Each has one right
    person to ask and takes ninety seconds in an interview.
    """
    analysis_key: str
    question: str
    because: str
    ask_role: str
    element_refs: list = field(default_factory=list)

    def as_row(self, engagement_id: str, run_id: str | None):
        return (engagement_id, self.analysis_key, self.question,
                self.because, self.element_refs, self.ask_role, run_id)


@dataclass
class Finding:
    analysis_key: str
    kind: str
    headline: str
    detail: dict = field(default_factory=dict)
    element_refs: list[str] = field(default_factory=list)

    def as_row(self, engagement_id: str, run_id: str | None):
        return (engagement_id, self.analysis_key, self.kind, self.headline,
                json.dumps(self.detail, default=_plain),
                self.element_refs, run_id)


def _pct(x: float) -> str:
    return f"{round(x * 100)}%"


def _count(n) -> str:
    """A number as somebody would say it. 1,295 rather than 1295."""
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def _when(value) -> str:
    """A date as somebody would say it.

    These sentences are written to be read aloud in a meeting, and nobody
    says "from two thousand twenty-six dash oh six dash eleven".
    """
    if not value:
        return "an unknown date"
    text = str(value)[:10]
    try:
        d = datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return text
    return f"{d.day} {d.strftime('%B %Y')}"


def _name(segment: str) -> str:
    return segment or "the business as a whole"


def compare_segments(analysis_key: str, label: str, rows: list[dict],
                     what: str, elements: list[str]) -> list[Finding]:
    """One sentence about how the parts of a business differ, or do not.

    rows are BySegment output: segment, of, rate. Only the outermost level
    is compared — a division against a division. Comparing a division
    against another division's deal type is a category error, and nesting
    multiplies the cuts until something looks different by chance.
    """
    top = [r for r in rows
           if r.get("depth") == 1
           and r.get("rate") is not None
           and (r.get("of") or 0) >= ENOUGH_TO_COMPARE]
    if len(top) < 2:
        # Not a finding, and worth saying why rather than staying silent:
        # a client whose segments are all too small to compare has told us
        # something about how they are organised.
        small = [r for r in rows if r.get("depth") == 1
                 and (r.get("of") or 0) < ENOUGH_TO_COMPARE]
        if len(small) >= 2:
            return [Finding(
                analysis_key, "too_small_to_compare",
                f"{what} cannot be compared across {label.lower()}: "
                f"{len(small)} of them hold fewer than "
                f"{ENOUGH_TO_COMPARE} records each.",
                {"segments": [r["segment"] for r in small]}, elements)]
        return []

    top.sort(key=lambda r: -r["rate"])
    best, worst = top[0], top[-1]
    gap = best["rate"] - worst["rate"]

    if gap < WORTH_SAYING:
        return [Finding(
            analysis_key, "no_difference",
            f"{what} is much the same across {label.lower()}: "
            f"{_pct(worst['rate'])} to {_pct(best['rate'])} "
            f"across {len(top)} of them.",
            {"segments": top}, elements)]

    return [Finding(
        analysis_key, "segments_differ",
        f"{what} ranges from {_pct(worst['rate'])} in "
        f"{_name(worst['segment'])} to {_pct(best['rate'])} in "
        f"{_name(best['segment'])}, a gap of "
        f"{round(gap * 100)} points.",
        {"best": best, "worst": worst, "all": top,
         "gap_in_points": round(gap * 100, 1)}, elements)]


def compare_eras(pipelines: list[dict], elements: list[str]) -> list[Finding]:
    """Whether the pipeline they replaced was working better than the new one.

    The single most valuable comparison available to a client who changed
    their process, and the one nothing else computes. It is also the one
    most easily got wrong: the newer pipeline has had less time, so its
    deals are disproportionately still open, and a win rate that counts
    them as not-yet-won flatters the old one.
    """
    eras = sorted({p["era"] for p in pipelines})
    if len(eras) < 2:
        return []

    out = []
    whole = {p["era"]: p for p in pipelines if not p.get("depth")}
    if len(whole) < 2:
        return out

    first, last = whole[eras[0]], whole[eras[-1]]
    out.append(Finding(
        "detect_pipeline", "pipeline_changed",
        f"The pipeline was replaced: {first['stages']} stages became "
        f"{last['stages']}, from {_when(last['era_from'])}. "
        f"{_count(first['deals'])} deals ran through the earlier one and "
        f"{_count(last['deals'])} through the current one.",
        {"earlier": first, "current": last}, elements))

    older = first.get("stage_records_inferred")
    newer = last.get("stage_records_inferred")
    if older is not None and newer is not None:
        better = newer < older - 0.1
        worse = newer > older + 0.1
        if better or worse:
            # Said with its caveat, because the newer pipeline is younger.
            #
            # Its deals are mostly still open and sitting in early stages,
            # which need less inferring than a deal that has been through
            # eight of them. An improvement here can be real or can be the
            # newer population simply having had less time, and a sentence
            # that does not say so invites somebody to claim credit.
            out.append(Finding(
                "detect_pipeline", "hygiene_changed",
                f"Stage records appear {'better' if better else 'worse'} "
                f"kept under the current pipeline: {_pct(newer)} of entries "
                f"had to be inferred against {_pct(older)} before. The "
                f"current pipeline has been running since "
                f"{_when(last.get('era_from'))}, so its deals have had less "
                f"time to accumulate stages and the comparison is not yet "
                f"like for like.",
                {"earlier": older, "current": newer,
                 "current_pipeline_began": last.get("era_from")}, elements))
    return out


# Who can answer what. A question about what populates a field is for
# whoever runs the CRM; one about why a division behaves differently is
# for whoever runs the division. Asking either of the other wastes both
# conversations.
REVOPS, SALES, MARKETING, FINANCE = ("operations", "sales", "marketing",
                                     "finance")


def questions_from(analysis_key: str, output: dict) -> list[Question]:
    """What this analysis found that it cannot explain."""
    out: list[Question] = []
    pop = output.get("population", {})
    rows = output.get("by_segment") or []

    if analysis_key == "win_loss_and_churn_patterns":
        unq, lost = pop.get("unqualified") or 0, pop.get("lost") or 0
        if unq > lost:
            out.append(Question(
                analysis_key,
                "What has to be true for a deal to be qualified out rather "
                "than worked? And who makes that call?",
                f"{unq} deals were closed as unqualified against {lost} "
                f"lost to somebody else, so what goes wrong is happening "
                f"before a competitor is involved.",
                SALES, ["1.1", "4.5", "5.1"]))

        top = [r for r in rows if r.get("depth") == 1
               and r.get("rate") is not None]
        if len(top) >= 2:
            top.sort(key=lambda r: -r["rate"])
            if top[0]["rate"] - top[-1]["rate"] >= WORTH_SAYING:
                out.append(Question(
                    analysis_key,
                    f"Why does {_name(top[-1]['segment'])} convert so "
                    f"differently from {_name(top[0]['segment'])}? Is it "
                    f"the market, the people, or how deals get in?",
                    f"{_pct(top[0]['rate'])} against "
                    f"{_pct(top[-1]['rate'])}, on "
                    f"{top[0]['of']} and {top[-1]['of']} closed deals.",
                    SALES, ["1.2", "1.3"]))

        unassigned = next((r for r in rows
                           if "not recorded" in (r.get("segment") or "")), None)
        if unassigned and (unassigned.get("of") or 0) >= ENOUGH_TO_COMPARE:
            out.append(Question(
                analysis_key,
                "What are the deals with no segment recorded against them? "
                "Are they a part of the business, or is the field simply "
                "not filled in?",
                f"{unassigned['of']} closed deals have no value in the "
                f"field the business is split by.",
                REVOPS, ["1.2"]))
    return out


def from_analysis(analysis_key: str, output: dict) -> list[Finding]:
    """Everything worth saying from one analysis's output."""
    label = ", ".join(output.get("split_by") or []) or "segment"
    rows = output.get("by_segment") or []

    if analysis_key == "win_loss_and_churn_patterns":
        out = compare_segments(analysis_key, label, rows, "The win rate",
                               ["1.3", "5.1"])
        # Unqualified is its own finding where it dwarfs losing, because a
        # client whose deals mostly die before a competitor sees them has
        # a qualification problem rather than a selling one.
        pop = output.get("population", {})
        lost, unq = pop.get("lost") or 0, pop.get("unqualified") or 0
        if unq >= max(ENOUGH_TO_COMPARE, lost * 1.5):
            out.append(Finding(
                analysis_key, "disqualification_dominates",
                f"Deals are closed as unqualified far more often than they "
                f"are lost: {_count(unq)} against {_count(lost)}. What goes "
                f"wrong happens before a competitor is in the room.",
                {"unqualified": unq, "lost": lost}, ["1.1", "4.5", "5.1"]))
        return out

    if analysis_key == "handover_completeness_census":
        return compare_segments(analysis_key, label, rows,
                                "The share of won deals handed over complete",
                                ["6.1", "H05"])
    if analysis_key == "renewal_timing_and_reason":
        return compare_segments(analysis_key, label, rows,
                                "The renewal rate", ["8.4", "H08"])
    if analysis_key == "cost_per_qualified_opportunity":
        return compare_segments(analysis_key, label, rows,
                                "The share of qualified opportunities",
                                ["3.1", "3.4"])
    return []


def run(conn, engagement_id: str, heartbeat=None) -> tuple[int, dict]:
    """Read what every analysis found, and say what it adds up to.

    Reads the latest succeeded run of each analysis rather than the one
    that happened to finish last. An engagement's findings are about all
    of its evidence, and recomputing from one analysis would drop every
    sentence the others support.

    A stale run is skipped. Its file has been removed and a finding
    resting on it would be a sentence nobody can trace.
    """
    with conn.cursor() as cur:
        cur.execute("""
            select distinct on (r.job_type)
                   r.job_type, r.id::text, r.compute_output, r.accepted_at
              from analysis_run r
             where r.engagement_id = %s
               and r.status = 'succeeded'
               and not r.is_stale
               and r.model_name <> 'stub'
               and (r.job_type like 'analysis:%%' or r.job_type = 'detect_pipeline')
             order by r.job_type, r.finished_at desc
        """, (engagement_id,))
        runs = cur.fetchall()
    conn.rollback()

    findings: list[Finding] = []
    questions: list[Question] = []
    read, skipped = 0, []
    newest_run = None
    for job_type, run_id, output, accepted in runs:
        key = job_type.split(":", 1)[1] if ":" in job_type else job_type
        if not isinstance(output, dict) or not output:
            skipped.append(key)
            continue
        read += 1
        newest_run = newest_run or run_id
        findings.extend(from_analysis(key, output))
        questions.extend(questions_from(key, output))

    # The pipelines are not an analysis output: they are rows the
    # detection wrote, and the era comparison reads them directly.
    with conn.cursor() as cur:
        cur.execute("""
            select era, stages, deals, era_from, stage_records_inferred,
                   coalesce(array_length(segment_values, 1), 0)
              from pipeline_board where engagement_id = %s
        """, (engagement_id,))
        # Converted here, where the database hands them over. Postgres
        # returns a numeric as a Decimal, which neither compares with a
        # float nor survives json.dumps, and chasing that at each use
        # means finding it again at the next one.
        pipelines = [{"era": e, "stages": st, "deals": d,
                      "era_from": str(f) if f else None,
                      "stage_records_inferred": (float(inf) if inf is not None
                                                 else None),
                      "depth": depth}
                     for e, st, d, f, inf, depth in cur.fetchall()]
    conn.rollback()
    findings.extend(compare_eras(pipelines, ["5.1", "H04"]))

    kept = store(conn, engagement_id, findings, newest_run)
    asked = store_questions(conn, engagement_id, questions, newest_run)
    return kept, {
        "questions_for_a_person": asked,
        "analysis": "roll_up",
        "analyses_read": read,
        "analyses_with_no_output": sorted(set(skipped)) or None,
        "findings_proposed": len(findings),
        "findings_stored": kept,
        "findings_already_set_aside": len(findings) - kept,
        "note": "What the evidence adds up to. A segment earns a sentence "
                "by being different rather than by existing, nothing is "
                "compared across a pipeline change, and a difference "
                "somebody has already explained is not raised again.",
    }


def store_questions(conn, engagement_id: str, questions: list[Question],
                    run_id: str | None = None) -> int:
    """Keep the questions, and never ask an answered one again.

    A question somebody has already put to the client and written down
    the answer to is finished. Raising it again after the analyses re-run
    would send an interviewer into a room to ask something they have the
    answer to, which is how a guide stops being read.
    """
    with conn.cursor() as cur:
        cur.executemany("""
            insert into data_question (engagement_id, analysis_key, question,
                   because, element_refs, ask_role, source_run_id)
                 values (%s,%s,%s,%s,%s,%s,%s)
            on conflict (engagement_id, question) do update
               set because = excluded.because,
                   source_run_id = excluded.source_run_id
             where data_question.answered_at is null
        """, [q.as_row(engagement_id, run_id) for q in questions])
    conn.commit()
    return len(questions)


def store(conn, engagement_id: str, findings: list[Finding],
          run_id: str | None = None) -> int:
    """Replace this engagement's proposals, keeping what was dismissed.

    A dismissal is a judgement a person made and it outlives a re-run. The
    same difference found again is the same difference, and raising it a
    second time after somebody explained it would train them to ignore the
    screen.
    """
    with conn.cursor() as cur:
        cur.execute("""
            select headline from rollup_finding
             where engagement_id = %s and dismissed_at is not null
        """, (engagement_id,))
        already_set_aside = {r[0] for r in cur.fetchall()}

        cur.execute("""
            delete from rollup_finding
             where engagement_id = %s and dismissed_at is null
        """, (engagement_id,))

        rows = [f.as_row(engagement_id, run_id) for f in findings
                if f.headline not in already_set_aside]
        cur.executemany("""
            insert into rollup_finding (engagement_id, analysis_key, kind,
                   headline, detail, element_refs, source_run_id)
                 values (%s,%s,%s,%s,%s::jsonb,%s,%s)
        """, rows)
    conn.commit()
    return len(rows)
