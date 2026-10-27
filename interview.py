"""Reading a conversation the way the consultant who held it would.

An hour with somebody who knows a business is the densest evidence an
assessment gets. The first version of this treated it as a form to tick:
for each question in the guide, was it asked, was it answered, and a
quotation under fifteen words. Thirty questions, thirty passes over the
same transcript, and an output that said almost nothing.

Run against a real interview it missed everything that mattered. The
client's revenue operations lead explained that a set of fields nobody
understood were retired, that their quoting tool had been uninstalled
after a failed implementation, that the sales process had thirteen
stages rather than the eight the data suggested, that the person who
appeared to be a system administrator owning three hundred renewals
actually ran the digital client success team, and that quota was set at
a hundred and seventy per cent of budget. Not one of those was an answer
to a question in the guide. They were volunteered, which is what people
do when they are talking rather than being surveyed, and the form had no
box for any of it.

So this reads the transcript whole, several times, each time with one
job:

  What was learned, against the elements this conversation serves. In
  enough words to use, with the words that were said attached.

  What this corrects. An assessment arrives at an interview carrying
  claims drawn from exports, and some of them are wrong. A client
  saying so is the most valuable thing in the hour and the previous
  version could not hear it.

  What is new. People, systems, processes and documents nobody knew
  existed. Each one is a thread somebody has to pull.

  What to ask next, and of whom. An interview raises questions for the
  other interviews, and those die in a consultant's memory unless
  something writes them down.

  And what was never asked, which the first version did do, and which
  is worth keeping.

Every claim carries a timestamp and the speaker's own words, and both
are checked against the transcript before anything is stored. A model
reading a hundred and fifty thousand characters will occasionally
produce a quotation that is a reasonable paraphrase of something nobody
said. Finding the timestamp and matching the words is cheap, and it is
the difference between evidence and plausible text.
"""
from __future__ import annotations

import json
import re
import unicodedata

import model

# A transcript longer than this is read in overlapping passes. Sonnet's
# context is far larger, but an hour of talk is about a hundred and
# fifty thousand characters and a model asked to hold four hundred
# thousand in mind answers the early part well and the late part badly.
READ_AT_ONCE = 180_000
OVERLAP = 6_000

# How close a quotation has to be to its claimed timestamp. Generous,
# because a model reports the timestamp of the exchange rather than of
# the sentence.
NEAR = 4_000


def _plain(s: str) -> str:
    """Words only, for comparing what was said to what was quoted.

    Transcripts are full of filler the model drops when quoting: "um",
    "uh", the stutters a verbatim service preserves. Matching on exact
    characters would reject true quotations, and matching too loosely
    would accept invented ones. Letters and digits, lowercased, with
    the filler removed, is the line that holds.
    """
    s = unicodedata.normalize("NFKD", s or "").lower()
    s = re.sub(r"\b(um|uh|er|ah|you know|i mean|like|kind of|sort of)\b", " ", s)
    return re.sub(r"[^a-z0-9]+", "", s)


def turns(transcript: str) -> list[tuple[str, str, str, str]]:
    """The transcript as what each person said, one turn at a time.

    Verification works on turns rather than flat text, for two reasons.
    A quotation running across a speaker change is not something
    anybody said: the interviewer interrupts, and a match against the
    whole transcript would stitch two people together and call it a
    quotation. And a real sentence is often broken in half by an
    interruption, so the halves have to be put back before a true
    quotation can be found at all.

    A turn therefore spans time rather than happening at an instant.
    Somebody explaining how contracting works talks for two minutes
    with four interjections in it, and that is one turn from its first
    timestamp to its last. Returns (first, last, speaker, what was
    said).
    """
    # Some services append the same conversation again with the
    # speakers unnamed. Reading it twice costs twice and tells the
    # model the same thing in two voices, so the second copy goes.
    mark = re.search(r"\n\[Attachment [^\]]*\]\n", transcript)
    if mark and "Unknown Speaker" in transcript[mark.end():mark.end() + 4000]:
        transcript = transcript[:mark.start()]

    found = re.findall(
        r"\[(\d+:\d\d:\d\d)\]\s*([^:\n]{1,60}?):\s*(.*?)(?=\n\n\[|\Z)",
        transcript, re.S)

    # first, last, speaker, text
    out: list[list[str]] = []
    for ts, who, said in found:
        who, said = who.strip(), " ".join(said.split())
        if not said:
            continue
        if out and out[-1][2] == who:
            out[-1][1], out[-1][3] = ts, out[-1][3] + " " + said
            continue
        # Stepped over a brief interruption, because people finish each
        # other's sentences: "ZZ, from my understanding-", "They're just
        # legacy", "...were retired fields" is one claim by one person
        # with four words of agreement inside it. Where the other person
        # said something substantial, the turn really did end.
        if (len(out) >= 2 and out[-2][2] == who
                and len(out[-1][3].split()) <= 8):
            out[-2][1], out[-2][3] = ts, out[-2][3] + " " + said
            continue
        out.append([ts, ts, who, said])
    return [(a, b, c, d) for a, b, c, d in out]


