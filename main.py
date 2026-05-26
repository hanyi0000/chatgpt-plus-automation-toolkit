from __future__ import annotations

import argparse
import asyncio
import sys

import authorization_flow
import fill_billing_test
from modules import free_register
from modules import session_export
from modules.paypal_flow import interactive_paypal
from modules.env_settings import settings_panel
from modules.terminal_theme import install_print_theme
from modules.terminal_theme import BLUE, CYAN, GREEN, MAGENTA, YELLOW, paint
from modules.browser import BrowserSession
from modules.chatgpt_register import ChatGPTRegister, FatalAccountError, ManualInterventionNeeded
from modules.checkout import create_plus_checkout_link, get_chatgpt_session
from modules.mail_provider import MailProvider
from modules.moemail_factory import create_moemail_accounts, moemail_api_enabled, split_domains
from modules.proxy_pool import ProxyPool
from modules.storage import AccountStore
from modules.utils import (
    env_bool,
    load_config,
    load_env,
    log,
    migrate_known_output_files,
    now_utc,
    output_file,
    resolve_path,
    safe_filename,
)


install_print_theme()


def _display_width(s: str) -> int:
    import unicodedata
    w = 0
    for ch in s:
        if unicodedata.combining(ch):
            continue
        w += 2 if unicodedata.east_asian_width(ch) in {"F", "W"} else 1
    return w


def _pad(s: str, width: int) -> str:
    return s + " " * max(1, width - _display_width(s))


def ui_header(title: str, subtitle: str = "", width: int = 52) -> None:
    print()
    head = paint(title, MAGENTA, bold=True)
    if subtitle:
        head += "   " + paint(subtitle, CYAN)
    print(head)
    print(paint("=" * width, MAGENTA))


def ui_footer(width: int = 52) -> None:
    print(paint("=" * width, MAGENTA))


def ui_kv_row(label: str, value: str, label_width: int = 18) -> None:
    print(f"  {paint(_pad(label, label_width), CYAN)}{paint(value, GREEN, bold=True)}")


def ui_option(key: str, name: str, hint: str = "", *, name_width: int = 24, dim: bool = False) -> None:
    key_text = paint(f"  {key}.", GREEN, bold=True)
    name_color = YELLOW if dim else CYAN
    name_text = paint(name, name_color, bold=not dim)
    left = f"{key_text} {name_text}{' ' * max(1, name_width - _display_width(name))}"
    if hint:
        print(f"{left}{paint(hint, BLUE)}")
    else:
        print(left.rstrip())


def ui_prompt(text: str) -> str:
    return input(paint(f"> {text} ", GREEN, bold=True)).strip()


def ui_error(text: str) -> None:
    print(paint(f"  ! {text}", YELLOW))


def short_error(exc: Exception) -> str:
    text = str(exc).strip().replace("\r", "\n")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (lines[0] if lines else exc.__class__.__name__)[:500]


def worker_log(worker_id: int, message: str) -> None:
    log(f"[worker-{worker_id:02d}] {message}")


def account_prefix(worker_id: int, email: str) -> str:
    return f"[worker-{worker_id:02d}][{email}]"


class SuccessCounter:
    def __init__(self, target: int):
        self.target = target
        self.value = 0
        self.inflight = 0
        self._lock = asyncio.Lock()

    async def reached(self) -> bool:
        async with self._lock:
            return self.value >= self.target

    async def acquire_slot(self) -> bool:
        async with self._lock:
            if self.value >= self.target:
                return False
            if self.value + self.inflight >= self.target:
                return False
            self.inflight += 1
            return True

    async def release_slot(self, success: bool) -> int:
        async with self._lock:
            self.inflight = max(0, self.inflight - 1)
            if success and self.value < self.target:
                self.value += 1
            return self.value


