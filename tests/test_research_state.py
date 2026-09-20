import asyncio
import json

import httpx
import pytest
from bs4 import BeautifulSoup

from research_baseline.agent import Agent
from research_baseline.config import Settings
from research_baseline.contracts import parse_action
from research_baseline.llm import Reply
from research_baseline.research_state import ResearchState, question_anchor, split_state
from research_baseline.tools import ToolRunner, page_text
from research_baseline.trace import Trace
from research_baseline.vendor.tool_types import ToolResult


def constraint(status="open", **extra):
    return {"id": "c1", "question_span": "first mention", "requirement": "Earliest explicit mention",
            "interpretation": "Search body, not only chapter titles", "status": status, "finding": "", "evidence": [], **extra}


def test_full_source_preserves_author_header_outside_article():
    html = '<html><head><title>Research title</title><meta name="citation_author" content="First Author"></head><body><header>First Author — Institute A</header><article>Abstract only</article><script>unsafe_script()</script></body></html>'
    text = page_text(BeautifulSoup(html, "html.parser"))
    assert "First Author — Institute A" in text
    assert "citation_author: First Author" in text
    assert "Abstract only" in text and "unsafe_script" not in text


def test_unsupported_claims_cannot_be_promoted_and_updates_are_atomic():
    state = ResearchState("Find the first mention")
    state.update({"constraints": [constraint("supported", finding="Chapter 8",
                 evidence=[{"url": "https://source.test", "quote": "a fabricated quote"}])]}, {})
    assert state.gaps()[0]["status"] == "open"
    assert not state.progress_keys()
    original = state.snapshot()
    with pytest.raises(ValueError):
        state.update({"constraints": [constraint(finding="partial update"), {"id": "c2", "question_span": "not in question"}]}, {})
    assert state.snapshot() == original
    quote = "The name appears in chapter eight."
    state.update({"constraints": [constraint("supported", finding="Chapter 8",
                 evidence=[{"url": "https://source.test", "quote": quote}])]}, {"https://source.test": "header\n" + quote})
    assert state.coverage()["supported"] == 1
    assert state.constraints["c1"]["evidence"][0]["char_start"] == 7
    assert len(state.progress_keys()) == 1
    keys = state.progress_keys()
    state.update({"constraints": [{"id": "c1", "interpretation": "rephrased"}]}, {"https://source.test": "header\n" + quote})
    assert state.progress_keys() == keys


def test_state_cannot_delete_or_relabel_an_existing_constraint():
    state = ResearchState("Find the first mention")
    state.update({"constraints": [constraint()]}, {})
    state.update({"constraints": [constraint(requirement="Any mention is enough", finding="still unresolved")]}, {})
    assert state.gaps()[0]["requirement"] == "Earliest explicit mention"
    assert state.gaps()[0]["finding"] == "still unresolved"


def test_question_anchor_restores_quotes_but_never_guesses_missing_words():
    question = '在书中“星河”首次提及时的章节数是多少？'
    assert question_anchor(question, '星河首次提及时的章节数') == '星河”首次提及时的章节数'
    assert not question_anchor(question, '星河出现时的章节数')
    assert not question_anchor('“星河”提及，后来“星河”提及', '星河提及')


def test_missing_state_root_brace_repaired_without_repairing_actions():
    rest, patch = split_state('<research_state>{"constraints":[]</research_state><answer>a</answer>')
    assert patch == {"constraints": []}
    assert parse_action(rest, "stop") == ("answer", "a")


def test_state_protocol_still_rejects_multiple_actions_and_truncation():
    tag = '<research_state>{"constraints":[]}</research_state>'
    rest, _ = split_state(tag + '<answer>a</answer><answer>b</answer>')
    with pytest.raises(ValueError):
        parse_action(rest, "stop")
    with pytest.raises(ValueError):
        split_state('<research_state>{"constraints":[]}' + '<answer>a</answer>')
    with pytest.raises(ValueError):
        parse_action('<tool_call>{"name":"read","arguments":{"url":"https://x","start":-1}}</tool_call>', "stop")


def test_bad_auxiliary_state_does_not_discard_valid_tool(tmp_path):
    content = '<research_state>{"constraints": [}</research_state><tool_call>{"name":"search","arguments":{"query":["evidence"]}}</tool_call>'
    rest, patch = split_state(content)
    assert parse_action(rest, "stop")[1]["name"] == "search"
    agent = Agent(Settings(), Trace(tmp_path / "trace"), None, type("Tools", (), {"source_texts": {}})())
    assert agent._update_research(patch)
    assert not agent.research.constraints


