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

from collections import defaultdict
from dataclasses import dataclass, field

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


@dataclass
class Match:
    column: str
    field_key: str
    score: float
    export_refs: list[str]


@dataclass
class Identification:
    """What a file appears to be, and how sure that is."""
    export_ref: str | None
    confidence: str                    # clear, likely, unsure, unknown
    matched: int
    of_fields: int
    key_column: str | None
    key_is: str | None                 # the canonical field the key matches
    also_serves: list[str] = field(default_factory=list)
    runner_up: str | None = None
    matches: list[Match] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "export_ref": self.export_ref,
            "confidence": self.confidence,
            "fields_matched": self.matched,
            "fields_the_line_wants": self.of_fields,
            "key_column": self.key_column,
            "key_is": self.key_is,
            "also_serves": self.also_serves,
            "runner_up": self.runner_up,
            "reason": self.reason,
            "matched_columns": [
                {"column": m.column, "field": m.field_key,
                 "score": round(m.score, 2)} for m in self.matches],
        }


def vocabulary(conn, engagement_id: str) -> list[dict]:
    """Every canonical field in this engagement's instrument."""
    with conn.cursor() as cur:
        cur.execute("""
            select cf.key, cf.name, cf.description, cf.expected_type::text,
                   cf.source_export_refs
              from canonical_field cf
              join engagement e on e.id = %s
                               and e.instrument_version_id = cf.instrument_version_id
        """, (engagement_id,))
        return [{"key": k, "name": n, "description": d, "type": t, "refs": r}
                for k, n, d, t, r in cur.fetchall()]


def line_sizes(conn, engagement_id: str) -> dict[str, int]:
    """How many fields each export line is meant to carry.

    A file matching eight fields means something different for a line that
    wants nine than for one that wants twenty-four.
    """
    out: dict[str, int] = defaultdict(int)
    for f in vocabulary(conn, engagement_id):
        for ref in f["refs"] or []:
            out[ref] += 1
    return dict(out)


