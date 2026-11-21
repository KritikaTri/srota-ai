import pytest

from srotaai.fetch import UnsafeURLError, assert_public_url, fetch


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8001/",
    "http://localhost/admin",
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata endpoint
    "http://10.0.0.5/",
    "file:///etc/passwd",
    "ftp://example.com/x",
])
def test_non_public_urls_are_rejected(url):
    with pytest.raises(UnsafeURLError):
        assert_public_url(url)


def test_fetch_reports_blocked_without_network_call():
    res = fetch("http://127.0.0.1:1/")
    assert not res.ok and res.error.startswith("blocked")
    assert res.attempts == []