def apply_env_config(cfg: dict, env_path: str = ".env", flow_key: str = "") -> dict:
    env = load_env(env_path)
    flow_source = env.get(f"{flow_key.upper()}_MAIL_SOURCE") if flow_key else ""
    mail_source = (flow_source or env.get("MAIL_SOURCE") or cfg.get("mail", {}).get("active_source") or "").strip().lower()
    if mail_source:
        configure_mail_source(cfg, mail_source)
    use_proxy = env_bool(env.get("USE_PROXY"), default=bool(cfg.get("browser", {}).get("use_proxy", False)))
    cfg.setdefault("browser", {})["use_proxy"] = use_proxy
    cfg["browser"]["proxy_file"] = env.get("PROXY_FILE") or cfg["browser"].get("proxy_file", "data/proxies/proxies.txt")
    cfg.setdefault("mail", {})["account_mode"] = env.get("MAIL_ACCOUNT_MODE") or cfg.get("mail", {}).get("account_mode", "pool")
    cfg["mail"]["moemail_base_url"] = env.get("MOEMAIL_BASE_URL") or cfg["mail"].get("moemail_base_url", "")
    cfg["mail"]["moemail_api_key"] = env.get("MOEMAIL_API_KEY") or cfg["mail"].get("moemail_api_key", "")
    cfg["mail"]["moemail_domains"] = env.get("MOEMAIL_DOMAIN_WHITELIST") or cfg["mail"].get("moemail_domains", "")
    cfg["mail"]["moemail_create_prefix"] = env.get("MOEMAIL_CREATE_PREFIX") or cfg["mail"].get("moemail_create_prefix", "openai")
    cfg["mail"]["moemail_create_mode"] = env.get("MOEMAIL_CREATE_MODE") or cfg["mail"].get("moemail_create_mode", "human")
    cfg.setdefault("gopay", {})["country_code"] = env.get("GOPAY_COUNTRY_CODE") or env.get("GOPAY_PHONE_COUNTRY_CODE") or cfg.get("gopay", {}).get("country_code", "+62")
    return cfg


def flow_mail_source(env: dict[str, str], flow_key: str) -> str:
    return (env.get(f"{flow_key.upper()}_MAIL_SOURCE") or env.get("MAIL_SOURCE") or "").strip().lower()


def choose_mail_source(cfg: dict, source_name: str | None) -> dict:
    if not source_name:
        return cfg
    configure_mail_source(cfg, source_name)
    return cfg


def configure_mail_source(cfg: dict, source_name: str) -> None:
    aliases = {
        "hotmail": "hotmail",
        "hotmail_graph": "hotmail",
        "moemail": "moemail",
        "icloud": "icloud_query",
        "icloud_query": "icloud_query",
        "qq": "qq",
        "qq_imap": "qq",
    }
    normalized = aliases.get(source_name)
    if not normalized:
        raise RuntimeError(f"MAIL_SOURCE 仅支持 moemail / hotmail / icloud_query / qq，当前值: {source_name}")
    sources = cfg.get("mail_sources") or {}
    source_cfg = sources.get(normalized)
    if normalized == "qq" and not source_cfg:
        current_mail = cfg.get("mail", {})
        source_cfg = {
            "source": "qq_imap",
            "accounts_file": "data/qq/accounts.txt",
            "raw_pool_file": "data/qq/mail_pool.txt",
            "code_timeout_sec": current_mail.get("code_timeout_sec", 150),
            "poll_interval_sec": current_mail.get("poll_interval_sec", 5),
        }
    if not source_cfg:
        raise RuntimeError(f"config.yaml 缂哄皯 mail_sources.{normalized}")
    cfg["mail"] = {**cfg.get("mail", {}), **source_cfg}
    cfg["mail"]["active_source"] = normalized


