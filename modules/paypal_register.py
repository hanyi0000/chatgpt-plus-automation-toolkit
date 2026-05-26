"""PayPal flow-1: register account and generate payment link."""
from __future__ import annotations

import asyncio
import re
import secrets
import shutil
import string
import traceback
from pathlib import Path
from typing import Any

import httpx

from .browser import BrowserSession
from .checkout import create_plus_checkout_link, get_chatgpt_session
from .free_browser_flow import FreeBrowserFlow
from .free_register import FreeProfile, FreeRegisterError, generate_free_profile, random_birth_date
from .mail_provider import MailProvider
from .proxy_pool import ProxyPool
from . import session_export
from .storage import MailAccount, parse_mail_line
from .utils import load_env, log, now_utc, resolve_path, safe_filename


PAYPAL_OUTPUT_ROOT = resolve_path("output/paypal注册")
LINK_POOL_DIR = PAYPAL_OUTPUT_ROOT / "长链接账号"
LINK_POOL_FILE = LINK_POOL_DIR / "account.txt"
PAYPAL_SESSIOND_DIR = PAYPAL_OUTPUT_ROOT / "sessiond"
PAYPAL_SESSION_CACHE_FILE = PAYPAL_SESSIOND_DIR / "session_cache.jsonl"
ICLOUD_DEFAULT_FILE = resolve_path("data/paypal/icloud_accounts.txt")
EXTERNAL_MAIL_FETCH_MODE_IMAP163 = {"desktop_imap163", "external_imap163", "imap163"}


def _is_network_navigation_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(
        key in text
        for key in (
            "err_connection_reset",
            "err_proxy_connection_failed",
            "err_connection_closed",
            "err_timed_out",
            "err_name_not_resolved",
            "net::err",
        )
    )


def _probe_proxy(proxy: str, timeout_sec: int = 12) -> tuple[bool, str]:
    try:
        with httpx.Client(proxy=proxy, timeout=timeout_sec, follow_redirects=False) as c:
            r = c.get("http://example.com")
            if r.status_code == 200:
                return True, "ok"
            body = (r.text or "").strip()
            if r.status_code == 403:
                m = re.search(r"forbidden ip=([0-9.]+)", body, flags=re.I)
                if m:
                    return False, f"403 forbidden: source ip {m.group(1)} not supported by proxy provider"
            return False, f"http {r.status_code}: {body[:160]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"


def generate_chatgpt_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "Lc" + "".join(secrets.choice(alphabet) for _ in range(16)) + "9!"


def load_icloud_accounts(path: Path | None = None) -> list[tuple[str, str]]:
    p = path or ICLOUD_DEFAULT_FILE
    if not p.exists():
        return []
    accounts: list[tuple[str, str]] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----", 1)
        if len(parts) == 2 and "@" in parts[0]:
            accounts.append((parts[0].strip(), parts[1].strip()))
    return accounts


def already_in_link_pool() -> set[str]:
    if not LINK_POOL_FILE.exists():
        return set()
    return {
        line.split("----", 1)[0].strip().lower()
        for line in LINK_POOL_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and "----" in line
    }


def save_to_link_pool(email: str, query_code: str, payment_link: str) -> None:
    LINK_POOL_DIR.mkdir(parents=True, exist_ok=True)
    with LINK_POOL_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{email}----{query_code}----{payment_link}\n")


def remove_from_icloud_file(email: str, path: Path | None = None) -> None:
    p = path or ICLOUD_DEFAULT_FILE
    if not p.exists():
        return
    lines = p.read_text(encoding="utf-8").splitlines()
    remaining = [l for l in lines if not l.strip().lower().startswith(email.lower())]
    p.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")


def _external_imap163_enabled(env: dict[str, str]) -> bool:
    mode = (env.get("MAIL_FETCH_SOURCE") or "").strip().lower()
    return mode in EXTERNAL_MAIL_FETCH_MODE_IMAP163


def _resolve_imap163_domain(env: dict[str, str]) -> str:
    merged = dict(env)
    ext_dir = (env.get("EXTERNAL_IMAP163_DIR") or "").strip()
    if ext_dir:
        ext_env_path = Path(ext_dir) / ".env"
        if ext_env_path.exists():
            for key, value in load_env(ext_env_path).items():
                merged.setdefault(key, value)
    return (merged.get("IMAP163_FORWARD_DOMAIN") or merged.get("MAIL_DOMAIN") or "").strip().lower()


def _generate_imap163_pending(count: int, done: set[str], domain: str) -> list[tuple[str, str]]:
    pending: list[tuple[str, str]] = []
    alphabet = string.ascii_lowercase + string.digits
    while len(pending) < max(1, count):
        email = f"{''.join(secrets.choice(alphabet) for _ in range(10))}@{domain}"
        if email.lower() in done:
            continue
        if any(email.lower() == item[0].lower() for item in pending):
            continue
        pending.append((email, "imap163"))
    return pending


