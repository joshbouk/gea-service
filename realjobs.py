"""The jobs that are real.

Everything else the service declares still returns fixture output, and says so
by leaving model_name as 'stub'. A run produced by this module names itself,
so there is never a question about whether a figure on a screen came from the
client's data or from a placeholder.
"""
from __future__ import annotations

import json
from pathlib import Path

import csvjobs
import storage

VERSION = "parse-1"


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

    try:
        result = csvjobs.read_export(
            local, on_progress=lambda rows, total: heartbeat(rows, total))
    finally:
        # Never leave a copy of a client's CRM on disk.
        Path(local).unlink(missing_ok=True)

    profiles = [csvjobs.profile_column(c, result.row_count) for c in result.columns]

    with conn.cursor() as cur:
        # Re-parsing the same bytes gives the same answer, so replacing the
        # previous description is safe. The artefact itself is immutable.
        cur.execute("delete from dataset where artefact_id = %s", (artefact_id,))
        cur.execute(
            """insert into dataset (artefact_id, row_count, column_count,
                   primary_date_field, date_min, date_max, malformed_rows,
                   parse_status, parse_errors)
               values (%s,%s,%s,%s,%s,%s,%s,'parsed',%s) returning id""",
            (artefact_id, result.row_count, result.column_count,
             result.primary_date_field, result.date_min, result.date_max,
             result.malformed_rows,
             json.dumps({"encoding": result.encoding,
                         "delimiter": result.delimiter,
                         "malformed_examples": result.malformed_examples})
             if result.malformed_rows else None))
        dataset_id = cur.fetchone()[0]

        cur.executemany(
            """insert into dataset_field_profile (dataset_id, field_name,
                   inferred_type, populated_count, populated_rate,
                   distinct_count, top_value, top_value_share, is_free_text, flags)
               values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            [(dataset_id, p["field_name"], p["inferred_type"],
              p["populated_count"], p["populated_rate"], p["distinct_count"],
              p["top_value"], p["top_value_share"], p["is_free_text"],
              p["flags"]) for p in profiles])

        cur.execute(
            "update artefact set row_count = %s, column_count = %s where id = %s",
            (result.row_count, result.column_count, artefact_id))
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
        cur.execute("delete from dataset where artefact_id = %s", (artefact_id,))
        cur.execute(
            """insert into dataset (artefact_id, parse_status, parse_errors)
               values (%s, 'failed', %s)""",
            (artefact_id, json.dumps({"reason": reason})))
    conn.commit()
