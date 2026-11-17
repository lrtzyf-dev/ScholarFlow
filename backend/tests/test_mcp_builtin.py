"""Built-in research tools use synthetic transports; no external service calls."""
from __future__ import annotations

import asyncio
import gzip
import ipaddress
import json
import threading
import time

import httpx
import pytest

from app import mcp_builtin as builtin
from app import netguard


TOOLS = ("arxiv_search", "semantic_scholar_search", "crossref_search", "web_fetch")
PRIVATE = "private-request-or-error-must-not-be-reflected"
QUERY = "synthetic research & 测试 #1"
ATOM_FEED = '''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>https://arxiv.org/abs/2601.00001</id><title>A synthetic\n title</title>
<summary>Synthetic abstract.</summary><published>2026-01-01T00:00:00Z</published>
<author><name>A. Researcher</name></author>
<link title="pdf" href="https://arxiv.org/pdf/2601.00001"/>
</entry></feed>'''
S2_ITEM = {"title": "Synthetic paper", "authors": [{"name": "A. Researcher"}],
           "year": 2026, "venue": "Synthetic Journal", "abstract": "Synthetic abstract.",
           "externalIds": {"DOI": "10.0000/synthetic"}, "citationCount": 3,
           "url": "https://example.test/paper", "openAccessPdf": {"url": "https://example.test/paper.pdf"}}
CROSSREF_ITEM = {"title": ["Synthetic paper"], "author": [{"given": "A.", "family": "Researcher"}],
                 "issued": {"date-parts": [[2026, 1, 1]]}, "container-title": ["Synthetic Journal"],
                 "type": "journal-article", "DOI": "10.0000/synthetic", "publisher": "Synthetic Press",
                 "URL": "https://example.test/paper"}


@pytest.fixture
def network(monkeypatch):
    """Exercise the real async HTTPX client and hooks against MockTransport."""
    calls, client_options = [], []
    handler = [lambda request: httpx.Response(200, text="Synthetic page.", headers={"content-type": "text/plain"})]
    real_client = httpx.AsyncClient

    def dispatch(request):
        calls.append(request)
        return handler[0](request)

    def client(**kwargs):
        client_options.append(kwargs)
        return real_client(transport=httpx.MockTransport(dispatch), **kwargs)

    monkeypatch.setattr(builtin.httpx, "AsyncClient", client)
    monkeypatch.setattr(netguard, "_resolve_ips", lambda host: [ipaddress.ip_address("93.184.216.34")])
    return calls, handler, client_options


def invoke(tool, **kwargs):
    arguments = {"url": "https://public.example.test/page"} if tool == "web_fetch" else {"query": QUERY}
    arguments.update(kwargs)
    return json.loads(asyncio.run(getattr(builtin, tool)(**arguments)))


def valid_response(tool):
    if tool == "arxiv_search":
        return httpx.Response(200, content=ATOM_FEED.encode(), headers={"content-type": "application/atom+xml"})
    if tool == "semantic_scholar_search":
        return httpx.Response(200, json={"data": [S2_ITEM]})
    if tool == "crossref_search":
        return httpx.Response(200, json={"status": "ok", "message": {"items": [CROSSREF_ITEM]}})
    return httpx.Response(200, text="<html><body>Real &amp; synthetic text.</body></html>", headers={"content-type": "text/html"})


@pytest.mark.parametrize("tool", TOOLS)
def test_success_preserves_legacy_fields_with_real_async_guard(network, tool):
    calls, handler, options = network
    handler[0] = lambda request: valid_response(tool)
    result = invoke(tool)
    assert result["status"] == "ok" and "error" not in result
    assert result["trust"] == "untrusted_external" and result["authorizes_actions"] is False
    assert len(calls) == 1 and calls[0].method == "GET"
    assert options[0]["trust_env"] is False and options[0]["max_redirects"] == 5
    if tool == "web_fetch":
        assert result["url"] == "https://public.example.test/page"
        assert result["content_type"] == "text/html" and result["text"] == "Real & synthetic text."
        assert not result["truncated"] and result["source_completeness"] == "unknown"
        assert not result["original_page_coverage_verified"]
    else:
        assert result["query"] == QUERY and result["count"] == len(result["papers"]) == 1
        paper = result["papers"][0]
        assert paper["authors"] == "A. Researcher" and paper["title"]
        if tool == "arxiv_search":
            assert calls[0].url.params["search_query"] == "all:" + QUERY
            assert paper["published"] == "2026-01-01" and paper["pdf"].endswith("2601.00001")
        else:
            assert calls[0].url.params["query"] == QUERY
            assert paper["year"] == 2026 and paper["doi"] == "10.0000/synthetic"


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("status,code", [(400, "http_error"), (401, "authentication"), (403, "forbidden"),
    (404, "not_found"), (429, "rate_limited"), (500, "unavailable")])
