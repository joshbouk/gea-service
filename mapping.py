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
NOISE = {"the", "of", "a", "field", "value", "custom", "property",
         "hs", "sf", "c", "x"}

# Confidence bands. Nothing below medium is offered as a proposal: a weak
# suggestion costs more attention than no suggestion, and teaches people to
# skim.
HIGH, MEDIUM, MEDIUM_LOW, FLOOR = 0.92, 0.75, 0.55, 0.75

# Near-misses are still kept, as candidates rather than answers. A column that
# scores 0.69 is not good enough to suggest and is far too useful to discard:
# when someone goes looking, it belongs at the top of the list rather than
# buried among five hundred others. Nothing here is ever pre-selected.
RESEARCH_FLOOR = 0.40


# Words that say where a column points rather than what it holds.
#
# HubSpot names a lookup "Associated Company ID" and Salesforce writes
# "Account: Account Name" on a report. Counted as words, they drag a
# correct match below the floor: account_industry against "Associated
# Company Industry" scores 0.67 on three tokens against two, and the
# match is exact once the word that means nothing is gone.
LOOKUP_NOISE = {"associated", "associate", "related", "parent", "primary",
                "lookup", "linked"}


def normalise(name: str) -> str:
    """Strip a column name back to its words.

    'Deal_Owner__c', 'deal owner' and 'DealOwner' all become 'deal owner'.
    """
    s = re.sub(r"__c$|^hs_|^sf_", "", name.strip(), flags=re.I)
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)          # dealOwner -> deal Owner
    s = re.sub(r"[^A-Za-z0-9]+", " ", s)
    return " ".join(s.lower().split())


def stem(word: str) -> str:
    """Reduce a word to its root, crudely but enough.

    A canonical key says created_at where the client's column says Create
    Date. Treating those as unrelated words made a correct match score low
    enough to be thrown away, which is a bug wearing the costume of caution.
    """
    for suffix in ("ies", "ied"):
        if len(word) > 4 and word.endswith(suffix):
            return word[:-3] + "y"
    if word.endswith("ss"):
        return word          # loss, address, process: the s is part of the word
    for suffix in ("ing", "ed", "es"):
        if len(word) > 5 and word.endswith(suffix):
            return word[: -len(suffix)]
    for suffix in ("s", "d"):
        if len(word) > 4 and word.endswith(suffix):
            word = word[: -len(suffix)]
            break
    # create and created both land on creat only if the trailing e goes too.
    return word[:-1] if len(word) > 4 and word.endswith("e") else word


def tokens(name: str) -> set[str]:
    words = [stem(w) for w in normalise(name).split()]
    return {w for w in words if w not in NOISE} or set(words)


ENTITIES = {"opportunity", "account", "contact", "subscription", "deal",
            "company", "line", "item", "activity", "note", "form", "forecast",
            "stage", "history"}

# The same thing under two names, because two CRMs named it differently.
#
# HubSpot says Deal and Company where Salesforce says Opportunity and
# Account, and the canonical keys had to pick one. Without this, every
# HubSpot export matches badly for a reason that has nothing to do with the
# client's data: "Deal Stage" and opportunity_status share no word at all.
#
# Only words that are genuinely interchangeable in a CRM. A near-synonym
# here would auto-accept a wrong column, which is the one mistake this
# matcher must not make.
SAME_THING = {
    "deal": "opportunity",
    "deals": "opportunity",
    "opp": "opportunity",
    "opportunities": "opportunity",
    "company": "account",
    "companies": "account",
    "organisation": "account",
    "organization": "account",
    "org": "account",
    "stage": "status",
    "person": "contact",
    "people": "contact",
    "engagement": "activity",
    "engagements": "activity",
    "amount": "value",
    "revenue": "value",
}


def canonicalise(word: str) -> str:
    return SAME_THING.get(word, word)

# Words that say what kind of thing a field is rather than which thing. Every
# entity in a CRM has a name, an id, a type and an owner, so these only mean
# something while their entity is still attached. Dropping "account" from
# account_name leaves "name", which matches the deal's name just as well —
# and that mistake, auto-accepted, would quietly mis-score the engagement.
STRUCTURAL = {"name", "id", "type", "owner", "date", "status", "code", "count",
              "number", "key", "ref", "label", "title", "created", "modified",
              "updated", "at", "by"}


