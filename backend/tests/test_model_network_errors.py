"""Transport diagnostics remain actionable without echoing request secrets."""
import errno
import socket
import ssl

import httpx
import pytest

from app.model_connection import network_error_message


SECRET = "Private-CaseSensitive-Key"


@pytest.mark.parametrize(("cause", "expected"), [
    (ssl.SSLCertVerificationError(1, SECRET), "证书验证失败"),
    (ssl.SSLError(1, SECRET), "TLS 安全连接失败"),
    (socket.gaierror(socket.EAI_NONAME, SECRET), "域名解析失败"),
    (ConnectionRefusedError(errno.ECONNREFUSED, SECRET), "拒绝连接"),
    (OSError(10061, SECRET), "拒绝连接"),
    (ConnectionResetError(errno.ECONNRESET, SECRET), "连接意外中断"),
    (ConnectionAbortedError(errno.ECONNABORTED, SECRET), "连接意外中断"),
])
def test_wrapped_native_error_is_classified_without_exception_text(cause, expected):
    intermediate = RuntimeError(f"proxy://{SECRET}@example.test")
    intermediate.__cause__ = cause
    exc = httpx.ConnectError(SECRET)
    exc.__cause__ = intermediate
    message = network_error_message(exc)
    assert expected in message
    assert SECRET not in message
    assert "example.test" not in message


@pytest.mark.parametrize(("error_type", "expected"), [
    (httpx.ProxyError, "代理连接失败"),
    (httpx.ConnectTimeout, "连接模型接口超时"),
    (httpx.ReadTimeout, "模型接口响应超时"),
    (httpx.PoolTimeout, "模型接口响应超时"),
    (httpx.ConnectError, "无法连接到接口地址"),
    (httpx.RemoteProtocolError, "通信协议异常"),
    (httpx.LocalProtocolError, "通信协议异常"),
])
def test_httpx_diagnostics_do_not_echo_exception_or_request(error_type, expected):
    request = httpx.Request("GET", f"https://example.test/{SECRET}",
                            headers={"Authorization": f"Bearer {SECRET}"})
    message = network_error_message(error_type(SECRET, request=request))
    assert expected in message
    assert SECRET not in message
    assert "example.test" not in message


def test_context_chain_is_checked_and_cycles_terminate():
    exc = httpx.ConnectError(SECRET)
    cause = socket.gaierror(socket.EAI_NONAME, SECRET)
    exc.__context__ = cause
    cause.__context__ = exc
    assert "域名解析失败" in network_error_message(exc)


def test_generic_message_never_interprets_untrusted_exception_text():
    message = network_error_message(httpx.ConnectError(
        f"CERTIFICATE_VERIFY_FAILED: proxy https://{SECRET}@example.test"))
    assert message == "无法连接到接口地址，请检查网络、系统代理和地址后重试"


@pytest.mark.parametrize("operation", ["legacy_models", "legacy_test", "profile_models", "profile_test"])
def test_connection_endpoints_expose_safe_classification(monkeypatch, operation):
    from app.api import model_profiles, settings

    error = httpx.ConnectError(SECRET)
    error.__cause__ = socket.gaierror(socket.EAI_NONAME, SECRET)

    def fail(*args, **kwargs):
        raise error

    if operation == "legacy_models":
        monkeypatch.setattr(settings, "discover_models", fail)
        result = settings.get_models(settings.ModelsIn(
            llm_base_url="https://example.test/v1", llm_api_key=SECRET))
    elif operation == "legacy_test":
        monkeypatch.setattr(settings, "probe_chat", fail)
        result = settings.test_connection(settings.TestIn(
            llm_base_url="https://example.test/v1", llm_api_key=SECRET,
            llm_model="Exact-Model", reasoning_effort=""))
    elif operation == "profile_models":
        monkeypatch.setattr(model_profiles, "discover_models", fail)
        result = model_profiles.discover_unsaved(model_profiles.DiscoverInput(
            base_url="https://example.test/v1", api_key=SECRET))
    else:
        monkeypatch.setattr(model_profiles, "_profile_values", lambda *args: {
            "base_url": "https://example.test/v1", "api_key": SECRET,
            "model_id": "Exact-Model", "reasoning_effort": "",
        })
        monkeypatch.setattr(model_profiles, "probe_chat", fail)
        result = model_profiles.test_saved(profile_id=model_profiles.store.LEGACY_PROFILE_ID, db=None)
    assert result["ok"] is False
    assert "域名解析失败" in result["message"]
    assert SECRET not in str(result)
    if operation.endswith("models"):
        assert result["models"] == []
