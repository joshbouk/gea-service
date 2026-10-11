"""GEA analysis service — stub worker.

The application and this service share one database and communicate only
through the `analysis_run` table. There is no HTTP between them. The
application inserts a queued row; this claims it, works, and writes the result
back. The browser watches the row over realtime.

**This is the stub.** It returns fixture output for every job type with
realistic timings and progress, so every screen can be built against it exactly
as if the numbers were real. When the real service replaces it, nothing in the
application changes. Figures it produces are invented and must never reach a
client deliverable.

    pip install -r requirements.txt
    python worker.py

Needs three environment variables. Keep all of them in your environment,
never in this repository, never in a chat window.

    DATABASE_URL          the same connection string gea-db/apply.py uses,
                          from the Connect button on the Supabase project page
    SUPABASE_URL          https://<project>.supabase.co
    SUPABASE_SERVICE_KEY  the service role key, from Project Settings, API
                          Keys. It reads client exports out of storage and
                          bypasses every access rule, so it is the most
                          sensitive credential in the project.

Ctrl-C stops it after the run in flight finishes. A claimed run is always seen
through; it is never abandoned half done.
"""
from __future__ import annotations

import json
import os
import random
import signal
import sys
import time
from datetime import datetime, timezone

import psycopg

import realjobs
import storage
from csvjobs import ParseError

POLL_SECONDS = 2.0

# Reaching storage can fail for reasons that have nothing to do with the file:
# a timeout, a blip, a token being refreshed. A consultant seeing "failed" on
# one of those would go back to the client about a file that is perfectly good.
# The service tries again itself, and only gives up when it really is not
# working.
STORAGE_ATTEMPTS = 3
STORAGE_BACKOFF = (2, 8)

# The connection will die: laptops sleep, networks drop, databases restart.
# The worker waits and reconnects rather than stopping, backing off so a
# database that is genuinely down is not hammered.
RECONNECT_MAX = 30
running = True


def log(message: str) -> None:
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')}  {message}", flush=True)


def stop(_signum, _frame):
    global running
    running = False
    log("finishing the current run, then stopping")


# --------------------------------------------------------------- fixtures --
# Each returns (total_units, output). total_units drives the progress count the
# interface shows: a census of 4,000 records reports 4,000, never a spinner.

def fixture(job_type: str) -> tuple[int, dict]:
    # parse is real and handled before this is reached.

    if job_type == "profile":
        columns = random.randint(28, 94)
        return columns, {
            "columns_profiled": columns,
            "flags": {"mostly_null": random.randint(2, 9),
                      "single_value": random.randint(0, 4),
                      "free_text_in_picklist": random.randint(0, 3)},
        }

    if job_type.startswith("analysis:"):
        population = random.randint(300, 1800)
        return population, {"analysis_key": job_type.split(":", 1)[1],
                            "population": population,
                            "note": "Fixture output. Figures are not real."}

    if job_type.startswith("classify:"):
        # The long one. This is what the progress count exists for.
        population = random.randint(1500, 4000)
        return population, {
            "classification_key": job_type.split(":", 1)[1],
            "records_classified": population,
            "validation_required": True,
            "note": "Fixture output. A classification run needs a trusted "
                    "validation set before it can be accepted.",
        }

    if job_type == "synthesise_interview":
        return 18, {"elements_evidenced": 11, "quotes_extracted": 18}

    if job_type == "parse_upload:aeo":
        return 21, {"prompts_parsed": 21, "brand_mentioned_in": 9}

    if job_type.startswith("generate:"):
        return 12, {"deliverable": job_type.split(":", 1)[1],
                    "pages": random.randint(8, 34),
                    "storage_path": "fixture/deliverable.docx"}

    # An unknown job type fails rather than quietly succeeding. The contract
    # says never construct a job the service does not declare, and this is how
    # a mistake gets noticed instead of producing nothing.
    raise ValueError(f"this service declares no job type '{job_type}'")


# ------------------------------------------------------------------ worker --

def parse_with_retries(conn, run_id, engagement_id, artefact_ids, heartbeat):
    """Read the file, trying again when the problem is ours rather than the file's.

    A malformed file, a missing artefact or an unreadable encoding fails at
    once, because those need someone to go back to the client. Not reaching
    storage is a problem on our side and is tried again before anyone is told.
    """
    for attempt in range(1, STORAGE_ATTEMPTS + 1):
        try:
            return realjobs.parse_export(
                conn, run_id, engagement_id, artefact_ids, heartbeat)
        except storage.NotConfigured:
            # Missing configuration is not a transient problem and retrying it
            # three times only delays saying so.
            conn.rollback()
            raise
        except storage.StorageError as exc:
            conn.rollback()
            # A file that is genuinely absent will still be absent in ten
            # seconds. Only a transient problem is worth trying again.
            if "not in storage at" in str(exc) or attempt == STORAGE_ATTEMPTS:
                raise
            pause = STORAGE_BACKOFF[min(attempt - 1, len(STORAGE_BACKOFF) - 1)]
            log(f"          storage did not answer ({exc}); "
                f"trying again in {pause}s")
            time.sleep(pause)
    raise AssertionError("unreachable")