def _normalize_mail_source(value: str) -> str:
    source = (value or "").strip().lower()
    aliases = {
        "hotmail": "hotmail_graph",
        "hotmail_graph": "hotmail_graph",
        "icloud": "icloud_query",
        "icloud_query": "icloud_query",
        "moemail": "moemail",
        "qq": "qq_imap",
        "qq_imap": "qq_imap",
    }
    return aliases.get(source, source or "moemail")


def _active_mail_source(cfg: dict[str, Any]) -> str:
    mail_cfg = cfg.get("mail", {})
    return _normalize_mail_source(str(mail_cfg.get("active_source") or mail_cfg.get("source") or "moemail"))


def _load_accounts_from_file(path: Path, done: set[str]) -> list[MailAccount]:
    if not path.exists():
        return []
    accounts: list[MailAccount] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        account = parse_mail_line(line)
        if not account:
            continue
        if account.email.lower() in done:
            continue
        accounts.append(account)
    return accounts


def _remove_from_account_file(email: str, path: Path) -> None:
    if not path.exists():
        return
    lines = path.read_text(encoding="utf-8").splitlines()
    remaining = []
    for line in lines:
        account = parse_mail_line(line) if line.strip() and not line.strip().startswith("#") else None
        if account and account.email.lower() == email.lower():
            continue
        remaining.append(line)
    path.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")


def filter_accounts_by_email(accounts: list[MailAccount], selected_email: str | None = None) -> list[MailAccount]:
    email = (selected_email or "").strip().lower()
    if not email:
        return accounts
    return [account for account in accounts if account.email.strip().lower() == email]


def resolve_register_proxy_settings(env: dict[str, str]) -> tuple[bool, str]:
    """Resolve the PayPal registration proxy independently from payment proxy settings."""
    use_proxy = (env.get("PAYPAL_REGISTER_USE_PROXY") or "").strip().lower() in ("true", "1", "yes")
    proxy_file = (
        env.get("PAYPAL_REGISTER_PROXY_FILE")
        or env.get("PAYPAL_PROXY_FILE")
        or env.get("PROXY_FILE")
        or "data/proxies/proxies.txt"
    )
    return use_proxy, proxy_file


