"""Which deals are the pipeline, and how they moved through it.

A client's opportunity table holds several motions at once. New business
works its way down a sales process. Renewals and auto-renewals are
created near the end of it, often in one move. Add-ons sit somewhere
between. Averaged together they produce a shape that is nobody's
pipeline: counts rising at late stages, conversion above a hundred per
cent, and a drawing that invites the reader to distrust the screen.

An earlier version of this file tried to tell them apart by the shape of
each deal's journey, counting how many stages it touched and where its
first record sat. That was inferring something the client already
records. VertexOne's export has a Type column reading Auto Renewal,
Renewal, Add On, New Business. A hundred and fifty one-stage deals at
Signature are auto-renewals and the file says so; thirty-seven one-stage
deals at Discover are new business and the file says that too. Same
shape, different things, and no arithmetic separates them as well as
reading the label.

So the funnel is the new-business deals, because that is what a sales
leader means by the pipeline. The other motions are counted beside it.

What journey shape is still good for is filling gaps inside a deal
already known to be new business. A seller who opens at Nurture because
the first two stages are administrative has still come through them:
seventy-two deals here start at Nurture or Champion and run to Contract,
twenty-four of them new business and fifty of them won. And a lead
worked to Discover and then disqualified belongs in the funnel as
leakage at the top, not set aside as unreadable.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

# What the client calls each motion, matched loosely because every CRM
# spells these differently.
RENEWAL_WORDS = ("renewal", "renew")
EXPANSION_WORDS = ("add on", "add-on", "addon", "upsell", "expansion",
                   "cross sell", "cross-sell")
NEW_BUSINESS_WORDS = ("new business", "new logo", "acquisition", "new")

THE_FUNNEL = "new business"
RENEWALS = "renewals"
EXPANSION = "expansion"
SOMETHING_ELSE = "other"


@dataclass
class Deal:
    """One deal's journey, as recorded, and what the client calls it."""
    deal_id: str
    deal_type: str | None = None
    at: dict = field(default_factory=dict)      # place -> date or None
    motion: str = SOMETHING_ELSE

    @property
    def places(self) -> list:
        return sorted(self.at)


def motion_of(deal_type) -> str:
    """Which motion this deal belongs to, from what the client calls it.

    Read rather than inferred. An unlabelled deal is not assumed to be
    new business: a client whose type field is empty is told the funnel
    could not be separated, which is true and useful, rather than shown
    a funnel with their renewals quietly inside it.
    """
    t = (deal_type or "").strip().lower()
    if not t:
        return SOMETHING_ELSE
    if any(w in t for w in RENEWAL_WORDS):
        return RENEWALS
    if any(w in t for w in EXPANSION_WORDS):
        return EXPANSION
    if any(w in t for w in NEW_BUSINESS_WORDS):
        return THE_FUNNEL
    return SOMETHING_ELSE


def split_by_motion(deals: dict) -> dict:
    out = defaultdict(list)
    for d in deals.values():
        d.motion = motion_of(d.deal_type)
        out[d.motion].append(d)
    return out


def through(deal: Deal, places: list, first_place: int) -> set:
    """Every place this deal passed through, recorded or not.

    From the top to the furthest it got. A seller who opens at the third
    stage because the first two are administrative has still come
    through them, and a deal recorded at the second and the fifth was at
    the third and fourth. Both are the same claim, and both are safe for
    a deal already known to be new business.

    A deal with nothing recorded passes through nothing, which keeps it
    out of every count rather than guessed into one.
    """
    if not deal.at:
        return set()
    return {p for p in places if first_place <= p <= max(deal.at)}


def measure(deals: list, places: list, first_place: int) -> dict:
    """Two numbers per stage, and the counts behind them.

    Days at the stage is the median over the deals that were in it and
    have a date on both sides. Never divided by deals that never
    arrived.

    Conversion is the share of the deals at the stage before that went
    on to reach this one. A deal that skipped the stage between still
    counts as having reached this one, because skipping is moving
    quickly rather than failing to convert.
    """
    reached = defaultdict(set)
    sat = defaultdict(list)
    for d in deals:
        for p in through(d, places, first_place):
            reached[p].add(d.deal_id)
        dated = sorted((p, w) for p, w in d.at.items() if w is not None)
        for (p, when), (_next_place, then) in zip(dated, dated[1:]):
            days = (then - when).days
            if 0 <= days <= 1095:
                sat[p].append(days)

    out = {}
    previous = None
    for p in places:
        here, days = reached.get(p, set()), sorted(sat.get(p, []))
        conversion = None
        if previous is not None and reached.get(previous):
            conversion = round(len(here & reached[previous])
                               / len(reached[previous]), 4)
        # Whether the median is worth reporting is a question about
        # coverage, not about size.
        #
        # A funnel narrows by design: nineteen deals at the last working
        # stage is the shape working, not a small sample. An absolute
        # floor of twenty-five refused to time exactly the stages where
        # deals get stuck, which is where a reader is looking.
        #
        # What makes a median thin is that few of the deals that got
        # here can be timed at all. Measured against the stage's own
        # population, a handful out of ninety is thin and nineteen out
        # of nineteen is complete.
        enough = len(days) >= 5 and (not here or len(days) >= len(here) * 0.15)
        out[p] = {
            "reached": len(here),
            "recorded": sum(1 for d in deals if p in d.at),
            "conversion": conversion,
            "median_days": days[len(days) // 2] if enough and days else None,
            "days_from": len(days),
        }
        if here:
            previous = p
    return out


def describe(populations: dict, name_of) -> dict:
    """The motions that are not the funnel, counted and placed.

    A hundred and fifty auto-renewals written straight into Signature is
    a fact about how this client's revenue arrives. Leaving it out would
    be a worse error than the one that put it inside the funnel.
    """
    def where(motion):
        at = defaultdict(int)
        for d in populations.get(motion, []):
            if d.at:
                at[min(d.at)] += 1
        return [{"stage": name_of(p), "deals": n}
                for p, n in sorted(at.items(), key=lambda kv: -kv[1])]

    return {
        "new_business": len(populations.get(THE_FUNNEL, [])),
        "renewals": len(populations.get(RENEWALS, [])),
        "expansion": len(populations.get(EXPANSION, [])),
        "not_labelled": len(populations.get(SOMETHING_ELSE, [])),
        "renewals_start_at": where(RENEWALS),
        "expansion_starts_at": where(EXPANSION),
    }
