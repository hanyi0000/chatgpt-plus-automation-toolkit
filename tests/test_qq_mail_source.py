import asyncio
from datetime import datetime, timezone

import get_oauth_rt
from modules.mail_provider import MailProvider
from modules.storage import MailAccount, parse_mail_line


def test_parse_qq_account_uses_second_field_as_imap_auth_code() -> None:
    account = parse_mail_line("test@qq.com----imap-auth-code")

    assert account is not None
    assert account.email == "test@qq.com"
    assert account.mail_url == "imap-auth-code"


def test_qq_provider_uses_imap_handler_for_auth_code(monkeypatch) -> None:
    captured: dict[str, str] = {}

    async def fake_fetch_qq_imap_code(account: MailAccount, _since, _exclude) -> str:
        captured["email"] = account.email
        captured["auth_code"] = str(account.mail_url)
        return "123456"

    monkeypatch.setattr("modules.mail_provider.fetch_qq_imap_code", fake_fetch_qq_imap_code)
    provider = MailProvider(source="qq_imap", timeout_sec=1, poll_interval_sec=0)
    account = MailAccount(email="test@qq.com", mail_url="imap-auth-code")

    code = asyncio.run(provider.wait_code(account, datetime.now(timezone.utc)))

    assert code == "123456"
    assert captured == {"email": "test@qq.com", "auth_code": "imap-auth-code"}


def test_oauth_followup_routes_qq_auth_code_to_imap(monkeypatch) -> None:
    called: dict[str, str] = {}

    def fake_fetch(mail_url: str, email: str = "", timeout: int = 12, since=None) -> str:
        called["email"] = email
        called["auth_code"] = mail_url
        return "654321"

    monkeypatch.setattr(get_oauth_rt, "fetch_qq_imap_email_code", fake_fetch)

    code = get_oauth_rt.fetch_latest_email_code("imap-auth-code", email="test@qq.com")

    assert code == "654321"
    assert called == {"email": "test@qq.com", "auth_code": "imap-auth-code"}
