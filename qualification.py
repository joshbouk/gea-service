"""When a deal became qualified, worked out rather than looked up.

No CRM holds this. Qualification is not an event anybody records; it is
a stage the business treats as the point where a deal counts, and which
stage that is differs by client and changes when a process is rebuilt.
Asked for a column, a consultant has nothing to give, and the analysis
that says what a qualified opportunity costs never runs.

So a consultant nominates the stage, on the pipeline where the stages
are already in front of them, once per pipeline because a client who
replaced a thirteen-stage process with a seven-stage one has two
answers.

The date is then the first time a deal reached that stage. A deal that
skipped it is qualified at the first stage beyond it that the deal did
reach and that carries a date, because sellers skip stages when a deal
is moving quickly and a deal that reached Negotiate without passing
Propose was qualified on the way.

A deal that never reached the stage at all is not qualified, and it is
counted rather than dropped: the proportion of a pipeline that never
qualifies is worth seeing.
"""
from __future__ import annotations

from collections import defaultdict


def nominated(conn, engagement_id: str) -> dict[tuple[str, int], str]:
    """The stage each pipeline treats as qualification, by segment and era."""
    with conn.cursor() as cur:
        cur.execute("""
            select segment, era, qualified_at_stage
              from pipeline
             where engagement_id = %s and qualified_at_stage is not null
        """, (engagement_id,))
        out = {(r[0], r[1]): r[2] for r in cur.fetchall()}
    conn.rollback()
    return out


def positions(conn, engagement_id: str) -> dict[tuple[str, int], dict[str, int]]:
    """Where each stage sits in each pipeline, so later can be told from earlier."""
    with conn.cursor() as cur:
        cur.execute("""
            select p.segment, p.era, s.stage_name, s.position
              from pipeline_stage s
              join pipeline p on p.id = s.pipeline_id
             where p.engagement_id = %s and not s.is_outcome
        """, (engagement_id,))
        out: dict[tuple[str, int], dict[str, int]] = defaultdict(dict)
        for segment, era, stage, position in cur.fetchall():
            out[(segment, era)][stage] = position
    conn.rollback()
    return out


def when_qualified(conn, engagement_id: str) -> tuple[dict[str, object], dict]:
    """The date each deal became qualified, and what could not be worked out.

    Reads the stage entries the pipeline detection already recorded, so
    it needs nothing from the client beyond the nomination.
    """
    stages = nominated(conn, engagement_id)
    if not stages:
        return {}, {"why": "no pipeline has a qualification stage nominated"}

    where = positions(conn, engagement_id)
    with conn.cursor() as cur:
        cur.execute("""
            select opportunity_id, segment, era, stage_name, entered_at
              from deal_stage_entry
             where engagement_id = %s and entered_at is not null
             order by opportunity_id, entered_at
        """, (engagement_id,))
        entries = cur.fetchall()
    conn.rollback()

    # Every dated stage a deal reached, with where that stage sits.
    journeys: dict[str, list[tuple[int, object]]] = defaultdict(list)
    belongs: dict[str, tuple[str, int]] = {}
    for deal, segment, era, stage, when in entries:
        key = (segment, era)
        place = where.get(key, {}).get(stage)
        if place is None:
            continue
        journeys[deal].append((place, when))
        belongs[deal] = key

    qualified: dict[str, object] = {}
    never = 0
    no_nomination = 0
    for deal, reached in journeys.items():
        key = belongs[deal]
        stage = stages.get(key)
        if stage is None:
            # The pipeline this deal ran through has no nomination. Not
            # the deal's fault and worth counting separately from a deal
            # that genuinely never qualified.
            no_nomination += 1
            continue
        bar = where.get(key, {}).get(stage)
        if bar is None:
            no_nomination += 1
            continue
        # The first dated stage at or past the bar. Sorted, so the first
        # one found is the earliest.
        at = [when for place, when in sorted(reached, key=lambda x: (x[1], x[0]))
              if place >= bar]
        if at:
            qualified[deal] = at[0]
        else:
            never += 1

    return qualified, {
        "deals_with_a_stage_history": len(journeys),
        "qualified": len(qualified),
        "never_qualified": never,
        "in_a_pipeline_with_no_nomination": no_nomination,
        "nominated": {f"{s} ({'earlier' if e == 1 else 'current'})": st
                      for (s, e), st in sorted(stages.items())},
    }
