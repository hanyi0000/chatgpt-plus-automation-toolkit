from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PanelFile:
    key: str
    label: str
    path: Path
    kind: str = "txt"


def _panel_file(root: Path, key: str, label: str, relative: str, kind: str = "txt") -> PanelFile:
    return PanelFile(key=key, label=label, path=root / Path(relative), kind=kind)


def get_panel_files(root: Path | str) -> dict[str, PanelFile]:
    root_path = Path(root)
    entries = [
        _panel_file(root_path, "env", ".env", ".env", "env"),
        _panel_file(root_path, "proxy_default", "通用代理池", "data/proxies/proxies.txt"),
        _panel_file(root_path, "proxy_jp", "日本代理池", "data/proxies/proxies_jp.txt"),
        _panel_file(root_path, "proxy_us", "美国代理池", "data/proxies/proxies_us.txt"),
        _panel_file(root_path, "paypal_card_codes", "PayPal 卡密池", "data/paypal/card_codes.txt"),
        _panel_file(root_path, "paypal_card_codes_used", "已用卡密", "data/paypal/card_codes_used.txt"),
        _panel_file(root_path, "paypal_card_codes_failed", "失败卡密", "data/paypal/card_codes_failed.txt"),
        _panel_file(root_path, "paypal_cards", "PayPal 虚拟卡池", "data/paypal/cards.txt"),
        _panel_file(root_path, "paypal_phones", "PayPal 手机号池", "data/paypal/phones.txt"),
        _panel_file(root_path, "hotmail_accounts", "Hotmail 账号池", "data/hotmail/accounts.txt"),
        _panel_file(root_path, "hotmail_mail_pool", "Hotmail 邮箱池", "data/hotmail/mail_pool.txt"),
        _panel_file(root_path, "icloud_accounts", "iCloud 账号池", "data/icloud/accounts.txt"),
        _panel_file(root_path, "icloud_mail_pool", "iCloud 邮箱池", "data/icloud/mail_pool.txt"),
        _panel_file(root_path, "qq_accounts", "QQ 账号池", "data/qq/accounts.txt"),
        _panel_file(root_path, "qq_mail_pool", "QQ 邮箱池", "data/qq/mail_pool.txt"),
        _panel_file(root_path, "mail_accounts", "通用账号池", "data/accounts.txt"),
        _panel_file(root_path, "mail_pool", "通用邮箱池", "data/mail_pool.txt"),
        _panel_file(root_path, "paypal_links", "PayPal 长链接账号", "output/paypal注册/长链接账号/account.txt"),
        _panel_file(root_path, "paypal_pending_auth", "PayPal 待授权账号", "output/paypal注册/待授权账号/account.txt"),
        _panel_file(root_path, "paypal_authorized_rt", "PayPal 授权 RT 输出", "output/paypal注册/授权成功/account-rt.txt"),
        _panel_file(root_path, "paypal_authorized_sub", "PayPal SUB 合并输出", "output/paypal注册/授权成功/sub2api_accounts.json"),
    ]
    return {entry.key: entry for entry in entries}
