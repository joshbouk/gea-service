"""What each file is, decided over the whole batch at once.

The rule-based version was rewritten four times in a day. Column names,
then the values in them, then Salesforce id prefixes, then what the keys
pointed at. Each fixed the last one's blind spot and opened a new one, and
by the fourth it was both slow and wrong: a contacts export proposed as
the deals export because it happened to be read first and had nothing to
compare itself against.

The fault was never the heuristics. It was reading one file at a time. A
client sends eight files together and they only make sense together: the
one with Role and Primary is contact roles *because* two others are deals
and contacts, and nothing about it says so on its own.

So the whole batch goes to a model in one call, with the twenty questions
the assessment asks. Deciding what a thing is from its context is what a
model does well and what a rule set does badly, and no amount of further
tuning was going to close that.

One rule survives. A Salesforce id names its own object in its first
three characters, and that is decisive, free and not worth asking about.
"""
from __future__ import annotations

import json

import model

# Per file, so a batch of eight stays inside a sensible prompt.
COLUMNS_SHOWN = 60
ROWS_SHOWN = 3
VALUE_LENGTH = 60


def best_rows(conn, dataset_id: str, columns: list[str],
              how_many: int = ROWS_SHOWN) -> list[dict]:
    """The fullest rows, not the first ones.

    A CRM export often opens with its thinnest records — a deal created
    last week with nothing on it yet, a contact with only an email. Those
    are the rows that happen to be at the top, and showing them makes
    every file look empty and alike.

    So the sample is chosen by how much is filled in. It costs one scan
    and it is the difference between a model seeing what a file is and
    seeing what it has in common with every other file.
    """
    if not columns:
        return []
    filled = " + ".join(
        f"(case when nullif(btrim(data ->> %s), '') is null then 0 else 1 end)"
        for _ in columns)
    with conn.cursor() as cur:
        cur.execute(f"""
            select data from dataset_row
             where dataset_id = %s
             order by ({filled}) desc, row_number
             limit %s
        """, (dataset_id, *columns, how_many))
        rows = [r[0] for r in cur.fetchall()]
    conn.rollback()
    return rows


def describe(conn, dataset_id: str, filename: str, row_count: int,
             columns: list[dict]) -> dict:
    """One file, as much as a model needs and no more."""
    # The most populated columns first: a file with three hundred columns
    # is mostly empty ones, and the full ones are what say what it is.
    ordered = sorted(columns,
                     key=lambda c: -(c.get("populated_rate") or 0))[:COLUMNS_SHOWN]
    names = [c["name"] for c in ordered]
    rows = best_rows(conn, dataset_id, names)

    return {
        "file": filename,
        "rows": row_count,
        "columns_in_total": len(columns),
        "columns_shown": names,
        "example_rows": [
            {n: str(r.get(n, ""))[:VALUE_LENGTH] for n in names if r.get(n)}
            for r in rows],
    }


SYSTEM = """You are helping a consultant work out which export each of a
client's data files is.

A client has sent several files from their CRM and other systems. The
filenames are often useless — report1.xls, or a Salesforce report title
nobody chose carefully. The columns and the values say what each one is.

You are given every file at once, because they only make sense together. A
file whose columns are Role and Primary is a contact roles export when two
of the other files are deals and contacts; on its own it could be
anything.

Rules:

- A file answers one export line, or none. Say none rather than guessing:
  a file placed wrongly corrupts every analysis that reads it, and a
  consultant choosing by hand costs thirty seconds.
- Two files can be two halves of one export, split to get under a column
  cap. Say so where the columns differ and a key column is shared.
- Give a reason a consultant can check by looking at the file. "The Stage
  column holds Closed Won and Closed Lost" can be verified. "High
  confidence" cannot.
- Confidence is clear, likely or unsure. Use unsure freely.

Reply with a JSON array and nothing else. One object per file:

  {"file": "<the filename exactly as given>",
   "export_ref": "<the ref, or null>",
   "confidence": "clear" | "likely" | "unsure",
   "reason": "<one sentence a consultant can check>",
   "also_serves": ["<other refs this file could also answer>"],
   "part_of": "<the filename this one continues, or null>"}
"""


def ask(files: list[dict], lines: list[dict]) -> list[dict]:
    """One call, every file, every question."""
    asked = "\n".join(
        f"  {l['ref']}  {l['question']}" for l in lines)
    described = json.dumps(files, ensure_ascii=False, indent=1)
    user = (f"The export lines this assessment asks for:\n\n{asked}\n\n"
            f"The files the client sent:\n\n{described}\n\n"
            f"Which export line does each file answer?")
    return model._parse(model._call(SYSTEM, user, max_tokens=4000))
