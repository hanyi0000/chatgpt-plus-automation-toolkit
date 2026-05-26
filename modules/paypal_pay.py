"""PayPal 流程2：从长链接池取账号 → Stripe → PayPal 注册绑卡 → 支付。

输入：output/paypal成品/长链接账号/account.txt + cards.txt + phones.txt
输出：output/paypal成品/待授权账号/account.txt
"""
from __future__ import annotations

import asyncio
import hashlib
import random
import re
import time
import traceback
from urllib.parse import urlparse
from pathlib import Path
from typing import Any

import requests

from .browser import BrowserSession
from .paypal_card_pool import CardInfo, CardPool
from .paypal_phone_pool import PhoneInfo, PhonePool
from .utils import load_env, log, resolve_path, safe_filename


PAYPAL_OUTPUT_ROOT = resolve_path("output/paypal注册")
LINK_POOL_FILE = PAYPAL_OUTPUT_ROOT / "长链接账号" / "account.txt"
PENDING_AUTH_DIR = PAYPAL_OUTPUT_ROOT / "待授权账号"
PENDING_AUTH_FILE = PENDING_AUTH_DIR / "account.txt"
PAYPAL_FLOW2_CODE_VERSION = "PAYPAL_AGREE_CONTINUE_FIX_2026-05-19_04"

_RANDOM_CARD_PROFILES: list[tuple[str, str, str, str, str]] = [
    ("New York", "NY", "10001", "W 34th St", "US"),
    ("Los Angeles", "CA", "90017", "S Grand Ave", "US"),
    ("Chicago", "IL", "60606", "N LaSalle St", "US"),
    ("Houston", "TX", "77002", "Louisiana St", "US"),
    ("Phoenix", "AZ", "85004", "E Washington St", "US"),
    ("Philadelphia", "PA", "19103", "Market St", "US"),
    ("San Antonio", "TX", "78205", "E Houston St", "US"),
    ("San Diego", "CA", "92101", "Broadway", "US"),
    ("Dallas", "TX", "75201", "Main St", "US"),
    ("San Jose", "CA", "95113", "Santa Clara St", "US"),
    ("Austin", "TX", "78701", "Congress Ave", "US"),
    ("Jacksonville", "FL", "32202", "Bay St", "US"),
    ("Fort Worth", "TX", "76102", "Houston St", "US"),
    ("Columbus", "OH", "43215", "High St", "US"),
    ("Charlotte", "NC", "28202", "Trade St", "US"),
    ("Indianapolis", "IN", "46204", "Meridian St", "US"),
    ("Seattle", "WA", "98101", "Pike St", "US"),
    ("Denver", "CO", "80202", "17th St", "US"),
    ("Boston", "MA", "02110", "Atlantic Ave", "US"),
    ("Nashville", "TN", "37219", "Church St", "US"),
]


def is_local_random_card_mode(env: dict[str, str]) -> bool:
    source = (env.get("PAYPAL_CARD_SOURCE") or "").strip().lower()
    return source in {"local_random", "random_local", "local"}


def _luhn_check_digit(number_without_check: str) -> str:
    digits = [int(ch) for ch in number_without_check]
    total = 0
    parity = (len(digits) + 1) % 2
    for idx, value in enumerate(digits):
        if idx % 2 == parity:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return str((10 - (total % 10)) % 10)


def _build_luhn_card_number(prefix: str, random_part_length: int, *, rng: random.Random) -> str:
    body = prefix + "".join(str(rng.randint(0, 9)) for _ in range(random_part_length))
    return body + _luhn_check_digit(body)


def _generate_local_random_card(index: int, email: str, env: dict[str, str]) -> CardInfo:
    seed_raw = f"{email.lower()}::{index}::{time.time_ns()}"
    seed = int(hashlib.sha256(seed_raw.encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed)

    first_pool = ["James", "Mary", "John", "Patricia", "Robert", "Jennifer", "Michael", "Linda"]
    last_pool = ["Smith", "Johnson", "Williams", "Brown", "Davis", "Miller", "Wilson", "Moore"]
    first_name = first_pool[rng.randrange(len(first_pool))]
    last_name = last_pool[rng.randrange(len(last_pool))]
    holder = f"{first_name} {last_name}"

    profile = _RANDOM_CARD_PROFILES[rng.randrange(len(_RANDOM_CARD_PROFILES))]
    city, state, zip_code, street_base, country = profile
    street_no = rng.randint(10, 9999)
    street = f"{street_no} {street_base}"

    year = time.gmtime().tm_year + rng.randint(2, 5)
    exp_year = str(year)[-2:]
    exp_month = str(rng.randint(1, 12)).zfill(2)
    cvv = str(rng.randint(100, 999))

    custom_bin = re.sub(r"\D+", "", (env.get("PAYPAL_RANDOM_CARD_BIN") or ""))[:8]
    custom_bin_allowed = len(custom_bin) >= 6 and not custom_bin.startswith("5200")
    if custom_bin_allowed:
        bin_prefix = custom_bin
    else:
        brands_raw = (env.get("PAYPAL_RANDOM_CARD_BRAND") or "visa,mastercard").strip().lower()
        brands = {b.strip() for b in brands_raw.split(",") if b.strip()}
        allowed_prefixes: list[str] = []
        if "visa" in brands:
            allowed_prefixes.extend(["453201", "448527", "412345"])
        if "mastercard" in brands or "master" in brands:
            allowed_prefixes.extend(["510510", "222100"])
        allowed_prefixes = [prefix for prefix in allowed_prefixes if not prefix.startswith("5200")]
        if not allowed_prefixes:
            allowed_prefixes = ["453201", "510510"]
        bin_prefix = allowed_prefixes[rng.randrange(len(allowed_prefixes))]
    random_len = 15 - len(bin_prefix)
    number = _build_luhn_card_number(bin_prefix, random_len, rng=rng)

    return CardInfo(
        number=number,
        exp_month=exp_month,
        exp_year=exp_year,
        cvv=cvv,
        holder_name=holder,
        first_name=first_name,
        last_name=last_name,
        street=street,
        city=city,
        state=state,
        zip_code=zip_code,
        country=country,
        phone="",
        sms_api_url="",
        raw_line=f"LOCAL_RANDOM::{email}::{index}",
    )


def _display_proxy(proxy: str | None) -> str:
    """隐藏代理密码用于日志显示。"""
    if not proxy:
        return "无"
    text = proxy.strip()
    if "@" in text:
        prefix, suffix = text.rsplit("@", 1)
        scheme = prefix.split("://", 1)[0] + "://" if "://" in prefix else ""
        return f"{scheme}***:***@{suffix}"
    return text


def load_link_pool() -> list[dict[str, str]]:
    if not LINK_POOL_FILE.exists():
        return []
    items = []
    for line in LINK_POOL_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        if len(parts) >= 3:
            items.append({"email": parts[0].strip(), "query_code": parts[1].strip(), "payment_link": "----".join(parts[2:]).strip()})
    return items


def save_pending_auth(email: str, query_code: str) -> None:
    PENDING_AUTH_DIR.mkdir(parents=True, exist_ok=True)
    with PENDING_AUTH_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{email}----{query_code}\n")


def remove_from_link_pool(email: str) -> None:
    if not LINK_POOL_FILE.exists():
        return
    lines = LINK_POOL_FILE.read_text(encoding="utf-8").splitlines()
    remaining = [l for l in lines if not l.strip().lower().startswith(email.lower())]
    LINK_POOL_FILE.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")


def generate_paypal_password(email: str) -> str:
    local = email.split("@")[0] if "@" in email else email
    prefix_raw = re.sub(r"[^a-zA-Z0-9]", "", local)
    prefix = (prefix_raw[:3] or "usr").lower()

    upper_pool = "ABCDEFGHJKLMNPQRSTUVWXYZ"
    lower_pool = "abcdefghjkmnpqrstuvwxyz"
    digit_pool = "246789"
    seed = hashlib.sha256(email.lower().encode("utf-8")).digest()

    def pick(pool: str, idx: int) -> str:
        return pool[seed[idx] % len(pool)]

    candidate = (
        f"{prefix}{pick(upper_pool, 0)}{pick(lower_pool, 1)}"
        f"#{pick(digit_pool, 2)}{pick(upper_pool, 3)}{pick(digit_pool, 4)}"
        f"!{pick(lower_pool, 5)}{pick(upper_pool, 6)}"
    )

    def has_4_key_sequence(s: str) -> bool:
        t = s.lower()
        rows = ("0123456789", "qwertyuiop", "asdfghjkl", "zxcvbnm")
        for row in rows:
            for i in range(len(row) - 3):
                seq = row[i : i + 4]
                if seq in t or seq[::-1] in t:
                    return True
        for i in range(len(t) - 3):
            chunk = t[i : i + 4]
            if chunk.isdigit():
                vals = [ord(c) for c in chunk]
                if all(vals[j + 1] - vals[j] == 1 for j in range(3)) or all(
                    vals[j + 1] - vals[j] == -1 for j in range(3)
                ):
                    return True
            if chunk.isalpha():
                vals = [ord(c) for c in chunk]
                if all(vals[j + 1] - vals[j] == 1 for j in range(3)) or all(
                    vals[j + 1] - vals[j] == -1 for j in range(3)
                ):
                    return True
        return False

    if has_4_key_sequence(candidate):
        candidate = (
            f"{prefix}{pick(upper_pool, 7)}{pick(lower_pool, 8)}"
            f"#{pick(digit_pool, 9)}{pick(upper_pool, 10)}{pick(digit_pool, 11)}"
            f"!{pick(lower_pool, 12)}{pick(upper_pool, 13)}"
        )
    return candidate


def poll_sms_code(api_url: str, *, timeout: int = 120, interval: int = 5) -> str:
    """轮询手机 API 获取验证码。"""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(api_url, timeout=10)
            text = resp.text.strip()
            parts = text.split("|", 2)
            status = parts[0].lower() if parts else ""
            content = parts[1] if len(parts) > 1 else ""
            if status != "no" and content and content != "暂无验证码":
                code_match = re.search(r"\b(\d{4,6})\b", content)
                if code_match:
                    return code_match.group(1)
        except Exception:
            pass
        time.sleep(interval)
    raise TimeoutError(f"手机验证码超时 ({timeout}s)")


async def fill_stripe(page, email: str, card: CardInfo) -> None:
    """Stripe 页面：选 PayPal + 填地址 + Subscribe。"""
    await page.wait_for_load_state("domcontentloaded", timeout=30000)
    await page.wait_for_timeout(8000)

    # 防御性修正：部分卡源会把 "CITY ZIP" 合并到 city 字段
    city_value = (card.city or "").strip()
    zip_value = (card.zip_code or "").strip()
    m_city_zip = re.search(r"^(?P<city>.*)\s+(?P<zip>\d{5}(?:-\d{4})?)$", city_value)
    if m_city_zip:
        city_value = (m_city_zip.group("city") or "").strip()
        if not zip_value:
            zip_value = (m_city_zip.group("zip") or "").strip()

    # 选 PayPal
    paypal_btn = page.locator('[data-testid="paypal-accordion-item-button"], [aria-label*="PayPal" i], text=PayPal').first
    try:
        await paypal_btn.click(timeout=5000, force=True)
    except Exception:
        await page.evaluate('document.querySelector("[data-testid=\\"paypal-accordion-item-button\\"]")?.click()')
    await page.wait_for_timeout(2000)

    # 国家选 US - 先等待下拉框可交互
    country_select = page.locator('#billingCountry, select[name*="country" i], select[autocomplete="country"]').first
    try:
        await country_select.wait_for(state="visible", timeout=8000)
        await country_select.select_option("US", timeout=5000)
    except Exception:
        try:
            await country_select.select_option(label="United States", timeout=3000)
        except Exception:
            pass

    # 等待国家切换后页面重新渲染地址字段
    await page.wait_for_timeout(3000)

    # 验证国家是否选中 US
    try:
        current_val = await country_select.input_value()
        if current_val != "US":
            log(f"[Stripe] 国家仍为 {current_val}，再次尝试...")
            await country_select.select_option("US", timeout=3000)
            await page.wait_for_timeout(2000)
    except Exception:
        pass

    # 手动输入地址
    try:
        manual = page.locator('text=手动输入地址, text=Enter address manually, a:has-text("手动"), a:has-text("manually")').first
        await manual.click(timeout=5000)
        await page.wait_for_timeout(1500)
    except Exception:
        pass

    # 填地址
    for selector, value in [
        ('#billingAddressLine1, input[name*="addressLine1" i], input[name*="address" i], input[placeholder*="地址" i], input[placeholder*="Address" i]', card.street),
        ('#billingLocality, input[name*="locality" i], input[name*="city" i], input[placeholder*="城市" i], input[placeholder*="City" i]', city_value),
        ('#billingPostalCode, input[name*="postalCode" i], input[name*="zip" i], input[placeholder*="邮编" i], input[placeholder*="ZIP" i]', zip_value),
    ]:
        try:
            await page.locator(selector).first.fill(value, timeout=5000)
        except Exception:
            pass

    # State - 尝试多种方式匹配
    try:
        state_el = page.locator('#billingAdministrativeArea, select[name*="state" i]').first
        if await state_el.is_visible(timeout=3000):
            tag = await state_el.evaluate("el => el.tagName.toLowerCase()")
            if tag == "select":
                try:
                    await state_el.select_option(value=card.state, timeout=3000)
                except Exception:
                    # 尝试用全称
                    state_names = {
                        "MN": "Minnesota", "TN": "Tennessee", "CA": "California",
                        "TX": "Texas", "NY": "New York", "FL": "Florida",
                        "IL": "Illinois", "PA": "Pennsylvania", "OH": "Ohio",
                        "GA": "Georgia", "NC": "North Carolina", "MI": "Michigan",
                        "NJ": "New Jersey", "VA": "Virginia", "WA": "Washington",
                        "AZ": "Arizona", "MA": "Massachusetts", "IN": "Indiana",
                        "MO": "Missouri", "MD": "Maryland", "WI": "Wisconsin",
                        "CO": "Colorado", "SC": "South Carolina", "AL": "Alabama",
                        "LA": "Louisiana", "KY": "Kentucky", "OR": "Oregon",
                        "OK": "Oklahoma", "CT": "Connecticut", "IA": "Iowa",
                        "MS": "Mississippi", "AR": "Arkansas", "KS": "Kansas",
                        "NV": "Nevada", "UT": "Utah", "NE": "Nebraska",
                        "NM": "New Mexico", "WV": "West Virginia", "ID": "Idaho",
                        "HI": "Hawaii", "ME": "Maine", "NH": "New Hampshire",
                        "RI": "Rhode Island", "MT": "Montana", "DE": "Delaware",
                        "SD": "South Dakota", "ND": "North Dakota", "AK": "Alaska",
                        "VT": "Vermont", "WY": "Wyoming", "DC": "District of Columbia",
                    }
                    full_name = state_names.get(card.state, card.state)
                    try:
                        await state_el.select_option(label=full_name, timeout=3000)
                    except Exception:
                        pass
            else:
                await state_el.fill(card.state, timeout=3000)
    except Exception:
        pass

    # 勾选条款
    try:
        cb = page.locator('input[type="checkbox"], [role="checkbox"]').first
        if await cb.is_visible(timeout=2000) and not await cb.is_checked():
            await cb.click(force=True)
            await page.wait_for_timeout(1000)
    except Exception:
        pass

    # 关闭可能弹出的地址建议下拉框（Google 地址自动补全）
    # 按 Escape 关闭下拉，再点击页面空白处确保焦点离开输入框
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)
        # 点击页面标题区域，确保没有下拉遮挡
        await page.locator('body').click(position={"x": 10, "y": 10}, force=True)
        await page.wait_for_timeout(500)
    except Exception:
        pass

    # Subscribe / 订阅 - 多种选择器兜底
    log("[Stripe] 点击订阅按钮...")
    subscribe_clicked = False
    subscribe_selectors = [
        'button:has-text("Subscribe")',
        'button:has-text("订阅")',
        'button.SubmitButton',
        'button.SubmitButton--complete',
        '[data-testid="hosted-payment-submit-button"]',
        'button[type="submit"]',
    ]
    for sel in subscribe_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                # 确保按钮没被禁用
                is_disabled = await btn.is_disabled()
                if is_disabled:
                    log(f"[Stripe] 按钮被禁用: {sel}，等待...")
                    await page.wait_for_timeout(3000)
                await btn.click(timeout=10000)
                subscribe_clicked = True
                log(f"[Stripe] 订阅按钮已点击 (选择器: {sel})")
                break
        except Exception:
            continue

    if not subscribe_clicked:
        # 终极兜底：点击页面上最后一个可见的 submit 按钮
        log("[Stripe] 常规选择器未命中，尝试 JS 点击...")
        await page.evaluate("""() => {
            const buttons = Array.from(document.querySelectorAll('button'));
            for (const btn of buttons.reverse()) {
                const text = (btn.textContent || '').trim();
                const rect = btn.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0 && /Subscribe|订阅|submit/i.test(text + btn.type)) {
                    btn.click();
                    return;
                }
            }
        }""")

    # 等跳转 PayPal - 如果 10 秒没跳转，再点一次订阅
    jumped = False
    for attempt in range(2):
        for _ in range(30 if attempt == 0 else 30):
            if "paypal.com" in page.url:
                jumped = True
                break
            await page.wait_for_timeout(1000)
        if jumped:
            break
        if attempt == 0:
            log("[Stripe] 30s 未跳转 PayPal，检查是否有表单错误并重试点击...")
            # 检查是否有错误提示
            has_error = await page.evaluate("""() => {
                const text = (document.body?.innerText || '');
                return /This is required|必填|invalid|错误|error/i.test(text);
            }""")
            if has_error:
                log("[Stripe] 检测到表单错误，可能地址未填完整")
            # 再点一次
            for sel in subscribe_selectors[:3]:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=1000):
                        await btn.click(timeout=5000)
                        log(f"[Stripe] 重试点击订阅按钮: {sel}")
                        break
                except Exception:
                    continue

    if not jumped:
        raise RuntimeError(f"60s 内未跳转 PayPal，URL: {page.url}")
    await page.wait_for_timeout(3000)


