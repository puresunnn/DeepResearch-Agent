# Derived from Yiming Han, MIT; see LICENSE.award and SOURCES.json.
import json
import re
import json5

def _extract_between(text: str, start_tag: str, end_tag: str) -> str:
    """Extract content between two tags."""
    start_idx = text.find(start_tag)
    if start_idx == -1:
        return ""
    start_idx += len(start_tag)
    end_idx = text.find(end_tag, start_idx)
    if end_idx == -1:
        return text[start_idx:].strip()
    return text[start_idx:end_idx].strip()


def _normalize_answer(answer: str) -> str:
    """Clean up answer format: remove common prefixes, trailing punctuation, and quote wrapping."""
    text = answer.strip()
    # Remove common prefixes
    for prefix in [
        "The answer is ", "the answer is ",
        "Answer: ", "answer: ",
        "答案是", "答案：",
    ]:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    # Remove wrapping quotes (Chinese and English)
    if len(text) >= 2:
        if (text[0] == '"' and text[-1] == '"') or (text[0] == "'" and text[-1] == "'"):
            text = text[1:-1].strip()
        if (text[0] == '\u201c' and text[-1] == '\u201d') or (text[0] == '\u2018' and text[-1] == '\u2019'):
            text = text[1:-1].strip()
        if (text[0] == '\u300c' and text[-1] == '\u300d'):
            text = text[1:-1].strip()
    return text


def _extract_tool_call(content: str) -> str | None:
    """Extract tool call JSON from content, supporting multiple formats."""
    # Format 1: <tool_call>...</tool_call>
    if "<tool_call>" in content and "</tool_call>" in content:
        return _extract_between(content, "<tool_call>", "</tool_call>")
    if "<tool_call>" in content:
        return content[content.find("<tool_call>") + len("<tool_call>"):].strip()

    # Format 2: <function=name>\n<parameter=key>\nvalue\n</parameter>\n</function>
    func_match = re.search(r'<function=(search|visit|calculate)>', content)
    if func_match:
        tool_name = func_match.group(1)
        func_start = func_match.start()
        func_end_tag = content.find("</function>", func_start)
        if func_end_tag == -1:
            func_block = content[func_start:]
        else:
            func_block = content[func_start:func_end_tag]
        args = {}
        for param_match in re.finditer(r'<parameter=(\w+)>\s*(.*?)\s*</parameter>', func_block, re.DOTALL):
            param_name = param_match.group(1)
            param_value = param_match.group(2).strip()
            try:
                args[param_name] = json5.loads(param_value)
            except Exception:
                args[param_name] = param_value
        result = json.dumps({"name": tool_name, "arguments": args})
        return result

    # Format 3: bare JSON tool call (no tags)
    bare_match = re.search(r'\{"name":\s*"(search|visit|calculate)"', content)
    if bare_match:
        json_start = bare_match.start()
        brace_depth = 0
        json_end = json_start
        for i in range(json_start, len(content)):
            if content[i] == '{':
                brace_depth += 1
            elif content[i] == '}':
                brace_depth -= 1
                if brace_depth == 0:
                    json_end = i + 1
                    break
        return content[json_start:json_end]
    return None
