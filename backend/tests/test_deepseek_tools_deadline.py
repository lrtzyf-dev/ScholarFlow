"""Small OCR keepalives must not hide the elapsed-time checks in a chunk buffer."""
import httpx
import pytest

from app.call_ledger import list_calls
from app.providers import deepseek_tools as provider


def test_newline_keepalives_check_deadline_without_waiting_for_64kb(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(provider.time, "monotonic", lambda: clock[0])

    class Keepalives(httpx.SyncByteStream):
        count = 0
        closed = False

        def __iter__(self):
            for _ in range(100):
                self.count += 1
                clock[0] += 1
                yield b"\n"

        def close(self):
            self.closed = True

    stream = Keepalives()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, headers={
        "content-type": "application/json"}, stream=stream))
    client = provider.DeepSeekToolsClient("synthetic-test-key", transport=transport, timeout_seconds=5)
    with pytest.raises(provider.DeepSeekToolsError) as failure:
        client.ocr(b"%PDF-synthetic", filename="input.pdf")
    assert failure.value.code == "timeout" and failure.value.may_be_billed is None
    assert failure.value.upstream_may_continue
    assert stream.count == 5 and stream.closed
    row, = list_calls()["items"]
    assert row["state"] == "unknown" and row["amount"] is None


def test_upload_checks_elapsed_time_between_chunks_and_closes_source(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(provider.time, "monotonic", lambda: clock[0])

    class Upload(httpx.SyncByteStream):
        count = 0
        closed = False

        def __iter__(self):
            for _ in range(100):
                self.count += 1
                clock[0] += 1
                yield b"chunk"

        def close(self):
            self.closed = True

    source = Upload()
    def check():
        if clock[0] >= 3:
            raise provider.DeepSeekToolsError("ocr", "timeout")
    wrapped = provider._DeadlineStream(source, check)
    try:
        with pytest.raises(provider.DeepSeekToolsError):
            list(wrapped)
    finally:
        wrapped.close()
    assert source.count == 3 and source.closed