def test_error_pages_are_failures_without_echoing_body_or_retry(network, tool, status, code):
    calls, handler, _ = network
    handler[0] = lambda request: httpx.Response(status, text=f"<html>{PRIVATE}</html>", headers={"content-type": "text/html"})
    result = invoke(tool)
    assert result["status"] == "failed" and result["code"] == code and result["status_code"] == status
    assert result["papers"] == [] and "text" not in result and result["automatic_retry"] is False
    assert PRIVATE not in json.dumps(result) and len(calls) == 1


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("exception,code", [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "network_error")])
def test_transport_errors_are_safe_and_not_retried(network, tool, exception, code):
    calls, handler, _ = network

    def fail(request):
        raise exception(PRIVATE, request=request)

    handler[0] = fail
    result = invoke(tool)
    assert result["code"] == code and result["status"] == "failed"
    assert PRIVATE not in json.dumps(result) and len(calls) == 1


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("status", [204, 206])
def test_noncomplete_2xx_is_not_success(network, tool, status):
    network[1][0] = lambda request: httpx.Response(status, text="partial content")
    result = invoke(tool)
    assert result["code"] == "incomplete_response" and result["status_code"] == status


@pytest.mark.parametrize("tool,payload", [
    ("arxiv_search", b"<feed>"), ("arxiv_search", b"<html>error page</html>"),
    ("arxiv_search", b'<feed xmlns="http://www.w3.org/2005/Atom"><entry/></feed>'),
    ("arxiv_search", b'<!DOCTYPE feed [<!ENTITY x "bad">]><feed xmlns="http://www.w3.org/2005/Atom"/>'),
    ("arxiv_search", '<!DOCTYPE feed [<!ENTITY x "bad">]><feed xmlns="http://www.w3.org/2005/Atom"/>'.encode("utf-16")),
    ("semantic_scholar_search", b"not JSON"), ("semantic_scholar_search", b"[]"),
    ("semantic_scholar_search", b"{}"), ("semantic_scholar_search", b'{"data":null}'),
    ("semantic_scholar_search", b'{"data":[null]}'),
    ("semantic_scholar_search", b'{"data":[{"title":"paper","authors":"not-list"}]}'),
    ("semantic_scholar_search", b'{"data":[{"title":"paper","year":true}]}'),
    ("semantic_scholar_search", b'{"data":[{"title":"paper","externalIds":[]}]}'),
    ("semantic_scholar_search", b'{"data":[],"data":[]}'),
    ("semantic_scholar_search", b'{"data":[],"extra":NaN}'),
    ("crossref_search", b"not JSON"), ("crossref_search", b"[]"), ("crossref_search", b"{}"),
    ("crossref_search", b'{"message":{"items":null}}'),
    ("crossref_search", b'{"message":{"items":[null]}}'),
    ("crossref_search", b'{"message":{"items":[{"title":"not-an-array"}]}}'),
    ("crossref_search", b'{"message":{"items":[{"author":[1]}]}}'),
    ("crossref_search", b'{"message":{"items":[{"issued":{"date-parts":[2026]}}]}}'),
    ("crossref_search", b'{"message":{"items":[],"items":[]}}'),
])
def test_invalid_xml_json_or_nested_schema_is_explanatory(network, tool, payload):
    network[1][0] = lambda request: httpx.Response(200, content=payload)
    result = invoke(tool)
    assert result["status"] == "failed" and result["code"] == "invalid_response"
    assert result["papers"] == [] and len(network[0]) == 1


@pytest.mark.parametrize("tool,payload", [
    ("arxiv_search", '<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/api/errors#invalid_query</id></entry></feed>'),
    ("semantic_scholar_search", '{"data":[],"error":"private-upstream-error"}'),
    ("crossref_search", '{"status":"failed","message":{"items":[]}}'),
])
def test_200_api_error_payload_is_not_empty_success(network, tool, payload):
    network[1][0] = lambda request: httpx.Response(200, text=payload)
    result = invoke(tool)
    assert result["code"] == "upstream_error" and result["status"] == "failed"
    assert "private-upstream-error" not in json.dumps(result)


@pytest.mark.parametrize("tool,payload", [
    ("arxiv_search", '<feed xmlns="http://www.w3.org/2005/Atom"/>'),
    ("semantic_scholar_search", '{"data":[]}'),
    ("crossref_search", '{"status":"ok","message":{"items":[]}}'),
])
def test_valid_empty_search_remains_distinct_from_malformed_response(network, tool, payload):
    network[1][0] = lambda request: httpx.Response(200, text=payload)
    result = invoke(tool)
    assert result["status"] == "empty" and result["count"] == 0 and result["papers"] == []
    assert "error" not in result


