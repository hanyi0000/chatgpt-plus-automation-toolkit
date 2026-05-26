import pytest

from modules.browser import parse_proxy
from modules.proxy_pool import normalize_proxy_url


def test_normalize_proxy_url_adds_http_scheme_to_bare_authenticated_proxy() -> None:
    proxy = "user-region-US:password@us-proxy.example.test:3010"

    assert normalize_proxy_url(proxy) == f"http://{proxy}"


def test_parse_proxy_accepts_bare_authenticated_proxy() -> None:
    assert parse_proxy("user-region-US:password@us-proxy.example.test:3010") == {
        "server": "http://us-proxy.example.test:3010",
        "username": "user-region-US",
        "password": "password",
    }


def test_parse_proxy_rejects_vless_urls_with_actionable_error() -> None:
    with pytest.raises(ValueError, match="vless://"):
        parse_proxy("vless://uuid@proxy.example.test:17616?security=reality")
