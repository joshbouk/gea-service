"""Working out what a file is.

The old way round was: pick an export line, read the instructions, produce
exactly that file, upload it against that line. Twenty-five times. It
assumes a consultant works through a list in order, which nobody does, and
it breaks the moment a Salesforce report hits the hundred-column cap and
has to come out in two passes.

The way round that matches an afternoon is: pull what you can, drop it in,
and be told what you have covered.

None of this is new machinery. The canonical vocabulary is already a
classifier — a hundred and twenty-odd fields, each tagged with the export
line it comes from — and the matcher already scores any column against any
field. What was missing was the arithmetic on top:

  What is this file? Score every column against every field and tally
  which export lines the good matches belong to.

  What is its key? The column that is fully populated, almost entirely
  distinct, and matches an identifier field.

  Is it part of something already here? Same best line, same key column,
  overlapping key values, different other columns.

  Does it answer more than one request? The tally says so, and that is the
  part that stops the system asking for something it has already been
  given.

Two things it must never do.

It proposes and does not decide. A file wrongly identified as deals when
it is quotes would mis-map a whole engagement, and the cost of being wrong
is far higher than the cost of a person spending four seconds agreeing.

It says nothing about scope. A 2024 deals export and a 2025 one are the
same shape. It reports the date range it found and leaves that judgement
where it belongs.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

import identify_batch
import mapping

# A column has to match this well to count towards identifying a file.
# Below it the match is a coincidence of wording and tallying it would
# make every file look like every other.
COUNTS_TOWARDS = 0.75

# A file has to match this much better than the runner-up to be proposed
# without hedging. Two lines neck and neck is a question, not an answer.
CLEAR_ENOUGH = 1.4

# And it has to match enough of a line at all. Salesforce column names do
# not resemble our field keys — "Stage" against opportunity_status matches
# nothing by name — so four good matches out of twenty-four is an ordinary
# result for a real export, and two out of ten is noise. Without a floor,
# a contact-roles file with an opportunity id and a contact id in it gets
# confidently called an activities export.
ENOUGH_MATCHES = 3
ENOUGH_OF_THE_LINE = 0.2

# A key is almost entirely distinct. Not entirely: real exports carry a
# handful of duplicate rows and refusing on one repeat would be useless.
KEY_DISTINCT = 0.98
KEY_POPULATED = 0.99


def _an(word: str) -> str:
    """a or an, so a generated sentence does not read as a draft."""
    return ("an " if word[:1].lower() in "aeiou" else "a ") + word


def from_salesforce_ids(conn, dataset_id: str, columns: list[dict]
                        ) -> tuple[str | None, str | None, str | None]:
    """The object a Salesforce key column names, and the column it was in.

    Only the file's own key. A contact roles export carries a Contact ID
    and an Opportunity ID, and reading either as the file's identity makes
    it a contacts export or a deals export — which it is not. Both are
    foreign keys and neither says what the file is.

    The file's own key is the one column that is unique, fully populated,
    and the only such column. Where two columns both look unique, nothing
    here can tell which is the subject, so this says nothing at all rather
    than guessing.
    """
    # Unique, and shaped like a Salesforce id.
    #
    # Requiring one unique column was too strict: a contacts export has a
    # unique Contact ID and a unique Email, so it had two and the rule
    # declined to read either. An email is unique and is not an id, and
    # the difference is visible — a Salesforce id is fifteen or eighteen
    # characters of letters and digits.
    unique = [c for c in columns
              if (c.get("row_count") or 0)
              and (c.get("populated_rate") or 0) >= 0.99
              and (c.get("distinct_count") or 0) / (c.get("row_count") or 1)
                  >= KEY_DISTINCT]
    if len(unique) > 1:
        shaped = []
        with conn.cursor() as cur:
            for col in unique:
                cur.execute("""
                    select count(*) filter (
                             where r.data ->> %s ~ '^[a-zA-Z0-9]{15}([a-zA-Z0-9]{3})?$'),
                           count(*)
                      from (select data from dataset_row
                             where dataset_id = %s limit 100) r
                """, (col["name"], dataset_id))
                looks, of = cur.fetchone()
                if of and looks / of >= 0.95:
                    shaped.append(col)
        conn.rollback()
        unique = shaped
    if len(unique) != 1:
        return None, None, None

    with conn.cursor() as cur:
        for col in unique:
            cur.execute("""
                select gea_id_prefix_object(r.data ->> %s),
                       left(btrim(r.data ->> %s), 3)
                  from dataset_row r
                 where r.dataset_id = %s
                   and nullif(btrim(r.data ->> %s), '') is not null
                 limit 1
            """, (col["name"], col["name"], dataset_id, col["name"]))
            row = cur.fetchone()
            if row and row[0]:
                conn.rollback()
                return row[0], col["name"], row[1]
    conn.rollback()
    return None, None, None


# Which export line each Salesforce object belongs to.
SALESFORCE_OBJECT_LINE = {
    "opportunity": "T1-01", "account": "T1-02", "contact": "T1-03",
    "lead": "T1-10", "case": "T1-06", "contract": "T1-07",
    "order": "T1-07", "task": "T2-03", "event": "T2-03",
    "opportunity_contact_role": "T1-08",
}


def export_lines(conn, engagement_id: str) -> list[dict]:
    """Every export line this engagement asks for, as a question."""
    with conn.cursor() as cur:
        cur.execute("""
            select el.ref, coalesce(el.question, el.title)
              from export_line el
              join engagement e on e.id = %s
                               and e.instrument_version_id
                                   = el.instrument_version_id
             order by el.ref
        """, (engagement_id,))
        out = [{"ref": r[0], "question": r[1]} for r in cur.fetchall()]
    conn.rollback()
    return out


def identify_everything_unassigned(conn, engagement_id: str, heartbeat=None):
    """Work out what every unplaced file is, over the whole set at once.

    The rule-based version was rewritten four times in a day and ended up
    both slow and wrong. It read one file at a time, so the first file in
    a batch had nothing to compare itself against and a contacts export
    was proposed as the deals export on its column names alone. And
    comparing every column of a three-hundred-column file against every
    key of a sixty-seven-thousand-row file took minutes per file.

    A client sends eight files together and they only make sense
    together, so they go together. One call, every file, every question.

    The Salesforce id rule still runs first and still wins: an id names
    its own object in its first three characters and that is not worth
    asking about.
    """
    with conn.cursor() as cur:
        cur.execute("""
            select a.id::text, a.original_filename, d.id::text, d.row_count
              from artefact a
              join dataset d on d.artefact_id = a.id
             where a.engagement_id = %s
               and a.purged_at is null
               and a.export_request_line_id is null
               and d.parse_status = 'parsed'
             order by a.created_at
        """, (engagement_id,))
        waiting = cur.fetchall()
    conn.rollback()

    if not waiting:
        return 0, {"analysis": "identify_files", "files_looked_at": 0,
                   "note": "Every file has been placed."}

    lines = export_lines(conn, engagement_id)

    settled_by_id: dict[str, tuple] = {}
    described = []
    for n, (artefact_id, filename, dataset_id, row_count) in enumerate(waiting, 1):
        if heartbeat:
            heartbeat(n - 1, len(waiting) + 1)
        with conn.cursor() as cur:
            cur.execute("""
                select p.field_name, p.populated_rate, p.distinct_count,
                       p.inferred_type, d.row_count
                  from dataset_field_profile p
                  join dataset d on d.id = p.dataset_id
                 where p.dataset_id = %s
            """, (dataset_id,))
            cols = [{"name": r[0], "populated_rate": float(r[1] or 0),
                     "distinct_count": r[2], "inferred_type": r[3],
                     "row_count": r[4]} for r in cur.fetchall()]
        conn.rollback()

        obj, in_column, prefix = from_salesforce_ids(conn, dataset_id, cols)
        if obj and obj in SALESFORCE_OBJECT_LINE:
            settled_by_id[filename] = (
                SALESFORCE_OBJECT_LINE[obj],
                f"The {in_column} column holds Salesforce ids beginning "
                f"{prefix}, which is how Salesforce writes "
                f"{_an(obj.replace('_', ' '))}. That settles it whatever the "
                f"columns are called.")
        described.append(
            identify_batch.describe(conn, dataset_id, filename, row_count, cols))

    if heartbeat:
        heartbeat(len(waiting), len(waiting) + 1)

    asked_failed = None
    try:
        answers = {a.get("file"): a for a in identify_batch.ask(described, lines)}
    except Exception as exc:
        # The ids still settled what they settled. A model that is down
        # should not lose the half of the answer that needed no model.
        if not settled_by_id:
            raise
        answers, asked_failed = {}, str(exc)

    valid = {l["ref"] for l in lines}
    done = []
    for artefact_id, filename, _ds, _rows in waiting:
        if filename in settled_by_id:
            ref, reason = settled_by_id[filename]
            confidence, also, part = "clear", [], None
        else:
            a = answers.get(filename) or {}
            ref = a.get("export_ref")
            # A line that does not exist is a guess. Dropped, not stored.
            ref = ref if ref in valid else None
            confidence = a.get("confidence") or "unsure"
            reason = a.get("reason") or (
                f"Working this out needed the analysis service, which said: "
                f"{asked_failed}" if asked_failed
                else "Nothing in the file says what it is.")
            also = [r for r in (a.get("also_serves") or []) if r in valid]
            part = a.get("part_of")

        with conn.cursor() as cur:
            cur.execute("""
                insert into artefact_identification (artefact_id, proposed_ref,
                       confidence, reason, also_serves, evidence)
                     values (%s,%s,%s,%s,%s,%s::jsonb)
                on conflict (artefact_id) do update
                   set proposed_ref = excluded.proposed_ref,
                       confidence = excluded.confidence,
                       reason = excluded.reason,
                       also_serves = excluded.also_serves,
                       evidence = excluded.evidence
                 where artefact_identification.decided_at is null
            """, (artefact_id, ref, confidence, reason, also,
                  json.dumps({"part_of": part,
                              "settled_by_id": filename in settled_by_id},
                             default=str)))
        conn.commit()
        done.append({"file": filename, "proposed": ref,
                     "confidence": confidence, "reason": reason})

    clear = sum(1 for d in done if d["confidence"] == "clear")
    return len(done), {
        "analysis": "identify_files",
        "files_looked_at": len(done),
        "settled": clear,
        "needing_a_person": len(done) - clear,
        "settled_by_a_salesforce_id": len(settled_by_id),
        "each": done,
        "note": "What each unplaced file appears to be, with the reason. "
                "The files were looked at together, because a file only "
                "makes sense beside the others: the one with Role and "
                "Primary is contact roles because two of the others are "
                "deals and contacts. Nothing has been placed.",
    }


