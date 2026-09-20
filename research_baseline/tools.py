from __future__ import annotations

import ast
import asyncio
import io
import ipaddress
import json
import math
import operator
import re
import socket
import time
from dataclasses import asdict
from itertools import islice
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

from .vendor.extractor_prompt import EXTRACTOR_PROMPT
from .vendor.html_reader import _parse_extraction
from .vendor.search_format import _format_search_results, _format_serper_results, _iqs_evidence_items, _serper_evidence_items
from .vendor.tool_types import EvidenceItem, ToolResult


def page_text(soup):
    """Keep searchable document text, including scholarly headers and metadata.

    Article-only extraction can delete titles, authors and affiliation headers.
    Remove executable/navigation noise, but keep the full visible document for find/read.
    """
    metadata = []
    if soup.title:
        metadata.append("Page title: " + soup.title.get_text(" ", strip=True))
    for tag in soup.find_all("meta"):
        name = str(tag.get("name") or tag.get("property") or "")
        if name.lower().startswith(("citation_", "dc.", "dcterms.", "og:title")) and tag.get("content"):
            metadata.append(name + ": " + tag["content"])
    for tag in soup(["script", "style", "nav", "footer", "aside", "iframe", "noscript", "svg", "form"]):
        tag.decompose()
    body = soup.body or soup
    visible = "\n".join(line.strip() for line in body.get_text("\n", strip=True).splitlines() if line.strip())
    return "\n".join(metadata) + "\n" + visible


