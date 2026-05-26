"""OAuth RT 获取工具。

用法:
  uv run python scripts/get_oauth_rt.py start --open --out token.json
  uv run python scripts/get_oauth_rt.py exchange --callback "<url-or-code>" --out token.json
  uv run python scripts/get_oauth_rt.py refresh --refresh-token "<rt>" --out token.json
  uv run python scripts/get_oauth_rt.py save --token-file token.json
  uv run python scripts/get_oauth_rt.py list
  uv run python scripts/get_oauth_rt.py get-at --account user@example.com --print-token

这个脚本不保存账号密码。密码只用于当前登录过程，长期保存的只有 refresh_token 和账号元信息。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timedelta, timezone
import getpass
import hashlib
import html
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import state_db
from error_classifier import classify_error, classify_exit
from mail_adapters.service import wait_code as wait_mail_adapter_code
from modules.grizzly_sms_provider import GrizzlySMSProvider
from modules.hero_sms_provider import HeroSMSProvider, PhoneCountry, local_phone_number
from modules.fivesim_sms_provider import FiveSimProvider
from modules.terminal_theme import install_print_theme


install_print_theme()


def _bootstrap_imports() -> Path:
    return Path(__file__).resolve().parent


REPO_ROOT = _bootstrap_imports()
DEFAULT_SESSION_FILE = REPO_ROOT / "输出" / "oauth-rt-session.json"
DEFAULT_ACCOUNT_STORE = REPO_ROOT / "输出" / "oauth-rt-accounts.json"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "输出" / "tokens"
DEFAULT_RT_TXT = REPO_ROOT / "输出" / "account-rt.txt"
DEFAULT_INPUT_ROOT = REPO_ROOT / "账号输入"
DEFAULT_TEAM_ACCOUNT_FILE = DEFAULT_INPUT_ROOT / "team helper 专用" / "account.txt"
DEFAULT_PLUS_ACCOUNT_FILE = DEFAULT_INPUT_ROOT / "普通授权文件生成" / "account.txt"
DEFAULT_TEAM_PENDING_INPUT_DIR = DEFAULT_INPUT_ROOT / "team pending"
DEFAULT_GOPAY_ACCOUNT_FILE = DEFAULT_INPUT_ROOT / "gopay手动订阅" / "account.txt"
DEFAULT_TEAM_OUTPUT_DIR = REPO_ROOT / "输出" / "team helper 专用" / "tokens"
DEFAULT_PLUS_OUTPUT_DIR = REPO_ROOT / "输出" / "普通授权文件生成" / "tokens"
DEFAULT_TEAM_CHILD_OUTPUT_DIR = REPO_ROOT / "输出" / "team子号授权文件" / "tokens"
DEFAULT_GOPAY_OUTPUT_DIR = REPO_ROOT / "输出" / "gopay手动订阅"
DEFAULT_TEAM_RT_TXT = REPO_ROOT / "输出" / "team helper 专用" / "account-rt.txt"
DEFAULT_PLUS_RT_TXT = REPO_ROOT / "输出" / "普通授权文件生成" / "account-rt.txt"
DEFAULT_TEAM_CHILD_RT_TXT = REPO_ROOT / "输出" / "team子号授权文件" / "account-rt.txt"
DEFAULT_GOPAY_ACCOUNT_TXT = DEFAULT_GOPAY_OUTPUT_DIR / "account.txt"
DEFAULT_GOPAY_ACCOUNT_JSON = DEFAULT_GOPAY_OUTPUT_DIR / "completed_accounts.json"
DEFAULT_STATE_DB = REPO_ROOT / "data" / "auth_tasks.db"
DEFAULT_AUTOMATION_DIR = Path(os.environ.get("TEAM_PENDING_AUTOMATION_DIR", r"C:\Users\Loucer\Desktop\自动化"))
DEFAULT_REGISTER_PROJECT_DIR = DEFAULT_AUTOMATION_DIR / "register_project"
SINGAPORE_ADDRESS_API_URL = "https://www.meiguodizhi.com/api/v1/dz"
ACCOUNT_STORE_LOCK = threading.Lock()
RT_TXT_LOCK = threading.Lock()
INPUT_REMOVE_LOCK = threading.Lock()
STATS_LOCK = threading.Lock()
SUB_OUTPUT_LOCK = threading.Lock()
CONSENT_LOCK = threading.Lock()
GOPAY_OUTPUT_LOCK = threading.Lock()
DEFAULT_INBOX_LOUCER_DOMAIN_WHITELIST = []
DEFAULT_MOEMAIL_DOMAIN_WHITELIST = [
    "dfvcws.com",
    "intereloucer.com",
    "interestedloucer.com",
    "interestloucered.com",
    "loucered.com.cn",
]
_RUN_REDEEM_AVAILABLE: bool | None = None
_RUN_REDEEM_IMPORT_ERROR_PRINTED = False
_HOTMAIL_LOCAL_CREDS_MISS_LOGGED: set[str] = set()
QQ_IMAP_DOMAINS = {"qq.com", "foxmail.com", "vip.qq.com"}


# ---- Standalone helpers: no AutoTeam-F import required ----
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_AUTH_URL = "https://auth.openai.com/oauth/authorize"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_AUTH_ORIGIN = "https://auth.openai.com"
CODEX_CALLBACK_PORT = 1455
CODEX_REDIRECT_URI = f"http://localhost:{CODEX_CALLBACK_PORT}/auth/callback"
CHATGPT_HOME_URL = "https://chatgpt.com/"

UTF8_READ_ENCODING = "utf-8-sig"
UTF8_WRITE_ENCODING = "utf-8"


def mark_failure(args, message: str, *, error_type: str = "") -> str:
    category = error_type or classify_error(message)
    if args is not None:
        setattr(args, "last_error", message)
        setattr(args, "error_type", category)
    print(f"[fail:{category}] {message}", file=sys.stderr)
    return category


def set_auth_stage(args, stage: str) -> None:
    if args is None:
        return
    setattr(args, "current_stage", stage)
    db_path = getattr(args, "state_db", "")
    email = getattr(args, "email", "") or getattr(args, "account_email", "")
    account_type = getattr(args, "auth_mode", "")
    source_path = getattr(args, "task_source_path", "")
    if not db_path or not email or not account_type or not source_path:
        return
    try:
        state_db.update_stage(db_path, email=email, account_type=account_type, source_path=source_path, stage=stage)
    except Exception:
        # 状态库只是诊断辅助，不能影响授权主流程。
        pass


def domain_of_email(email: str) -> str:
    if "@" not in (email or ""):
        return ""
    return email.rsplit("@", 1)[-1].strip().lower()


def read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding=UTF8_READ_ENCODING)


def write_text(path: str | Path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding=UTF8_WRITE_ENCODING)


SYSTEM_CHROME_PATHS = [
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
]


def resolve_system_chrome_executable() -> str:
    override = os.environ.get("PLAYWRIGHT_CHROME_EXECUTABLE", "").strip()
    if override and Path(override).exists():
        return override
    for path in SYSTEM_CHROME_PATHS:
        if path.exists():
            return str(path)
    return ""


def get_playwright_launch_options(headless: bool | None = None, *, use_system_chrome: bool | None = None) -> dict:
    if headless is None:
        headless = os.environ.get("PLAYWRIGHT_HEADLESS", "").strip().lower() in {"1", "true", "yes", "on"}
    if use_system_chrome is None:
        use_system_chrome = os.environ.get("PLAYWRIGHT_USE_SYSTEM_CHROME", "").strip().lower() in {"1", "true", "yes", "on"}
    options = {
        "headless": bool(headless),
        "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
    }
    chrome_executable = resolve_system_chrome_executable() if use_system_chrome else ""
    if chrome_executable:
        options["executable_path"] = chrome_executable
    proxy_url = os.environ.get("PLAYWRIGHT_PROXY_URL", "").strip()
    proxy_server = os.environ.get("PLAYWRIGHT_PROXY_SERVER", "").strip()
    proxy_username = os.environ.get("PLAYWRIGHT_PROXY_USERNAME", "").strip()
    proxy_password = os.environ.get("PLAYWRIGHT_PROXY_PASSWORD", "").strip()
    proxy_bypass = os.environ.get("PLAYWRIGHT_PROXY_BYPASS", "").strip()
    proxy = None
    if proxy_url:
        proxy = {"server": proxy_url}
    elif proxy_server:
        proxy = {"server": proxy_server}
        if proxy_username:
            proxy["username"] = proxy_username
        if proxy_password:
            proxy["password"] = proxy_password
    if proxy:
        if proxy_bypass:
            proxy["bypass"] = proxy_bypass
        options["proxy"] = proxy
    return options


def env_flag_enabled(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off", "disable", "disabled"}


def grant_auth_local_network_access(context, page=None) -> None:
    """Allow auth.openai.com to call the localhost OAuth callback when Chrome asks."""
    if not env_flag_enabled("PLAYWRIGHT_GRANT_LOCAL_NETWORK_ACCESS", default=True):
        return

    try:
        context.grant_permissions(["local-network-access"], origin=CODEX_AUTH_ORIGIN)
        print("[browser] 已预授权 auth.openai.com 访问本地网络；未出现权限弹窗时会自动跳过。")
        return
    except Exception as exc:
        first_error = str(exc).splitlines()[0]

    if page is None:
        print(f"[browser] 本地网络权限预授权未生效，继续原流程: {first_error}")
        return

    try:
        session = context.new_cdp_session(page)
        session.send(
            "Browser.grantPermissions",
            {
                "origin": CODEX_AUTH_ORIGIN,
                "permissions": ["localNetworkAccess"],
            },
        )
        print("[browser] 已通过 CDP 预授权 auth.openai.com 访问本地网络。")
    except Exception as exc:
        detail = str(exc).splitlines()[0]
        print(f"[browser] 本地网络权限预授权未生效，继续原流程: {first_error}; CDP={detail}")


try:
    from playwright.sync_api import sync_playwright
except Exception as exc:  # pragma: no cover - runtime dependency hint
    sync_playwright = None
    PLAYWRIGHT_IMPORT_ERROR = exc
else:
    PLAYWRIGHT_IMPORT_ERROR = None


def _generate_pkce():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _parse_jwt_payload(token):
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (4 - len(payload) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


def _build_auth_url(code_challenge, state):
    params = {
        "client_id": CODEX_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": CODEX_REDIRECT_URI,
        "scope": "openid email profile offline_access",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "consent",
    }
    return f"{CODEX_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _exchange_auth_code(auth_code, code_verifier, fallback_email=None):
    import requests

    resp = requests.post(
        CODEX_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": CODEX_CLIENT_ID,
            "code": auth_code,
            "redirect_uri": CODEX_REDIRECT_URI,
            "code_verifier": code_verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        print(f"[error] Token 交换失败: HTTP {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        return None

    token_data = resp.json()
    id_token = token_data.get("id_token", "")
    claims = _parse_jwt_payload(id_token)
    auth_claims = claims.get("https://api.openai.com/auth", {})
    return {
        "access_token": token_data.get("access_token"),
        "refresh_token": token_data.get("refresh_token"),
        "id_token": id_token,
        "account_id": auth_claims.get("chatgpt_account_id", ""),
        "email": claims.get("email", fallback_email or ""),
        "plan_type": auth_claims.get("chatgpt_plan_type", "unknown"),
        "expired": time.time() + token_data.get("expires_in", 3600),
    }


def _click_primary_auth_button(page, field, labels):
    label_re = re.compile(rf"^(?:{'|'.join(re.escape(label) for label in labels)})$", re.I)
    try:
        form = field.locator("xpath=ancestor::form[1]").first
        btn = form.get_by_role("button", name=label_re).first
        if btn.is_visible(timeout=2000):
            btn.click()
            return True
    except Exception:
        pass
    try:
        form = field.locator("xpath=ancestor::form[1]").first
        btn = form.locator('button[type="submit"], input[type="submit"]').first
        if btn.is_visible(timeout=2000):
            btn.click()
            return True
    except Exception:
        pass
    try:
        btn = page.get_by_role("button", name=label_re).last
        if btn.is_visible(timeout=2000):
            btn.click()
            return True
    except Exception:
        pass
    try:
        field.press("Enter")
        return True
    except Exception:
        return False


def fill_auth_field(field, value: str, *, label: str = "field", timeout: int = 8000) -> bool:
    deadline = time.time() + timeout / 1000
    last_error = ""
    while time.time() < deadline:
        try:
            field.wait_for(state="visible", timeout=1000)
            if field.is_enabled(timeout=1000):
                if label == "密码":
                    # OpenAI auth 的密码页偶尔只认真实键盘事件；直接 fill/JS 注入会出现按钮不推进。
                    field.click(timeout=2000)
                    field.press("Control+A", timeout=1000)
                    field.type(value, delay=18, timeout=6000)
                else:
                    field.fill(value, timeout=3000)
                return True
            last_error = "element is disabled"
        except Exception as exc:
            last_error = str(exc).splitlines()[0]
        time.sleep(0.35)

    try:
        handle = field.element_handle(timeout=2000)
        if not handle:
            if label == "密码":
                print("[debug] 密码框已变化或暂不可用，继续观察页面。")
            else:
                print(f"[login] {label} 输入框不可用: {last_error}")
            return False
        handle.evaluate(
            """(element, value) => {
                element.removeAttribute('disabled');
                element.removeAttribute('readonly');
                const proto = element.tagName === 'TEXTAREA'
                    ? window.HTMLTextAreaElement.prototype
                    : window.HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                if (setter) setter.call(element, value);
                else element.value = value;
                element.dispatchEvent(new Event('input', { bubbles: true }));
                element.dispatchEvent(new Event('change', { bubbles: true }));
                element.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: value.slice(-1) || 'a' }));
            }""",
            value,
        )
        print(f"[login] {label} 已通过 JS 注入填写")
        return True
    except Exception as exc:
        if label == "密码":
            print("[debug] 密码框已变化，继续观察页面。")
        else:
            print(f"[login] {label} 填写失败: {str(exc).splitlines()[0]}")
        return False
# ---- End standalone helpers ----


EMAIL_SELECTORS = (
    'input[name="email"], input[id="email-input"], input[id="email"], input[type="email"], '
    'input[autocomplete="email"], input[autocomplete="username"], input[placeholder*="email" i], '
    'input[placeholder*="邮箱"]'
)
PASSWORD_SELECTORS = 'input[name="password"], input[type="password"]'
CODE_SELECTORS = (
    'input[name="code"], input[inputmode="numeric"], input[autocomplete="one-time-code"], '
    'input[placeholder*="验证码"], input[placeholder*="code" i]'
)
OTP_INVALID_HINTS = (
    "invalid code",
    "incorrect code",
    "wrong code",
    "expired code",
    "check the code and try again",
    "验证码无效",
    "验证码错误",
    "验证码已过期",
)
PASSWORD_INVALID_HINTS = (
    "incorrect email address or password",
    "incorrect password",
    "wrong password",
    "invalid email or password",
    "邮箱或密码错误",
    "密码错误",
    "密码不正确",
)
AUTH_INVALID_STATE_HINTS = (
    "invalid_state",
    "no_valid_organizations",
    "验证过程中出错",
    "糟糕，出错了",
    "请重试",
)
OTP_SWITCH_SELECTORS = (
    'button:has-text("一次性验证码"), button:has-text("邮箱验证码"), '
    'button:has-text("Email login"), button:has-text("email login"), '
    'button:has-text("one-time"), button:has-text("One-time"), '
    'button:has-text("email code"), button:has-text("Email code"), '
    'a:has-text("一次性验证码"), a:has-text("邮箱验证码"), '
    'a:has-text("Email login"), a:has-text("one-time")'
)
CHATGPT_LOGIN_SELECTORS = (
    'a[href*="/auth/login"], button:has-text("Log in"), a:has-text("Log in"), '
    'button:has-text("登录"), a:has-text("登录"), [data-testid="login-button"], '
    'button:has-text("Log in"), button:has-text("登陆"), a:has-text("登陆")'
)
CHATGPT_LOGGED_IN_SELECTORS = (
    'button[data-testid*="profile"], [data-testid="accounts-profile-button"], '
    'button[aria-label*="profile" i], button[aria-label*="account" i], '
    'button:has-text("升级套餐"), a:has-text("升级套餐"), '
    'button:has-text("Upgrade plan"), a:has-text("Upgrade plan")'
)
CHATGPT_SIGNUP_SELECTORS = (
    'button:has-text("免费注册"), a:has-text("免费注册"), '
    'button:has-text("Sign up"), a:has-text("Sign up")'
)
AUTH_CONTINUE_LABELS = (
    "Continue",
    "继续",
    "Next",
    "下一步",
    "Log in",
    "登录",
    "Verify",
    "验证",
    "Submit",
    "确认",
)
ONBOARDING_NAME_SELECTORS = (
    'input[name="name"], input[name="fullName"], input[name="full_name"], '
    'input[autocomplete="name"], input[placeholder*="全名"], input[placeholder*="姓名"], '
    'input[placeholder*="name" i]'
)
ONBOARDING_AGE_SELECTORS = (
    'input[name="age"], input[type="number"], input[placeholder*="年龄"], '
    'input[placeholder*="age" i]'
)
ONBOARDING_SUBMIT_SELECTORS = (
    'button:has-text("完成账户创建"), button:has-text("完成账号创建"), '
    'button:has-text("Continue"), button:has-text("继续"), '
    'button:has-text("Create account"), button:has-text("完成")'
)
FREE_TRIAL_SELECTORS = (
    'button:has-text("免费试用"), a:has-text("免费试用"), '
    'button:has-text("Free trial"), a:has-text("Free trial"), '
    'button:has-text("Try Plus"), a:has-text("Try Plus")'
)
PLUS_FREE_TRIAL_SELECTORS = (
    'button:has-text("领取免费试用"), a:has-text("领取免费试用"), '
    'button:has-text("Start free trial"), a:has-text("Start free trial"), '
    'button:has-text("Claim free trial"), a:has-text("Claim free trial"), '
    'button:has-text("Free trial"), a:has-text("Free trial")'
)
COUNTRY_DROPDOWN_SELECTORS = (
    'button:has-text("美国"), button:has-text("United States"), '
    'button:has-text("Indonesia"), button:has-text("印度尼西亚"), '
    'button:has-text("Country"), button:has-text("Region"), button:has-text("国家"), button:has-text("地区"), '
    '[role="combobox"]:has-text("美国"), [role="combobox"]:has-text("United States"), '
    '[role="combobox"]:has-text("Country"), [role="combobox"]:has-text("Region"), '
    '[role="button"]:has-text("美国"), [role="button"]:has-text("United States")'
)
RANDOM_FIRST_NAMES = (
    "Alex",
    "Taylor",
    "Jordan",
    "Morgan",
    "Casey",
    "Riley",
    "Jamie",
    "Avery",
    "Quinn",
    "Parker",
    "Harper",
    "Logan",
)
RANDOM_LAST_NAMES = (
    "Miller",
    "Carter",
    "Bennett",
    "Parker",
    "Collins",
    "Reed",
    "Walker",
    "Brooks",
    "Hayes",
    "Cooper",
    "Morgan",
    "Foster",
)


class _ReusableHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class CallbackWaiter:
    def __init__(self, expected_state: str, port: int = CODEX_CALLBACK_PORT):
        self.expected_state = expected_state
        self.port = port
        self.code = ""
        self.error = ""
        self.raw_url = ""
        self._event = threading.Event()
        self._server: _ReusableHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        waiter = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if not self.path.startswith("/auth/callback"):
                    self.send_error(404)
                    return

                host = self.headers.get("Host", f"localhost:{self.server.server_port}")
                waiter.record(f"http://{host}{self.path}")
                ok = bool(waiter.code) and not waiter.error
                body = "Authentication successful. You can close this window." if ok else waiter.error
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))

            def log_message(self, format, *args):
                return

        self._server = _ReusableHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def record(self, callback_url: str) -> None:
        parsed = parse_callback_or_code(callback_url, expected_state=self.expected_state, allow_plain_code=False)
        self.raw_url = parsed["raw"]
        self.code = parsed["code"]
        self.error = parsed["error"]
        self._event.set()

    def wait(self, timeout: int) -> str:
        self._event.wait(timeout=max(0, timeout))
        return self.code

    def stop(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:
                pass
            self._server = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1)
        self._thread = None


def parse_callback_or_code(raw: str, *, expected_state: str = "", allow_plain_code: bool = True) -> dict:
    text = (raw or "").strip()
    if not text:
        return {"code": "", "state": "", "error": "code 为空", "raw": text}

    if allow_plain_code and "://" not in text and "?" not in text and "=" not in text:
        return {"code": text, "state": "", "error": "", "raw": text}

    candidate = text
    if "://" not in candidate:
        if candidate.startswith("?"):
            candidate = "http://localhost/auth/callback" + candidate
        elif "=" in candidate:
            candidate = "http://localhost/auth/callback?" + candidate
        else:
            candidate = "http://" + candidate

    parsed_url = urllib.parse.urlparse(candidate)
    query = urllib.parse.parse_qs(parsed_url.query)
    fragment = urllib.parse.parse_qs(parsed_url.fragment)

    def value(name: str) -> str:
        return (query.get(name) or fragment.get(name) or [""])[0].strip()

    code = value("code")
    state = value("state")
    error = value("error") or value("error_description")
    if state and expected_state and state != expected_state:
        code = ""
        error = "OAuth state 不匹配"
    if not code and not error:
        error = "回调 URL 中缺少 code"
    return {"code": code, "state": state, "error": error, "raw": text}


def save_session(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def parse_env_line(line: str) -> tuple[str, str] | None:
    text = line.strip()
    if not text or text.startswith("#") or "=" not in text:
        return None
    key, _, value = text.partition("=")
    value = value.strip().strip('"').strip("'")
    return key.strip(), value


def load_root_env() -> dict:
    env_path = REPO_ROOT / ".env"
    data = {}
    if env_path.exists():
        for line in read_text(env_path).splitlines():
            parsed = parse_env_line(line)
            if parsed:
                key, value = parsed
                data[key] = os.environ.get(key, value)
    for key in (
        "AUTH_SERVER_UPLOAD",
        "AUTH_SERVER_URL",
        "AUTH_SERVER_API_KEY",
        "INBOX_LOUCER_BASE_URL",
        "INBOX_LOUCER_USERNAME",
        "INBOX_LOUCER_PASSWORD",
        "INBOX_LOUCER_DOMAIN_WHITELIST",
        "LOUCER_INBOX_BASE_URL",
        "LOUCER_INBOX_USERNAME",
        "LOUCER_INBOX_PASSWORD",
        "MOEMAIL_ENABLED",
        "MOEMAIL_BASE_URL",
        "MOEMAIL_API_KEY",
        "MOEMAIL_DOMAIN_WHITELIST",
    ):
        if key in os.environ:
            data[key] = os.environ[key]
    return data


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_session(path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"找不到 session 文件: {path}")
    data = json.loads(read_text(path))
    if not data.get("code_verifier") or not data.get("state"):
        raise RuntimeError(f"session 文件缺少 code_verifier/state: {path}")
    return data


def bundle_from_refresh_response(data: dict, refresh_token: str) -> dict:
    id_token = data.get("id_token", "")
    claims = _parse_jwt_payload(id_token) if id_token else {}
    auth_claims = claims.get("https://api.openai.com/auth", {})
    expires_in = data.get("expires_in", 3600) or 3600
    return {
        "access_token": data.get("access_token"),
        "refresh_token": data.get("refresh_token") or refresh_token,
        "id_token": id_token,
        "account_id": auth_claims.get("chatgpt_account_id", ""),
        "email": claims.get("email", ""),
        "plan_type": auth_claims.get("chatgpt_plan_type", "unknown"),
        "expired": time.time() + expires_in,
    }


def refresh_bundle(refresh_token: str) -> dict | None:
    import requests

    resp = requests.post(
        CODEX_TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": CODEX_CLIENT_ID,
            "refresh_token": refresh_token,
            "scope": "openid profile email",
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        print(f"[error] Token 刷新失败: HTTP {resp.status_code} {resp.text[:200]}", file=sys.stderr)
        return None
    return bundle_from_refresh_response(resp.json(), refresh_token)


def normalize_bundle(bundle: dict) -> dict:
    expired = normalize_expired(bundle.get("expired") or 0)
    return {
        "email": bundle.get("email") or "",
        "plan_type": bundle.get("plan_type") or "unknown",
        "account_id": bundle.get("account_id") or "",
        "access_token": bundle.get("access_token") or "",
        "refresh_token": bundle.get("refresh_token") or "",
        "id_token": bundle.get("id_token") or "",
        "expired": expired,
        "created_at": bundle.get("created_at") or utc_now(),
    }


def server_upload_payload(bundle: dict, account_type: str = "") -> dict:
    payload = normalize_bundle(bundle)
    payload["account_type"] = account_type or bundle.get("account_type") or "unknown"
    payload["source"] = "local_script"
    code_address = str(bundle.get("code_address") or bundle.get("mail_url") or "").strip()
    source_format = str(bundle.get("source_format") or "").strip().lower()
    if code_address and (source_format == "icloud_query" or not re.match(r"^https?://", code_address, flags=re.IGNORECASE)):
        payload["mail_query_code"] = code_address
    return payload


def normalize_expired(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0
    if text.isdigit():
        return float(text)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def iso_utc(value: float | int | str | None = None) -> str:
    timestamp = normalize_expired(value) if value else time.time()
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def codex_auth_file_payload(bundle: dict) -> dict:
    payload = normalize_bundle(bundle)
    return {
        "access_token": payload["access_token"],
        "account_id": payload["account_id"],
        "disabled": bool(bundle.get("disabled", False)),
        "email": payload["email"],
        "expired": iso_utc(payload["expired"]),
        "id_token": payload["id_token"],
        "last_refresh": bundle.get("last_refresh") or iso_utc(),
        "refresh_token": payload["refresh_token"],
        "type": bundle.get("type") or "codex",
        "websockets": bool(bundle.get("websockets", False)),
    }


def sub2api_expires_at(value: float | int | str | None) -> str:
    timestamp = normalize_expired(value) if value else time.time()
    tz = timezone(timedelta(hours=8))
    return datetime.fromtimestamp(timestamp, tz=tz).isoformat(timespec="seconds")


def sub2api_account_payload(bundle: dict) -> dict:
    payload = normalize_bundle(bundle)
    claims = _parse_jwt_payload(payload.get("id_token", "")) if payload.get("id_token") else {}
    auth_claims = claims.get("https://api.openai.com/auth", {}) if isinstance(claims, dict) else {}
    if not isinstance(auth_claims, dict):
        auth_claims = {}
    expired = normalize_expired(payload.get("expired"))
    expires_in = max(0, int(round(expired - time.time()))) if expired else 0
    email = payload.get("email") or claims.get("email", "")
    account_id = payload.get("account_id") or auth_claims.get("chatgpt_account_id", "")
    credentials = {
        "_token_version": int(time.time() * 1000),
        "access_token": payload.get("access_token", ""),
        "chatgpt_account_id": account_id,
        "chatgpt_user_id": auth_claims.get("chatgpt_user_id") or auth_claims.get("user_id") or claims.get("sub", ""),
        "email": email,
        "expires_at": sub2api_expires_at(expired),
        "expires_in": expires_in,
        "id_token": payload.get("id_token", ""),
        "organization_id": auth_claims.get("organization_id", ""),
        "refresh_token": payload.get("refresh_token", ""),
    }
    return {
        "name": email or account_id or "unknown",
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "extra": {"email": email},
        "concurrency": 10,
        "priority": 1,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
    }


def load_sub2api_store(path: Path) -> dict:
    if not path.exists():
        return {"exported_at": utc_now(), "proxies": [], "accounts": []}
    data = json.loads(read_text(path))
    if not isinstance(data, dict):
        raise RuntimeError(f"SUB 输出文件格式无效: {path}")
    accounts = data.get("accounts", [])
    if not isinstance(accounts, list):
        raise RuntimeError(f"SUB 输出文件 accounts 字段无效: {path}")
    return {
        "exported_at": data.get("exported_at") or utc_now(),
        "proxies": data.get("proxies") if isinstance(data.get("proxies"), list) else [],
        "accounts": accounts,
    }


def sub2api_identity(account: dict) -> tuple[str, str]:
    credentials = account.get("credentials", {}) if isinstance(account.get("credentials"), dict) else {}
    email = (credentials.get("email") or account.get("extra", {}).get("email") if isinstance(account.get("extra"), dict) else "")
    account_id = credentials.get("chatgpt_account_id") or ""
    return str(email or "").strip().lower(), str(account_id or "").strip().lower()


def default_sub2api_path(output_dir: str) -> Path:
    base = Path(output_dir)
    if base.name.lower() == "tokens":
        return base.parent / "sub2api_accounts.json"
    return base / "sub2api_accounts.json"


def default_single_sub2api_dir(output_dir: str) -> Path:
    base = Path(output_dir)
    if base.name.lower() == "tokens":
        return base.parent / "sub2api_authorized"
    return base / "sub2api_authorized"


def single_sub2api_store(record: dict) -> dict:
    return {
        "exported_at": utc_now(),
        "proxies": [],
        "accounts": [record],
    }


def write_single_sub2api_output(directory: Path, bundle: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    record = sub2api_account_payload(bundle)
    email, account_id = sub2api_identity(record)
    identifier = safe_filename_part(email or account_id or "unknown")
    output_path = directory / f"{identifier}.json"
    write_text(output_path, json.dumps(single_sub2api_store(record), ensure_ascii=False, indent=2))
    return output_path


def upsert_sub2api_output(path: Path, bundle: dict) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = load_sub2api_store(path)
    record = sub2api_account_payload(bundle)
    email, account_id = sub2api_identity(record)
    accounts = data.setdefault("accounts", [])
    replaced = False
    for index, account in enumerate(accounts):
        existing_email, existing_id = sub2api_identity(account)
        if (email and existing_email == email) or (account_id and existing_id == account_id):
            accounts[index] = record
            replaced = True
            break
    if not replaced:
        accounts.append(record)
    accounts.sort(key=lambda item: sub2api_identity(item))
    data["exported_at"] = utc_now()
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
    return record


def maybe_write_sub2api_output(args, bundle: dict) -> None:
    standard_output = getattr(args, "standard_output", False) or bool(getattr(args, "account_file", ""))
    sub_out = str(getattr(args, "sub_out", "") or "").strip()
    if not standard_output and not sub_out:
        return
    if getattr(args, "no_sub_output", False):
        return
    out_path = Path(sub_out) if sub_out else default_sub2api_path(getattr(args, "output_dir", str(DEFAULT_OUTPUT_DIR)))
    single_dir = out_path.parent / "sub2api_authorized" if sub_out else default_single_sub2api_dir(getattr(args, "output_dir", str(DEFAULT_OUTPUT_DIR)))
    with SUB_OUTPUT_LOCK:
        upsert_sub2api_output(out_path, bundle)
        single_path = write_single_sub2api_output(single_dir, bundle)
    setattr(args, "task_sub_written", True)
    record_run_stat(args, "sub_written")
    print(f"[ok] 已追加/更新 SUB JSON: {out_path}")
    print(f"[ok] 已写入单账号 SUB JSON: {single_path}")


def load_account_store(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "accounts": []}
    data = json.loads(read_text(path))
    if isinstance(data, list):
        return {"version": 1, "accounts": data}
    if not isinstance(data, dict):
        raise RuntimeError(f"账号库格式无效: {path}")
    accounts = data.get("accounts", [])
    if not isinstance(accounts, list):
        raise RuntimeError(f"账号库 accounts 字段无效: {path}")
    return {"version": data.get("version", 1), "accounts": accounts}


def save_account_store(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


def account_identity(account: dict) -> str:
    return (account.get("account_id") or account.get("email") or "").strip().lower()


def find_account(data: dict, identifier: str) -> dict | None:
    needle = (identifier or "").strip().lower()
    if not needle:
        return None
    for account in data.get("accounts", []):
        if (account.get("email") or "").strip().lower() == needle:
            return account
        if (account.get("account_id") or "").strip().lower() == needle:
            return account
    return None


def upsert_account_bundle(store_path: Path, bundle: dict, *, refreshed: bool = False) -> dict:
    payload = normalize_bundle(bundle)
    if not payload["refresh_token"]:
        raise RuntimeError("token bundle 缺少 refresh_token，不能保存到账号库")
    if not payload["email"] and not payload["account_id"]:
        raise RuntimeError("token bundle 缺少 email/account_id，不能保存到账号库")

    data = load_account_store(store_path)
    accounts = data.setdefault("accounts", [])
    now = utc_now()
    existing = None
    for account in accounts:
        same_email = payload["email"] and account.get("email", "").lower() == payload["email"].lower()
        same_id = payload["account_id"] and account.get("account_id", "").lower() == payload["account_id"].lower()
        if same_email or same_id:
            existing = account
            break

    record = {
        "email": payload["email"],
        "plan_type": payload["plan_type"],
        "account_id": payload["account_id"],
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "id_token": payload["id_token"],
        "expired": payload["expired"],
        "created_at": existing.get("created_at", payload["created_at"]) if existing else payload["created_at"],
        "updated_at": now,
        "last_refresh_at": now if refreshed else (existing or {}).get("last_refresh_at", ""),
    }

    if existing:
        existing.clear()
        existing.update(record)
        saved = existing
    else:
        accounts.append(record)
        saved = record

    accounts.sort(key=lambda item: ((item.get("email") or "").lower(), item.get("account_id") or ""))
    save_account_store(store_path, data)
    return saved


def merge_refresh_with_existing(bundle: dict, existing: dict) -> dict:
    merged = dict(existing)
    merged.update(bundle)
    if not merged.get("email"):
        merged["email"] = existing.get("email", "")
    if not merged.get("account_id"):
        merged["account_id"] = existing.get("account_id", "")
    if not merged.get("plan_type") or merged.get("plan_type") == "unknown":
        merged["plan_type"] = existing.get("plan_type", "unknown")
    return merged


def print_account(account: dict, *, print_token: bool = False) -> None:
    print("[account] email:", account.get("email", ""))
    print("[account] plan_type:", account.get("plan_type", "unknown"))
    print("[account] account_id:", account.get("account_id", ""))
    print("[account] expired:", account.get("expired", 0))
    print("[account] created_at:", account.get("created_at", ""))
    print("[account] updated_at:", account.get("updated_at", ""))
    print("[account] last_refresh_at:", account.get("last_refresh_at", ""))
    print("[account] access_token:", token_preview(account.get("access_token", ""), print_token))
    print("[account] refresh_token:", token_preview(account.get("refresh_token", ""), print_token))
    if not print_token:
        print("[hint] 默认隐藏完整 token；需要终端完整输出时加 --print-token。")


def token_preview(token: str, print_full: bool) -> str:
    if not token:
        return ""
    if print_full:
        return token
    if len(token) <= 40:
        return f"{token[:8]}..."
    return f"{token[:24]}...{token[-8:]}"


def write_and_print(bundle: dict, *, out: str = "", print_token: bool = False) -> dict:
    payload = normalize_bundle(bundle)
    if out:
        out_path = Path(out)
        if out_path.parent and str(out_path.parent) != ".":
            out_path.parent.mkdir(parents=True, exist_ok=True)
        write_text(out_path, json.dumps(codex_auth_file_payload(payload), ensure_ascii=False, indent=2))
        print(f"[ok] 已写入: {out_path}")

    print("[ok] email:", payload["email"])
    print("[ok] plan_type:", payload["plan_type"])
    print("[ok] account_id:", payload["account_id"])
    print("[ok] access_token:", token_preview(payload["access_token"], print_token))
    print("[ok] refresh_token:", token_preview(payload["refresh_token"], print_token))
    if payload["access_token"] and not print_token:
        print("[hint] 默认隐藏完整 token；需要终端完整输出时加 --print-token。")
    return payload


def record_run_stat(args, key: str, amount: int = 1) -> None:
    stats = getattr(args, "run_stats", None)
    if not isinstance(stats, dict):
        return
    with STATS_LOCK:
        stats[key] = int(stats.get(key, 0) or 0) + amount


def maybe_save_store(args, bundle: dict, *, refreshed: bool = False) -> None:
    if not getattr(args, "save_store", False):
        return
    with ACCOUNT_STORE_LOCK:
        account = upsert_account_bundle(Path(args.store), bundle, refreshed=refreshed)
    setattr(args, "task_store_saved", True)
    record_run_stat(args, "store_saved")
    print(f"[ok] 已保存到账号库: {Path(args.store)}")
    print(f"[ok] 账号索引: {account.get('email') or account.get('account_id')}")


def should_upload_to_server(env: dict) -> bool:
    raw = (env.get("AUTH_SERVER_UPLOAD") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def upload_bundle_to_server(bundle: dict, account_type: str = "") -> bool:
    env = load_root_env()
    if not should_upload_to_server(env):
        return False

    base_url = (env.get("AUTH_SERVER_URL") or "").strip().rstrip("/")
    api_key = (env.get("AUTH_SERVER_API_KEY") or env.get("ACCOUNT_POOL_API_KEY") or "").strip()
    if not base_url or not api_key:
        print("[warn] 已开启服务器上传，但 AUTH_SERVER_URL/AUTH_SERVER_API_KEY 未配置。")
        return False

    payload = server_upload_payload(bundle, account_type=account_type)

    try:
        import requests

        resp = requests.post(
            f"{base_url}/api/accounts/upsert",
            json=payload,
            headers={"X-API-Key": api_key},
            timeout=15,
        )
        if resp.status_code not in {200, 201}:
            print(f"[warn] 服务器上传失败: HTTP {resp.status_code} {resp.text[:200]}")
            return False
        print("[ok] 已同步到服务器数据库")
        return True
    except Exception as exc:
        print(f"[warn] 服务器上传异常，不影响本地落盘: {exc}")
        return False


def safe_filename_part(value: str, *, default: str = "unknown") -> str:
    text = (value or default).strip().lower()
    text = re.sub(r"[^a-z0-9@._+-]+", "_", text)
    text = text.strip("._-")
    return text or default


def standard_json_path(bundle: dict, output_dir: str) -> Path:
    payload = normalize_bundle(bundle)
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    email = safe_filename_part(payload.get("email", ""), default="no_email")
    plan = safe_filename_part(payload.get("plan_type", ""), default="unknown")
    return Path(output_dir) / f"{stamp}_{email}_{plan}.json"


def append_rt_line(path: str, bundle: dict) -> None:
    with RT_TXT_LOCK:
        payload = normalize_bundle(bundle)
        email = payload.get("email", "")
        rt = payload.get("refresh_token", "")
        source_format = str(bundle.get("source_format") or "").strip().lower()
        code_address = str(bundle.get("code_address") or bundle.get("mail_url") or "").strip()
        if not email or not rt:
            return
        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        existing = read_text(out_path) if out_path.exists() else ""
        lines = [line for line in existing.splitlines() if not line.lower().startswith(f"{email.lower()}----")]
        if source_format == "icloud_query" and code_address:
            lines.append(f"{email}----{code_address}----{rt}")
        else:
            lines.append(f"{email}----{rt}")
        write_text(out_path, "\n".join(lines) + "\n")
    print(f"[ok] 已追加/更新 RT TXT: {out_path}")


def gopay_record_from_entry(entry: dict) -> dict:
    return {
        "email": (entry.get("email") or "").strip(),
        "mail_url": (entry.get("mail_url") or "").strip(),
        "saved_at": utc_now(),
    }


def append_gopay_completed_entry(entry: dict, *, output_dir: str = "") -> dict:
    record = gopay_record_from_entry(entry)
    email = record["email"]
    if not email:
        raise RuntimeError("gopay 手动订阅记录缺少账号邮箱")

    out_dir = Path(output_dir or DEFAULT_GOPAY_OUTPUT_DIR)
    txt_path = out_dir / "account.txt"
    json_path = out_dir / "completed_accounts.json"
    with GOPAY_OUTPUT_LOCK:
        out_dir.mkdir(parents=True, exist_ok=True)

        existing_txt = read_text(txt_path) if txt_path.exists() else ""
        txt_lines = [
            line
            for line in existing_txt.splitlines()
            if not line.lower().startswith(f"{email.lower()}----")
        ]
        txt_lines.append(f"{email}----{record['mail_url']}")
        write_text(txt_path, "\n".join(txt_lines) + "\n")

        if json_path.exists():
            data = json.loads(read_text(json_path))
            if not isinstance(data, dict):
                data = {}
        else:
            data = {}
        records = data.get("accounts", [])
        if not isinstance(records, list):
            records = []
        records = [
            item
            for item in records
            if str(item.get("email", "")).strip().lower() != email.lower()
        ]
        records.append(record)
        records.sort(key=lambda item: str(item.get("email", "")).lower())
        data["updated_at"] = utc_now()
        data["accounts"] = records
        write_text(json_path, json.dumps(data, ensure_ascii=False, indent=2))

    print(f"[ok] gopay 手动订阅已保存账号和接码地址: {txt_path}")
    print(f"[ok] gopay JSON 已更新: {json_path}")
    return {"txt_path": str(txt_path), "json_path": str(json_path), "record": record}


def write_result_outputs(args, bundle: dict) -> dict:
    standard_output = getattr(args, "standard_output", False) or bool(getattr(args, "account_file", ""))
    out = getattr(args, "out", "") or ""
    if standard_output and not out:
        out = str(standard_json_path(bundle, getattr(args, "output_dir", str(DEFAULT_OUTPUT_DIR))))
    payload = write_and_print(bundle, out=out, print_token=getattr(args, "print_token", False))
    account_input = getattr(args, "account_input_override", None) or {}
    payload["source_format"] = account_input.get("source_format", "")
    payload["mail_url"] = account_input.get("mail_url", "")
    payload["code_address"] = account_input.get("mail_url", "") or account_input.get("code_address", "")
    if not payload["source_format"] and payload.get("email", "").lower().endswith("@icloud.com") and payload.get("code_address") and not re.match(r"^https?://", str(payload["code_address"]), flags=re.IGNORECASE):
        payload["source_format"] = "icloud_query"
    if not payload.get("code_address") and payload.get("source_format") == "icloud_query":
        payload["code_address"] = payload.get("mail_url", "")
    if out:
        setattr(args, "task_output_json", str(out))
        setattr(args, "task_local_written", True)
    record_run_stat(args, "local_written")
    if standard_output:
        append_rt_line(getattr(args, "rt_txt", str(DEFAULT_RT_TXT)), payload)
        setattr(args, "task_rt_saved", True)
    maybe_write_sub2api_output(args, payload)
    return payload


def parse_mail_url_password(mail_url: str) -> str:
    try:
        parsed = urllib.parse.urlparse(mail_url.strip())
        query = urllib.parse.parse_qs(parsed.query)
        return (query.get("p") or query.get("password") or [""])[0].strip()
    except Exception:
        return ""


def strip_html_to_text(content: str) -> str:
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", content or "")
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_email_code(content: str) -> str:
    text = strip_html_to_text(content)
    if not text:
        return ""

    patterns = [
        r"(?:OpenAI|ChatGPT|Codex).{0,50}(?:代码|验证码|verification code|code).{0,30}?(?<!\d)(\d{6})(?!\d)",
        r"(?:代码|验证码|verification code|code).{0,30}?(?:为|是|is|:|：)?\s*(?<!\d)(\d{6})(?!\d)",
        r"(?<!\d)(\d{6})(?!\d).{0,50}(?:OpenAI|ChatGPT|Codex|代码|验证码|verification code|code)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)

    # Some iCloud query bodies omit useful OpenAI/verification words in the
    # visible text. Keep a visible-text fallback, but avoid URL/query noise.
    matches = re.findall(r"(?<!\d)(\d{6})(?!\d)", text)
    return matches[0] if matches else ""


def fetch_ssin_email_code(mail_url: str, email: str = "", timeout: int = 12) -> str:
    parsed = urllib.parse.urlparse(mail_url)
    query = urllib.parse.parse_qs(parsed.query)
    jwt = (query.get("jwt") or [""])[0].strip()
    if not jwt:
        return ""
    try:
        import requests

        address = email.strip()
        if not address:
            claims = _parse_jwt_payload(jwt)
            address = (claims.get("address") or "").strip()
        path = "/api/mails?limit=10&offset=0"
        if address:
            path += "&address=" + urllib.parse.quote(address)
        response = requests.get(
            "https://tempmailapi.ssin.online" + path,
            headers={
                "Authorization": "Bearer " + jwt,
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
                ),
                "Content-Type": "application/json",
                "x-lang": "zh",
                "x-fingerprint": "ERROR",
            },
            timeout=timeout,
        )
        if response.status_code != 200:
            print(f"[mail] ssin 邮箱 API 访问失败: HTTP {response.status_code} {response.text[:80]}")
            return ""
        data = response.json()
        mails = data.get("results") or []
        for mail in mails:
            content = "\n".join(
                str(mail.get(key) or "")
                for key in ("subject", "from", "source", "text", "html", "content", "raw")
            )
            code = extract_email_code(content)
            if code:
                print(f"[mail] 已从 ssin 邮箱 API 获取验证码: {code}")
                return code
        print(f"[mail] ssin 邮箱 API 暂未识别到验证码邮件，共 {len(mails)} 封。")
        return ""
    except Exception as exc:
        print(f"[mail] ssin 邮箱 API 获取失败: {exc}")
        return ""


def fetch_nissanserena_email_code(mail_url: str, email: str = "", timeout: int = 12) -> str:
    if not email:
        return ""
    try:
        import requests

        parsed = urllib.parse.urlparse(mail_url)
        search_path = "/otp/search"
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query = [(key, value) for key, value in query if key.lower() not in {"email", "_ts"}]
        query.append(("email", email.strip()))
        query.append(("_ts", str(int(time.time()))))
        search_url = urllib.parse.urlunparse(
            parsed._replace(path=search_path, query=urllib.parse.urlencode(query), fragment="")
        )
        response = requests.get(
            search_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Cache-Control": "no-cache",
                "Referer": "https://nissanserena.my.id/otp",
            },
            timeout=timeout,
        )
        if response.status_code != 200:
            print(f"[mail] NissanSerena OTP 页面访问失败: HTTP {response.status_code}")
            return ""
        text = strip_html_to_text(response.text)
        if "OTP Tidak ditemukan" in text:
            print(f"[mail] NissanSerena 暂未找到验证码: {email}")
            return ""
        code = extract_email_code(response.text)
        if code:
            print(f"[mail] 已从 NissanSerena OTP 获取验证码: {code}")
            return code
        print(f"[mail] NissanSerena 页面已返回，但暂未识别到验证码: {email}")
        return ""
    except Exception as exc:
        print(f"[mail] NissanSerena OTP 获取失败: {exc}")
        return ""


def extract_code_from_json(data) -> str:
    if isinstance(data, dict):
        for key in ("otp", "code", "verification_code", "verificationCode", "pin"):
            value = str(data.get(key) or "").strip()
            if re.fullmatch(r"\d{6}", value):
                return value
        for value in data.values():
            code = extract_code_from_json(value)
            if code:
                return code
    if isinstance(data, list):
        for item in data:
            code = extract_code_from_json(item)
            if code:
                return code
    return ""


def fetch_ray_otp_email_code(mail_url: str, email: str = "", timeout: int = 12) -> str:
    parsed = urllib.parse.urlparse(mail_url)
    query = urllib.parse.parse_qs(parsed.query)
    address = (email or extract_email_address(mail_url)).strip()
    service = (query.get("service") or query.get("type") or ["openai"])[0].strip() or "openai"
    if not address:
        print("[mail] Ray OTP 需要邮箱账号，当前没有可查询的 email。")
        return ""
    try:
        import requests

        env = load_root_env()
        api_base = (
            os.environ.get("RAY_OTP_API_BASE")
            or env.get("RAY_OTP_API_BASE")
            or "https://www.cezhgpt.my.id"
        ).strip().rstrip("/")
        api_key = (
            os.environ.get("RAY_OTP_API_KEY")
            or env.get("RAY_OTP_API_KEY")
            or "cfmail_secret_2025"
        ).strip()
        lookup_url = (
            f"{api_base}/api/mailboxes/admin/address/"
            f"{urllib.parse.quote(address, safe='')}/otp?service={urllib.parse.quote(service, safe='')}"
        )
        response = requests.get(
            lookup_url,
            headers={
                "X-API-Key": api_key,
                "Accept": "application/json",
                "Cache-Control": "no-cache",
                "Referer": "https://ray-otp.vercel.app/",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
                ),
            },
            timeout=timeout,
        )
        if response.status_code == 404:
            print(f"[mail] Ray OTP 暂未找到验证码: {address}")
            return ""
        if response.status_code != 200:
            print(f"[mail] Ray OTP 获取失败: HTTP {response.status_code} {response.text[:80]}")
            return ""
        data = response.json() if response.text else {}
        code = extract_code_from_json(data)
        if not code:
            code = extract_email_code(json.dumps(data, ensure_ascii=False))
        if code:
            print(f"[mail] 已从 Ray OTP 获取验证码: {code}")
            return code
        message = str(data.get("message") or data.get("error") or "") if isinstance(data, dict) else ""
        print(f"[mail] Ray OTP 已返回但暂未识别到验证码: {address}{' | ' + message[:80] if message else ''}")
        return ""
    except Exception as exc:
        print(f"[mail] Ray OTP 获取失败: {exc}")
        return ""


def fetch_icloud_thefindnet_email_code(mail_url: str, email: str = "", timeout: int = 12, since: datetime | None = None) -> str:
    address = (email or extract_email_address(mail_url)).strip().lower()
    if not address.endswith("@icloud.com"):
        return ""

    query_code = ""
    if mail_url and not re.match(r"^https?://", mail_url, flags=re.IGNORECASE):
        query_code = mail_url.strip()
    else:
        parsed = urllib.parse.urlparse(mail_url)
        query = urllib.parse.parse_qs(parsed.query)
        query_code = (query.get("query_code") or query.get("code") or query.get("password") or [""])[0].strip()

    if not query_code:
        return ""

    try:
        import requests

        session = requests.Session()
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://icloud.thefindnet.xyz",
            "Referer": "https://icloud.thefindnet.xyz/",
        }
        response = session.post(
            "https://icloud.thefindnet.xyz/public/search-emails.php",
            json={"credentials": f"{address}----{query_code}"},
            headers=headers,
            timeout=timeout,
        )
        if response.status_code != 200:
            print(f"[mail] iCloud 查询平台搜索失败: HTTP {response.status_code} {response.text[:120]}")
            return ""
        data = response.json()
        emails = data.get("emails") or []
        since_utc = since.astimezone(timezone.utc) if since and since.tzinfo else since.replace(tzinfo=timezone.utc) if since else None
        for item in emails:
            created_at = parse_iso_datetime(str(item.get("created_at") or item.get("date") or item.get("received_at") or ""))
            if since_utc and created_at and created_at < since_utc:
                continue
            content = "\n".join(
                str(item.get(key) or "")
                for key in ("subject", "from", "to", "date", "snippet", "body_excerpt", "from_email", "to_email")
            )
            code = extract_email_code(content)
            if code:
                print(f"[mail] 已从 iCloud 查询平台获取验证码: {code}")
                return code

        for item in emails:
            created_at = parse_iso_datetime(str(item.get("created_at") or item.get("date") or item.get("received_at") or ""))
            if since_utc and created_at and created_at < since_utc:
                continue
            mail_id = item.get("id")
            if not mail_id:
                continue
            body_response = session.get(
                f"https://icloud.thefindnet.xyz/public/get-email-body.php?id={mail_id}",
                headers={**headers, "Content-Type": "", "Referer": "https://icloud.thefindnet.xyz/"},
                timeout=timeout,
            )
            if body_response.status_code != 200:
                continue
            try:
                body_data = body_response.json()
            except Exception:
                body_data = {"html_body": body_response.text}
            content = "\n".join(
                str(body_data.get(key) or "")
                for key in ("subject", "from", "to", "date", "html_body", "text_body", "body", "snippet", "body_excerpt")
            )
            code = extract_email_code(content)
            if code:
                print(f"[mail] 已从 iCloud 查询平台正文获取验证码: {code}")
                return code

        if since_utc:
            print(f"[mail] iCloud 未找到时间窗口内新验证码，回退尝试当前最新验证码: {address}")
            return fetch_icloud_thefindnet_email_code(mail_url, email=email, timeout=timeout, since=None)
        print(f"[mail] iCloud 查询平台已读取邮箱但未找到验证码: {address}")
        return ""
    except Exception as exc:
        print(f"[mail] iCloud 查询平台获取失败: {exc}")
        return ""


def fetch_qq_imap_email_code(mail_url: str, email: str = "", timeout: int = 12, since: datetime | None = None) -> str:
    address = (email or extract_email_address(mail_url)).strip()
    auth_code = str(mail_url or "").strip()
    if not address or not auth_code:
        return ""
    try:
        from modules.mail_provider import fetch_qq_imap_code
        from modules.storage import MailAccount

        effective_since = since or (datetime.now(timezone.utc) - timedelta(minutes=10))
        account = MailAccount(email=address, mail_url=auth_code)
        code = run_async_blocking(fetch_qq_imap_code(account, effective_since, set()))
        if code:
            print(f"[mail] 已从 QQ IMAP 获取验证码: {code}")
            return str(code).strip()
        print(f"[mail] QQ IMAP 已读取邮箱但未找到新验证码: {address}")
        return ""
    except Exception as exc:
        print(f"[mail] QQ IMAP 获取失败: {exc}")
        return ""


def fetch_latest_email_code(mail_url: str, email: str = "", timeout: int = 12, since: datetime | None = None) -> str:
    if not mail_url:
        return ""
    mail_url = str(mail_url or "").strip()
    if email and is_moemail_email(email):
        return fetch_moemail_email_code(email, timeout=timeout)
    if email_domain(email) == "icloud.com" and not re.match(r"^https?://", mail_url, flags=re.IGNORECASE):
        effective_since = since or (datetime.now(timezone.utc) - timedelta(seconds=max(60, timeout)))
        return fetch_icloud_thefindnet_email_code(mail_url, email=email, timeout=timeout, since=effective_since)
    if email_domain(email) in QQ_IMAP_DOMAINS and not re.match(r"^https?://", mail_url, flags=re.IGNORECASE):
        effective_since = since or (datetime.now(timezone.utc) - timedelta(minutes=10))
        return fetch_qq_imap_email_code(mail_url, email=email, timeout=timeout, since=effective_since)
    embedded_email = extract_email_address(mail_url)
    if embedded_email and not re.match(r"^https?://", mail_url, flags=re.IGNORECASE):
        if is_moemail_email(embedded_email):
            return fetch_moemail_email_code(embedded_email, timeout=timeout)
        return ""
    try:
        import requests

        parsed = urllib.parse.urlparse(mail_url)
        if parsed.scheme and parsed.scheme not in {"http", "https"}:
            return ""
        if not parsed.scheme or not parsed.netloc:
            return ""
        host = parsed.netloc.lower()
        if embedded_email and is_moemail_email(embedded_email):
            return fetch_moemail_email_code(embedded_email, timeout=timeout)
        if host in {"mail.ssin.online", "ssin.online"}:
            return fetch_ssin_email_code(mail_url, email=email, timeout=timeout)
        if host == "nissanserena.my.id":
            return fetch_nissanserena_email_code(mail_url, email=email, timeout=timeout)
        if host in {"ray-otp.vercel.app", "www.cezhgpt.my.id", "cezhgpt.my.id"}:
            return fetch_ray_otp_email_code(mail_url, email=email, timeout=timeout)
        query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        query.append(("_ts", str(int(time.time()))))
        cache_busted = urllib.parse.urlunparse(parsed._replace(query=urllib.parse.urlencode(query)))
        response = requests.get(
            cache_busted,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
                ),
                "Cache-Control": "no-cache",
            },
            timeout=timeout,
        )
        if response.status_code != 200:
            print(f"[mail] 邮箱页面访问失败: HTTP {response.status_code}")
            return ""
        code = extract_email_code(response.text)
        if code:
            print(f"[mail] 已自动获取邮箱验证码: {code}")
        else:
            target = f" ({email})" if email else ""
            print(f"[mail] 暂未在邮箱页面识别到验证码{target}")
        return code
    except Exception as exc:
        print(f"[mail] 自动获取验证码失败: {exc}")
        return ""


def split_env_list(value: str) -> list[str]:
    return [item.strip().lower().lstrip("@") for item in re.split(r"[\s,，;；]+", str(value or "")) if item.strip()]


def email_domain(email: str) -> str:
    match = re.search(r"[\w.+-]+@([\w.-]+\.[A-Za-z]{2,})", str(email or ""))
    return match.group(1).strip().lower() if match else ""


def extract_email_address(value: str) -> str:
    match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", str(value or ""))
    return match.group(0).strip().lower() if match else ""


def env_bool(value: str, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def moemail_domain_whitelist() -> list[str]:
    env = load_root_env()
    configured = split_env_list(env.get("MOEMAIL_DOMAIN_WHITELIST", ""))
    merged = configured or DEFAULT_MOEMAIL_DOMAIN_WHITELIST
    return list(dict.fromkeys(domain.strip().lower().lstrip("@") for domain in merged if domain.strip()))


def is_moemail_enabled() -> bool:
    env = load_root_env()
    has_api_config = bool((env.get("MOEMAIL_BASE_URL") or "").strip() and (env.get("MOEMAIL_API_KEY") or "").strip())
    return env_bool(env.get("MOEMAIL_ENABLED", ""), default=has_api_config)


def is_moemail_email(email: str) -> bool:
    if not is_moemail_enabled():
        return False
    domain = email_domain(email)
    if not domain:
        return False
    for root in moemail_domain_whitelist():
        if domain == root or domain.endswith(f".{root}"):
            return True
    return False


def fetch_moemail_email_code(email: str, timeout: int = 12) -> str:
    if not email or not is_moemail_email(email):
        return ""
    env = load_root_env()
    base_url = (os.environ.get("MOEMAIL_BASE_URL") or env.get("MOEMAIL_BASE_URL") or "").strip().rstrip("/")
    api_key = (os.environ.get("MOEMAIL_API_KEY") or env.get("MOEMAIL_API_KEY") or "").strip()
    if not base_url or not api_key:
        print("[mail] MoeMail 配置未填写：请在 .env 配置 MOEMAIL_BASE_URL / MOEMAIL_API_KEY。")
        return ""
    try:
        import requests
        response = requests.get(
            f"{base_url}/api/otp/latest",
            params={"email": email, "_ts": str(int(time.time()))},
            headers={"X-API-Key": api_key, "Accept": "application/json"},
            timeout=timeout,
        )
        if response.status_code == 404:
            print(f"[mail] MoeMail 邮箱不存在或无权限: {email}")
            return ""
        if response.status_code != 200:
            print(f"[mail] MoeMail 获取验证码失败: HTTP {response.status_code} {response.text[:80]}")
            return ""
        data = response.json() if response.text else {}
        code = str(data.get("code") or "").strip()
        if not code:
            message = data.get("message") if isinstance(data.get("message"), dict) else {}
            code = extract_email_code("\n".join([
                str(data.get("subject") or ""),
                str(message.get("content") or ""),
                str(message.get("html") or ""),
            ]))
        if code:
            print(f"[mail] 已从 MoeMail 获取验证码: {code}")
            return code
        print(f"[mail] MoeMail 暂未识别到验证码: {email}")
    except Exception as exc:
        print(f"[mail] MoeMail 获取失败: {exc}")
    return ""


def inbox_loucer_domain_whitelist() -> list[str]:
    env = load_root_env()
    configured = split_env_list(env.get("INBOX_LOUCER_DOMAIN_WHITELIST", ""))
    merged = configured or DEFAULT_INBOX_LOUCER_DOMAIN_WHITELIST
    return list(dict.fromkeys(domain.strip().lower().lstrip("@") for domain in merged if domain.strip()))


def is_inbox_loucer_email(email: str) -> bool:
    domain = email_domain(email)
    if not domain:
        return False
    for root in inbox_loucer_domain_whitelist():
        if domain == root or domain.endswith(f".{root}"):
            return True
    return False


def fetch_hotmail_email_code(email: str, timeout: int = 12) -> str:
    if not email:
        return ""
    global _RUN_REDEEM_AVAILABLE, _RUN_REDEEM_IMPORT_ERROR_PRINTED
    if _RUN_REDEEM_AVAILABLE is False:
        return ""
    try:
        automation_dir = Path(os.environ.get("TEAM_PENDING_AUTOMATION_DIR", str(DEFAULT_AUTOMATION_DIR)))
        if not (automation_dir / "run_redeem.py").exists():
            _RUN_REDEEM_AVAILABLE = False
            if not _RUN_REDEEM_IMPORT_ERROR_PRINTED:
                _RUN_REDEEM_IMPORT_ERROR_PRINTED = True
                print(f"[mail] Hotmail token 池已禁用：未找到 run_redeem.py ({automation_dir})")
            return ""
        if str(automation_dir) not in sys.path:
            sys.path.insert(0, str(automation_dir))
        import run_redeem as rr
        _RUN_REDEEM_AVAILABLE = True

        project_dir = Path(os.environ.get("TEAM_PENDING_REGISTER_PROJECT_DIR", str(DEFAULT_REGISTER_PROJECT_DIR)))
        match = rr.find_latest_hotmail_verification_code(project_dir, email)
        if match and getattr(match, "code", ""):
            print(f"[mail] 宸蹭粠 Hotmail token 姹犺幏鍙栭獙璇佺爜: {match.code}")
            return str(match.code).strip()
    except ModuleNotFoundError as exc:
        if (exc.name or "") == "run_redeem":
            _RUN_REDEEM_AVAILABLE = False
            if not _RUN_REDEEM_IMPORT_ERROR_PRINTED:
                _RUN_REDEEM_IMPORT_ERROR_PRINTED = True
                print("[mail] Hotmail token 池已禁用：缺少 run_redeem 模块")
            return ""
        print(f"[mail] Hotmail token 姹犳湭鍛戒腑: {exc}")
    except Exception as exc:
        print(f"[mail] Hotmail token 姹犳湭鍛戒腑: {exc}")
    return ""


def fetch_inbox_loucer_email_code(email: str, timeout: int = 12) -> str:
    if not email:
        return ""
    env = load_root_env()
    base_url = (
        os.environ.get("INBOX_LOUCER_BASE_URL")
        or env.get("INBOX_LOUCER_BASE_URL")
        or env.get("LOUCER_INBOX_BASE_URL")
        or ""
    ).strip()
    username = (
        os.environ.get("INBOX_LOUCER_USERNAME")
        or env.get("INBOX_LOUCER_USERNAME")
        or env.get("LOUCER_INBOX_USERNAME")
        or ""
    ).strip()
    password = (
        os.environ.get("INBOX_LOUCER_PASSWORD")
        or env.get("INBOX_LOUCER_PASSWORD")
        or env.get("LOUCER_INBOX_PASSWORD")
        or ""
    ).strip()
    if not base_url or not username or not password:
        if is_inbox_loucer_email(email):
            print("[mail] 自建邮箱配置未填写：请在 .env 配置 INBOX_LOUCER_BASE_URL / USERNAME / PASSWORD。")
        return ""
    try:
        automation_dir = Path(os.environ.get("TEAM_PENDING_AUTOMATION_DIR", str(DEFAULT_AUTOMATION_DIR)))
        if str(automation_dir) not in sys.path:
            sys.path.insert(0, str(automation_dir))
        from inbox_loucer_client import InboxLoucerClient

        client = InboxLoucerClient(base_url=base_url, username=username, password=password, timeout_seconds=timeout)
        for message in client.list_messages(email, include_raw=True, raw_limit=5):
            derived = message.derived if isinstance(message.derived, dict) else {}
            code = str(derived.get("code") or "").strip()
            if not code:
                code = extract_email_code("\n".join([message.subject, message.raw_text]))
            if code:
                print(f"[mail] 已从自建邮箱获取验证码: {code}")
                return code
    except Exception as exc:
        print(f"[mail] 自建邮箱未命中: {exc}")
    return ""


def fetch_external_email_code(email: str, timeout: int = 12) -> str:
    lower_email = (email or "").strip().lower()
    if lower_email.endswith(("@hotmail.com", "@outlook.com", "@live.com", "@msn.com")):
        code = fetch_hotmail_code_from_local_pool(email, timeout=timeout)
        if code:
            return code
        # 兜底：保留旧的 token 池抓码逻辑
        code = fetch_hotmail_email_code(email, timeout=timeout)
        if code:
            return code
    if is_moemail_email(email):
        return fetch_moemail_email_code(email, timeout=timeout)
    return ""


def parse_iso_datetime(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        try:
            return parsedate_to_datetime(text)
        except Exception:
            return None


def run_async_blocking(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def fetch_hotmail_graph_mail_url_code(mail_url: str, email: str, timeout: int = 12) -> str:
    try:
        prefix = "hotmail_graph://"
        if not mail_url.startswith(prefix):
            return ""
        _address, _sep, query_text = mail_url[len(prefix):].partition("?")
        query = urllib.parse.parse_qs(query_text)
        client_id = (query.get("client_id") or [""])[0].strip()
        refresh_token = (query.get("refresh_token") or [""])[0].strip()
        if not client_id or not refresh_token:
            return ""
        from modules.mail_provider import fetch_hotmail_graph_code
        from modules.storage import MailAccount

        # The OTP email can arrive before the polling function starts, especially
        # with multiple visible browser workers. Look back a little instead of
        # filtering from "right now".
        since = datetime.now(timezone.utc) - timedelta(minutes=10)
        account = MailAccount(email=email, client_id=client_id, refresh_token=refresh_token, mail_url=mail_url)
        code = run_async_blocking(
            fetch_hotmail_graph_code(
                account,
                since,
                set(),
            )
        )
        if code:
            print(f"[mail] 已从 Hotmail/Outlook 获取验证码: {code}")
        else:
            print(f"[mail] Hotmail/Outlook 已读取邮箱但未找到新验证码: {email}")
        return code or ""
    except Exception as exc:
        print(f"[mail] Hotmail Graph 未命中: {exc}")
        return ""


def wait_latest_email_code(
    mail_url: str,
    email: str = "",
    timeout: int = 90,
    interval: float = 5.0,
    exclude: set[str] | None = None,
    since: datetime | None = None,
) -> str:
    deadline = time.time() + max(1, timeout)
    attempt = 0
    exclude = exclude or set()
    while time.time() < deadline:
        attempt += 1
        code = fetch_latest_email_code(mail_url, email=email, since=since)
        if code and code not in exclude:
            return code
        if code in exclude:
            print("[mail] 邮箱里还是上一条已尝试验证码，继续等待新验证码。")
        left = max(0, int(deadline - time.time()))
        if left <= 0:
            break
        print(f"[mail] 等待验证码邮件中... {left}s")
        time.sleep(max(1.0, interval))
    return ""


def wait_any_email_code(
    mail_url: str,
    email: str = "",
    timeout: int = 90,
    interval: float = 5.0,
    exclude: set[str] | None = None,
    since: datetime | None = None,
) -> str:
    normalized_mail_url = (mail_url or "").strip()
    # 兼容 "email----email" 旧格式，避免阻断外部抓码
    if email and normalized_mail_url and normalized_mail_url.lower() == email.lower():
        normalized_mail_url = ""
    effective_timeout = timeout
    effective_interval = interval
    if email and is_moemail_email(email):
        effective_timeout = min(int(timeout or 60), 60)
        effective_interval = min(float(interval or 2.0), 2.0)
    return wait_mail_adapter_code(
        email=email,
        mail_url=normalized_mail_url,
        timeout=effective_timeout,
        interval=effective_interval,
        exclude=exclude,
        fetch_latest=lambda url, account: fetch_hotmail_graph_mail_url_code(url, account, timeout=12)
        if url.startswith("hotmail_graph://")
        else fetch_latest_email_code(url, email=account, since=since),
        fetch_external=lambda account: "" if normalized_mail_url else fetch_external_email_code(account, timeout=12),
        sleep=time.sleep,
        now=time.time,
        log=print,
    )


def _iter_hotmail_pool_candidates() -> list[Path]:
    roots: list[Path] = [REPO_ROOT]
    if getattr(sys, "frozen", False):
        exe_root = Path(sys.executable).resolve().parent
        roots.extend([exe_root, exe_root / "_internal", REPO_ROOT.parent])
    roots.append(Path.cwd())

    candidates: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        for rel in (
            Path("data/hotmail/accounts.txt"),
            Path("data/hotmail/mail_pool.txt"),
            Path("data/mail_pool.txt"),
            Path("data/accounts.txt"),
        ):
            p = (root / rel).resolve()
            key = str(p).lower()
            if key in seen:
                continue
            seen.add(key)
            candidates.append(p)
    return candidates


def _find_hotmail_credentials_from_local_pool(email: str) -> tuple[str, str] | None:
    target = (email or "").strip().lower()
    if not target:
        return None
    try:
        from modules.storage import parse_mail_line
    except Exception:
        return None
    for path in _iter_hotmail_pool_candidates():
        try:
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                account = parse_mail_line(line.strip())
                if not account:
                    continue
                if (account.email or "").strip().lower() != target:
                    continue
                client_id = (account.client_id or "").strip()
                refresh_token = (account.refresh_token or "").strip()
                if client_id and refresh_token:
                    return client_id, refresh_token
        except Exception:
            continue
    return None


def fetch_hotmail_code_from_local_pool(email: str, timeout: int = 12) -> str:
    global _HOTMAIL_LOCAL_CREDS_MISS_LOGGED
    creds = _find_hotmail_credentials_from_local_pool(email)
    if not creds:
        key = (email or "").strip().lower()
        if key and key not in _HOTMAIL_LOCAL_CREDS_MISS_LOGGED:
            _HOTMAIL_LOCAL_CREDS_MISS_LOGGED.add(key)
            print(f"[mail] Hotmail 鏈湴姹犳湭鎵惧埌鍑嵁: {email}")
        return ""
    client_id, refresh_token = creds
    try:
        from modules.mail_provider import fetch_hotmail_graph_code
        from modules.storage import MailAccount

        print("[mail] matched hotmail credentials from local pool, using AppleEmail-first fetch")
        since = datetime.now(timezone.utc) - timedelta(minutes=10)
        account = MailAccount(
            email=email,
            client_id=client_id,
            refresh_token=refresh_token,
            mail_url=f"hotmail_graph://{email}?client_id={urllib.parse.quote(client_id, safe='')}&refresh_token={urllib.parse.quote(refresh_token, safe='')}",
        )
        code = run_async_blocking(fetch_hotmail_graph_code(account, since, set()))
        if code:
            print(f"[mail] got code from Hotmail/Outlook source: {code}")
            return str(code).strip()
        print(f"[mail] Hotmail/Outlook source had no fresh code yet: {email}")
        return ""
    except Exception as exc:
        print(f"[mail] local hotmail pool fetch has no fresh code yet: {email} ({exc})")
        return ""


def normalize_input_entry(entry: dict) -> dict:
    email = (entry.get("email") or entry.get("account") or "").strip()
    mail_url = (entry.get("mail_url") or "").strip()
    password = (entry.get("password") or "").strip()
    client_id = (entry.get("client_id") or "").strip()
    refresh_token = (entry.get("refresh_token") or "").strip()
    # 兼容 "email----email" 旧格式：第二段不是可用接码地址
    if email and mail_url and mail_url.lower() == email.lower():
        mail_url = ""
    if password.lower() in {"-", "--", "---", "----", "无", "none", "null", "empty", "验证码登录"}:
        password = ""
    if client_id and refresh_token and not mail_url:
        mail_url = f"hotmail_graph://{email}?client_id={urllib.parse.quote(client_id, safe='')}&refresh_token={urllib.parse.quote(refresh_token, safe='')}"
    source_format = "generic"
    if email.lower().endswith("@icloud.com") and mail_url and not re.match(r"^https?://", mail_url, flags=re.IGNORECASE):
        source_format = "icloud_query"
    elif email_domain(email) in QQ_IMAP_DOMAINS and mail_url and not re.match(r"^https?://", mail_url, flags=re.IGNORECASE):
        source_format = "qq_imap"
    return {
        "email": email,
        "password": password,
        "mail_url": mail_url,
        "client_id": client_id,
        "refresh_token": refresh_token,
        "source_format": source_format,
        "raw": entry.get("raw", ""),
    }


def parse_account_file_text(text: str) -> list[dict]:
    entries: list[dict] = []
    current: dict = {}
    current_raw: list[str] = []
    label_pattern = re.compile(
        r"(账号|账户|account|email|邮箱|密码|password|pass|邮箱地址|邮件地址|mail_url|mail url|mail|接码地址|验证码地址|邮箱链接|邮件链接)\s*[:：]\s*",
        flags=re.IGNORECASE,
    )

    def flush() -> None:
        nonlocal current, current_raw
        if current:
            current["raw"] = "\n".join(current_raw).strip()
            normalized = normalize_input_entry(current)
            if normalized["email"]:
                entries.append(normalized)
        current = {}
        current_raw = []

    def parse_labeled_segments(line: str) -> dict:
        matches = list(label_pattern.finditer(line))
        if not matches:
            return {}
        parsed: dict = {}
        for index, match in enumerate(matches):
            raw_label = match.group(1).strip().lower()
            value_start = match.end()
            value_end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
            value = line[value_start:value_end].strip()
            if not value:
                continue

            if raw_label in {"密码", "password", "pass"}:
                parsed["password"] = value
                continue

            if raw_label in {"邮箱地址", "邮件地址", "mail_url", "mail url", "mail", "接码地址", "验证码地址", "邮箱链接", "邮件链接"}:
                parsed["mail_url"] = value
                continue

            if raw_label in {"邮箱", "email"} and value.startswith(("http://", "https://")):
                parsed["mail_url"] = value
                continue

            email_match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", value)
            parsed["email"] = email_match.group(0) if email_match else value
        return parsed

    def merge_current(parsed: dict) -> None:
        for key in ("email", "password", "mail_url"):
            if parsed.get(key):
                current[key] = parsed[key]

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#"):
            continue
        if not line:
            flush()
            continue

        if "----" in line and re.match(r"^[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\s*----", line):
            flush()
            parts = [part.strip() for part in line.split("----", 3)]
            if len(parts) == 4:
                email, password, client_id, refresh_token = parts
                entries.append(
                    normalize_input_entry(
                        {
                            "email": email,
                            "password": password,
                            "client_id": client_id,
                            "refresh_token": refresh_token,
                            "raw": line,
                        }
                    )
                )
                continue
            email, tail = line.split("----", 1)
            entries.append(
                normalize_input_entry(
                    {
                        "email": email,
                        "mail_url": tail,
                        "raw": line,
                    }
                )
            )
            continue

        current_raw.append(raw_line)
        parsed_segments = parse_labeled_segments(line)
        if parsed_segments:
            if current.get("email") and parsed_segments.get("email") and parsed_segments["email"].lower() != current["email"].lower():
                current_raw.pop()
                flush()
                current_raw.append(raw_line)
            merge_current(parsed_segments)
            if current.get("email") and current.get("mail_url") and (parsed_segments.get("mail_url") or parsed_segments.get("password")):
                flush()
            continue

        match = re.match(r"^(账号|账户|account|email|邮箱)\s*[:：]\s*(.+)$", line, flags=re.IGNORECASE)
        if match:
            key = match.group(1).lower()
            value = match.group(2).strip()
            if key in {"邮箱", "email"} and value.startswith("http"):
                current["mail_url"] = value
            else:
                current["email"] = value
            continue

        match = re.match(r"^(密码|password|pass)\s*[:：]\s*(.+)$", line, flags=re.IGNORECASE)
        if match:
            current["password"] = match.group(2).strip()
            continue

        match = re.match(r"^(邮箱地址|邮件地址|mail|mail_url|mail url)\s*[:：]\s*(.+)$", line, flags=re.IGNORECASE)
        if match:
            current["mail_url"] = match.group(2).strip()
            continue

        if not current.get("email"):
            email_match = re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", line)
            if email_match:
                current["email"] = email_match.group(0)
        if not current.get("mail_url"):
            url_match = re.search(r"https?://\S+", line)
            if url_match:
                current["mail_url"] = url_match.group(0)

    flush()
    return entries


def load_account_inputs(path: str) -> list[dict]:
    account_path = Path(path)
    if not account_path.exists():
        raise RuntimeError(f"找不到账号输入文件: {account_path}")
    entries = parse_account_file_text(read_text(account_path))
    if not entries:
        raise RuntimeError(f"账号输入文件没有解析到有效账号: {account_path}")
    return entries


def normalize_pending_json_entry(record: dict, path: Path) -> dict:
    email = str(record.get("email") or record.get("account") or record.get("login_email") or "").strip()
    password = str(
        record.get("password")
        or record.get("login_password")
        or record.get("openai_password")
        or record.get("gpt_password")
        or ""
    ).strip()
    mail_url = str(
        record.get("mail_url")
        or record.get("mailUrl")
        or record.get("email_url")
        or record.get("mailbox_url")
        or ""
    ).strip()
    return {
        "email": email,
        "password": password,
        "mail_url": mail_url,
        "raw": str(path),
        "json_path": str(path),
        "pending_source_type": "pending_json",
        "pending_record": record,
    }


def normalize_pending_txt_entry(entry: dict, path: Path) -> dict:
    normalized = normalize_input_entry(entry)
    raw = str(entry.get("raw") or normalized.get("raw") or "")
    invite_url = ""
    for line in raw.splitlines():
        match = re.match(r"^(邀请链接|invite|invite_url|team_invite_url)\s*[:：]\s*(.+)$", line.strip(), flags=re.IGNORECASE)
        if match:
            invite_url = match.group(2).strip()
            break
    return {
        **normalized,
        "account_txt_path": str(path),
        "pending_source_type": "pending_txt",
        "pending_record": {
            "email": normalized["email"],
            "password": normalized["password"],
            "mail_url": normalized["mail_url"],
            "team_invite_url": invite_url,
            "source": "account.txt",
        },
    }


def load_pending_inputs(folder: str | Path) -> list[dict]:
    root = Path(folder)
    root.mkdir(parents=True, exist_ok=True)
    entries: list[dict] = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime):
        try:
            record = json.loads(read_text(path))
        except Exception as exc:
            print(f"[warn] pending JSON 读取失败，已跳过: {path} -> {exc}")
            continue
        if not isinstance(record, dict):
            print(f"[warn] pending JSON 不是对象，已跳过: {path}")
            continue
        entry = normalize_pending_json_entry(record, path)
        if not entry["email"]:
            print(f"[warn] pending JSON 缺少 email，已跳过: {path}")
            continue
        entries.append(entry)

    account_txt = root / "account.txt"
    if account_txt.exists():
        try:
            txt_entries = parse_account_file_text(read_text(account_txt))
        except Exception as exc:
            print(f"[warn] pending account.txt 读取失败，已跳过: {account_txt} -> {exc}")
            txt_entries = []
        for entry in txt_entries:
            normalized = normalize_pending_txt_entry(entry, account_txt)
            if normalized["email"]:
                entries.append(normalized)
    return entries


def load_pending_json_inputs(folder: str | Path) -> list[dict]:
    return load_pending_inputs(folder)


def remove_pending_json_file(entry: dict) -> bool:
    path = Path(entry.get("json_path") or "")
    if not path.exists():
        return False
    path.unlink()
    return True


def remove_pending_input_entry(entry: dict) -> bool:
    if entry.get("json_path"):
        return remove_pending_json_file(entry)
    if entry.get("account_txt_path"):
        return remove_account_from_input_file(entry["account_txt_path"], entry.get("email", ""))
    return False


def load_rt_txt_emails(path: str) -> set[str]:
    rt_path = Path(path)
    if not rt_path.exists():
        return set()
    emails = set()
    for line in read_text(rt_path).splitlines():
        email = line.split("----", 1)[0].strip().lower()
        if email:
            emails.add(email)
    return emails


def choose_account_input(args) -> dict | None:
    override = getattr(args, "account_input_override", None)
    if isinstance(override, dict):
        chosen = override
        print(f"[input] 选中账号: {chosen['email']} | password={'yes' if chosen.get('password') else 'no'}")
        if chosen.get("mail_url"):
            print("[input] 已读取邮箱地址/临时邮箱链接")
        if chosen.get("json_path"):
            print(f"[input] 已读取 pending JSON: {chosen['json_path']}")
        return chosen
    if not getattr(args, "account_file", ""):
        return None
    entries = load_account_inputs(args.account_file)
    account_email = (getattr(args, "account_email", "") or "").strip().lower()
    if account_email:
        chosen = next((entry for entry in entries if entry["email"].lower() == account_email), None)
        if not chosen:
            raise RuntimeError(f"账号输入文件中找不到指定邮箱: {account_email}")
    elif getattr(args, "account_index", 0):
        index = args.account_index
        if index < 1 or index > len(entries):
            raise RuntimeError(f"--account-index 超出范围: {index}，总数 {len(entries)}")
        chosen = entries[index - 1]
    else:
        stored = {
            (account.get("email") or "").strip().lower()
            for account in load_account_store(Path(args.store)).get("accounts", [])
        }
        stored.update(load_rt_txt_emails(args.rt_txt))
        chosen = next((entry for entry in entries if entry["email"].lower() not in stored), None)
        if not chosen:
            raise RuntimeError("account.txt 中的账号都已在账号库/RT TXT 中出现；可用 --account-index 指定重跑某个账号")
    print(f"[input] 选中账号: {chosen['email']} | password={'yes' if chosen['password'] else 'no'}")
    if chosen.get("mail_url"):
        print("[input] 已读取邮箱地址/临时邮箱链接")
    return chosen


def remove_account_from_input_file(path: str, email: str) -> bool:
    with INPUT_REMOVE_LOCK:
        account_path = Path(path)
        if not account_path.exists() or not email:
            return False
        target = email.strip().lower()
        lines = read_text(account_path).splitlines()
        kept: list[str] = []
        removed = False
        cleaned_orphans = False
        block: list[str] = []
        email_pattern = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
        credential_label_pattern = re.compile(
            r"^(密码|password|pass|邮箱地址|邮件地址|mail|mail_url|mail url|邀请链接|invite|invite_url|team_invite_url)\s*[:：]",
            flags=re.IGNORECASE,
        )

        def line_email(value: str) -> str:
            match = email_pattern.search(value)
            return match.group(0).strip().lower() if match else ""

        def block_has_target(items: list[str]) -> bool:
            return any(target in line.lower() for line in items)

        def block_has_any_email(items: list[str]) -> bool:
            return any(line_email(line) for line in items)

        def is_orphan_credential_block(items: list[str]) -> bool:
            meaningful = [
                line.strip()
                for line in items
                if line.strip() and not line.strip().startswith("#")
            ]
            if not meaningful or block_has_any_email(items):
                return False
            return all(credential_label_pattern.match(line) for line in meaningful)

        def flush_block() -> None:
            nonlocal block, removed, cleaned_orphans
            if not block:
                return
            if block_has_target(block):
                removed = True
            elif is_orphan_credential_block(block):
                cleaned_orphans = True
            else:
                kept.extend(block)
            block = []

        for line in lines:
            stripped = line.strip()
            if stripped.startswith("#"):
                flush_block()
                kept.append(line)
                continue
            if "----" in stripped:
                flush_block()
                if stripped.split("----", 1)[0].strip().lower() == target:
                    removed = True
                else:
                    kept.append(line)
                continue
            if line_email(stripped) and block_has_any_email(block):
                flush_block()
            block.append(line)

        flush_block()
        if removed or cleaned_orphans:
            while kept and not kept[-1].strip():
                kept.pop()
            write_text(account_path, ("\n".join(kept) + "\n") if kept else "")
        return removed


def auth_kind_config(kind: str) -> dict:
    auth_kind = (kind or "").strip().lower()
    if auth_kind in {"1", "team", "t", "helper", "team-helper", "team_helper"}:
        return {
            "kind": "team_helper",
            "title": "team helper 专用",
            "input_type": "account_txt",
            "account_file": str(DEFAULT_TEAM_ACCOUNT_FILE),
            "output_dir": str(DEFAULT_TEAM_OUTPUT_DIR),
            "rt_txt": str(DEFAULT_TEAM_RT_TXT),
        }
    if auth_kind in {"2", "plus", "p", "normal", "普通", "common"}:
        return {
            "kind": "normal",
            "title": "普通授权文件生成",
            "input_type": "account_txt",
            "account_file": str(DEFAULT_PLUS_ACCOUNT_FILE),
            "output_dir": str(DEFAULT_PLUS_OUTPUT_DIR),
            "rt_txt": str(DEFAULT_PLUS_RT_TXT),
        }
    if auth_kind in {"3", "team_pending", "team-pending", "teampending", "pending"}:
        return {
            "kind": "team_pending",
            "title": "team pending 授权",
            "input_type": "pending_json",
            "account_file": str(DEFAULT_TEAM_PENDING_INPUT_DIR),
            "output_dir": str(DEFAULT_TEAM_CHILD_OUTPUT_DIR),
            "rt_txt": str(DEFAULT_TEAM_CHILD_RT_TXT),
        }
    if auth_kind in {"4", "gopay", "go-pay", "gopay_manual", "gopay-manual", "gopay手动订阅"}:
        return {
            "kind": "gopay_manual",
            "title": "gopay手动订阅",
            "input_type": "account_txt",
            "manual_subscription": True,
            "account_file": str(DEFAULT_GOPAY_ACCOUNT_FILE),
            "output_dir": str(DEFAULT_GOPAY_OUTPUT_DIR),
            "rt_txt": str(DEFAULT_GOPAY_ACCOUNT_TXT),
        }
    raise RuntimeError(f"未知授权类型: {kind}")


def ensure_auth_project_layout() -> None:
    for path in [
        DEFAULT_TEAM_ACCOUNT_FILE,
        DEFAULT_PLUS_ACCOUNT_FILE,
        DEFAULT_GOPAY_ACCOUNT_FILE,
        DEFAULT_TEAM_PENDING_INPUT_DIR / ".keep",
        DEFAULT_TEAM_RT_TXT,
        DEFAULT_PLUS_RT_TXT,
        DEFAULT_TEAM_CHILD_RT_TXT,
        DEFAULT_GOPAY_ACCOUNT_TXT,
    ]:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            write_text(path, "")
    DEFAULT_GOPAY_ACCOUNT_JSON.parent.mkdir(parents=True, exist_ok=True)
    if not DEFAULT_GOPAY_ACCOUNT_JSON.exists():
        write_text(DEFAULT_GOPAY_ACCOUNT_JSON, json.dumps({"updated_at": "", "accounts": []}, ensure_ascii=False, indent=2))
    DEFAULT_TEAM_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_PLUS_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_TEAM_CHILD_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DEFAULT_GOPAY_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    state_db.init_db(DEFAULT_STATE_DB)


def exchange_code(code: str, code_verifier: str, *, fallback_email: str = "") -> dict:
    bundle = _exchange_auth_code(code, code_verifier, fallback_email=fallback_email)
    if not bundle:
        raise RuntimeError("OAuth code 交换 token 失败")
    return bundle


def capture_code_from_url(url: str) -> str:
    if f"localhost:{CODEX_CALLBACK_PORT}/auth/callback" not in (url or ""):
        return ""
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    fragment = urllib.parse.parse_qs(parsed.fragment)
    return (query.get("code") or fragment.get("code") or [""])[0].strip()


def is_oauth_consent_url(url: str) -> bool:
    lower_url = (url or "").lower()
    return (
        "oauth/authorize" in lower_url
        or "sign-in-with-chatgpt" in lower_url
        or "/codex/consent" in lower_url
        or "consent" in lower_url
    )


def is_phone_required_page(page) -> bool:
    lower_url = (page.url or "").lower()
    if "/add-phone" in lower_url or "add-phone" in lower_url:
        return True
    try:
        body = page.locator("body").inner_text(timeout=800).lower().replace("\n", " ")
    except Exception:
        return False
    return any(
        hint in body
        for hint in [
            "phone number is required",
            "phone number required",
            "电话号码是必填项",
            "电话号码为必填项",
            "添加电话号码",
            "add phone",
        ]
    )


def hero_sms_enabled(args) -> bool:
    return bool(str(getattr(args, "hero_sms_api_key", "") or "").strip() and str(getattr(args, "hero_sms_country", "") or "").strip())


def sms_provider_name(args) -> str:
    name = str(getattr(args, "sms_provider", "") or "").strip().lower()
    if not name and hero_sms_enabled(args):
        return "herosms"
    if name in {"hero", "hero_sms", "herosms"}:
        return "herosms"
    if name in {"grizzly", "grizzlysms", "grizzly_sms"}:
        return "grizzly"
    if name in {"fivesim", "5sim", "five_sim", "5sims"}:
        return "fivesim"
    return name


def sms_enabled(args) -> bool:
    return bool(
        str(getattr(args, "sms_api_key", "") or getattr(args, "hero_sms_api_key", "") or "").strip()
        and str(getattr(args, "sms_country", "") or getattr(args, "hero_sms_country", "") or "").strip()
    )


def selected_sms_country(args) -> PhoneCountry:
    country_id = int(getattr(args, "sms_country", 0) or getattr(args, "hero_sms_country", 0) or 0)
    return PhoneCountry(
        iso_code=str(getattr(args, "sms_country_iso", "") or getattr(args, "hero_sms_country_iso", "") or "").strip().upper(),
        dial_code=str(getattr(args, "sms_dial_code", "") or getattr(args, "hero_sms_dial_code", "") or "").strip().lstrip("+"),
        name=str(getattr(args, "sms_country_name", "") or getattr(args, "hero_sms_country_name", "") or "").strip() or str(country_id),
        hero_sms_country=country_id,
    )


def selected_hero_sms_country(args) -> PhoneCountry:
    hero_country = int(getattr(args, "hero_sms_country", 0) or 0)
    return PhoneCountry(
        iso_code=str(getattr(args, "hero_sms_country_iso", "") or "").strip().upper(),
        dial_code=str(getattr(args, "hero_sms_dial_code", "") or "").strip().lstrip("+"),
        name=str(getattr(args, "hero_sms_country_name", "") or "").strip() or str(hero_country),
        hero_sms_country=hero_country,
    )


def find_phone_input(page):
    for selector in ('input[name="phoneNumberInput"]', 'input[type="tel"]', 'input[autocomplete="tel"]', 'input[name*="phone" i]'):
        found = maybe_visible(page, selector, timeout=800)
        if found:
            return found
    return None


def select_phone_country(page, country: PhoneCountry) -> None:
    if not country.dial_code and not country.iso_code:
        return
    print(f"[SMS] 选择手机号国家: {country.name} +{country.dial_code}")
    try:
        already = page.evaluate(
            """(code) => {
                for (const node of document.querySelectorAll('button, select')) {
                    const text = (node.innerText || node.textContent || '').trim();
                    if (text.includes(`+${code}`) || text.includes(`(${code})`)) return text;
                }
                return '';
            }""",
            country.dial_code,
        )
        if already:
            print(f"[SMS] 页面国家已匹配: {already}")
            return
    except Exception:
        pass

    try:
        changed = page.evaluate(
            """({ iso, code, name }) => {
                const select = document.querySelector('select');
                if (!select) return '';
                const options = Array.from(select.options || []);
                let target = null;
                if (iso) target = options.find(opt => String(opt.value || '').toUpperCase() === iso);
                if (!target && name) target = options.find(opt => (opt.text || '').includes(name));
                if (!target && code) target = options.find(opt => (opt.text || '').includes(`+${code}`) || (opt.text || '').includes(`(${code})`));
                if (!target) return '';
                const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
                if (setter) setter.call(select, target.value);
                else select.value = target.value;
                select.dispatchEvent(new Event('input', { bubbles: true }));
                select.dispatchEvent(new Event('change', { bubbles: true }));
                for (const button of document.querySelectorAll('button')) {
                    const text = (button.innerText || '').trim();
                    if (text.includes(`+${code}`) || text.includes(`(${code})`)) return text;
                }
                return target.text || target.value;
            }""",
            {"iso": country.iso_code, "code": country.dial_code, "name": country.name},
        )
        if changed:
            print(f"[SMS] 已选择国家: {changed}")
            time.sleep(0.8)
            return
    except Exception as exc:
        print(f"[SMS] select 国家选择失败，继续尝试备用方式: {str(exc).splitlines()[0]}")

    try:
        button = page.locator('button[aria-haspopup="listbox"]').filter(has_text=re.compile(r"\+\d")).first
        if button.is_visible(timeout=1000):
            button.click(timeout=3000)
            time.sleep(1)
            option = None
            if country.iso_code:
                option = page.locator(f'[data-key="{country.iso_code}"]').first
                if not option.is_visible(timeout=800):
                    option = None
            if option is None:
                option = page.get_by_text(re.compile(rf"(\+|\(){re.escape(country.dial_code)}\)?")).first
            if option and option.is_visible(timeout=2000):
                option.click(timeout=3000)
                print(f"[SMS] 已通过下拉选择国家 +{country.dial_code}")
                time.sleep(0.8)
                return
    except Exception as exc:
        print(f"[SMS] 下拉国家选择失败，继续使用完整号码兜底: {str(exc).splitlines()[0]}")


def click_phone_submit(page, field=None) -> bool:
    try:
        clicked = page.evaluate(
            """() => {
                const labels = ['继续', 'Continue', 'Next', 'Verify', 'Submit'];
                for (const button of document.querySelectorAll('button[type="submit"], button')) {
                    const text = (button.innerText || '').trim();
                    if (!labels.some(label => text === label || text.includes(label))) continue;
                    if (button.disabled) continue;
                    const rect = button.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) continue;
                    ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click'].forEach(type => {
                        button.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
                    });
                    return true;
                }
                return false;
            }"""
        )
        if clicked:
            return True
    except Exception:
        pass
    if field is not None:
        return _click_primary_auth_button(page, field, ["Continue", "继续", "Next", "Verify"])
    return False


def fill_phone_and_wait_sms_page(page, phone: str, country: PhoneCountry) -> None:
    print(f"[SMS] 开始处理手机号页面: 国家={country.name} ISO={country.iso_code or '-'} 区号=+{country.dial_code or '-'}", flush=True)
    select_phone_country(page, country)
    phone_input = find_phone_input(page)
    if not phone_input:
        raise RuntimeError("未找到手机号输入框")
    print("[SMS] 已找到手机号输入框", flush=True)
    current_country = ""
    try:
        current_country = str(
            page.evaluate(
                r"""() => {
                    for (const node of document.querySelectorAll('button, select')) {
                        const text = (node.innerText || node.textContent || '').trim();
                        const match = text.match(/\+(\d+)/);
                        if (match) return match[1];
                    }
                    return '';
                }"""
            )
            or ""
        )
    except Exception:
        current_country = ""
    full_digits = re.sub(r"\D+", "", phone)
    value = local_phone_number(phone, country) if country.dial_code and current_country == country.dial_code else full_digits
    number_kind = "本地号码" if value != full_digits else "完整号码"
    print(f"[SMS] 准备填入手机号: 接码号码={phone}, 页面国家=+{current_country or '-'}, 输入类型={number_kind}, 输入值={value}", flush=True)
    if not fill_auth_field(phone_input, value, label="手机号"):
        raise RuntimeError("手机号填写失败")
    print("[SMS] 手机号已填入页面", flush=True)
    if not click_phone_submit(page, phone_input):
        raise RuntimeError("手机号提交按钮点击失败")
    print("[SMS] 已点击手机号页面继续/提交按钮，等待验证码页面", flush=True)
    time.sleep(4)


def find_sms_code_input(page):
    for selector in (
        'input[name="code"]',
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"]',
        'input[type="tel"]',
        'input[type="text"]',
        'input[type="number"]',
    ):
        try:
            locators = page.locator(selector)
            count = min(locators.count(), 8)
            for index in range(count):
                locator = locators.nth(index)
                if not locator.is_visible(timeout=400):
                    continue
                try:
                    name = locator.get_attribute("name", timeout=400) or ""
                except Exception:
                    name = ""
                if name == "phoneNumberInput":
                    continue
                return locator
        except Exception:
            continue
    return None


def page_looks_like_sms_verification(page) -> bool:
    lower_url = (page.url or "").lower()
    if "contact-verification" in lower_url or "phone-verification" in lower_url:
        return True
    try:
        text = page.locator("body").inner_text(timeout=800).lower()
    except Exception:
        text = ""
    return bool(find_sms_code_input(page) and any(hint in text for hint in ("sms", "text message", "verification code", "验证码", "短信")))


def fill_sms_code(page, code: str) -> None:
    code_input = find_sms_code_input(page)
    if not code_input:
        raise RuntimeError("未找到短信验证码输入框")
    print(f"[SMS] 已找到短信验证码输入框，准备填入验证码: {code}", flush=True)
    if not fill_auth_field(code_input, code, label="验证码"):
        raise RuntimeError("短信验证码填写失败")
    print("[SMS] 短信验证码已填入页面", flush=True)
    click_phone_submit(page, code_input)
    print("[SMS] 已点击验证码页面继续/提交按钮", flush=True)
    time.sleep(4)


def handle_phone_required_with_hero_sms(page, args, remaining_seconds) -> bool:
    if not hero_sms_enabled(args):
        return False
    provider = HeroSMSProvider(str(args.hero_sms_api_key).strip())
    country = selected_hero_sms_country(args)
    activation = None
    try:
        service = str(getattr(args, "hero_sms_service", "") or "dr")
        operator = str(getattr(args, "hero_sms_operator", "") or "").strip()
        print(
            f"[SMS] HeroSMS 自动接码启动: service={service}, country={country.name}({country.hero_sms_country}), "
            f"operator={operator or '任何运营商'}",
            flush=True,
        )
        activation = provider.get_number(
            service,
            country.hero_sms_country,
            operator=operator,
        )
        provider.mark_ready(activation.activation_id)
        fill_phone_and_wait_sms_page(page, activation.phone_number, country)
        deadline = time.time() + min(max(30, int(remaining_seconds())), int(float(getattr(args, "hero_sms_poll_interval", 5.0) or 5.0) * int(getattr(args, "hero_sms_max_attempts", 60) or 60)) + 10)
        wait_round = 0
        while time.time() < deadline:
            wait_round += 1
            if page_looks_like_sms_verification(page):
                print(f"[SMS] 页面已进入短信验证码阶段，开始向 HeroSMS 拉取验证码", flush=True)
                break
            if capture_code_from_url(page.url):
                print("[SMS] 页面已直接产生 OAuth 回调，无需短信验证码", flush=True)
                provider.complete(activation.activation_id)
                return True
            if wait_round == 1 or wait_round % 5 == 0:
                print(f"[SMS] 等待短信验证码输入页出现... 当前 URL: {page.url}", flush=True)
            time.sleep(1)
        code = provider.poll_for_code(
            activation.activation_id,
            interval=float(getattr(args, "hero_sms_poll_interval", 5.0) or 5.0),
            max_attempts=int(getattr(args, "hero_sms_max_attempts", 60) or 60),
        )
        fill_sms_code(page, code)
        status, detail = wait_for_code_submit_result(page, timeout=12)
        if status == "invalid":
            raise RuntimeError(f"短信验证码无效或过期: {detail}")
        if status == "pending":
            print("[SMS] 验证码已提交，页面暂未明确推进，继续观察授权流程", flush=True)
        else:
            print("[SMS] 验证码提交成功，页面已推进", flush=True)
        provider.complete(activation.activation_id)
        return True
    except Exception as exc:
        print(f"[SMS] HeroSMS 自动接码失败: {exc}", flush=True)
        if activation:
            provider.cancel(activation.activation_id)
        raise


def handle_phone_required_with_sms_provider(page, args, remaining_seconds) -> bool:
    if not sms_enabled(args):
        return False
    provider_name = sms_provider_name(args) or "herosms"
    if provider_name in {"fivesim", "5sim"}:
        provider_name = "fivesim"
    api_key = str(getattr(args, "sms_api_key", "") or getattr(args, "hero_sms_api_key", "") or "").strip()
    if provider_name == "grizzly":
        provider = GrizzlySMSProvider(api_key)
        label = "GrizzlySMS"
    elif provider_name == "fivesim":
        provider = FiveSimProvider(api_key)
        label = "5sim"
    else:
        provider = HeroSMSProvider(api_key)
        label = "HeroSMS"
    country = selected_sms_country(args)
    activation = None
    try:
        default_service = "openai" if provider_name == "fivesim" else "dr"
        service = str(getattr(args, "sms_service", "") or getattr(args, "hero_sms_service", "") or default_service).strip() or default_service
        operator = str(getattr(args, "sms_operator", "") or getattr(args, "hero_sms_operator", "") or "").strip()
        if provider_name == "fivesim" and not operator:
            operator = "any"
        print(
            f"[SMS] {label} 自动接码启动: service={service}, country={country.name}({country.hero_sms_country}), "
            f"operator={operator or '任何运营商'}",
            flush=True,
        )
        if provider_name == "fivesim":
            slug = str(getattr(args, "fivesim_country_slug", "") or "").strip().lower()
            if not slug:
                # 兜底：按 ISO 映射成 slug
                from modules.fivesim_sms_provider import FIVESIM_ISO_TO_COUNTRY

                slug = FIVESIM_ISO_TO_COUNTRY.get(country.iso_code.upper(), "")
            if not slug:
                raise RuntimeError("5sim 缺少国家 slug，无法请求号码")
            country_arg = slug
        else:
            country_arg = country.hero_sms_country
        activation = provider.get_number(
            service,
            country_arg,
            operator=operator,
        )
        provider.mark_ready(activation.activation_id)
        fill_phone_and_wait_sms_page(page, activation.phone_number, country)
        poll_interval = float(getattr(args, "sms_poll_interval", 0) or getattr(args, "hero_sms_poll_interval", 5.0) or 5.0)
        max_attempts = int(getattr(args, "sms_max_attempts", 0) or getattr(args, "hero_sms_max_attempts", 60) or 60)
        deadline = time.time() + min(max(30, int(remaining_seconds())), int(poll_interval * max_attempts) + 10)
        wait_round = 0
        while time.time() < deadline:
            wait_round += 1
            if page_looks_like_sms_verification(page):
                print(f"[SMS] 页面已进入短信验证码阶段，开始向 {label} 拉取验证码", flush=True)
                break
            if capture_code_from_url(page.url):
                print("[SMS] 页面已直接产生 OAuth 回调，无需短信验证码", flush=True)
                provider.complete(activation.activation_id)
                return True
            if wait_round == 1 or wait_round % 5 == 0:
                print(f"[SMS] 等待短信验证码输入页出现... 当前 URL: {page.url}", flush=True)
            time.sleep(1)
        code = provider.poll_for_code(
            activation.activation_id,
            interval=poll_interval,
            max_attempts=max_attempts,
        )
        fill_sms_code(page, code)
        status, detail = wait_for_code_submit_result(page, timeout=12)
        if status == "invalid":
            raise RuntimeError(f"短信验证码无效或过期: {detail}")
        if status == "pending":
            print("[SMS] 验证码已提交，页面暂未明确推进，继续观察授权流程", flush=True)
        else:
            print("[SMS] 验证码提交成功，页面已推进", flush=True)
        provider.complete(activation.activation_id)
        return True
    except Exception as exc:
        print(f"[SMS] {label} 自动接码失败: {exc}", flush=True)
        if activation:
            provider.cancel(activation.activation_id)
        raise


def maybe_visible(page, selector: str, timeout: int = 1000):
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        frames = [page.main_frame]
        frames.extend(frame for frame in page.frames if frame != page.main_frame)
        for frame in frames:
            try:
                locator = frame.locator(selector).first
                if locator.is_visible(timeout=250):
                    return locator
            except Exception:
                pass
        time.sleep(0.2)
    return None


def detect_otp_error(page) -> str:
    try:
        body = page.locator("body").inner_text(timeout=1000).lower().replace("\n", " ")
    except Exception:
        return ""
    for hint in OTP_INVALID_HINTS:
        if hint in body:
            return hint
    return ""


def detect_password_error(page) -> str:
    try:
        body = page.locator("body").inner_text(timeout=1000).lower().replace("\n", " ")
    except Exception:
        return ""
    for hint in PASSWORD_INVALID_HINTS:
        if hint.lower() in body:
            return hint
    return ""


def detect_auth_invalid_state(page) -> str:
    try:
        body = page.locator("body").inner_text(timeout=1000).lower().replace("\n", " ")
    except Exception:
        body = ""
    lower_url = (page.url or "").lower()
    haystack = f"{lower_url} {body}"
    if "no_valid_organizations" in haystack:
        return "no_valid_organizations"
    for hint in AUTH_INVALID_STATE_HINTS:
        if hint.lower() in haystack:
            return hint
    return ""


def click_auth_invalid_state_retry(page) -> bool:
    selectors = [
        'button:has-text("重试")',
        'button:has-text("再试一次")',
        '[role="button"]:has-text("重试")',
        '[role="button"]:has-text("再试一次")',
        'button:has-text("Retry")',
        '[role="button"]:has-text("Retry")',
        'button:has-text("Try again")',
        '[role="button"]:has-text("Try again")',
        '[aria-label*="重试"]',
        '[title*="重试"]',
        'input[type="button"][value*="重试"]',
        'input[type="submit"][value*="重试"]',
    ]
    for selector in selectors:
        try:
            items = page.locator(selector)
            count = min(items.count(), 6)
            for index in range(count - 1, -1, -1):
                item = items.nth(index)
                if item.is_visible(timeout=500) and item.is_enabled(timeout=500):
                    item.click(timeout=3000)
                    return True
        except Exception:
            pass
    try:
        button = page.get_by_role("button", name=re.compile(r"^(重试|再试一次|Retry|Try again)$", re.I)).last
        if button.is_visible(timeout=1000) and button.is_enabled(timeout=1000):
            button.click(timeout=3000)
            return True
    except Exception:
        pass
    try:
        fuzzy = page.locator(
            'button:has-text("重试"), button:has-text("再试一次"), '
            '[role="button"]:has-text("重试"), [role="button"]:has-text("再试一次"), '
            'button:has-text("Retry"), button:has-text("Try again")'
        ).last
        if fuzzy.is_visible(timeout=1000) and fuzzy.is_enabled(timeout=1000):
            fuzzy.click(timeout=3000)
            return True
    except Exception:
        pass
    try:
        clicked = page.evaluate(
            """() => {
                const isVisible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 10 || r.height < 10) return false;
                    const s = window.getComputedStyle(el);
                    return s && s.display !== 'none' && s.visibility !== 'hidden' && Number(s.opacity || '1') > 0.05;
                };
                const keys = ['重试', '再试一次', 'retry', 'try again'];
                const nodes = Array.from(document.querySelectorAll(
                    'button, [role="button"], a, input[type="button"], input[type="submit"], div[role="button"], span'
                ));
                for (let i = nodes.length - 1; i >= 0; i--) {
                    const el = nodes[i];
                    if (!isVisible(el)) continue;
                    const t = ((el.innerText || el.textContent || el.value || '') + '').toLowerCase().replace(/\\s+/g, ' ').trim();
                    if (!t) continue;
                    if (!keys.some((k) => t.includes(k))) continue;
                    const target = el.closest('button,[role="button"],a,input[type="button"],input[type="submit"],div[role="button"]') || el;
                    try {
                        target.click();
                        return true;
                    } catch {}
                }
                return false;
            }"""
        )
        if clicked:
            return True
    except Exception:
        pass
    return False


def retry_auth_invalid_state_in_place(page, args, invalid_state: str, attempt: int, max_attempts: int, *, label: str = "") -> bool:
    prefix = f"[{label}] " if label else ""
    set_auth_stage(args, "retry_auth_invalid_state")
    print(f"[login] {prefix}验证过程出错({invalid_state})，尝试点击“重试”刷新当前页面 ({attempt}/{max_attempts})。")
    if not click_auth_invalid_state_retry(page):
        print(f"[login] {prefix}未找到“重试”按钮，将按原失败逻辑处理。")
        return False
    deadline = time.time() + 12
    while time.time() < deadline:
        time.sleep(0.6)
        if not detect_auth_invalid_state(page):
            print(f"[login] {prefix}重试后错误页已消失，继续当前账号授权流程。")
            return True
    print(f"[login] {prefix}点击重试后仍停留在错误页，继续观察/必要时再次重试。")
    return True


def wait_for_password_submit_result(page, timeout: int = 8) -> tuple[str, str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        err = detect_password_error(page)
        if err:
            return "invalid", err
        if not maybe_visible(page, PASSWORD_SELECTORS, timeout=300):
            return "accepted", ""
        time.sleep(0.5)
    err = detect_password_error(page)
    if err:
        return "invalid", err
    return "pending", ""


def wait_for_code_submit_result(page, timeout: int = 12) -> tuple[str, str]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        err = detect_otp_error(page)
        if err:
            return "invalid", err
        if not maybe_visible(page, CODE_SELECTORS, timeout=300):
            return "accepted", ""
        time.sleep(0.5)
    err = detect_otp_error(page)
    if err:
        return "invalid", err
    return "pending", ""


def find_consent_button(page):
    candidates = [
        'button:has-text("继续"), button:has-text("Continue"), button:has-text("Allow")',
        'button:has-text("同意"), button:has-text("Authorize"), button:has-text("授权")',
        'button:has-text("Continue to Codex"), button:has-text("Sign in with ChatGPT")',
        'button[type="submit"], input[type="submit"]',
    ]
    for selector in candidates:
        try:
            buttons = page.locator(selector)
            count = min(buttons.count(), 8)
            for index in range(count - 1, -1, -1):
                btn = buttons.nth(index)
                if btn.is_visible(timeout=500) and btn.is_enabled(timeout=500):
                    return btn
        except Exception:
            pass
    try:
        body_buttons = page.locator("button").filter(has_not_text=re.compile(r"忘记|forgot", re.I))
        count = min(body_buttons.count(), 12)
        for index in range(count - 1, -1, -1):
            btn = body_buttons.nth(index)
            if btn.is_visible(timeout=300) and btn.is_enabled(timeout=300):
                text = (btn.inner_text(timeout=300) or "").strip()
                if text and len(text) <= 40:
                    return btn
    except Exception:
        pass
    return None


def click_consent_or_workspace(page, label: str = "", capture=None, has_auth_code=None) -> bool:
    prefix = f"[{label}] " if label else ""
    btn = find_consent_button(page)
    if not btn:
        return False

    lock_needed = is_oauth_consent_url(page.url)
    lock = CONSENT_LOCK if lock_needed else threading.Lock()
    with lock:
        try:
            if capture and capture(page.url):
                return True
            btn = find_consent_button(page)
            if not btn:
                return False
            if lock_needed:
                print(f"[login] {prefix}进入 OAuth consent 单通道，准备点击授权。")
            print(f"[login] {prefix}点击继续/授权")
            try:
                btn.scroll_into_view_if_needed(timeout=1000)
            except Exception:
                pass
            btn.click(timeout=5000)
            deadline = time.time() + (5 if lock_needed else 2)
            while time.time() < deadline:
                if capture and capture(page.url):
                    return True
                if has_auth_code and has_auth_code():
                    return True
                time.sleep(0.25)
            return True
        except Exception as exc:
            print(f"[debug] {prefix}授权按钮点击未完成: {str(exc).splitlines()[0]}")
            return False


def handle_authorization_step(page, auth_mode: str, label: str = "", capture=None, has_auth_code=None) -> bool:
    # 普通授权不主动选择 workspace/个人空间，只按当前页面继续授权。
    # 如果 OpenAI 页面要求选择项目，用户可以在浏览器中手动保持默认后点继续。
    return click_consent_or_workspace(page, label=label, capture=capture, has_auth_code=has_auth_code)


def click_otp_switch(page) -> bool:
    switch = maybe_visible(page, OTP_SWITCH_SELECTORS, timeout=1500)
    if not switch:
        return False
    try:
        print("[login] 切换到邮箱验证码登录")
        switch.click()
        time.sleep(3)
        return True
    except Exception:
        return False


def click_chatgpt_login(page) -> bool:
    candidates = [
        page.get_by_role("button", name=re.compile(r"^(登录|登陆|Log in)$", re.I)).last,
        page.get_by_role("link", name=re.compile(r"^(登录|登陆|Log in)$", re.I)).last,
        page.locator(CHATGPT_LOGIN_SELECTORS).last,
    ]
    for login in candidates:
        try:
            if login.is_visible(timeout=1200) and login.is_enabled(timeout=1200):
                print("[gopay] 点击官网登录")
                login.click(timeout=5000)
                time.sleep(3)
                return True
        except Exception:
            pass
    return False


def is_chatgpt_logged_in(page) -> bool:
    lower_url = (page.url or "").lower()
    if "auth.openai.com" in lower_url or "/auth/login" in lower_url or "/auth/signin" in lower_url:
        return False
    if maybe_visible(page, CHATGPT_LOGIN_SELECTORS, timeout=500):
        return False
    if maybe_visible(page, CHATGPT_SIGNUP_SELECTORS, timeout=500):
        return False
    if maybe_visible(page, CHATGPT_LOGGED_IN_SELECTORS, timeout=1000):
        return True
    try:
        body = page.locator("body").inner_text(timeout=1000).lower()
    except Exception:
        body = ""
    if not body:
        return False
    logged_out_hints = [
        "log in",
        "sign up",
        "登录",
        "登陆",
        "免费注册",
        "获取为你量身定制的回复",
        "log in to get",
    ]
    if any(hint in body for hint in logged_out_hints):
        return False
    if "chatgpt.com" in lower_url and any(hint in body for hint in ["upgrade plan", "升级套餐", "settings", "设置"]):
        return True
    return False


def click_auth_continue(page, field=None) -> bool:
    if field is not None and _click_primary_auth_button(page, field, list(AUTH_CONTINUE_LABELS)):
        return True
    label_re = re.compile(r"^(继续|下一步|登录|登陆|验证|确认|Continue|Next|Log in|Verify|Submit)$", re.I)
    candidates = [
        page.get_by_role("button", name=label_re).last,
        page.locator('button[type="submit"], input[type="submit"]').last,
    ]
    for button in candidates:
        try:
            if button.is_visible(timeout=1200) and button.is_enabled(timeout=1200):
                button.click(timeout=5000)
                return True
        except Exception:
            pass
    if field is not None:
        try:
            field.press("Enter")
            return True
        except Exception:
            pass
    return False


def random_onboarding_profile() -> tuple[str, str]:
    name = f"{secrets.choice(RANDOM_FIRST_NAMES)} {secrets.choice(RANDOM_LAST_NAMES)}"
    age = str(18 + secrets.randbelow(26))
    return name, age


def click_onboarding_submit(page) -> bool:
    label_re = re.compile(r"^(完成账户创建|完成账号创建|完成|继续|Continue|Create account)$", re.I)
    candidates = [
        page.get_by_role("button", name=label_re).last,
        page.locator(ONBOARDING_SUBMIT_SELECTORS).last,
        page.locator('button[type="submit"], input[type="submit"]').last,
    ]
    for button in candidates:
        try:
            if button.is_visible(timeout=1200) and button.is_enabled(timeout=1200):
                button.click(timeout=5000)
                return True
        except Exception:
            pass
    return False


def maybe_complete_chatgpt_onboarding(page, args=None) -> bool:
    name_input = maybe_visible(page, ONBOARDING_NAME_SELECTORS, timeout=700)
    age_input = maybe_visible(page, ONBOARDING_AGE_SELECTORS, timeout=700)
    if not name_input and not age_input:
        return False

    set_auth_stage(args, "fill_onboarding_profile")
    full_name, age = random_onboarding_profile()
    print(f"[gopay] 检测到新号资料页，填写随机姓名和年龄: {full_name} / {age}")

    if name_input and not fill_auth_field(name_input, full_name, label="全名"):
        time.sleep(1)
        return True
    if age_input and not fill_auth_field(age_input, age, label="年龄"):
        time.sleep(1)
        return True

    if click_onboarding_submit(page):
        set_auth_stage(args, "submit_onboarding_profile")
        print("[gopay] 已点击完成账户创建，继续等待进入 ChatGPT。")
    else:
        print("[gopay] 已填写随机资料，但未点到完成按钮，继续观察页面。")
    time.sleep(3)
    return True


def page_body_text(page, timeout: int = 1000) -> str:
    try:
        return page.locator("body").inner_text(timeout=timeout)
    except Exception:
        return ""


def click_text_button_or_link(page, pattern: str, *, label: str = "", timeout: int = 1200) -> bool:
    text_re = re.compile(pattern, re.I)
    candidates = [
        page.get_by_role("button", name=text_re).last,
        page.get_by_role("link", name=text_re).last,
        page.locator("button").filter(has_text=text_re).last,
        page.locator("a").filter(has_text=text_re).last,
    ]
    for item in candidates:
        try:
            if item.is_visible(timeout=timeout) and item.is_enabled(timeout=timeout):
                if label:
                    print(label)
                item.scroll_into_view_if_needed(timeout=1000)
                item.click(timeout=5000)
                time.sleep(2)
                return True
        except Exception:
            pass
    return False


def maybe_skip_usage_reason(page, args=None) -> bool:
    body = page_body_text(page)
    if not re.search(r"是什么促使你使用\s*ChatGPT|What brings you to ChatGPT", body, flags=re.I):
        return False
    set_auth_stage(args, "skip_usage_reason")
    if click_text_button_or_link(page, r"^(跳过|Skip)$", label="[gopay] 检测到用途选择页，点击跳过。"):
        return True
    print("[gopay] 检测到用途选择页，但未点到跳过按钮，继续观察页面。")
    time.sleep(1)
    return True


def maybe_continue_ready_page(page, args=None) -> bool:
    body = page_body_text(page)
    if not re.search(r"你已准备就绪|You're ready|You.?re ready", body, flags=re.I):
        return False
    set_auth_stage(args, "continue_ready_page")
    if click_text_button_or_link(page, r"^(继续|Continue)$", label="[gopay] 检测到准备就绪页，点击继续。"):
        return True
    print("[gopay] 检测到准备就绪页，但未点到继续按钮，继续观察页面。")
    time.sleep(1)
    return True


def maybe_start_tips_modal(page, args=None) -> bool:
    body = page_body_text(page)
    if not re.search(r"入门技巧|尽管问|请勿共享敏感信息|核实你的信息|Getting started|Ask anything", body, flags=re.I):
        return False
    set_auth_stage(args, "start_tips_modal")
    if click_text_button_or_link(page, r"^(好的，开始吧|好的.*开始|Let's go|Get started)$", label="[gopay] 检测到入门技巧弹窗，点击开始。"):
        return True
    print("[gopay] 检测到入门技巧弹窗，但未点到开始按钮，继续观察页面。")
    time.sleep(1)
    return True


def maybe_open_free_trial(page, args=None) -> bool:
    if not is_chatgpt_logged_in(page):
        return False
    button = maybe_visible(page, FREE_TRIAL_SELECTORS, timeout=1000)
    set_auth_stage(args, "open_free_trial")
    if button:
        try:
            print("[gopay] 检测到免费试用入口，点击进入套餐页。")
            button.scroll_into_view_if_needed(timeout=1000)
            button.click(timeout=5000)
            wait_or_open_pricing_page(page)
            return True
        except Exception as exc:
            print(f"[gopay] 免费试用入口点击未完成: {str(exc).splitlines()[0]}")
            try:
                button.click(timeout=2500, force=True)
                print("[gopay] 已强制点击免费试用入口，等待套餐页。")
                wait_or_open_pricing_page(page)
                return True
            except Exception:
                pass
    try:
        clicked = page.evaluate(
            """() => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const text = (el) => (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
                const target = Array.from(document.querySelectorAll('button,a,[role=button],div,span'))
                    .filter(visible)
                    .sort((a, b) => {
                        const ar = a.getBoundingClientRect();
                        const br = b.getBoundingClientRect();
                        return ar.top - br.top || ar.left - br.left;
                    })
                    .find((el) => /免费试用|Free trial|Try Plus/i.test(text(el)));
                if (!target) return false;
                const clickTarget = target.closest('button,a,[role=button]') || target;
                clickTarget.scrollIntoView({ block: 'center', inline: 'nearest' });
                const rect = clickTarget.getBoundingClientRect();
                const opts = { bubbles: true, cancelable: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 };
                clickTarget.dispatchEvent(new PointerEvent('pointerdown', opts));
                clickTarget.dispatchEvent(new MouseEvent('mousedown', opts));
                clickTarget.dispatchEvent(new PointerEvent('pointerup', opts));
                clickTarget.dispatchEvent(new MouseEvent('mouseup', opts));
                clickTarget.dispatchEvent(new MouseEvent('click', opts));
                clickTarget.click();
                return true;
            }"""
        )
        if clicked:
            print("[gopay] DOM 兜底点击免费试用入口，等待套餐页。")
            wait_or_open_pricing_page(page)
            return True
    except Exception:
        pass
    return False


def wait_or_open_pricing_page(page, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_subscription_page(page):
            return
        time.sleep(0.5)
    try:
        print("[gopay] 免费试用点击后未进入套餐页，改为直接打开 #pricing。")
        page.goto(f"{CHATGPT_HOME_URL}#pricing", wait_until="domcontentloaded", timeout=30000)
        time.sleep(4)
    except Exception as exc:
        print(f"[gopay] 打开 #pricing 失败，继续观察: {str(exc).splitlines()[0]}")


def is_subscription_page(page) -> bool:
    body = page_body_text(page).lower()
    lower_url = (page.url or "").lower()
    return (
        "pricing" in lower_url
        or "subscribe" in lower_url
        or ("plus" in body and ("pro" in body or "go" in body) and ("free trial" in body or "免费试用" in body or "领取免费试用" in body))
    )


def maybe_select_subscription_country(page, args=None, country: str = "印度尼西亚") -> bool:
    if not is_subscription_page(page):
        return False
    body = page_body_text(page)
    if country in body or "Indonesia" in body or "IDR" in body:
        return False

    dropdown = find_country_dropdown(page)
    if not dropdown:
        return False

    set_auth_stage(args, "select_subscription_country")
    print(f"[gopay] 套餐页选择国家/地区: {country}")
    debug_subscription_country_controls(page)
    opened = open_country_dropdown(page, dropdown)
    if not opened:
        print("[gopay] 国家下拉打开失败，继续观察页面。")
    else:
        time.sleep(0.5)

    option_patterns = [
        r"^(印度尼西亚|Indonesia)$",
        r"印度尼西亚",
        r"Indonesia",
    ]
    if click_country_option_with_scroll(page, option_patterns):
        time.sleep(3)
        print("[gopay] 已切换到印度尼西亚，等待价格刷新。")
        return True
    if search_and_click_subscription_country(page, ["Indonesia", "印度尼西亚"]):
        time.sleep(3)
        print("[gopay] 已通过搜索切换到印度尼西亚，等待价格刷新。")
        return True
    print("[gopay] 未找到印度尼西亚选项，继续观察页面。")
    time.sleep(1)
    return True


def debug_subscription_country_controls(page) -> None:
    try:
        result = page.evaluate(
            """() => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const text = (el) => (el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ');
                return Array.from(document.querySelectorAll('button, [role="button"], [role="combobox"], [aria-haspopup], select'))
                    .filter(visible)
                    .map((el) => {
                        const rect = el.getBoundingClientRect();
                        return {
                            tag: el.tagName,
                            role: el.getAttribute('role') || '',
                            aria: el.getAttribute('aria-label') || '',
                            expanded: el.getAttribute('aria-expanded') || '',
                            text: text(el).slice(0, 80),
                            top: Math.round(rect.top),
                            left: Math.round(rect.left),
                            width: Math.round(rect.width),
                            height: Math.round(rect.height),
                        };
                    })
                    .filter((item) => /美国|United States|US|国家|地区|Country|Region|免费|Plus|Go|Pro|订阅|Subscribe|Ind/i.test(`${item.text} ${item.aria} ${item.role}`) || item.top > window.innerHeight * 0.55)
                    .sort((a, b) => b.top - a.top)
                    .slice(0, 12);
            }"""
        )
        if isinstance(result, list):
            compact = "; ".join(
                f"{item.get('tag')} role={item.get('role')} exp={item.get('expanded')} pos={item.get('left')},{item.get('top')} size={item.get('width')}x{item.get('height')} text={item.get('text') or item.get('aria')}"
                for item in result[:8]
            )
            if compact:
                print(f"[debug] 套餐页国家控件候选: {compact}")
    except Exception:
        pass


def find_country_dropdown(page):
    try:
        page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        time.sleep(0.6)
    except Exception:
        pass

    dropdown = maybe_visible(page, COUNTRY_DROPDOWN_SELECTORS, timeout=1000)
    if dropdown:
        return dropdown

    try:
        handle = page.evaluate_handle(
            """() => {
                const countryRe = /(国家|地区|Country|Region|美国|United States|USA|US|印度尼西亚|Indonesia|印度|India|英国|United Kingdom|越南|Vietnam|泽西岛|Jersey)/i;
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const meta = (node) => [
                    node.innerText,
                    node.textContent,
                    node.getAttribute('aria-label'),
                    node.getAttribute('data-testid'),
                    node.id,
                    node.className,
                ].join(' ');
                const nodes = Array.from(document.querySelectorAll('button, [role="button"], [role="combobox"], [aria-haspopup], [data-testid]'))
                    .filter((node) => visible(node) && countryRe.test(meta(node)));
                nodes.sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom);
                return nodes[0] || null;
            }"""
        )
        return handle.as_element()
    except Exception:
        return None


def open_country_dropdown(page, dropdown) -> bool:
    role_candidates = [
        lambda: page.get_by_role("combobox", name=re.compile(r"美国|United States|US|USA|国家|地区|Country|Region", re.I)).last,
        lambda: page.locator('button[role="combobox"]').filter(has_text=re.compile(r"美国|United States|US|USA", re.I)).last,
        lambda: page.locator('[role="combobox"]').filter(has_text=re.compile(r"美国|United States|US|USA", re.I)).last,
    ]
    for make_locator in role_candidates:
        try:
            locator = make_locator()
            if locator.is_visible(timeout=1000):
                locator.scroll_into_view_if_needed(timeout=1500)
                time.sleep(0.2)
                locator.click(timeout=2500, force=True, position={"x": 55, "y": 18})
                time.sleep(0.8)
                if country_popup_opened(page) or country_option_visible(page):
                    return True
                locator.press("Enter", timeout=1500)
                time.sleep(0.6)
                if country_popup_opened(page) or country_option_visible(page):
                    return True
                locator.press("Space", timeout=1500)
                time.sleep(0.6)
                if country_popup_opened(page) or country_option_visible(page):
                    return True
                print("[debug] Playwright role combobox 点击后仍未展开国家列表。")
                return True
        except Exception as exc:
            print(f"[gopay] role combobox 打开失败: {str(exc).splitlines()[0]}")

    try:
        dropdown.scroll_into_view_if_needed(timeout=1000)
        time.sleep(0.2)
    except Exception:
        pass

    click_attempts = [
        lambda: dropdown.click(timeout=2500),
        lambda: dropdown.click(timeout=2500, force=True),
        lambda: dropdown.press("Enter", timeout=1500),
        lambda: dropdown.press("Space", timeout=1500),
        lambda: dropdown.evaluate("(el) => el.click()"),
        lambda: dropdown.evaluate(
            """(el) => {
                const rect = el.getBoundingClientRect();
                const opts = { bubbles: true, cancelable: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 };
                el.dispatchEvent(new PointerEvent('pointerdown', opts));
                el.dispatchEvent(new MouseEvent('mousedown', opts));
                el.dispatchEvent(new PointerEvent('pointerup', opts));
                el.dispatchEvent(new MouseEvent('mouseup', opts));
                el.dispatchEvent(new MouseEvent('click', opts));
            }"""
        ),
    ]
    for attempt in click_attempts:
        try:
            attempt()
            time.sleep(0.8)
            if country_popup_opened(page) or country_option_visible(page):
                return True
        except Exception as exc:
            print(f"[gopay] 国家下拉点击兜底失败: {str(exc).splitlines()[0]}")
    try:
        box = page.evaluate(
            """() => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const text = (el) => (el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '').trim().replace(/\\s+/g, ' ');
                const target = Array.from(document.querySelectorAll('button[role="combobox"], [role="combobox"], button[aria-haspopup]'))
                    .filter(visible)
                    .filter((el) => /^(美国|United States|US|USA)$/.test(text(el)) || /country|region|国家|地区/i.test([text(el), el.getAttribute('aria-label'), el.id, el.className].join(' ')))
                    .sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom)[0];
                if (!target) return null;
                target.scrollIntoView({ block: 'center', inline: 'nearest' });
                const rect = target.getBoundingClientRect();
                return { x: Math.round(rect.left + rect.width * 0.78), y: Math.round(rect.top + rect.height / 2), text: text(target) };
            }"""
        )
        if isinstance(box, dict) and box.get("x") is not None:
            page.mouse.move(box["x"], box["y"])
            page.mouse.down()
            time.sleep(0.08)
            page.mouse.up()
            time.sleep(0.8)
            if country_popup_opened(page) or country_option_visible(page):
                return True
            print(f"[debug] 已对国家 combobox 坐标点击: {box.get('text')} @ {box.get('x')},{box.get('y')}")
            return True
    except Exception as exc:
        print(f"[gopay] 国家 combobox 坐标点击失败: {str(exc).splitlines()[0]}")
    try:
        result = page.evaluate(
            """() => {
                const countryRe = /(国家|地区|Country|Region|美国|United States|USA|US|印度尼西亚|Indonesia|印度|India|英国|United Kingdom|越南|Vietnam|泽西岛|Jersey)/i;
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const meta = (node) => [
                    node.innerText,
                    node.textContent,
                    node.getAttribute('aria-label'),
                    node.getAttribute('data-testid'),
                    node.id,
                    node.className,
                ].join(' ');
                const candidates = Array.from(document.querySelectorAll('button, [role="button"], [role="combobox"], [aria-haspopup], [data-testid]'))
                    .filter((node) => visible(node) && countryRe.test(meta(node)))
                    .filter((node) => {
                        const rect = node.getBoundingClientRect();
                        return rect.top > window.innerHeight * 0.35 || /国家|地区|Country|Region|United States|美国/i.test(meta(node));
                    })
                    .sort((a, b) => {
                        const ar = a.getBoundingClientRect();
                        const br = b.getBoundingClientRect();
                        return br.bottom - ar.bottom || ar.left - br.left;
                    });
                const target = candidates[0];
                if (!target) return { ok: false, reason: 'not-found' };
                target.scrollIntoView({ block: 'center', inline: 'nearest' });
                const rect = target.getBoundingClientRect();
                const x = rect.left + rect.width / 2;
                const y = rect.top + rect.height / 2;
                const opts = { bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true, clientX: x, clientY: y };
                for (const type of ['pointerover', 'mouseover', 'pointermove', 'mousemove', 'pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                    const EventCtor = type.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
                    target.dispatchEvent(new EventCtor(type, opts));
                }
                target.click();
                return { ok: true, text: (target.innerText || target.textContent || target.getAttribute('aria-label') || '').trim() };
            }"""
        )
        if isinstance(result, dict) and result.get("ok"):
            time.sleep(0.8)
            if country_popup_opened(page) or country_option_visible(page):
                return True
            # 有些 Radix/虚拟列表不容易从 DOM 判断弹层状态；点击成功后让后续选项搜索继续尝试。
            if result.get("text"):
                return True
    except Exception as exc:
        print(f"[gopay] 国家下拉 DOM 坐标点击失败: {str(exc).splitlines()[0]}")
    try:
        clicked = page.evaluate(
            """() => {
                const y = Math.max(80, window.innerHeight - 56);
                const points = [
                    [window.innerWidth - 90, y],
                    [window.innerWidth - 150, y],
                    [window.innerWidth / 2, y],
                ];
                for (const [x, yy] of points) {
                    const el = document.elementFromPoint(x, yy);
                    if (!el) continue;
                    const target = el.closest('button, [role="button"], [role="combobox"], [aria-haspopup]') || el;
                    const rect = target.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) continue;
                    const cx = Math.min(Math.max(x, rect.left + 4), rect.right - 4);
                    const cy = Math.min(Math.max(yy, rect.top + 4), rect.bottom - 4);
                    const opts = { bubbles: true, cancelable: true, pointerId: 1, pointerType: 'mouse', isPrimary: true, clientX: cx, clientY: cy };
                    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
                        const EventCtor = type.startsWith('pointer') && window.PointerEvent ? PointerEvent : MouseEvent;
                        target.dispatchEvent(new EventCtor(type, opts));
                    }
                    target.click?.();
                    return true;
                }
                return false;
            }"""
        )
        if clicked:
            time.sleep(0.8)
            return True
    except Exception:
        pass
    try:
        page.keyboard.press("Enter")
        time.sleep(0.6)
        if country_popup_opened(page) or country_option_visible(page):
            return True
    except Exception:
        pass
    return country_popup_opened(page) or country_option_visible(page)


def country_option_visible(page) -> bool:
    try:
        return bool(page.evaluate(
            """() => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                };
                return Array.from(document.querySelectorAll('[role="option"], [role="menuitem"], [cmdk-item], [data-radix-collection-item], button, div, span'))
                    .some((el) => visible(el) && /^(印度尼西亚|Indonesia)$/.test((el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ')));
            }"""
        ))
    except Exception:
        return False


def country_popup_opened(page) -> bool:
    try:
        result = page.evaluate(
            """() => {
                const countryRe = /(印度尼西亚|Indonesia|美国|United States|印度|India|英国|United Kingdom|越南|Vietnam|泽西岛|Jersey)/i;
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const nodes = Array.from(document.querySelectorAll('[role="listbox"], [role="menu"], [role="dialog"], [role="presentation"], [cmdk-list], [data-radix-popper-content-wrapper], [data-radix-portal], div'));
                return nodes.some((el) => {
                    const text = el.innerText || el.textContent || '';
                    if (!visible(el) || !countryRe.test(text)) return false;
                    return el.scrollHeight > el.clientHeight + 20 || /\n/.test(text) || text.length > 80;
                });
            }"""
        )
        return bool(result)
    except Exception:
        return False


def click_country_option_with_scroll(page, option_patterns: list[str]) -> bool:
    targets = ["印度尼西亚", "Indonesia"]
    for attempt in range(1, 16):
        result = page.evaluate(
            """async ({ targets, attempt }) => {
                const nextFrame = () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
                const isVisible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.visibility !== 'hidden' && style.display !== 'none';
                };
                const itemText = (el) => (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
                const isTarget = (el) => targets.some((target) => itemText(el) === target);
                const clickTarget = () => {
                    const visibleItems = Array.from(document.querySelectorAll('[role="option"], [role="menuitem"], [cmdk-item], [data-radix-collection-item], button, div, span'))
                        .filter((node) => isVisible(node) && isTarget(node))
                        .sort((a, b) => {
                            const ar = a.getBoundingClientRect();
                            const br = b.getBoundingClientRect();
                            const aScore = /^(BUTTON|LI)$/i.test(a.tagName) || a.getAttribute('role') ? 0 : 1;
                            const bScore = /^(BUTTON|LI)$/i.test(b.tagName) || b.getAttribute('role') ? 0 : 1;
                            return aScore - bScore || ar.top - br.top || ar.left - br.left;
                        });
                    const found = visibleItems[0];
                    if (!found) return null;
                    const clickable = found.closest('[role="option"], [role="menuitem"], [cmdk-item], [data-radix-collection-item], button, [role="button"]') || found;
                    clickable.scrollIntoView({ block: 'center', inline: 'nearest' });
                    const rect = clickable.getBoundingClientRect();
                    const opts = { bubbles: true, cancelable: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 };
                    clickable.dispatchEvent(new PointerEvent('pointerdown', opts));
                    clickable.dispatchEvent(new MouseEvent('mousedown', opts));
                    clickable.dispatchEvent(new PointerEvent('pointerup', opts));
                    clickable.dispatchEvent(new MouseEvent('mouseup', opts));
                    clickable.dispatchEvent(new MouseEvent('click', opts));
                    clickable.click();
                    return itemText(found);
                };

                const clickedNow = clickTarget();
                if (clickedNow) return { ok: true, selected: clickedNow, phase: 'already-visible' };

                const candidates = Array.from(document.querySelectorAll('*'));
                const scrollables = candidates
                    .filter((el) => isVisible(el) && el.scrollHeight > el.clientHeight + 20)
                    .filter((el) => {
                        const text = (el.innerText || el.textContent || '').trim();
                        return (
                            text.includes('美国')
                            || text.includes('United States')
                            || text.includes('阿尔巴尼亚')
                            || text.includes('百慕大')
                            || text.includes('印度')
                            || text.includes('Indonesia')
                        );
                    });
                if (!scrollables.length) return { ok: false, reason: 'no-scroll-container' };

                if (attempt === 1) {
                    for (const el of scrollables) {
                        el.scrollTop = el.scrollHeight;
                        el.dispatchEvent(new Event('scroll', { bubbles: true }));
                    }
                } else {
                    for (const el of scrollables) {
                        const step = Math.max(160, el.clientHeight * 0.45);
                        el.scrollTop = attempt % 2 === 0
                            ? Math.max(0, el.scrollTop - step)
                            : Math.min(el.scrollHeight, el.scrollTop + step);
                        el.dispatchEvent(new Event('scroll', { bubbles: true }));
                    }
                }
                await nextFrame();

                const clicked = clickTarget();
                if (clicked) return { ok: true, selected: clicked, phase: attempt === 1 ? 'bottom' : 'upward' };
                return {
                    ok: false,
                    reason: 'not-found',
                    tops: scrollables.slice(0, 5).map((el) => ({ top: el.scrollTop, height: el.scrollHeight, client: el.clientHeight, cls: el.className, role: el.getAttribute('role') })),
                };
            }""",
            {"targets": targets, "attempt": attempt},
        )
        if isinstance(result, dict) and result.get("ok"):
            print(f"[gopay] 国家列表已点击: {result.get('selected', '印度尼西亚')}")
            return True
        time.sleep(0.3)
    return False


def search_and_click_subscription_country(page, targets: list[str]) -> bool:
    try:
        result = page.evaluate(
            """async ({ targets }) => {
                const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const text = (el) => (el.innerText || el.textContent || el.value || '').trim().replace(/\\s+/g, ' ');
                const setValue = (el, value) => {
                    el.focus();
                    el.click();
                    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
                    if (setter) setter.call(el, value);
                    else el.value = value;
                    el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: value }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                };
                const clickTarget = () => {
                    const nodes = Array.from(document.querySelectorAll('[role="option"], [role="menuitem"], [cmdk-item], [data-radix-collection-item], button, div, span'))
                        .filter((node) => visible(node) && targets.some((target) => text(node) === target))
                        .sort((a, b) => {
                            const ar = a.getBoundingClientRect();
                            const br = b.getBoundingClientRect();
                            return ar.top - br.top || ar.left - br.left;
                        });
                    const found = nodes[0];
                    if (!found) return '';
                    const target = found.closest('[role="option"], [role="menuitem"], [cmdk-item], [data-radix-collection-item], button, [role="button"]') || found;
                    target.scrollIntoView({ block: 'center', inline: 'nearest' });
                    const rect = target.getBoundingClientRect();
                    const opts = { bubbles: true, cancelable: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 };
                    target.dispatchEvent(new PointerEvent('pointerdown', opts));
                    target.dispatchEvent(new MouseEvent('mousedown', opts));
                    target.dispatchEvent(new PointerEvent('pointerup', opts));
                    target.dispatchEvent(new MouseEvent('mouseup', opts));
                    target.dispatchEvent(new MouseEvent('click', opts));
                    target.click();
                    return text(found);
                };
                const searchInputs = Array.from(document.querySelectorAll('input[type="search"], input[role="combobox"], input[placeholder], input'))
                    .filter(visible)
                    .filter((el) => {
                        const meta = [el.placeholder, el.getAttribute('aria-label'), el.getAttribute('role'), el.id, el.className].join(' ');
                        return /search|country|region|国家|地区|搜索|combobox/i.test(meta) || el === document.activeElement;
                    });
                for (const input of searchInputs) {
                    setValue(input, 'Indonesia');
                    await sleep(500);
                    const clicked = clickTarget();
                    if (clicked) return { ok: true, selected: clicked, phase: 'input-search' };
                }
                document.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: 'I' }));
                await sleep(80);
                for (const ch of 'Indonesia') {
                    document.activeElement?.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: ch }));
                    document.activeElement?.dispatchEvent(new KeyboardEvent('keypress', { bubbles: true, key: ch }));
                    document.activeElement?.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ch }));
                }
                await sleep(700);
                const clicked = clickTarget();
                if (clicked) return { ok: true, selected: clicked, phase: 'keyboard-search' };
                return {
                    ok: false,
                    active: document.activeElement?.tagName || '',
                    visibleText: (document.body?.innerText || '').slice(-1200),
                };
            }""",
            {"targets": targets},
        )
        if isinstance(result, dict) and result.get("ok"):
            print(f"[gopay] 国家列表搜索后已点击: {result.get('selected', 'Indonesia')}")
            return True
    except Exception:
        pass
    return False


def maybe_click_plus_free_trial(page, args=None) -> bool:
    if not is_subscription_page(page):
        return False
    body = page_body_text(page)
    if "IDR" not in body and "印度尼西亚" not in body and "Indonesia" not in body:
        if maybe_select_subscription_country(page, args, "印度尼西亚"):
            return True
        print("[gopay] 套餐页还未确认切到印度尼西亚，暂不点击 Plus 免费试用。")
        return False

    set_auth_stage(args, "click_plus_free_trial")
    plus_heading = None
    try:
        plus_heading = page.get_by_text(re.compile(r"^Plus$", re.I)).last
        plus_heading.wait_for(state="visible", timeout=1000)
    except Exception:
        plus_heading = None

    if plus_heading is not None:
        try:
            card = plus_heading.locator("xpath=ancestor::*[self::section or self::article or self::div][.//button or .//a][1]").first
            button = card.locator(PLUS_FREE_TRIAL_SELECTORS).first
            if button.is_visible(timeout=1200) and button.is_enabled(timeout=1200):
                print("[gopay] 点击 Plus 领取免费试用。")
                button.scroll_into_view_if_needed(timeout=1000)
                button.click(timeout=5000)
                time.sleep(5)
                return True
        except Exception:
            pass

    button = maybe_visible(page, PLUS_FREE_TRIAL_SELECTORS, timeout=1200)
    if button:
        try:
            print("[gopay] 点击领取免费试用。")
            button.scroll_into_view_if_needed(timeout=1000)
            button.click(timeout=5000)
            time.sleep(5)
            return True
        except Exception as exc:
            print(f"[gopay] 领取免费试用点击未完成: {str(exc).splitlines()[0]}")
            return True
    return False


def is_payment_or_checkout_page(page) -> bool:
    lower_url = (page.url or "").lower()
    body = page_body_text(page, timeout=1200).lower()
    url_hints = ["checkout", "payment", "billing", "pay", "stripe", "gopay", "invoice"]
    body_hints = [
        "gopay",
        "payment",
        "付款",
        "支付",
        "结账",
        "checkout",
        "billing",
        "账单",
        "card number",
        "银行卡",
        "credit card",
        "add payment",
        "添加付款",
    ]
    return any(hint in lower_url for hint in url_hints) and any(hint in body for hint in body_hints)


def normalize_singapore_address_payload(record: dict) -> dict:
    full_name = str(record.get("full_name") or record.get("Full_Name") or record.get("name") or "").strip()
    address = str(record.get("address") or record.get("Address") or "").strip()
    postal_code = str(record.get("postal_code") or record.get("Zip_Code") or record.get("zip") or "").strip()
    if not full_name or not address or not postal_code:
        raise RuntimeError("新加坡地址缺少 full_name/address/postal_code")
    return {
        "full_name": full_name,
        "address": address,
        "postal_code": postal_code,
        "country": "新加坡",
        "country_code": "SG",
    }


def normalize_us_address_payload(record: dict) -> dict:
    full_name = str(record.get("full_name") or record.get("Full_Name") or record.get("name") or "").strip()
    address = str(record.get("address") or record.get("Address") or "").strip()
    postal_code = str(record.get("postal_code") or record.get("Zip_Code") or record.get("zip") or "").strip()
    city = str(record.get("city") or record.get("City") or "").strip()
    state = str(record.get("state") or record.get("State") or "").strip()
    state_full = str(record.get("state_full") or record.get("State_Full") or "").strip()
    if not full_name or not address or not postal_code:
        raise RuntimeError("美国地址缺少 full_name/address/postal_code")
    return {
        "full_name": full_name,
        "address": address,
        "postal_code": postal_code,
        "country": "美国",
        "country_code": "US",
        "city": city,
        "state": state,
        "state_full": state_full,
    }


def fetch_random_singapore_address() -> dict:
    print("[gopay] 正在后台获取新加坡账单地址。")
    try:
        import requests
    except Exception as exc:
        raise RuntimeError(f"requests 未安装或导入失败: {exc}") from exc

    last_error = ""
    for method in ("address", "refresh"):
        try:
            response = requests.post(
                SINGAPORE_ADDRESS_API_URL,
                json={"path": "/sg-address", "method": method},
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Origin": "https://www.meiguodizhi.com",
                    "Referer": "https://www.meiguodizhi.com/sg-address",
                },
                timeout=20,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("status") != "ok" or not isinstance(data.get("address"), dict):
                last_error = json.dumps(data, ensure_ascii=False)[:300]
                continue
            payload = normalize_singapore_address_payload(data["address"])
            print(f"[gopay] 已获取账单地址: {payload['full_name']} | {payload['address']} | {payload['postal_code']}")
            return payload
        except Exception as exc:
            last_error = str(exc).splitlines()[0]
    raise RuntimeError(f"后台获取新加坡地址失败: {last_error}")


def fetch_random_us_address() -> dict:
    print("[gopay] 正在后台获取美国账单地址。")
    try:
        import requests
    except Exception as exc:
        raise RuntimeError(f"requests 未安装或导入失败: {exc}") from exc

    last_error = ""
    for method in ("address", "refresh"):
        try:
            response = requests.post(
                SINGAPORE_ADDRESS_API_URL,
                json={"path": "/usa-address", "method": method},
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Origin": "https://www.meiguodizhi.com",
                    "Referer": "https://www.meiguodizhi.com/usa-address",
                },
                timeout=20,
            )
            response.raise_for_status()
            data = response.json()
            if data.get("status") != "ok" or not isinstance(data.get("address"), dict):
                last_error = json.dumps(data, ensure_ascii=False)[:300]
                continue
            payload = normalize_us_address_payload(data["address"])
            print(f"[gopay] 已获取美国账单地址: {payload['full_name']} | {payload['address']} | {payload['city']} {payload['state']} {payload['postal_code']}")
            return payload
        except Exception as exc:
            last_error = str(exc).splitlines()[0]
    raise RuntimeError(f"后台获取美国地址失败: {last_error}")


def normalize_gopay_billing_country(country: str) -> str:
    raw = str(country or "").strip().lower()
    if raw in {"us", "usa", "united_states", "united-states", "america", "美国"}:
        return "us"
    return "sg"


def fetch_random_gopay_billing_address(country: str = "sg") -> dict:
    return fetch_random_us_address() if normalize_gopay_billing_country(country) == "us" else fetch_random_singapore_address()


def fetch_random_singapore_address_from_context(context) -> dict:
    return fetch_random_singapore_address()


def generate_plus_hosted_checkout_url(page, *, billing_country: str = "id") -> str:
    country = str(billing_country or "id").strip().upper()
    currency = "IDR" if country == "ID" else "USD"
    result = page.evaluate(
        """async ({ country, currency }) => {
            const session = await fetch('/api/auth/session').then((r) => r.json()).catch(() => ({}));
            const accessToken = session?.accessToken;
            if (!accessToken) return { ok: false, error: 'accessToken 为空，请确认 ChatGPT 已登录' };
            const payload = {
                plan_name: 'chatgptplusplan',
                billing_details: { country, currency },
                cancel_url: 'https://chatgpt.com/#pricing',
                promo_campaign: {
                    promo_campaign_id: 'plus-1-month-free',
                    is_coupon_from_query_param: false,
                },
                checkout_ui_mode: 'hosted',
            };
            const response = await fetch('/backend-api/payments/checkout', {
                method: 'POST',
                headers: {
                    Authorization: `Bearer ${accessToken}`,
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(payload),
            });
            const data = await response.json().catch(() => ({}));
            const url = data?.url || data?.stripe_hosted_url || data?.checkout_url || '';
            if (!response.ok || !url) {
                return { ok: false, status: response.status, error: data?.detail || data?.error || data?.message || '未返回支付链接' };
            }
            return { ok: true, url };
        }""",
        {"country": country, "currency": currency},
    )
    if not isinstance(result, dict) or not result.get("ok") or not result.get("url"):
        raise RuntimeError(f"Plus Hosted 长链接生成失败: {(result or {}).get('error') or result}")
    url = str(result["url"])
    print(f"[gopay] 已生成 Plus Hosted 长链接: {url[:90]}{'...' if len(url) > 90 else ''}")
    return url


def subscription_country_ready_for_hosted(page, country: str = "印度尼西亚") -> bool:
    if not is_subscription_page(page):
        return False
    body = page_body_text(page, timeout=1000)
    return bool(re.search(r"IDR|Indonesia|印度尼西亚", body, flags=re.I))


def fill_hosted_checkout_billing(page, address: dict) -> bool:
    filled = False
    deadline = time.time() + 30
    while time.time() < deadline:
        for frame in checkout_frames(page):
            try:
                result = frame.evaluate(
                    """async ({ address }) => {
                        const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
                        const visible = (el) => {
                            if (!el) return false;
                            const rect = el.getBoundingClientRect();
                            const style = getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                        };
                        const text = (el) => (el?.innerText || el?.textContent || el?.value || '').trim().replace(/\\s+/g, ' ');
                        const cssEscape = (value) => {
                            if (window.CSS?.escape) return CSS.escape(value);
                            return String(value).replace(/["\\\\]/g, '\\\\$&');
                        };
                        const meta = (el) => [
                            el.name,
                            el.id,
                            el.autocomplete,
                            el.placeholder,
                            el.getAttribute('aria-label'),
                            el.closest('label')?.innerText,
                            el.id ? document.querySelector(`label[for="${cssEscape(el.id)}"]`)?.innerText : '',
                        ].join(' ');
                        const inputKind = (el) => {
                            const raw = meta(el);
                            const m = raw.toLowerCase();
                            if (/card|cvc|cvv|expiry|expire|month|year|phone|email|coupon|promo|search|文件|file/.test(m)) return '';
                            if (/postal|postcode|zip|postal-code|邮编|邮政编码/.test(m)) return 'postal';
                            if (/city|locality|address-level2|城市|市区/.test(m)) return 'city';
                            if (/state|province|region|address-level1|州|省/.test(m)) return 'state';
                            if (/address.*line.*2|address-line2|line2|apt|suite|address2|地址.*2/.test(m)) return 'address2';
                            if (/address.*line.*1|address-line1|line1|street-address|street|address|地址/.test(m)) return 'address';
                            if (/full.*name|billing.*name|cardholder.*name|customer.*name|^\\s*name\\s*$|全名|姓名/.test(raw)) return 'name';
                            return '';
                        };
                        const setValue = async (el, value) => {
                            if (!el || value == null) return false;
                            const type = String(el.type || 'text').toLowerCase();
                            if (['hidden', 'checkbox', 'radio', 'submit', 'button', 'file'].includes(type)) return false;
                            el.scrollIntoView({ block: 'center', inline: 'nearest' });
                            el.focus();
                            el.click();
                            const str = String(value);
                            const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                            if (setter) setter.call(el, '');
                            else el.value = '';
                            el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward', data: null }));
                            await sleep(30);
                            if (setter) setter.call(el, str);
                            else el.value = str;
                            el.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, cancelable: true, inputType: 'insertText', data: str }));
                            el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: str }));
                            el.dispatchEvent(new Event('change', { bubbles: true }));
                            el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: str.slice(-1) || 'a' }));
                            return true;
                        };
                        const currentInputs = () => Array.from(document.querySelectorAll('input, textarea')).filter(visible).filter((el) => {
                            const type = String(el.type || 'text').toLowerCase();
                            return !['hidden', 'checkbox', 'radio', 'submit', 'button', 'file'].includes(type);
                        });
                        const findInput = (kind) => currentInputs().find((el) => inputKind(el) === kind);
                        const changed = [];
                        const countryTargets = String(address.country_code || address.country || '').toUpperCase() === 'US'
                            ? [/United States/i, /美国/i, /^US$/i]
                            : [/Singapore/i, /新加坡/i, /^SG$/i];
                        for (const select of Array.from(document.querySelectorAll('select')).filter(visible)) {
                            if (!/country|国家|地区/i.test(meta(select))) continue;
                            const option = Array.from(select.options).find((item) => countryTargets.some((re) => re.test(item.textContent || item.value || '')));
                            if (option && select.value !== option.value) {
                                select.value = option.value;
                                select.dispatchEvent(new Event('input', { bubbles: true }));
                                select.dispatchEvent(new Event('change', { bubbles: true }));
                                changed.push('country');
                                await sleep(500);
                            }
                        }
                        const manual = Array.from(document.querySelectorAll('button,a,[role=button],div,span'))
                            .filter(visible)
                            .find((el) => /手动输入地址|Enter address manually|Enter manually|Manual address/i.test(text(el)));
                        if (manual) {
                            manual.click();
                            changed.push('manual');
                            await sleep(600);
                        }
                        const name = findInput('name');
                        if (name && await setValue(name, address.full_name)) {
                            changed.push('name');
                        }
                        const line1 = findInput('address');
                        if (line1) {
                            line1.focus();
                            line1.click();
                            const proto = line1.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                            const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                            if (!line1.value) {
                                if (setter) setter.call(line1, ' ');
                                else line1.value = ' ';
                                line1.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: ' ' }));
                                await sleep(250);
                            }
                            if (await setValue(line1, address.address)) {
                                changed.push('address');
                                await sleep(900);
                            }
                        }
                        const city = findInput('city');
                        if (city && address.city && await setValue(city, address.city)) changed.push('city');
                        const stateInput = findInput('state');
                        if (stateInput) {
                            const value = address.state || address.state_full || '';
                            if (value && await setValue(stateInput, value)) changed.push('state');
                        }
                        const postal = findInput('postal');
                        if (postal && address.postal_code && await setValue(postal, address.postal_code)) changed.push('postal');
                        for (const select of Array.from(document.querySelectorAll('select')).filter(visible)) {
                            if (!/state|province|州|省/i.test(meta(select))) continue;
                            const targets = [address.state, address.state_full].filter(Boolean);
                            const option = Array.from(select.options).find((item) => targets.some((target) => new RegExp(`(^|\\\\b)${String(target).replace(/[.*+?^${}()|[\\]\\\\]/g, '\\\\$&')}(\\\\b|$)`, 'i').test(`${item.textContent || ''} ${item.value || ''}`)));
                            if (option) {
                                select.value = option.value;
                                select.dispatchEvent(new Event('input', { bubbles: true }));
                                select.dispatchEvent(new Event('change', { bubbles: true }));
                                changed.push('state');
                            }
                        }
                        return Array.from(new Set(changed));
                    }""",
                    {"address": address},
                )
                if isinstance(result, list) and {"name", "address", "postal"}.issubset(set(str(item) for item in result)):
                    print("[gopay] Hosted 页账单姓名、地址、邮编已填写。")
                    return True
                if result:
                    filled = True
            except Exception:
                pass
        time.sleep(1)
    if filled:
        print("[gopay] Hosted 页已填写部分账单字段，请人工检查缺失项。")
    else:
        print("[gopay] Hosted 页未识别到账单输入框，请人工填写。")
    return filled


def maybe_prepare_gopay_billing(page, args=None) -> bool:
    if not is_payment_or_checkout_page(page):
        return False
    set_auth_stage(args, "prepare_gopay_billing")
    address = getattr(args, "gopay_billing_address", None)
    if not isinstance(address, dict):
        address = fetch_random_gopay_billing_address(getattr(args, "gopay_billing_country", "sg"))
        setattr(args, "gopay_billing_address", address)

    if not checkout_gopay_is_selected(page):
        maybe_select_gopay_payment_method(page)
        time.sleep(2)

    country_name = address.get("country") or "新加坡"
    country_ok = maybe_select_checkout_country(page, country_name)
    filled = fill_checkout_billing_address(page, address)
    prepared = filled and (country_ok or checkout_country_is_selected(page, country_name))
    setattr(args, "task_gopay_billing_prepared", prepared)
    if prepared:
        print(f"[gopay] GoPay 和{country_name}账单地址已填写，等待人工确认后 next。")
    else:
        print(f"[gopay] 已获取{country_name}账单地址，正在等待/填写 GoPay 账单字段。")
    return prepared


def maybe_select_gopay_payment_method(page) -> bool:
    for frame in checkout_frames(page):
        try:
            clicked = frame.evaluate(
                """() => {
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const label = Array.from(document.querySelectorAll('button, [role="button"], div, label, span'))
                        .find((el) => visible(el) && (el.innerText || el.textContent || '').trim() === 'GoPay');
                    if (!label) return false;
                    const candidates = [];
                    let node = label;
                    for (let i = 0; node && i < 7; i += 1, node = node.parentElement) {
                        candidates.push(node);
                    }
                    const target = candidates.find((el) => {
                        const rect = el.getBoundingClientRect();
                        const text = (el.innerText || el.textContent || '').trim();
                        return visible(el) && text.includes('GoPay') && rect.width >= 120 && rect.height >= 48;
                    }) || label;
                    target.scrollIntoView({ block: 'center', inline: 'nearest' });
                    const rect = target.getBoundingClientRect();
                    const opts = { bubbles: true, cancelable: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 };
                    target.dispatchEvent(new PointerEvent('pointerdown', opts));
                    target.dispatchEvent(new MouseEvent('mousedown', opts));
                    target.dispatchEvent(new PointerEvent('pointerup', opts));
                    target.dispatchEvent(new MouseEvent('mouseup', opts));
                    target.dispatchEvent(new MouseEvent('click', opts));
                    return true;
                }"""
            )
            if clicked:
                print("[gopay] 选择 GoPay 付款方式。")
                time.sleep(2)
                return True
        except Exception:
            pass
    try:
        clicked = page.evaluate(
            """() => {
                const visible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = getComputedStyle(el);
                    return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                };
                const label = Array.from(document.querySelectorAll('button, [role="button"], div, label, span'))
                    .find((el) => visible(el) && (el.innerText || el.textContent || '').trim() === 'GoPay');
                if (!label) return false;
                const candidates = [];
                let node = label;
                for (let i = 0; node && i < 7; i += 1, node = node.parentElement) {
                    candidates.push(node);
                }
                const target = candidates.find((el) => {
                    const rect = el.getBoundingClientRect();
                    const text = (el.innerText || el.textContent || '').trim();
                    return visible(el) && text.includes('GoPay') && rect.width >= 120 && rect.height >= 48;
                }) || label;
                target.scrollIntoView({ block: 'center', inline: 'nearest' });
                const rect = target.getBoundingClientRect();
                const opts = { bubbles: true, cancelable: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 };
                target.dispatchEvent(new PointerEvent('pointerdown', opts));
                target.dispatchEvent(new MouseEvent('mousedown', opts));
                target.dispatchEvent(new PointerEvent('pointerup', opts));
                target.dispatchEvent(new MouseEvent('mouseup', opts));
                target.dispatchEvent(new MouseEvent('click', opts));
                return true;
            }"""
        )
        if clicked:
            print("[gopay] 选择 GoPay 付款方式。")
            time.sleep(2)
            return True
    except Exception:
        pass
    for frame in checkout_frames(page):
        try:
            item = frame.locator("text=GoPay").last
            if item.is_visible(timeout=800):
                item.click(timeout=3000)
                print("[gopay] 选择 GoPay 付款方式。")
                time.sleep(1)
                return True
        except Exception:
            pass
    if click_text_button_or_link(page, r"^GoPay$", label="[gopay] 选择 GoPay 付款方式。", timeout=800):
        time.sleep(1)
        return True
    try:
        item = page.locator("text=GoPay").last
        if item.is_visible(timeout=800):
            item.click(timeout=3000)
            print("[gopay] 选择 GoPay 付款方式。")
            time.sleep(1)
            return True
    except Exception:
        pass
    return False


def checkout_frames(page) -> list:
    frames = []
    try:
        frames.append(page.main_frame)
    except Exception:
        pass
    try:
        for frame in page.frames:
            if frame not in frames:
                frames.append(frame)
    except Exception:
        pass
    return frames


def checkout_has_gopay_billing_form(page) -> bool:
    for frame in checkout_frames(page):
        try:
            if frame.evaluate(
                """() => {
                    const text = (document.body?.innerText || '').replace(/\\s+/g, ' ');
                    return /GoPay/.test(text)
                        && /(账单地址|Billing address)/i.test(text)
                        && /(全名|Full name|姓名)/i.test(text)
                        && /(邮编|Postal code|ZIP|Postcode)/i.test(text);
                }"""
            ):
                return True
        except Exception:
            pass
    return False


def checkout_gopay_is_selected(page) -> bool:
    for frame in checkout_frames(page):
        try:
            if frame.evaluate(
                """() => {
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const activeGoPay = Array.from(document.querySelectorAll('button, [role="tab"], [role="button"]'))
                        .filter(visible)
                        .find((el) => {
                            const text = (el.innerText || el.textContent || el.value || '').trim();
                            if (text !== 'GoPay') return false;
                            const selected = el.getAttribute('aria-selected') || el.getAttribute('data-selected') || '';
                            const checked = el.getAttribute('aria-checked') || '';
                            const cls = el.className || '';
                            return selected === 'true' || checked === 'true' || /selected|active/i.test(cls);
                        });
                    if (activeGoPay) return true;
                    const text = (document.body?.innerText || '').replace(/\\s+/g, ' ');
                    return /用\\s*GoPay\\s*完成结账|Complete.*GoPay|GoPay.*checkout/i.test(text);
                }"""
            ):
                return True
        except Exception:
            pass
    return False


def checkout_country_is_selected(page, country: str = "新加坡") -> bool:
    targets = [country]
    if country == "新加坡":
        targets.append("Singapore")
    elif country == "美国":
        targets.extend(["United States", "US"])
    for frame in checkout_frames(page):
        try:
            if frame.evaluate(
                """({ targets }) => {
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const controls = Array.from(document.querySelectorAll('select, button, [role="button"], [role="combobox"]'))
                        .filter(visible)
                        .filter((el) => {
                            const meta = [
                                el.name,
                                el.id,
                                el.getAttribute('aria-label'),
                                el.closest('label')?.innerText,
                                el.parentElement?.innerText,
                            ].join(' ');
                            return /country|region|国家|地区/i.test(meta);
                        });
                    return controls.some((el) => {
                        const selected = el.tagName === 'SELECT'
                            ? (el.selectedOptions[0]?.textContent || el.value || '')
                            : (el.innerText || el.textContent || el.value || '');
                        return targets.some((target) => selected.includes(target));
                    });
                }""",
                {"targets": targets},
            ):
                return True
        except Exception:
            pass
    return False


def maybe_select_checkout_country(page, country: str = "新加坡") -> bool:
    if checkout_country_is_selected(page, country):
        return False
    dropdown = find_checkout_country_dropdown(page)
    if not dropdown:
        return False
    if set_checkout_country_select(dropdown, country):
        return True
    try:
        print(f"[gopay] 账单地址国家/地区选择: {country}")
        dropdown.scroll_into_view_if_needed(timeout=1000)
        dropdown.click(timeout=3000)
        time.sleep(1)
    except Exception:
        try:
            dropdown.evaluate("(el) => el.click()")
            time.sleep(1)
        except Exception:
            return False
    return click_checkout_country_option(page, country)


def set_checkout_country_select(element, country: str = "新加坡") -> bool:
    targets = [country]
    if country == "新加坡":
        targets.append("Singapore")
    elif country == "美国":
        targets.extend(["United States", "US"])
    try:
        result = element.evaluate(
            """(el, targets) => {
                if (el.tagName !== 'SELECT') return false;
                const option = Array.from(el.options).find((item) => {
                    const text = item.textContent || item.value || '';
                    return targets.some((target) => text.includes(target));
                });
                if (!option) return false;
                el.value = option.value;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }""",
            targets,
        )
        if result:
            print(f"[gopay] 已选择{country}。")
            time.sleep(0.5)
            return True
    except Exception:
        pass
    return False


def find_checkout_country_dropdown(page):
    patterns = [
        r"(日本|Japan|美国|United States|新加坡|Singapore|国家或地区|Country|Region)",
        r"(国家或地区|Country|Region)",
    ]
    for frame in checkout_frames(page):
        for pattern in patterns:
            try:
                handle = frame.evaluate_handle(
                    """(patternText) => {
                        const re = new RegExp(patternText, 'i');
                        const visible = (el) => {
                            const rect = el.getBoundingClientRect();
                            const style = getComputedStyle(el);
                            return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                        };
                        const nodes = Array.from(document.querySelectorAll('button, [role="button"], [role="combobox"], select'))
                            .filter((node) => visible(node) && re.test((node.innerText || node.textContent || node.value || '').trim()));
                        nodes.sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top);
                        return nodes[0] || null;
                    }""",
                    pattern,
                )
                element = handle.as_element()
                if element:
                    return element
            except Exception:
                pass
    return None


def click_checkout_country_option(page, country: str = "新加坡") -> bool:
    targets = [country]
    if country == "新加坡":
        targets.append("Singapore")
    elif country == "美国":
        targets.extend(["United States", "US"])
    for frame in checkout_frames(page):
        try:
            result = frame.evaluate(
                """async ({ targets }) => {
                    const nextFrame = () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const txt = (el) => (el.innerText || el.textContent || el.value || '').trim().replace(/\\s+/g, ' ');
                    const isTarget = (el) => targets.some((target) => txt(el) === target || txt(el).includes(target));
                    const click = () => {
                        const exactOptions = Array.from(document.querySelectorAll('[role="option"], [role="menuitem"], option'))
                            .filter((el) => visible(el) && targets.some((target) => txt(el) === target));
                        const fuzzyOptions = Array.from(document.querySelectorAll('[role="option"], [role="menuitem"], option'))
                            .filter((el) => visible(el) && isTarget(el));
                        const otherTargets = Array.from(document.querySelectorAll('button, div, span'))
                            .filter((el) => visible(el) && targets.some((target) => txt(el) === target));
                        const item = exactOptions[0] || fuzzyOptions[0] || otherTargets[0];
                        if (!item) return false;
                        item.scrollIntoView({ block: 'center', inline: 'nearest' });
                        const rect = item.getBoundingClientRect();
                        const opts = { bubbles: true, cancelable: true, clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 };
                        item.dispatchEvent(new PointerEvent('pointerdown', opts));
                        item.dispatchEvent(new MouseEvent('mousedown', opts));
                        item.dispatchEvent(new PointerEvent('pointerup', opts));
                        item.dispatchEvent(new MouseEvent('mouseup', opts));
                        item.dispatchEvent(new MouseEvent('click', opts));
                        item.click();
                        return true;
                    };
                    if (click()) return true;
                    const scrollables = Array.from(document.querySelectorAll('*')).filter((el) => visible(el) && el.scrollHeight > el.clientHeight + 20);
                    for (const el of scrollables) {
                        el.scrollTop = el.scrollHeight;
                        el.dispatchEvent(new Event('scroll', { bubbles: true }));
                    }
                    await nextFrame();
                    return click();
                }""",
                {"targets": targets},
            )
            if result:
                print(f"[gopay] 已选择{country}。")
                return True
        except Exception:
            pass
    return False


def fill_checkout_billing_address(page, address: dict) -> bool:
    fields = [
        ("name", "全名", address["full_name"]),
        ("name", "Full name", address["full_name"]),
        ("name", "姓名", address["full_name"]),
        ("address", "地址", address["address"]),
        ("address", "Address", address["address"]),
        ("address", "地址行", address["address"]),
        ("postal", "邮编", address["postal_code"]),
        ("postal", "Postal code", address["postal_code"]),
        ("postal", "ZIP", address["postal_code"]),
        ("postal", "Postcode", address["postal_code"]),
    ]
    filled_kinds = set()
    for kind, label, value in fields:
        for frame in checkout_frames(page):
            locator_candidates = [
                lambda frame=frame, label=label: frame.get_by_label(label, exact=False),
                lambda frame=frame, label=label: frame.get_by_placeholder(label, exact=False),
            ]
            for make_locator in locator_candidates:
                try:
                    locator = make_locator().first
                    if locator.is_visible(timeout=500) and locator.is_enabled(timeout=500):
                        fill_auth_field(locator, value, label=label, timeout=3000)
                        filled_kinds.add(kind)
                        break
                except Exception:
                    pass
    dom_kinds = fill_checkout_inputs_by_dom(page, address)
    filled_kinds.update(dom_kinds)
    filled = "name" in filled_kinds and "postal" in filled_kinds
    if filled:
        print("[gopay] 已填写账单姓名、地址和邮编。")
    else:
        print("[gopay] 未识别到账单地址输入框，继续等待页面。")
    return filled


def fill_checkout_inputs_by_dom(page, address: dict) -> set[str]:
    filled_kinds = set()
    for frame in checkout_frames(page):
        try:
            result = frame.evaluate(
                """({ address }) => {
                    const visible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = getComputedStyle(el);
                        return rect.width > 0 && rect.height > 0 && style.display !== 'none' && style.visibility !== 'hidden';
                    };
                    const countryTargets = String(address.country_code || address.country || '').toUpperCase() === 'US'
                        ? ['美国', 'United States', 'US']
                        : ['新加坡', 'Singapore', 'SG'];
                    const setValue = (el, value) => {
                        const type = String(el.type || 'text').toLowerCase();
                        if (['hidden', 'checkbox', 'radio', 'submit', 'button', 'file'].includes(type)) return;
                        const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
                        const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
                        if (setter) setter.call(el, value);
                        else el.value = value;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    };
                    const inputs = Array.from(document.querySelectorAll('input, textarea')).filter(visible);
                    const changed = [];
                    for (const el of inputs) {
                        const meta = [
                            el.name,
                            el.id,
                            el.autocomplete,
                            el.placeholder,
                            el.getAttribute('aria-label'),
                            el.closest('label')?.innerText,
                        ].join(' ').toLowerCase();
                        if (/postal|zip|postcode|邮编/.test(meta)) {
                            setValue(el, address.postal_code); changed.push('postal');
                        } else if (/city|locality|城市/.test(meta)) {
                            if (address.city) { setValue(el, address.city); changed.push('city'); }
                        } else if (/state|province|州|省/.test(meta)) {
                            const value = address.state || address.state_full || '';
                            if (value) { setValue(el, value); changed.push('state'); }
                        } else if (/address.*line.*1|address-line1|line1|street-address|street|address|地址/.test(meta)) {
                            setValue(el, address.address); changed.push('address');
                        } else if (/full.*name|billing.*name|cardholder.*name|customer.*name|^\\s*name\\s*$|全名|姓名/.test(meta)) {
                            setValue(el, address.full_name); changed.push('name');
                        }
                    }
                    const country = Array.from(document.querySelectorAll('select')).find((el) => {
                        const meta = [
                            el.name,
                            el.id,
                            el.autocomplete,
                            el.getAttribute('aria-label'),
                            el.closest('label')?.innerText,
                            el.parentElement?.innerText,
                        ].join(' ').toLowerCase();
                        return visible(el) && /country|国家|地区/.test(meta);
                    });
                    if (country) {
                        const option = Array.from(country.options).find((item) => {
                            const text = item.textContent || item.value || "";
                            return countryTargets.some((target) => text.includes(target));
                        });
                        if (option) {
                            country.value = option.value;
                            country.dispatchEvent(new Event('input', { bubbles: true }));
                            country.dispatchEvent(new Event('change', { bubbles: true }));
                            changed.push('country');
                        }
                    }
                    return Array.from(new Set(changed));
                }""",
                {"address": address},
            )
            if isinstance(result, list):
                filled_kinds.update(str(item) for item in result)
        except Exception:
            pass
    return filled_kinds


def wait_for_gopay_next(email: str, mail_url: str) -> None:
    print("")
    print(f"[gopay] {email} 已登录并停在官网。")
    if mail_url:
        print(f"[gopay] 接码地址: {mail_url}")
    print("[gopay] 请在浏览器里完成 gopay 手动订阅配置。")
    while True:
        command = input("[gopay] 配置好后在这里输入 next 保存并进入下一轮: ").strip().lower()
        if command == "next":
            return
        if command in {"quit", "exit", "q"}:
            raise KeyboardInterrupt
        print("[gopay] 未保存。请输入 next 继续，或输入 quit 结束。")


def cmd_gopay_manual_login(args) -> int:
    try:
        account_input = choose_account_input(args)
    except Exception as exc:
        mark_failure(args, f"账号输入读取失败: {exc}")
        return 1

    email = (getattr(args, "email", "") or "").strip()
    if account_input and not email:
        email = account_input["email"]
    if not email:
        email = input("[gopay] ChatGPT 邮箱: ").strip()
    if not email:
        mark_failure(args, "邮箱不能为空")
        return 1

    password = getattr(args, "password", "") or ""
    if account_input and not password:
        password = account_input["password"]
    mail_url = account_input.get("mail_url", "") if account_input else ""
    if account_input:
        setattr(args, "account_input_override", account_input)
    account_file_without_password = bool(account_input) and not password
    if account_file_without_password:
        print("[gopay] 账号文件未提供密码，自动使用邮箱验证码登录。")

    tried_email_codes: set[str] = set()
    account_timeout_seconds = max(30, int(getattr(args, "account_timeout_seconds", 180) or 180))
    account_deadline = time.time() + account_timeout_seconds

    def remaining_seconds() -> float:
        return max(0.0, account_deadline - time.time())

    def timed_out() -> bool:
        return remaining_seconds() <= 0

    headless = bool(getattr(args, "headless", False))
    mode_label = "无头浏览器" if headless else "可见浏览器"
    use_system_chrome = bool(getattr(args, "use_system_chrome", True))
    chrome_path = resolve_system_chrome_executable() if use_system_chrome else ""
    chrome_label = f"本机 Google Chrome: {chrome_path}" if chrome_path else "Playwright Chromium"
    print(f"[gopay] {email} 正在打开 ChatGPT 官网{mode_label}（{chrome_label}），会优先使用一次性验证码登录。")

    if sync_playwright is None:
        mark_failure(args, f"Playwright 未安装或导入失败: {PLAYWRIGHT_IMPORT_ERROR}", error_type="page_changed")
        print("[hint] 先运行: python -m pip install -r requirements.txt && python -m playwright install chromium")
        return 1

    browser = None
    saved = False
    login_clicked = False
    email_submitted = False
    otp_requested = False
    otp_submitted = False
    hosted_link_opened = False
    invalid_state_retry_count = 0
    max_invalid_state_retries = max(0, int(getattr(args, "invalid_state_retries", 2) or 2))
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(**get_playwright_launch_options(headless=headless, use_system_chrome=use_system_chrome))
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()
            set_auth_stage(args, "open_chatgpt_home")
            page.goto(CHATGPT_HOME_URL, wait_until="domcontentloaded", timeout=min(60000, max(10000, int(remaining_seconds() * 1000))))
            time.sleep(3)

            for step in range(1, int(getattr(args, "max_steps", 60) or 60) + 1):
                if timed_out():
                    mark_failure(args, f"单账号登录超过 {account_timeout_seconds}s，已主动结束。", error_type="account_timeout")
                    break
                if is_phone_required_page(page):
                    mark_failure(args, "登录阶段出现手机号必填页，已弃置当前账号并继续后续账号", error_type="phone_required")
                    break
                invalid_state = detect_auth_invalid_state(page)
                if invalid_state:
                    if invalid_state_retry_count < max_invalid_state_retries and retry_auth_invalid_state_in_place(
                        page,
                        args,
                        invalid_state,
                        invalid_state_retry_count + 1,
                        max_invalid_state_retries,
                        label=email,
                    ):
                        invalid_state_retry_count += 1
                        time.sleep(2)
                        continue
                    error_type = "no_valid_organizations" if invalid_state == "no_valid_organizations" else "invalid_state"
                    mark_failure(args, f"验证过程中出错({invalid_state})，当前页重试耗尽，弃置当前账号", error_type=error_type)
                    break

                if is_payment_or_checkout_page(page):
                    if getattr(args, "gopay_hosted_link", False):
                        address = getattr(args, "gopay_billing_address", None)
                        if not isinstance(address, dict):
                            address = fetch_random_gopay_billing_address(getattr(args, "gopay_billing_country", "sg"))
                            setattr(args, "gopay_billing_address", address)
                        maybe_select_gopay_payment_method(page)
                        if not fill_hosted_checkout_billing(page, address):
                            time.sleep(2)
                            continue
                    else:
                        if not maybe_prepare_gopay_billing(page, args):
                            time.sleep(2)
                            continue
                    set_auth_stage(args, "gopay_manual_wait")
                    print("[gopay] 已进入付款/gopay 配置页，GoPay 和账单地址已准备好，暂停等待人工配置。")
                    wait_for_gopay_next(email, mail_url)
                    result = append_gopay_completed_entry(
                        {"email": email, "mail_url": mail_url},
                        output_dir=getattr(args, "output_dir", str(DEFAULT_GOPAY_OUTPUT_DIR)),
                    )
                    setattr(args, "task_output_json", result["json_path"])
                    setattr(args, "task_local_written", True)
                    setattr(args, "task_gopay_saved", True)
                    saved = True
                    if account_input and getattr(args, "remove_after_success", False):
                        if remove_account_from_input_file(args.account_file, account_input["email"]):
                            setattr(args, "task_removed_from_input", True)
                            print(f"[input] 已从账号输入文件移除: {account_input['email']}")
                    return 0

                if is_chatgpt_logged_in(page):
                    if getattr(args, "gopay_hosted_link", False):
                        if not hosted_link_opened and subscription_country_ready_for_hosted(page):
                            set_auth_stage(args, "generate_plus_hosted_link")
                            try:
                                address = fetch_random_gopay_billing_address(getattr(args, "gopay_billing_country", "sg"))
                                setattr(args, "gopay_billing_address", address)
                                url = generate_plus_hosted_checkout_url(page, billing_country="id")
                                hosted_link_opened = True
                                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                                print("[gopay] 已打开 Plus Hosted 长链接，准备填写账单。")
                            except Exception as exc:
                                mark_failure(args, f"生成/打开 Plus Hosted 长链接失败: {exc}", error_type="payment_link_error")
                                break
                            time.sleep(2)
                            continue
                        set_auth_stage(args, "wait_plus_trial_page")
                        progressed = False
                        if maybe_select_subscription_country(page, args, "印度尼西亚"):
                            progressed = True
                        elif maybe_click_plus_free_trial(page, args):
                            progressed = True
                        elif maybe_open_free_trial(page, args):
                            progressed = True
                        elif maybe_continue_ready_page(page, args):
                            progressed = True
                        elif maybe_start_tips_modal(page, args):
                            progressed = True
                        if progressed:
                            continue
                        print("[gopay] 已登录，继续查找免费试用/套餐页入口；确认套餐页切到 IDR 后才生成长链接。")
                        time.sleep(1)
                    else:
                        if not otp_submitted and account_file_without_password:
                            print("[gopay] 已检测到登录态，继续查找免费试用入口。")
                        set_auth_stage(args, "wait_free_trial_entry")
                        time.sleep(1)
                        continue

                if maybe_complete_chatgpt_onboarding(page, args):
                    continue
                if maybe_skip_usage_reason(page, args):
                    continue
                if maybe_continue_ready_page(page, args):
                    continue
                if maybe_start_tips_modal(page, args):
                    continue
                if maybe_select_subscription_country(page, args, "印度尼西亚"):
                    continue
                if maybe_click_plus_free_trial(page, args):
                    continue
                if maybe_open_free_trial(page, args):
                    continue

                if not login_clicked and click_chatgpt_login(page):
                    login_clicked = True
                    set_auth_stage(args, "click_login")
                    print("[gopay] 已进入登录流程，准备填写邮箱。")
                    continue

                email_input = maybe_visible(page, EMAIL_SELECTORS, timeout=1000)
                if email_input:
                    set_auth_stage(args, "fill_email")
                    print("[gopay] 填入邮箱")
                    if not fill_auth_field(email_input, email, label="邮箱"):
                        time.sleep(2)
                        continue
                    if click_auth_continue(page, email_input):
                        email_submitted = True
                        print("[gopay] 已提交邮箱，等待一次性验证码入口。")
                    else:
                        print("[gopay] 邮箱已填写，但没有点到继续按钮，继续观察页面。")
                    time.sleep(3)
                    continue

                password_input = maybe_visible(page, PASSWORD_SELECTORS, timeout=1000)
                if password_input:
                    set_auth_stage(args, "fill_password_or_otp_switch")
                    if (getattr(args, "prefer_otp", True) or account_file_without_password) and click_otp_switch(page):
                        otp_requested = True
                        print("[gopay] 已选择一次性验证码登录，等待验证码输入框。")
                        continue
                    if account_file_without_password:
                        print("[gopay] 账号文件没有密码，正在继续查找一次性验证码入口。")
                        time.sleep(2)
                        continue
                    if password:
                        print("[gopay] 填入密码")
                        if not fill_auth_field(password_input, password, label="密码"):
                            time.sleep(2)
                            continue
                        click_auth_continue(page, password_input)
                        status, detail = wait_for_password_submit_result(page, timeout=8)
                        if status == "invalid":
                            if getattr(args, "auto_mail_code", True) and mail_url and click_otp_switch(page):
                                otp_requested = True
                                print("[gopay] 密码校验失败，已自动切换到一次性验证码登录。")
                                time.sleep(2)
                                continue
                            mark_failure(args, "密码错误或邮箱账号不匹配", error_type="password_error")
                            break
                        time.sleep(1)
                        continue
                    if click_otp_switch(page):
                        continue
                    password = getpass.getpass("[gopay] 当前页面需要密码，输入密码（留空则继续等待一次性验证码入口）: ")
                    continue

                if not otp_requested and click_otp_switch(page):
                    otp_requested = True
                    print("[gopay] 已选择一次性验证码登录，等待验证码输入框。")
                    continue

                code_input = maybe_visible(page, CODE_SELECTORS, timeout=1000)
                if code_input:
                    set_auth_stage(args, "wait_otp")
                    code = ""
                    if getattr(args, "auto_mail_code", True):
                        print("[gopay] 检测到验证码输入框，开始自动读取邮箱验证码。")
                        code = wait_any_email_code(
                            mail_url,
                            email=email,
                            timeout=min(int(getattr(args, "mail_code_timeout", 60) or 60), max(1, int(remaining_seconds()))),
                            interval=getattr(args, "mail_code_interval", 5.0),
                            exclude=tried_email_codes,
                        )
                    if timed_out() and not code:
                        mark_failure(args, f"等待验证码超过单账号总时限 {account_timeout_seconds}s", error_type="mail_code_timeout")
                        break
                    if not code:
                        code = input("[gopay] 输入邮箱验证码: ").strip()
                    if not code:
                        print("[gopay] 验证码为空，取消。")
                        break
                    print("[gopay] 填入验证码")
                    set_auth_stage(args, "submit_otp")
                    if not fill_auth_field(code_input, code, label="验证码"):
                        time.sleep(2)
                        continue
                    click_auth_continue(page, code_input)
                    status, detail = wait_for_code_submit_result(page, timeout=12)
                    if status == "invalid":
                        tried_email_codes.add(code)
                        print(f"[gopay] 验证码无效或过期: {detail}，请重新输入新验证码。")
                        continue
                    otp_submitted = True
                    print("[gopay] 验证码已提交，继续等待进入 ChatGPT 官网登录态。")
                    time.sleep(2)
                    continue

                if step % 8 == 0:
                    stage_hint = (
                        "等待登录按钮" if not login_clicked else
                        "等待邮箱提交结果" if not email_submitted else
                        "等待一次性验证码入口/验证码框" if not otp_requested else
                        "等待验证码提交后登录完成"
                    )
                    print(f"[gopay] {email} {stage_hint}，当前 URL: {page.url}")
                time.sleep(1)

            if not saved and getattr(args, "keep_open_on_fail", False):
                input("[gopay] 未完成登录。浏览器保持打开，手动查看后按回车关闭: ")
        finally:
            set_auth_stage(args, "closing_browser")
            if browser and (saved or not getattr(args, "keep_open_on_fail", False)):
                browser.close()

    if not saved and not getattr(args, "last_error", ""):
        mark_failure(args, "未登录到 ChatGPT 官网", error_type="login_not_completed")
    return 0 if saved else 1


def cmd_start(args) -> int:
    session_path = Path(args.session)
    code_verifier, code_challenge = _generate_pkce()
    state = secrets.token_urlsafe(16)
    auth_url = _build_auth_url(code_challenge, state)
    session = {
        "state": state,
        "code_verifier": code_verifier,
        "auth_url": auth_url,
        "redirect_uri": CODEX_REDIRECT_URI,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    save_session(session_path, session)
    print("[start] session:", session_path)
    print("[start] redirect_uri:", CODEX_REDIRECT_URI)
    print("[start] auth_url:", auth_url)

    if args.open:
        webbrowser.open(auth_url)

    waiter = CallbackWaiter(state)
    try:
        waiter.start()
    except OSError as exc:
        print(f"[error] 端口 {CODEX_CALLBACK_PORT} 被占用或不可用: {exc}", file=sys.stderr)
        print("[hint] 仍可复制 auth_url 登录后，用 exchange --callback 粘贴回调 URL 或 code。")
        return 2

    print(f"[start] 正在监听 http://127.0.0.1:{CODEX_CALLBACK_PORT}/auth/callback")
    try:
        code = waiter.wait(args.timeout)
        if waiter.error:
            print(f"[error] {waiter.error}", file=sys.stderr)
            return 1
        if not code:
            if args.no_prompt:
                print("[start] 等待超时，未收到回调。")
                return 1
            raw = input("[start] 粘贴回调 URL 或 code（留空则只保留 session）: ").strip()
            if not raw:
                print("[start] 未换取 token；后续可运行 exchange --callback。")
                return 0
            parsed = parse_callback_or_code(raw, expected_state=state)
            if parsed["error"]:
                print(f"[error] {parsed['error']}", file=sys.stderr)
                return 1
            code = parsed["code"]

        bundle = exchange_code(code, code_verifier, fallback_email=args.email)
        write_result_outputs(args, bundle)
        maybe_save_store(args, bundle)
        return 0
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    finally:
        waiter.stop()


def cmd_exchange(args) -> int:
    try:
        session = load_session(Path(args.session))
        parsed = parse_callback_or_code(args.callback, expected_state=session.get("state", ""))
        if parsed["error"]:
            print(f"[error] {parsed['error']}", file=sys.stderr)
            return 1
        bundle = exchange_code(parsed["code"], session["code_verifier"], fallback_email=args.email)
        write_result_outputs(args, bundle)
        maybe_save_store(args, bundle)
        return 0
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


def cmd_refresh(args) -> int:
    try:
        rt = (args.refresh_token or "").strip()
        if not rt:
            print("[error] refresh_token 为空", file=sys.stderr)
            return 1
        bundle = refresh_bundle(rt)
        if not bundle:
            return 1
        if args.email and not bundle.get("email"):
            bundle["email"] = args.email
        write_result_outputs(args, bundle)
        maybe_save_store(args, bundle, refreshed=True)
        return 0
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


def cmd_login(args) -> int:
    try:
        account_input = choose_account_input(args)
    except Exception as exc:
        mark_failure(args, f"账号输入读取失败: {exc}")
        return 1
    if account_input:
        setattr(args, "account_input_override", account_input)

    email = (args.email or "").strip()
    if account_input and not email:
        email = account_input["email"]
    if not email:
        email = input("[login] ChatGPT 邮箱: ").strip()
    if not email:
        mark_failure(args, "邮箱不能为空")
        return 1

    password = args.password or ""
    if account_input and not password:
        password = account_input["password"]
    mail_url = account_input.get("mail_url", "") if account_input else ""
    account_file_without_password = bool(account_input) and not password
    if account_file_without_password:
        print("[login] 账号文件未提供密码，自动使用邮箱验证码登录。")
    if args.ask_password_plain and not password:
        password = input("[login] 密码（明文输入，留空则后续可改用验证码登录）: ")
    if args.ask_password and not password:
        password = getpass.getpass("[login] 密码（留空则尝试邮箱验证码登录）: ")

    manual_code = args.code or ""
    tried_email_codes: set[str] = set()
    auth_mode = (getattr(args, "auth_mode", "") or "team_helper").strip().lower()
    code_verifier, code_challenge = _generate_pkce()
    state = secrets.token_urlsafe(16)
    auth_url = _build_auth_url(code_challenge, state)
    auth_code = ""
    saw_oauth_consent = False
    invalid_state_retry_count = 0
    max_invalid_state_retries = max(0, int(getattr(args, "invalid_state_retries", 2) or 2))
    account_timeout_seconds = max(30, int(getattr(args, "account_timeout_seconds", 180) or 180))
    account_deadline = time.time() + account_timeout_seconds

    def remaining_seconds() -> float:
        return max(0.0, account_deadline - time.time())

    def timed_out() -> bool:
        return remaining_seconds() <= 0

    print("[login] auth_url:", auth_url)
    headless = bool(getattr(args, "headless", False))
    mode_label = "无头浏览器" if headless else "可见浏览器"
    print(f"[login] {email} 正在打开{mode_label}，遇到验证码时会自动读取邮箱；识别不到再回到终端提示输入。")

    if sync_playwright is None:
        mark_failure(args, f"Playwright 未安装或导入失败: {PLAYWRIGHT_IMPORT_ERROR}", error_type="page_changed")
        print("[hint] 先运行: python -m pip install -r requirements.txt && python -m playwright install chromium")
        return 1

    browser = None
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(**get_playwright_launch_options(headless=headless))
            # Playwright 的 new_context() 是临时无痕上下文，不复用本机 Chrome 资料和登录态。
            context = browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            grant_auth_local_network_access(context, page)

            def capture(url: str) -> str:
                nonlocal auth_code
                code = capture_code_from_url(url)
                if code and not auth_code:
                    auth_code = code
                return auth_code

            def has_auth_code() -> bool:
                return bool(auth_code)

            page.on("request", lambda request: capture(request.url))
            page.on("response", lambda response: capture(response.url))
            page.on("framenavigated", lambda frame: capture(frame.url))
            set_auth_stage(args, "open_auth_url")
            page.goto(auth_url, wait_until="domcontentloaded", timeout=min(60000, max(10000, int(remaining_seconds() * 1000))))
            time.sleep(3)

            for step in range(1, args.max_steps + 1):
                if timed_out():
                    mark_failure(args, f"单账号授权超过 {account_timeout_seconds}s，已主动结束，避免占住并发线程。", error_type="account_timeout")
                    break
                if is_phone_required_page(page):
                    if sms_enabled(args):
                        try:
                            set_auth_stage(args, f"{sms_provider_name(args) or 'sms'}_phone_required")
                            print("[SMS] 授权阶段出现手机号必填页，开始自动申请手机号并接收短信验证码。")
                            handle_phone_required_with_sms_provider(page, args, remaining_seconds)
                            continue
                        except Exception as exc:
                            mark_failure(args, f"接码平台手机号验证失败: {exc}", error_type="phone_required")
                            break
                    else:
                        mark_failure(args, "授权阶段出现手机号必填页，已弃置当前账号并继续后续账号", error_type="phone_required")
                        break
                invalid_state = detect_auth_invalid_state(page)
                if invalid_state:
                    if invalid_state_retry_count < max_invalid_state_retries and retry_auth_invalid_state_in_place(
                        page,
                        args,
                        invalid_state,
                        invalid_state_retry_count + 1,
                        max_invalid_state_retries,
                        label=email,
                    ):
                        invalid_state_retry_count += 1
                        time.sleep(2)
                        continue
                    error_type = "no_valid_organizations" if invalid_state == "no_valid_organizations" else "invalid_state"
                    mark_failure(args, f"验证过程中出错({invalid_state})，当前页重试耗尽，弃置当前账号", error_type=error_type)
                    break
                if is_oauth_consent_url(page.url):
                    saw_oauth_consent = True
                capture(page.url)
                if auth_code:
                    break

                email_input = maybe_visible(page, EMAIL_SELECTORS, timeout=1000)
                if email_input:
                    set_auth_stage(args, "fill_email")
                    print("[login] 填入邮箱")
                    if not fill_auth_field(email_input, email, label="邮箱"):
                        time.sleep(2)
                        continue
                    _click_primary_auth_button(page, email_input, ["Continue", "继续", "Log in"])
                    time.sleep(3)
                    continue

                password_input = maybe_visible(page, PASSWORD_SELECTORS, timeout=1000)
                if password_input:
                    set_auth_stage(args, "fill_password_or_otp_switch")
                    if (args.prefer_otp or account_file_without_password) and click_otp_switch(page):
                        continue

                    if account_file_without_password:
                        print("[login] 账号文件没有密码，继续等待邮箱验证码入口...")
                        time.sleep(2)
                        continue

                    if password:
                        print("[login] 填入密码")
                        if not fill_auth_field(password_input, password, label="密码"):
                            time.sleep(2)
                            continue
                        _click_primary_auth_button(page, password_input, ["Continue", "继续", "Log in"])
                        status, detail = wait_for_password_submit_result(page, timeout=8)
                        if status == "invalid":
                            if getattr(args, "auto_mail_code", True) and mail_url and click_otp_switch(page):
                                print("[login] 密码校验失败，已自动切换到一次性验证码登录。")
                                time.sleep(2)
                                continue
                            mark_failure(args, "密码错误或邮箱账号不匹配", error_type="password_error")
                            break
                        if status == "pending":
                            print("[login] 密码已提交，但页面还没明确推进，继续观察。")
                        time.sleep(1)
                        continue

                    if args.ask_password_plain:
                        password = input("[login] 当前页面需要密码（明文输入，留空则使用一次性验证码登录）: ")
                    else:
                        password = getpass.getpass("[login] 当前页面需要密码，输入密码（留空则使用一次性验证码登录）: ")
                    if not password:
                        if click_otp_switch(page):
                            continue
                        print("[login] 未找到一次性验证码入口，已取消密码输入。")
                        break
                    continue

                code_input = maybe_visible(page, CODE_SELECTORS, timeout=1000)
                if code_input:
                    set_auth_stage(args, "wait_otp")
                    otp_since = datetime.now(timezone.utc)
                    code = manual_code
                    manual_code = ""
                    if not code and getattr(args, "auto_mail_code", True):
                        print("[login] 检测到验证码输入框，开始自动读取邮箱验证码。")
                        code = wait_any_email_code(
                            mail_url,
                            email=email,
                            timeout=min(int(getattr(args, "mail_code_timeout", 60) or 60), max(1, int(remaining_seconds()))),
                            interval=getattr(args, "mail_code_interval", 5.0),
                            exclude=tried_email_codes,
                            since=otp_since,
                        )
                    if timed_out() and not code:
                        mark_failure(args, f"等待验证码超过单账号总时限 {account_timeout_seconds}s", error_type="mail_code_timeout")
                        break
                    if not code:
                        code = input("[login] 输入邮箱验证码: ").strip()
                    if not code:
                        print("[login] 验证码为空，取消。")
                        break
                    print("[login] 填入验证码")
                    set_auth_stage(args, "submit_otp")
                    if not fill_auth_field(code_input, code, label="验证码"):
                        time.sleep(2)
                        continue
                    _click_primary_auth_button(page, code_input, ["Continue", "继续", "Verify"])
                    status, detail = wait_for_code_submit_result(page, timeout=12)
                    if status == "invalid":
                        tried_email_codes.add(code)
                        print(f"[login] 验证码无效或过期: {detail}，请重新输入新验证码。")
                        continue
                    if status == "pending":
                        print("[login] 验证码已提交，但页面还没明确推进；先保留当前验证码，不计入旧码排除。")
                    else:
                        print("[login] 验证码已提交，页面已推进。")
                    time.sleep(1)
                    continue

                if handle_authorization_step(page, auth_mode, label=email, capture=capture, has_auth_code=has_auth_code):
                    set_auth_stage(args, "consent_or_workspace")
                    continue

                if step % 8 == 0:
                    print(f"[login] {email} 等待页面推进中，当前 URL: {page.url}")
                time.sleep(1)

            capture(page.url)
            if not auth_code and args.keep_open_on_fail:
                input("[login] 未捕获 code。浏览器保持打开，手动处理后按回车关闭: ")
        finally:
            set_auth_stage(args, "closing_browser")
            if browser and (auth_code or not args.keep_open_on_fail):
                browser.close()

    if not auth_code:
        if getattr(args, "last_error", ""):
            return 1
        if saw_oauth_consent:
            mark_failure(args, "OAuth consent 页已出现，但未捕获到 authorization code", error_type="oauth_consent_callback_missing")
            return 1
        mark_failure(args, "未捕获到 OAuth authorization code", error_type="oauth_callback_missing")
        return 1

    try:
        set_auth_stage(args, "exchange_token")
        bundle = exchange_code(auth_code, code_verifier, fallback_email=email)
        payload = write_result_outputs(args, bundle)
        maybe_save_store(args, payload)
        if auth_mode == "team_pending":
            print("[ok] team pending 授权文件仅本地落盘，不上传服务器数据库。")
            setattr(args, "task_server_skipped", True)
            record_run_stat(args, "server_skipped")
        else:
            if upload_bundle_to_server(payload, account_type=auth_mode):
                setattr(args, "task_server_uploaded", True)
                record_run_stat(args, "server_uploaded")
            else:
                setattr(args, "task_server_failed", True)
                record_run_stat(args, "server_failed")
        if account_input and getattr(args, "remove_after_success", False):
            if remove_account_from_input_file(args.account_file, account_input["email"]):
                setattr(args, "task_removed_from_input", True)
                print(f"[input] 已从账号输入文件移除: {account_input['email']}")
        return 0
    except Exception as exc:
        mark_failure(args, f"授权结果处理失败: {exc}")
        return 1


def cmd_save(args) -> int:
    try:
        token_path = Path(args.token_file)
        if not token_path.exists():
            print(f"[error] 找不到 token 文件: {token_path}", file=sys.stderr)
            return 1
        bundle = json.loads(read_text(token_path))
        account = upsert_account_bundle(Path(args.store), bundle)
        print(f"[ok] 已保存到账号库: {Path(args.store)}")
        print_account(account, print_token=args.print_token)
        return 0
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


def cmd_list(args) -> int:
    try:
        data = load_account_store(Path(args.store))
        accounts = data.get("accounts", [])
        if not accounts:
            print(f"[list] 账号库为空: {Path(args.store)}")
            return 0
        print(f"[list] 账号库: {Path(args.store)}")
        print(f"[list] 共 {len(accounts)} 个账号")
        for index, account in enumerate(accounts, start=1):
            email = account.get("email") or "-"
            plan = account.get("plan_type") or "unknown"
            account_id = account.get("account_id") or "-"
            expired = float(account.get("expired") or 0)
            left = int(expired - time.time()) if expired else 0
            status = f"{left}s 后过期" if left > 0 else "已过期/未知"
            print(f"{index}. {email} | {plan} | {account_id} | {status}")
        return 0
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


def cmd_show(args) -> int:
    try:
        data = load_account_store(Path(args.store))
        account = find_account(data, args.account)
        if not account:
            print(f"[error] 账号库中找不到: {args.account}", file=sys.stderr)
            return 1
        print_account(account, print_token=args.print_token)
        return 0
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


def cmd_get_at(args) -> int:
    try:
        store_path = Path(args.store)
        data = load_account_store(store_path)
        account = find_account(data, args.account)
        if not account:
            print(f"[error] 账号库中找不到: {args.account}", file=sys.stderr)
            return 1
        rt = (account.get("refresh_token") or "").strip()
        if not rt:
            print(f"[error] 账号缺少 refresh_token: {args.account}", file=sys.stderr)
            return 1

        bundle = refresh_bundle(rt)
        if not bundle:
            return 1
        bundle = merge_refresh_with_existing(bundle, account)
        saved = upsert_account_bundle(store_path, bundle, refreshed=True)
        write_result_outputs(args, saved)
        print(f"[ok] 账号库已更新: {store_path}")
        return 0
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


def cmd_inputs(args) -> int:
    try:
        account_path = Path(args.account_file)
        if not account_path.exists():
            print(f"[error] 找不到账号输入文件: {account_path}", file=sys.stderr)
            return 1
        entries = parse_account_file_text(read_text(account_path))
        print(f"[inputs] 共解析到 {len(entries)} 个账号")
        for index, entry in enumerate(entries, start=1):
            if args.limit and index > args.limit:
                break
            print(
                f"{index}. {entry['email']} | password={'yes' if entry['password'] else 'no'} "
                f"| mail_url={'yes' if entry['mail_url'] else 'no'}"
            )
        return 0
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


def cmd_status_db(args) -> int:
    try:
        db_path = Path(args.state_db)
        state_db.init_db(db_path)
        counts = state_db.count_by_status(db_path)
        print(f"[state] SQLite 状态库: {db_path}")
        if counts:
            print("[state] 状态统计: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
        else:
            print("[state] 还没有运行记录。")
        rows = state_db.latest_tasks(db_path, limit=args.limit)
        if not rows:
            return 0
        print(f"[state] 最近 {len(rows)} 条:")
        for row in rows:
            email = row.get("email") or "-"
            account_type = row.get("account_type") or "-"
            status = row.get("status") or "-"
            error_type = row.get("error_type") or "-"
            attempts = row.get("attempt_count") or 0
            updated_at = row.get("updated_at") or "-"
            stage = row.get("current_stage") or "-"
            print(f"- {updated_at} | {email} | {account_type} | {status} | stage={stage} | attempts={attempts} | error={error_type}")
        return 0
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


def cmd_authorize(args) -> int:
    try:
        ensure_auth_project_layout()
        print("请选择功能：")
        print("1. team helper 专用")
        print("2. 普通授权文件生成")
        print("3. team pending 授权")
        print("4. gopay手动订阅")
        choice = (args.kind or input("输入 1/2/3/4: ")).strip()
        config = auth_kind_config(choice)

        account_file = Path(config["account_file"])
        if config.get("input_type") == "pending_json":
            entries = load_pending_inputs(account_file)
            input_label = "pending 输入目录"
        else:
            entries = parse_account_file_text(read_text(account_file)) if account_file.exists() else []
            input_label = "账号文件"
        print(f"[{config['title']}] {input_label}: {account_file}")
        print(f"[{config['title']}] 当前可用账号: {len(entries)} 个")
        if not entries:
            if config.get("input_type") == "pending_json":
                print("[hint] 请先把 pending 账号 JSON 或 account.txt 放进对应目录。")
            else:
                print("[hint] 请先把账号放进对应 account.txt。")
            return 1

        preview_count = min(len(entries), args.preview_limit)
        for index, entry in enumerate(entries[:preview_count], start=1):
            extra = f" | file={Path(entry['json_path']).name}" if entry.get("json_path") else ""
            if entry.get("account_txt_path"):
                extra = f" | file={Path(entry['account_txt_path']).name}"
            print(f"{index}. {entry['email']} | password={'yes' if entry['password'] else 'no'}{extra}")
        if len(entries) > preview_count:
            print(f"... 还有 {len(entries) - preview_count} 个未显示")

        count_raw = (args.count or input("这次授权几个账号？直接回车默认 1 个: ")).strip()
        count = int(count_raw) if count_raw else 1
        if count < 1:
            print("[error] 授权数量必须 >= 1", file=sys.stderr)
            return 1

        indexes_raw = (args.indexes or input("要从号池指定序号可输入 1,3,5；直接回车默认从前往后: ")).strip()
        selected: list[dict]
        if indexes_raw:
            indexes = []
            for item in indexes_raw.split(","):
                item = item.strip()
                if not item:
                    continue
                indexes.append(int(item))
            selected = []
            for index in indexes:
                if index < 1 or index > len(entries):
                    print(f"[error] 账号序号超出范围: {index}", file=sys.stderr)
                    return 1
                selected.append(entries[index - 1])
            selected = selected[:count]
        else:
            selected = entries[:count]

        if len(selected) < count:
            print(f"[warn] 可用账号不足，只会处理 {len(selected)} 个。")

        workers = 1
        if len(selected) > 1:
            if config.get("manual_subscription"):
                print("[gopay] 手动订阅流程需要在后端逐个输入 next，已固定为单线程顺序处理。")
            else:
                workers_raw = str(getattr(args, "workers", "") or input("并发线程数？直接回车默认 1: ")).strip()
                workers = int(workers_raw) if workers_raw else 1
                if workers < 1:
                    print("[error] 并发线程数必须 >= 1", file=sys.stderr)
                    return 1
                workers = min(workers, len(selected))
        if workers > 1:
            print(f"[parallel] 启用并发授权：{workers} 个线程。OAuth code 从各自浏览器页面捕获，不共用本地回调监听端口。")
        stagger_seconds = max(0.0, float(getattr(args, "stagger_seconds", 2.0) or 0.0))
        if workers > 1 and stagger_seconds > 0:
            print(f"[parallel] 并发启动错峰：每个线程槽位间隔 {stagger_seconds:.1f}s。")
        retry_count = max(0, int(getattr(args, "retries", 0) or 0))
        retry_cooldown_seconds = max(0.0, float(getattr(args, "retry_cooldown_seconds", 0.0) or 0.0))
        domain_stagger_seconds = max(0.0, float(getattr(args, "domain_stagger_seconds", 0.0) or 0.0))
        headless_fallback = bool(getattr(args, "headless_fallback", False))
        if retry_count:
            print(f"[scheduler] 失败自动重试：最多 {retry_count} 次，冷却 {retry_cooldown_seconds:.1f}s。")
        if domain_stagger_seconds > 0:
            print(f"[scheduler] 同邮箱域名错峰：每个域名间隔 {domain_stagger_seconds:.1f}s。")
        if headless_fallback and getattr(args, "headless", False):
            print("[scheduler] 已启用无头失败自动切有头兜底。")

        run_stats = {
            "local_written": 0,
            "sub_written": 0,
            "store_saved": 0,
            "server_uploaded": 0,
            "server_failed": 0,
            "server_skipped": 0,
            "gopay_saved": 0,
        }
        state_db_path = Path(getattr(args, "state_db", str(DEFAULT_STATE_DB)))
        state_db.init_db(state_db_path)
        stale_count = state_db.mark_stale_running_failed(state_db_path, older_than_seconds=max(300, int(getattr(args, "account_timeout_seconds", 180) or 180) * 2))
        if stale_count:
            print(f"[scheduler] 已把 {stale_count} 个历史 running 超时任务标记为 failed，不影响本次重跑。")
        domain_next_start: dict[str, float] = {}
        domain_lock = threading.Lock()

        def wait_domain_slot(email: str) -> None:
            if domain_stagger_seconds <= 0:
                return
            domain = domain_of_email(email)
            if not domain:
                return
            with domain_lock:
                now = time.time()
                available_at = domain_next_start.get(domain, now)
                wait_seconds = max(0.0, available_at - now)
                domain_next_start[domain] = max(now, available_at) + domain_stagger_seconds
            if wait_seconds > 0:
                print(f"[scheduler] {email} 与同域名账号错峰，等待 {wait_seconds:.1f}s。")
                time.sleep(wait_seconds)

        def run_selected_entry(index: int, entry: dict) -> tuple[int, dict, int, str]:
            if workers > 1 and stagger_seconds > 0:
                delay = ((index - 1) % workers) * stagger_seconds
                if delay > 0:
                    print(f"[parallel] {entry['email']} 延迟 {delay:.1f}s 后启动。")
                    time.sleep(delay)
            print(f"[{config['title']}] === {index}/{len(selected)}: {entry['email']} ===")
            source_path = str(entry.get("json_path") or entry.get("account_txt_path") or config["account_file"])
            source_type = str(entry.get("pending_source_type") or ("pending_json" if config.get("input_type") == "pending_json" else "account_txt"))
            last_error = ""
            last_type = ""
            base_headless = bool(getattr(args, "headless", False))
            current_headless = base_headless
            attempts_total = retry_count + 1 + (1 if headless_fallback and base_headless else 0)

            for attempt in range(1, attempts_total + 1):
                wait_domain_slot(entry["email"])
                local_args = argparse.Namespace(**vars(args))
                local_args.run_stats = run_stats
                local_args.account_file = config["account_file"]
                local_args.account_email = entry["email"]
                local_args.account_input_override = entry if config.get("input_type") == "pending_json" or config.get("manual_subscription") else None
                local_args.output_dir = config["output_dir"]
                local_args.rt_txt = config["rt_txt"]
                local_args.standard_output = True
                local_args.save_store = False if config.get("manual_subscription") else True
                local_args.remove_after_success = config.get("input_type") != "pending_json"
                local_args.email = entry["email"]
                local_args.password = entry["password"]
                local_args.account_index = 0
                local_args.auth_mode = config["kind"]
                local_args.prefer_otp = True if config.get("manual_subscription") else getattr(args, "prefer_otp", False)
                local_args.headless = current_headless
                local_args.task_output_json = ""
                local_args.task_local_written = False
                local_args.task_rt_saved = False
                local_args.task_sub_written = False
                local_args.task_store_saved = False
                local_args.task_server_uploaded = False
                local_args.task_server_skipped = False
                local_args.task_server_failed = False
                local_args.task_removed_from_input = False
                local_args.task_gopay_saved = False
                local_args.task_source_path = source_path
                local_args.current_stage = ""
                local_args.last_error = ""
                local_args.error_type = ""

                if attempt > 1:
                    print(f"[scheduler] 重试 {attempt}/{attempts_total}: {entry['email']}")
                state_db.start_task(
                    state_db_path,
                    email=entry["email"],
                    account_type=config["kind"],
                    source_type=source_type,
                    source_path=source_path,
                    headless=bool(getattr(local_args, "headless", False)),
                )
                try:
                    if config.get("manual_subscription"):
                        code = cmd_gopay_manual_login(local_args)
                    else:
                        code = cmd_login(local_args)
                except Exception as exc:
                    code = 1
                    mark_failure(local_args, f"未捕获异常: {exc}")

                if code == 0 and config.get("input_type") == "pending_json":
                    if remove_pending_input_entry(entry):
                        local_args.task_removed_from_input = True
                        print(f"[input] 已移除 pending 输入: {source_path}")

                last_error = getattr(local_args, "last_error", "") or ("授权成功" if code == 0 else "授权失败，详见上方日志")
                last_type = getattr(local_args, "error_type", "") or classify_exit(code, last_error)
                state_db.finish_task(
                    state_db_path,
                    email=entry["email"],
                    account_type=config["kind"],
                    source_path=source_path,
                    status="success" if code == 0 else "failed",
                    error_type="" if code == 0 else last_type,
                    last_error="" if code == 0 else last_error,
                    output_json=getattr(local_args, "task_output_json", ""),
                    rt_saved=bool(getattr(local_args, "task_rt_saved", False)),
                    store_saved=bool(getattr(local_args, "task_store_saved", False)),
                    server_uploaded=bool(getattr(local_args, "task_server_uploaded", False)),
                    server_skipped=bool(getattr(local_args, "task_server_skipped", False)) or bool(getattr(local_args, "task_gopay_saved", False)),
                    removed_from_input=bool(getattr(local_args, "task_removed_from_input", False)),
                )
                if code == 0 and getattr(local_args, "task_gopay_saved", False):
                    record_run_stat(local_args, "gopay_saved")
                if code == 0:
                    return index, entry, code, ""

                if (
                    headless_fallback
                    and current_headless
                    and attempt < attempts_total
                ):
                    print(f"[scheduler] {entry['email']} 无头失败，下一次切换为可见浏览器兜底。")
                    current_headless = False
                if attempt < attempts_total and retry_cooldown_seconds > 0:
                    print(f"[scheduler] {entry['email']} 冷却 {retry_cooldown_seconds:.1f}s 后重试。")
                    time.sleep(retry_cooldown_seconds)

            return index, entry, 1, last_type or classify_error(last_error)

        results: list[tuple[int, dict, int, str]] = []
        if workers == 1:
            for index, entry in enumerate(selected, start=1):
                results.append(run_selected_entry(index, entry))
        else:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(run_selected_entry, index, entry)
                    for index, entry in enumerate(selected, start=1)
                ]
                for future in as_completed(futures):
                    results.append(future.result())

        success = 0
        failure_types: dict[str, int] = {}
        for _index, entry, code, failure_type in sorted(results, key=lambda item: item[0]):
            if code == 0:
                success += 1
            else:
                category = failure_type or "unknown"
                failure_types[category] = failure_types.get(category, 0) + 1
                print(f"[warn] 当前账号授权失败，已保留在输入文件: {entry['email']} | 分类={category}")

        print(f"[{config['title']}] 完成：成功 {success} / {len(selected)}")
        print(
            f"[summary] CPA JSON 落盘 {run_stats['local_written']}，"
            f"SUB JSON 更新 {run_stats['sub_written']}，"
            f"账号库保存 {run_stats['store_saved']}，"
            f"服务器同步成功 {run_stats['server_uploaded']}，"
            f"服务器未同步/失败 {run_stats['server_failed']}，"
            f"服务器跳过 {run_stats['server_skipped']}，"
            f"gopay 手动记录 {run_stats['gopay_saved']}。"
        )
        if failure_types:
            print("[summary] 失败分类: " + ", ".join(f"{key}={value}" for key, value in sorted(failure_types.items())))
        print(f"[summary] SQLite 状态库: {state_db_path}")
        return 0 if success == len(selected) else 1
    except Exception as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", default="", help="把完整 token JSON 写到指定文件")
    parser.add_argument("--print-token", action="store_true", help="在终端打印完整 access/refresh token")
    parser.add_argument("--email", default="", help="id_token 无邮箱时使用的 fallback 邮箱")
    parser.add_argument("--store", default=str(DEFAULT_ACCOUNT_STORE), help="本地账号库 JSON 路径")
    parser.add_argument("--save-store", action="store_true", help="把本次 token bundle 保存到本地账号库")
    parser.add_argument("--standard-output", action="store_true", help="输出标准 JSON 文件名，并维护 账号----RT 的 TXT")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="标准 JSON 输出目录")
    parser.add_argument("--rt-txt", default=str(DEFAULT_RT_TXT), help="标准 TXT 输出文件，格式为 账号----refresh_token")
    parser.add_argument("--sub-out", default="", help="SUB 聚合格式输出文件，默认写到输出分类目录 sub2api_accounts.json")
    parser.add_argument("--no-sub-output", action="store_true", help="标准输出时不写 SUB 聚合 JSON")
    parser.add_argument("--sms-provider", default="", help="接码平台：herosms / grizzly / fivesim")
    parser.add_argument("--sms-api-key", default="", help="接码平台 API Key")
    parser.add_argument("--sms-service", default="", help="接码平台服务代码")
    parser.add_argument("--sms-country", type=int, default=0, help="接码平台国家 ID")
    parser.add_argument("--sms-country-iso", default="", help="手机号国家 ISO")
    parser.add_argument("--sms-dial-code", default="", help="手机号国家区号")
    parser.add_argument("--sms-country-name", default="", help="手机号国家名称")
    parser.add_argument("--sms-operator", default="", help="接码平台运营商/服务商；留空为任何")
    parser.add_argument("--sms-poll-interval", type=float, default=0.0, help="短信验证码轮询间隔秒数")
    parser.add_argument("--sms-max-attempts", type=int, default=0, help="短信验证码最大轮询次数")
    parser.add_argument("--fivesim-country-slug", default="", help="5sim 国家 slug，如 indonesia / philippines；provider=fivesim 时必填")
    parser.add_argument("--hero-sms-api-key", default="", help="HeroSMS API Key；传入后手机号必填页会自动接码")
    parser.add_argument("--hero-sms-service", default="dr", help="HeroSMS 服务代码，默认 dr")
    parser.add_argument("--hero-sms-country", type=int, default=0, help="HeroSMS 国家 ID")
    parser.add_argument("--hero-sms-country-iso", default="", help="手机号国家 ISO")
    parser.add_argument("--hero-sms-dial-code", default="", help="手机号国家区号")
    parser.add_argument("--hero-sms-country-name", default="", help="手机号国家名称")
    parser.add_argument("--hero-sms-operator", default="", help="HeroSMS 运营商；留空为任何运营商")
    parser.add_argument("--hero-sms-poll-interval", type=float, default=5.0, help="短信验证码轮询间隔秒数")
    parser.add_argument("--hero-sms-max-attempts", type=int, default=60, help="短信验证码最大轮询次数")


def add_store_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--store", default=str(DEFAULT_ACCOUNT_STORE), help="本地账号库 JSON 路径")
    parser.add_argument("--print-token", action="store_true", help="在终端打印完整 access/refresh token")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="获取 OpenAI/Codex OAuth access_token + refresh_token")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="生成 OAuth 授权链接，监听 localhost 回调，并换取 token")
    add_common_args(start)
    start.add_argument("--open", action="store_true", help="生成后用默认浏览器打开授权链接")
    start.add_argument("--timeout", type=int, default=180, help="等待 localhost 回调的秒数")
    start.add_argument("--session", default=str(DEFAULT_SESSION_FILE), help="保存 PKCE/session 状态的文件")
    start.add_argument("--no-prompt", action="store_true", help="超时后不进入手动粘贴提示")
    start.set_defaults(func=cmd_start)

    exchange = sub.add_parser("exchange", help="使用上一轮 session 和 callback URL/code 换取 token")
    add_common_args(exchange)
    exchange.add_argument("--callback", required=True, help="OpenAI 回调 URL 或纯 code")
    exchange.add_argument("--session", default=str(DEFAULT_SESSION_FILE), help="读取 PKCE/session 状态的文件")
    exchange.set_defaults(func=cmd_exchange)

    refresh = sub.add_parser("refresh", help="使用 refresh_token 刷新 access_token")
    add_common_args(refresh)
    refresh.add_argument("--refresh-token", required=True, help="OAuth refresh_token")
    refresh.set_defaults(func=cmd_refresh)

    login = sub.add_parser("login", help="打开浏览器并自动填写邮箱/密码/验证码后换取 token")
    add_common_args(login)
    login.add_argument("--password", default="", help="账号密码；不建议写入 shell 历史")
    login.add_argument("--ask-password", action="store_true", help="启动后在本机隐藏输入密码")
    login.add_argument("--ask-password-plain", action="store_true", help="启动后用明文输入密码，方便 PowerShell 粘贴")
    login.add_argument("--prefer-otp", action="store_true", help="看到密码页时优先点击一次性验证码登录")
    login.add_argument("--code", default="", help="本轮邮箱验证码；没有则运行中提示输入")
    login.add_argument("--no-auto-mail-code", dest="auto_mail_code", action="store_false", default=True, help="不要从 account.txt 的邮箱地址自动读取验证码")
    login.add_argument("--mail-code-timeout", type=int, default=60, help="自动等待邮箱验证码的最长秒数，MoeMail 会自动限制到 60 秒内")
    login.add_argument("--mail-code-interval", type=float, default=2.0, help="自动轮询邮箱验证码的间隔秒数")
    login.add_argument("--max-steps", type=int, default=60, help="最多推进页面步骤数")
    login.add_argument("--account-timeout-seconds", type=int, default=180, help="单个账号授权总超时，超时会关闭浏览器")
    login.add_argument("--invalid-state-retries", type=int, default=2, help="检测到验证错误页时，在当前页面点击重试的次数")
    login.add_argument("--keep-open-on-fail", action="store_true", help="失败时保留浏览器，方便人工查看页面")
    login.add_argument("--headless", action="store_true", help="使用无头浏览器运行")
    login.add_argument("--no-system-chrome", dest="use_system_chrome", action="store_false", default=True, help="流程四调试用：不使用本机 Google Chrome，改用 Playwright Chromium")
    login.add_argument("--gopay-hosted-link", action="store_true", help="流程四：登录后生成 Plus Hosted 长链接，只自动打开链接并填账单，后续支付人工完成")
    login.add_argument("--gopay-billing-country", choices=["sg", "us"], default="sg", help="流程四账单地址国家，默认 sg，可选 us")
    login.add_argument("--incognito", action="store_true", default=True, help="使用临时无痕上下文（默认行为）")
    login.add_argument("--account-file", default="", help="批量账号输入文件，例如 account.txt")
    login.add_argument("--account-index", type=int, default=0, help="从账号输入文件中选第 N 个账号；默认选第一个未处理账号")
    login.add_argument("--account-email", default="", help="从账号输入文件中选择指定邮箱")
    login.add_argument("--remove-after-success", action="store_true", help="授权成功后从账号输入文件中移除该账号")
    login.add_argument("--auth-mode", choices=["team_helper", "normal"], default="team_helper", help="授权策略：team_helper 保持默认，normal 只点击继续/授权")
    login.add_argument("--state-db", default=str(DEFAULT_STATE_DB), help="SQLite 状态库路径")
    login.set_defaults(func=cmd_login)

    authorize = sub.add_parser("authorize", help="交互式授权入口：选择 team helper/普通授权，批量处理对应号池")
    add_common_args(authorize)
    authorize.add_argument("--kind", default="", help="授权类型：team helper/普通授权/team pending 或 1/2/3；不填则交互选择")
    authorize.add_argument("--count", default="", help="本次授权几个账号；不填则交互输入")
    authorize.add_argument("--indexes", default="", help="指定号池序号，例如 1,3,5；不填则从前往后")
    authorize.add_argument("--workers", default="", help="并发线程数；不填则交互输入，默认 1")
    authorize.add_argument("--stagger-seconds", type=float, default=2.0, help="并发启动错峰秒数，默认 2")
    authorize.add_argument("--retries", type=int, default=0, help="失败后自动重试次数，默认 0")
    authorize.add_argument("--retry-cooldown-seconds", type=float, default=0.0, help="每次失败重试前冷却秒数，默认 0")
    authorize.add_argument("--domain-stagger-seconds", type=float, default=0.0, help="同邮箱域名账号启动错峰秒数，默认 0")
    authorize.add_argument("--headless-fallback", action="store_true", help="无头浏览器失败后自动追加一次可见浏览器兜底")
    authorize.add_argument("--preview-limit", type=int, default=20, help="菜单里最多预览多少个账号")
    authorize.add_argument("--password", default="", help=argparse.SUPPRESS)
    authorize.add_argument("--ask-password", action="store_true", help=argparse.SUPPRESS)
    authorize.add_argument("--ask-password-plain", action="store_true", help=argparse.SUPPRESS)
    authorize.add_argument("--prefer-otp", action="store_true", help="看到密码页时优先点击一次性验证码登录")
    authorize.add_argument("--code", default="", help=argparse.SUPPRESS)
    authorize.add_argument("--no-auto-mail-code", dest="auto_mail_code", action="store_false", default=True, help="不要从 account.txt 的邮箱地址自动读取验证码")
    authorize.add_argument("--mail-code-timeout", type=int, default=60, help="自动等待邮箱验证码的最长秒数，MoeMail 会自动限制到 60 秒内")
    authorize.add_argument("--mail-code-interval", type=float, default=2.0, help="自动轮询邮箱验证码的间隔秒数")
    authorize.add_argument("--max-steps", type=int, default=60, help="每个账号最多推进页面步骤数")
    authorize.add_argument("--account-timeout-seconds", type=int, default=180, help="单个账号授权总超时，超时会关闭浏览器并释放并发线程")
    authorize.add_argument("--invalid-state-retries", type=int, default=2, help="检测到验证错误页时，在当前页面点击重试的次数")
    authorize.add_argument("--keep-open-on-fail", action="store_true", help="失败时保留浏览器，方便人工查看页面")
    authorize.add_argument("--headless", action="store_true", help="使用无头浏览器运行")
    authorize.add_argument("--no-system-chrome", dest="use_system_chrome", action="store_false", default=True, help=argparse.SUPPRESS)
    authorize.add_argument("--gopay-hosted-link", action="store_true", help="流程四：登录后生成 Plus Hosted 长链接，只自动打开链接并填账单，后续支付人工完成")
    authorize.add_argument("--gopay-billing-country", choices=["sg", "us"], default="sg", help="流程四账单地址国家，默认 sg，可选 us")
    authorize.add_argument("--incognito", action="store_true", default=True, help=argparse.SUPPRESS)
    authorize.add_argument("--account-file", default="", help=argparse.SUPPRESS)
    authorize.add_argument("--account-index", type=int, default=0, help=argparse.SUPPRESS)
    authorize.add_argument("--account-email", default="", help=argparse.SUPPRESS)
    authorize.add_argument("--remove-after-success", action="store_true", help=argparse.SUPPRESS)
    authorize.add_argument("--auth-mode", default="", help=argparse.SUPPRESS)
    authorize.add_argument("--state-db", default=str(DEFAULT_STATE_DB), help="SQLite 状态库路径")
    authorize.set_defaults(func=cmd_authorize)

    inputs = sub.add_parser("inputs", help="解析并预览 account.txt，不打印密码明文")
    inputs.add_argument("--account-file", required=True, help="批量账号输入文件，例如 account.txt")
    inputs.add_argument("--limit", type=int, default=50, help="最多显示多少条")
    inputs.set_defaults(func=cmd_inputs)

    status_db = sub.add_parser("status-db", help="查看授权运行 SQLite 状态库")
    status_db.add_argument("--state-db", default=str(DEFAULT_STATE_DB), help="SQLite 状态库路径")
    status_db.add_argument("--limit", type=int, default=20, help="最近显示多少条")
    status_db.set_defaults(func=cmd_status_db)

    save = sub.add_parser("save", help="把 token.json 导入本地账号库")
    add_store_args(save)
    save.add_argument("--token-file", required=True, help="由 start/exchange/login/refresh 生成的 token JSON")
    save.set_defaults(func=cmd_save)

    list_cmd = sub.add_parser("list", help="列出本地账号库里的账号")
    list_cmd.add_argument("--store", default=str(DEFAULT_ACCOUNT_STORE), help="本地账号库 JSON 路径")
    list_cmd.set_defaults(func=cmd_list)

    show = sub.add_parser("show", help="查看某个账号的元信息和 token 预览")
    add_store_args(show)
    show.add_argument("--account", required=True, help="邮箱或 account_id")
    show.set_defaults(func=cmd_show)

    get_at = sub.add_parser("get-at", help="用账号库中的 refresh_token 刷新并获取 access_token")
    add_store_args(get_at)
    get_at.add_argument("--account", required=True, help="邮箱或 account_id")
    get_at.add_argument("--out", default="", help="把刷新后的完整 token JSON 写到指定文件")
    get_at.set_defaults(func=cmd_get_at)

    refresh_account = sub.add_parser("refresh-account", help="get-at 的别名：刷新账号库中的 access_token")
    add_store_args(refresh_account)
    refresh_account.add_argument("--account", required=True, help="邮箱或 account_id")
    refresh_account.add_argument("--out", default="", help="把刷新后的完整 token JSON 写到指定文件")
    refresh_account.set_defaults(func=cmd_get_at)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