def _seconds(t):
    try:
        h, m, s = (int(x) for x in str(t).strip().split(":"))
        return h * 3600 + m * 60 + s
    except (ValueError, AttributeError):
        return None


def verify(claim: dict, spoken: list[tuple[str, str, str, str]]) -> str:
    """Whether the words claimed were really said, by one person, when.

    Four outcomes. Found in a turn whose span covers the stated time is
    evidence. Found in a turn elsewhere is true with a wrong citation,
    which matters when somebody clicks through to hear it. Found only
    across a speaker change is a quotation nobody said, assembled from
    two people. Found nowhere did not happen, however well it reads.
    """
    said = _plain(claim.get("quote", ""))
    if len(said) < 12:
        return "no quotation"

    want = _seconds(claim.get("at"))
    hits = [(a, b) for a, b, _who, text in spoken if said in _plain(text)]
    if not hits:
        if said in _plain(" ".join(text for _a, _b, _c, text in spoken)):
            return "not said by one person; it runs across a speaker change"
        return "not in the transcript"
    if want is None:
        return f"said at {hits[0][0]}, but no timestamp was given"
    # Within the turn, or a minute either side of it. A model cites the
    # line it read from, and a sentence may have begun just before.
    for first, last in hits:
        a, b = _seconds(first), _seconds(last)
        if a is not None and b is not None and a - 60 <= want <= b + 60:
            return "verified"
    return f"said at {hits[0][0]}, not {claim.get('at')}"


SHARED = (
    "You are reading the transcript of an interview conducted during a "
    "growth assessment of a business. The consultant who held it needs "
    "to find what is in it.\n\n"
    "Three rules hold for everything you return.\n\n"
    "Quote the speaker's own words, from the transcript, as they appear. "
    "Do not tidy them, do not merge two sentences, do not paraphrase "
    "into something cleaner. A quotation that reads better than the "
    "transcript is a quotation you invented.\n\n"
    "Give the timestamp of the exchange you took it from, in the form it "
    "appears in the transcript.\n\n"
    "Return nothing you cannot ground that way. An empty array is a "
    "correct answer. A plausible answer nobody said is not."
)


def _ask(system: str, user: str, tokens: int = 8000) -> list[dict]:
    text = model._call(system, user, max_tokens=tokens)
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t.startswith("json"):
            t = t[4:]
    start, end = t.find("["), t.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        rows = json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return []
    return [r for r in rows if isinstance(r, dict)]


def what_was_learned(transcript: str, who: str, role: str,
                     elements: list[dict]) -> list[dict]:
    """What this conversation says about the things being assessed.

    Not a quotation fragment. The substance, in enough words for a
    consultant to score an element from, with the words that support it
    attached. An element is a question about how a business works, and
    somebody who works there has just spent an hour answering.
    """
    menu = "\n".join(f"  {e['ref']}: {e['name']}" for e in elements)
    system = SHARED + (
        "\n\nReturn a JSON array. Each object:\n"
        '  "element": the reference of the element it bears on\n'
        '  "what": what this conversation establishes about it, in one '
        "or two sentences of your own words. Enough for somebody to act "
        "on without reading the transcript.\n"
        '  "quote": the speaker\'s words that establish it\n'
        '  "at": the timestamp\n'
        '  "how_sure": "stated" where they said it outright, "implied" '
        "where it follows from what they said, \"unclear\" where you are "
        "reading between lines\n\n"
        "Several entries may bear on one element, and most elements will "
        "have none. Do not stretch to cover the list. Do not score "
        "anything or say what any of it is worth.")
    user = (f"The interview is with {who}"
            f"{', ' + role if role else ''}.\n\n"
            f"The elements being assessed:\n{menu}\n\n"
            f"--- THE TRANSCRIPT ---\n{transcript}")
    return _ask(system, user)