def calculate(expression):
    """Restricted arithmetic, not arbitrary Python execution."""
    if not isinstance(expression, str) or len(expression) > 300:
        raise ValueError("Expression must be a string of at most 300 characters")
    tree = ast.parse(expression, mode="eval")
    if len(list(ast.walk(tree))) > 80:
        raise ValueError("Expression too complex")
    binary = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
              ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod}
    def visit(node):
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            value = node.value
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = visit(node.operand) * (-1 if isinstance(node.op, ast.USub) else 1)
        elif isinstance(node, ast.BinOp) and type(node.op) in binary:
            left, right = visit(node.left), visit(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 10:
                raise ValueError("Exponent exceeds limit")
            value = binary[type(node.op)](left, right)
        else:
            raise ValueError("Only numbers, parentheses and + - * / ** % are supported")
        if not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 1e30:
            raise ValueError("Numeric result outside supported range")
        return value
    return visit(tree.body)


class ToolRunner:
    def __init__(self, settings, trace, http, llm, *, mock=False):
        self.settings, self.trace, self.http, self.llm = settings, trace, http, llm
        self.mock = mock
        self.semaphore = asyncio.Semaphore(settings.max_tool_concurrency)
        self.counts = {
            "search_queries": 0,
            "search_api_requests": 0,
            "search_api_successes": 0,
            "search_api_failures": 0,
            "search_api_cancelled": 0,
            "search_api_latency_seconds": 0.0,
            "visit_pages": 0,
            "calculate_calls": 0,
            "find_calls": 0,
            "read_calls": 0,
            "duplicate_calls": 0,
            "tool_errors": 0,
        }
        self.queries = set()
        self.pages = {}
        self.observed_urls = set()
        self.evidence = []
        self.source_texts = {}
        self.source_aliases = {}
        self.source_artifacts = {}
        self.calculations = []

    async def execute(self, name, args):
        self.trace.emit("tool_start", tool=name, arguments=args)
        try:
            if name == "search":
                values = self._strings(args.get("query"), "query")
                results = await asyncio.gather(*(self._search(q) for q in values))
                result = self._combine(results)
            elif name == "visit":
                values = self._strings(args.get("url"), "url")
                results = await asyncio.gather(*(self._visit(url, str(args.get("goal", "Find answer-relevant evidence"))) for url in values))
                result = self._combine(results)
            elif name == "calculate":
                self.counts["calculate_calls"] += 1
                value = calculate(args.get("expression", ""))
                self.calculations.append({"expression": args["expression"], "value": value})
                result = ToolResult(content=json.dumps({"expression": args["expression"], "value": value}), metadata={"type": "calculate"})
            elif name in {"find", "read"}:
                self.counts[name + "_calls"] += 1
                result = self._locate(name, args)
            else:
                raise ValueError(f"Unknown tool: {name}")
        except asyncio.CancelledError:
            self.trace.emit("tool_cancelled", tool=name)
            raise
        except Exception as error:
            result = ToolResult(content=f"{type(error).__name__}: {error}", success=False)
        self.trace.emit("tool_end", tool=name, result=asdict(result))
        if not result.success:
            self.counts["tool_errors"] += 1
        for item in result.evidence_items:
            row = asdict(item)
            if row not in self.evidence:
                self.evidence.append(row)
        self.trace.write_json("evidence.json", self.evidence)
        return result

    def _locate(self, name, args):
        """Read only in-memory downloaded sources; never open arbitrary paths or URLs."""
        url = self.source_aliases.get(args["url"], args["url"])
        if url not in self.source_texts:
            raise ValueError("Source not downloaded. Call visit first, then find/read its returned URL.")
        text = self.source_texts[url]
        start = args.get("start", 0)
        if type(start) is not int or not 0 <= start <= len(text):
            raise ValueError("start outside source bounds")
        limit = max(1, self.settings.max_tool_result_chars - 1000)
        next_start = None
        if name == "read":
            length = args.get("length", 6000)
            if type(length) is not int or not 1 <= length <= 16000:
                raise ValueError("length must be 1..16000")
            spans = [(start, min(len(text), start + min(length, limit)))]
            if spans[0][1] < len(text):
                next_start = spans[0][1]
        else:
            needle = args.get("text")
            if not isinstance(needle, str) or not 1 <= len(needle.strip()) <= 200:
                raise ValueError("find requires a literal keyword of 1..200 characters")
            matches = list(islice(re.finditer(re.escape(needle), text[start:], re.I), 6))
            if not matches:
                return ToolResult(content=f"Source: {url}\nNo literal match after character {start}; total_chars={len(text)}. This does not prove semantic absence or absence in other chapters.", metadata={"url": url, "matches": 0})
            spans = []
            radius = min(650, max(20, limit // 12))
            for match in matches[:5]:
                left, right = max(start, start + match.start() - radius), min(len(text), start + match.end() + radius)
                if spans and left <= spans[-1][1]:
                    spans[-1] = (spans[-1][0], max(spans[-1][1], right))
                else:
                    spans.append((left, right))
            if len(matches) > 5:
                next_start = start + matches[4].end()
        items, blocks = [], []
        for left, right in spans:
            excerpt = text[left:right]
            blocks.append(f"[chars {left}:{right}]\n{excerpt}")
            items.append(EvidenceItem(text=excerpt, source_url=url, kind="observed", quality=0.6,
                                      metadata={"artifact": self.source_artifacts.get(url), "char_start": left,
                                                "char_end": right, "mode": name}))
        header = f"Source: {url}\ntotal_chars={len(text)}; next_start={next_start}; offsets are zero-based characters.\n"
        return ToolResult(content=header + "\n\n".join(blocks), evidence_items=items,
                          metadata={"url": url, "next_start": next_start, "total_chars": len(text)})

    def _strings(self, value, field):
        values = [value] if isinstance(value, str) else value
        if not isinstance(values, list) or not 1 <= len(values) <= self.settings.max_tool_concurrency:
            raise ValueError(f"{field}: expected 1..{self.settings.max_tool_concurrency} strings")
        if any(not isinstance(v, str) or not v.strip() or len(v) > 2000 for v in values):
            raise ValueError(f"Invalid {field}")
        return [v.strip() for v in values]

    @staticmethod
    def _combine(results):
        return ToolResult(content="\n=======\n".join(r.content for r in results),
                          evidence_items=[e for r in results for e in r.evidence_items],
                          metadata={"items": [r.metadata for r in results]}, success=any(r.success for r in results))

    async def _search(self, query):
        fingerprint = re.sub(r"\s+", " ", query).strip().casefold()
        if fingerprint in self.queries:
            self.counts["duplicate_calls"] += 1
            return ToolResult(content="Duplicate query blocked; change the evidence gap or source.", success=False)
        if self.counts["search_queries"] >= self.settings.max_search_queries:
            return ToolResult(content="Search budget exhausted", success=False)
        self.queries.add(fingerprint)
        self.counts["search_queries"] += 1
        provider = self.settings.search_provider
        async with self.semaphore:
            self.counts["search_api_requests"] += 1
            request_number = self.counts["search_api_requests"]
            started = time.monotonic()
            self.trace.emit("search_api_request", provider=provider, request_number=request_number, query=query)
            try:
                if provider == "serper":
                    response = await self.http.post("https://google.serper.dev/search", json={"q": query, "num": 8},
                                                    headers={"X-API-Key": self.settings.serper_api_key}, timeout=self.settings.tool_timeout_seconds)
                else:
                    response = await self.http.get("https://cloud-iqs.aliyuncs.com/search/genericSearch", params={"query": query, "timeRange": "NoLimit"},
                                                   headers={"X-API-Key": self.settings.iqs_api_key}, timeout=self.settings.tool_timeout_seconds)
                response.raise_for_status()
                data = response.json()
                if provider == "serper":
                    evidence = _serper_evidence_items(data.get("organic", []))
                    content = _format_serper_results(query, data)
                else:
                    evidence = _iqs_evidence_items(data.get("pageItems", []))
                    content = _format_search_results(query, data.get("pageItems", []))
                latency = round(time.monotonic() - started, 3)
                self.counts["search_api_successes"] += 1
                self.counts["search_api_latency_seconds"] = round(self.counts["search_api_latency_seconds"] + latency, 3)
                self.trace.emit("search_api_response", provider=provider, request_number=request_number, query=query,
                                status=response.status_code, success=True, latency_seconds=latency,
                                result_count=len(evidence))
                self.observed_urls.update(e.source_url for e in evidence if e.source_url)
                self.trace.emit("search_response", provider=provider, query=query, response=data)
                return ToolResult(content=content, evidence_items=evidence, metadata={"query": query, "provider": provider}, success=bool(evidence))
            except asyncio.CancelledError:
                latency = round(time.monotonic() - started, 3)
                self.counts["search_api_cancelled"] += 1
                self.counts["search_api_latency_seconds"] = round(self.counts["search_api_latency_seconds"] + latency, 3)
                self.trace.emit("search_api_response", provider=provider, request_number=request_number, query=query,
                                status=None, success=False, cancelled=True, latency_seconds=latency)
                raise
            except Exception as error:
                latency = round(time.monotonic() - started, 3)
                self.counts["search_api_failures"] += 1
                self.counts["search_api_latency_seconds"] = round(self.counts["search_api_latency_seconds"] + latency, 3)
                status = getattr(getattr(error, "response", None), "status_code", None)
                self.trace.emit("search_api_response", provider=provider, request_number=request_number, query=query,
                                status=status, success=False, cancelled=False, latency_seconds=latency,
                                error=f"{type(error).__name__}: {error}")
                return ToolResult(content=f"Search failed: {error}", metadata={"query": query, "provider": provider}, success=False)

    async def _validate_url(self, url):
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("Only public HTTP(S) URLs without credentials are allowed")
        if self.mock and parsed.hostname == "fixture.test":
            return
        addresses = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(addr[4][0]).is_global for addr in addresses):
            raise ValueError("Private/local network URL blocked")

    async def _download(self, url):
        for _ in range(6):
            await self._validate_url(url)
            async with self.http.stream("GET", url, headers={"User-Agent": "ResearchBaseline/0.1"}, timeout=self.settings.tool_timeout_seconds, follow_redirects=False) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers["location"])
                    continue
                response.raise_for_status()
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > self.settings.max_download_bytes:
                        raise ValueError("Page exceeds MAX_DOWNLOAD_BYTES")
                return bytes(buffer), response.headers.get("content-type", ""), url, response.encoding or "utf-8"
        raise ValueError("Too many redirects")

    async def _visit(self, url, goal):
        if url not in self.observed_urls:
            return ToolResult(content="URL must come from the question, a search result, or a previously read page.", success=False)
        if url in self.pages:
            self.counts["duplicate_calls"] += 1
            return self.pages[url]
        if self.counts["visit_pages"] >= self.settings.max_visit_pages:
            return ToolResult(content="Visit budget exhausted", success=False)
        # Reserve before awaiting; concurrent actions share the same counters.
        self.counts["visit_pages"] += 1
        async with self.semaphore:
            try:
                try:
                    raw, content_type, final_url, encoding = await asyncio.wait_for(self._download(url), timeout=self.settings.tool_timeout_seconds)
                    if "pdf" in content_type or raw.startswith(b"%PDF"):
                        reader = PdfReader(io.BytesIO(raw))
                        text = "\n\n".join(f"[Page {i + 1}]\n{page.extract_text() or ''}" for i, page in enumerate(reader.pages))
                    else:
                        html = raw.decode(encoding, errors="replace")
                        if "html" in content_type or "<html" in html[:1000].lower():
                            soup = BeautifulSoup(html, "html.parser")
                            links = [urljoin(final_url, a["href"]) for a in soup.find_all("a", href=True)][:100]
                            self.observed_urls.update(link for link in links if urlsplit(link).scheme in {"https", "http"})
                            # Preserve row boundaries rather than flattening every table cell.
                            for table in soup.find_all("table"):
                                table.replace_with("\n" + "\n".join(" | ".join(cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])) for row in table.find_all("tr")) + "\n")
                            text = page_text(soup) + ("\n\nLinks:\n" + "\n".join(links) if links else "")
                        elif "image/" in content_type or "video/" in content_type:
                            raise ValueError("Visual media is not supported by this text baseline")
                        else:
                            text = html
                except Exception as direct_error:
                    if not self.settings.jina_api_key:
                        raise
                    await self._validate_url(url)
                    self.trace.emit("page_fallback", url=url, error=str(direct_error), provider="jina")
                    response = await self.http.get("https://r.jina.ai/" + url, headers={"Authorization": f"Bearer {self.settings.jina_api_key}", "Accept": "text/markdown"}, timeout=self.settings.tool_timeout_seconds)
                    response.raise_for_status()
                    if len(response.content) > self.settings.max_download_bytes:
                        raise ValueError("Jina response exceeds download limit")
                    text, final_url = response.text, url
                if len(text.strip()) < 20:
                    raise ValueError("No usable text; page may require JS, OCR or authentication")
                artifact = self.trace.artifact("page", text)
                self.source_texts[final_url] = text
                self.source_aliases[url] = final_url
                self.source_artifacts[final_url] = artifact
                self.trace.emit("page_read", url=url, final_url=final_url, artifact=artifact)
                content = text
                mode = "raw_excerpt"
                if self.settings.extractor_enabled and len(text) > self.settings.extractor_threshold_chars:
                    try:
                        reply = await self.llm.complete([{"role": "user", "content": EXTRACTOR_PROMPT.format(webpage_content=text[:50000], goal=goal)}], role="extractor", model=self.settings.extractor_model)
                        extraction = _parse_extraction(reply.content)
                        # Only promote a verbatim span; a generated summary is not verified evidence.
                        if extraction.parsed and extraction.evidence and extraction.evidence in text:
                            content = extraction.evidence
                            mode = "verbatim_extraction"
                        else:
                            self.trace.emit("extraction_rejected", url=url, reason="Evidence is not a contiguous original span; returning raw text")
                    except Exception as error:
                        self.trace.emit("extraction_fallback", url=url, error=str(error))
                limit = self.settings.max_tool_result_chars
                start = text.find(content) if mode == "verbatim_extraction" else 0
                excerpt = content[:limit]
                if len(content) > limit:
                    excerpt += "\n[TRUNCATED: use find(url, text) for keywords or read(url, start, length) to continue in the downloaded full source.]"
                item = EvidenceItem(text=content[:limit], source_url=final_url, kind="observed", quality=0.6,
                                    metadata={"artifact": artifact, "char_start": start, "char_end": start + min(limit, len(content)), "mode": mode})
                result = ToolResult(content=f"Source: {final_url}\n[chars {start}:{start + min(limit, len(content))}; total_chars={len(text)}]\n{excerpt}", evidence_items=[item], metadata={"url": url, "artifact": artifact})
                self.pages[url] = result
                return result
            except Exception as error:
                return ToolResult(content=f"Visit failed for {url}: {error}", metadata={"url": url}, success=False)