async def fill_paypal(page, email: str, card: CardInfo, phone: PhoneInfo, paypal_password: str, proxy: str | None = None) -> None:
    """PayPal 页面：注册 + 绑卡。"""
    await page.wait_for_timeout(5000)

    # 第一步：填邮箱（确保所有可见的 email 字段都被填写）
    log("[PayPal] 填写邮箱...")
    email_filled = False
    # PayPal 中文页面的 placeholder 是 "电子邮箱地址或手机号码"
    email_selectors = 'input[name="email"], input[type="email"], input[placeholder*="邮箱" i], input[placeholder*="email" i], input[placeholder*="手机号" i]'
    email_fields = page.locator(email_selectors)
    count = await email_fields.count()
    for i in range(count):
        field = email_fields.nth(i)
        try:
            if await field.is_visible(timeout=2000):
                await field.fill("", timeout=2000)
                await field.fill(email, timeout=3000)
                email_filled = True
                break
        except Exception:
            pass
    if not email_filled:
        log("[PayPal] ⚠️ 未找到可见的邮箱输入框，尝试备用选择器")
        try:
            await page.locator('input[aria-label*="email" i], input[aria-label*="邮箱" i]').first.fill(email, timeout=5000)
        except Exception:
            pass

    # 点击"继续付款"优先（PayPal 注册页面的实际按钮文字），然后是其他变体
    clicked_next = False
    next_selectors = [
        'button:has-text("继续付款")',
        'button:has-text("Continue")',
        'button:has-text("继续")',
        'button:has-text("Next")',
        'button:has-text("下一页")',
        'button:has-text("次へ")',
        'button[id="btnNext"]',
        'button[type="submit"]',
    ]
    for sel in next_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click(timeout=5000)
                clicked_next = True
                log(f"[PayPal] 点击了按钮 (选择器: {sel})")
                break
        except Exception:
            continue

    if not clicked_next:
        log("[PayPal] ⚠️ 未找到可点击的下一步按钮")

    # 等待 PayPal 页面加载完成（按钮转圈结束，新页面元素出现）
    log("[PayPal] 等待页面跳转/加载...")
    for _ in range(30):
        await page.wait_for_timeout(2000)
        # 检查是否已经进入了注册表单（有国家选择框或手机号输入框）
        try:
            has_form = await page.evaluate("""() => {
                const selects = document.querySelectorAll('select');
                const hasCountrySelect = Array.from(selects).some(s => s.options.length > 50 || /country/i.test(s.name + s.id));
                const hasPhoneInput = !!document.querySelector('input[name*="phone" i], input[id*="phone" i], input[placeholder*="Phone" i], input[placeholder*="手机" i]');
                const hasCardInput = !!document.querySelector('input[name*="card" i], input[id*="card" i], input[placeholder*="Card" i], input[placeholder*="卡号" i]');
                return hasCountrySelect || hasPhoneInput || hasCardInput;
            }""")
        except Exception:
            # 页面正在导航中，等一下再试
            continue
        if has_form:
            log("[PayPal] 注册表单已加载")
            break
    else:
        log("[PayPal] ⚠️ 等待 60 秒后仍未检测到注册表单，继续尝试...")

    await page.wait_for_timeout(2000)

    # 第二步：进入注册表单后，先切国家到 US
    # 等待国家下拉框出现并可交互
    log("[PayPal] 等待国家选择框加载...")
    try:
        await page.locator('select').first.wait_for(state="attached", timeout=10000)
    except Exception:
        pass
    await page.wait_for_timeout(2000)

    log("[PayPal] 切换国家到 United States...")
    # 使用 select_option 而非直接设置 value，确保触发完整的 change 事件链
    country_switched = False
    try:
        country_sel = page.locator('select[name*="country" i], select[id*="country" i]').first
        if await country_sel.is_visible(timeout=3000):
            await country_sel.select_option("US", timeout=5000)
            country_switched = True
    except Exception:
        pass

    if not country_switched:
        # 备用方案：遍历所有 select 找到国家下拉
        await page.evaluate("""() => {
            const selects = Array.from(document.querySelectorAll('select'));
            for (const sel of selects) {
                const opt = Array.from(sel.options).find(o => o.value === 'US');
                if (opt && (sel.name.toLowerCase().includes('country') || sel.id.toLowerCase().includes('country') || sel.options.length > 50)) {
                    sel.value = 'US';
                    sel.dispatchEvent(new Event('input', {bubbles: true}));
                    sel.dispatchEvent(new Event('change', {bubbles: true}));
                    break;
                }
            }
        }""")

    # 国家切换后页面会重新渲染表单字段，必须等待足够时间
    log("[PayPal] 等待页面根据新国家重新加载表单...")
    await page.wait_for_timeout(5000)

    # 等待表单字段重新出现（国家切换后地址字段会重新渲染）
    try:
        await page.locator('input[name*="phone" i], input[id*="phone" i], input[placeholder*="Phone" i]').first.wait_for(state="visible", timeout=10000)
    except Exception:
        await page.wait_for_timeout(3000)

    # 验证国家是否切换成功
    current_country = await page.evaluate("""() => {
        const selects = Array.from(document.querySelectorAll('select'));
        for (const sel of selects) {
            if (sel.name.toLowerCase().includes('country') || sel.id.toLowerCase().includes('country') || sel.options.length > 50) {
                return sel.value;
            }
        }
        return '';
    }""")
    if current_country != "US":
        log(f"[PayPal] ⚠️ 国家仍为 {current_country}，再次尝试切换...")
        try:
            country_sel = page.locator('select[name*="country" i], select[id*="country" i]').first
            await country_sel.select_option("US", timeout=5000)
            await page.wait_for_timeout(5000)
        except Exception:
            await page.evaluate("""() => {
                const selects = Array.from(document.querySelectorAll('select'));
                for (const sel of selects) {
                    const opt = Array.from(sel.options).find(o => o.value === 'US');
                    if (opt && (sel.name.toLowerCase().includes('country') || sel.id.toLowerCase().includes('country') || sel.options.length > 50)) {
                        sel.value = 'US';
                        sel.dispatchEvent(new Event('input', {bubbles: true}));
                        sel.dispatchEvent(new Event('change', {bubbles: true}));
                        break;
                    }
                }
            }""")
            await page.wait_for_timeout(5000)
    else:
        log("[PayPal] ✓ 国家已确认切换为 US")

    # 再次确认邮箱（国家切换后页面可能重置了邮箱字段）
    try:
        email_fields_after = page.locator('input[name="email"], input[type="email"]')
        count_after = await email_fields_after.count()
        for i in range(count_after):
            field = email_fields_after.nth(i)
            if await field.is_visible(timeout=1000):
                val = (await field.input_value()).strip()
                if not val:
                    log("[PayPal] 邮箱字段为空，重新填写...")
                    await field.fill(email, timeout=3000)
                break
    except Exception:
        pass

    # 手机号
    phone_local = phone.number.lstrip("+1") if phone.number.startswith("+1") else phone.number.lstrip("+")
    try:
        phone_input = page.locator('input[name*="phone" i], input[id*="phone" i], input[placeholder*="Phone" i]').first
        await phone_input.fill("", timeout=2000)
        await phone_input.fill(phone_local, timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(500)

    # 卡号
    try:
        card_input = page.locator('input[name="cardnumber"], input[id*="card" i], input[placeholder*="Card" i]').first
        await card_input.fill("", timeout=2000)
        await card_input.fill(card.number, timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(500)

    # 有效期
    try:
        exp_input = page.locator('input[name="exp-date"], input[id*="exp" i], input[placeholder*="Expir" i]').first
        await exp_input.fill("", timeout=2000)
        await exp_input.fill(f"{card.exp_month}/{card.exp_year}", timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(500)

    # CVV
    try:
        cvv_input = page.locator('input[name="cvv"], input[id*="cvv" i], input[placeholder*="CVV" i]').first
        await cvv_input.fill("", timeout=2000)
        await cvv_input.fill(card.cvv, timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(500)

    # 地址（先清空再填写，避免残留旧值）
    # PayPal 页面字段可能用 placeholder 而非 name 属性，需要多种选择器兜底
    log("[PayPal] 填写地址信息...")

    # First name
    try:
        loc = page.locator('input[name="fname"], input[id*="first" i], input[placeholder*="First" i], input[autocomplete="given-name"]').first
        if await loc.is_visible(timeout=2000):
            await loc.fill("", timeout=2000)
            await loc.fill(card.first_name, timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(300)

    # Last name
    try:
        loc = page.locator('input[name="lname"], input[id*="last" i], input[placeholder*="Last" i], input[autocomplete="family-name"]').first
        if await loc.is_visible(timeout=2000):
            await loc.fill("", timeout=2000)
            await loc.fill(card.last_name, timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(300)

    # Street address - PayPal 页面可能用浮动 label 而非 placeholder
    # 尝试多种方式定位 street 字段
    street_filled = False
    street_selectors = [
        'input[name*="street" i]',
        'input[name*="address" i]:not([name*="email" i])',
        'input[autocomplete="address-line1"]',
        'input[placeholder*="Street" i]',
        'input[placeholder*="地址" i]',
        'input[placeholder*="Address" i]',
        'input[aria-label*="Street" i]',
        'input[aria-label*="address" i]:not([aria-label*="email" i])',
        'input[id*="street" i]',
        'input[id*="address" i]:not([id*="email" i])',
    ]
    for sel in street_selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=1500):
                await loc.fill("", timeout=2000)
                await loc.fill(card.street, timeout=5000)
                street_filled = True
                log(f"[PayPal] Street 已填 (选择器: {sel})")
                break
        except Exception:
            continue

    if not street_filled:
        # 最后兜底：通过 label 文本找到对应的 input
        log("[PayPal] Street 常规选择器均未命中，尝试通过 label 文本定位...")
        try:
            loc = page.get_by_label("Street address", exact=False).first
            if await loc.is_visible(timeout=2000):
                await loc.fill(card.street, timeout=5000)
                street_filled = True
        except Exception:
            pass
        if not street_filled:
            try:
                loc = page.get_by_label("地址", exact=False).first
                if await loc.is_visible(timeout=2000):
                    await loc.fill(card.street, timeout=5000)
                    street_filled = True
            except Exception:
                pass
        if not street_filled:
            # 终极兜底：找 Billing address 区域下第一个空的 text input（排除 first/last name）
            try:
                street_filled = await page.evaluate("""(street) => {
                    const inputs = Array.from(document.querySelectorAll('input[type="text"], input:not([type])'));
                    for (const inp of inputs) {
                        const rect = inp.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        const label = (inp.placeholder || inp.getAttribute('aria-label') || inp.name || inp.id || '').toLowerCase();
                        // 跳过已知字段
                        if (/first|last|city|zip|postal|phone|email|apt|suite|bldg/i.test(label)) continue;
                        // 跳过已有值的
                        if ((inp.value || '').trim()) continue;
                        // 这个可能就是 street
                        const proto = HTMLInputElement.prototype;
                        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                        desc?.set?.call(inp, street);
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }
                    return false;
                }""", card.street)
            except Exception:
                pass
    if not street_filled:
        log("[PayPal] ⚠️ Street address 填写失败，所有选择器均未命中")
    await page.wait_for_timeout(300)

    # City
    try:
        loc = page.locator('input[name="city"], input[name*="city" i], input[autocomplete="address-level2"], input[placeholder*="City" i], input[placeholder*="城市" i]').first
        if await loc.is_visible(timeout=3000):
            await loc.fill("", timeout=2000)
            await loc.fill(card.city, timeout=5000)
        else:
            raise Exception("not visible")
    except Exception:
        try:
            loc = page.get_by_placeholder("City").first
            await loc.fill(card.city, timeout=5000)
        except Exception:
            pass
    await page.wait_for_timeout(300)

    # ZIP code
    try:
        loc = page.locator('input[name*="zip" i], input[name*="postal" i], input[autocomplete="postal-code"], input[placeholder*="ZIP" i], input[placeholder*="邮编" i], input[placeholder*="Postal" i]').first
        if await loc.is_visible(timeout=2000):
            await loc.fill("", timeout=2000)
            await loc.fill(card.zip_code, timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(300)

    # State（可能是 select 下拉或 input 输入框）
    try:
        state_sel = page.locator('select[name="state"], select[name*="state" i], select[autocomplete="address-level1"], select[aria-label*="State" i]').first
        if await state_sel.is_visible(timeout=3000):
            try:
                await state_sel.select_option(value=card.state, timeout=5000)
            except Exception:
                # 尝试用 label 匹配
                try:
                    await state_sel.select_option(label=card.state, timeout=3000)
                except Exception:
                    pass
        else:
            # State 可能是 input 而非 select
            state_input = page.locator('input[name*="state" i], input[placeholder*="State" i], input[autocomplete="address-level1"]').first
            if await state_input.is_visible(timeout=2000):
                await state_input.fill("", timeout=2000)
                await state_input.fill(card.state, timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(500)

    # 密码
    try:
        pwd_input = page.locator('input[name="password"], input[type="password"]').first
        if await pwd_input.is_visible(timeout=3000):
            await pwd_input.fill("", timeout=2000)
            await pwd_input.fill(paypal_password, timeout=5000)
    except Exception:
        pass
    await page.wait_for_timeout(1000)

    # 最终检查：确认所有关键字段已填写
    log("[PayPal] 检查表单完整性...")
    empty_fields = await page.evaluate("""() => {
        const checks = [
            {name: 'email', selectors: 'input[name="email"], input[type="email"]'},
            {name: 'phone', selectors: 'input[name*="phone" i], input[id*="phone" i]'},
            {name: 'card', selectors: 'input[name="cardnumber"], input[id*="card" i]'},
            {name: 'firstName', selectors: 'input[name="fname"], input[id*="first" i], input[autocomplete="given-name"], input[placeholder*="First" i]'},
            {name: 'lastName', selectors: 'input[name="lname"], input[id*="last" i], input[autocomplete="family-name"], input[placeholder*="Last" i]'},
            {name: 'street', selectors: 'input[name*="street" i], input[name*="address" i], input[autocomplete="address-line1"], input[placeholder*="Street" i]'},
            {name: 'city', selectors: 'input[name="city"], input[name*="city" i], input[autocomplete="address-level2"], input[placeholder*="City" i]'},
            {name: 'zip', selectors: 'input[name*="zip" i], input[name*="postal" i], input[autocomplete="postal-code"], input[placeholder*="ZIP" i]'},
        ];
        const empty = [];
        for (const {name, selectors} of checks) {
            const els = document.querySelectorAll(selectors);
            let found = false;
            for (const el of els) {
                const rect = el.getBoundingClientRect();
                if (rect.width > 0 && rect.height > 0) {
                    if ((el.value || '').trim()) {
                        found = true;
                    }
                    break;
                }
            }
            if (!found) empty.push(name);
        }
        return empty;
    }""")
    if empty_fields:
        log(f"[PayPal] ⚠️ 以下字段仍为空: {empty_fields}，尝试补填...")
        # 补填邮箱
        if "email" in empty_fields:
            try:
                await page.locator('input[name="email"], input[type="email"]').first.fill(email, timeout=3000)
            except Exception:
                pass
        # 补填手机号
        if "phone" in empty_fields:
            try:
                await page.locator('input[name*="phone" i], input[id*="phone" i]').first.fill(phone_local, timeout=3000)
            except Exception:
                pass
        # 补填 street
        if "street" in empty_fields:
            try:
                loc = page.locator('input[placeholder*="Street" i], input[name*="street" i], input[name*="address" i]:not([name*="email" i]), input[aria-label*="Street" i], input[id*="street" i], input[id*="address" i]:not([id*="email" i])').first
                if await loc.is_visible(timeout=2000):
                    await loc.fill(card.street, timeout=3000)
                else:
                    loc = page.get_by_label("Street address", exact=False).first
                    await loc.fill(card.street, timeout=3000)
            except Exception:
                # 终极兜底
                await page.evaluate("""(street) => {
                    const inputs = Array.from(document.querySelectorAll('input[type="text"], input:not([type])'));
                    for (const inp of inputs) {
                        const rect = inp.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        const label = (inp.placeholder || inp.getAttribute('aria-label') || inp.name || inp.id || '').toLowerCase();
                        if (/first|last|city|zip|postal|phone|email|apt|suite|bldg/i.test(label)) continue;
                        if ((inp.value || '').trim()) continue;
                        const proto = HTMLInputElement.prototype;
                        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                        desc?.set?.call(inp, street);
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        return;
                    }
                }""", card.street)
        # 补填 city
        if "city" in empty_fields:
            try:
                loc = page.locator('input[placeholder*="City" i], input[name*="city" i]').first
                await loc.fill(card.city, timeout=3000)
            except Exception:
                pass
        await page.wait_for_timeout(1000)

    async def _refill_card_fields(c: CardInfo) -> None:
        try:
            card_input = page.locator('input[name="cardnumber"], input[id*="card" i], input[placeholder*="Card" i]').first
            await card_input.fill("", timeout=2000)
            await card_input.fill(c.number, timeout=5000)
        except Exception:
            pass
        await page.wait_for_timeout(300)
        try:
            exp_input = page.locator('input[name="exp-date"], input[id*="exp" i], input[placeholder*="Expir" i]').first
            await exp_input.fill("", timeout=2000)
            await exp_input.fill(f"{c.exp_month}/{c.exp_year}", timeout=5000)
        except Exception:
            pass
        await page.wait_for_timeout(300)
        try:
            cvv_input = page.locator('input[name="cvv"], input[id*="cvv" i], input[placeholder*="CVV" i]').first
            await cvv_input.fill("", timeout=2000)
            await cvv_input.fill(c.cvv, timeout=5000)
        except Exception:
            pass
        await page.wait_for_timeout(300)

    async def _is_card_rejected() -> bool:
        try:
            body_text = ""
            try:
                body_text = await page.locator("body").inner_text(timeout=2500)
            except Exception:
                body_text = ""
            normalized = (body_text or "").replace("’", "'").lower()
            if any(
                k in normalized
                for k in (
                    "we weren't able to add this card",
                    "check all the details are correct",
                    "try a different card",
                    "unable to add this card",
                    "无法添加此卡",
                    "添加此卡失败",
                    "请尝试其他卡",
                )
            ):
                return True

            # 文本抓取失败或页面分段渲染时，补一层可见错误提示定位。
            err = page.locator(
                "text=We weren’t able to add this card, "
                "text=We weren't able to add this card, "
                "text=try a different card, "
                "text=无法添加此卡, "
                "text=请尝试其他卡"
            ).first
            if await err.is_visible(timeout=600):
                return True
            return False
        except Exception:
            return False

    # Agree & Create Account；如遇卡被拒，自动生成新卡并重试一次
    env = load_env(".env")
    max_regen = 1
    try:
        max_regen = max(0, int((env.get("PAYPAL_CARD_REGEN_ON_DECLINE") or "1").strip() or "1"))
    except Exception:
        max_regen = 1
    working_card = card
    for regen_idx in range(max_regen + 1):
        create_btn = page.locator('button:has-text("Agree"), button:has-text("Create Account"), button[type="submit"]').first
        try:
            await create_btn.click(timeout=10000)
        except Exception as exc:
            if _looks_like_captcha_pointer_block(exc):
                log("[PayPal] 创建账号按钮被 CAPTCHA 遮挡，处理后重试点击")
                await handle_paypal_captcha(page, solver_proxy=proxy, force=True)
                await _wait_captcha_cleared(page, timeout_seconds=30)
                await create_btn.click(timeout=10000)
            else:
                raise
        await page.wait_for_timeout(2500)

        # 检测并处理人机验证码（PayPal 安全问题 / CAPTCHA）
        await handle_paypal_captcha(page, solver_proxy=proxy)
        await page.wait_for_timeout(1200)

        rejected = False
        for _ in range(6):
            if await _is_card_rejected():
                rejected = True
                break
            await page.wait_for_timeout(800)
        if rejected:
            if regen_idx >= max_regen:
                raise RuntimeError("银行卡被拒（We weren’t able to add this card），且已达到自动换卡上限")
            new_card = _generate_local_random_card(
                int(time.time() * 1000) + regen_idx + random.randint(1, 9999),
                email,
                env,
            )
            log(f"[PayPal] 检测到卡被拒，自动生成新卡重试 ({regen_idx + 1}/{max_regen})")
            working_card = new_card
            await _refill_card_fields(working_card)
            continue
        break


async def handle_paypal_captcha(page, timeout_seconds: int = 180, solver_proxy: str | None = None, force: bool = False) -> None:
    """检测 PayPal 人机验证码并处理。

    支持两种模式（通过 .env 配置）：
    - PAYPAL_CAPTCHA_MODE=manual（默认）：检测到验证码后暂停等待手动处理
    - PAYPAL_CAPTCHA_MODE=api：调用打码平台 API 自动解决

    .env 配置项：
        PAYPAL_CAPTCHA_MODE=manual          # manual 或 api
        PAYPAL_CAPTCHA_TIMEOUT=180          # 等待超时秒数（手动模式）
        CAPSOLVER_API_KEY=CAP-xxx           # CapSolver API Key（api 模式）
    """
    # 检测是否有验证码弹窗（避免 v3 eval 误判）
    has_captcha, reason = await _detect_captcha_signal(page)
    if not has_captcha and not force:
        return
    if not has_captcha and force:
        has_any_frame = await _has_any_captcha_frame(page)
        if not has_any_frame:
            return
        reason = "force_by_timeout"
        has_captcha = True
    # 连续两次确认，避免“挑战未完全加载”就误触发打码
    await page.wait_for_timeout(1200)
    has_captcha_2, reason_2 = await _detect_captcha_signal(page)
    if not has_captcha_2:
        # force 模式下，如果 iframe 仍在，继续解码而不是直接跳过
        if force and await _has_any_captcha_frame(page):
            has_captcha_2 = True
            reason_2 = "force_frame_present"
        else:
            log(f"[PayPal] CAPTCHA 预检未稳定（首次={reason}, 二次={reason_2}），跳过本次自动解码")
            return

    log(f"[PayPal] ⚠️ 检测到人机验证码（CAPTCHA），需要处理... reason={reason_2 or reason}")

    env = load_env(".env")
    mode = (env.get("PAYPAL_CAPTCHA_MODE") or "manual").strip().lower()

    if mode == "api":
        await _solve_captcha_via_api(page, env, solver_proxy=solver_proxy)
    else:
        await _wait_captcha_manual(page, timeout_seconds)


async def _detect_captcha(page) -> bool:
    """检测页面是否出现了验证码弹窗。"""
    ok, _ = await _detect_captcha_signal(page)
    return ok


async def _detect_captcha_signal(page) -> tuple[bool, str]:
    """返回 (是否应触发解码, 原因)。"""
    try:
        result = await page.evaluate("""() => {
            const isVisible = (el) => {
                if (!el) return false;
                const rect = el.getBoundingClientRect();
                if (rect.width < 20 || rect.height < 20) return false;
                const style = window.getComputedStyle(el);
                if (!style) return false;
                if (style.display === 'none' || style.visibility === 'hidden') return false;
                if (Number(style.opacity || '1') < 0.05) return false;
                return true;
            };

            const text = (document.body?.innerText || '').replace(/\\s+/g, ' ');
            // PayPal 验证码特征
            const hasCaptchaText = /安全问题|security challenge|请选择包含|select all images|人行横道|crosswalk|traffic light|bus|bicycle|i'?m not a robot|robot check/i.test(text);

            const frames = Array.from(document.querySelectorAll('iframe'));
            const frameInfos = frames.map((f) => {
                const src = (f.getAttribute('src') || '').toLowerCase();
                const title = (f.getAttribute('title') || '').toLowerCase();
                const rect = f.getBoundingClientRect();
                return { src, title, w: rect.width, h: rect.height, visible: isVisible(f) };
            });

            const hasVisibleV2OrChallengeFrame = frameInfos.some((x) => {
                if (!x.visible) return false;
                const isV3Eval = /recaptcha_v3|source=recaptchav3eval/.test(x.src);
                if (isV3Eval) return false;
                return /api2\\/bframe|api2\\/anchor|recaptcha_v2|hcaptcha|challenge/.test(x.src + ' ' + x.title) && x.w >= 140 && x.h >= 60;
            });

            const hasVisiblePaypalChallenge = Array.from(
                document.querySelectorAll('[data-testid="captcha"], .captcha-container, #captcha, [class*="challenge" i], [class*="captcha" i]')
            ).some((el) => isVisible(el));

            const recaptchaFrames = frameInfos.filter((x) => /recaptcha/.test(x.src));
            const onlyV3EvalFrames =
                recaptchaFrames.length > 0 &&
                recaptchaFrames.every((x) => /recaptcha_v3|source=recaptchav3eval/.test(x.src));

            let reason = 'none';
            if (hasVisibleV2OrChallengeFrame) reason = 'visible_challenge_frame';
            else if (hasVisiblePaypalChallenge) reason = 'visible_paypal_challenge';
            else if (hasCaptchaText && !onlyV3EvalFrames) reason = 'captcha_text';
            else if (onlyV3EvalFrames) reason = 'v3_eval_only';

            const shouldSolve =
                hasVisibleV2OrChallengeFrame ||
                hasVisiblePaypalChallenge ||
                (hasCaptchaText && !onlyV3EvalFrames);

            return { shouldSolve, reason };
        }""")
        if isinstance(result, dict):
            return bool(result.get("shouldSolve")), str(result.get("reason") or "")
        return bool(result), ""
    except Exception:
        return False, "detect_error"


async def _has_any_captcha_frame(page) -> bool:
    """宽松检测：只要页面存在 captcha 相关 iframe 就返回 True（用于超时兜底）。"""
    try:
        return bool(await page.evaluate("""() => {
            return !!document.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="captcha"], iframe[title*="challenge" i]');
        }"""))
    except Exception:
        return False


def _looks_like_captcha_pointer_block(exc: Exception | str) -> bool:
    text = str(exc or "").lower()
    return ("intercepts pointer events" in text) and (
        "recaptcha" in text or "hcaptcha" in text or "captcha" in text
    )


async def _fill_visible_tel_inputs_direct(target, code: str) -> bool:
    """不依赖 click，直接向可见 tel 输入框写入验证码。target 可是 page 或 frame。"""
    try:
        mode = await target.evaluate(
            """(code) => {
                const isVisible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 8 || r.height < 8) return false;
                    const st = window.getComputedStyle(el);
                    if (!st) return false;
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    if (Number(st.opacity || '1') < 0.05) return false;
                    return true;
                };
                const setVal = (el, v) => {
                    const proto = HTMLInputElement.prototype;
                    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                    if (desc && typeof desc.set === 'function') desc.set.call(el, v);
                    else el.value = v;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                };
                const inputs = Array.from(document.querySelectorAll('input[type="tel"]')).filter(isVisible);
                if (!inputs.length) return "";

                // 单输入框：整串写入
                if (inputs.length === 1) {
                    setVal(inputs[0], String(code));
                    inputs[0].focus();
                    return "single_direct";
                }
                // 多输入格：逐位写入
                const digits = String(code).replace(/\\D/g, "").slice(0, inputs.length).split("");
                if (!digits.length) return "";
                for (let i = 0; i < digits.length; i++) {
                    setVal(inputs[i], digits[i]);
                }
                inputs[Math.min(digits.length - 1, inputs.length - 1)].focus();
                return "multi_direct";
            }""",
            str(code),
        )
        return bool(mode)
    except Exception:
        return False


async def _fill_paypal_otp_inputs_direct(target, code: str) -> bool:
    """匹配 PayPal OTP 输入框并直填；支持 ciBasic 与几何分组兜底。"""
    try:
        mode = await target.evaluate(
            """(rawCode) => {
                const code = String(rawCode || '').replace(/\\D/g, '');
                if (!code) return '';
                const isVisible = (el) => {
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    if (r.width < 8 || r.height < 8) return false;
                    const st = window.getComputedStyle(el);
                    if (!st) return false;
                    if (st.display === 'none' || st.visibility === 'hidden') return false;
                    if (Number(st.opacity || '1') < 0.05) return false;
                    return true;
                };
                const setVal = (el, v) => {
                    const proto = HTMLInputElement.prototype;
                    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                    if (desc && typeof desc.set === 'function') desc.set.call(el, v);
                    else el.value = v;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: v }));
                };
                const idx = (el) => {
                    const id = String(el.id || '');
                    const name = String(el.name || '');
                    const m = id.match(/ciBasic-(\\d+)$/i) || name.match(/ciBasic-(\\d+)$/i);
                    return m ? Number(m[1]) : 9999;
                };
                const looksOtp = (el) => {
                    const id = String(el.id || '').toLowerCase();
                    const name = String(el.name || '').toLowerCase();
                    const cls = String(el.className || '').toLowerCase();
                    const aria = String(el.getAttribute('aria-label') || '').toLowerCase();
                    const ac = String(el.getAttribute('autocomplete') || '').toLowerCase();
                    return id.includes('cibasic') || name.includes('cibasic') ||
                        cls.includes('code_input') || ac.includes('one-time-code') ||
                        /\\b1\\s*-\\s*6\\b/.test(aria) || aria.includes('code');
                };
                let inputs = Array.from(
                    document.querySelectorAll('input[type="tel"][id*="ciBasic" i], input[type="tel"][name*="ciBasic" i]')
                ).filter(isVisible);
                if (inputs.length < 6) {
                    const all = Array.from(document.querySelectorAll('input[type="tel"], input[inputmode="numeric"], input[autocomplete="one-time-code"]'))
                        .filter(isVisible)
                        .map((el) => ({ el, r: el.getBoundingClientRect(), otp: looksOtp(el) }))
                        .filter((x) => x.r.width >= 20 && x.r.width <= 120 && x.r.height >= 25 && x.r.height <= 90);

                    const groups = new Map();
                    for (const item of all) {
                        const key = String(Math.round(item.r.top / 12) * 12);
                        if (!groups.has(key)) groups.set(key, []);
                        groups.get(key).push(item);
                    }

                    let best = [];
                    for (const group of groups.values()) {
                        const sorted = group.sort((a, b) => a.r.left - b.r.left);
                        const otpScore = sorted.filter((x) => x.otp).length;
                        if (sorted.length >= 6 && (otpScore >= 2 || sorted.every((x) => x.r.width <= 80))) {
                            if (sorted.length > best.length || otpScore > best.filter((x) => x.otp).length) {
                                best = sorted;
                            }
                        }
                    }
                    inputs = best.slice(0, 6).map((x) => x.el);
                }
                inputs.sort((a, b) => idx(a) - idx(b));
                // 只在 OTP 输入框数量足够时启用，避免误命中少量非 OTP tel 框导致“部分填入”
                if (inputs.length < 6) return '';
                if (idx(inputs[0]) === 9999) {
                    inputs.sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
                }
                const digits = code.slice(0, 6).split('');
                if (digits.length < 6) return '';
                for (let i = 0; i < 6; i++) setVal(inputs[i], digits[i]);

                // 回读校验：必须 6 位都写入，才算成功
                const compact = inputs.slice(0, 6).map((el) => String(el.value || '')).join('');
                if (compact.length < 6) return '';
                return `paypal_otp_6`;
            }""",
            str(code),
        )
        return bool(mode)
    except Exception:
        return False


async def _wait_captcha_manual(page, timeout_seconds: int = 180) -> None:
    """方案1：暂停等待手动处理验证码。

    检测到验证码后每 3 秒检查一次是否已消失，最多等待 timeout_seconds 秒。
    用户手动完成验证码后脚本自动继续。
    """
    log(f"[PayPal] 🖐️ 请手动完成验证码！等待最多 {timeout_seconds} 秒...")
    log("[PayPal] 完成验证码后脚本会自动继续")

    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout_seconds:
        await page.wait_for_timeout(3000)
        # 检查验证码是否已消失
        still_has = await _detect_captcha(page)
        if not still_has:
            log("[PayPal] ✓ 验证码已完成，继续流程")
            await page.wait_for_timeout(2000)
            return
        # 检查是否已经跳转到下一页（验证码通过后可能直接跳转）
        try:
            body_text = await page.evaluate("() => (document.body?.innerText || '').slice(0, 500)")
            if "code" in body_text.lower() or "验证码" in body_text or "verify" in body_text.lower():
                log("[PayPal] ✓ 页面已跳转到验证码/下一步页面，继续流程")
                return
        except Exception:
            # 页面可能在导航
            await page.wait_for_timeout(2000)
            return

    log("[PayPal] ⚠️ 验证码等待超时，继续尝试...")


async def _inject_recaptcha_token(page, token: str) -> None:
    """向页面注入 reCAPTCHA token，并尽可能触发回调。"""
    inject_js = """(token) => {
        const touch = (el) => {
            if (!el) return;
            el.value = token;
            el.innerHTML = token;
            try { el.dispatchEvent(new Event('input', { bubbles: true })); } catch {}
            try { el.dispatchEvent(new Event('change', { bubbles: true })); } catch {}
        };

        const ensureField = (selector, id, name) => {
            let el = document.querySelector(selector);
            if (!el) {
                el = document.createElement('textarea');
                if (id) el.id = id;
                if (name) el.name = name;
                el.style.display = 'none';
                document.body.appendChild(el);
            }
            touch(el);
            return el;
        };

        ensureField('#g-recaptcha-response', 'g-recaptcha-response', 'g-recaptcha-response');
        ensureField('textarea[name="g-recaptcha-response"]', '', 'g-recaptcha-response');
        ensureField('textarea[name="g-recaptcha-response-100000"]', '', 'g-recaptcha-response-100000');

        for (const el of Array.from(document.querySelectorAll('[name*="captcha-response" i], textarea[id*="captcha" i]'))) {
            touch(el);
        }

        for (const cbEl of Array.from(document.querySelectorAll('[data-callback]'))) {
            const cb = cbEl.getAttribute('data-callback');
            if (!cb) continue;
            const fn = window[cb];
            if (typeof fn === 'function') {
                try { fn(token); } catch {}
            }
        }

        if (window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) {
            const clients = window.___grecaptcha_cfg.clients;
            for (const cid of Object.keys(clients)) {
                const stack = [clients[cid]];
                while (stack.length) {
                    const obj = stack.pop();
                    if (!obj || typeof obj !== 'object') continue;
                    for (const k of Object.keys(obj)) {
                        const v = obj[k];
                        if (typeof v === 'function' && /callback/i.test(k)) {
                            try { v(token); } catch {}
                        } else if (v && typeof v === 'object') {
                            stack.push(v);
                        }
                    }
                }
            }
        }
    }"""

    await page.evaluate(inject_js, token)
    for frame in page.frames:
        try:
            u = (frame.url or "").lower()
            if "recaptcha" in u or "paypal.com" in u:
                await frame.evaluate(inject_js, token)
        except Exception:
            pass


async def _inject_hcaptcha_token(page, token: str) -> None:
    """向页面注入 hCaptcha token，并尽可能触发回调。"""
    inject_js = """(token) => {
        const touch = (el) => {
            if (!el) return;
            el.value = token;
            el.innerHTML = token;
            try { el.dispatchEvent(new Event('input', { bubbles: true })); } catch {}
            try { el.dispatchEvent(new Event('change', { bubbles: true })); } catch {}
        };

        const ensureField = (selector, name) => {
            let el = document.querySelector(selector);
            if (!el) {
                el = document.createElement('textarea');
                if (name) el.name = name;
                el.style.display = 'none';
                document.body.appendChild(el);
            }
            touch(el);
        };

        ensureField('textarea[name="h-captcha-response"]', 'h-captcha-response');
        ensureField('textarea[name="g-recaptcha-response"]', 'g-recaptcha-response');
        for (const el of Array.from(document.querySelectorAll('[name*="captcha-response" i], textarea[id*="captcha" i]'))) {
            touch(el);
        }
        for (const cbEl of Array.from(document.querySelectorAll('[data-callback]'))) {
            const cb = cbEl.getAttribute('data-callback');
            if (!cb) continue;
            const fn = window[cb];
            if (typeof fn === 'function') {
                try { fn(token); } catch {}
            }
        }
        if (window.hcaptcha && typeof window.hcaptcha.execute === 'function') {
            try { window.hcaptcha.execute(); } catch {}
        }
    }"""

    await page.evaluate(inject_js, token)
    for frame in page.frames:
        try:
            u = (frame.url or "").lower()
            if "hcaptcha" in u or "paypal.com" in u:
                await frame.evaluate(inject_js, token)
        except Exception:
            pass


async def _wait_captcha_cleared(page, timeout_seconds: int = 45) -> bool:
    """等待验证码真正消失，或页面进入下一步（短信/验证页）。"""
    start = asyncio.get_event_loop().time()
    while asyncio.get_event_loop().time() - start < timeout_seconds:
        await page.wait_for_timeout(1500)
        if not await _detect_captcha(page):
            return True
        try:
            body = await page.locator("body").inner_text(timeout=1500)
            low = body.lower()
            if ("verification code" in low) or ("enter code" in low) or ("验证码" in body):
                return True
        except Exception:
            # 页面导航瞬间读取失败也视作可能前进，继续下一轮
            pass
    return False


async def _is_paypal_verification_stage(page) -> bool:
    """判断是否已进入 PayPal 短信验证码阶段（用于避免残留 iframe 误判）。"""
    try:
        body = await page.locator("body").inner_text(timeout=1500)
        low = body.lower()
        markers = (
            "verification code",
            "enter code",
            "security code",
            "one-time code",
            "text message",
            "sms code",
            "验证码",
        )
        if any(m in low for m in markers):
            return True
    except Exception:
        pass

    try:
        if await page.locator('input[type="tel"]:visible').count() > 0:
            return True
    except Exception:
        pass

    try:
        for fr in page.frames:
            if fr is page.main_frame:
                continue
            try:
                if await fr.locator('input[type="tel"]:visible').count() > 0:
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return False


async def _post_captcha_nudge(page) -> None:
    """token 注入后，尝试触发页面继续动作（部分站点需要 callback 后再点按钮）。"""
    selectors = [
        'button:has-text("Continue")',
        'button:has-text("Verify")',
        'button:has-text("Submit")',
        'button:has-text("Next")',
        'button:has-text("下一步")',
        'button:has-text("继续")',
        'button[type="submit"]',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1200):
                await btn.click(timeout=2000)
                log(f"[PayPal] 验证码后触发按钮: {sel}")
                await page.wait_for_timeout(800)
                return
        except Exception:
            continue


async def _solve_captcha_via_api(page, env: dict[str, str], solver_proxy: str | None = None) -> None:
    """方案2：调用打码平台 API 自动解决验证码。

    目前支持 CapSolver / CaptchaAI / 2Captcha。

    .env 配置：
        CAPSOLVER_API_KEY=CAP-xxxxxxxx     # CapSolver API Key
        CAPTCHA_API_PROVIDER=capsolver      # capsolver / captchaai / twocaptcha
        TWOCAPTCHA_API_KEY=xxxxxxxx         # 2Captcha API Key（如果用 2Captcha）
        CAPTCHAAI_KEY=xxxxxxxx              # CaptchaAI API Key（如果用 captchaai）

    工作原理：
    1. 检测验证码类型（reCAPTCHA v2 / hCaptcha / 图片验证码）
    2. 提取 sitekey 和页面 URL
    3. 发送到打码平台
    4. 等待返回 token
    5. 将 token 注入页面回调函数
    """
    provider = (env.get("CAPTCHA_API_PROVIDER") or "capsolver").strip().lower()

    if provider == "capsolver":
        api_key = (env.get("CAPSOLVER_API_KEY") or "").strip()
        if not api_key:
            log("[PayPal] CAPSOLVER_API_KEY 未配置，回退到手动模式")
            await _wait_captcha_manual(page)
            return
        await _solve_with_capsolver(page, api_key)
    elif provider == "captchaai":
        api_key = (env.get("CAPTCHAAI_KEY") or env.get("CAPTCHAAI_API_KEY") or "").strip()
        if not api_key:
            log("[PayPal] CAPTCHAAI_KEY 未配置，回退到手动模式")
            await _wait_captcha_manual(page)
            return
        await _solve_with_captchaai(page, api_key, env, solver_proxy=solver_proxy)
    elif provider == "twocaptcha":
        api_key = (env.get("TWOCAPTCHA_API_KEY") or "").strip()
        if not api_key:
            log("[PayPal] TWOCAPTCHA_API_KEY 未配置，回退到手动模式")
            await _wait_captcha_manual(page)
            return
        await _solve_with_twocaptcha(page, api_key)
    else:
        log(f"[PayPal] 未知打码平台: {provider}，回退到手动模式")
        await _wait_captcha_manual(page)


async def _extract_captcha_info(page) -> dict[str, str]:
    """提取验证码参数，支持 reCAPTCHA v2 / v3 与 hCaptcha。"""
    return await page.evaluate("""() => {
        const pickQuery = (src, keys) => {
            try {
                const u = new URL(src, location.href);
                const want = new Set((keys || []).map(k => String(k).toLowerCase()));
                for (const [k, v] of u.searchParams.entries()) {
                    if (want.has(String(k).toLowerCase()) && (v || '').trim()) return String(v).trim();
                }
            } catch {}
            return '';
        };
        const isEnterprise = !!window.grecaptcha?.enterprise || !!document.querySelector('script[src*="recaptcha/enterprise"]');
        const frames = Array.from(document.querySelectorAll('iframe[src]')).map(f => f.getAttribute('src') || '');

        const recFrames = frames.filter(src => /recaptcha/i.test(src));
        if (recFrames.length) {
            // 优先选真正 challenge 相关的 v2 iframe，避免误用 v3 eval key
            let rec = recFrames.find(src => /recaptcha_v2|api2\\/anchor|api2\\/bframe/i.test(src));
            if (!rec) {
                rec = recFrames.find(src => /[?&](k|sitekey|siteKey)=/i.test(src)) || recFrames[0];
            }
            const low = rec.toLowerCase();
            const siteKey = pickQuery(rec, ['k', 'sitekey', 'siteKey', 'render']);
            const dataS = pickQuery(rec, ['s', 'data-s']);
            const action = pickQuery(rec, ['action']) || 'verify';
            const isV3 = /recaptcha_v3/.test(low) || (/api\\.js/.test(low) && /(?:\\?|&)render=/.test(low));
            const isInvisible = /(?:\\?|&)size=invisible(?:&|$)/.test(low) || /invisible/.test(low);
            return {
                provider: 'recaptcha',
                version: isV3 ? 'v3' : 'v2',
                enterprise: isEnterprise ? '1' : '0',
                siteKey: siteKey ? decodeURIComponent(siteKey) : '',
                pageurl: rec,
                dataS,
                action,
                invisible: (isV3 || isInvisible) ? '1' : '0',
            };
        }

        const hc = frames.find(src => /hcaptcha/i.test(src));
        if (hc) {
            const siteKey = pickQuery(hc, ['sitekey', 'siteKey']);
            return {
                provider: 'hcaptcha',
                version: 'v2',
                enterprise: '0',
                siteKey: siteKey ? decodeURIComponent(siteKey) : '',
                pageurl: hc,
                dataS: '',
                action: 'verify',
                invisible: '0',
            };
        }

        const el = document.querySelector('[data-sitekey]');
        if (el) {
            const siteKey = el.getAttribute('data-sitekey') || '';
            const isHcaptcha = !!document.querySelector('.h-captcha, [data-hcaptcha-widget-id]');
            return {
                provider: isHcaptcha ? 'hcaptcha' : 'recaptcha',
                version: 'v2',
                enterprise: isEnterprise ? '1' : '0',
                siteKey,
                pageurl: location.href,
                dataS: '',
                action: 'verify',
                invisible: '0',
            };
        }

        return {
            provider: 'unknown',
            version: 'v2',
            enterprise: '0',
            siteKey: '',
            pageurl: location.href,
            dataS: '',
            action: 'verify',
            invisible: '0',
        };
    }""")


async def _get_recaptcha_frame_hints(page) -> list[str]:
    """返回页面中 reCAPTCHA 相关 iframe 的 src（用于调试版本识别）。"""
    try:
        return await page.evaluate("""() => {
            return Array.from(document.querySelectorAll('iframe[src]'))
                .map(f => f.getAttribute('src') || '')
                .filter(src => /recaptcha/i.test(src))
                .slice(0, 8);
        }""")
    except Exception:
        return []


def _parse_solver_proxy(proxy: str | None) -> tuple[str, str] | tuple[None, None]:
    """把浏览器代理转换成打码平台 proxy/proxytype 参数。"""
    if not proxy:
        return None, None
    raw = proxy.strip()
    if not raw:
        return None, None
    if "://" not in raw:
        raw = f"http://{raw}"
    try:
        u = urlparse(raw)
        scheme = (u.scheme or "http").lower()
        host = u.hostname or ""
        port = u.port
        if not host or not port:
            return None, None
        auth = ""
        if u.username:
            auth = u.username
            if u.password:
                auth += f":{u.password}"
            auth += "@"
        solver_proxy = f"{auth}{host}:{port}"
        proxy_type = "SOCKS5" if "socks5" in scheme else "HTTP"
        return solver_proxy, proxy_type
    except Exception:
        return None, None


async def _solve_with_captchaai(page, api_key: str, env: dict[str, str], solver_proxy: str | None = None) -> None:
    """使用 CaptchaAI 解决验证码（支持 reCAPTCHA v2/v3 + hCaptcha）。"""
    import recaptcha_solver as rs

    log("[PayPal] 使用 CaptchaAI 打码平台...")
    info = await _extract_captcha_info(page)
    provider = info.get("provider", "unknown")
    version = info.get("version", "v2")
    force_v2 = (env.get("PAYPAL_CAPTCHA_FORCE_V2") or "").strip().lower() in ("1", "true", "yes")
    if force_v2 and version == "v3":
        version = "v2"
    enterprise = info.get("enterprise") == "1"
    invisible = info.get("invisible") == "1"
    site_key = info.get("siteKey", "")
    page_url = info.get("pageurl") or page.url
    action = info.get("action") or "verify"
    data_s = info.get("dataS") or ""

    if not site_key or provider not in {"recaptcha", "hcaptcha"}:
        log(f"[PayPal] CaptchaAI 未提取到可用 sitekey/provider，回退到手动模式 | provider={provider}")
        await _wait_captcha_manual(page)
        return
    if provider == "hcaptcha":
        page_url = page.url

    proxy_for_solver, proxy_type = _parse_solver_proxy(solver_proxy)
    hints = await _get_recaptcha_frame_hints(page)
    if hints:
        log(f"[PayPal] CAPTCHA frame hints: {' | '.join(hints)}")
    log(
        f"[PayPal] 本地求解参数: provider={provider} version={version} enterprise={enterprise} invisible={invisible} "
        f"proxy={bool(proxy_for_solver)} sitekey={site_key[:20]}... pageurl={str(page_url)[:120]}"
    )
    token = ""
    try:
        server_retry = 2
        try:
            server_retry = max(0, int((env.get("PAYPAL_CAPTCHAAI_SERVER_RETRY") or "2").strip() or "2"))
        except Exception:
            server_retry = 2
        total_attempts = server_retry + 1
        for attempt in range(1, total_attempts + 1):
            try:
                if provider == "hcaptcha":
                    token = await asyncio.to_thread(
                        rs.solve_hcaptcha,
                        api_key,
                        site_key,
                        page_url,
                        180,
                        20,
                        5,
                        invisible,
                        proxy_for_solver or "",
                        proxy_type or "HTTP",
                    )
                elif version == "v3":
                    min_score = 0.3
                    try:
                        min_score = float((env.get("PAYPAL_CAPTCHA_V3_MIN_SCORE") or "0.3").strip() or "0.3")
                    except Exception:
                        min_score = 0.3
                    token = await asyncio.to_thread(
                        rs.solve_recaptcha_v3,
                        api_key, site_key, page_url, action, min_score, enterprise, 180
                    )
                else:
                    token = await asyncio.to_thread(
                        rs.solve_recaptcha_v2,
                        api_key, site_key, page_url, invisible, enterprise, 180, 20, 5, data_s, proxy_for_solver, proxy_type
                    )
                break
            except Exception as exc:
                err = str(exc or "")
                if ("ERROR_SERVER_ERROR" in err or "SERVER_ERROR" in err) and attempt < total_attempts:
                    log(f"[PayPal] CaptchaAI 服务器错误，自动重试 ({attempt}/{total_attempts - 1})")
                    await page.wait_for_timeout(2500)
                    continue
                raise
    except Exception as exc:
        log(f"[PayPal] CaptchaAI 解题失败: {exc}")
        await _wait_captcha_manual(page)
        return

    if not token:
        log("[PayPal] CaptchaAI 未返回 token，回退到手动模式")
        await _wait_captcha_manual(page)
        return

    log("[PayPal] CaptchaAI 已解决验证码，注入 token...")
    if provider == "hcaptcha":
        await _inject_hcaptcha_token(page, token)
    else:
        await _inject_recaptcha_token(page, token)
    await _post_captcha_nudge(page)
    await page.wait_for_timeout(3000)
    cleared = await _wait_captcha_cleared(page, timeout_seconds=45)
    if cleared:
        log("[PayPal] 验证码 token 已注入，页面已通过/进入下一步")
    else:
        if version == "v3":
            log("[PayPal] v3 token 未通过，尝试按 v2 再求解一次...")
            info2 = await _extract_captcha_info(page)
            provider2 = info2.get("provider", "unknown")
            site_key2 = info2.get("siteKey", "")
            page_url2 = info2.get("pageurl") or page.url
            data_s2 = info2.get("dataS") or ""
            invisible2 = info2.get("invisible") == "1"
            enterprise2 = info2.get("enterprise") == "1"
            if provider2 == "recaptcha" and site_key2:
                try:
                    token2 = await asyncio.to_thread(
                        rs.solve_recaptcha_v2,
                        api_key, site_key2, page_url2, invisible2, enterprise2, 180, 20, 5, data_s2, proxy_for_solver, proxy_type
                    )
                    if token2:
                        log("[PayPal] v2 补解成功，重新注入 token...")
                        await _inject_recaptcha_token(page, token2)
                        await _post_captcha_nudge(page)
                        await page.wait_for_timeout(3000)
                        if await _wait_captcha_cleared(page, timeout_seconds=45):
                            log("[PayPal] v2 补解后验证码已通过")
                            return
                except Exception as exc:
                    log(f"[PayPal] v2 补解失败: {exc}")
        # 官方文档常见问题：pageurl 不匹配会导致 token 注入后仍被拒绝
        # 这里再用“当前父页面 URL”补试一次 v2 求解。
        try:
            parent_url = page.url
            if parent_url and parent_url != page_url:
                log("[PayPal] 尝试切换 pageurl=父页面 重新按 v2 求解...")
                token3 = await asyncio.to_thread(
                    rs.solve_recaptcha_v2,
                    api_key, site_key, parent_url, invisible, enterprise, 180, 20, 5, data_s, proxy_for_solver, proxy_type
                )
                if token3:
                    log("[PayPal] 父页面 pageurl 补解成功，重新注入 token...")
                    await _inject_recaptcha_token(page, token3)
                    await _post_captcha_nudge(page)
                    await page.wait_for_timeout(3000)
                    if await _wait_captcha_cleared(page, timeout_seconds=45):
                        log("[PayPal] 父页面 pageurl 补解后验证码已通过")
                        return
        except Exception as exc:
            log(f"[PayPal] 父页面 pageurl 补解失败: {exc}")
        log("[PayPal] 验证码 token 已注入，但挑战仍存在，回退手动处理")
        await _wait_captcha_manual(page)


async def _solve_with_capsolver(page, api_key: str) -> None:
    """使用 CapSolver 解决验证码（支持 reCAPTCHA v2/v3）。"""
    import httpx

    log("[PayPal] 使用 CapSolver 打码平台...")
    captcha_info = await _extract_captcha_info(page)
    provider = captcha_info.get("provider", "unknown")
    version = captcha_info.get("version", "v2")
    enterprise = captcha_info.get("enterprise") == "1"
    site_key = captcha_info.get("siteKey", "")
    page_url = captcha_info.get("pageurl") or page.url
    action = captcha_info.get("action") or "verify"

    if not site_key or provider == "unknown":
        log("[PayPal] 无法提取验证码 siteKey，回退到手动模式")
        await _wait_captcha_manual(page)
        return

    if provider == "hcaptcha":
        captcha_type = "HCaptchaTaskProxyLess"
    elif version == "v3":
        captcha_type = "ReCaptchaV3TaskProxyLess"
    else:
        captcha_type = "ReCaptchaV2TaskProxyLess"

    log(f"[PayPal] 本地求解参数: provider={provider} enterprise={enterprise} sitekey={site_key[:20]}... pageurl={str(page_url)[:120]}")
    log(f"[PayPal] 验证码类型: {captcha_type}, siteKey: {site_key[:20]}...")

    task: dict[str, Any] = {
        "type": captcha_type,
        "websiteURL": page_url,
        "websiteKey": site_key,
    }
    if captcha_type.startswith("ReCaptcha"):
        task["isEnterprise"] = enterprise
    if captcha_type == "ReCaptchaV3TaskProxyLess":
        min_score = 0.3
        try:
            min_score = float((load_env(".env").get("PAYPAL_CAPTCHA_V3_MIN_SCORE") or "0.3").strip() or "0.3")
        except Exception:
            min_score = 0.3
        task["pageAction"] = action or "verify"
        task["minScore"] = min_score

    create_payload = {"clientKey": api_key, "task": task}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post("https://api.capsolver.com/createTask", json=create_payload)
        result = resp.json()

    if result.get("errorId"):
        log(f"[PayPal] CapSolver 创建任务失败: {result.get('errorDescription', result)}")
        await _wait_captcha_manual(page)
        return

    task_id = result.get("taskId")
    if not task_id:
        log(f"[PayPal] CapSolver 未返回 taskId: {result}")
        await _wait_captcha_manual(page)
        return

    log(f"[PayPal] CapSolver 任务已创建: {task_id}，等待解决...")

    for _ in range(60):
        await page.wait_for_timeout(3000)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://api.capsolver.com/getTaskResult",
                json={"clientKey": api_key, "taskId": task_id},
            )
            result = resp.json()

        status = result.get("status", "")
        if status == "ready":
            solution = result.get("solution", {})
            token = solution.get("gRecaptchaResponse") or solution.get("token") or ""
            if not token:
                log(f"[PayPal] CapSolver 返回 ready 但无 token: {result}")
                await _wait_captcha_manual(page)
                return

            log("[PayPal] CapSolver 已解决验证码，注入 token...")
            if "ReCaptcha" in captcha_type:
                await _inject_recaptcha_token(page, token)
            else:
                await page.evaluate("""(token) => {
                    const ta = document.querySelector('[name="h-captcha-response"], textarea[name="g-recaptcha-response"]');
                    if (ta) ta.value = token;
                    if (window.hcaptcha) {
                        try { window.hcaptcha.execute(); } catch {}
                    }
                }""", token)
            await page.wait_for_timeout(3000)
            cleared = await _wait_captcha_cleared(page, timeout_seconds=45)
            if cleared:
                log("[PayPal] 验证码 token 已注入，页面已通过/进入下一步")
            else:
                log("[PayPal] 验证码 token 已注入，但挑战仍存在，回退手动处理")
                await _wait_captcha_manual(page)
            return

        if status == "failed":
            log(f"[PayPal] CapSolver 解题失败: {result.get('errorDescription', '')}")
            await _wait_captcha_manual(page)
            return

    log("[PayPal] CapSolver 等待超时（180s），回退到手动模式")
    await _wait_captcha_manual(page)
async def _solve_with_twocaptcha(page, api_key: str) -> None:
    """使用 2Captcha 解决验证码（支持 reCAPTCHA v2/v3）。"""
    import httpx

    log("[PayPal] 使用 2Captcha 打码平台...")

    captcha_info = await _extract_captcha_info(page)
    provider = captcha_info.get("provider", "unknown")
    version = captcha_info.get("version", "v2")
    enterprise = captcha_info.get("enterprise") == "1"
    site_key = captcha_info.get("siteKey", "")
    page_url = captcha_info.get("pageurl") or page.url
    action = captcha_info.get("action") or "verify"

    if not site_key or provider != "recaptcha":
        log("[PayPal] 无法提取 reCAPTCHA siteKey，回退到手动模式")
        await _wait_captcha_manual(page)
        return

    log(f"[PayPal] 本地求解参数: provider=recaptcha enterprise={enterprise} sitekey={site_key[:20]}... pageurl={str(page_url)[:120]}")

    submit_params = {
        "key": api_key,
        "method": "userrecaptcha",
        "googlekey": site_key,
        "pageurl": page_url,
        "json": 1,
    }
    if enterprise:
        submit_params["enterprise"] = 1
    if version == "v3":
        submit_params["version"] = "v3"
        submit_params["action"] = action or "verify"
        submit_params["min_score"] = (load_env(".env").get("PAYPAL_CAPTCHA_V3_MIN_SCORE") or "0.3").strip() or "0.3"

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get("https://2captcha.com/in.php", params=submit_params)
        result = resp.json()

    if result.get("status") != 1:
        log(f"[PayPal] 2Captcha 提交失败: {result}")
        await _wait_captcha_manual(page)
        return

    request_id = result.get("request")
    log(f"[PayPal] 2Captcha 任务已提交: {request_id}，等待解决...")

    for _ in range(40):
        await page.wait_for_timeout(5000)
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get("https://2captcha.com/res.php", params={
                "key": api_key,
                "action": "get",
                "id": request_id,
                "json": 1,
            })
            result = resp.json()

        if result.get("status") == 1:
            token = result.get("request", "")
            log("[PayPal] 2Captcha 已解决验证码，注入 token...")
            await _inject_recaptcha_token(page, token)
            await page.wait_for_timeout(3000)
            cleared = await _wait_captcha_cleared(page, timeout_seconds=45)
            if cleared:
                log("[PayPal] 验证码 token 已注入，页面已通过/进入下一步")
            else:
                log("[PayPal] 验证码 token 已注入，但挑战仍存在，回退手动处理")
                await _wait_captcha_manual(page)
            return

        if "CAPCHA_NOT_READY" not in str(result.get("request", "")):
            log(f"[PayPal] 2Captcha 失败: {result}")
            await _wait_captcha_manual(page)
            return

    log("[PayPal] 2Captcha 等待超时，回退到手动模式")
    await _wait_captcha_manual(page)
async def fill_sms_code(
    page,
    api_url: str,
    solver_proxy: str | None = None,
    *,
    prefix: str = "[PayPal]",
) -> None:
    """等待并填入 PayPal 手机验证码（复刻 source4 逻辑）。"""
    await page.wait_for_timeout(800)
    body = await page.locator("body").inner_text(timeout=5000)
    if "code" not in body.lower() and "验证" not in body:
        return

    # 二次风控常发生在短信页刚加载时，先做一次预处理。
    has_captcha_pre, reason_pre = await _detect_captcha_signal(page)
    if has_captcha_pre:
        log(f"[PayPal] 短信验证码页检测到 CAPTCHA，先处理... reason={reason_pre}")
        await handle_paypal_captcha(page, solver_proxy=solver_proxy)
        await _wait_captcha_cleared(page, timeout_seconds=30)

    code = poll_sms_code(api_url, timeout=120, interval=5)
    log(f"[PayPal] 验证码: {code}")

    # 优先使用直填，避免 click 被遮挡导致输入中断。
    typed = False
    if await _fill_paypal_otp_inputs_direct(page, code):
        typed = True

    if not typed:
        all_tel = page.locator('input[type="tel"]:visible')
        tel_count = await all_tel.count()
        for i in range(tel_count):
            val = await all_tel.nth(i).input_value()
            if not val:
                try:
                    await all_tel.nth(i).click(timeout=2000)
                except Exception as exc:
                    if _looks_like_captcha_pointer_block(exc):
                        log("[PayPal] 验证码输入框被 CAPTCHA 遮挡，处理后重试点击")
                        await handle_paypal_captcha(page, solver_proxy=solver_proxy, force=True)
                        await _wait_captcha_cleared(page, timeout_seconds=30)
                        await all_tel.nth(i).click(timeout=3000)
                    else:
                        raise
                await page.wait_for_timeout(300)
                for digit in code:
                    await page.keyboard.press(digit)
                    await page.wait_for_timeout(200)
                typed = True
                break

    # 快速路径：验证码写入后先短暂等待，尽快进入提交/确认点击
    await page.wait_for_timeout(500)
    # 输入后再次出现验证码时，先处理再点提交。
    has_captcha_post, reason_post = await _detect_captcha_signal(page)
    if has_captcha_post:
        log(f"[PayPal] 验证码输入后检测到 CAPTCHA，处理后再提交... reason={reason_post}")
        await handle_paypal_captcha(page, solver_proxy=solver_proxy)
        await _wait_captcha_cleared(page, timeout_seconds=30)

    try:
        btn = page.locator('button:has-text("Confirm"), button:has-text("Submit"), button:has-text("Verify"), button[type="submit"]').first
        if await btn.is_visible(timeout=3000):
            try:
                await btn.click()
            except Exception as exc:
                    if _looks_like_captcha_pointer_block(exc):
                        log("[PayPal] 提交按钮被 CAPTCHA 遮挡，处理后重试提交")
                        await handle_paypal_captcha(page, solver_proxy=solver_proxy, force=True)
                        await _wait_captcha_cleared(page, timeout_seconds=30)
                        await btn.click(timeout=3000)
                    else:
                        raise
    except Exception:
        pass

    # 提交后立刻尝试点击确认，缩短“收到验证码 -> 点击 Agree and Continue”耗时。
    await _click_paypal_agree_and_continue_if_present(page, solver_proxy=solver_proxy, prefix=prefix)


async def check_phone_rejected(page) -> bool:
    """检查是否出现 'Try a different phone number' 弹窗。"""
    try:
        body = await page.locator("body").inner_text(timeout=2000)
        if "different phone" in body.lower() or "Try a different" in body:
            # 点 OK 关闭弹窗
            try:
                await page.locator('button:has-text("OK")').first.click(timeout=3000)
            except Exception:
                pass
            await page.wait_for_timeout(1000)
            return True
    except Exception:
        pass
    return False


async def _click_paypal_agree_and_continue_if_present(
    page,
    *,
    solver_proxy: str | None = None,
    prefix: str = "[PayPal]",
) -> bool:
    """若出现 PayPal review 页的 Agree and Continue，则自动点击。"""
    selectors = [
        'button:has-text("Agree and Continue")',
        'button:has-text("Agree & Continue")',
        'button:has-text("同意并继续")',
        'button:has-text("继续并同意")',
        'button:has-text("继续")',
    ]
    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1200):
                # 页面常在底部，先滚动到按钮再点击
                try:
                    await btn.scroll_into_view_if_needed(timeout=1200)
                except Exception:
                    pass
                try:
                    await btn.click(timeout=5000)
                except Exception as exc:
                    if _looks_like_captcha_pointer_block(exc):
                        log(f"{prefix} Agree and Continue 按钮被 CAPTCHA 遮挡，处理后重试")
                        await handle_paypal_captcha(page, solver_proxy=solver_proxy, force=True)
                        await _wait_captcha_cleared(page, timeout_seconds=30)
                        await btn.click(timeout=5000)
                    else:
                        raise
                log(f"{prefix} 已点击支付确认按钮: {sel}")
                await page.wait_for_timeout(1500)
                return True
        except Exception:
            continue
    return False


async def pay_one(
    item: dict[str, str],
    card: CardInfo,
    phone_pool: PhonePool,
    cfg: dict[str, Any],
    worker_id: int = 1,
    max_phone_retries: int = 3,
    proxy: str | None = None,
) -> bool:
    """执行一次 PayPal 支付。"""
    email = item["email"]
    query_code = item["query_code"]
    payment_link = item["payment_link"]
    prefix = f"[paypal-pay-{worker_id:02d}][{email}]"
    paypal_password = generate_paypal_password(email)

    browser_cfg = cfg.get("browser", {})
    profile_dir = resolve_path("profiles") / f"paypal_pay_{safe_filename(email)}"

    if proxy:
        log(f"{prefix} 使用代理: {_display_proxy(proxy)}")

    session = BrowserSession(
        profile_dir=profile_dir,
        headless=bool(browser_cfg.get("headless", False)),
        slow_mo=int(browser_cfg.get("slow_mo", 80)),
        timeout_ms=int(browser_cfg.get("timeout_ms", 60000)),
        proxy=proxy,
        fingerprint_seed=email,
    )

    phone: PhoneInfo | None = None
    try:
        await session.__aenter__()
        page = await session.current_page()

        # 打开长链接
        log(f"{prefix} 打开支付链接...")
        await page.goto(payment_link, wait_until="domcontentloaded")
        try:
            await page.locator(
                'input[autocomplete="cc-number"], iframe[name*="__privateStripeFrame"], text=支付方式'
            ).first.wait_for(timeout=2500)
        except Exception:
            pass
        await page.wait_for_timeout(1200)

        # Stripe
        log(f"{prefix} Stripe 填充...")
        await fill_stripe(page, email, card)

        # PayPal - 可能需要换手机号重试
        for attempt in range(1, max_phone_retries + 1):
            phone = phone_pool.acquire(worker_id)
            if not phone:
                raise RuntimeError("手机号池已耗尽")
            log(f"{prefix} PayPal 注册 (手机: {phone.number}, 尝试 {attempt}/{max_phone_retries})...")

            await fill_paypal(page, email, card, phone, paypal_password, proxy=proxy)

            # 检查手机号是否被拒
            await page.wait_for_timeout(1200)
            # CAPTCHA 未通过时，不应误判手机号拒绝；先做一次有 reason 的复检与兜底处理
            has_captcha_after, reason_after = await _detect_captcha_signal(page)
            if has_captcha_after:
                log(f"{prefix} 检测到 CAPTCHA 残留（reason={reason_after}），尝试再处理一次...")
                await handle_paypal_captcha(page, solver_proxy=proxy)
                # 关键修复：不要仅凭 challenge iframe 残留直接判失败；
                # 若页面已推进到短信验证码阶段，也视为 CAPTCHA 已通过。
                cleared = await _wait_captcha_cleared(page, timeout_seconds=20)
                if not cleared:
                    has_captcha_after2, reason_after2 = await _detect_captcha_signal(page)
                    if has_captcha_after2:
                        # 兼容场景：CAPTCHA 已通过但 challenge iframe 短暂残留。
                        if reason_after2 == "visible_challenge_frame" and await _is_paypal_verification_stage(page):
                            log(f"{prefix} CAPTCHA 残留 iframe，但已进入短信验证码阶段，继续流程")
                        else:
                            raise RuntimeError(f"CAPTCHA 未通过（reason={reason_after2}），需人工处理或更换打码策略")
            if await check_phone_rejected(page):
                log(f"{prefix} 手机号 {phone.number} 被拒，换号重试...")
                phone_pool.mark_failed(phone.number)
                phone = None
                continue

            # 验证码
            log(f"{prefix} 等待验证码...")
            await fill_sms_code(page, phone.api_url, solver_proxy=proxy)
            await _click_paypal_agree_and_continue_if_present(page, solver_proxy=proxy, prefix=prefix)
            phone_pool.release(phone.number, success=True)
            break
        else:
            raise RuntimeError(f"手机号重试 {max_phone_retries} 次均被拒")

        # 等待回到 chatgpt.com
        log(f"{prefix} 等待支付完成...")
        for i in range(60):
            if "chatgpt.com" in page.url or "success" in page.url.lower():
                break
            if "paypal.com" in page.url and i % 2 == 0:
                await _click_paypal_agree_and_continue_if_present(page, solver_proxy=proxy, prefix=prefix)
            await page.wait_for_timeout(1000)

        final_url = page.url
        if "chatgpt.com" in final_url or "success" in final_url.lower():
            log(f"{prefix} ✅ 支付成功！")
            save_pending_auth(email, query_code)
            remove_from_link_pool(email)
            return True
        else:
            log(f"{prefix} ⚠️ 最终 URL: {final_url}")
            save_pending_auth(email, query_code)
            remove_from_link_pool(email)
            return True

    except Exception as exc:
        log(f"{prefix} ❌ 失败: {exc}")
        traceback.print_exc()
        if phone:
            phone_pool.release(phone.number, success=False)
        return False
    finally:
        await session.__aexit__(None, None, None)


async def run_paypal_pay(
    cfg: dict[str, Any],
    count: int = 1,
    workers: int = 1,
    card_source_mode: str | None = None,
) -> int:
    """批量执行流程2。返回成功数。"""
    log(f"PayPal flow2 code version: {PAYPAL_FLOW2_CODE_VERSION} file={Path(__file__).resolve()}")
    env = load_env(".env")
    pool = load_link_pool()
    if not pool:
        log("PayPal 流程2：长链接池为空，请先运行流程1")
        return 0

    cards_file = env.get("PAYPAL_CARDS_FILE") or "data/paypal/cards.txt"
    phones_file = env.get("PAYPAL_PHONES_FILE") or "data/paypal/phones.txt"
    max_uses = int(env.get("PAYPAL_PHONE_MAX_USES") or 5)
    max_retries = int(env.get("PAYPAL_PHONE_RETRY_ON_REJECT") or 3)

    card_pool = CardPool(cards_file)
    phone_pool = PhonePool(phones_file, max_uses=max_uses)
    if card_source_mode:
        local_random_mode = card_source_mode.strip().lower() in {"local_random", "random_local", "local"}
    else:
        local_random_mode = is_local_random_card_mode(env)

    # 代理池（通过 PAYPAL_USE_PROXY 开关控制）
    from .proxy_pool import ProxyPool
    use_proxy = (env.get("PAYPAL_USE_PROXY") or "").strip().lower() in ("true", "1", "yes")
    proxy_pool: ProxyPool | None = None
    if use_proxy:
        proxy_file = env.get("PAYPAL_PROXY_FILE") or env.get("PROXY_FILE") or "data/proxies/proxies.txt"
        proxy_pool = ProxyPool(proxy_file)
        if proxy_pool.count() <= 0:
            log(f"PayPal 流程2：PAYPAL_USE_PROXY 已开启但代理池为空: {proxy_file}")
            return 0
        log(f"PayPal 流程2：代理已启用，代理数={proxy_pool.count()}")

    if not local_random_mode:
        desired_cards = min(count, len(pool), phone_pool.count())
        if desired_cards > 0 and card_pool.count() < desired_cards:
            from .paypal_card_redeem import ensure_card_supply

            ensure_card_supply(env, desired_cards, log_prefix="PayPal 流程2")

        if card_pool.count() <= 0:
            log("PayPal 流程2：卡池为空")
            return 0
    else:
        mode_label = card_source_mode or "PAYPAL_CARD_SOURCE=local_random"
        log(f"PayPal 流程2：已启用本地随机卡资料模式（{mode_label}）")
    if phone_pool.count() <= 0:
        log(f'-----------{phones_file}|{phone_pool}')
        log("PayPal 流程2：手机号池为空")
        return 0

    target = min(count, len(pool), phone_pool.count()) if local_random_mode else min(count, len(pool), card_pool.count())
    card_desc = "本地随机" if local_random_mode else str(card_pool.count())
    log(f"PayPal 流程2：长链接 {len(pool)} 个，卡 {card_desc} 张，手机号 {phone_pool.count()} 个，本次目标 {target}，并发 {workers}")

    success = 0
    sem = asyncio.Semaphore(workers)

    async def worker(index: int, item: dict[str, str]) -> None:
        nonlocal success
        async with sem:
            if local_random_mode:
                card = _generate_local_random_card(index, item["email"], env)
            else:
                card = card_pool.take_one()
                if not card:
                    log(f"[paypal-pay-{index:02d}] 卡池已空")
                    return
            proxy = proxy_pool.pick(index) if proxy_pool else None
            ok = await pay_one(item, card, phone_pool, cfg, worker_id=index, max_phone_retries=max_retries, proxy=proxy)
            if ok and not local_random_mode:
                card_pool.remove(card)
            if ok:
                success += 1

    tasks = [asyncio.create_task(worker(i + 1, item)) for i, item in enumerate(pool[:target])]
    await asyncio.gather(*tasks)
    log(f"PayPal 流程2 完成：成功 {success}/{target}")
    return success

