from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta, timezone
import importlib.util
import imaplib
import sys
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import httpx

from .storage import MailAccount
from .utils import PROJECT_ROOT, extract_code, extract_codes, load_env, log


GRAPH_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
IMAP_TOKEN_SCOPE = "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"
OUTLOOK_IMAP_HOST = "outlook.office365.com"
QQ_IMAP_HOST = "imap.qq.com"
APPLEEMAIL_BASE_URL = "https://www.appleemail.top"
ICLOUD_THEFINDNET_BASE_URL = "https://icloud.thefindnet.xyz"
APPLEEMAIL_MAILBOXES = ("INBOX", "Junk")
HOTMAIL_IMAP_MAILBOXES = ("INBOX", "Junk", "Junk Email")
QQ_IMAP_MAILBOXES = ("INBOX",)
MAIL_TIME_SKEW = timedelta(minutes=10)
HOTMAIL_CODE_TIME_SKEW = timedelta(seconds=45)
HOTMAIL_APPLEEMAIL_HTTP_TIMEOUT_SEC = 8
HOTMAIL_APPLEEMAIL_ATTEMPT_TIMEOUT_SEC = 10
HOTMAIL_FALLBACK_INTERVAL_SEC = 60
HOTMAIL_IMAP_ATTEMPT_TIMEOUT_SEC = 15
HOTMAIL_GRAPH_ATTEMPT_TIMEOUT_SEC = 12
HOTMAIL_FALLBACK_MISS_THRESHOLD = 3
HOTMAIL_APPLE_FIRST_HARD_MODE = True


@dataclass
class ImapTokenCacheEntry:
    access_token: str
    refresh_token: str
    expires_at: datetime


_IMAP_TOKEN_CACHE: dict[tuple[str, str], ImapTokenCacheEntry] = {}
_APPLEEMAIL_UNAVAILABLE: set[str] = set()
_HOTMAIL_FALLBACK_LAST_RUN: dict[str, datetime] = {}
_HOTMAIL_APPLEEMAIL_MISS_COUNT: dict[str, int] = {}
_EXTERNAL_IMAP163_FETCHERS: dict[str, Any] = {}
_EXTERNAL_IMAP163_FAILED_PATHS: set[str] = set()

EXTERNAL_MAIL_FETCH_MODE_ENV = "MAIL_FETCH_SOURCE"
EXTERNAL_MAIL_FETCH_MODE_IMAP163 = {"desktop_imap163", "external_imap163", "imap163"}
EXTERNAL_IMAP163_DIR_ENV = "EXTERNAL_IMAP163_DIR"
DEFAULT_EXTERNAL_IMAP163_DIR = ""


def is_appleemail_nonrecoverable_error(message: str) -> bool:
    text = str(message or "")
    lowered = text.lower()
    markers = (
        "not supported",
        "permission",
        "unauthorized",
        "forbidden",
        "only for nineemail",
        "接口仅限九邮微软邮箱使用",
        "九邮微软邮箱",
    )
    return any(marker in lowered or marker in text for marker in markers)


