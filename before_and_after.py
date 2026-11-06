"""What an analysis says now, against what it said before.

Every test in this repository is structural. The rules pass, the
migrations apply, the modules parse — and none of that touches whether
the numbers are right. A threshold was changed in four minutes, shipped
green, and turned a client's readable sales funnel into scattered counts
nobody could use. The suite said ninety out of ninety.

So: run the analyses against a real engagement, write down what they
said, and compare after a change. Not to assert that figures never move
— a fix is supposed to move them — but so that what moved is in front of
somebody before it reaches a client.

    python before_and_after.py record <engagement>   # keep today's answers
    python before_and_after.py compare <engagement>  # against the last ones

The rule this exists to enforce: no analysis change ships without being
run against real data, with the before and after shown.

It changes nothing in the database. Two analyses save what they find:
detect_pipeline rebuilds the pipelines, and synthesise_interview reads
the next transcript and stores its findings. Run from here, both used
to commit, so a comparison taken while the mappings were broken rebuilt
the pipelines from broken mappings, lost the qualifying stages stored on
them, and read an interview nobody had asked to read. Now every analysis
runs inside one transaction that is rolled back at the end, and the ones
that ask the model are not run at all: they cost money, and a model's
answer differs run to run, so a difference proves nothing.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys

import psycopg

import analyses

HERE = pathlib.Path(__file__).parent / ".answers"


# Read with the model. Not run here; see the note at the top.
ASKS_THE_MODEL = {
    "synthesise_interview",
    "note_sufficiency",
    "discovery_consistency_classification",
    "adversarial_score_review",
}


class Sandbox:
    """The connection as an analysis sees it, with commit taken away.

    An analysis that commits gets a savepoint instead, and one that rolls
    back goes back to that savepoint, so inside the run everything behaves
    as it would for the worker: a later analysis sees the pipelines an
    earlier one stored. The whole transaction is rolled back at the end.
    """

    def __init__(self, conn):
        self._conn = conn
        with conn.cursor() as cur:
            cur.execute("savepoint before_and_after")

    def commit(self):
        with self._conn.cursor() as cur:
            cur.execute("release savepoint before_and_after")
            cur.execute("savepoint before_and_after")

    def rollback(self):
        with self._conn.cursor() as cur:
            cur.execute("rollback to savepoint before_and_after")

    def __getattr__(self, name):
        return getattr(self._conn, name)


def shape_of(output: dict) -> dict:
    """The figures worth watching, not every number in the output.

    A full diff of an analysis output is unreadable and mostly noise.
    What matters is the handful of numbers somebody would quote: the
    population, the rates, and for a pipeline the stage counts, which
    are what went wrong.
    """
    keep: dict = {}
    for k, v in output.items():
        if isinstance(v, (int, float, bool)) or v is None:
            keep[k] = v
        elif k == "population" and isinstance(v, dict):
            keep[k] = v
        elif k == "by_segment" and isinstance(v, list):
            keep[k] = [{kk: s.get(kk) for kk in ("segment", "of", "rate")}
                       for s in v]
    return keep


def pipelines(conn, engagement_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("""
            select p.segment, p.era, p.deals, p.stage_records_inferred,
                   p.enters_at_the_top, p.is_a_funnel,
                   s.stage_name, s.position, s.deals_entered,
                   s.deals_recorded, s.conversion_from_previous,
                   s.median_days_here, s.days_measured_on, s.is_outcome
              from pipeline p
              left join pipeline_stage s on s.pipeline_id = p.id
             where p.engagement_id = %s
             order by p.segment, p.era, s.is_outcome, s.position
        """, (engagement_id,))
        cols = [d.name for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.rollback()
    return json.loads(json.dumps(rows, default=str))


def run_them_all(conn, engagement_id: str) -> dict:
    out: dict = {}
    total = len(analyses.BUILT)
    for n, (key, fn) in enumerate(sorted(analyses.BUILT.items()), 1):
        # Said out loud, because some of these read a hundred and fifty
        # thousand rows and a silent minute looks like a hang.
        print(f"  [{n}/{total}] {key}", flush=True)
        if key in ASKS_THE_MODEL:
            out[key] = {"skipped": True}
            print("          not run here: it asks the model", flush=True)
            continue
        try:
            _n, result = fn(conn, engagement_id, lambda a, b: None)
            out[key] = {"ran": True, "shape": shape_of(result)}
            print(f"          ran", flush=True)
        except ValueError as exc:
            conn.rollback()
            # Not an error. An analysis that cannot run on this client's
            # evidence says so, and that is itself worth watching: a
            # change that makes one stop running is a change to notice.
            out[key] = {"ran": False, "why": str(exc)[:200]}
            print(f"          cannot run yet: {str(exc)[:70]}", flush=True)
        except Exception as exc:
            conn.rollback()
            out[key] = {"ran": False, "broke": f"{type(exc).__name__}: {exc}"[:200]}
            print(f"          BROKE: {type(exc).__name__}: {exc}"[:90], flush=True)
    print("  reading the pipelines", flush=True)
    out["_pipelines"] = pipelines(conn, engagement_id)
    return out


def differences(before: dict, after: dict) -> list[str]:
    said = []
    for key in sorted(set(before) | set(after)):
        if key == "_pipelines":
            continue
        b, a = before.get(key), after.get(key)
        if (b or {}).get("skipped") or (a or {}).get("skipped"):
            continue
        if b is None:
            said.append(f"  {key}: new")
        elif a is None:
            said.append(f"  {key}: gone")
        elif b.get("ran") != a.get("ran"):
            said.append(f"  {key}: {'now runs' if a['ran'] else 'stopped running'}"
                        f" — {a.get('why') or a.get('broke') or ''}")
        elif b.get("shape") != a.get("shape"):
            for field in sorted(set(b.get("shape", {})) | set(a.get("shape", {}))):
                bv, av = b.get("shape", {}).get(field), a.get("shape", {}).get(field)
                if bv != av:
                    said.append(f"  {key}.{field}: {bv!r} → {av!r}")

    # The pipelines, by segment and stage, because that is where the
    # damage was and a summary would have hidden it.
    def by_stage(rows):
        return {(r["segment"], r["era"], r["stage_name"]): r for r in rows}
    b, a = by_stage(before.get("_pipelines", [])), by_stage(after.get("_pipelines", []))
    for k in sorted(set(b) | set(a), key=lambda x: tuple(str(i) for i in x)):
        if k not in b:
            said.append(f"  pipeline {k[0]} era {k[1]}: {k[2]} appeared")
        elif k not in a:
            said.append(f"  pipeline {k[0]} era {k[1]}: {k[2]} gone")
        else:
            for f in ("deals_entered", "conversion_from_previous",
                      "median_days_here", "is_funnel"):
                if b[k].get(f) != a[k].get(f):
                    said.append(f"  pipeline {k[0]} era {k[1]} {k[2]}.{f}: "
                                f"{b[k].get(f)!r} → {a[k].get(f)!r}")
    return said


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in ("record", "compare"):
        print(__doc__)
        return 2
    what, engagement_id = sys.argv[1], sys.argv[2]
    # The same connection the worker uses, found the same way, plus a
    # .env beside it. Asking somebody to set a variable they have
    # already set is how a tool goes unused.
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        env = pathlib.Path(__file__).parent / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                name, _, value = line.partition("=")
                if name.strip() == "DATABASE_URL":
                    dsn = value.strip().strip('"').strip("'")
                    break
    if not dsn:
        print("  No DATABASE_URL, and no .env beside this file holding one.\n"
              "  It is the same connection worker.py uses, so if the worker\n"
              "  runs in a window, this will too.")
        return 2

    HERE.mkdir(exist_ok=True)
    kept = HERE / f"{engagement_id}.json"

    # Not "with psycopg.connect(...) as conn": leaving that block commits.
    conn = psycopg.connect(dsn)
    try:
        now = run_them_all(Sandbox(conn), engagement_id)
    finally:
        conn.rollback()
        conn.close()

    if what == "record":
        kept.write_text(json.dumps(now, indent=1, default=str))
        ran = sum(1 for k, v in now.items()
                  if k != "_pipelines" and v.get("ran"))
        print(f"  wrote {kept}")
        print(f"  {ran} analyses ran, "
              f"{len(now['_pipelines'])} pipeline stage rows")
        return 0

    if not kept.exists():
        print(f"  nothing recorded for this engagement yet. Run record first.")
        return 2
    said = differences(json.loads(kept.read_text()), now)
    if not said:
        print("  nothing changed")
        return 0
    print(f"  {len(said)} things changed:\n")
    for line in said[:120]:
        print(line)
    if len(said) > 120:
        print(f"  … and {len(said) - 120} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