def test_nullable_optional_scholar_metadata_is_valid(network):
    network[1][0] = lambda request: httpx.Response(200, json={"data": [{"title": "Untyped metadata", "year": None,
        "abstract": None, "authors": None, "externalIds": None, "openAccessPdf": None, "venue": None}]})
    result = invoke("semantic_scholar_search")
    assert result["status"] == "ok" and result["papers"][0]["year"] is None
    assert result["papers"][0]["abstract"] == result["papers"][0]["doi"] == ""


@pytest.mark.parametrize("media,body", [
    ("text/plain", "a" * 8001), ("text/html", "<html><script>ignore me</script><body>" + "中" * 8001 + "</body></html>")])
def test_web_character_truncation_is_explicit_and_preserves_original_limit(network, media, body):
    network[1][0] = lambda request: httpx.Response(200, content=body.encode(), headers={"content-type": media})
    result = invoke("web_fetch")
    assert result["status"] == "partial" and result["truncated"] and result["output_is_partial"]
    assert len(result["text"]) == result["returned_characters"] == 8000
    assert result["text_characters"] == 8001 and result["source_completeness"] == "truncated"
    assert "ignore me" not in result["text"] and not result["original_page_coverage_verified"]


def test_web_html_removes_hidden_text_and_plain_text_preserves_charset(network):
    network[1][0] = lambda request: httpx.Response(200, text="<html><!-- comment --><style>style</style>"
        "<script>script</script><noscript>noscript</noscript><p>Visible &amp; text</p></html>", headers={"content-type": "text/html"})
    assert invoke("web_fetch")["text"] == "Visible & text"
    network[1][0] = lambda request: httpx.Response(200, content="中文正文".encode("gb18030"),
        headers={"content-type": "text/plain; charset=gb18030"})
    assert invoke("web_fetch", url="public.example.test/page")["text"] == "中文正文"


def test_web_exact_limit_is_not_truncated_and_url_case_does_not_break_guard(network):
    network[1][0] = lambda request: httpx.Response(200, text="x" * 8000, headers={"content-type": "TEXT/PLAIN; charset=utf-8"})
    result = invoke("web_fetch", url="HTTPS://PUBLIC.EXAMPLE.TEST/page")
    assert result["status"] == "ok" and result["returned_characters"] == 8000 and not result["truncated"]


@pytest.mark.parametrize("media,body,code", [
    ("application/pdf", b"%PDF-1.7", "unsupported_content_type"),
    ("application/octet-stream", b"binary", "unsupported_content_type"),
    ("", b"binary\x00content", "unsupported_content_type"),
    ("text/html", b"<html><script>hidden</script></html>", "empty_content"),
    ("text/plain", b"  \n\t", "empty_content"),
])
def test_web_binary_and_empty_content_do_not_become_read_evidence(network, media, body, code):
    network[1][0] = lambda request: httpx.Response(200, content=body, headers={"content-type": media})
    result = invoke("web_fetch")
    assert result["code"] == code and "text" not in result


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self.chunks, self.read_count, self.closed = chunks, 0, False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("known_length", [True, False])
def test_response_byte_limit_stops_reading_and_closes_stream(network, monkeypatch, tool, known_length):
    monkeypatch.setattr(builtin, "MAX_RESPONSE_BYTES", 32)
    stream = Chunks([b"x" * 16, b"x" * 17, b"must not be read"])
    network[1][0] = lambda request: httpx.Response(200, stream=stream,
        headers={"content-length": "100"} if known_length else {})
    result = invoke(tool)
    assert result["code"] == "response_too_large" and result["papers"] == []
    assert stream.closed and stream.read_count == (0 if known_length else 2)
    assert len(network[0]) == 1


def test_decoded_compressed_body_limit_and_valid_gzip_are_not_decoded_twice(network, monkeypatch):
    compressed = gzip.compress(b"Visible text")
    stream = Chunks([compressed])
    network[1][0] = lambda request: httpx.Response(200, stream=stream,
        headers={"content-type": "text/plain", "content-encoding": "gzip"})
    assert invoke("web_fetch")["text"] == "Visible text"
    monkeypatch.setattr(builtin, "MAX_RESPONSE_BYTES", 32)
    stream = Chunks([gzip.compress(b"x" * 1000)])
    result = invoke("web_fetch")
    assert result["code"] == "response_too_large" and stream.closed