def similarity(canonical: str, column: str, drop_entity: bool = True) -> float:
    """How alike two names are, from 0 to 1.

    Canonical keys name the entity as well as the field — opportunity_close_date
    — while a CRM export usually does not, because the whole file is that
    entity. The bare field name is tried as well, and the better score wins.
    """
    best = _similarity(canonical, column)

    # Dropping the entity is what lets a bare "Owner" in the deals file
    # find opportunity_owner. Across files it is the opposite of helpful:
    # with both entities dropped, "Deal Name" matches account_name exactly
    # as well as it matches opportunity_name, and every CRM object has a
    # name, an owner and a created date.
    #
    # So within the file a field belongs to, drop it. Everywhere else,
    # insist on it.
    if not drop_entity:
        return best

    words = [canonicalise(w) for w in normalise(canonical).split()]
    if len(words) > 1 and words[0] in ENTITIES:
        # Dropping the entity helps find the column at all. It can no longer
        # cause a silent accept, because only an exact name does that.
        best = max(best, _similarity(" ".join(words[1:]), column))
    return best


def _similarity(canonical: str, column: str) -> float:
    a = " ".join(stem(canonicalise(w)) for w in normalise(canonical).split()
                 if w not in LOOKUP_NOISE)
    b = " ".join(stem(canonicalise(w)) for w in normalise(column).split()
                 if w not in LOOKUP_NOISE)
    if not a or not b:
        return 0.0
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

    # Extra words dilute a containment match. Two or three are a CRM being
    # verbose; ten mean the column is measuring something about the field
    # rather than being it.
    if contains and len(tb) > len(ta):
        contains *= max(0.0, 1.0 - 0.18 * (len(tb) - len(ta)))

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


def is_exact(canonical_key: str, column_name: str) -> bool:
    """Is the column named this field, allowing for the entity prefix?

    contact_job_title and a column called "Job Title" are the same thing.
    contact_id and a column called "Contact Source" are not, however well
    they score.
    """
    a, b = normalise(canonical_key), normalise(column_name)
    if a == b:
        return True
    words = a.split()
    return len(words) > 1 and words[0] in ENTITIES and " ".join(words[1:]) == b


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
    proposed_is_exact: bool = False

    @property
    def status(self) -> str:
        """Only an exact name maps itself. Everything else waits for a person.

        Confidence alone is not enough. A column called "Contact Source"
        scores highly against contact_id and is a different thing entirely,
        and a silent wrong mapping is the most expensive mistake this screen
        can make: it is never reviewed, and every figure built on it is wrong
        in a way nobody traces back.

        Two further things stop even an exact name from accepting itself. A
        cap-bearing field, because the cost of getting one wrong is a score
        ceiling nobody chose. And a column empty in every row, because "the
        field exists and nobody fills it in" is a finding somebody has to
        rule on.
        """
        if self.proposed_field_name is None:
            return "proposed"
        if self.is_cap_bearing or self.proposed_is_empty:
            return "proposed"
        return "auto_accepted" if self.proposed_is_exact else "proposed"


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
            canonical_field_id: str, columns: list[Column],
            own_exports: set[str] | None = None) -> Proposal:
    """Pick the best column for one canonical field, and say why.

    own_exports names the export lines this field is meant to come from.
    Columns from those files are preferred; columns from anywhere else are
    still considered, because a deals export carrying associated company
    columns genuinely does serve the companies line.

    The preference is what keeps them apart. Every CRM object has a
    created date, so Created Date in the deals export matches
    account_created_at exactly as well as it matches
    opportunity_created_at. Without the preference the answer depends on
    which file was read first.
    """
    scored: list[tuple[float, Column, list[str]]] = []
    own = own_exports or set()

    for col in columns:
        in_own_file = not own or col.export_ref in own
        base = similarity(canonical_key, col.name, drop_entity=in_own_file)

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

        if own and not in_own_file and score >= RESEARCH_FLOOR:
            good.append(
                f"is in the {col.export_ref or 'uploaded'} export rather "
                f"than the one this field comes from, and still names it")

        if score > 0:
            scored.append((score, col, (good, bad)))

    scored.sort(key=lambda s: (-s[0], s[1].name))
    viable = [s for s in scored if s[0] >= FLOOR]
    research = [s for s in scored if s[0] >= RESEARCH_FLOOR]

    def candidates(exclude: str | None) -> list[dict]:
        return [{"field_name": c.name,
                 "confidence": band(sc),
                 "populated_rate": round(c.populated_rate, 4),
                 "inferred_type": c.inferred_type}
                for sc, c, _ in research if c.name != exclude][:5]

    if not viable:
        return Proposal(
            canonical_field_id, canonical_key, None, None, None,
            alternatives=candidates(None),
            evidence_note=(
                f"No column in the {', '.join(sorted({c.export_ref for c in columns})) or 'uploaded'} "
                f"export looks like {canonical_key}."
                if columns else
                f"The export {canonical_key} comes from has not been uploaded yet."),
            is_cap_bearing=is_cap_bearing)

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
        proposed_is_exact=is_exact(canonical_key, best.name),
        alternatives=candidates(best.name),
        is_cap_bearing=is_cap_bearing)
