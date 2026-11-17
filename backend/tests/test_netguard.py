"""SSRF 防护测试：内网/回环/保留地址必须阻断，公网地址放行。"""
import pytest

from app.netguard import SSRFError, assert_public_url

BLOCKED = [
    "http://127.0.0.1/",
    "http://127.0.0.1:8080/secret",
    "http://169.254.169.254/latest/meta-data",
    "http://192.168.1.1/",
    "http://10.0.0.5/",
    "http://172.16.0.1/",
    "http://172.31.255.255/",
    "http://localhost/",
    "http://host.local/",
    "ftp://example.com/file",
    "http://[::1]/",
    "http://0.0.0.0/",
]

PUBLIC = [
    "http://8.8.8.8/",
    "https://1.1.1.1/",
]


@pytest.mark.parametrize("url", BLOCKED)
def test_blocked_urls(url):
    with pytest.raises(SSRFError):
        assert_public_url(url)


@pytest.mark.parametrize("url", PUBLIC)
def test_public_urls(url):
    assert_public_url(url) is None
