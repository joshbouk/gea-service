"""Proposing which of the client's columns is which canonical field.

A HubSpot contact export runs to five hundred columns. The instrument needs a
hundred and twenty-three specific things. Matching those by hand is a day of
work and the kind of task people rush at four o'clock on a Friday.

So the service proposes, and a person decides. Three rules shape it.

**Only look where the field could be.** Every canonical field declares the
export lines it comes from. A loss reason lives in the deals export, so it is
never matched against a contact file. That single constraint removes most of
the wrong answers before any matching happens.

**An empty column is not a missing column.** A client whose `loss_reason`
column exists and is blank on every row is a different finding from one who
has no such field. The first is a discipline problem, the second a
configuration one, and they lead to different conversations. Both are
proposed; the note says which is which.

**Every cap-bearing field goes to a person, whatever the confidence.** Those
are the fields that cap a score when absent. A machine being confident is not
the same as somebody having looked.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

# Words that appear in so many CRM column names they carry no signal.
NOISE = {"the", "of", "a", "id", "field", "value", "custom", "property",
         "hs", "sf", "c", "x"}

# Confidence bands. A proposal below LOW is not offered at all: a bad
# suggestion costs more attention than no suggestion.
HIGH, MEDIUM, MEDIUM_LOW, FLOOR = 0.92, 0.75, 0.55, 0.55


def normalise(name: str) -> str:
    """Strip a column name back to its words.

    'Deal_Owner__c', 'deal owner' and 'DealOwner' all become 'deal owner'.
    """
    s = re.sub(r"__c$|^hs_|^sf_", "", name.strip(), flags=re.I)
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)          # dealOwner -> deal Owner
    s = re.sub(r"[^A-Za-z0-9]+", " ", s)
    return " ".join(s.lower().split())


def tokens(name: str) -> set[str]:
    return {t for t in normalise(name).split() if t not in NOISE} or \
           set(normalise(name).split())


ENTITIES = {"opportunity", "account", "contact", "subscription", "deal",
            "company", "line", "item", "activity", "note", "form", "forecast",
            "stage", "history"}

# Words that say what kind of thing a field is rather than which thing. Every
# entity in a CRM has a name, an id, a type and an owner, so these only mean
# something while their entity is still attached. Dropping "account" from
# account_name leaves "name", which matches the deal's name just as well —
# and that mistake, auto-accepted, would quietly mis-score the engagement.
STRUCTURAL = {"name", "id", "type", "owner", "date", "status", "code", "count",
              "number", "key", "ref", "label", "title", "created", "modified",
              "updated", "at", "by"}


def similarity(canonical: str, column: str) -> float:
    """How alike two names are, from 0 to 1.

    Canonical keys name the entity as well as the field — opportunity_close_date
    — while a CRM export usually does not, because the whole file is that
    entity. The bare field name is tried as well, and the better score wins.
    """
    best = _similarity(canonical, column)
    words = normalise(canonical).split()
    if len(words) > 1 and words[0] in ENTITIES:
        rest = words[1:]
        # Only drop the entity when what remains still names something. An
        # amount or an annual revenue is distinctive on its own; a name is not.
        if not all(w in STRUCTURAL for w in rest):
            best = max(best, _similarity(" ".join(rest), column))
    return best


def _similarity(canonical: str, column: str) -> float:
    a, b = normalise(canonical), normalise(column)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0

    ta, tb = tokens(canonical), tokens(column)
    overlap = len(ta & tb) / len(ta | tb) if ta and tb else 0.0
    # Every word of the canonical name appearing in the column name is a
    # strong signal even when the column has extra words round it.
    contains = 1.0 if ta and ta <= tb else 0.0
    sequence = SequenceMatcher(None, a, b).ratio()

    return max(contains * 0.95, overlap * 0.9 + sequence * 0.1, sequence * 0.85)


@dataclass
class Column:
    name: str
    inferred_type: str | None
    populated_rate: float
    populated_count: int
    distinct_count: int | None
    dataset_id: str
    export_ref: str


@dataclass
class Proposal:
    canonical_field_id: str
    canonical_key: str
    dataset_id: str | None
    proposed_field_name: str | None
    confidence: str | None
    evidence_note: str
    alternatives: list[dict]
    is_cap_bearing: bool
    proposed_is_empty: bool = False

    @property
    def status(self) -> str:
        """High confidence maps itself. Two things stop it.

        A cap-bearing field always waits for a person, because the cost of
        getting one wrong is a score ceiling nobody chose.

        A column that is empty in every row waits too, however exactly its
        name matches. "The field exists and nobody fills it in" is a finding
        about how the client works, and somebody has to decide whether that
        counts as mapped or as absent. Accepting it silently means the first
        anyone hears of it is an analysis returning nothing.
        """
        if self.proposed_field_name is None:
            return "proposed"
        if self.is_cap_bearing or self.proposed_is_empty:
            return "proposed"
        return "auto_accepted" if self.confidence == "high" else "proposed"


TYPE_AGREEMENT = {
    "date": {"date"},
    "timestamp": {"date"},
    "number": {"number"},
    "integer": {"number"},
    "currency": {"number"},
    "boolean": {"boolean"},
    "text": {"text", "free text"},
    "enum": {"text"},
}


def band(score: float) -> str:
    if score >= HIGH:
        return "high"
    if score >= MEDIUM:
        return "medium"
    if score >= MEDIUM_LOW:
        return "medium_low"
    return "low"


def propose(canonical_key: str, description: str | None,
            expected_type: str | None, is_cap_bearing: bool,
            canonical_field_id: str, columns: list[Column]) -> Proposal:
    """Pick the best column for one canonical field, and say why."""
    scored: list[tuple[float, Column, list[str]]] = []

    for col in columns:
        base = similarity(canonical_key, col.name)

        good: list[str] = []
        bad: list[str] = []
        score = base

        expected = (expected_type or "").lower()
        if expected and col.inferred_type and col.populated_count > 0:
            wanted = TYPE_AGREEMENT.get(expected)
            if wanted and col.inferred_type in wanted:
                score = min(1.0, score + 0.05)
                good.append(f"holds {col.inferred_type} values, as expected")
            elif wanted:
                score -= 0.12
                bad.append(
                    f"holds {col.inferred_type} values where {expected} was expected")

        # A column that is empty everywhere is still the right column if the
        # name matches. It scores slightly lower so a populated near-match
        # wins, but it is never discarded: an empty field is the finding.
        if col.populated_count == 0:
            score -= 0.08
            bad.append("is empty in every row")
        elif col.populated_rate < 0.5:
            bad.append(
                f"is populated on only {col.populated_count:,} rows "
                f"({col.populated_rate:.0%})")
        else:
            good.append(f"is populated on {col.populated_count:,} rows")

        if score > 0:
            scored.append((score, col, (good, bad)))

    scored.sort(key=lambda s: (-s[0], s[1].name))
    viable = [s for s in scored if s[0] >= FLOOR]

    if not viable:
        return Proposal(
            canonical_field_id, canonical_key, None, None, None,
            evidence_note=(
                f"No column in the {', '.join(sorted({c.export_ref for c in columns})) or 'uploaded'} "
                f"export looks like {canonical_key}."
                if columns else
                f"The export {canonical_key} comes from has not been uploaded yet."),
            alternatives=[], is_cap_bearing=is_cap_bearing)

    best_score, best, (good, bad) = viable[0]
    if normalise(canonical_key) == normalise(best.name):
        note = f'The column "{best.name}" is this field exactly'
    else:
        note = f'"{best.name}" is the closest column name'
    if good:
        note += ", and " + ", and ".join(good)
    if bad:
        note += (". It " if good else ", but it ") + "; it ".join(bad)
    note += "."

    return Proposal(
        canonical_field_id, canonical_key, best.dataset_id, best.name,
        band(best_score), note,
        proposed_is_empty=best.populated_count == 0,
        alternatives=[{"field_name": c.name,
                       "confidence": band(s),
                       "populated_rate": round(c.populated_rate, 4),
                       "inferred_type": c.inferred_type}
                      for s, c, _ in viable[1:4]],
        is_cap_bearing=is_cap_bearing)
