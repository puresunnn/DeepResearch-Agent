"""Vendor pure components from the workspace with source hashes and MIT notices.

No imports of upstream runtime modules: those create clients at import time.
"""
import ast
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT / "baseline/research_baseline/vendor"
AWARD = ROOT / "Research-Agent---1st-place-in-Alibaba-Cloud-Data-AI-Competition"


def extract(path, names, header):
    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)
    blocks = []
    for node in tree.body:
        name = getattr(node, "name", "")
        if not name and isinstance(node, ast.Assign):
            name = getattr(node.targets[0], "id", "")
        if name in names:
            start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
            blocks.append("\n".join(source.splitlines()[start - 1:node.end_lineno]))
    if len(blocks) != len(names):
        raise ValueError(f"Upstream symbols changed in {path.name}")
    return header + "\n\n" + "\n\n\n".join(blocks) + "\n"


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    (DEST / "__init__.py").write_text("", encoding="utf-8")
    notice = '# Derived from Yiming Han, MIT; see LICENSE.award and SOURCES.json.\n'
    specs = {
        "protocol.py": (AWARD / "agent_loop.py", ["_extract_between", "_normalize_answer", "_extract_tool_call"], "import json\nimport re\nimport json5"),
        "search_format.py": (AWARD / "tools_search.py", ["_contains_chinese", "_serper_evidence_items", "_iqs_evidence_items", "_format_serper_results", "_format_search_results"], "import urllib.parse\nfrom .tool_types import EvidenceItem"),
        "html_reader.py": (AWARD / "tools_visit.py", ["extract_main_text", "ExtractionResult", "_parse_extraction"], "import json\nfrom dataclasses import dataclass\nfrom bs4 import BeautifulSoup"),
        "extractor_prompt.py": (AWARD / "prompts.py", ["EXTRACTOR_PROMPT"], ""),
        "judge_prompt.py": (ROOT / "xbench-evals/eval_grader.py", ["LLM_JUDGE_PROMPT"], ""),
    }
    manifest = []
    for filename, (source, names, header) in specs.items():
        output = extract(source, names, notice + header)
        if filename == "protocol.py":
            output = output.replace("(search|visit)", "(search|visit|calculate)")
            output = "\n".join(line for line in output.splitlines() if 'print(f"[agent]' not in line) + "\n"
        (DEST / filename).write_text(output, encoding="utf-8")
        manifest.append({"file": filename, "source": str(source.relative_to(ROOT)), "symbols": names,
                         "upstream_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                         "vendored_sha256": hashlib.sha256((DEST / filename).read_bytes()).hexdigest()})
    for source_name, dest_name in [("tool_types.py", "tool_types.py"), ("LICENSE", "LICENSE.award")]:
        source = AWARD / source_name
        (DEST / dest_name).write_bytes(source.read_bytes())
        manifest.append({"file": dest_name, "source": str(source.relative_to(ROOT)),
                         "upstream_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                         "vendored_sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    (DEST / "LICENSE.xbench").write_bytes((ROOT / "xbench-evals/LICENSE").read_bytes())
    (DEST / "SOURCES.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Vendored {len(manifest)} components/notices into {DEST}")


if __name__ == "__main__":
    main()