class MailProvider:
    def __init__(self, source: str, timeout_sec: int, poll_interval_sec: int, log_prefix: str = ""):
        self.source = source
        self.timeout_sec = timeout_sec
        self.poll_interval_sec = poll_interval_sec
        self.log_prefix = log_prefix

    def log(self, message: str) -> None:
        log(f"{self.log_prefix} {message}".strip())

    async def wait_code(self, account: MailAccount, since: datetime, exclude: set[str] | None = None) -> str:
        exclude = exclude or set()
        self.log(f"开始等待邮箱验证码: {account.email} | 排除旧码数={len(exclude)}")
        if account.mail_url and self.source != "qq_imap":
            code = await wait_code_with_legacy_adapter(
                account.mail_url,
                account.email,
                self.timeout_sec,
                self.poll_interval_sec,
                exclude,
            )
            self.log(f"已通过旧版接码适配器获取验证码: {code}")
            return code
        deadline = asyncio.get_running_loop().time() + self.timeout_sec
        last_error: str | None = None
        while asyncio.get_running_loop().time() < deadline:
            try:
                code = await self.fetch_code(account, since, exclude)
                if code:
                    if self.source == "hotmail_graph":
                        self.log(f"已从 Hotmail 新邮件提取验证码: {code}")
                    else:
                        self.log(f"已从邮箱来源 {self.source} 提取验证码: {code}")
                    return code
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                self.log(f"邮箱验证码暂未取到: {last_error}")
            await asyncio.sleep(self.poll_interval_sec)
        raise TimeoutError(f"验证码等待超时: {last_error or '没有新验证码'}")

    async def fetch_code(self, account: MailAccount, since: datetime, exclude: set[str] | None = None) -> str | None:
        if self.source == "qq_imap":
            return await fetch_qq_imap_code(account, since, exclude or set())
        if account.mail_url:
            return await fetch_mail_url_code(account.mail_url, exclude or set())
        if self.source == "moemail":
            raise RuntimeError("moemail 账号行需要包含接码地址，例如：账号----xxx@example.com 接码地址----https://...")
        if self.source == "icloud_query":
            return await fetch_icloud_query_code(account, since, exclude or set())
        if self.source != "hotmail_graph":
            raise RuntimeError(f"当前邮箱来源仅支持 moemail / hotmail_graph / icloud_query / qq_imap，实际配置: {self.source}")
        return await fetch_hotmail_graph_code(account, since, exclude or set())


async def fetch_mail_url_code(mail_url: str, exclude: set[str]) -> str | None:
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(mail_url)
        if response.status_code >= 400:
            raise RuntimeError(f"接码地址读取失败: HTTP {response.status_code}")
        code = choose_mail_code(response.text, exclude)
        if code:
            log(f"已从接码地址提取验证码: {code}")
            return code
    return None


async def fetch_icloud_query_code(account: MailAccount, since: datetime, exclude: set[str]) -> str | None:
    if not account.email.lower().endswith("@icloud.com"):
        return None
    query_code = (account.mail_url or "").strip()
    if not query_code:
        raise RuntimeError("iCloud 查询账号需要 email----查询码 格式")
    since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.post(
            f"{ICLOUD_THEFINDNET_BASE_URL}/public/search-emails.php",
            json={"credentials": f"{account.email}----{query_code}"},
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
                ),
                "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json",
                "Origin": ICLOUD_THEFINDNET_BASE_URL,
                "Referer": f"{ICLOUD_THEFINDNET_BASE_URL}/",
            },
        )
        if response.status_code >= 400:
            raise RuntimeError(f"iCloud 查询平台搜索失败: HTTP {response.status_code} {response.text[:120]}")
        data = response.json()
        emails = data.get("emails") or []
        for item in emails:
            received = parse_icloud_time(item)
            if received and received < since_utc:
                continue
            text = "\n".join(
                str(item.get(key) or "")
                for key in ("subject", "from", "to", "date", "snippet", "body_excerpt", "from_email", "to_email")
            )
            code = extract_code(text)
            if code and code not in exclude:
                log(f"已从 iCloud 查询平台提取验证码: {code}")
                return code
        for item in emails:
            received = parse_icloud_time(item)
            if received and received < since_utc:
                continue
            mail_id = item.get("id")
            if not mail_id:
                continue
            body_response = await client.get(
                f"{ICLOUD_THEFINDNET_BASE_URL}/public/get-email-body.php?id={mail_id}",
                headers={"Accept": "application/json", "Referer": f"{ICLOUD_THEFINDNET_BASE_URL}/"},
            )
            if body_response.status_code >= 400:
                continue
            try:
                body_data = body_response.json()
            except Exception:
                body_data = {"html_body": body_response.text}
            text = "\n".join(
                str(body_data.get(key) or "")
                for key in ("subject", "from", "to", "date", "html_body", "text_body", "body", "snippet", "body_excerpt")
            )
            code = extract_code(text)
            if code and code not in exclude:
                log(f"已从 iCloud 查询平台正文提取验证码: {code}")
                return code
    return None


