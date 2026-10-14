"""The jobs that are real.

Everything else the service declares still returns fixture output, and says so
by leaving model_name as 'stub'. A run produced by this module names itself,
so there is never a question about whether a figure on a screen came from the
client's data or from a placeholder.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import csvjobs
import storage

VERSION = "parse-3"


def parse_export(conn, run_id: str, engagement_id: str, artefact_ids: list[str],
                 heartbeat) -> tuple[int, dict]:
    """Read one client export and record what is in it.

    Writes the dataset and a profile row per column, in one transaction, so a
    half-described file never appears on a screen.
    """
    if not artefact_ids:
        raise ValueError("a parse job needs an artefact to read")
    if len(artefact_ids) > 1:
        raise ValueError(
            f"a parse job reads one file; this one names {len(artefact_ids)}")

    artefact_id = artefact_ids[0]
    with conn.cursor() as cur:
        cur.execute(
            "select storage_path, original_filename, byte_size, sha256 "
            "from artefact where id = %s and engagement_id = %s",
            (artefact_id, engagement_id))
        row = cur.fetchone()
    conn.rollback()

    if row is None:
        raise ValueError(
            "the file this job names is not recorded on this engagement")
    storage_path, filename, byte_size, _ = row

    # No progress is reported while downloading. Reporting bytes here and rows
    # afterwards made the count fill and then reset, which reads as a fault.
    # The interface shows "no count reported" until the first row lands, which
    # is true.
    local = storage.download(storage_path)

    # The local copy is deleted at the end rather than here, because the
    # rows are read from it a second time further down. It used to go the
    # moment profiling finished, which is why the first attempt at storing
    # rows found no file.
    try:
        result = csvjobs.read_export(
            local, on_progress=lambda rows, total: heartbeat(rows, total))
    except Exception:
        Path(local).unlink(missing_ok=True)
        raise

    profiles = [csvjobs.profile_column(c, result.row_count) for c in result.columns]

    with conn.cursor() as cur:
        # The dataset row is updated rather than replaced. Field mappings point
        # at it, so deleting it is refused once anyone has mapped a column —
        # which made re-reading a file impossible exactly when it mattered
        # most, after the mapping work had been done.
        #
        # Re-parsing the same bytes gives the same answer, so the row keeps its
        # identity and its description is rewritten. The field profiles under
        # it are replaced outright.
        cur.execute(
            """insert into dataset (artefact_id, row_count, column_count,
                   primary_date_field, date_min, date_max, malformed_rows,
                   parse_status, parse_errors)
               values (%s,%s,%s,%s,%s,%s,%s,'parsed',%s)
               on conflict (artefact_id) do update
                  set row_count = excluded.row_count,
                      column_count = excluded.column_count,
                      primary_date_field = excluded.primary_date_field,
                      date_min = excluded.date_min,
                      date_max = excluded.date_max,
                      malformed_rows = excluded.malformed_rows,
                      parse_status = excluded.parse_status,
                      parse_errors = excluded.parse_errors
               returning id""",
            (artefact_id, result.row_count, result.column_count,
             result.primary_date_field, result.date_min, result.date_max,
             result.malformed_rows,
             json.dumps({"encoding": result.encoding,
                         "delimiter": result.delimiter,
                         "malformed_examples": result.malformed_examples})
             if result.malformed_rows else None))
        dataset_id = cur.fetchone()[0]

        cur.execute("delete from dataset_field_profile where dataset_id = %s",
                    (dataset_id,))

        cur.executemany(
            """insert into dataset_field_profile (dataset_id, field_name,
                   inferred_type, populated_count, populated_rate,
                   distinct_count, top_value, top_value_share, is_free_text,
                   flags, sample_values)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [(dataset_id, p["field_name"], p["inferred_type"],
              p["populated_count"], p["populated_rate"], p["distinct_count"],
              p["top_value"], p["top_value_share"], p["is_free_text"],
              p["flags"], [v[:200] for v in p["sample_values"]])
             for p in profiles])

        # The rows themselves. Everything that used to need the file —
        # joining two exports, identifying what a file is from its values,
        # drilling from a figure to the deals behind it — becomes a query
        # once they are here.
        #
        # Written in batches because a single statement holding a hundred
        # thousand rows is a statement nobody can interrupt.
        cur.execute("delete from dataset_row where dataset_id = %s",
                    (dataset_id,))
        batch, n = [], 0
        for number, row in enumerate(csvjobs.rows_of(local, result), 1):
            batch.append((dataset_id, number, json.dumps(row)))
            if len(batch) >= 2000:
                cur.executemany(
                    "insert into dataset_row (dataset_id, row_number, data) "
                    "values (%s, %s, %s::jsonb)", batch)
                n += len(batch)
                batch = []
        if batch:
            cur.executemany(
                "insert into dataset_row (dataset_id, row_number, data) "
                "values (%s, %s, %s::jsonb)", batch)
            n += len(batch)

        cur.execute(
            "update artefact set row_count = %s, column_count = %s where id = %s",
            (result.row_count, result.column_count, artefact_id))
    conn.commit()

    # Never leave a copy of a client's CRM on disk.
    Path(local).unlink(missing_ok=True)

    # Reading a file is only ever the first half. Uploading is the decision a
    # person makes; proposing the mappings is the consequence, and it should
    # not depend on anyone remembering to ask for it.
    #
    # Queued here rather than by a database trigger because only this code
    # knows a read has just finished. A trigger would have to infer it from
    # the row, and a re-read of the same file leaves the row looking identical.
    with conn.cursor() as cur:
        cur.execute(
            """select run_id, was_created from gea_enqueue_run(
                   %s, 'propose_mappings', null, null, null, null, %s::jsonb)""",
            (engagement_id, json.dumps({
                "after_dataset": str(dataset_id),
                # Microseconds, because two readings a second apart are still
                # two readings and each deserves its own proposal run.
                "read_at": datetime.now(timezone.utc).isoformat(),
            })))
    conn.commit()

    flagged = sum(1 for p in profiles if p["flags"])
    return result.row_count, {
        "filename": filename,
        "row_count": result.row_count,
        "column_count": result.column_count,
        "malformed_rows": result.malformed_rows,
        "encoding": result.encoding,
        "delimiter": result.delimiter,
        "date_range": ({"field": result.primary_date_field,
                        "from": str(result.date_min),
                        "to": str(result.date_max)}
                       if result.primary_date_field else None),
        "columns_profiled": len(profiles),
        "columns_flagged": flagged,
        "dataset_id": str(dataset_id),
    }