def identify(columns: list[dict], vocab: list[dict], sizes: dict[str, int]
             ) -> Identification:
    """What is this file?

    columns wants, per column: name, populated_rate, distinct_count,
    row_count, inferred_type.
    """
    # Per column, the best score each export line can manage — not the
    # single best field overall.
    #
    # The matcher drops the entity from a canonical key so that "Owner"
    # finds opportunity_owner at all. The cost is that "Owner" then matches
    # account_owner and activity_owner exactly as well, and picking one
    # winner means a column votes for whichever line the vocabulary happens
    # to list first. A Salesforce opportunity report came out matching
    # three fields instead of eight that way.
    #
    # So an ambiguous column votes for every line it could belong to, and
    # the line with the most evidence overall wins. Which is the right
    # answer: eight columns that might be opportunity fields are an
    # opportunity export, even though each one alone is ambiguous.
    per_line_best: dict[str, dict[str, Match]] = defaultdict(dict)
    best_per_column: dict[str, Match] = {}
    for col in columns:
        best: Match | None = None
        for f in vocab:
            score = mapping.similarity(f["key"], col["name"])
            if mapping.is_exact(f["key"], col["name"]):
                score = max(score, 0.99)
            if score < COUNTS_TOWARDS:
                continue
            m = Match(col["name"], f["key"], score, f["refs"] or [])
            if best is None or score > best.score:
                best = m
            for ref in f["refs"] or []:
                held = per_line_best[ref].get(col["name"])
                if held is None or score > held.score:
                    per_line_best[ref][col["name"]] = m
        if best:
            best_per_column[col["name"]] = best

    if not best_per_column:
        return Identification(
            None, "unknown", 0, 0, None, None,
            reason="None of the column names matched a field the assessment "
                   "asks for. Either this is not a client export, or it uses "
                   "names nothing in the instrument recognises.")

    weight: dict[str, float] = defaultdict(float)
    count: dict[str, int] = defaultdict(int)
    for ref, by_col in per_line_best.items():
        for m in by_col.values():
            weight[ref] += m.score
            count[ref] += 1

    key_column, key_is = find_key(columns, best_per_column, vocab)

    ranked = sorted(weight.items(), key=lambda kv: -kv[1])
    top_ref, top_weight = ranked[0]
    runner_up, second_weight = (ranked[1] if len(ranked) > 1 else (None, 0.0))

    share = count[top_ref] / max(sizes.get(top_ref, 1), 1)
    if count[top_ref] < ENOUGH_MATCHES or share < ENOUGH_OF_THE_LINE:
        return Identification(
            top_ref, "unsure", count[top_ref], sizes.get(top_ref, 0),
            key_column, key_is,
            matches=sorted(best_per_column.values(), key=lambda m: -m.score),
            reason=(f"Only {count[top_ref]} column"
                    f"{'' if count[top_ref] == 1 else 's'} matched anything "
                    f"the assessment asks for, which is too little to say "
                    f"what this file is. The closest line is {top_ref}. "
                    f"Say which it is and the columns will be mapped from "
                    f"there."))

    if second_weight == 0:
        confidence = "clear"
    elif top_weight / second_weight >= CLEAR_ENOUGH:
        confidence = "clear"
    elif top_weight / second_weight >= 1.1:
        confidence = "likely"
    else:
        confidence = "unsure"

    # A line whose fields this file also carries, well enough to be worth
    # saying. Not the same as being that file: a deals export carrying the
    # account name does not replace the accounts export.
    also = [ref for ref, n in count.items()
            if ref != top_ref and n >= 3 and n / max(sizes.get(ref, 1), 1) >= 0.4]

    matched = count[top_ref]
    wanted = sizes.get(top_ref, 0)
    reason = (f"{matched} of the {wanted} fields this line asks for were "
              f"matched by name.")
    if confidence == "unsure" and runner_up:
        reason += (f" It matches {runner_up} almost as well, so this one "
                   f"needs a person to say which.")
    if key_column is None:
        reason += (" No column looks like a row identifier, so this file "
                   "cannot be joined to another.")

    return Identification(
        export_ref=top_ref, confidence=confidence, matched=matched,
        of_fields=wanted, key_column=key_column, key_is=key_is,
        also_serves=sorted(also), runner_up=runner_up,
        matches=sorted(best_per_column.values(), key=lambda m: -m.score),
        reason=reason)


def find_key(columns: list[dict], matched: dict[str, Match],
             vocab: list[dict]) -> tuple[str | None, str | None]:
    """The column that identifies a row.

    Fully populated, almost entirely distinct, and matching an identifier
    field. All three matter: a created date is distinct and is not a key, and
    a company name is neither.
    """
    id_fields = {f["key"] for f in vocab if f["type"] == "id"}
    best = None
    for col in columns:
        rows = col.get("row_count") or 0
        distinct = col.get("distinct_count") or 0
        populated = col.get("populated_rate") or 0
        if not rows or populated < KEY_POPULATED:
            continue
        if distinct / rows < KEY_DISTINCT:
            continue
        m = matched.get(col["name"])
        if m and m.field_key in id_fields:
            return col["name"], m.field_key
        if best is None:
            best = col["name"]
    # A unique, fully populated column that matches no identifier field is
    # still a usable key — plenty of exports lead with an unnamed record id.
    return (best, None) if best else (None, None)


def is_part_of(this: Identification, this_columns: set[str],
               other: Identification, other_columns: set[str]) -> str | None:
    """Whether two files are two passes of the same export.

    Which is the normal case in Salesforce, where a report caps at about a
    hundred columns and anything wider has to come out in halves.
    """
    if not this.export_ref or this.export_ref != other.export_ref:
        return None
    if not this.key_column or this.key_column != other.key_column:
        return None
    shared = (this_columns & other_columns) - {this.key_column}
    if len(shared) > max(2, len(this_columns) // 4):
        return None            # too much overlap: a reissue, not a part
    return (f"Both files are {this.export_ref} and share the key "
            f"{this.key_column}, with different fields either side of it. "
            f"They look like two passes of one export.")
