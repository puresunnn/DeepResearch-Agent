"""Runtime protocol and answer contracts; no benchmark answers or IDs."""
from __future__ import annotations

import ast
import json
import re

import json5

from .vendor.protocol import _extract_tool_call, _normalize_answer


PROTOCOL_REPAIR = '''No tool was executed. Correct only the rejected action's format.
Return exactly ONE action. A tool_call contains a JSON object, not XML child tags.
Valid example: <tool_call>{"name":"search","arguments":{"query":["your original query"]}}</tool_call>
Other tools: visit with {"url":["observed URL"],"goal":"needed fact"},
calculate with {"expression":"arithmetic"}. Preserve the intended tool and parameters.
find with {"url":"downloaded URL","text":"literal keyword","start":0},
read with {"url":"downloaded URL","start":0,"length":6000}.
An optional research_state JSON block may precede the action; use exact question spans.
If multiple different actions were proposed, choose one; do not claim any executed.
For a final answer use <answer>only the requested information</answer> with no tool call.
'''


def actionable_text(content):
    text = re.sub(r"<(think|decision)>.*?</\1>", "", content, flags=re.S)
    if re.search(r"</?(?:think|decision)>", text):
        raise ValueError("Unclosed reasoning/decision region")
    return text.split("<tool_response>", 1)[0].strip()


def _note(notes, step):
    if notes is not None:
        notes.append(step)


def _strict_json(text, notes):
    """Strict JSON is the contract; json5 is the tier-1 repair for quotes, commas and unquoted keys."""
    try:
        return json.loads(text)
    except ValueError:
        call = json5.loads(text)
        _note(notes, "relaxed_json")
        return call


def parse_action(content, finish_reason, notes=None):
    """notes, when passed, collects the tier-1 normalization steps this parse required."""
    if finish_reason == "length":
        raise ValueError("Truncated output; resend one complete action")
    text = actionable_text(content)
    if "<answer>" in text:
        match = re.fullmatch(r"<answer>(.*?)</answer>", text, flags=re.S)
        if not match or re.search(r"</?(?:answer|tool_call|function|name|arguments)[=>]", match[1]):
            raise ValueError("Answer must be complete and cannot contain another action")
        if not match[1].strip():
            raise ValueError("Empty answer")
        return "answer", match[1].strip()
    if text.count("<tool_call>") > 1 or len(re.findall(r"<function=", text)) > 1:
        raise ValueError("Multiple actions; return one complete action")
    if "<tool_call>" in text:
        match = re.fullmatch(r"<tool_call>(.*?)</tool_call>", text, flags=re.S)
        if not match:
            raise ValueError("Expected one complete tool_call wrapper")
        body = match[1].strip()
        xml = re.fullmatch(r"<name>\s*(search|visit|calculate|find|read)\s*</name>\s*<arguments>(.*?)</arguments>", body, flags=re.S)
        if xml:
            _note(notes, "xml_child_tags")
            call = {"name": xml[1], "arguments": _strict_json(xml[2], notes)}
        else:
            call = _strict_json(body, notes)
    elif text.startswith("<function="):
        if not re.fullmatch(r"<function=(search|visit|calculate)>.*?</function>", text, flags=re.S):
            raise ValueError("Expected one complete function wrapper")
        _note(notes, "function_wrapper")
        call = json.loads(_extract_tool_call(text))
    else:
        # Parse the whole object: never silently select the first of several calls.
        call = _strict_json(text, notes)
    if not isinstance(call, dict) or set(call) != {"name", "arguments"}:
        raise ValueError("Tool call requires only name and arguments")
    name, args = call["name"], call["arguments"]
    fields = {"search": {"query"}, "visit": {"url", "goal"}, "calculate": {"expression"},
              "find": {"url", "text", "start"}, "read": {"url", "start", "length"}}
    if not isinstance(name, str) or name not in fields or not isinstance(args, dict):
        raise ValueError("Unknown tool or arguments is not an object")
    required = {"search": "query", "visit": "url", "calculate": "expression", "find": "url", "read": "url"}[name]
    if required not in args or set(args) - fields[name]:
        raise ValueError(f"{name} requires {required}; unexpected argument fields")
    value = args[required]
    if name == "calculate":
        valid = isinstance(value, str) and bool(value.strip())
    else:
        valid = (isinstance(value, str) and bool(value.strip())) or (
            isinstance(value, list) and bool(value) and all(isinstance(v, str) and v.strip() for v in value))
    if not valid or ("goal" in args and not isinstance(args["goal"], str)):
        raise ValueError(f"Invalid {required}/goal value")
    if name in {"find", "read"}:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("find/read require one URL string")
        if type(args.get("start", 0)) is not int or args.get("start", 0) < 0:
            raise ValueError("start must be a non-negative integer")
        if name == "read" and (type(args.get("length", 6000)) is not int or not 1 <= args.get("length", 6000) <= 16000):
            raise ValueError("length must be 1..16000")
        if name == "find" and (not isinstance(args.get("text"), str) or not 1 <= len(args["text"].strip()) <= 200):
            raise ValueError("find requires literal text of 1..200 characters")
    return "tool", call


def exact_message_requested(question):
    wants_message = re.search(r"错误消息字符串|异常消息字符串|(?:exact\s+)?(?:error|exception)\s+message(?:\s+string)?", question, re.I)
    wants_type = re.search(r"异常类型|错误类型|异常类名|exception\s+(?:type|class)|error\s+type|traceback", question, re.I)
    return bool(wants_message and not wants_type)


def answer_instructions(question):
    if exact_message_requested(question):
        return ("The requested slot is the error MESSAGE STRING from source code. Return only the literal "
                "message value, not the exception class, traceback, code fence, translation or explanation. "
                "Preserve every character of the message including punctuation and internal quotes.")
    return ("Return ONLY the requested information. Check language, ordering, units, precision and naming "
            "against the question. Do not add explanations, alternate answers or labels.")


def normalize_final(question, answer, source_texts):
    if not exact_message_requested(question):
        return _normalize_answer(answer), None
    # Exact strings must not pass through general quote/prefix cleanup.
    text = answer.strip()
    prefix = re.fullmatch(r"([A-Za-z_]\w*(?:Error|Exception)):\s*(.+)", text, flags=re.S)
    if prefix:
        exception, message = prefix.groups()
        literal = r'''("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')'''
        pattern = r"\braise\s+" + re.escape(exception) + r"\s*\(\s*" + literal + r"\s*\)"
        for source in source_texts:
            for match in re.finditer(pattern, source):
                try:
                    value = ast.literal_eval(match[1])
                except (ValueError, SyntaxError):
                    continue
                if value == message:
                    return value, "removed_exception_type_verified_in_source"
    return text, None
