"""Reading a draft for the tells.

docs/report-style.md says what a Blend report sounds like. This enforces the
eleven rules in its section 6, which are the ones a program can actually
check.

Every rule here is mechanical. None of them asks a model whether the prose
is any good, because a model asked to grade its own writing says yes, and
because a rule that cannot be stated precisely cannot be argued with when it
fires on a sentence somebody likes.

What this cannot catch is a draft that breaks no rule and says nothing. That
is section 9 of the guide and it is a person's job: if the first paragraph of
a section could have been written about a different company without changing
a word, it is filler.

Each failure names the sentence. A checker that reports "three style issues"
sends somebody hunting through thirty pages.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Sentence splitting that survives "41%." and "Mr. Travis" and "v0.11".
_ABBREV = r"(?<!\bMr)(?<!\bMrs)(?<!\bMs)(?<!\bDr)(?<!\bNo)(?<!\bvs)(?<!\be\.g)(?<!\bi\.e)"
_SENTENCE = re.compile(rf"{_ABBREV}(?<=[.!?])\s+(?=[A-Z\"'])")


def sentences(text: str) -> list[str]:
    """Sentences, not lines.

    The first version of this split on newlines, so every sentence that
    wrapped was cut in two and any pattern spanning the break was invisible.
    Real documents wrap, and a tricolon with a line break in the middle of
    it is still a tricolon.

    Paragraphs are joined first. Headings, table rows and list items are
    read separately because they are not prose and the rules that apply to
    them are different.
    """
    out = []
    for para in re.split(r"\n\s*\n", text):
        lines = [l.strip() for l in para.split("\n") if l.strip()]
        prose, apart = [], []
        for l in lines:
            (apart if l.startswith(("#", "|", "-", "*", ">", "1.", "2.", "3."))
             else prose).append(l)
        joined = " ".join(prose)
        if joined:
            out.extend(x.strip() for x in _SENTENCE.split(joined) if x.strip())
        out.extend(apart)
    return out


def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


# Some tells are certain and some need an eye.
#
# An em-dash aside, a colon reveal, a rhetorical question and a banned word
# are wrong wherever they appear, so they refuse the draft.
#
# A three-part list is not. "A sponsor, their leadership team and a board"
# enumerates three real readers; "specific, measurable and owned" is
# rhythm. No program tells those apart, and a checker that refuses the
# first teaches people to ignore it. Those are raised for a person to look
# at and do not block anything.
#
# The first version of this made the tricolon a hard rule and it fired
# thirty-six times on the style guide itself, mostly on real lists.
ADVISORY = {"1", "S4-hedge"}


@dataclass
class Finding:
    rule: str
    what: str
    sentence: str
    fix: str

    @property
    def refuses(self) -> bool:
        return self.rule not in ADVISORY

    def as_dict(self) -> dict:
        return {"rule": self.rule, "what": self.what,
                "sentence": self.sentence[:300], "fix": self.fix,
                "refuses": self.refuses}


# Rule 1 — the tricolon ------------------------------------------------------
# "specific, measurable and owned". Three items joined by a final and or or,
# where each item is short. The loudest single tell there is.
# Both punctuations: "specific, measurable, and owned" and the British
# "specific, measurable and owned". The second is the commoner one here and
# the first version of this checker missed every instance of it.
_TRICOLON = re.compile(
    r"\b(\w+(?:\s+\w+){0,2}),\s+(\w+(?:\s+\w+){0,2}),?\s+(?:and|or)\s+"
    r"(\w+(?:\s+\w+){0,2})\b",
    re.I)

# Rule 2 — manufactured contrast ---------------------------------------------
_NOT_X_BUT_Y = [
    re.compile(r"\bis\s+not\s+(?:just|only|merely|simply)\b[^.]{0,80}?[—–-]\s*it", re.I),
    re.compile(r"\bisn'?t\s+(?:just|only|merely|simply)\b[^.]{0,80}?[—–-]\s*it", re.I),
    re.compile(r"\bnot\s+\w[^,.]{0,50},\s+but\s+(?:rather\s+)?\w", re.I),
    re.compile(r"\bit'?s\s+not\s+(?:about|that)\b[^.]{0,60}?[,—–-]\s*it'?s", re.I),
    # "isn't just failing to record it, they're actively obscuring it"
    re.compile(r"\b(?:is|are|isn'?t|aren'?t|was|were)\s+(?:not\s+)?"
               r"(?:just|only|merely|simply)\b[^.]{0,90}?,\s*"
               r"(?:they|it|he|she|we)'?(?:re|s)?\s+\w+", re.I),
]

# Rule 3 — the aside between dashes ------------------------------------------
_EM_ASIDE = re.compile(r"[^\s][ ]?[—–][ ]?[a-z]")
_EM_ANY = re.compile(r"\s[—–]\s|\w[—–]\w")

# Rule 4 — colon then reveal --------------------------------------------------
# A colon mid-sentence followed by a clause rather than a list.
_COLON_REVEAL = re.compile(r"[a-z,)\"']\s*:\s+[a-z][^,;]{10,}", re.I)

# Rule 5 — rhetorical questions ----------------------------------------------
_QUESTION = re.compile(r"\?\s*$")

# Rule 6 — scare quotes around a label ----------------------------------------
_SCARE = re.compile(r"(?<![\w.])[\"“']([a-z][\w\s-]{2,28})[\"”'](?![\w])")

# Rule 7 — participial throat-clear -------------------------------------------
_THROAT = re.compile(
    r"^(Looking at|Turning to|Considering|Examining|Analysing|Analyzing|"
    r"Having (?:said|established|reviewed)|When it comes to|In terms of|"
    r"With (?:regard|respect) to|Taking (?:a|into)|Diving into|Drilling into)\b",
    re.I)

# Rule 8 — alliteration in a heading ------------------------------------------
_HEADING = re.compile(r"^#{1,6}\s+(.*)$", re.M)

# Rule 9 — quotation length ---------------------------------------------------
_QUOTED = re.compile(r"[\"“]([^\"”]{8,})[\"”]")
QUOTE_WORD_LIMIT = 25

# Rule 11 — a recommendation in a findings section ----------------------------
_RECOMMENDS = re.compile(
    r"\b(?:should|needs? to|must|ought to|we recommend|recommend(?:ing|ed)?\s+that|"
    r"the client (?:should|needs)|consider(?:ing)?\s+(?:implementing|adopting|moving))\b",
    re.I)

# Section 4 of the guide ------------------------------------------------------
BANNED_WORDS = {
    "delve", "robust", "holistic", "seamless", "seamlessly", "synergy",
    "synergies", "ecosystem", "tapestry", "testament", "underscore",
    "underscores", "pivotal", "crucial", "crucially", "vital", "myriad",
    "plethora", "realm", "foster", "fostering", "cultivate", "cultivating",
    "embark", "elevate", "empower", "empowering", "streamline",
    "streamlining", "transformative", "paradigm", "granular",
}
BANNED_PHRASES = [
    "leverage", "leveraging", "leveraged",
    "navigate the", "navigating the", "the landscape", "landscape of",
    "unlock", "unlocking", "harness", "harnessing",
    "game-changing", "game changing", "best-in-class", "best in class",
    "world-class", "world class", "cutting-edge", "cutting edge",
    "state-of-the-art", "mission-critical", "actionable insight",
    "actionable insights", "key takeaway", "key takeaways", "deep dive",
    "double-click", "north star", "low-hanging fruit", "low hanging fruit",
    "move the needle", "boil the ocean", "at the end of the day",
    "it's worth noting", "it is worth noting", "it's important to",
    "it is important to", "in today's", "in the modern era",
    "as organisations increasingly", "as organizations increasingly",
    "that said", "having said that", "needless to say",
]
BANNED_OPENERS = ("Notably", "Importantly", "Interestingly", "Crucially",
                  "Significantly", "Ultimately", "Furthermore", "Moreover",
                  "Additionally", "In conclusion", "To summarise",
                  "To summarize", "Overall")
# Only where a number is in the same sentence: if the figure is there, the
# claim can be made.
HEDGES = ("appear to", "appears to", "seem to", "seems to", "suggest that",
          "suggests that", "may indicate", "might indicate", "could indicate",
          "could potentially", "tend to", "tends to", "it would appear",
          "arguably", "relatively", "somewhat")
_HAS_NUMBER = re.compile(r"\d")


def _alliterative(heading: str) -> bool:
    words = [w for w in re.findall(r"[A-Za-z]{4,}", heading)]
    if len(words) < 3:
        return False
    letters = [w[0].lower() for w in words]
    for i in range(len(letters) - 2):
        if letters[i] == letters[i + 1] == letters[i + 2]:
            return True
    return False


def check(text: str, section: str = "", findings_section: bool = False
          ) -> list[Finding]:
    """Read a draft and return everything that breaks a hard rule."""
    out: list[Finding] = []

    def add(rule, what, sentence, fix):
        out.append(Finding(rule, what, sentence.strip(), fix))

    for s in sentences(text):
        low = s.lower()

        m = _TRICOLON.search(s)
        if m and all(len(g.split()) <= 3 for g in m.groups()):
            add("1", "a three-part list", s,
                "Count the items. If the third was added for rhythm, cut it.")

        for pat in _NOT_X_BUT_Y:
            if pat.search(s):
                add("2", "manufactured contrast", s,
                    "Say what the thing is, without saying what it is not.")
                break

        if _EM_ASIDE.search(s) or _EM_ANY.search(s):
            add("3", "an aside between dashes", s,
                "Either it is the point, so make it a sentence, or it is "
                "padding, so cut it.")

        if _COLON_REVEAL.search(s):
            add("4", "a colon before the reveal", s,
                "Drop the run-up and say the second half.")

        if _QUESTION.search(s):
            add("5", "a rhetorical question", s,
                "The reader is paying for answers.")

        for m in _SCARE.finditer(s):
            if m.group(1).lower() not in ("other", "n/a", "closed won",
                                          "closed lost"):
                add("6", f"scare quotes around {m.group(1)!r}", s,
                    "Name the thing plainly or drop the quotes.")
                break

        if _THROAT.search(s):
            add("7", "a participial throat-clear", s,
                "Start with what was found.")

        if findings_section and _RECOMMENDS.search(s):
            add("11", "a recommendation in a findings section", s,
                "This belongs in the roadmap. Findings say what is, not "
                "what to do.")

        for w in re.findall(r"[A-Za-z'’-]+", low):
            if w in BANNED_WORDS:
                add("S4", f"the word {w!r}", s, "Use the ordinary word.")
                break
        for phrase in BANNED_PHRASES:
            if phrase in low:
                add("S4", f"the phrase {phrase!r}", s,
                    "Say it plainly instead.")
                break
        if s.split(",")[0].strip() in BANNED_OPENERS or \
                any(s.startswith(o + ",") or s.startswith(o + " ")
                    for o in BANNED_OPENERS):
            add("S4", "a signposting opener", s,
                "Delete the first word. The sentence is stronger.")
        if _HAS_NUMBER.search(s):
            for h in HEDGES:
                if h in low:
                    add("S4-hedge", f"the hedge {h!r} beside a figure", s,
                        "The number is there. Make the claim.")
                    break

    # Quotations are checked against the whole text: one that runs across a
    # line break is split by the sentence reader and would never be seen.
    for m in _QUOTED.finditer(" ".join(text.split())):
        words = m.group(1).split()
        if len(words) > QUOTE_WORD_LIMIT:
            add("9", f"a quotation of {len(words)} words", m.group(1),
                f"Cut it to {QUOTE_WORD_LIMIT} words or paraphrase. A long "
                f"quotation is usually the consultant avoiding the work of "
                f"saying what it means.")

    for h in _HEADING.findall(text):
        if _alliterative(h):
            add("8", "alliteration in a heading", h,
                "A heading is a label, not a slogan.")

    return out


def report(text: str, section: str = "", findings_section: bool = False) -> dict:
    found = check(text, section, findings_section)
    refusals = [f for f in found if f.refuses]
    advisories = [f for f in found if not f.refuses]
    by_rule: dict[str, int] = {}
    for f in refusals:
        by_rule[f.rule] = by_rule.get(f.rule, 0) + 1
    return {
        "section": section,
        "passes": not refusals,
        "refused": len(refusals),
        "to_look_at": len(advisories),
        "by_rule": by_rule,
        "findings": [f.as_dict() for f in refusals],
        "advisories": [f.as_dict() for f in advisories],
        "words": len(text.split()),
    }