async def wait_code_with_legacy_adapter(
    mail_url: str,
    email: str,
    timeout_sec: int,
    interval_sec: float,
    exclude: set[str],
) -> str:
    env = load_env(".env")
    fetch_mode = (env.get(EXTERNAL_MAIL_FETCH_MODE_ENV) or "").strip().lower()
    if fetch_mode in EXTERNAL_MAIL_FETCH_MODE_IMAP163:
        code = await wait_code_with_external_imap163(
            email=email,
            timeout_sec=timeout_sec,
            exclude=exclude,
            env=env,
        )
        if code:
            return code
        log("[mail] 外部 imap163 抓码未取到验证码，回退项目内置抓码逻辑")

    def run() -> str:
        if str(PROJECT_ROOT) not in sys.path:
            sys.path.insert(0, str(PROJECT_ROOT))
        import get_oauth_rt  # type: ignore

        return get_oauth_rt.wait_any_email_code(
            mail_url,
            email=email,
            timeout=timeout_sec,
            interval=interval_sec,
            exclude=exclude,
        )

    code = await asyncio.to_thread(run)
    if not code:
        raise TimeoutError("legacy mail adapter failed to fetch verification code")
    return code


def load_external_imap163_fetcher(env: dict[str, str]) -> Any | None:
    configured_dir = (env.get(EXTERNAL_IMAP163_DIR_ENV) or DEFAULT_EXTERNAL_IMAP163_DIR).strip()
    if not configured_dir:
        return None
    imap163_path = Path(configured_dir) / "imap163.py"
    cache_key = str(imap163_path.resolve()) if imap163_path.exists() else str(imap163_path)
    if cache_key in _EXTERNAL_IMAP163_FETCHERS:
        return _EXTERNAL_IMAP163_FETCHERS[cache_key]
    if cache_key in _EXTERNAL_IMAP163_FAILED_PATHS:
        return None
    if not imap163_path.exists():
        log(f"[mail] 外部 imap163 脚本不存在: {imap163_path}")
        _EXTERNAL_IMAP163_FAILED_PATHS.add(cache_key)
        return None
    try:
        module_name = f"_external_imap163_{abs(hash(cache_key))}"
        spec = importlib.util.spec_from_file_location(module_name, str(imap163_path))
        if spec is None or spec.loader is None:
            raise RuntimeError("spec_from_file_location returned empty loader")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fetcher = getattr(module, "fetch_otp_for_email", None)
        if not callable(fetcher):
            raise RuntimeError("imap163.py 缺少 fetch_otp_for_email()")
        _EXTERNAL_IMAP163_FETCHERS[cache_key] = fetcher
        log(f"[mail] 已启用外部 imap163 抓码: {imap163_path}")
        return fetcher
    except Exception as exc:  # noqa: BLE001
        _EXTERNAL_IMAP163_FAILED_PATHS.add(cache_key)
        log(f"[mail] 加载外部 imap163 抓码失败: {exc}")
        return None


async def wait_code_with_external_imap163(
    *,
    email: str,
    timeout_sec: int,
    exclude: set[str],
    env: dict[str, str],
) -> str:
    if not email:
        return ""
    fetcher = load_external_imap163_fetcher(env)
    if fetcher is None:
        return ""

    def run() -> str:
        timeout = max(30, int(timeout_sec or 90))
        code = fetcher(email, timeout=timeout)
        return (code or "").strip()

    code = await asyncio.to_thread(run)
    if code and code not in exclude:
        return code
    if code in exclude:
        log("[mail] 外部 imap163 返回的是已尝试旧验证码，继续等待新验证码")
    return ""
def choose_mail_code(text: str, exclude: set[str]) -> str | None:
    normalized = re.sub(r"\s+", " ", text)
    priority_patterns = [
        r"自动识别验证码[^0-9]{0,80}(?:openai|chatgpt)?[^0-9]{0,80}(\d{6})",
        r"(?:openai|chatgpt)[^0-9]{0,120}(\d{6})",
        r"输入此临时验证码以继续[^0-9]{0,80}(\d{6})",
        r"临时验证码[^0-9]{0,80}(\d{6})",
        r"verification code[^0-9]{0,80}(\d{6})",
    ]
    for pattern in priority_patterns:
        for code in re.findall(pattern, normalized, flags=re.I):
            if code not in exclude:
                return code
    for code in extract_codes(normalized):
        if code not in exclude:
            return code
    return None


