from __future__ import annotations

import base64
import csv
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .vendor.judge_prompt import LLM_JUDGE_PROMPT


@dataclass(frozen=True)
class Case:
    id: str
    question: str
    gold: str
    category: str = ""


def load_cases(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        if path.suffix.lower() == ".csv":
            rows = list(csv.DictReader(handle))
        else:
            rows = [json.loads(line) for line in handle if line.strip()]
    cases = []
    for index, row in enumerate(rows):
        question, gold = row.get("prompt", row.get("question", "")), str(row.get("answer", ""))
        if row.get("canary"):
            key = row["canary"].encode()
            def decrypt(value):
                return bytes(b ^ key[i % len(key)] for i, b in enumerate(base64.b64decode(value))).decode()
            question, gold = decrypt(question), decrypt(gold)
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"Empty question at row {index}")
        cases.append(Case(str(row.get("id", index)), question, gold, row.get("type", "")))
    if len({c.id for c in cases}) != len(cases):
        raise ValueError("Duplicate dataset ids")
    return cases


async def grade(case, prediction, llm, judge_model):
    if not prediction:
        return {"status": "graded", "score": 0, "method": "empty_answer"}
    if prediction.strip() == case.gold.strip():
        return {"status": "graded", "score": 1, "method": "exact_match"}
    if not case.gold:
        return {"status": "unscored", "score": None, "reason": "no_reference_answer"}
    prompt = LLM_JUDGE_PROMPT.format(question=case.question, correct_answer=case.gold, response=prediction)
    try:
        reply = await llm.complete([{"role": "user", "content": prompt}], role="judge", model=judge_model)
        match = re.search(r"结论\s*[:：]\s*(正确|错误)", reply.content)
        if not match or reply.finish_reason == "length":
            raise ValueError("Judge verdict missing or truncated")
        return {"status": "graded", "score": int(match.group(1) == "正确"), "method": "xbench_prompt_newapi_judge", "response": reply.content}
    except Exception as error:
        return {"status": "judge_error", "score": None, "error": str(error)}


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def code_hash():
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.rglob("*.py")):
        digest.update(str(path.relative_to(Path(__file__).parent)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()
