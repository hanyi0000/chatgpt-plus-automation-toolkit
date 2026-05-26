"""PayPal Plus 主编排：菜单 + 三个子流程入口。"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
from typing import Any

from .storage import MailAccount
from .utils import load_config, load_env, log, resolve_path
from .paypal_register import LINK_POOL_FILE, register_one, resolve_register_proxy_settings, run_paypal_register
from .paypal_pay import run_paypal_pay, PENDING_AUTH_FILE, PAYPAL_OUTPUT_ROOT, is_local_random_card_mode
from .paypal_card_pool import CardPool
from .paypal_card_redeem import ensure_card_supply
from .paypal_phone_pool import PhonePool
from .proxy_pool import ProxyPool
from .storage import parse_mail_line

PAYPAL_SESSIOND_DIR = PAYPAL_OUTPUT_ROOT / "sessiond"
PAYPAL_SESSION_CACHE_FILE = PAYPAL_SESSIOND_DIR / "session_cache.jsonl"


def _active_mail_source(cfg: dict[str, Any]) -> str:
    mail_cfg = cfg.get("mail", {})
    return _normalize_mail_source(str(mail_cfg.get("active_source") or mail_cfg.get("source") or "moemail"))


def _build_account_lookup(cfg: dict[str, Any]) -> dict[str, MailAccount]:
    out: dict[str, MailAccount] = {}
    mail_cfg = cfg.get("mail", {})
    accounts_file = str(mail_cfg.get("accounts_file") or "")
    if not accounts_file:
        return out
    path = resolve_path(accounts_file)
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        account = parse_mail_line(line)
        if not account:
            continue
        out[account.email.strip().lower()] = account
    return out


def _mail_account_from_pending(record: dict[str, str], lookup: dict[str, MailAccount]) -> MailAccount:
    email = str(record.get("account") or "").strip()
    fallback = lookup.get(email.lower())
    password = str(record.get("password") or "").strip() or (fallback.password if fallback else None)
    client_id = str(record.get("client_id") or "").strip() or (fallback.client_id if fallback else None)
    refresh_token = str(record.get("refresh_token") or "").strip() or (fallback.refresh_token if fallback else None)
    code_address = str(record.get("code_address") or "").strip() or (fallback.mail_url if fallback else "") or email
    # mail_url 使用 code_address 可复用旧版接码适配器（含 email----email 历史格式）
    mail_url = code_address
    return MailAccount(
        email=email,
        password=password or None,
        client_id=client_id or None,
        refresh_token=refresh_token or None,
        mail_url=mail_url,
        raw=record.get("raw", ""),
    )


async def _hydrate_session_via_flow1_login(
    record: dict[str, str],
    *,
    cfg: dict[str, Any],
    cache_path: str,
    lookup: dict[str, MailAccount],
    index: int,
    total: int,
    proxy: str | None = None,
) -> bool:
    email = str(record.get("account") or "").strip()
    prefix = f"[paypal-sessiond-{index:02d}][{email}]"
    account = _mail_account_from_pending(record, lookup)
    mail_source = _active_mail_source(cfg)
    try:
        log(f"{prefix} 开始补录 session ({index}/{total})")
        if proxy:
            log(f"{prefix} 使用流程1代理补录 session")
        link = await register_one(
            account,
            mail_source,
            cfg,
            worker_id=index,
            proxy=proxy,
            create_payment_link=False,
            session_cache_path=cache_path,
            session_source="paypal_flow3_flow1_login_bootstrap",
        )
        return link is not None
    except Exception as exc:  # noqa: BLE001
        log(f"{prefix} session 补录失败: {exc}")
        return False


def _count_lines(path: str) -> int:
    p = resolve_path(path) if isinstance(path, str) else path
    if not p.exists():
        return 0
    return sum(1 for l in p.read_text(encoding="utf-8").splitlines() if l.strip() and not l.strip().startswith("#"))


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


def _mail_source_label(source: str) -> str:
    labels = {
        "moemail": "自建邮箱池 (MoeMail)",
        "hotmail_graph": "微软邮箱 (Hotmail / Outlook)",
        "icloud_query": "iCloud 查询邮箱",
        "qq_imap": "QQ 邮箱 (IMAP 授权码)",
    }
    return labels.get(source, source)


def _count_accounts_file(path: str) -> int:
    p = resolve_path(path)
    if not p.exists():
        return 0
    count = 0
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if parse_mail_line(line):
            count += 1
    return count


def _pending_records(path: str) -> list[dict[str, str]]:
    p = resolve_path(path)
    if not p.exists():
        return []
    seen: set[str] = set()
    rows: list[dict[str, str]] = []
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        text = line.strip().lstrip("\ufeff\u200b\u2060")
        if not text or text.startswith("#") or "----" not in text:
            continue
        parts = [part.strip() for part in text.split("----", 3)]
        if len(parts) < 2:
            continue
        email = parts[0]
        if not re.fullmatch(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", email):
            continue
        key = email.lower()
        if key in seen:
            continue
        seen.add(key)
        rows.append({"account": email, "code_address": parts[1]})
    return rows


def _authorized_emails(output_root: str) -> set[str]:
    root = resolve_path(output_root)
    out: set[str] = set()

    rt_file = root / "account-rt.txt"
    if rt_file.exists():
        for line in rt_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            email = line.split("----", 1)[0].strip()
            if "@" in email:
                out.add(email.lower())

    token_dir = root / "tokens"
    if token_dir.exists():
        for token_file in token_dir.glob("*.json"):
            m = re.search(r"([a-zA-Z0-9_.+-]+@[a-zA-Z0-9_.-]+)", token_file.name)
            if m:
                out.add(m.group(1).lower())

    sub_file = root / "sub2api_accounts.json"
    if sub_file.exists():
        try:
            data = json.loads(sub_file.read_text(encoding="utf-8"))
            for item in data.get("accounts", []):
                email = (
                    item.get("name")
                    or item.get("extra", {}).get("email")
                    or item.get("credentials", {}).get("email")
                )
                if isinstance(email, str) and "@" in email:
                    out.add(email.lower())
        except Exception:
            pass
    return out


def _count_authorizable_pending(pending_file: str, auth_output_root: str) -> int:
    pending = _pending_records(pending_file)
    if not pending:
        return 0
    done = _authorized_emails(auth_output_root)
    return sum(1 for row in pending if row["account"].lower() not in done)


def interactive_paypal(config_path: str = "config.yaml", cfg: dict[str, Any] | None = None) -> int:
    """PayPal Plus 交互菜单。"""
    from main import apply_env_config, ask_positive_int, ui_header, ui_footer, ui_option, ui_prompt, ui_error, ui_kv_row

    cfg = cfg or apply_env_config(load_config(config_path))
    env = load_env(".env")

    while True:
        cards_file = env.get("PAYPAL_CARDS_FILE") or "data/paypal/cards.txt"
        phones_file = env.get("PAYPAL_PHONES_FILE") or "data/paypal/phones.txt"
        mail_cfg = cfg.get("mail", {})
        active_source = _normalize_mail_source(str(mail_cfg.get("active_source") or mail_cfg.get("source") or "moemail"))
        accounts_file = str(mail_cfg.get("accounts_file") or "")
        source_pool_count = _count_accounts_file(accounts_file) if accounts_file else 0

        link_count = _count_lines(str(LINK_POOL_FILE))
        pending_count = _count_authorizable_pending(str(PENDING_AUTH_FILE), str(PAYPAL_OUTPUT_ROOT / "授权成功"))
        card_count = CardPool(cards_file).count()
        local_random_mode = is_local_random_card_mode(env)
        phone_pool = PhonePool(phones_file, max_uses=int(env.get("PAYPAL_PHONE_MAX_USES") or 5))

        ui_header("PayPal Plus", "注册 + 支付 + 授权")
        ui_kv_row("当前邮箱源", _mail_source_label(active_source))
        ui_kv_row("邮箱池可用", str(source_pool_count))
        ui_kv_row("长链接待支付", str(link_count))
        ui_kv_row("待授权账号", str(pending_count))
        ui_kv_row("虚拟卡", "本地随机" if local_random_mode else str(card_count))
        ui_kv_row("手机号可用", str(phone_pool.count()))
        ui_footer()

        ui_option("1", "生成长链接", "按当前邮箱源注册 + 获取 accessToken（日本代理）")
        ui_option("2", "流程2 真实卡支付", "长链接 -> PayPal 绑真实卡（美国代理）")
        ui_option("3", "流程2 无卡支付", "长链接 -> PayPal 绑本地随机卡（无真实卡）")
        ui_option("4", "授权落盘", "待授权 -> OAuth -> token")
        ui_option("5", "全自动（真实卡）", "按顺序运行 1 -> 2(真实卡) -> 3")
        ui_option("6", "全自动（无卡）", "按顺序运行 1 -> 2(本地随机卡) -> 3")
        ui_option("7", "返回", "", dim=True)
        ui_footer()

        choice = ui_prompt("请输入选项 [1-7]:")

        if choice == "1":
            max_count = max(1, source_pool_count)
            count = ask_positive_int("生成几个长链接", default=1, max_value=max_count)
            workers = ask_positive_int("并发数", default=1, max_value=count)
            asyncio.run(run_paypal_register(cfg, count=count, workers=workers))

        elif choice == "2":
            if link_count <= 0:
                ui_error("长链接池为空，请先运行流程1")
                continue
            if phone_pool.count() <= 0:
                ui_error("手机号池为空，请在 data/paypal/phones.txt 添加手机号")
                continue

            desired_cards = min(link_count, phone_pool.count())
            ensure_card_supply(env, desired_cards, log_prefix="PayPal 流程2")
            card_count = CardPool(cards_file).count()
            if card_count <= 0:
                ui_error("卡池为空，且自动兑换未获取到可用卡，请检查 data/paypal/card_codes.txt 与兑换接口配置")
                continue
            max_count = min(link_count, card_count, phone_pool.count())
            count = ask_positive_int("支付几个", default=1, max_value=max_count)
            workers = ask_positive_int("并发数", default=1, max_value=min(count, phone_pool.count()))
            asyncio.run(run_paypal_pay(cfg, count=count, workers=workers, card_source_mode="real"))

        elif choice == "3":
            if link_count <= 0:
                ui_error("长链接池为空，请先运行流程1")
                continue
            if phone_pool.count() <= 0:
                ui_error("手机号池为空，请在 data/paypal/phones.txt 添加手机号")
                continue

            max_count = min(link_count, phone_pool.count())
            count = ask_positive_int("无卡支付几个", default=1, max_value=max_count)
            workers = ask_positive_int("并发数", default=1, max_value=min(count, phone_pool.count()))
            asyncio.run(run_paypal_pay(cfg, count=count, workers=workers, card_source_mode="local_random"))

        elif choice == "4":
            if pending_count <= 0:
                ui_error("待授权池为空，请先运行前置流程")
                continue
            return _run_paypal_authorize()

        elif choice in {"5", "6"}:
            use_local_random_mode = choice == "6"
            if phone_pool.count() <= 0:
                ui_error("手机号池为空，请在 data/paypal/phones.txt 添加手机号")
                continue

            if source_pool_count > 0:
                max_count = max(1, min(source_pool_count, phone_pool.count()))
            else:
                max_count = max(1, phone_pool.count())
            count = ask_positive_int("全自动处理几个", default=1, max_value=max_count)
            workers_reg = ask_positive_int("流程1并发数", default=1, max_value=count)
            workers_pay = ask_positive_int("流程2并发数", default=1, max_value=min(count, phone_pool.count()))
            workers_auth = ask_positive_int("流程3并发数", default=1, max_value=count)

            # 流程1：生成长链接
            reg_success = asyncio.run(run_paypal_register(cfg, count=count, workers=workers_reg))
            if reg_success <= 0:
                ui_error("流程1未生成可用长链接，全自动已停止")
                continue

            # 刷新资源计数
            link_count_after = _count_lines(str(LINK_POOL_FILE))
            phone_count_after = PhonePool(phones_file, max_uses=int(env.get("PAYPAL_PHONE_MAX_USES") or 5)).count()
            if link_count_after <= 0:
                ui_error("流程1后长链接池仍为空，全自动已停止")
                continue
            if phone_count_after <= 0:
                ui_error("流程2前手机号池为空，全自动已停止")
                continue

            # 流程2：PayPal 支付
            if not use_local_random_mode:
                desired_cards = min(link_count_after, phone_count_after, count)
                ensure_card_supply(env, desired_cards, log_prefix="PayPal 流程2")
                card_count_after = CardPool(cards_file).count()
                if card_count_after <= 0:
                    ui_error("流程2前卡池为空，且自动兑换未获取到可用卡，全自动已停止")
                    continue
                pay_target = min(count, link_count_after, card_count_after, phone_count_after)
            else:
                pay_target = min(count, link_count_after, phone_count_after)
            pay_workers = min(workers_pay, pay_target, phone_count_after)
            pay_mode = "local_random" if use_local_random_mode else "real"
            pay_success = asyncio.run(
                run_paypal_pay(cfg, count=pay_target, workers=max(1, pay_workers), card_source_mode=pay_mode)
            )
            if pay_success <= 0:
                ui_error("流程2未产生待授权账号，全自动已停止")
                continue

            # 流程3：授权落盘
            pending_after = _count_authorizable_pending(str(PENDING_AUTH_FILE), str(PAYPAL_OUTPUT_ROOT / "授权成功"))
            if pending_after <= 0:
                ui_error("流程2后待授权池为空，全自动已停止")
                continue
            auth_target = min(pay_success, pending_after)
            auth_workers = min(workers_auth, auth_target)
            return _run_paypal_authorize(count=auth_target, workers=max(1, auth_workers))

        elif choice in {"7", "0"}:
            return 0

        else:
            ui_error("请输入 1-7")


def _run_paypal_authorize(*, count: int | None = None, workers: int | None = None) -> int:
    """复用 authorization_flow 对 PayPal 待授权账号执行 OAuth。"""
    import authorization_flow

    paypal_auth_output = str(PAYPAL_OUTPUT_ROOT / "授权成功")
    args = argparse.Namespace(
        count=count,
        workers=workers,
        paid_file=str(PENDING_AUTH_FILE),
        output_root=paypal_auth_output,
        country="",
        sms_provider="",
        hero_sms_api_key="",
        hero_sms_service="",
        hero_sms_country_top_n=None,
        hero_sms_operator_threshold=None,
        grizzly_api_key="",
        grizzly_service="",
        grizzly_country_top_n=None,
        grizzly_provider_threshold=None,
    )
    return authorization_flow.interactive_authorize(args)


def _run_paypal_session_export(cfg: dict[str, Any] | None = None) -> int:
    """复用 Session 快捷导出，将 PayPal 待授权池导出为 CPA + sub2api。"""
    from . import session_export

    if cfg is None:
        cfg = load_config("config.yaml")

    output_root = resolve_path(str(PAYPAL_OUTPUT_ROOT / "授权成功"))
    cache_path = resolve_path(str(PAYPAL_SESSION_CACHE_FILE))
    PAYPAL_SESSIOND_DIR.mkdir(parents=True, exist_ok=True)
    env = load_env(".env")

    # Session 补录复用流程1浏览器设置；注册阶段默认直连。
    use_proxy, proxy_file = resolve_register_proxy_settings(env)
    proxy_pool: ProxyPool | None = None
    if use_proxy:
        proxy_pool = ProxyPool(proxy_file)
        if proxy_pool.count() <= 0:
            log(f"PayPal 流程3 Session 导出：流程1代理已开启但代理池为空: {proxy_file}")
        else:
            log(f"PayPal 流程3 Session 导出：已启用流程1代理，代理数={proxy_pool.count()}")

    paid_records = session_export.read_paid_records(str(PENDING_AUTH_FILE))
    cache_records = session_export.load_session_cache(cache_path)
    missing = [
        record
        for record in paid_records
        if not session_export.find_cache_record(str(record.get("account") or ""), cache_records)
    ]

    if missing:
        log(f"PayPal 流程3 Session 导出：检测到 {len(missing)} 个账号缺少 session，开始自动登录补录")
        lookup = _build_account_lookup(cfg)
        hydrated = 0
        for idx, record in enumerate(missing, 1):
            proxy = proxy_pool.pick(idx) if proxy_pool else None
            ok = asyncio.run(
                _hydrate_session_via_flow1_login(
                    record,
                    cfg=cfg,
                    cache_path=str(cache_path),
                    lookup=lookup,
                    index=idx,
                    total=len(missing),
                    proxy=proxy,
                )
            )
            if not ok:
                continue
            hydrated += 1

        log(f"PayPal 流程3 Session 导出：自动补录 session 完成 {hydrated}/{len(missing)}")

    result = session_export.export_paid_sessions(
        paid_file=str(PENDING_AUTH_FILE),
        cache_path=str(cache_path),
        output_root=str(output_root),
    )

    # 兼容当前流程三目录结构：同步一份标准 sub2api_accounts.json
    session_sub = output_root / "session_sub2api_accounts.json"
    std_sub = output_root / "sub2api_accounts.json"
    if session_sub.exists():
        std_sub.write_text(session_sub.read_text(encoding="utf-8"), encoding="utf-8")

    success = int(result.get("success", 0) or 0)
    total = int(result.get("total", 0) or 0)
    skipped = int(len(result.get("skipped") or []))
    log(f"PayPal 流程3 Session 导出完成: 成功={success}/{total}，跳过={skipped}，输出={output_root}")
    if success > 0:
        log(f"PayPal 流程3 Session 单账号 SUB 目录: {output_root / 'sub2api_session'}")
    return 0 if success > 0 or total == 0 else 1
