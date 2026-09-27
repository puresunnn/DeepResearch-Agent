"""Deterministic HTTP contract fixtures. No gold answers and no internet access."""
import json

import httpx


def transport():
    def handler(request):
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpu-deepseek-v4-flash"}, {"id": "qwen3.7-plus"}, {"id": "qwen3.7-flash"}, {"id": "qwen3.7-max"}]})
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content)
            messages = body["messages"]
            observations = "\n".join(m["content"] for m in messages[2:] if m["role"] == "user")
            if "MOCK_SOURCE_VALUE" not in observations:
                if "fixture.test" in observations:
                    content = '<decision>Read the retrieved source.</decision><tool_call>{"name":"visit","arguments":{"url":["https://fixture.test/source"],"goal":"Read the fixture value"}}</tool_call>'
                else:
                    content = '<decision>Locate a source.</decision><tool_call>{"name":"search","arguments":{"query":["synthetic pipeline source"]}}</tool_call>'
            elif '"value": 42' not in observations:
                content = '<decision>Check arithmetic.</decision><tool_call>{"name":"calculate","arguments":{"expression":"6*7"}}</tool_call>'
            else:
                content = '<decision>Fixture source and calculation received.</decision><answer>MOCK_PIPELINE_OK</answer>'
            return httpx.Response(200, json={"id": "mock-completion", "model": body["model"], "choices": [{"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 120, "completion_tokens": 50}})
        if request.url.host == "google.serper.dev":
            return httpx.Response(200, json={"organic": [{"title": "Synthetic source", "link": "https://fixture.test/source", "snippet": "A deterministic fixture for checking tool transport; this is not a benchmark answer."}]})
        if request.url.host == "cloud-iqs.aliyuncs.com":
            return httpx.Response(200, json={"pageItems": [{"title": "Synthetic source", "link": "https://fixture.test/source", "snippet": "A deterministic fixture, not a benchmark answer."}]})
        if request.url.host == "fixture.test":
            return httpx.Response(200, text='<html><article>MOCK_SOURCE_VALUE: 42. This synthetic source verifies page reading and trace persistence.</article></html>', headers={"content-type": "text/html"})
        raise AssertionError(f"Unexpected HTTP call in mock mode: {request.url}")
    return httpx.MockTransport(handler)