def prompt_mail_source(cfg: dict) -> str:
    sources = cfg.get("mail_sources") or {}
    available = []
    for key in ("moemail", "hotmail", "icloud_query", "qq"):
        if key == "qq" or sources.get(key):
            available.append(key)
    if not available:
        raise RuntimeError("config.yaml has no available mail_sources")
    current = (cfg.get("mail", {}).get("active_source") or cfg.get("mail", {}).get("source") or available[0]).strip()
    labels = {
        "icloud_query": "iCloud 查询邮箱",
        "hotmail": "微软邮箱 (Hotmail / Outlook)",
        "moemail": "自建邮箱池 (MoeMail)",
        "qq": "QQ 邮箱 (IMAP 授权码)",
    }
    ui_header("选择邮箱来源", f"当前: {current}")
    for index, key in enumerate(available, start=1):
        marker = paint("当前", GREEN, bold=True) if key == current else paint("可选", BLUE)
        ui_option(str(index), labels.get(key, key), marker, name_width=32)
    ui_footer()
    while True:
        choice = ui_prompt(f"请输入选项 [1-{len(available)}]，回车保持当前值:")
        if not choice and current in available:
            return current
        try:
            index = int(choice)
        except ValueError:
            ui_error("请输入有效数字")
            continue
        if 1 <= index <= len(available):
            return available[index - 1]
        ui_error("请输入有效选项")


def create_store(cfg: dict) -> AccountStore:
    mail_cfg = cfg["mail"]
    output_cfg = cfg["output"]
    return AccountStore(
        accounts_file=mail_cfg["accounts_file"],
        raw_pool_file=mail_cfg["raw_pool_file"],
        success_file=output_cfg["success_file"],
        failed_file=output_cfg["failed_file"],
        in_progress_file=output_cfg.get("in_progress_file", output_file("flow1_in_progress")),
    )


async def ensure_register_accounts(cfg: dict, store: AccountStore, desired_count: int) -> int:
    pending = store.pending_count()
    if pending >= desired_count:
        return pending
    env = load_env(".env")
    if not moemail_api_enabled(cfg, env):
        return pending
    mail_cfg = cfg.get("mail", {})
    base_url = (mail_cfg.get("moemail_base_url") or "").strip()
    api_key = (mail_cfg.get("moemail_api_key") or "").strip()
    if not base_url or not api_key:
        log("MoeMail API 已启用，但 MOEMAIL_BASE_URL / MOEMAIL_API_KEY 未配置，跳过自动创建邮箱")
        return pending
    need = desired_count - pending
    domains = split_domains(mail_cfg.get("moemail_domains"))
    log(f"MoeMail API 自动补号: 当前={pending}, 目标={desired_count}, 准备创建={need}, 域名={','.join(domains)}")
    created = await create_moemail_accounts(
        base_url=base_url,
        api_key=api_key,
        count=need,
        domains=domains,
        prefix=mail_cfg.get("moemail_create_prefix", "openai"),
        mode=mail_cfg.get("moemail_create_mode", "human"),
        expiry_time_ms=0,
    )
    if created:
        existing_text = store.accounts_file.read_text(encoding="utf-8") if store.accounts_file.exists() else ""
        raw_text = store.raw_pool_file.read_text(encoding="utf-8") if store.raw_pool_file.exists() else ""
        with store.accounts_file.open("a", encoding="utf-8") as accounts_fh, store.raw_pool_file.open("a", encoding="utf-8") as raw_fh:
            for item in created:
                if item.email.lower() not in existing_text.lower():
                    accounts_fh.write(item.mail_line.rstrip() + "\n")
                if item.email.lower() not in raw_text.lower():
                    raw_fh.write(item.mail_line.rstrip() + "\n")
        log(f"MoeMail API 自动补号完成: 新增={len(created)}")
    return store.pending_count()


def create_proxy_pool(cfg: dict) -> ProxyPool | None:
    browser_cfg = cfg.get("browser", {})
    if not browser_cfg.get("use_proxy"):
        return None
    proxy_file = browser_cfg.get("proxy_file", "data/proxies/proxies.txt")
    proxy_pool = ProxyPool(proxy_file)
    if proxy_pool.count() <= 0:
        raise RuntimeError(f"USE_PROXY 已启用，但代理池为空: {proxy_file}")
    return proxy_pool


def display_proxy(proxy: str | None) -> str:
    if not proxy:
        return "not-set"
    text = proxy.strip()
    if "@" in text:
        prefix, suffix = text.rsplit("@", 1)
        scheme = prefix.split("://", 1)[0] + "://" if "://" in prefix else ""
        return f"{scheme}***:***@{suffix}"
    return text


