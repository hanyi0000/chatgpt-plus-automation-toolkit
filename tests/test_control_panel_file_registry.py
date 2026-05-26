from pathlib import Path

from control_panel.file_registry import PanelFile, get_panel_files


def test_get_panel_files_resolves_known_paths(tmp_path: Path) -> None:
    files = get_panel_files(tmp_path)

    expected = {
        "paypal_card_codes": "data/paypal/card_codes.txt",
        "paypal_cards": "data/paypal/cards.txt",
        "paypal_phones": "data/paypal/phones.txt",
        "proxy_default": "data/proxies/proxies.txt",
        "proxy_jp": "data/proxies/proxies_jp.txt",
        "proxy_us": "data/proxies/proxies_us.txt",
        "hotmail_accounts": "data/hotmail/accounts.txt",
        "qq_accounts": "data/qq/accounts.txt",
        "paypal_links": "output/paypal注册/长链接账号/account.txt",
        "paypal_pending_auth": "output/paypal注册/待授权账号/account.txt",
    }

    for key, relative in expected.items():
        assert key in files
        assert isinstance(files[key], PanelFile)
        assert files[key].path == tmp_path / Path(relative)


def test_panel_file_keys_match_instances(tmp_path: Path) -> None:
    files = get_panel_files(tmp_path)

    for key, panel_file in files.items():
        assert panel_file.key == key
        assert panel_file.label
        assert panel_file.kind in {"txt", "env"}