def record_parse_failure(conn, artefact_id: str, reason: str) -> None:
    """A file that could not be read is recorded as such, not left blank.

    An empty dataset and a failed one look identical on a screen otherwise,
    and the difference is what a consultant needs to tell the client.
    """
    with conn.cursor() as cur:
        cur.execute(
            """insert into dataset (artefact_id, parse_status, parse_errors)
               values (%s, 'failed', %s)
               on conflict (artefact_id) do update
                  set parse_status = 'failed',
                      parse_errors = excluded.parse_errors""",
            (artefact_id, json.dumps({"reason": reason})))
        cur.execute("""delete from dataset_field_profile where dataset_id =
                       (select id from dataset where artefact_id = %s)""",
                    (artefact_id,))
    conn.commit()


def propose_mappings(conn, engagement_id: str, heartbeat) -> tuple[int, dict]:
    """Match the client's columns to the canonical vocabulary.

    Proposes; never decides. A mapping a person has already confirmed,
    corrected or marked unmapped is left exactly as they left it — running
    this again after a new export arrives must not quietly undo someone's
    work.
    """
    import mapping

    with conn.cursor() as cur:
        cur.execute("""
            select cf.id, cf.key, cf.description, cf.expected_type,
                   cf.is_cap_bearing, cf.source_export_refs
              from canonical_field cf
              join engagement e on e.instrument_version_id = cf.instrument_version_id
             where e.id = %s
             order by cf.key""", (engagement_id,))
        fields = cur.fetchall()

        # Every column of every file uploaded for this engagement, carrying
        # the export line it came from so a field is only matched against the
        # file it could possibly be in.
        cur.execute("""
            select p.field_name, p.inferred_type, p.populated_rate,
                   p.populated_count, p.distinct_count, d.id, el.ref
              from dataset_field_profile p
              join dataset d on d.id = p.dataset_id
              join artefact a on a.id = d.artefact_id
              join export_request_line erl on erl.id = a.export_request_line_id
              join export_line el on el.id = erl.export_line_id
             where a.engagement_id = %s and a.purged_at is null
               and d.parse_status = 'parsed'""", (engagement_id,))
        columns = [mapping.Column(r[0], r[1], float(r[2]), r[3], r[4],
                                  str(r[5]), r[6]) for r in cur.fetchall()]

        cur.execute("""select canonical_field_id from field_mapping
                        where engagement_id = %s
                          and status in ('confirmed','corrected','unmapped')""",
                    (engagement_id,))
        settled = {str(r[0]) for r in cur.fetchall()}
    conn.rollback()

    if not columns:
        raise ValueError(
            "no client export has been read yet, so there are no columns to "
            "match against")

    by_export: dict[str, list] = {}
    for col in columns:
        by_export.setdefault(col.export_ref, []).append(col)

    proposals, skipped = [], 0
    for i, (fid, key, description, expected, cap_bearing, refs) in enumerate(fields, 1):
        if str(fid) in settled:
            skipped += 1
            continue
        # The exports this field comes from first, and then everything
        # else.
        #
        # Scoping to the field's own export was right as far as it went and
        # went too far: a deals export carrying associated company columns
        # could never serve account_id, because the file was uploaded
        # against T1-01 and account_id comes from T1-02. Those columns were
        # invisible for ever.
        #
        # So every column is a candidate, and the ones from the right file
        # win ties. A column in the file a field belongs to is more likely
        # to be that field than an identically named column somewhere else,
        # which is what keeps Created Date in the deals export away from
        # account_created_at.
        own = [c for ref in (refs or []) for c in by_export.get(ref, [])]
        own_names = {id(c) for c in own}
        elsewhere = [c for c in columns if id(c) not in own_names]
        candidates = own + elsewhere
        proposals.append(mapping.propose(
            key, description, expected, cap_bearing, str(fid), candidates,
            own_exports=set(refs or [])))
        if i % 10 == 0:
            heartbeat(i, len(fields))

    with conn.cursor() as cur:
        for p in proposals:
            cur.execute("""
                insert into field_mapping (engagement_id, canonical_field_id,
                       dataset_id, proposed_field_name, confidence,
                       evidence_note, candidate_alternatives, status)
                values (%s,%s,%s,%s,%s,%s,%s,%s)
                on conflict (engagement_id, canonical_field_id) do update
                   set dataset_id = excluded.dataset_id,
                       proposed_field_name = excluded.proposed_field_name,
                       confidence = excluded.confidence,
                       evidence_note = excluded.evidence_note,
                       candidate_alternatives = excluded.candidate_alternatives,
                       status = excluded.status
                 where field_mapping.status in ('proposed','auto_accepted')""",
                (engagement_id, p.canonical_field_id, p.dataset_id,
                 p.proposed_field_name, p.confidence, p.evidence_note,
                 json.dumps(p.alternatives), p.status))
    conn.commit()

    auto = sum(1 for p in proposals if p.status == "auto_accepted")
    unmatched = sum(1 for p in proposals if p.proposed_field_name is None)
    needs_eyes = sum(1 for p in proposals
                     if p.status == "proposed" and p.proposed_field_name)
    cap_bearing = sum(1 for p in proposals if p.is_cap_bearing)

    heartbeat(len(fields), len(fields))
    return len(fields), {
        "fields_considered": len(fields),
        "already_settled": skipped,
        "auto_accepted": auto,
        "needs_review": needs_eyes,
        "no_match_found": unmatched,
        "cap_bearing_in_review": cap_bearing,
        "columns_searched": len(columns),
    }
