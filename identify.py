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


# What a column's values say about it, when its name says nothing.
#
# Name matching gets seven of nine real exports right. The two it misses
# are the ones whose columns are called Role, Stage, Entered — words every
# CRM object uses. The values settle both instantly: a column holding
# Closed Won and Closed Lost is a deal stage, and one holding Decision
# Maker is a contact role.
#
# Salesforce is easier still. The first three characters of an id say what
# object it belongs to, so a key column starting 006 is an opportunity
# export whoever renamed its columns.
VALUE_SIGNATURES = [
    ("T1-01", ("closed won", "closed lost", "closedwon", "closed/won",
               "negotiation", "prospecting", "qualification")),
    ("T1-08", ("decision maker", "economic buyer", "champion", "influencer",
               "evaluator", "technical buyer", "executive sponsor")),
    ("T2-03", ("call", "email", "meeting", "task", "note logged",
               "inbound call", "outbound call")),
    ("T1-10", ("open - not contacted", "working - contacted", "unqualified",
               "mql", "sql", "marketing qualified lead")),
    ("T1-06", ("new", "open", "pending", "on hold", "solved", "escalated")),
    ("T3-08", ("commit", "best case", "pipeline", "omitted", "closed")),
]


def from_values(conn, dataset_id: str, columns: list[dict]) -> dict[str, float]:
    """Which export lines this file's own values point at."""
    votes: dict[str, float] = defaultdict(float)
    with conn.cursor() as cur:
        for col in columns:
            # A column with hundreds of distinct values is a name or an
            # amount, and tells us nothing about what the file is.
            distinct = col.get("distinct_count") or 0
            if distinct == 0 or distinct > 40:
                continue
            cur.execute("""
                select lower(btrim(r.data ->> %s)) as v, count(*)
                  from dataset_row r
                 where r.dataset_id = %s
                   and nullif(btrim(r.data ->> %s), '') is not null
                 group by 1 order by 2 desc limit 40
            """, (col["name"], dataset_id, col["name"]))
            seen = {v for v, _n in cur.fetchall()}
            if not seen:
                continue
            for ref, words in VALUE_SIGNATURES:
                hits = sum(1 for w in words if any(w in v for v in seen))
                if hits >= 2:
                    votes[ref] += hits
    conn.rollback()
    return dict(votes)


def from_salesforce_ids(conn, dataset_id: str, columns: list[dict]
                        ) -> tuple[str | None, str | None]:
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
    unique = [c for c in columns
              if (c.get("row_count") or 0)
              and (c.get("populated_rate") or 0) >= 0.99
              and (c.get("distinct_count") or 0) / (c.get("row_count") or 1)
                  >= KEY_DISTINCT]
    if len(unique) != 1:
        return None, None

    with conn.cursor() as cur:
        for col in unique:
            cur.execute("""
                select gea_id_prefix_object(r.data ->> %s)
                  from dataset_row r
                 where r.dataset_id = %s
                   and nullif(btrim(r.data ->> %s), '') is not null
                 limit 1
            """, (col["name"], dataset_id, col["name"]))
            row = cur.fetchone()
            if row and row[0]:
                conn.rollback()
                return row[0], col["name"]
    conn.rollback()
    return None, None


# Which export line each Salesforce object belongs to.
SALESFORCE_OBJECT_LINE = {
    "opportunity": "T1-01", "account": "T1-02", "contact": "T1-03",
    "lead": "T1-10", "case": "T1-06", "contract": "T1-07",
    "order": "T1-07", "task": "T2-03", "event": "T2-03",
    "opportunity_contact_role": "T1-08",
}


def identify(columns: list[dict], vocab: list[dict], sizes: dict[str, int],
             conn=None, dataset_id: str | None = None,
             engagement_id: str | None = None) -> Identification:
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

    # What the files beside it say.
    #
    # A Salesforce export set arrives together and references itself. A
    # file whose Opportunity IDs all appear in the deals export, and whose
    # Contact IDs all appear in the contacts export, is one of the two
    # lines that join deals to people — which takes twenty candidates down
    # to two before a single column name has been read.
    #
    # A filter rather than an answer. What separates contact roles from
    # activities is the values, and the narrowing is what makes reading
    # them decisive instead of suggestive.
    narrowed_to: list[str] = []
    if conn is not None and dataset_id is not None and engagement_id:
        pointed_at = set(from_the_other_files(conn, engagement_id,
                                              dataset_id, columns).values())
        narrowed_to = lines_that_reference(conn, engagement_id, pointed_at)
        if narrowed_to:
            weight = {r: w for r, w in weight.items() if r in narrowed_to} \
                or weight
            count = {r: n for r, n in count.items() if r in narrowed_to} \
                or count
            if weight:
                ranked = sorted(weight.items(), key=lambda kv: -kv[1])
                top_ref, top_weight = ranked[0]
                runner_up, second_weight = (ranked[1] if len(ranked) > 1
                                            else (None, 0.0))

    # What the values say, where there are rows to look at. This is the
    # half that settles a file whose column names are Role and Stage.
    if conn is not None and dataset_id is not None:
        obj, in_column = from_salesforce_ids(conn, dataset_id, columns)
        if obj and obj in SALESFORCE_OBJECT_LINE:
            ref = SALESFORCE_OBJECT_LINE[obj]
            return Identification(
                ref, "clear", count.get(ref, 0), sizes.get(ref, 0),
                in_column, None,
                also_serves=sorted(r for r, n in count.items()
                                   if r != ref and n >= 3),
                matches=sorted(best_per_column.values(), key=lambda m: -m.score),
                reason=(f"The {in_column} column holds Salesforce ids "
                        f"beginning {obj[:3]}, which is how Salesforce "
                        f"writes a {obj.replace('_', ' ')}. That settles it "
                        f"whatever the columns are called."))

        for ref, weight_from_values in from_values(conn, dataset_id, columns).items():
            weight[ref] += weight_from_values
            count[ref] = max(count.get(ref, 0), 1)
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


