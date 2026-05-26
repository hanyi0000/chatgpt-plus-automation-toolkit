from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_playwright_keyboard_press_is_not_called_with_timeout() -> None:
    source = (ROOT / "modules" / "free_browser_flow.py").read_text(encoding="utf-8")

    assert 'keyboard.press("Enter", timeout=' not in source


def test_log_output_does_not_force_gbk_roundtrip() -> None:
    source = (ROOT / "modules" / "utils.py").read_text(encoding="utf-8")

    assert ".encode(\"gbk\"" not in source
    assert ".decode(\"gbk\"" not in source


def test_paypal_auto_filler_removes_authchallenge_response_div_directly() -> None:
    source = (ROOT / "modules" / "paypal_auto_filler.py").read_text(encoding="utf-8")

    assert 'div[data-app="authchallenge_response"]' in source
    assert "const EXACT_REMOVE_SELECTORS" in source
    assert "for (const sel of EXACT_REMOVE_SELECTORS)" in source