def test_total_deadline_bounds_endless_small_response_chunks(network, monkeypatch):
    monkeypatch.setattr(builtin, "REQUEST_TIMEOUT_SECONDS", 0.05)

    class Endless(Chunks):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.005)
                self.read_count += 1
                yield b" "

    stream = Endless([])
    network[1][0] = lambda request: httpx.Response(200, stream=stream)
    started = time.monotonic()
    result = invoke("web_fetch")
    assert result["code"] == "timeout" and stream.closed and time.monotonic() - started < 2
    assert len(network[0]) == 1


def test_slow_dns_does_not_block_event_loop_and_cannot_send_after_timeout(network, monkeypatch):
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(builtin, "REQUEST_TIMEOUT_SECONDS", 0.1)

    def slow_guard(request):
        started.set()
        assert release.wait(3), "test failed to release synthetic DNS guard"

    monkeypatch.setattr(builtin, "request_guard_hook", slow_guard)

    async def scenario():
        task = asyncio.create_task(builtin.web_fetch("https://public.example.test/page"))
        try:
            deadline = time.monotonic() + 1
            while not started.is_set():
                assert time.monotonic() < deadline
                await asyncio.sleep(0.001)
            # This coroutine must remain schedulable while the DNS worker waits.
            await asyncio.sleep(0.01)
            assert not task.done() and not release.is_set()
            result = json.loads(await asyncio.wait_for(task, timeout=0.8))
            assert result["code"] == "timeout" and not network[0]
        finally:
            release.set()
            await task
        await asyncio.sleep(0.02)
        assert not network[0], "expired request continued after DNS eventually resolved"

    asyncio.run(scenario())


@pytest.mark.parametrize("failure,reason,hint", [
    ("dns", "dns_resolution_failed", "无法解析"),
    ("private", "non_public_address", "内网"),
    ("fake-ip", "reserved_test_address", "fake-IP"),
])
def test_dns_diagnostics_distinguish_failure_private_and_fake_ip_without_leaks(network, monkeypatch, failure, reason, hint):
    def resolve(host):
        if failure == "dns":
            raise netguard.SSRFError("无法解析主机名：" + PRIVATE)
        return [ipaddress.ip_address("10.1.2.3" if failure == "private" else "198.19.0.23")]

    monkeypatch.setattr(netguard, "_resolve_ips", resolve)
    result = invoke("web_fetch", url="https://" + PRIVATE + ".test/private")
    assert result["code"] == "blocked_target" and result["reason_code"] == reason
    assert hint in result["hint"] and PRIVATE not in json.dumps(result)
    assert "10.1.2.3" not in json.dumps(result) and "198.19.0.23" not in json.dumps(result)
    assert not network[0]


@pytest.mark.parametrize("tool", TOOLS)
def test_redirect_to_private_address_is_blocked_before_second_request(network, tool):
    network[1][0] = lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    result = invoke(tool)
    assert result["code"] == "blocked_target" and len(network[0]) == 1
    assert "127.0.0.1" not in json.dumps(result)


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://169.254.169.254/metadata", "http://localhost/private"])
def test_web_private_initial_target_never_reaches_transport(network, url):
    result = invoke("web_fetch", url=url)
    assert result["code"] == "blocked_target" and not network[0]


@pytest.mark.parametrize("url", [None, "", "ftp://example.test/file", "https://user:password@example.test/", "https://example.test/\n"])
def test_invalid_web_inputs_are_rejected_before_transmission(network, url):
    result = invoke("web_fetch", url=url)
    assert result["code"] == "invalid_request" and not network[0]


@pytest.mark.parametrize("url", ["https://example.test:invalid/", "https://[invalid/", "https://bad host.test/", "https://example.test/\\foo"])
def test_malformed_url_details_are_invalid_request_not_parser_errors(network, url):
    result = invoke("web_fetch", url=url)
    assert result["code"] == "invalid_request" and not network[0]


@pytest.mark.parametrize("length,code", [("not-a-size", "invalid_response"), ("9" * 30, "response_too_large")])
def test_invalid_response_length_does_not_read_body(network, length, code):
    stream = Chunks([b"do not read"])
    network[1][0] = lambda request: httpx.Response(200, stream=stream, headers={"content-length": length})
    assert invoke("web_fetch")["code"] == code
    assert stream.closed and stream.read_count == 0


@pytest.mark.parametrize("tool,limit_name", [("arxiv_search", "max_results"), ("semantic_scholar_search", "limit"), ("crossref_search", "rows")])
def test_invalid_search_inputs_never_reach_transport(network, tool, limit_name):
    assert invoke(tool, query=" ")["code"] == "invalid_request"
    assert invoke(tool, **{limit_name: True})["code"] == "invalid_request"
    assert invoke(tool, **{limit_name: "not-a-number"})["code"] == "invalid_request"
    assert not network[0]