# Set by work_one so the loop can fail the run if something unexpected goes
# wrong inside it.
claimed_run_id: str | None = None


def work_one(conn: psycopg.Connection) -> bool:
    """Claim a run and see it through. False when nothing was queued."""
    global claimed_run_id
    with conn.cursor() as cur:
        cur.execute("select id, job_type from gea_claim_run()")
        claimed = cur.fetchone()
    conn.commit()

    if not claimed:
        claimed_run_id = None
        return False

    run_id, job_type = claimed
    claimed_run_id = run_id
    log(f"claimed   {job_type}  {run_id}")

    def heartbeat(done: int, total: int) -> None:
        with conn.cursor() as cur:
            cur.execute("select gea_heartbeat_run(%s, %s, %s)",
                        (run_id, int(done), int(total)))
        conn.commit()

    # parse is real work on the client's own file. Everything else is still
    # fixture output and says so by leaving model_name as 'stub'.
    if job_type == "parse":
        with conn.cursor() as cur:
            cur.execute("select engagement_id, input_artefact_ids "
                        "from analysis_run where id = %s", (run_id,))
            engagement_id, artefact_ids = cur.fetchone()
        conn.rollback()

        try:
            total, output = parse_with_retries(
                conn, run_id, engagement_id, list(artefact_ids or []), heartbeat)
        except (ParseError, storage.StorageError, ValueError) as exc:
            conn.rollback()
            if artefact_ids:
                try:
                    realjobs.record_parse_failure(conn, artefact_ids[0], str(exc))
                except Exception:                                  # noqa: BLE001
                    conn.rollback()
            with conn.cursor() as cur:
                cur.execute("update analysis_run set status='failed', "
                            "finished_at=now(), error=%s where id=%s",
                            (str(exc), run_id))
            conn.commit()
            log(f"failed    {job_type}  {exc}")
            return True

        with conn.cursor() as cur:
            cur.execute(
                "update analysis_run set status='succeeded', finished_at=now(), "
                "progress_done=%s, progress_total=%s, compute_output=%s::jsonb, "
                "model_name=%s where id=%s",
                (total, total, json.dumps(output), realjobs.VERSION, run_id))
        conn.commit()
        log(f"succeeded {job_type}  {output['row_count']} rows, "
            f"{output['column_count']} columns, "
            f"{output['malformed_rows']} unreadable")
        return True

    if job_type == "propose_mappings":
        with conn.cursor() as cur:
            cur.execute("select engagement_id from analysis_run where id = %s",
                        (run_id,))
            engagement_id = cur.fetchone()[0]
        conn.rollback()
        try:
            total, output = realjobs.propose_mappings(
                conn, engagement_id, heartbeat)
        except ValueError as exc:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("update analysis_run set status='failed', "
                            "finished_at=now(), error=%s where id=%s",
                            (str(exc), run_id))
            conn.commit()
            log(f"failed    {job_type}  {exc}")
            return True

        with conn.cursor() as cur:
            cur.execute(
                "update analysis_run set status='succeeded', finished_at=now(), "
                "progress_done=%s, progress_total=%s, compute_output=%s::jsonb, "
                "model_name=%s where id=%s",
                (total, total, json.dumps(output), realjobs.VERSION, run_id))
        conn.commit()
        log(f"succeeded {job_type}  {output['auto_accepted']} matched, "
            f"{output['needs_review']} to review, "
            f"{output['no_match_found']} with no match")
        return True

    # Analyses that have been built read the client's own data. The rest
    # still return fixture output and say so by leaving model_name 'stub',
    # which the database refuses to accept as evidence.
    if job_type.startswith("analysis:") and \
            job_type.split(":", 1)[1] in analyses.BUILT:
        key = job_type.split(":", 1)[1]
        with conn.cursor() as cur:
            cur.execute("select engagement_id from analysis_run where id = %s",
                        (run_id,))
            engagement_id = cur.fetchone()[0]
        conn.rollback()
        try:
            total, output = analyses.BUILT[key](conn, engagement_id, heartbeat)
        except (ValueError, storage.StorageError) as exc:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("update analysis_run set status='failed', "
                            "finished_at=now(), error=%s where id=%s",
                            (str(exc), run_id))
            conn.commit()
            log(f"failed    {job_type}  {exc}")
            return True

        with conn.cursor() as cur:
            cur.execute(
                "update analysis_run set status='succeeded', finished_at=now(), "
                "progress_done=%s, progress_total=%s, compute_output=%s::jsonb, "
                "model_name=%s where id=%s",
                (total, total, json.dumps(output), analyses.VERSION, run_id))
        conn.commit()
        p = output["population"]
        log(f"succeeded {job_type}  {p['described']:,} of "
            f"{p['closed_won']:,} closed-won described")
        return True

    try:
        total, output = fixture(job_type)
    except ValueError as exc:
        with conn.cursor() as cur:
            cur.execute("update analysis_run set status='failed', "
                        "finished_at=now(), error=%s where id=%s",
                        (str(exc), run_id))
        conn.commit()
        log(f"failed    {job_type}  {exc}")
        return True

    # Progress in visible steps, paused so the interface has something to show.
    # The real service reports as it actually goes.
    steps = 12
    for step in range(1, steps + 1):
        with conn.cursor() as cur:
            cur.execute("select gea_heartbeat_run(%s, %s, %s)",
                        (run_id, int(total * step / steps), total))
        conn.commit()
        time.sleep(random.uniform(0.25, 0.6))

    with conn.cursor() as cur:
        cur.execute(
            "update analysis_run set status='succeeded', finished_at=now(), "
            "progress_done=%s, progress_total=%s, compute_output=%s::jsonb, "
            "model_name='stub' where id=%s",
            (total, total, json.dumps(output), run_id))
    conn.commit()
    log(f"succeeded {job_type}  {total} units")
    return True