async def register_one(
    account: MailAccount,
    mail_source: str,
    cfg: dict[str, Any],
    worker_id: int = 1,
    proxy: str | None = None,
    create_payment_link: bool = True,
    session_cache_path: str | Path | None = None,
    session_source: str = "paypal_flow1",
) -> str | None:
    email = account.email
    prefix = f"[paypal-reg-{worker_id:02d}][{email}]"
    chatgpt_password = generate_chatgpt_password()
    full_name, age = generate_free_profile(
        int(cfg.get("register_profile", {}).get("age_min", 21)),
        int(cfg.get("register_profile", {}).get("age_max", 45)),
    )
    profile = FreeProfile(
        full_name=full_name,
        age=age,
        password=chatgpt_password,
        birth_date=random_birth_date(int(age)),
    )

    mail_provider = MailProvider(
        source=mail_source,
        timeout_sec=150,
        poll_interval_sec=5,
        log_prefix=prefix,
    )

    browser_cfg = cfg.get("browser", {})
    profile_dir = resolve_path("profiles") / f"paypal_reg_{safe_filename(email)}"

    session_kwargs = dict(
        profile_dir=profile_dir,
        headless=bool(browser_cfg.get("headless", False)),
        slow_mo=int(browser_cfg.get("slow_mo", 80)),
        timeout_ms=int(browser_cfg.get("timeout_ms", 60000)),
        proxy=proxy,
        fingerprint_seed=email,
    )
    session = BrowserSession(**session_kwargs)

    page = None
    flow = None
    try:
        await session.__aenter__()
        page = await session.current_page()
        flow = FreeBrowserFlow(page, prefix)

        log(f"{prefix} start register")
        for nav_attempt in range(1, 4):
            try:
                await flow.navigate_to_signup_email(email)
                break
            except Exception as exc:
                if nav_attempt == 1 and "wait button timeout" in str(exc):
                    try:
                        body = await page.locator("body").inner_text(timeout=3000)
                    except Exception:
                        body = ""
                    if "ChatGPT" in body and ("历史聊天记录" in body or "新聊天" in body or "免费版" in body):
                        log(f"{prefix} detected existing ChatGPT login profile; clearing profile and retrying signup")
                        await session.__aexit__(None, None, None)
                        shutil.rmtree(profile_dir, ignore_errors=True)
                        session = BrowserSession(**session_kwargs)
                        await session.__aenter__()
                        page = await session.current_page()
                        flow = FreeBrowserFlow(page, prefix)
                        continue
                if nav_attempt >= 3 or not _is_network_navigation_error(exc):
                    raise
                log(f"{prefix} open signup failed ({nav_attempt}/3): {exc}; retrying...")
                await page.wait_for_timeout(3000 * nav_attempt)

        # 显式分支：密码页先创建密码；验证码页直接走验证码流程。
        stage = await flow.detect_post_email_stage(timeout_ms=12_000)
        if stage == "password":
            log(f"{prefix} detected password stage first; creating password before waiting email code")
            await flow.fill_password_if_shown(chatgpt_password)
        elif stage == "email_code":
            log(f"{prefix} detected email-code stage; continue with code flow")
        else:
            # 保底：未知页面状态时，仍尝试处理一次密码页，避免漏掉“先密码”分支。
            await flow.fill_password_if_shown(chatgpt_password)

        since = now_utc()
        bad_codes: set[str] = set()

        log(f"{prefix} waiting email code")
        try:
            email_code = await mail_provider.wait_code(account, since, bad_codes)
        except TimeoutError:
            resent = await flow.click_resend_code()
            if not resent:
                raise
            since = now_utc()
            email_code = await mail_provider.wait_code(account, since, bad_codes)

        for attempt in range(1, 4):
            try:
                await flow.enter_email_verification_code(email_code)
                break
            except RuntimeError as exc:
                if attempt >= 3:
                    raise
                bad_codes.add(email_code)
                resent = await flow.click_resend_code()
                if resent:
                    since = now_utc()
                await page.wait_for_timeout(1200)
                email_code = await mail_provider.wait_code(account, since, bad_codes)

        await page.wait_for_timeout(700)
        await flow.fill_password_if_shown(chatgpt_password)
        await page.wait_for_timeout(700)
        await flow.fill_about_you_and_submit(
            profile.full_name,
            profile.age,
            profile.birth_date,
            "[PayPal-Reg]",
            verification_code=email_code,
        )
        await flow.wait_until_url_leaves("about-you", timeout_ms=15000)

        log(f"{prefix} fetch accessToken")
        access_page_opened = False
        for url in ("https://chatgpt.com/", "https://chat.openai.com/"):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                access_page_opened = True
                break
            except Exception as exc:
                if not _is_network_navigation_error(exc):
                    raise
                log(f"{prefix} open {url} failed: {exc}")
                try:
                    await page.goto(url, wait_until="commit", timeout=20_000)
                    access_page_opened = True
                    log(f"{prefix} open {url} reached commit stage; continue fetching accessToken")
                    break
                except Exception as commit_exc:
                    log(f"{prefix} open {url} commit fallback failed: {commit_exc}")
                await page.wait_for_timeout(2000)
        if not access_page_opened:
            raise FreeRegisterError("failed to open ChatGPT page for accessToken")
        await page.wait_for_timeout(2000)
        chatgpt_session = await get_chatgpt_session(page)
        access_token = str(chatgpt_session.get("accessToken") or "")
        if not access_token:
            await page.wait_for_timeout(3000)
            chatgpt_session = await get_chatgpt_session(page)
            access_token = str(chatgpt_session.get("accessToken") or "")
        if not access_token:
            raise FreeRegisterError("failed to fetch accessToken")

        payment_link = ""
        if create_payment_link:
            env = load_env(".env")
            billing_country = env.get("PAYPAL_BILLING_COUNTRY") or "US"
            chatgpt_cfg = {**cfg["chatgpt"], "billing_country": billing_country, "currency": "USD"}
            payment_link = await create_plus_checkout_link(page, access_token, chatgpt_cfg)
        source_format = (
            "hotmail_graph"
            if account.client_id and account.refresh_token
            else ("icloud_query" if email.lower().endswith("@icloud.com") else ("qq_imap" if mail_source == "qq_imap" else "code_address"))
        )
        code_address = (account.code_address or account.mail_url or "").strip()
        session_record = session_export.extract_session_record(
            chatgpt_session,
            email=email,
            mail_source=mail_source,
            source_format=source_format,
            code_address=code_address,
            payment_link=payment_link,
            profile_dir=str(profile_dir),
            source=session_source,
        )
        cache_target = session_cache_path or PAYPAL_SESSION_CACHE_FILE
        cache_path = session_export.upsert_session_cache(session_record, path=cache_target)
        log(f"{prefix} session cached: {cache_path}")
        if create_payment_link:
            log(f"{prefix} link ok")
            return payment_link
        log(f"{prefix} session bootstrap ok")
        return ""

    except Exception as exc:
        if page is not None:
            try:
                current_url = page.url
                title = await page.title()
                body = await page.evaluate("() => (document.body?.innerText || '').slice(0, 1200)")
                log(f"{prefix} debug page: url={current_url} title={title!r} body={body!r}")
            except Exception:
                pass
        if flow is not None:
            try:
                await flow.screenshot("paypal_reg_failed.png")
            except Exception:
                pass
        log(f"{prefix} failed: {exc}")
        traceback.print_exc()
        return None
    finally:
        await session.__aexit__(None, None, None)