def what_this_corrects(transcript: str, who: str,
                       beliefs: list[dict]) -> list[dict]:
    """Where the client contradicts what the data said.

    An assessment arrives at an interview carrying claims drawn from
    exports, and some are wrong. The exports showed a system
    administrator owning three hundred and fourteen renewal deals; the
    interview established that he runs the digital client success team
    and books them all day. The number was right and the conclusion was
    wrong, and only a person could say so.

    This is the highest-value thing an interview produces and the
    reason to hold one at all.
    """
    if not beliefs:
        return []
    menu = "\n".join(f"  - {b['claim']}" for b in beliefs)
    system = SHARED + (
        "\n\nYou are given things the assessment currently believes, "
        "drawn from the client's own data. Find where this conversation "
        "corrects, complicates or confirms any of them.\n\n"
        "Return a JSON array. Each object:\n"
        '  "belief": the belief it bears on, copied exactly\n'
        '  "verdict": "wrong", "incomplete", or "confirmed"\n'
        '  "what_they_said": what the conversation establishes instead, '
        "in your own words\n"
        '  "quote": their words\n'
        '  "at": the timestamp\n\n'
        "Only where the conversation genuinely bears on the belief. "
        "Silence is not a contradiction, and a belief nobody discussed "
        "is not in your answer at all.")
    user = (f"The interview is with {who}.\n\n"
            f"What the assessment currently believes:\n{menu}\n\n"
            f"--- THE TRANSCRIPT ---\n{transcript}")
    return _ask(system, user)


def what_is_new(transcript: str, known: dict) -> list[dict]:
    """People, systems, processes and documents nobody knew about.

    One hour produced a contracting lead nobody had been told about, a
    conversational intelligence tool, a sales methodology installed by
    an outside firm, a quoting tool mid-implementation, a capability
    worksheet that is the main reason contracting takes as long as it
    does, and a pricing workbook on its twentieth version. Every one is
    a thread somebody has to pull, and none appeared in any export.
    """
    system = SHARED + (
        "\n\nFind what this conversation reveals that an outsider "
        "reading the client's data would not know: people, systems, "
        "tools, processes, documents, projects.\n\n"
        "Return a JSON array. Each object:\n"
        '  "kind": "person", "system", "process", "document" or "project"\n'
        '  "name": what it is called\n'
        '  "what": what it is and why it matters here, in a sentence\n'
        '  "quote": their words\n'
        '  "at": the timestamp\n'
        '  "worth_chasing": "yes" where somebody should follow this up, '
        '"no" where it is context\n\n'
        "Leave out anything in the list of what is already known. Leave "
        "out people mentioned only in passing with no bearing on how the "
        "business runs.")
    already = (", ".join(sorted(known.get("names", [])))
               or "nothing recorded yet")
    user = (f"Already known: {already}\n\n"
            f"--- THE TRANSCRIPT ---\n{transcript}")
    return _ask(system, user)


def what_to_ask_next(transcript: str, who: str,
                     others: list[dict]) -> list[dict]:
    """The questions this hour raised for the other interviews.

    A consultant finishes an interview with three things to ask
    somebody else and loses two of them by Thursday. The people are
    known, the conversation just happened, and writing it down is the
    whole job.
    """
    if not others:
        return []
    menu = "\n".join(
        f"  {o['name']}"
        + (f", {o['role']}" if o.get("role") else "")
        + (" — already held" if o.get("held") else "")
        for o in others)
    system = SHARED + (
        "\n\nThis conversation will have raised things worth putting to "
        "somebody else on the list.\n\n"
        "Return a JSON array. Each object:\n"
        '  "who": the name of the person to ask, from the list\n'
        '  "question": the question, written as you would say it to them\n'
        '  "because": why this conversation raises it, in a sentence, '
        "including what was said\n"
        '  "quote": the words that raise it\n'
        '  "at": the timestamp\n\n'
        "Only where this interview genuinely raises it. Do not invent "
        "questions a guide would already cover. Where they named "
        "somebody not on the list, use their name anyway and say so in "
        "the reason.")
    user = (f"The interview just held was with {who}.\n\n"
            f"The others:\n{menu}\n\n"
            f"--- THE TRANSCRIPT ---\n{transcript}")
    return _ask(system, user)


def coverage(transcript: str, questions: list[dict]) -> list[dict]:
    """Which of the questions were put, and which were answered.

    The one thing the first version did that was worth keeping. A guide
    has questions for a reason, and consultants run out of time and skip
    the last ten minutes. The difference is that this reads the
    transcript once for all of them rather than once per question.
    """
    if not questions:
        return []
    menu = "\n".join(f"  [{q['id']}] {q['text']}" for q in questions)
    system = SHARED + (
        "\n\nFor each question in the guide, say whether it was put to "
        "the interviewee and whether what came back addressed it.\n\n"
        "Return a JSON array, one object per question:\n"
        '  "id": the id in brackets, exactly\n'
        '  "asked": "yes" where the question or something close to it '
        "was put, \"no\" otherwise\n"
        '  "answered": "yes" where what they said bears on it, "partly" '
        "where it was deflected or generalised, \"no\" otherwise\n"
        '  "quote": their words where there are any, otherwise ""\n'
        '  "at": the timestamp, or "" where it was never asked\n\n'
        "Return every question you were given and no others. A question "
        "answered before it was asked, because they volunteered it, "
        'counts as asked.')
    user = (f"The guide:\n{menu}\n\n--- THE TRANSCRIPT ---\n{transcript}")
    return _ask(system, user, tokens=12000)
