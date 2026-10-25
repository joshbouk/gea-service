"""The report, drafted from the evidence.

The system drafts and the consultant finishes, not the other way round. A
consultant handed a blank document writes around the evidence; one handed
a draft argues with it, and arguing with a draft produces a better report
in less time. Everything the data can say, this says.

Where human judgement is needed the section asks for it in a sentence and
prints the answer. Nobody is asked to compose three paragraphs in a brand
voice at eleven at night, which is how a report ends up sounding like four
different people.

The output is a list of blocks the Blend branded-document builder turns
into a Word file. This module writes no Word XML and never will: the
fonts and the header artwork cannot be reproduced programmatically and
the template is the only correct source of them.
"""
from __future__ import annotations

import json
from datetime import date

import style

# Blend's branded builder takes these and nothing else.
BROW, H1, H2, H3, P, BULLETS, SPACER, BREAK = (
    "brow", "h1", "h2", "h3", "p", "bullets", "spacer", "pagebreak")


def _p(text: str) -> dict:
    return {"type": P, "text": text}


def _bullets(items: list[str]) -> dict:
    return {"type": BULLETS, "items": items}


def _count(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def _when(value, region: str = "uk") -> str:
    """A date as somebody would say it, where the report is going.

    A British reader writes 11 June 2026 and an American one June 11,
    2026. Getting it wrong is the kind of small thing that tells a reader
    the document was written for somebody else.
    """
    if not value:
        return "an unknown date"
    if not isinstance(value, date):
        return str(value)[:10]
    if region == "us":
        return value.strftime("%B %-d, %Y")
    return f"{value.day} {value.strftime('%B %Y')}"


def money(amount, facts: dict) -> str:
    """A figure in the currency the client reports in.

    Never a bare number. A reader meeting 4,200 in a report decides for
    themselves what currency it is, and they decide it is theirs.
    """
    try:
        n = f"{float(amount):,.0f}"
    except (TypeError, ValueError):
        return str(amount)
    return f"{facts.get('symbol', '')}{n}"


def engagement_facts(conn, engagement_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("""
            select c.company_name, e.name, e.window_start, e.window_end,
                   e.sponsor_name, e.sponsor_role, iv.label,
                   e.score_frozen_at, e.systems,
                   e.report_region::text, e.currency, e.locale,
                   cu.symbol
              from engagement e
              left join currency_in_use cu on cu.code = e.currency
              join client c on c.id = e.client_id
              join instrument_version iv on iv.id = e.instrument_version_id
             where e.id = %s
        """, (engagement_id,))
        row = cur.fetchone()
    conn.rollback()
    if not row:
        raise ValueError("that engagement is not on record")
    return {"client": row[0], "engagement": row[1], "from": row[2],
            "to": row[3], "sponsor": row[4], "sponsor_role": row[5],
            "instrument": row[6], "frozen_at": row[7],
            "systems": row[8] or [], "region": row[9],
            "currency": row[10], "locale": row[11],
            # A code with no symbol prints as the code, which is plain
            # and correct. Inventing a symbol would not be.
            "symbol": row[12] or (row[10] + " ")}


def evidence_section(conn, engagement_id: str) -> list[dict]:
    """What was looked at, and what could not be.

    First in the report and not last. A reader who finds a gap themselves
    on page nine distrusts pages one to eight; saying it on page two
    costs a page and buys the rest.
    """
    with conn.cursor() as cur:
        cur.execute("""
            select arrived, never_coming, still_expected,
                   fields_arrived, fields_asked_for, settled
              from evidence_readiness where engagement_id = %s
        """, (engagement_id,))
        r = cur.fetchone()
        cur.execute("""
            select el.ref, el.question, erl.status::text, erl.unavailable_reason
              from export_request_line erl
              join export_line el on el.id = erl.export_line_id
             where erl.engagement_id = %s and erl.included
               and erl.status in ('absent', 'refused')
             order by el.ref
        """, (engagement_id,))
        gone = cur.fetchall()
    conn.rollback()
    if not r:
        return [_p("No evidence has been recorded for this engagement.")]

    arrived, never, waiting, fields_in, fields_all, settled = r
    # Postgres sums nothing to null. A report saying "None of 142 fields"
    # is a report nobody finishes reading.
    fields_in = fields_in or 0
    out = [_p(
        f"The assessment read {_count(arrived)} of the "
        f"{_count(arrived + never + waiting)} data sets it asks for, "
        f"covering {_count(fields_in)} of {_count(fields_all)} fields.")]

    if gone:
        out.append(_p(
            "These were not available, and each one limits what the "
            "assessment can say about the part of the business it covers."))
        out.append(_bullets([
            f"**{q}** {reason or 'No reason was recorded.'}"
            for _ref, q, _st, reason in gone]))

    if not settled:
        # Said plainly rather than hidden. A report written while files
        # were still arriving is a report about to be wrong, and the
        # reader is entitled to know which way.
        out.append(_p(
            f"{_count(waiting)} data sets were still expected when this was "
            f"written. Anything resting on them may change."))
    return out


def findings_section(conn, engagement_id: str,
                     stage_code: str | None = None) -> list[dict]:
    """What was found, in order of how much it matters.

    Scoped to a stage where the section is about one. Without that, the
    stage chapters each printed every finding: four copies of the same
    four sentences, which is how a reader learns to skim.
    """
    with conn.cursor() as cur:
        cur.execute("""
            select f.kind, f.headline, f.element_refs
              from rollup_finding f
             where f.engagement_id = %s and f.dismissed_at is null
               and (%s::text is null or exists (
                     select 1 from unnest(f.element_refs) r
                       join element e on e.ref = r
                       join engagement en on en.id = f.engagement_id
                                         and en.instrument_version_id
                                             = e.instrument_version_id
                      where e.stage_code = %s))
             order by case f.kind
                        when 'pipeline_changed' then 1
                        when 'disqualification_dominates' then 2
                        when 'segments_differ' then 3
                        when 'hygiene_changed' then 4
                        when 'no_difference' then 8
                        when 'too_small_to_compare' then 9
                        else 5 end, f.headline
        """, (engagement_id, stage_code, stage_code))
        rows = cur.fetchall()
    conn.rollback()
    if not rows:
        if stage_code:
            # Not an error and not worth a paragraph of apology. A stage
            # the data had nothing to say about is a stage scored on
            # interviews, and the scores above say so.
            return []
        return [_p(
            "No findings have been worked out yet. Run the analyses and the "
            "roll-up before producing this report.")]

    said, quiet = [], []
    for kind, headline, _refs in rows:
        (quiet if kind in ("no_difference", "too_small_to_compare")
         else said).append(headline)

    out = [_p(h) for h in said]
    if quiet:
        # These are answers too. "The three divisions win at much the
        # same rate" is worth a client's time and gets said with the
        # same confidence as a gap.
        out.append({"type": H3, "text": "And what did not differ"})
        out.append(_bullets(quiet))
    return out


def scores_section(conn, engagement_id: str,
                   stage_code: str | None = None) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            select stage_code, stage_name, scored, elements, excluded, average
              from engagement_stage_score where engagement_id = %s
               and (%s::text is null or stage_code = %s)
             order by stage_code
        """, (engagement_id, stage_code, stage_code))
        stages = cur.fetchall()
    conn.rollback()
    if not stages:
        return [_p("Nothing has been scored yet.")]

    lines = []
    for code, name, scored, elements, excluded, avg in stages:
        # The denominator is always stated. A stage averaging four out of
        # three elements is a different claim from four out of six, and
        # the number alone cannot tell them apart.
        if avg is None:
            lines.append(f"**{name}** not scored")
            continue
        said = f"**{name}** {avg}, across {_count(scored)} of "\
               f"{_count(elements)} elements"
        if excluded:
            said += f" ({_count(excluded)} set aside)"
        lines.append(said)
    return [_bullets(lines)]


def pipeline_section(conn, engagement_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            select segment, era, stages, deals, era_from, era_to
              from pipeline_board
             where engagement_id = %s
               and coalesce(array_length(segment_values, 1), 0) = 0
             order by era
        """, (engagement_id,))
        eras = cur.fetchall()
    conn.rollback()
    if not eras:
        return []
    if len(eras) == 1:
        s = eras[0]
        return [_p(f"The pipeline runs {_count(s[2])} stages, and "
                   f"{_count(s[3])} deals went through it.")]
    first, last = eras[0], eras[-1]
    return [_p(
        f"The pipeline was replaced during the period assessed: "
        f"{_count(first[2])} stages became {_count(last[2])}, from "
        f"{_when(last[4])}. {_count(first[3])} deals ran through the earlier "
        f"one and {_count(last[3])} through the current one. Nothing in this "
        f"report compares a figure across that change, because a rate "
        f"spanning two pipelines describes neither.")]


BUILDERS = {
    "evidence": evidence_section,
    "findings": findings_section,
    "scores": scores_section,
    "pipeline": pipeline_section,
}


def draft(conn, engagement_id: str) -> tuple[list[dict], dict]:
    """Every block of the report, and what is missing from it."""
    facts = engagement_facts(conn, engagement_id)
    with conn.cursor() as cur:
        cur.execute("""
            select r.ref, r.title, r.sort_order, r.purpose, r.reads, r.asks,
                   r.needed, r.answer, r.ready, s.stage_code
              from report_readiness r
              join engagement e on e.id = r.engagement_id
              join report_section s on s.ref = r.ref
                                   and s.instrument_version_id
                                       = e.instrument_version_id
             where r.engagement_id = %s
             order by r.sort_order
        """, (engagement_id,))
        sections = cur.fetchall()
    conn.rollback()

    blocks: list[dict] = [
        {"type": BROW, "text": "Growth Engineering Assessment"},
        {"type": H1, "text": facts["client"]},
        _p(f"Assessed over the period {_when(facts['from'], facts['region'])} "
           f"to {_when(facts['to'], facts['region'])}. Figures are in "
           f"{facts['currency']}."),
    ]
    unanswered, empty = [], []

    for (ref, title, _order, _purpose, reads, asks, needed,
         answer, ready, stage_code) in sections:
        body: list[dict] = []
        for source in (reads or []):
            builder = BUILDERS.get(source)
            if not builder:
                continue
            if source in ("findings", "scores"):
                body.extend(builder(conn, engagement_id, stage_code))
            else:
                body.extend(builder(conn, engagement_id))
        if answer:
            body.append(_p(answer))
        elif asks:
            unanswered.append({"ref": ref, "title": title, "asks": asks})

        if not body:
            if needed:
                empty.append(ref)
            continue
        blocks.append({"type": H2, "text": title})
        blocks.extend(body)

    return blocks, {
        "client": facts["client"],
        "instrument": facts["instrument"],
        # Which template to build it from, and how to write the dates.
        "region": facts["region"],
        "currency": facts["currency"],
        "locale": facts["locale"],
        "scores_frozen": facts["frozen_at"] is not None,
        "sections_written": sum(1 for b in blocks if b["type"] == H2),
        "questions_unanswered": unanswered,
        "sections_with_nothing_to_say": empty,
    }


def check_the_prose(blocks: list[dict]) -> list[dict]:
    """Every sentence the report will print, against the style rules.

    Run before the document is built rather than after. A breach found in
    a Word file is a breach somebody has to fix twice.
    """
    bad = []
    for b in blocks:
        texts = [b["text"]] if b.get("text") else list(b.get("items") or [])
        for t in texts:
            if b["type"] in (BROW, H1, H2, H3):
                continue
            r = style.report(t)
            if r["refused"]:
                bad.append({"text": t, "breaks": [f["what"] for f in r["findings"]]})
    return bad


def write_content_file(blocks: list[dict], path: str) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(blocks, fh, ensure_ascii=False, indent=1)
    return path