async def run_paypal_register(
    cfg: dict[str, Any],
    count: int = 1,
    workers: int = 1,
    selected_email: str | None = None,
) -> int:
    """Batch run flow-1 (register + payment link)."""
    env = load_env(".env")
    active_source = _active_mail_source(cfg)
    accounts_file = resolve_path(str(cfg.get("mail", {}).get("accounts_file") or ""))
    icloud_file = resolve_path(env.get("PAYPAL_ICLOUD_FILE") or "data/paypal/icloud_accounts.txt")
    done = already_in_link_pool()
    pending_accounts: list[MailAccount] = []

    if active_source in {"hotmail_graph", "moemail"}:
        pending_accounts = _load_accounts_from_file(accounts_file, done)
    elif active_source == "icloud_query":
        pending_accounts = _load_accounts_from_file(accounts_file, done)
        if not pending_accounts:
            # Backward-compatible fallback for old PayPal iCloud file.
            legacy_accounts = load_icloud_accounts(icloud_file)
            pending_accounts = [
                MailAccount(email=e, mail_url=q, raw=f"{e}----{q}")
                for e, q in legacy_accounts
                if e.lower() not in done
            ]
    else:
        pending_accounts = _load_accounts_from_file(accounts_file, done)

    if selected_email:
        before_count = len(pending_accounts)
        pending_accounts = filter_accounts_by_email(pending_accounts, selected_email)
        if not pending_accounts:
            log(
                f"PayPal flow1: selected email not found or already used: "
                f"{selected_email} | source={active_source} | pool_count={before_count}"
            )
            return 0

    if not pending_accounts:
        if active_source == "moemail" and _external_imap163_enabled(env):
            domain = _resolve_imap163_domain(env)
            if domain:
                pending_accounts = [
                    MailAccount(email=e, mail_url=q, raw=f"{e}----{q}")
                    for e, q in _generate_imap163_pending(count, done, domain)
                ]
                log(f"PayPal flow1: source={active_source}, pool empty, auto-generated {len(pending_accounts)} imap163 emails")
            else:
                log("PayPal flow1: source=moemail, pool empty and IMAP163_FORWARD_DOMAIN is not configured")
                return 0
        else:
            log(f"PayPal flow1: no pending accounts for source={active_source}")
            return 0

    use_proxy, proxy_file = resolve_register_proxy_settings(env)
    proxy_pool: ProxyPool | None = None
    if use_proxy:
        proxy_pool = ProxyPool(proxy_file)
        if proxy_pool.count() == 0:
            log(f"PayPal flow1: proxy is enabled but pool is empty: {proxy_file}")
            return 0
        log(f"PayPal flow1: proxy enabled, pool size={proxy_pool.count()}")
        # Preflight check first proxy to fail fast with actionable reason.
        first_proxy = proxy_pool.pick(1)
        if first_proxy:
            ok, reason = _probe_proxy(first_proxy)
            if not ok:
                log(f"PayPal flow1: proxy precheck failed: {reason}")
                log("PayPal flow1: cliproxy is required for JP region, stop this run")
                return 0
            else:
                log("PayPal flow1: proxy precheck passed")

    target = min(count, len(pending_accounts))
    log(f"PayPal flow1: source={active_source}, pending={len(pending_accounts)}, target={target}, workers={workers}")

    success = 0
    sem = asyncio.Semaphore(workers)

    async def worker(index: int, account: MailAccount) -> None:
        nonlocal success
        async with sem:
            proxy = proxy_pool.pick(index) if proxy_pool else None
            if proxy:
                log(f"[paypal-reg-{index:02d}] using proxy: {proxy}")
            link = await register_one(account, active_source, cfg, worker_id=index, proxy=proxy)
            if link:
                code_address = account.code_address or "mail"
                save_to_link_pool(account.email, code_address, link)
                # Register success consumes one mailbox account from selected source pool.
                if code_address != "imap163" and accounts_file and accounts_file.exists():
                    _remove_from_account_file(account.email, accounts_file)
                elif code_address != "imap163" and active_source == "icloud_query":
                    remove_from_icloud_file(account.email, icloud_file)
                success += 1

    tasks = [
        asyncio.create_task(worker(i + 1, account))
        for i, account in enumerate(pending_accounts[:target])
    ]
    await asyncio.gather(*tasks)
    log(f"PayPal flow1 done: success={success}/{target}")
    return success