def make_sms_args(args: argparse.Namespace | None = None) -> argparse.Namespace:
    args = args or argparse.Namespace()
    return argparse.Namespace(
        country=getattr(args, "country", "") or "",
        sms_provider=getattr(args, "sms_provider", "") or "",
        hero_sms_api_key="",
        hero_sms_service="",
        hero_sms_country_top_n=None,
        hero_sms_operator_threshold=None,
        grizzly_api_key="",
        grizzly_service="",
        grizzly_country_top_n=None,
        grizzly_provider_threshold=None,
    )


def resolve_flow_sms_selection(args: argparse.Namespace | None = None, *, flow_label: str, flow_key: str) -> dict[str, object] | None:
    return authorization_flow.resolve_authorization_sms_selection(make_sms_args(args), flow_label=flow_label, flow_key=flow_key)


def resolve_flow1_sms_selection(args: argparse.Namespace | None = None) -> dict[str, object] | None:
    return resolve_flow_sms_selection(args, flow_label="流程一", flow_key="FLOW1")


def resolve_free_sms_selection(args: argparse.Namespace | None = None) -> dict[str, object] | None:
    return resolve_flow_sms_selection(args, flow_label="Free 注册", flow_key="FREE")


async def run_account(
    cfg: dict,
    store: AccountStore,
    worker_id: int,
    proxy: str | None = None,
    sms_selection: dict[str, object] | None = None,
) -> bool | None:
    account = store.claim_next(worker_id)
    if not account:
        worker_log(worker_id, "accounts.txt has no pending account")
        return None
    prefix = account_prefix(worker_id, account.email)
    log(f"{prefix} start processing account")
    mail_cfg = cfg["mail"]
    mail_provider = MailProvider(
        source=mail_cfg["source"],
        timeout_sec=int(mail_cfg.get("code_timeout_sec", 150)),
        poll_interval_sec=int(mail_cfg.get("poll_interval_sec", 5)),
        log_prefix=prefix,
    )
    browser_cfg = cfg["browser"]
    log(
        f"{prefix} 浏览器配置: headless={bool(browser_cfg.get('headless', False))}, "
        f"proxy={display_proxy(proxy)}"
    )
    profile_dir = resolve_path("profiles") / safe_filename(account.email)
    since = now_utc()
    session = BrowserSession(
        profile_dir=profile_dir,
        headless=bool(browser_cfg.get("headless", False)),
        slow_mo=int(browser_cfg.get("slow_mo", 80)),
        timeout_ms=int(browser_cfg.get("timeout_ms", 60000)),
        proxy=proxy,
        fingerprint_seed=account.email,
    )
    try:
        await session.__aenter__()
        assert session.page is not None
        register = ChatGPTRegister(
            page=session.page,
            page_getter=session.current_page,
            start_url=cfg["chatgpt"]["start_url"],
            entry_action=cfg["chatgpt"].get("entry_action", "signup"),
            mail_provider=mail_provider,
            age_min=int(cfg["register_profile"]["age_min"]),
            age_max=int(cfg["register_profile"]["age_max"]),
            sms_selection=sms_selection,
            log_prefix=prefix,
        )
        await register.run_until_logged_in(account, since)
        page = await session.current_page()
        if "chatgpt.com" not in page.url:
            await page.goto("https://chatgpt.com/", wait_until="domcontentloaded")
        log(f"{prefix} 已登录，开始临时获取 accessToken")
        page = await session.current_page()
        chatgpt_session = await get_chatgpt_session(page)
        access_token = str(chatgpt_session.get("accessToken") or "")
        if not access_token:
            raise RuntimeError("无法获取 accessToken，当前页面可能未登录 ChatGPT")
        log(f"{prefix} accessToken acquired, generating Plus checkout link")
        payment_link = await create_plus_checkout_link(page, access_token, cfg["chatgpt"])
        cache_record = session_export.extract_session_record(
            chatgpt_session,
            email=account.email,
            mail_source=cfg.get("mail", {}).get("active_source", cfg.get("mail", {}).get("source", "")),
            source_format=(
                "hotmail_graph"
                if account.client_id and account.refresh_token
                else (
                    "icloud_query"
                    if account.email.lower().endswith("@icloud.com")
                    else ("qq_imap" if cfg.get("mail", {}).get("source") == "qq_imap" else "code_address")
                )
            ),
            code_address=account.code_address,
            payment_link=payment_link,
            profile_dir=str(profile_dir),
            source="main_flow1",
        )
        cache_path = session_export.upsert_session_cache(cache_record)
        log(f"{prefix} 已缓存流程四 Session: {cache_path}")
        await session.__aexit__(None, None, None)
        session = None
        store.save_success(account.email, account.code_address, payment_link)
        store.complete(account.email)
        log(f"{prefix} 成功，已写入 {output_file('flow1_success')}")
        print()
        print(f"{account.email}----{account.code_address}----{payment_link}")
        return True
    except FatalAccountError as exc:
        await save_failure_artifacts(prefix, account.email, session)
        if session:
            await session.__aexit__(type(exc), exc, exc.__traceback__)
        store.save_failed(account.email, short_error(exc))
        store.return_to_pool(account)
        log(f"{prefix} fatal error, account returned to pool (not removed): {exc}")
        return False
    except ManualInterventionNeeded as exc:
        await save_failure_artifacts(prefix, account.email, session)
        if session:
            await session.__aexit__(type(exc), exc, exc.__traceback__)
        store.save_failed(account.email, short_error(exc))
        store.return_to_pool(account)
        log(f"{prefix} 需要人工处理，页面识别失败，账号已退回号池: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001
        await save_failure_artifacts(prefix, account.email, session)
        if session:
            await session.__aexit__(type(exc), exc, exc.__traceback__)
        store.save_failed(account.email, short_error(exc))
        store.return_to_pool(account)
        log(f"{prefix} 普通失败，账号已退回号池: {short_error(exc)}")
        return False


async def save_failure_artifacts(prefix: str, email: str, session: object | None) -> None:
    if session is None:
        return
    try:
        page = await session.current_page()
        out = resolve_path("output/gopay_register_plus/debug") / safe_filename(email)
        out.mkdir(parents=True, exist_ok=True)
        html = await page.content()
        (out / "last_page.html").write_text(html, encoding="utf-8")
        (out / "last_url.txt").write_text(page.url, encoding="utf-8")
        await page.screenshot(path=str(out / "last_page.png"), full_page=True)
        log(f"{prefix} 已保存失败现场: {out}")
    except Exception:
        pass


async def run_once(config_path: str) -> int:
    cfg = apply_env_config(load_config(config_path), flow_key="FLOW1")
    store = create_store(cfg)
    sms_selection = resolve_flow1_sms_selection()
    result = await run_account(cfg, store, worker_id=1, sms_selection=sms_selection)
    return 0 if result is not False else 1


async def worker_loop(
    cfg: dict,
    store: AccountStore,
    worker_id: int,
    counter: SuccessCounter,
    proxy_pool: ProxyPool | None,
    sms_selection: dict[str, object] | None,
) -> None:
    while True:
        if not await counter.acquire_slot():
            return
        proxy = proxy_pool.pick(worker_id) if proxy_pool else None
        result = await run_account(cfg, store, worker_id, proxy=proxy, sms_selection=sms_selection)
        if result is None:
            await counter.release_slot(success=False)
            return
        total = await counter.release_slot(success=result is True)
        if result is True:
            worker_log(worker_id, f"当前成功数 {total}/{counter.target}")


async def run_many(config_path: str, workers: int, count: int) -> int:
    cfg = apply_env_config(load_config(config_path), flow_key="FLOW1")
    sms_selection = resolve_flow1_sms_selection()
    return await run_many_with_config(cfg, workers, count, sms_selection=sms_selection)


def ask_positive_int(prompt: str, default: int, max_value: int | None = None) -> int:
    hint = f"默认 {default}"
    if max_value is not None:
        hint += f"，最大 {max_value}"
    while True:
        raw = input(paint(f"> {prompt} ", GREEN, bold=True) + paint(f"[{hint}] ", BLUE)).strip()
        if not raw:
            value = default
        else:
            try:
                value = int(raw)
            except ValueError:
                ui_error("请输入数字")
                continue
        if value < 1:
            ui_error("请输入大于 0 的数字")
            continue
        if max_value is not None and value > max_value:
            ui_error(f"超过上限，最大允许 {max_value}")
            continue
        return value


def interactive_main(config_path: str) -> int:
    cfg = apply_env_config(load_config(config_path), flow_key="FLOW1")
    store = create_store(cfg)
    pending = store.pending_count()
    api_mode = moemail_api_enabled(cfg, load_env(".env"))
    mail_source = cfg.get("mail", {}).get("active_source", cfg.get("mail", {}).get("source", "unknown"))
    ui_header("流程一", "注册账号并生成长链接")
    ui_kv_row("邮箱来源", str(mail_source))
    ui_kv_row("号池剩余", str(pending))
    ui_kv_row("MoeMail 自动补号", "启用" if api_mode else "关闭")
    ui_footer()
    if pending <= 0 and not api_mode:
        ui_error("accounts.txt 没有可用账号")
        return 0
    max_count = None if api_mode else pending
    count = ask_positive_int("本次要成功生成几个长链接", default=1, max_value=max_count)
    pending = asyncio.run(ensure_register_accounts(cfg, store, count))
    if pending <= 0:
        ui_error("没有可处理账号，且自动创建邮箱失败")
        return 1
    if pending < count:
        ui_error(f"当前最多只能处理 {pending} 个账号，目标已自动调整为 {pending}")
        count = pending
    workers = ask_positive_int("并发数", default=1, max_value=count)
    sms_selection = resolve_flow1_sms_selection()
    print()
    return asyncio.run(run_many_with_config(cfg, workers=workers, count=count, sms_selection=sms_selection))


def make_gopay_args(config_path: str) -> argparse.Namespace:
    cfg = load_config(config_path)
    env = load_env(".env")
    flow2_defaults = fill_billing_test.flow2_fast_defaults(env)
    country_code = (
        env.get("GOPAY_COUNTRY_CODE")
        or env.get("GOPAY_PHONE_COUNTRY_CODE")
        or cfg.get("gopay", {}).get("country_code")
        or "+62"
    )
    return argparse.Namespace(
        success_file=output_file("flow1_success"),
        index=None,
        account=None,
        config=config_path,
        headless=None,
        keep_open=False,
        submit=True,
        stop_at_phone=False,
        phone=None,
        pin=env.get("GOPAY_PIN") or env.get("GOPAY_PAYMENT_PIN") or "",
        country_code=country_code,
        billing_retries=flow2_defaults["billing_retries"],
        wait_otp=True,
        prompt_otp=False,
        prompt_pin=False,
        otp_timeout=flow2_defaults["otp_timeout"],
        retry_technical_error=True,
        retry_interval=flow2_defaults["retry_interval"],
        retry_timeout=flow2_defaults["retry_timeout"],
        force_country_dom=False,
        use_linking_api=True,
        use_proxy=None,
        proxy_file=None,
        proxy=None,
        wait_manual_success=True,
        manual_success_timeout=flow2_defaults["manual_success_timeout"],
        paid_output=output_file("flow2_paid_success"),
        remove_paid_source=True,
        nonzero_output=output_file("flow2_nonzero_billing"),
        remove_nonzero_source=False,
        continue_after_nonzero=False,
        batch=True,
    )


def interactive_gopay_plus(config_path: str, cfg: dict) -> int:
    while True:
        ui_header("GoPay 注册 Plus", "选择子功能")
        sub_items = [
            ("1", "注册并生成链接", "流程一"),
            ("2", "GoPay 支付长链接", "流程二"),
            ("3", "OAuth 授权", "流程三"),
            ("4", "Session 导出", "流程四"),
            ("5", "返回上级菜单", ""),
        ]
        for key, name, hint in sub_items:
            ui_option(key, name, hint, dim=(key == "5"))
        ui_footer()
        choice = ui_prompt("请输入选项 [1-5]:")
        if choice == "1":
            result = interactive_main(config_path)
            if should_return_to_menu():
                continue
            return result
        if choice == "2":
            result = fill_billing_test.interactive_batch(make_gopay_args(config_path))
            if should_return_to_menu():
                continue
            return result
        if choice == "3":
            result = authorization_flow.interactive_authorize()
            if should_return_to_menu():
                continue
            return result
        if choice == "4":
            result = session_export.interactive_session_export()
            if should_return_to_menu():
                continue
            return result
        if choice in {"0", "5"}:
            return -1  # sentinel to indicate "back to main menu"
        ui_error("请输入 1 到 5 之间的数字")


def interactive_menu(config_path: str) -> int:
    cfg = apply_env_config(load_config(config_path))
    if len((cfg.get("mail_sources") or {})) > 1:
        chosen_source = prompt_mail_source(cfg)
        choose_mail_source(cfg, chosen_source)
    while True:
        print_home_menu(cfg)
        choice = ui_prompt("请输入选项 [1-5]:")
        if choice == "1":
            sms_selection = resolve_free_sms_selection()
            result = free_register.interactive_free_register(config_path, cfg, sms_selection, ask_positive_int)
            if should_return_to_menu():
                continue
            return result
        if choice == "2":
            result = interactive_gopay_plus(config_path, cfg)
            if result == -1:
                continue
            if should_return_to_menu():
                continue
            return result
        if choice == "3":
            result = interactive_paypal(config_path, cfg)
            if should_return_to_menu():
                continue
            return result
        if choice == "4":
            settings_panel(".env")
            cfg = apply_env_config(load_config(config_path))
            continue
        if choice in {"0", "5"}:
            return 0
        ui_error("请输入 1 到 5 之间的数字")


def print_home_menu(cfg: dict) -> None:
    ui_header("ChatGPT Assistant", "选择功能")
    menu_items = [
        ("1", "Free 注册", "注册 + 授权 + 导出"),
        ("2", "GoPay 注册 Plus", "注册 + 支付 + 授权 + 导出"),
        ("3", "PayPal Plus", "注册 + 支付 + 授权"),
        ("4", "设置", "邮箱源 / 接码 / 设备绑定"),
        ("5", "退出", ""),
    ]
    for key, name, hint in menu_items:
        ui_option(key, name, hint, dim=(key in {"4", "5"}))
    ui_footer()
    print(paint("  作者：hanyiz2", BLUE, bold=True))


def should_return_to_menu() -> bool:
    while True:
        ui_header("任务完成", "选择下一步")
        ui_option("1", "返回上一界面", "继续操作")
        ui_option("2", "退出脚本", "", dim=True)
        ui_footer()
        choice = ui_prompt("请输入选项 [1/2]，回车默认 1:") or "1"
        if choice == "1":
            return True
        if choice in {"0", "2"}:
            return False
        ui_error("请输入 1 或 2")


async def run_many_with_config(cfg: dict, workers: int, count: int, sms_selection: dict[str, object] | None = None) -> int:
    store = create_store(cfg)
    await ensure_register_accounts(cfg, store, count)
    counter = SuccessCounter(count)
    worker_count = max(1, workers)
    target_count = max(1, count)
    counter.target = target_count
    proxy_pool = create_proxy_pool(cfg)
    proxy_status = f"enabled, proxy_count={proxy_pool.count()}" if proxy_pool else "disabled"
    if proxy_pool and proxy_pool.count() <= 0:
        raise RuntimeError(f"USE_PROXY 已启用，但代理池为空: {cfg.get('browser', {}).get('proxy_file')}")
    log(
        f"启动多线程: workers={worker_count}, 目标成功数={target_count}, "
        f"邮箱来源={cfg.get('mail', {}).get('active_source', cfg.get('mail', {}).get('source'))}, 代理={proxy_status}"
    )
    if sms_selection:
        country = sms_selection.get("country")
        operator = sms_selection.get("operator")
        operator_label = getattr(operator, "label", "") if operator else ""
        log(
            "流程一/手机接码: "
            f"平台={sms_selection.get('provider_label')}, "
            f"country={getattr(country, 'name', '-')}(+{getattr(country, 'dial_code', '-')})"
            f"operator={operator_label or 'any'}"
        )
    else:
        log("流程一/手机接码: 未启用或未配置，遇到手机号必填仍按原逻辑弃置账号")
    tasks = [
        asyncio.create_task(worker_loop(cfg, store, worker_id, counter, proxy_pool, sms_selection))
        for worker_id in range(1, worker_count + 1)
    ]
    await asyncio.gather(*tasks)
    log(f"多线程结束，成功数 {counter.value}/{counter.target}")
    return 0 if counter.value >= counter.target else 1


def main() -> int:
    migrate_known_output_files()
    parser = argparse.ArgumentParser(description="ChatGPT 注册 / GoPay / 授权 / Session 导出 / Free 注册工具")
    parser.add_argument(
        "--mode",
        choices=["register", "gopay", "authorize", "session-export", "free-register"],
        help="功能模式: register / gopay / authorize / session-export / free-register",
    )
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    parser.add_argument("--workers", type=int, help="并发浏览器 worker 数")
    parser.add_argument("--count", type=int, help="目标成功数量")
    parser.add_argument("--country", default="", help="流程二/三接码国家: 序号 / ISO / 平台国家 ID，例如 US")
    parser.add_argument("--sms-provider", default="", help="流程二/三接码平台: herosms / grizzly")
    parser.add_argument("--mail-source", choices=["moemail", "hotmail", "hotmail_graph", "icloud", "icloud_query", "qq", "qq_imap"], help="邮箱来源: moemail / hotmail / icloud_query / qq")
    parser.add_argument("--register-mode", choices=["phone", "email"], default="phone", help="Free 注册模式: phone(默认) / email")
    args = parser.parse_args()
    if args.mode == "gopay":
        return fill_billing_test.interactive_batch(make_gopay_args(args.config))
    if args.mode == "authorize":
        return authorization_flow.interactive_authorize(
            argparse.Namespace(
                count=args.count,
                workers=args.workers,
                paid_file=output_file("flow2_paid_success"),
                country=args.country,
                sms_provider=args.sms_provider,
                hero_sms_api_key="",
                hero_sms_service="",
                hero_sms_country_top_n=None,
                hero_sms_operator_threshold=None,
                grizzly_api_key="",
                grizzly_service="",
                grizzly_country_top_n=None,
                grizzly_provider_threshold=None,
            )
        )
    if args.mode == "session-export":
        return session_export.interactive_session_export()
    if args.mode == "free-register":
        cfg = apply_env_config(load_config(args.config), flow_key="FREE")
        sms_selection = resolve_free_sms_selection(args)
        return asyncio.run(
            free_register.run_free_register_many(
                cfg,
                count=args.count or 1,
                workers=args.workers or 1,
                sms_selection=sms_selection,
                register_mode=getattr(args, "register_mode", "phone") or "phone",
            )
        )
    if args.mode is None and args.workers is None and args.count is None and len(sys.argv) <= 1:
        return interactive_menu(args.config)
    cfg = apply_env_config(load_config(args.config), flow_key="FLOW1")
    choose_mail_source(cfg, args.mail_source)
    sms_selection = resolve_flow1_sms_selection(args)
    workers = args.workers or 1
    count = args.count or 1
    if workers <= 1 and count <= 1:
        store = create_store(cfg)
        proxy_pool = create_proxy_pool(cfg)
        result = asyncio.run(
            run_account(
                cfg,
                store,
                worker_id=1,
                proxy=proxy_pool.pick(1) if proxy_pool else None,
                sms_selection=sms_selection,
            )
        )
        return 0 if result is not False else 1
    return asyncio.run(run_many_with_config(cfg, workers, count, sms_selection=sms_selection))


if __name__ == "__main__":
    raise SystemExit(main())

