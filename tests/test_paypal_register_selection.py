from modules.paypal_register import filter_accounts_by_email, resolve_register_proxy_settings
from modules.storage import MailAccount


def test_filter_accounts_by_email_is_case_insensitive() -> None:
    accounts = [
        MailAccount(email="first@hotmail.com", raw="first@hotmail.com----x"),
        MailAccount(email="Target@Hotmail.com", raw="Target@Hotmail.com----x"),
    ]

    selected = filter_accounts_by_email(accounts, "target@hotmail.com")

    assert [account.email for account in selected] == ["Target@Hotmail.com"]


def test_filter_accounts_by_email_returns_all_when_empty() -> None:
    accounts = [MailAccount(email="first@hotmail.com", raw="first@hotmail.com----x")]

    assert filter_accounts_by_email(accounts, "") == accounts


def test_registration_proxy_does_not_inherit_payment_proxy() -> None:
    enabled, proxy_file = resolve_register_proxy_settings(
        {
            "PAYPAL_USE_PROXY": "true",
            "PAYPAL_PROXY_FILE": "data/proxies/payment.txt",
        }
    )

    assert not enabled
    assert proxy_file == "data/proxies/payment.txt"


def test_registration_proxy_can_be_enabled_explicitly() -> None:
    enabled, proxy_file = resolve_register_proxy_settings(
        {
            "PAYPAL_REGISTER_USE_PROXY": "true",
            "PAYPAL_REGISTER_PROXY_FILE": "data/proxies/register.txt",
            "PAYPAL_USE_PROXY": "false",
        }
    )

    assert enabled
    assert proxy_file == "data/proxies/register.txt"