def parse_icloud_time(item: dict[str, Any]) -> datetime | None:
    for key in ("created_at", "date", "received_at", "receivedDateTime"):
        value = item.get(key)
        if not value:
            continue
        text = str(value).strip()
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            parsed = datetime.fromisoformat(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            try:
                parsed = parsedate_to_datetime(text)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except Exception:
                continue
    return None


async def fetch_hotmail_graph_code(account: MailAccount, since: datetime, exclude: set[str]) -> str | None:
    if not account.client_id or not account.refresh_token:
        raise RuntimeError("Hotmail Graph 需要 email----password----client_id----refresh_token 格式")
    if account.email.lower() not in _APPLEEMAIL_UNAVAILABLE:
        try:
            code = await fetch_appleemail_code(account, since, exclude)
            if code:
                return code
        except Exception as exc:  # noqa: BLE001
            if "不支持或无权限" in str(exc) or is_appleemail_nonrecoverable_error(str(exc)):
                _APPLEEMAIL_UNAVAILABLE.add(account.email.lower())
            log(f"AppleEmail API 取码失败，继续尝试 Outlook IMAP: {exc}")
    imap_error = ""
    try:
        code = await fetch_hotmail_imap_code(account, since, exclude)
        if code:
            return code
    except Exception as exc:  # noqa: BLE001
        imap_error = str(exc)
        log(f"Outlook IMAP 取码失败，回退 Hotmail Graph: {exc}")
    try:
        token = await refresh_graph_access_token(account.client_id, account.refresh_token)
    except Exception as exc:  # noqa: BLE001
        graph_error = str(exc)
        log(f"Hotmail Graph 刷新失败: {graph_error}")
        if "invalid_grant" in imap_error and "invalid_grant" in graph_error:
            raise RuntimeError("hotmail_oauth_invalid_grant: IMAP 和 Graph OAuth 均失效")
        return None
    messages = await list_recent_messages(token)
    for item in messages:
        received = parse_graph_time(item.get("receivedDateTime"))
        if received and received < since:
            continue
        sender = (((item.get("from") or {}).get("emailAddress") or {}).get("address") or "").lower()
        subject = item.get("subject") or ""
        preview = item.get("bodyPreview") or ""
        body = ((item.get("body") or {}).get("content") or "")
        text = f"{sender}\n{subject}\n{preview}\n{body}"
        if not looks_like_openai_mail(text):
            continue
        code = extract_code(text)
        if code and code not in exclude:
            return code
    return None


async def fetch_appleemail_code(account: MailAccount, since: datetime, exclude: set[str]) -> str | None:
    if not account.client_id or not account.refresh_token:
        return None
    since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
    since_floor = since_utc - MAIL_TIME_SKEW
    async with httpx.AsyncClient(timeout=HOTMAIL_APPLEEMAIL_HTTP_TIMEOUT_SEC, follow_redirects=True) as client:
        for mailbox in APPLEEMAIL_MAILBOXES:
            payload = {
                "refresh_token": account.refresh_token,
                "client_id": account.client_id,
                "email": account.email,
                "mailbox": mailbox,
                "response_type": "json",
            }
            url = f"{APPLEEMAIL_BASE_URL}/api/mail-all"
            response = await client.post(
                url,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                json=payload,
            )
            if response.status_code == 405:
                response = await client.get(url, params=payload, headers={"Accept": "application/json"})
            if response.status_code >= 400:
                raise RuntimeError(f"HTTP {response.status_code} {response.text[:160]}")
            data = response.json()
            if appleemail_unavailable(data):
                raise RuntimeError(f"小苹果 API 不支持或无权限访问该邮箱: {appleemail_error_message(data)}")
            messages = normalize_appleemail_messages(data)
            candidates: list[tuple[datetime, str]] = []
            for item in messages:
                received = parse_appleemail_time(item)
                if received and received < since_floor:
                    continue
                text = appleemail_message_text(item)
                if not looks_like_openai_mail(text):
                    continue
                code = extract_code(text)
                if code and code not in exclude:
                    sort_ts = received or datetime.min.replace(tzinfo=timezone.utc)
                    candidates.append((sort_ts, code))
                elif code and code in exclude:
                    log(f"小苹果 API 命中验证码但在排除列表中，等待更新: {code}")
            if candidates:
                candidates.sort(key=lambda x: x[0], reverse=True)
                newest_ts, newest_code = candidates[0]
                log(f"已从小苹果 API 提取验证码: {newest_code} (time={newest_ts.isoformat()})")
                return newest_code
            log(f"小苹果 API {mailbox} 暂未找到 OpenAI 新验证码: {account.email}")
    return None


def appleemail_unavailable(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    success = data.get("success")
    code = str(data.get("code") or "").lower()
    message = appleemail_error_message(data).lower()
    if success is False:
        return True
    unavailable_hints = [
        "invalid",
        "unauthorized",
        "forbidden",
        "not found",
        "no permission",
        "permission",
        "expired",
        "invalid_grant",
        "not exist",
        "auth failed",
        "error",
    ]
    return code in {"401", "403", "404", "400", "invalid_grant"} or any(hint in message for hint in unavailable_hints)


def appleemail_error_message(data: Any) -> str:
    if not isinstance(data, dict):
        return ""
    for key in ("message", "msg", "error", "detail"):
        value = data.get(key)
        if value:
            return str(value)
    return ""


def normalize_appleemail_messages(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("data", "mails", "messages", "results", "list"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if any(key in data for key in ("subject", "body", "text", "html", "code", "verification_code")):
        return [data]
    return []


def appleemail_message_text(item: dict[str, Any]) -> str:
    fields = []
    for key in (
        "from",
        "sender",
        "subject",
        "body",
        "text",
        "html",
        "content",
        "snippet",
        "preview",
        "code",
        "verification_code",
    ):
        value = item.get(key)
        if value is not None:
            fields.append(str(value))
    return "\n".join(fields)


def parse_appleemail_time(item: dict[str, Any]) -> datetime | None:
    for key in ("date", "time", "created_at", "received_at", "receivedDateTime", "internalDate"):
        value = item.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, (int, float)):
            timestamp = float(value) / 1000 if float(value) > 10_000_000_000 else float(value)
            return datetime.fromtimestamp(timestamp, tz=timezone.utc)
        text = str(value).strip()
        try:
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            parsed = datetime.fromisoformat(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(text)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except Exception:
                continue
    return None


async def fetch_hotmail_imap_code(account: MailAccount, since: datetime, exclude: set[str]) -> str | None:
    access_token = await get_imap_access_token(account.client_id or "", account.refresh_token or "")
    return await asyncio.to_thread(fetch_hotmail_imap_code_sync, account.email, access_token, since, exclude)


async def fetch_qq_imap_code(account: MailAccount, since: datetime, exclude: set[str]) -> str | None:
    auth_code = (account.mail_url or account.password or "").strip()
    if not auth_code:
        raise RuntimeError("QQ IMAP 需要 email----授权码 格式；授权码需在 QQ 邮箱设置中开启 IMAP 后生成")
    return await asyncio.to_thread(fetch_qq_imap_code_sync, account.email, auth_code, since, exclude)


async def get_imap_access_token(client_id: str, refresh_token: str) -> str:
    if not client_id or not refresh_token:
        raise RuntimeError("Hotmail IMAP 需要 client_id 和 refresh_token")
    cache_key = (client_id, refresh_token)
    cached = _IMAP_TOKEN_CACHE.get(cache_key)
    now = datetime.now(timezone.utc)
    if cached and cached.expires_at > now + timedelta(minutes=2):
        return cached.access_token
    data = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": cached.refresh_token if cached else refresh_token,
        "scope": IMAP_TOKEN_SCOPE,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(GRAPH_TOKEN_URL, data=data)
        if response.status_code >= 400:
            raise RuntimeError(f"IMAP OAuth 刷新 token 失败: HTTP {response.status_code} {response.text[:200]}")
        payload = response.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise RuntimeError(f"IMAP OAuth 响应缺少 access_token: {payload}")
    expires_in = int(payload.get("expires_in") or 3600)
    _IMAP_TOKEN_CACHE[cache_key] = ImapTokenCacheEntry(
        access_token=access_token,
        refresh_token=payload.get("refresh_token") or data["refresh_token"],
        expires_at=now + timedelta(seconds=max(60, expires_in)),
    )
    return access_token


def fetch_hotmail_imap_code_sync(email: str, access_token: str, since: datetime, exclude: set[str]) -> str | None:
    since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
    date_filter = since_utc.strftime("%d-%b-%Y")
    auth_string = f"user={email}\x01auth=Bearer {access_token}\x01\x01"
    with imaplib.IMAP4_SSL(OUTLOOK_IMAP_HOST, 993) as conn:
        conn.authenticate("XOAUTH2", lambda _response: auth_string.encode("utf-8"))
        for mailbox in HOTMAIL_IMAP_MAILBOXES:
            try:
                select_status, _ = conn.select(mailbox, readonly=True)
            except Exception:
                continue
            if select_status != "OK":
                continue
            status, data = conn.search(None, "SINCE", date_filter)
            if status != "OK":
                continue
            ids = (data[0] or b"").split()
            for message_id in reversed(ids[-40:]):
                status, fetched = conn.fetch(message_id, "(BODY.PEEK[] INTERNALDATE)")
                if status != "OK":
                    continue
                raw_message = b""
                internal_date = None
                for item in fetched:
                    if not isinstance(item, tuple):
                        continue
                    metadata, payload = item
                    raw_message += payload or b""
                    match = re.search(rb'INTERNALDATE "([^"]+)"', metadata or b"")
                    if match:
                        try:
                            internal_date = parsedate_to_datetime(match.group(1).decode("ascii", errors="ignore"))
                        except Exception:
                            internal_date = None
                if internal_date and internal_date < since_utc:
                    continue
                text = decode_imap_message(raw_message)
                if not looks_like_openai_mail(text):
                    continue
                code = extract_code(text)
                if code and code not in exclude:
                    log(f"已从 Outlook IMAP({mailbox}) 提取验证码: {code}")
                    return code
                if code and code in exclude:
                    log(f"Outlook IMAP({mailbox}) 命中验证码但在排除列表中，等待更新: {code}")
    return None


def fetch_qq_imap_code_sync(email: str, auth_code: str, since: datetime, exclude: set[str]) -> str | None:
    since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
    since_floor = since_utc - MAIL_TIME_SKEW
    date_filter = since_floor.strftime("%d-%b-%Y")
    with imaplib.IMAP4_SSL(QQ_IMAP_HOST, 993) as conn:
        conn.login(email, auth_code)
        for mailbox in QQ_IMAP_MAILBOXES:
            try:
                select_status, _ = conn.select(mailbox, readonly=True)
            except Exception:
                continue
            if select_status != "OK":
                continue
            status, data = conn.search(None, "SINCE", date_filter)
            if status != "OK":
                continue
            ids = (data[0] or b"").split()
            for message_id in reversed(ids[-40:]):
                status, fetched = conn.fetch(message_id, "(BODY.PEEK[] INTERNALDATE)")
                if status != "OK":
                    continue
                raw_message = b""
                internal_date = None
                for item in fetched:
                    if not isinstance(item, tuple):
                        continue
                    metadata, payload = item
                    raw_message += payload or b""
                    match = re.search(rb'INTERNALDATE "([^"]+)"', metadata or b"")
                    if match:
                        try:
                            internal_date = parsedate_to_datetime(match.group(1).decode("ascii", errors="ignore"))
                        except Exception:
                            internal_date = None
                received = parse_imap_received_time(raw_message, internal_date)
                if received and received < since_floor:
                    continue
                text = decode_imap_message(raw_message)
                if not looks_like_openai_mail(text):
                    continue
                code = extract_code(text)
                if code and code not in exclude:
                    log(f"已从 QQ IMAP({mailbox}) 提取验证码: {code}")
                    return code
                if code and code in exclude:
                    log(f"QQ IMAP({mailbox}) 命中验证码但在排除列表中，等待更新: {code}")
    return None


def decode_imap_message(raw_message: bytes) -> str:
    from email import policy
    from email.parser import BytesParser

    if not raw_message:
        return ""
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw_message)
    except Exception:
        return raw_message.decode("utf-8", errors="ignore")
    parts: list[str] = []
    subject = str(message.get("subject") or "")
    sender = str(message.get("from") or "")
    parts.extend([sender, subject])
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_type() not in {"text/plain", "text/html"}:
                continue
            try:
                parts.append(part.get_content())
            except Exception:
                payload = part.get_payload(decode=True) or b""
                parts.append(payload.decode(part.get_content_charset() or "utf-8", errors="ignore"))
    else:
        try:
            parts.append(message.get_content())
        except Exception:
            payload = message.get_payload(decode=True) or b""
            parts.append(payload.decode(message.get_content_charset() or "utf-8", errors="ignore"))
    return "\n".join(parts)


def parse_imap_received_time(raw_message: bytes, fallback: datetime | None) -> datetime | None:
    # Junk 邮箱中的 INTERNALDATE 可能是“移动到垃圾箱时间”，优先使用邮件头 Date 判断真实发送时间。
    from email import policy
    from email.parser import BytesParser

    if not raw_message:
        if fallback and not fallback.tzinfo:
            return fallback.replace(tzinfo=timezone.utc)
        return fallback
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw_message)
    except Exception:
        if fallback and not fallback.tzinfo:
            return fallback.replace(tzinfo=timezone.utc)
        return fallback
    date_header = str(message.get("date") or "").strip()
    if date_header:
        try:
            parsed = parsedate_to_datetime(date_header)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except Exception:
            pass
    if fallback and not fallback.tzinfo:
        return fallback.replace(tzinfo=timezone.utc)
    return fallback


async def refresh_graph_access_token(client_id: str, refresh_token: str) -> str:
    data = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "scope": "offline_access Mail.Read User.Read",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(GRAPH_TOKEN_URL, data=data)
        if response.status_code >= 400:
            raise RuntimeError(f"Graph 刷新 token 失败: HTTP {response.status_code} {response.text[:200]}")
        payload = response.json()
    access_token = payload.get("access_token")
    if not access_token:
        raise RuntimeError(f"Graph 刷新 token 响应缺少 access_token: {payload}")
    return access_token


async def list_recent_messages(access_token: str) -> list[dict[str, Any]]:
    params = {
        "$top": "20",
        "$orderby": "receivedDateTime desc",
        "$select": "subject,bodyPreview,body,from,receivedDateTime",
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30) as client:
        all_messages: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        endpoints = (
            GRAPH_MESSAGES_URL,
            f"{GRAPH_MESSAGES_URL}/..",  # guard no-op fallback placeholder
            "https://graph.microsoft.com/v1.0/me/mailFolders/Inbox/messages",
            "https://graph.microsoft.com/v1.0/me/mailFolders/JunkEmail/messages",
            "https://graph.microsoft.com/v1.0/me/mailFolders/DeletedItems/messages",
        )
        for url in endpoints:
            if url.endswith("/.."):
                continue
            response = await client.get(url, params=params, headers=headers)
            if response.status_code >= 400:
                continue
            payload = response.json()
            for item in payload.get("value") or []:
                msg_id = str(item.get("id") or "")
                if msg_id and msg_id in seen_ids:
                    continue
                if msg_id:
                    seen_ids.add(msg_id)
                all_messages.append(item)
    all_messages.sort(key=lambda m: str(m.get("receivedDateTime") or ""), reverse=True)
    return all_messages[:80]


def parse_graph_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            return parsedate_to_datetime(value)
        except Exception:
            return None


def looks_like_openai_mail(text: str) -> bool:
    low = text.lower()
    has_sender_hint = any(
        key in low
        for key in [
            "openai",
            "chatgpt",
            "account-security",
            "noreply@openai.com",
            "info@account.openai.com",
            "auth0.openai.com",
        ]
    )
    has_code_hint = any(
        key in low
        for key in [
            "code",
            "verification",
            "verify",
            "one-time",
            "otp",
            "验证码",
            "临时验证码",
            "登录代码",
            "安全代码",
        ]
    )
    return has_sender_hint and has_code_hint