def test_find_and_read_recover_late_evidence_without_redownloading(tmp_path):
    async def scenario():
        trace = Trace(tmp_path / "trace")
        settings = Settings(max_tool_result_chars=1800)
        async with httpx.AsyncClient() as http:
            tools = ToolRunner(settings, trace, http, None)
            original = "https://source.test/original"
            canonical = "https://source.test/canonical"
            text = "Intro text. " * 1000 + "UNIQUE_BOUNDARY: first appearance in body." + " trailing" * 500
            downloads = []
            async def download(url):
                downloads.append(url)
                return text.encode(), "text/plain", canonical, "utf-8"
            tools._download = download
            tools.observed_urls.add(original)
            first = await tools.execute("visit", {"url": [original]})
            assert "UNIQUE_BOUNDARY" not in first.content
            found = await tools.execute("find", {"url": original, "text": "UNIQUE_BOUNDARY"})
            assert "UNIQUE_BOUNDARY" in found.content
            assert found.evidence_items[0].source_url == canonical
            item = found.evidence_items[0]
            assert text[item.metadata["char_start"]:item.metadata["char_end"]] == item.text
            read = await tools.execute("read", {"url": canonical, "start": 12000, "length": 100})
            assert "UNIQUE_BOUNDARY" in read.content
            assert read.metadata["next_start"] == 12100
            assert downloads == [original]
            unknown = await tools.execute("read", {"url": "file:///secret", "start": 0})
            assert not unknown.success
    asyncio.run(scenario())


def test_compaction_preserves_early_conflicts_and_evidence(tmp_path):
    settings = Settings(max_context_chars=100)
    tools = type("Tools", (), {"evidence": []})()
    agent = Agent(settings, Trace(tmp_path / "trace"), None, tools)
    agent.research = ResearchState("Find the first mention")
    agent.research.update({"constraints": [constraint("conflicting", finding="Title and body disagree")]}, {})
    agent.messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "question"}] + [{"role": "user", "content": "noise" * 100}] * 8
    agent._compact()
    assert "Title and body disagree" in agent.messages[2]["content"]


def test_audit_uses_original_question_and_evidence_not_previous_narrative(tmp_path):
    agent = Agent(Settings(), Trace(tmp_path / "trace"), None,
                  type("Tools", (), {"evidence": [{"source_url": "https://source.test", "text": "source passage", "metadata": {}}]})())
    agent.messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "original question"},
                      {"role": "assistant", "content": "UNSUPPORTED_OLD_NARRATIVE"}]
    agent.candidate = "candidate"
    messages = agent._audit_messages()
    rendered = json.dumps(messages)
    assert "original question" in rendered and "source passage" in rendered and "candidate" in rendered
    assert "UNSUPPORTED_OLD_NARRATIVE" not in rendered


def test_batched_long_pages_keep_all_sources_visible(tmp_path):
    tools = ToolRunner(Settings(max_tool_result_chars=1200), Trace(tmp_path / "trace"), None, None)
    combined = tools._combine([ToolResult(content=f"Source: https://s.test/{i}\n" + "x" * 5000) for i in range(3)])
    assert len(combined.content) <= 1200
    for i in range(3):
        assert f"https://s.test/{i}" in combined.content


def test_stagnation_reviews_during_research_and_context_does_not_accumulate(tmp_path):
    async def scenario():
        trace = Trace(tmp_path / "trace")
        settings = Settings(max_rounds=5)
        class Model:
            calls = []
            async def complete(self, messages):
                self.calls.append(json.dumps(messages))
                index = len(self.calls)
                prefix = ('<research_state>' + json.dumps({"constraints": [constraint()]}) + '</research_state>') if index == 1 else ''
                if index > 5:
                    return Reply('<answer>unresolved</answer>', 'stop')
                return Reply(prefix + '<tool_call>' + json.dumps({"name": "search", "arguments": {"query": [f"new snippet {index}"]}}) + '</tool_call>', 'stop')
        model = Model()
        from research_baseline.mock import transport
        async with httpx.AsyncClient(transport=transport()) as http:
            tools = ToolRunner(settings, trace, http, model, mock=True)
            agent = Agent(settings, trace, model, tools)
            await agent.run("Find the first mention")
        assert "Research has stalled" in model.calls[3]
        assert agent.progress_checks == 1
        assert all(call.count('<research_context>') <= 1 for call in model.calls)
    asyncio.run(scenario())


def test_gap_review_requests_targeted_read_before_accepting_answer(tmp_path):
    async def scenario():
        settings, trace = Settings(), Trace(tmp_path / "trace")
        quote = "The first mention is chapter eight."
        url = "https://source.test/page"
        initial = '<research_state>' + json.dumps({"constraints": [constraint()]}) + '</research_state>'
        supported = '<research_state>' + json.dumps({"constraints": [constraint("supported", finding="Chapter 8", evidence=[{"url": url, "quote": quote}])]}) + '</research_state>'
        responses = [initial + '<answer>8</answer>',
                     '<tool_call>' + json.dumps({"name": "find", "arguments": {"url": url, "text": "first mention"}}) + '</tool_call>',
                     supported + '<answer>8</answer>']
        class Model:
            async def complete(self, messages):
                return Reply(responses.pop(0), "stop")
        async with httpx.AsyncClient() as http:
            tools = ToolRunner(settings, trace, http, None)
            tools.source_texts[url] = quote
            # Existing snippet discovers the page but cannot support the constraint.
            tools.evidence = [{"source_url": url, "text": "chapter list", "metadata": {}}]
            agent = Agent(settings, trace, Model(), tools)
            result = await agent.run("Find the first mention")
        assert result["answer"] == "8"
        assert result["evidence_complete"]
        assert tools.counts["find_calls"] == 1
        events = [json.loads(line) for line in (trace.directory / "events.jsonl").read_text().splitlines()]
        assert any(e["event"] == "gap_review" and e["coverage"]["unresolved"] == ["c1"] for e in events)
    asyncio.run(scenario())
