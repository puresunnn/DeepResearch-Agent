"""Small, source-grounded research state; never receives evaluation fields."""
from __future__ import annotations

import copy
import json
import re


STATE_INSTRUCTIONS = '''
Maintain a small constraint ledger, not a long reasoning transcript. Before your FIRST
action, include a compact block like this (replace the placeholders):
<research_state>{"constraints":[{"id":"c1","question_span":"verbatim question phrase","requirement":"fact to establish","interpretation":"","status":"open","finding":"","evidence":[]}]}</research_state>
Use 1-10 constraints covering the requested output and all restrictive clauses. Keep
explicit categories, exclusions (other/except), relations, temporal scope, population,
counting unit and first/last occurrence. Do not silently add nationality or narrow a
population. A named event's date and a country's founding date may have several meanings.
The verbatim question_span takes precedence over your paraphrase. Recheck exclusions
against that span before using a member as an endpoint. For extrema or counts, enumerate
the eligible population, excluded members and dated records before computing the result.
In subsequent turns include the tag only to update changed constraints, reusing IDs.
Updates need only id and changed fields; keep question_span and requirement unchanged.
Never emit a state block alone: always follow it with one tool_call or answer.
Status is open, supported, or conflicting. Evidence entries are
{"url":"downloaded source URL","quote":"short exact original passage"}. Search snippets discover candidates;
read the page before marking a critical constraint supported. A quote's existence does
not prove your interpretation: check entity, relation, scope, exceptions and completeness.
For derived findings cite the inputs and use calculate for arithmetic. For first/last
occurrence distinguish title from body, explicit naming from description, and check the
boundary and earlier coverage. For author questions inspect author order AND affiliations.
Before answering, update the ledger with findings and evidence. Seek the most discriminating
missing fact, not more pages repeating an accepted fact. When a candidate conflicts with
a hard constraint, search an alternative or a counterexample. Do not guess from popularity.
This optional state tag precedes the ONE tool_call or answer; it is not a tool action.
'''


def split_state(content):
    matches = list(re.finditer(r"<research_state>(.*?)</research_state>", content, re.S))
    if not matches and "research_state>" not in content:
        return content, None
    if len(matches) != 1:
        raise ValueError("Expected one complete research_state JSON block")
    match = matches[0]
    rest = content[:match.start()] + content[match.end():]
    if "research_state>" in rest:
        raise ValueError("Incomplete research_state block")
    try:
        return rest, json.loads(match[1])
    except ValueError as error:
        # A missing final object brace is an unambiguous metadata-only repair.
        body = match[1].strip()
        if body.startswith("{") and body.endswith("]"):
            try:
                repaired = json.loads(body + "}")
                if isinstance(repaired, dict) and set(repaired) == {"constraints"}:
                    return rest, repaired
            except ValueError:
                pass
        # Metadata failure must not discard an otherwise valid research action.
        return rest, {"_invalid_state": str(error)}


def question_anchor(question, span):
    if span and span in question:
        return span
    # Models often omit typographic quotes/spaces around a named entity. Restore
    # the actual source span only when its non-quote characters match uniquely.
    def retained(char):
        return not char.isspace() and char not in '\"\'“”‘’「」『』'
    positions = [i for i, char in enumerate(question) if retained(char)]
    normalized = "".join(question[i] for i in positions)
    target = "".join(char for char in span if retained(char))
    if not target or normalized.count(target) != 1:
        return ""
    start = normalized.index(target)
    return question[positions[start]:positions[start + len(target) - 1] + 1]


class ResearchState:
    def __init__(self, question):
        self.question = question
        self.constraints = {}
        self.revision = 0

    def update(self, patch, source_texts):
        if patch is None:
            return
        if not isinstance(patch, dict) or set(patch) != {"constraints"}:
            raise ValueError("research_state requires a constraints array")
        rows = patch["constraints"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 10:
            raise ValueError("Use 1..10 constraints")
        updated = copy.deepcopy(self.constraints)
        seen = set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Constraint must be an object")
            ident = row.get("id")
            if not isinstance(ident, str) or not re.fullmatch(r"c\d{1,2}", ident) or ident in seen:
                raise ValueError("Constraint IDs must be unique c1..c99")
            seen.add(ident)
            old = updated.get(ident, {})
            value = {**old, **row}
            if old:
                # Rewording an immutable label must not throw away valid findings.
                # Keep the original identity; semantic revisions belong in interpretation.
                value["question_span"] = old["question_span"]
                value["requirement"] = old["requirement"]
            for key in ("question_span", "requirement", "interpretation", "finding"):
                if not isinstance(value.get(key, ""), str) or len(value.get(key, "")) > 1200:
                    raise ValueError(f"Invalid constraint {key}")
                value.setdefault(key, "")
            value["question_span"] = question_anchor(self.question, value["question_span"])
            if not value["question_span"] or not value["requirement"]:
                raise ValueError("Constraint needs a verbatim question_span and a requirement")
            if value.get("status") not in {"open", "supported", "conflicting"}:
                raise ValueError("Constraint status must be open/supported/conflicting")
            refs = value.get("evidence", [])
            if not isinstance(refs, list) or len(refs) > 5:
                raise ValueError("At most five evidence passages per constraint")
            checked = []
            for ref in refs:
                if not isinstance(ref, dict):
                    raise ValueError("Evidence requires url and quote")
                url, quote = ref.get("url"), ref.get("quote")
                if not isinstance(url, str) or not isinstance(quote, str) or not 8 <= len(quote) <= 1600:
                    raise ValueError("Evidence needs URL and exact quote of 8..1600 characters")
                source = source_texts.get(url, "")
                start = source.find(quote)
                checked.append({"url": url, "quote": quote, "quote_found": start >= 0,
                                "char_start": start if start >= 0 else None})
            value["evidence"] = checked
            value["evidence_issue"] = ""
            if value["status"] == "supported" and (not value["finding"] or not checked or not all(r["quote_found"] for r in checked)):
                value["status"] = "open"
                value["evidence_issue"] = "Support needs a finding and exact passages from downloaded sources; use visit/find/read."
            updated[ident] = value
        if len(updated) > 10:
            raise ValueError("At most ten persistent constraints")
        if updated != self.constraints:
            self.revision += 1
        self.constraints = updated

    def snapshot(self):
        return {"constraints": list(self.constraints.values()), "revision": self.revision,
                "note": "Quote locations are checked by code; semantic support is model-assessed."}

    def gaps(self):
        return [row for row in self.constraints.values() if row["status"] != "supported"]

    def coverage(self):
        return {"total": len(self.constraints), "supported": sum(c["status"] == "supported" for c in self.constraints.values()),
                "unresolved": [c["id"] for c in self.gaps()]}

    def progress_keys(self):
        """Only source-backed progress counts; new snippets or wording do not."""
        return {(c["id"], c["status"], e["url"], e["char_start"])
                for c in self.constraints.values() for e in c["evidence"]
                if e["quote_found"] and c["status"] in {"supported", "conflicting"}}

    def guidance(self):
        if not self.constraints:
            return "Before finalizing, supply a compact research_state covering the original question and source evidence."
        gaps = self.gaps()
        rows = [{k: c[k] for k in ("id", "question_span", "requirement", "interpretation", "finding", "status", "evidence_issue")} for c in gaps]
        return ("Unresolved constraints (choose the next discriminating lookup; update state when resolved):\n"
                + json.dumps(rows, ensure_ascii=False)) if gaps else "All recorded constraints supported; check that the ledger covers every clause of the original question before answering."