def from_the_other_files(conn, engagement_id: str, dataset_id: str,
                         columns: list[dict]) -> dict[str, str]:
    """What this file's keys say about it, given the files beside it.

    A Salesforce export set arrives together and the files reference each
    other. A file whose every Opportunity ID appears in the deals export's
    key column is about deals: contact roles, line items and stage history
    all announce themselves this way and none of them can be told apart by
    their column names, which are Role, Quantity and Stage.

    Returns the export line each foreign key points at. A file pointing at
    deals and at contacts is the contact roles export and nothing else is.
    """
    out: dict[str, str] = {}
    with conn.cursor() as cur:
        # The key column of every other file on the engagement, and which
        # line that file was taken for.
        cur.execute("""
            select d.id::text, el.ref, p.field_name
              from dataset d
              join artefact a on a.id = d.artefact_id and a.purged_at is null
              join export_request_line erl on erl.id = a.export_request_line_id
              join export_line el on el.id = erl.export_line_id
              join dataset_field_profile p on p.dataset_id = d.id
             where a.engagement_id = %s
               and d.id <> %s
               and d.parse_status = 'parsed'
               and p.populated_rate >= 0.99
               and d.row_count > 0
               and p.distinct_count::numeric / d.row_count >= %s
        """, (engagement_id, dataset_id, KEY_DISTINCT))
        others = cur.fetchall()

        for col in columns:
            # Only a column that looks like a reference: well populated,
            # and repeating. A unique column is this file's own key.
            rows = col.get("row_count") or 0
            if not rows or (col.get("populated_rate") or 0) < 0.5:
                continue
            if (col.get("distinct_count") or 0) / rows > 0.9:
                continue

            for other_ds, ref, other_col in others:
                cur.execute("""
                    select count(*) filter (where there.v is not null),
                           count(*)
                      from (select distinct nullif(btrim(r.data ->> %s), '') as v
                              from dataset_row r where r.dataset_id = %s
                             limit 300) mine
                      left join lateral (
                        select 1 as v from dataset_row o
                         where o.dataset_id = %s
                           and nullif(btrim(o.data ->> %s), '') = mine.v
                         limit 1) there on true
                     where mine.v is not null
                """, (col["name"], dataset_id, other_ds, other_col))
                found, looked = cur.fetchone()
                # Nearly all of them, or it is a coincidence of format.
                if looked >= 10 and found / looked >= 0.9:
                    out[col["name"]] = ref
                    break
    conn.rollback()
    return out


def lines_that_reference(conn, engagement_id: str, refs: set[str]) -> list[str]:
    """Which export lines join the things this file points at.

    No table of combinations is needed: the field keys say it. T1-08 wants
    contact_role_opportunity_id and contact_role_contact_id, so the
    contact roles export is a line that references deals and people, and
    a file whose keys point at both is one of the lines that does.

    A filter rather than an answer. Contact roles and activities both
    reference deals and people, and what separates those is the values —
    Decision Maker against Call and Email — which the rest of the
    identification already reads. Narrowing from twenty lines to two is
    what makes that reading decisive.
    """
    if not refs:
        return []
    with conn.cursor() as cur:
        cur.execute("""
            select cf.key, cf.source_export_refs
              from canonical_field cf
              join engagement e on e.id = %s
                               and e.instrument_version_id = cf.instrument_version_id
             where cf.key ~ '_(opportunity|contact|account|subscription)_id$'
        """, (engagement_id,))
        rows = cur.fetchall()
    conn.rollback()

    # What each line references, from the keys it wants.
    OWNER = {"_opportunity_id": "T1-01", "_contact_id": "T1-03",
             "_account_id": "T1-02", "_subscription_id": "T1-07"}
    references: dict[str, set] = defaultdict(set)
    for key, line_refs in rows:
        for suffix, points_at in OWNER.items():
            if key.endswith(suffix):
                for ref in (line_refs or []):
                    # A line does not reference itself: T1-01 holds
                    # opportunity_account_id and is not a file about
                    # accounts.
                    if ref != points_at:
                        references[ref].add(points_at)
    return sorted(ref for ref, pointed in references.items()
                  if pointed == refs)


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
