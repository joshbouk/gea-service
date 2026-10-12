"""Asking a model to read what people wrote.

Everything until now counted things. This reads them, and the difference is
that counting cannot be confidently wrong. A classification can: it comes
back fluent, structured and plausible whether or not it is true, and nothing
downstream can tell.

So the rules here are narrower than they would be for a chat.

  The model only ever labels. It is never asked for a rate, a verdict or a
  conclusion; those are computed from the labels in code, where the
  arithmetic can be checked. GEA-P03 says so explicitly: a verdict returned
  without all four measured rates is grounds for discarding the output.

  Every label carries the span of text it came from. A classification with
  no evidence span is dropped, not trusted, because the span is what a
  person checks during the validation pass at step 2.4.

  A record the model did not return is missing, not negative. Silently
  treating an unreturned record as a 'no' would turn a dropped batch into a
  finding about the client.

  Nothing is retried into existence. A batch that comes back unparseable
  twice is reported as unread, with its record ids.
"""
from __future__ import annotations

import json
import os
import time

import requests

# The model is recorded on every run. A figure in a report has to be
# traceable to what produced it, and "a model" is not an answer.
MODEL = os.environ.get("GEA_MODEL", "claude-sonnet-4-6")
API = "https://api.anthropic.com/v1/messages"
VERSION_API = "2023-06-01"

# Records per call. Small enough that one malformed reply costs little, large
# enough that a census does not take all afternoon.
BATCH = 20
ATTEMPTS = 2
TIMEOUT = 180


class NotConfigured(RuntimeError):
    """No API key. Not transient: retrying cannot help."""


class ModelError(RuntimeError):
    """The model could not be reached, or would not answer usefully."""


def check_configured() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise NotConfigured(
            "ANTHROPIC_API_KEY is not set, so the analyses that read what "
            "people wrote cannot run. See the comment at the top of "
            "worker.py.")


def _call(system: str, user: str, max_tokens: int = 8000) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise NotConfigured("ANTHROPIC_API_KEY is not set.")

    last = None
    for attempt in range(1, ATTEMPTS + 1):
        try:
            r = requests.post(
                API,
                headers={"x-api-key": key,
                         "anthropic-version": VERSION_API,
                         "content-type": "application/json"},
                json={"model": MODEL, "max_tokens": max_tokens,
                      "system": system,
                      "messages": [{"role": "user", "content": user}]},
                timeout=TIMEOUT)
            if r.status_code == 200:
                blocks = r.json().get("content", [])
                return "".join(b.get("text", "") for b in blocks
                               if b.get("type") == "text")
            # Overload and rate limiting are worth waiting out. A bad request
            # will be bad again in ten seconds.
            if r.status_code in (429, 500, 502, 503, 529):
                last = f"{r.status_code} {r.text[:200]}"
                time.sleep(3 * attempt)
                continue
            raise ModelError(f"the model refused the request: {r.status_code} "
                             f"{r.text[:300]}")
        except requests.RequestException as exc:
            last = str(exc)
            time.sleep(3 * attempt)
    raise ModelError(f"the model could not be reached: {last}")


def _parse(text: str) -> list[dict]:
    """Pull the JSON array out of a reply, or raise.

    Models wrap JSON in prose and fences however firmly you ask them not to.
    That is worth tolerating. Inventing structure that is not there is not.
    """
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```")[1]
        if t.startswith("json"):
            t = t[4:]
    start, end = t.find("["), t.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("no JSON array in the reply")
    rows = json.loads(t[start:end + 1])
    if not isinstance(rows, list):
        raise ValueError("the reply was not a list")
    return rows


def classify(records: list[dict], instructions: str, labels: list[str],
             heartbeat=None) -> tuple[dict[str, dict], list[str]]:
    """Label each record. Returns what came back, and what did not.

    records   each {"id": ..., "text": ...}
    labels    the questions to answer yes or no about each record
    """
    system = (
        "You classify customer relationship records for an assessment. You "
        "answer only with a JSON array, no prose before or after.\n\n"
        "For every record you are given, return an object with:\n"
        '  "id": the record id exactly as supplied\n'
        + "".join(f'  "{l}": "yes" or "no"\n' for l in labels)
        + '  "evidence": a quotation of under fifteen words from the record '
          "that supports your answers, or \"\" if there is none\n"
        '  "confidence": "high", "medium" or "low"\n\n'
        "Answer yes only where the record states it. A rep's assumption, an "
        "implication, or your own inference is a no. Do not give partial "
        "credit. Do not return a record you were not given, and do not omit "
        "one you were. Never return a rate, a total or a verdict: those are "
        "computed elsewhere from your labels.")

    out: dict[str, dict] = {}
    unread: list[str] = []
    batches = [records[i:i + BATCH] for i in range(0, len(records), BATCH)]

    for n, batch in enumerate(batches, 1):
        payload = json.dumps([{"id": r["id"], "record": r["text"][:4000]}
                              for r in batch], ensure_ascii=False)
        try:
            rows = _parse(_call(system, instructions + "\n\n" + payload))
        except (ValueError, json.JSONDecodeError, ModelError):
            unread.extend(r["id"] for r in batch)
            if heartbeat:
                heartbeat(n, len(batches))
            continue

        given = {r["id"] for r in batch}
        for row in rows:
            rid = str(row.get("id", "")).strip()
            if rid not in given:
                continue            # a record nobody asked about
            answers = {l: str(row.get(l, "")).strip().lower() for l in labels}
            if any(a not in ("yes", "no") for a in answers.values()):
                continue            # a label outside the set is not a label
            evidence = str(row.get("evidence", "")).strip()
            if not evidence:
                continue            # no span, no claim
            out[rid] = {"labels": answers, "evidence": evidence[:300],
                        "confidence": str(row.get("confidence", "")).lower()}
        # Anything in the batch the model did not return is unread, not a no.
        unread.extend(r["id"] for r in batch if r["id"] not in out)
        if heartbeat:
            heartbeat(n, len(batches))

    return out, unread