def connect(dsn: str, quiet: bool = False):
    """Open a connection, waiting for the database if it is not there yet.

    Returns None when asked to stop while waiting.
    """
    delay = 1
    while running:
        try:
            conn = psycopg.connect(dsn)
            if not quiet:
                log("connected")
            return conn
        except psycopg.OperationalError as exc:
            if not quiet:
                log(f"database not reachable ({str(exc).splitlines()[0]}); "
                    f"trying again in {delay}s")
            time.sleep(delay)
            delay = min(delay * 2, RECONNECT_MAX)
    return None


def main() -> int:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL is not set. See the comment at the top of this "
              "file.", file=sys.stderr)
        return 1

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    # Everything the worker needs, checked before it reports itself ready. A
    # worker that starts, sits idle looking healthy and fails on the first real
    # job is worse than one that refuses to start.
    try:
        storage.check_configured()
    except storage.NotConfigured as exc:
        print(str(exc), file=sys.stderr)
        return 1

    try:
        first = psycopg.connect(dsn)
    except psycopg.OperationalError as exc:
        # Refuse to start against a database that is not there. A worker that
        # silently retries forever on a typo in DATABASE_URL looks identical
        # to one with no work to do.
        print(f"Could not reach the database. {exc}", file=sys.stderr)
        return 1
    conn = first

    # A run left claimed by a worker that died stays claimed forever: claiming
    # only takes queued runs, so nothing else will ever touch it. Clear those
    # before looking for new work.
    try:
        with conn.cursor() as cur:
            cur.execute("select run_id, job_type, abandoned_for "
                        "from gea_fail_abandoned_runs()")
            abandoned = cur.fetchall()
        conn.commit()
        for _, job_type, silent_for in abandoned:
            log(f"abandoned {job_type}  claimed by a service that stopped, "
                f"silent for {silent_for}")
        if abandoned:
            log(f"          {len(abandoned)} abandoned "
                f"{'run' if len(abandoned) == 1 else 'runs'} failed. Redo any "
                f"that still need doing.")
    except Exception as exc:                                   # noqa: BLE001
        # Not a reason to refuse to start: there is still work to do.
        conn.rollback()
        log(f"could not check for abandoned runs ({exc})")

    log("analysis service started. Ctrl-C to stop.")
    idle_reported = False

    while running:
        try:
            did_work = work_one(conn)

        except psycopg.OperationalError as exc:
            # The connection died: the machine slept, the network dropped, the
            # database restarted. None of that is a reason to stop. Replace the
            # connection and carry on. Nothing is lost — a run claimed on a
            # connection that died was never committed as claimed, so it is
            # still queued for whoever picks it up next.
            log(f"lost the connection ({str(exc).splitlines()[0]})")
            try:
                conn.close()
            except Exception:                                  # noqa: BLE001
                pass
            conn = connect(dsn)
            if conn is None:
                break
            idle_reported = False
            continue

        except Exception as exc:                               # noqa: BLE001
            # Something we did not anticipate. The run must not be left
            # running: a job nobody will ever finish shows as stalled hours
            # later with no reason attached, and the person reading the screen
            # has no way to know it is never coming back.
            log(f"error: {exc}")
            try:
                conn.rollback()
                if claimed_run_id:
                    with conn.cursor() as cur:
                        cur.execute(
                            "update analysis_run set status='failed', "
                            "finished_at=now(), error=%s where id=%s "
                            "and status='running'",
                            (f"the service failed unexpectedly: {exc}",
                             claimed_run_id))
                    conn.commit()
            except psycopg.Error:
                # Rolling back on a broken connection throws again. Let the
                # next pass through the loop deal with it rather than dying
                # inside the handler, which is what used to kill the process.
                pass
            time.sleep(POLL_SECONDS)
            continue

        if did_work:
            idle_reported = False
        else:
            if not idle_reported:
                log("queue empty, waiting")
                idle_reported = True
            time.sleep(POLL_SECONDS)

    try:
        conn.close()
    except Exception:                                          # noqa: BLE001
        pass

    log("stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
