#!/usr/bin/env python3
"""
PayPal / OpenAI / Stripe checkout 自动注册并填表脚本。

依赖:
    pip install playwright requests
    playwright install chromium

用法:
    # 无头模式 + 传入要打开的支付/注册 URL（默认就是无头）
    python paypal_auto_filler.py "<URL>"

    # 调试时切回有头浏览器
    HEADLESS=0 python paypal_auto_filler.py "<URL>"

    # 不传 URL，则保持 about:blank，自己手动导航（仅有头模式有意义）
    HEADLESS=0 python paypal_auto_filler.py

数据来源:
    - meiguodizhi.com    随机美国身份（地址 / 信用卡 / 姓名 / 生日 等）
    - sms.qiqicdn1.cf    手机号对应的接码 API（轮询拿验证码）
"""

from __future__ import annotations

import json
import os
import random
import platform as _platform
import re
import string
import sys
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import requests
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    sync_playwright,
)


# ============================ 配置 ============================
# 仅这里需要手填：手机号 + 对应接码 API + 代理
CONFIG = {
    "phone": "+15729108922",
    "sms_api_url": (
        "https://sms.qiqicdn1.cf/api/get_sms"
        "?key=0fecadf122a13cb89fbe259192f3952e"
    ),
    # 代理：支持 URL 形式（推荐）"http://user:pass@host:port" / "socks5://..."
    # 或简写 "host:port"、"host:port:user:pass"（兼容旧格式），留空 "" 表示直连
    # 浏览器 (Playwright) 和 Python requests (meiguodizhi/SMS/ipinfo) 全部走它
    # 业务代理: 默认空(直连)。需要时用 --proxy URL 临时启用。
    "proxy": "us.cliproxy.io:3010:hpnm1178546-region-US:mwz6vsx2",
    # 启动目标 URL：直接在这里填好，不传 argv 也会自动打开
    # 优先级：sys.argv[1] > CONFIG["target_url"] > about:blank
    "target_url": "https://pay.openai.com/c/pay/cs_live_a1h5o8fRSk1eYWv1dTInNb9nbmCMpSq34emfzP83tFg1TjhHfn5UH1QxjA#fidnandhYHdWcXxpYCc%2FJ2FgY2RwaXEnKSdpamZkaWAnPyd%2FbScpJ3ZwZ3Zmd2x1cWxqa1BrbHRwYGtgdnZAa2RnaWBhJz9jZGl2YCknYnBkZmRoamlgU2R3bGRrcSc%2FJ2Zqa3F3amknKSdkdWxOYHwnPyd1blppbHNgWjA0TUp3VnJGM200a31Cakw2aVFEYldvXFN3fzFhUDZjU0pkZ3xGZk5XNnVnQE9icEZTRGl0Rn1hfUZQc2pXbTRdUnJXZGZTbGpzUDZuSU5zdW5vbTJMdG5SNTVsXVR2b2o2aycpJ2N3amhWYHdzYHcnP3F3cGApJ2dkZm5id2pwa2FGamlqdyc%2FJyZjY2NjY2MnKSdpZHxqcHFRfHVgJz8ndmxrYmlgWmxxYGgnKSdga2RnaWBVaWRmYG1qaWFgd3YnP3F3cGB4JSUl",
}


def parse_proxy_str(s: str):
    """把代理字符串拆成 Playwright proxy dict + requests proxies dict。

    支持两种格式：
      1) URL 格式（推荐）："http://user:pass@host:port" / "socks5://host:port"
         无 scheme 时自动补 "http://"
      2) 旧格式："host:port:user:pass"
    返回 (playwright_proxy, requests_proxies) 或 (None, None)。
    """
    if not s:
        return None, None
    # 容忍 BOM / 零宽字符
    raw = s.strip().lstrip("﻿​⁠")
    if not raw:
        return None, None

    # 旧格式兼容：恰好 4 段且不含 "://" / "@"
    if "://" not in raw and "@" not in raw and raw.count(":") == 3:
        host, port, user, password = raw.split(":")
        scheme = "http"
        server = f"{scheme}://{host}:{port}"
        playwright_proxy = {"server": server, "username": user, "password": password}
        url_for_requests = f"{scheme}://{user}:{password}@{host}:{port}"
        return playwright_proxy, {"http": url_for_requests, "https": url_for_requests}

    # URL 格式（参考 无卡plus源码/modules/browser.py:parse_proxy）
    if "://" not in raw:
        raw = f"http://{raw}"
    parsed = urlparse(raw)
    if not parsed.hostname or not parsed.port:
        print(f"[PP] 代理格式错误（应为 http://user:pass@host:port）: {s}", flush=True)
        return None, None

    playwright_proxy = {"server": f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"}
    if parsed.username:
        playwright_proxy["username"] = parsed.username
    if parsed.password:
        playwright_proxy["password"] = parsed.password

    # requests 直接复用原 URL（其中已包含 user:pass@host:port）
    requests_proxies = {"http": raw, "https": raw}
    return playwright_proxy, requests_proxies


# 启动时解析一次，全局复用
PLAYWRIGHT_PROXY, REQUESTS_PROXIES = parse_proxy_str(CONFIG.get("proxy", ""))

# 身份数据 API（一次返回地址 + 信用卡 + 姓名等完整 profile）
PROFILE_API_URL = "https://www.meiguodizhi.com/api/v1/dz"

# OpenAI/Stripe checkout 上 PayPal 流程需要填写 billing address 才能让 Subscribe
# 按钮从 --incomplete 变成 --complete。这里"固定死"一个美国地址。
STRIPE_BILLING_ADDRESS = {
    "street": "1600 Amphitheatre Pkwy",
    "city": "Mountain View",
    "zip": "94043",
    "state": "CA",
}

# 邮箱可选域；随机挑一个
EMAIL_DOMAINS = ["icloud.com"]

# ---- 浏览器指纹池：按当前 OS 自动选 UA 池，避免 UA 跟实际 GPU/Canvas 指纹冲突 ----
MAC_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
]
WIN_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36",
]
LINUX_USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

VIEWPORTS = [
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1680, "height": 1050},
    {"width": 1920, "height": 1080},
    {"width": 1280, "height": 800},
]
# 美区时区，注册 US 账户保持一致更稳
TIMEZONES = [
    "America/New_York",
    "America/Los_Angeles",
    "America/Chicago",
    "America/Denver",
    "America/Phoenix",
]


def _current_os() -> str:
    s = _platform.system().lower()
    if s == "darwin":
        return "mac"
    if s == "windows":
        return "windows"
    return "linux"


def random_fingerprint() -> dict:
    """按当前真实 OS 抽 UA + 平台字段；其他属性（viewport / tz / CPU / Memory）随机。
    UA 与 WebGL/Canvas（真实 GPU）保持同 OS 一致，避免 DataDome 检测不匹配。"""
    os_name = _current_os()
    if os_name == "mac":
        ua = random.choice(MAC_USER_AGENTS)
        platform_str = "MacIntel"
    elif os_name == "windows":
        ua = random.choice(WIN_USER_AGENTS)
        platform_str = "Win32"
    else:
        ua = random.choice(LINUX_USER_AGENTS)
        platform_str = "Linux x86_64"
    return {
        "user_agent": ua,
        "viewport": random.choice(VIEWPORTS),
        "timezone": random.choice(TIMEZONES),
        "platform": platform_str,
        "os": os_name,
        "hardware_concurrency": random.choice([4, 8, 12, 16]),
        "device_memory": random.choice([4, 8, 16]),
    }


# 短信验证码轮询参数
SMS_POLL_TIMEOUT = 120   # 秒：等待短信总超时
SMS_POLL_INTERVAL = 4    # 秒：两次轮询的间隔
OTP_WAIT_AFTER_SUBMIT = 4  # 秒：提交后等多久再检测 OTP 输入框

# 兜底数据，API 失败时使用
DEFAULT_PROFILE_RAW = {
    "Address": "123 Main St",
    "City": "New York",
    "State": "NY",
    "State_Full": "New York",
    "Zip_Code": "10001",
    "Credit_Card_Number": "4111111111111111",
    "CVV2": "123",
    "Expires": "12/2029",
    "Full_Name": "James Smith",
    "Title": "Mr.",
}

# 注入到页面的 CSS：隐藏验证码与地址自动补全
HIDE_CSS = (
    "#captcha-standalone,.captcha-overlay,.captcha-container,"
    'div[data-app="authchallenge_response"],'
    ".AddressAutocomplete-results"
    "{display:none!important;height:0!important;overflow:hidden!important}"
)


# ============================ 工具函数 ============================
# 日志分级 (前缀为 HH:MM:SS.mmm 时间戳):
#   log(msg)             - 详细 debug,默认静默,VERBOSE=1 或 DEBUG=1 才打印
#   log_step(msg)        - 单点关键节点,前缀 >>>
#   log_step_begin(name) - 配对步骤开始,前缀 ▶, 内部记开始时间
#   log_step_end(name)   - 配对步骤完成,前缀 ✓, 打印耗时
_VERBOSE_LOG = (
    os.environ.get("VERBOSE", "0") == "1"
    or os.environ.get("DEBUG", "0") == "1"
)
_STEP_TIMES: dict[str, float] = {}


def _ts() -> str:
    """返回当前 HH:MM:SS.mmm 时间戳。"""
    t = time.time()
    return f"{time.strftime('%H:%M:%S', time.localtime(t))}.{int((t % 1) * 1000):03d}"


def log(msg: str) -> None:
    """详细 debug 日志,默认静默,通过 VERBOSE=1 或 DEBUG=1 开启。"""
    if _VERBOSE_LOG:
        print(f"{_ts()}  {msg}", flush=True)


def log_step(msg: str) -> None:
    """单点关键节点(始终打印)。"""
    print(f"{_ts()}  >>> {msg}", flush=True)


def log_step_begin(name: str) -> None:
    """配对步骤开始,记录开始时间。"""
    _STEP_TIMES[name] = time.time()
    print(f"{_ts()}  ▶ START : {name}", flush=True)


def log_step_end(name: str, ok: bool = True, extra: str = "") -> None:
    """配对步骤完成,打印总耗时。ok=False 用 ✗ 标记失败。"""
    t0 = _STEP_TIMES.pop(name, None)
    elapsed = (time.time() - t0) if t0 else 0.0
    icon = "✓" if ok else "✗"
    tail = f" ({elapsed:.1f}s)" if t0 else ""
    if extra:
        tail = f"{tail} | {extra}"
    print(f"{_ts()}  {icon} END   : {name}{tail}", flush=True)


def rand_email() -> str:
    """随机邮箱：本地名 12–18 位字母+数字，域名从 EMAIL_DOMAINS 抽。"""
    pool = string.ascii_lowercase + string.digits
    local = "".join(random.choice(pool) for _ in range(random.randint(12, 18)))
    return f"{local}@{random.choice(EMAIL_DOMAINS)}"


def rand_gmail() -> str:
    """历史函数名（rand_gmail）保留为别名，实际生成 iCloud 邮箱。
    所有 PayPal 注册流程调用方继续工作，但生成的邮箱域名是 icloud.com。"""
    return rand_email()


def phone_local(phone: str) -> str:
    """把带国家码的电话号转成纯本地号格式：
    "+15729108922" -> "5729108922"
    PayPal 的电话输入框通常不接受 +1 前缀，国家码由旁边的下拉框承担。"""
    digits = re.sub(r"\D", "", phone or "")
    # 美国/加拿大码：+1 → 11 位，去掉前导 1
    if len(digits) == 11 and digits.startswith("1"):
        return digits[1:]
    return digits


def rand_password() -> str:
    letters = string.ascii_letters
    digits = string.digits
    symbols = "!@#$%^"
    pool = letters + digits + symbols
    seed = [
        random.choice(string.ascii_lowercase),
        random.choice(string.ascii_uppercase),
        random.choice(digits),
        random.choice(symbols),
    ]
    seed += [random.choice(pool) for _ in range(10)]
    random.shuffle(seed)
    return "".join(seed)


def split_full_name(full: str) -> tuple[str, str]:
    """将 'James Smith' 拆为 ('James', 'Smith')；若无空格则补默认姓。"""
    parts = [p for p in re.split(r"\s+", (full or "").strip()) if p]
    if len(parts) >= 2:
        return parts[0], " ".join(parts[1:])
    if len(parts) == 1:
        return parts[0], "Smith"
    return "James", "Smith"


def normalize_expiry(expires: str) -> str:
    """API 返回 'MM/YYYY'，PayPal 字段格式 'MM / YY'。"""
    m = re.match(r"^\s*(\d{1,2})\s*/\s*(\d{2,4})\s*$", expires or "")
    if not m:
        return "12 / 29"
    mm = m.group(1).zfill(2)
    yy = m.group(2)[-2:]
    return f"{mm} / {yy}"


@dataclass
class Profile:
    street: str
    city: str
    state_code: str       # CT / NY ...
    state_full: str       # Connecticut / New York ...
    zip: str
    card_number: str
    card_expiry: str      # "MM / YY"
    card_cvv: str
    first_name: str
    last_name: str
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "raw"}


def _parse_profile(raw: dict) -> Profile:
    a = raw or DEFAULT_PROFILE_RAW
    first, last = split_full_name(a.get("Full_Name", ""))
    return Profile(
        street=a.get("Address") or DEFAULT_PROFILE_RAW["Address"],
        city=a.get("City") or DEFAULT_PROFILE_RAW["City"],
        state_code=a.get("State") or DEFAULT_PROFILE_RAW["State"],
        state_full=a.get("State_Full") or a.get("State") or DEFAULT_PROFILE_RAW["State_Full"],
        zip=(a.get("Zip_Code") or DEFAULT_PROFILE_RAW["Zip_Code"])[:5],
        card_number=(a.get("Credit_Card_Number") or DEFAULT_PROFILE_RAW["Credit_Card_Number"]).replace(" ", ""),
        card_expiry=normalize_expiry(a.get("Expires") or DEFAULT_PROFILE_RAW["Expires"]),
        card_cvv=a.get("CVV2") or DEFAULT_PROFILE_RAW["CVV2"],
        first_name=first,
        last_name=last,
        raw=a,
    )


# 50 个美国州 + DC + 5 个 US 海外属地，两位代码
US_STATE_CODES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV "
    "WI WY DC AS GU MP PR VI".split()
)


def _is_us_profile(raw: dict) -> bool:
    """严格判定 meiguodizhi 返回的 profile 是不是美国地址。"""
    state = (raw.get("State") or "").strip().upper()
    phone = (raw.get("Telephone") or "").strip()
    zip_code = (raw.get("Zip_Code") or "").strip()
    # 1) State 必须是美国 50 州 / DC / 属地的两位代码
    if state not in US_STATE_CODES:
        return False
    # 2) 电话必须 +1 开头
    if not phone.startswith("+1"):
        return False
    # 3) ZIP 必须 5 位纯数字（美国邮编格式）
    if not (zip_code[:5].isdigit() and len(zip_code) >= 5):
        return False
    return True


def fetch_profile() -> Profile:
    """从 meiguodizhi.com 抓一个**严格美国**的 profile（地址+卡+姓名）。
    返回的不是美国地址就重抽，最多 3 次；都失败回退默认 US 兜底。"""
    log("Fetching US profile from meiguodizhi.com ...")
    for attempt in range(1, 4):
        try:
            resp = requests.post(
                PROFILE_API_URL,
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0",
                },
                json={"path": "/", "method": "refresh_addr"},
                timeout=12,
                proxies=REQUESTS_PROXIES,
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data.get("address") or data
            if _is_us_profile(raw):
                profile = _parse_profile(raw)
                log(
                    f"US profile ✓ (try {attempt}): "
                    f"{profile.first_name} {profile.last_name} | "
                    f"{profile.street}, {profile.city}, {profile.state_full} {profile.zip} | "
                    f"card ****{profile.card_number[-4:]} exp {profile.card_expiry}"
                )
                return profile
            log(
                f"  try {attempt}: not US (state={raw.get('State')}, "
                f"tel={raw.get('Telephone')}); retrying"
            )
        except Exception as e:  # noqa: BLE001
            log(f"  try {attempt}: profile fetch error ({e}); retrying")
    log("3 attempts failed to get US profile; using default US fallback")
    return _parse_profile(DEFAULT_PROFILE_RAW)


# -------------------------- SMS --------------------------
_OTP_PATTERNS = [
    re.compile(r"(?<!\d)(\d{6})(?!\d)"),
    re.compile(r"(?<!\d)(\d{4,5})(?!\d)"),
    re.compile(r"(?<!\d)(\d{7,8})(?!\d)"),
]


def _extract_code(text: str) -> Optional[str]:
    for pat in _OTP_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


def fetch_sms_code(
    timeout: int = SMS_POLL_TIMEOUT,
    interval: int = SMS_POLL_INTERVAL,
    since_ts: Optional[float] = None,
) -> Optional[str]:
    """轮询 SMS API，返回验证码字符串；超时返回 None。

    接口实测返回如 'yes|你的验证码是 123456|2026-06-29 00:00:00'
    或 'no|暂无验证码|...'。
    """
    log("Polling SMS API for verification code...")
    deadline = time.time() + timeout
    last_payload = ""
    while time.time() < deadline:
        try:
            resp = requests.get(
                CONFIG["sms_api_url"],
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
                proxies=REQUESTS_PROXIES,
            )
            text = (resp.text or "").strip()
        except Exception as e:  # noqa: BLE001
            log(f"SMS request error: {e}")
            time.sleep(interval)
            continue

        if text != last_payload:
            log(f"SMS payload: {text[:200]}")
            last_payload = text

        parts = text.split("|")
        status = parts[0].lower() if parts else ""
        body = parts[1] if len(parts) > 1 else text
        if status == "yes" or "yes" in status:
            code = _extract_code(body) or _extract_code(text)
            if code:
                log(f"SMS code received: {code}")
                log_step(f"获取手机验证码: {code}")
                return code
        time.sleep(interval)
    log("SMS poll timeout")
    return None


# ============================ 页面操作 ============================
# 这段 JS 用原生 setter 写入值并触发 input/change/blur 事件，
# 用于绕过 React/Vue 等受控组件对 .value 直接赋值的拦截。
NATIVE_FILL_JS = """
(args) => {
    const { selector, value } = args;
    const el = document.querySelector(selector);
    if (!el) return { ok: false, reason: 'not_found' };
    const proto = el.tagName === 'TEXTAREA'
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
    setter.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
    el.dispatchEvent(new Event('blur', { bubbles: true }));
    return { ok: true, value: el.value };
}
"""


def fill(page: Page, selector: str, value: str) -> bool:
    try:
        result = page.evaluate(NATIVE_FILL_JS, {"selector": selector, "value": value})
    except Exception as e:  # noqa: BLE001
        log(f"FILL ERROR {selector}: {e}")
        return False
    if not result.get("ok"):
        log(f"NOT FOUND: {selector}")
        return False
    log(f"{selector} = {result.get('value')}")
    return True


def fill_by_id(page: Page, element_id: str, value: str) -> bool:
    return fill(page, f"#{element_id}", value)


# 批量填充 JS（两阶段+批量事件派发，让 React 一次批量渲染避免逐字段闪烁）
#   阶段 1: 所有字段静默设值（清除 React _valueTracker + native setter），无事件
#   阶段 2: 统一派发 input 事件（React 状态更新批处理）
#   阶段 3: 统一派发 change 事件
#   阶段 4: 只对最后一个字段派发 blur（避免 N 次单字段同步验证）
BATCH_FILL_JS = r"""
(fields) => {
    const t0 = performance.now();
    const entries = Object.entries(fields);
    const results = {};
    const elements = [];

    // Phase 1: 全部静默设值
    for (const [id, value] of entries) {
        const el = document.getElementById(id);
        if (!el) { results[id] = 'not_found'; continue; }
        const proto = el.tagName === 'TEXTAREA'
            ? HTMLTextAreaElement.prototype
            : HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        // 关键技巧:清掉 React _valueTracker 让 React 一定感知 value 已变
        if (el._valueTracker) el._valueTracker.setValue('');
        setter.call(el, value);
        elements.push([id, el]);
    }

    // Phase 2: 批量派发 input(React 会合并状态更新)
    for (const [, el] of elements) {
        el.dispatchEvent(new Event('input', { bubbles: true }));
    }
    // Phase 3: 批量派发 change
    for (const [, el] of elements) {
        el.dispatchEvent(new Event('change', { bubbles: true }));
    }
    // Phase 4: 只对最后一个字段 blur(触发整体校验)
    if (elements.length > 0) {
        const lastEl = elements[elements.length - 1][1];
        lastEl.dispatchEvent(new Event('blur', { bubbles: true }));
    }

    for (const [id, el] of elements) {
        results[id] = el.value;
    }
    results.__elapsed_ms__ = Math.round(performance.now() - t0);
    return results;
}
"""


def batch_fill_by_id(page: Page, fields: dict) -> dict:
    """一次 evaluate 两阶段批量填多个 #id 字段。返回 {id: result_value or 'not_found'}。

    两阶段策略:全部 setter 完成后才统一派发 input/change 事件,React 批处理一次渲染,
    避免"一个一个填写"的视觉闪烁。只对最后字段触发 blur,减少 N-1 次单字段同步验证。
    """
    t0 = time.time()
    try:
        results = page.evaluate(BATCH_FILL_JS, fields)
    except Exception as e:  # noqa: BLE001
        log(f"BATCH FILL ERROR: {e}")
        return {}
    elapsed_js = (results or {}).pop("__elapsed_ms__", 0)
    elapsed_total_ms = int((time.time() - t0) * 1000)
    log(f"  [batch_fill] {len(fields)} fields, "
        f"js={elapsed_js}ms, total={elapsed_total_ms}ms")
    for k, v in (results or {}).items():
        log(f"  #{k} = {v}")
    return results or {}


def batch_fill_with_aliases(
    page: Page,
    fields: list[tuple[str, list[str], str]],
) -> dict:
    """一次 evaluate 批量填多个字段,每个字段支持多 selector 候选(找第一个可见可填的)。

    与逐字段 fill_first_matching 相比:
      - 1 次 Playwright RPC (省 N-1 次 slow_mo 延迟)
      - 浏览器内 JS 同步执行所有 DOM 操作
      - 不派发 blur 事件 (避免逐字段同步校验)
      - 清 React _valueTracker 让 React/AngularJS 一定感知 value 已变

    fields: [(label, selectors, value), ...]
    返回: {label: {ok, sel, value} or {ok=False, reason}}
    """
    js = r"""
    (fieldsArr) => {
        const t0 = performance.now();
        const results = {};
        function isClickableInput(el) {
            if (!el || el.disabled) return false;
            const rect = el.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) return false;
            const cs = window.getComputedStyle(el);
            if (cs.display === 'none' || cs.visibility === 'hidden') return false;
            if (Number(cs.opacity || '1') < 0.05) return false;
            return true;
        }
        for (const [label, selectors, value] of fieldsArr) {
            let target = null;
            let matched = null;
            for (const sel of selectors) {
                try {
                    const els = document.querySelectorAll(sel);
                    for (const el of els) {
                        if (isClickableInput(el)) {
                            target = el; matched = sel; break;
                        }
                    }
                    if (target) break;
                } catch (e) { continue; }
            }
            if (!target) { results[label] = { ok: false, reason: 'not_found' }; continue; }
            const proto = target.tagName === 'TEXTAREA'
                ? HTMLTextAreaElement.prototype
                : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
            if (target._valueTracker) target._valueTracker.setValue('');
            try { target.focus(); } catch(e) {}
            setter.call(target, value);
            target.dispatchEvent(new Event('input', { bubbles: true }));
            target.dispatchEvent(new Event('change', { bubbles: true }));
            results[label] = { ok: true, sel: matched, actual_len: (target.value || '').length };
        }
        results.__elapsed_ms__ = Math.round(performance.now() - t0);
        return results;
    }
    """
    fields_arr = [[label, sels, value] for label, sels, value in fields]
    try:
        results = page.evaluate(js, fields_arr) or {}
    except Exception as e:  # noqa: BLE001
        log(f"  [batch_fill_aliases] JS 异常: {e}")
        return {}
    elapsed = results.pop("__elapsed_ms__", 0)
    log(f"  [batch_fill_aliases] {len(fields)} 字段,js={elapsed}ms")
    for label, info in results.items():
        if info.get("ok"):
            log_step(f"填 {label}: ✓ (sel={info.get('sel')})")
        else:
            log_step(f"⚠️ {label}: {info.get('reason')}")
    return results


def fill_first_matching(
    page: Page,
    label: str,
    selectors: list[str],
    value: str,
) -> bool:
    """按顺序尝试多个 CSS selector,填入第一个可见可填的元素。

    每次调用打详细日志:命中哪个 selector / 填入值 / 实际 DOM 值。
    覆盖 PayPal React 新版 + AngularJS 旧版两套字段命名。
    """
    masked = value if len(value) <= 6 else f"{value[:2]}***{value[-2:]}"
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if not loc.is_visible(timeout=1500):
                continue
            try:
                loc.fill("", timeout=1500)
            except Exception:  # noqa: BLE001
                pass
            loc.fill(value, timeout=5000)
            # 读回 DOM 值确认真填了
            try:
                actual = loc.input_value(timeout=1000)
            except Exception:  # noqa: BLE001
                actual = "?"
            ok = (actual or "").strip() == value or len(actual or "") > 0
            icon = "✓" if ok else "⚠️"
            log(f"  [fill] {icon} {label}: sel={sel} value={masked} actual_len={len(actual or '')}")
            log_step(f"填 {label}: {masked}  ({icon})")
            return True
        except Exception as e:  # noqa: BLE001
            log(f"  [fill] {label}: sel={sel} 异常 {e}")
            continue
    log(f"  [fill] ✗ {label}: 所有 selector 都未命中 ({len(selectors)} 个)")
    log_step(f"⚠️ {label}: 未找到字段")
    return False


def fill_select(page: Page, element_id: str, *candidates: str) -> bool:
    """按候选词选择下拉项；3 阶段匹配:精确 value -> 精确 text -> 模糊 includes。

    修复 bug:之前 fill_select("billingAdministrativeArea", "California", "CA") 用
    includes 匹配,"CA".toLowerCase() = "ca" 会命中 "Ameri**ca**n Samoa",导致州
    被选错。现在精确匹配 value="CA" 优先,只有都失败才退化为 includes。
    """
    js = """
    (args) => {
        const { id, needles } = args;
        const el = document.getElementById(id);
        if (!el) return { ok: false, reason: 'not_found' };

        function select(opt, mode) {
            el.value = opt.value;
            el.dispatchEvent(new Event('input',  { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
            return { ok: true, text: opt.text, value: opt.value, mode };
        }

        // Phase 1: 精确 value 匹配(优先)
        for (const needle of needles) {
            for (const opt of el.options) {
                if (opt.value === needle) return select(opt, 'exact_value');
            }
        }
        // Phase 2: 精确 text 匹配(大小写不敏感)
        for (const needle of needles) {
            const lo = needle.toLowerCase();
            for (const opt of el.options) {
                if (opt.text.toLowerCase() === lo) return select(opt, 'exact_text');
            }
        }
        // Phase 3: includes 模糊匹配(原行为兜底)
        for (const needle of needles) {
            const lo = needle.toLowerCase();
            for (const opt of el.options) {
                if (opt.text.toLowerCase().includes(lo)
                    || opt.value.toLowerCase().includes(lo)) {
                    return select(opt, 'fuzzy');
                }
            }
        }
        return { ok: false, reason: 'no_match' };
    }
    """
    needles = [c for c in candidates if c]
    result = page.evaluate(js, {"id": element_id, "needles": needles})
    if not result.get("ok"):
        log(f"SELECT {element_id}: {result.get('reason')}")
        return False
    log(f"{element_id} = {result.get('text')} "
        f"(value={result.get('value')}, mode={result.get('mode')})")
    return True


def click_submit(page: Page, retries: int = 10,
                 extra_selectors: tuple[str, ...] = ()) -> bool:
    """查找并点击提交按钮。
    严格按属性选择器命中，不识别按钮文案。
    优先用 React 事件链派发（同 PayPal accordion 按钮的 0×0 修复一致），
    Stripe 这类 React 应用普通 .click() 经常不触发 onClick。"""
    selectors: tuple[str, ...] = (
        *extra_selectors,
        'button[data-testid="submit-button"]',
        'button[data-testid="hosted-payment-submit-button"]',
        'button[data-atomic-wait-intent="Submit_Email"]',
        "button.SubmitButton--complete",
        'button[type="submit"]',  # 通用兜底
    )

    for attempt in range(retries):
        matched_sel = None
        for sel in selectors:
            el = page.query_selector(sel)
            if el is None:
                continue
            try:
                if el.is_disabled():
                    continue
            except Exception:  # noqa: BLE001
                pass
            matched_sel = sel
            break

        if matched_sel is None:
            log(f"No enabled submit button matched any selector, waiting... ({attempt})")
            time.sleep(1)
            continue

        log(f"Submit selector matched: {matched_sel}")
        if click_react(page, matched_sel, "submit"):
            return True
        log(f"  react click failed; retry ({attempt})")
        time.sleep(1)

    return False


def check_terms_checkbox(page: Page) -> bool:
    """勾选 OpenAI/Stripe checkout 上的条款 checkbox。
    Stripe 用 React 控制 checkbox，必须派发完整事件链才会真正勾上。"""
    sel = '#termsOfServiceConsentCheckbox'
    js_is_checked = (
        "() => { const c = document.getElementById('termsOfServiceConsentCheckbox');"
        " return c ? c.checked : null; }"
    )
    try:
        state = page.evaluate(js_is_checked)
    except Exception:  # noqa: BLE001
        state = None
    if state is None:
        log("Terms checkbox not present (skip)")
        return True
    if state is True:
        log("Terms already checked")
        return True

    for attempt in range(3):
        click_react(page, sel, f"terms checkbox (try {attempt + 1})")
        time.sleep(0.4)
        try:
            if page.evaluate(js_is_checked):
                log(f"Terms checked ✓ after try {attempt + 1}")
                return True
        except Exception:  # noqa: BLE001
            pass
    log("Terms check FAILED after 3 tries")
    return False


def set_country_us_if_needed(page: Page) -> bool:
    """把 PayPal 的 #country select 切到 US。
    PayPal 的 select 是 React 控制，必须走原型链 native setter +
    派发 input/change 事件，React 才会接收新值。"""
    js = """
    () => {
        const el = document.getElementById('country');
        if (!el) return { exists: false };
        if (el.value === 'US') return { exists: true, changed: false };
        const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set;
        setter.call(el, 'US');
        el.dispatchEvent(new Event('input', { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
        return { exists: true, changed: true, after_value: el.value };
    }
    """
    result = page.evaluate(js)
    if result.get("changed"):
        log(f"Country -> US (after_value={result.get('after_value')}), waiting for re-render...")
        return True
    return False


# 常见 OTP 输入框定位
OTP_SELECTORS = [
    "#otp", "#otpCode", "#smsCode", "#verificationCode", "#verification_code",
    "#code", "input[name='otp']", "input[name='code']",
    "input[autocomplete='one-time-code']",
]

# PayPal 6 位分散输入框：name="ciBasic-0".."ciBasic-5", id="ci-ciBasic-0".."ci-ciBasic-5"
PAYPAL_SPLIT_OTP_SELECTOR = 'input[name^="ciBasic-"]'


def find_otp_input(page: Page):
    """返回 (kind, payload):
    - ('split', count) 表示 PayPal 的 6 位分散输入框，count 是 input 数量
    - ('single', selector) 单 input 输入框，selector 是命中的 CSS selector
    - (None, None) 没找到
    """
    # 优先识别 PayPal 6 位分散输入框
    handles = page.query_selector_all(PAYPAL_SPLIT_OTP_SELECTOR)
    if handles:
        return 'split', len(handles)
    # 单 input
    for sel in OTP_SELECTORS:
        el = page.query_selector(sel)
        if el:
            return 'single', sel
    return None, None


def handle_otp_if_present(page: Page) -> bool:
    """提交后若出现 OTP 输入框，拉短信验证码并填入。
    支持 PayPal 6 位分散输入框和传统单输入框两种。"""
    log_step_begin("处理手机验证码 OTP")
    log(f"Waiting {OTP_WAIT_AFTER_SUBMIT}s for possible OTP step...")
    time.sleep(OTP_WAIT_AFTER_SUBMIT)
    kind, payload = find_otp_input(page)
    if kind is None:
        log("No OTP field detected.")
        log_step_end("处理手机验证码 OTP", extra="未检测到 OTP 输入框")
        return False
    log(f"OTP detected: kind={kind}, payload={payload}")

    log_step_begin("拉取 SMS 验证码")
    code = fetch_sms_code()
    if not code:
        log("No SMS code obtained; abort OTP step.")
        log_step_end("拉取 SMS 验证码", ok=False, extra="超时未拿到")
        log_step_end("处理手机验证码 OTP", ok=False)
        return False
    log_step_end("拉取 SMS 验证码", extra=f"{code[:1]}****{code[-1:]}")

    log_step_begin("填入 OTP")
    if kind == 'split':
        # PayPal 6 位分散输入框：逐个填入 + 触发 input 事件
        # React 会监听 input 事件自动 focus 下一个框；输完最后一位通常会自动提交。
        js = """
        (code) => {
            const inputs = document.querySelectorAll('input[name^="ciBasic-"]');
            const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
            let filled = 0;
            inputs.forEach((inp, i) => {
                if (i >= code.length) return;
                try { inp.focus(); } catch(e) {}
                setter.call(inp, code[i]);
                inp.dispatchEvent(new Event('input', { bubbles: true }));
                inp.dispatchEvent(new Event('change', { bubbles: true }));
                filled++;
            });
            // 最后一个 input 失焦，触发 PayPal 验证
            const last = inputs[Math.min(code.length - 1, inputs.length - 1)];
            if (last) last.dispatchEvent(new Event('blur', { bubbles: true }));
            return filled;
        }
        """
        try:
            n = page.evaluate(js, code)
            log(f"Filled {n} digits into split OTP inputs ({code[:1]}****{code[-1:]})")
            log_step_end("填入 OTP", extra=f"split, {n} digits")
        except Exception as e:  # noqa: BLE001
            log(f"OTP split fill error: {e}")
            log_step_end("填入 OTP", ok=False, extra=str(e))
            log_step_end("处理手机验证码 OTP", ok=False)
            return False
        # PayPal 通常自动提交；等一下看页面是否变化
        time.sleep(3)
        log_step_end("处理手机验证码 OTP")
        return True

    # 单输入框：填入 + 提交按钮
    sel = payload
    fill(page, sel, code)
    time.sleep(0.5)
    click_submit(page)
    log_step_end("填入 OTP", extra=f"single ({sel})")
    log_step_end("处理手机验证码 OTP")
    return True


# ============================ 页面处理器 ============================
# 给 React app 用的"真实点击"事件链：派发 pointerdown/mousedown/pointerup/mouseup/click，
# 这样 React 的 synthetic event 才会响应。仅 el.click() 在很多 React 组件上是无效的。
# clientX/Y 用 Math.max(1, ...) 处理 0×0 元素（Stripe 的 accordion-item-button 实测就是 0×0）。
REACT_CLICK_JS = """
(sel) => {
    const el = typeof sel === 'string' ? document.querySelector(sel) : sel;
    if (!el) return { ok: false, reason: 'not_found' };
    try { el.scrollIntoView({ block: 'center', behavior: 'instant' }); } catch(e) {}
    const r = el.getBoundingClientRect();
    const cx = r.left + Math.max(1, r.width / 2);
    const cy = r.top + Math.max(1, r.height / 2);
    const opts = {
        bubbles: true, cancelable: true, view: window, button: 0,
        clientX: cx, clientY: cy
    };
    try {
        if (typeof PointerEvent === 'function') {
            el.dispatchEvent(new PointerEvent('pointerdown', opts));
        }
        el.dispatchEvent(new MouseEvent('mousedown', opts));
        if (typeof PointerEvent === 'function') {
            el.dispatchEvent(new PointerEvent('pointerup', opts));
        }
        el.dispatchEvent(new MouseEvent('mouseup', opts));
        el.dispatchEvent(new MouseEvent('click', opts));
    } catch(e) {
        return { ok: false, reason: 'dispatch_failed', error: String(e) };
    }
    return { ok: true };
}
"""


def click_react(page: Page, selector: str, label: str = "?") -> bool:
    """按 selector 派发完整鼠标事件链，触发 React handler。
    完全绕过 Playwright 的可见性检查。"""
    try:
        result = page.evaluate(REACT_CLICK_JS, selector)
    except Exception as e:  # noqa: BLE001
        log(f"  react-click eval error ({label}): {e}")
        return False
    if result.get("ok"):
        log(f"  react-clicked: {label} ({selector})")
        return True
    log(f"  react-click failed ({label}): {result.get('reason')}")
    return False


def safe_click(handle, label: str = "?") -> bool:
    """四段式点击 ElementHandle:
    1) scroll into view
    2) 派发完整事件链 (React 兼容)
    3) Playwright force click 兜底
    4) el.click() 最后兜底"""
    try:
        handle.scroll_into_view_if_needed(timeout=2000)
    except Exception as e:  # noqa: BLE001
        log(f"  scroll_into_view skipped: {e}")

    try:
        result = handle.evaluate(REACT_CLICK_JS)
        if result.get("ok"):
            log(f"  react-clicked: {label}")
            return True
        log(f"  react-click failed ({label}): {result.get('reason')}")
    except Exception as e:  # noqa: BLE001
        log(f"  react-click eval error ({label}): {e}")

    try:
        handle.click(force=True, timeout=4000)
        log(f"  force-clicked: {label}")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"  force click failed ({label}): {e}; fallback to el.click()")
    try:
        handle.evaluate("el => el.click()")
        log(f"  js-clicked: {label}")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"  js click failed ({label}): {e}")
    return False


def _read_stripe_total_amount(page: Page) -> str:
    """读取 Stripe ProductSummary-totalAmount,返回 'US$0.00' 之类字符串。"""
    try:
        return page.evaluate(
            """() => {
                const el = document.querySelector(
                    '#ProductSummary-totalAmount, '
                    + '[data-testid="product-summary-total-amount"]'
                );
                return el ? (el.innerText || el.textContent || '').trim() : '';
            }"""
        ) or ""
    except Exception:  # noqa: BLE001
        return ""


def _paypal_resolve_fast_path(paypal_url: str) -> str | None:
    """DataDome 绕过:给 PayPal URL 加 ul=1 + paypal_client_cfci 后缀,
    HEAD 拿服务端 302 Location,跳过 DataDome 客户端挑战页。

    传入:跳转后的 paypal.com URL (含 ba_token 等参数)
    返回:302 Location URL(若存在);否则 None

    例:
      传入: https://www.paypal.com/agreements/approve?ba_token=BA-xxx
      拼成: https://www.paypal.com/agreements/approve?ba_token=BA-xxx
            &ul=1&paypal_client_cfci=modxo_vaulted_not_recurring-FIRST_PAGE_LOAD
      302 Location: 真实表单页 URL,直接 page.goto 即可绕过 DataDome 风控
    """
    from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
    try:
        parsed = urlparse(paypal_url)
        if "paypal.com" not in (parsed.netloc or "").lower():
            return None
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        # 已经有 ul=1 + paypal_client_cfci 就不重复
        params.setdefault("ul", "1")
        params.setdefault(
            "paypal_client_cfci",
            "modxo_vaulted_not_recurring-FIRST_PAGE_LOAD",
        )
        full_url = urlunparse(parsed._replace(query=urlencode(params)))
        ua = os.environ.get(
            "USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/143.0.0.0 Safari/537.36",
        )
        log_step(f"302 fast-path: HEAD {full_url[:100]}")
        resp = requests.head(
            full_url,
            proxies=REQUESTS_PROXIES,
            headers={
                "User-Agent": ua,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            },
            timeout=15,
            allow_redirects=False,
        )
        loc = resp.headers.get("Location") or resp.headers.get("location")
        log_step(f"302 fast-path: status={resp.status_code} loc={(loc or '')[:100]}")
        if resp.status_code in (301, 302, 303, 307, 308) and loc:
            # 相对路径补齐
            if loc.startswith("/"):
                loc = f"{parsed.scheme}://{parsed.netloc}{loc}"
            return loc
        return None
    except Exception as e:  # noqa: BLE001
        log_step(f"⚠️ 302 fast-path 失败: {e}")
        return None


def _detect_stripe_end_state(page: Page) -> str | None:
    """探测 Stripe 终态页 .FullPageMessage(已完成/会话超时)。

    判定:必须有 .FullPageMessage-Message-Detail 终态详情文案,
    或可见 h2 非空(排除 loading 占位)。返回探测到的文案(用于日志),
    无则返回 None。
    """
    try:
        return page.evaluate(
            """() => {
                const d = document.querySelector('.FullPageMessage-Message-Detail');
                if (d) { const t=(d.textContent||'').trim(); if(t.length>=2) return t; }
                const h2 = document.querySelector('.FullPageMessage-Message h2');
                if (h2 && h2.offsetParent !== null) {
                    const t=(h2.textContent||'').trim(); if(t.length>=2) return t;
                }
                return null;
            }"""
        )
    except Exception:  # noqa: BLE001
        return None


def _mark_paid_no_action_needed(text: str | None = None, url: str | None = None) -> None:
    """统一打印"支付成功 无须支付"并置位 _SUCCESS_FLAG。"""
    global _SUCCESS_FLAG
    _SUCCESS_FLAG = True
    log("=" * 60)
    log("🎉 支付成功！无须支付！(Stripe 终态页)")
    if text:
        log(f"   终态文案: {str(text)[:120]!r}")
    if url:
        log(f"   final url: {url}")
    log("=" * 60)
    log_step("🎉 支付成功！无须支付！流程结束")


def _should_short_circuit() -> bool:
    """handler 内的步骤间检查:成功/终止已置位就静默退出,不打误导日志。"""
    return _SUCCESS_FLAG or _ABORT_FLAG


def _stripe_amount_is_zero(amount_str: str) -> bool:
    """判定金额字符串是否为 0。'US$0.00' / '$0' / 'JP¥0' / '€0,00' 都视为 0。"""
    if not amount_str:
        return False
    # 提取第一个数字片段(支持 1,234.56 / 1.234,56 / 0.00 等)
    m = re.search(r"(\d[\d,\.]*)", amount_str)
    if not m:
        return False
    raw = m.group(1)
    # 把 1,234.56 -> 1234.56;1.234,56 -> 1234.56;0.00 -> 0.00
    if raw.count(",") > 0 and raw.count(".") > 0:
        # 既有 , 又有 .,根据最右侧符号判断小数点
        if raw.rfind(",") > raw.rfind("."):
            raw = raw.replace(".", "").replace(",", ".")
        else:
            raw = raw.replace(",", "")
    elif raw.count(",") > 0 and raw.count(".") == 0:
        # 只有 ,:可能是千分号或欧式小数点。简化为千分号(0,00 罕见但仍接受)
        if len(raw.split(",")[-1]) == 2:  # ",00" 形式 -> 欧式小数
            raw = raw.replace(",", ".")
        else:
            raw = raw.replace(",", "")
    try:
        return float(raw) == 0.0
    except ValueError:
        return False


def handle_openai_stripe(page: Page) -> None:
    """OpenAI/Stripe 页面（加速版）：
    - 入口先读总金额,非 0 直接终止流程(置 _ABORT_FLAG)
    - 入口去掉 time.sleep(2),直接靠 wait_for_selector 等按钮挂载
    - PayPal 选中后用 wait_for_selector 等 #billingAddressLine1 出现,替代 time.sleep(1.5)
    - 3 个 billing 字段用 batch_fill_by_id 一次填(原本 3 次 fill_by_id ≈ 3*slow_mo)
    - PayPal 按钮点击重试改为更密集轮询(0.2s × 4 polls vs 0.7s × 1)
    - 填表后验证 city/zip/line1 非空,空则重试一次(防 React 重渲染丢失)
    """
    global _ABORT_FLAG, _ABORT_REASON
    log("=== OpenAI/Stripe Page ===")
    log_step_begin("GPT/Stripe 支付页")
    t0 = time.time()

    # ---- 入口短路:Stripe 终态页(已完成/会话超时) ----
    end_text = _detect_stripe_end_state(page)
    if end_text:
        _mark_paid_no_action_needed(end_text, page.url)
        log_step_end("GPT/Stripe 支付页", extra="终态页,无须支付")
        return

    # ---- 金额检查:必须 $0 才走自动支付流程 ----
    log_step_begin("读取订阅金额")
    try:
        page.wait_for_selector(
            '#ProductSummary-totalAmount, [data-testid="product-summary-total-amount"]',
            state="attached", timeout=8000,
        )
    except Exception:  # noqa: BLE001
        # 8s 内 ProductSummary 没出现:可能页面已经是 Stripe 终态页
        end_text_mid = _detect_stripe_end_state(page)
        if end_text_mid:
            _mark_paid_no_action_needed(end_text_mid, page.url)
            log_step_end("读取订阅金额", extra="终态页,无须读金额")
            log_step_end("GPT/Stripe 支付页", extra="终态页,无须支付")
            return
        log("⚠️ ProductSummary-totalAmount 8s 内未出现,跳过金额检查继续")
    amount = _read_stripe_total_amount(page)
    log(f"Total amount: {amount!r}")
    if amount and not _stripe_amount_is_zero(amount):
        reason = f"金额非 0: {amount!r},终止流程(不会自动支付)"
        log("=" * 60)
        log(f"❌ {reason}")
        log("=" * 60)
        _ABORT_REASON = reason
        _ABORT_FLAG = True
        log_step_end("读取订阅金额", ok=False, extra=f"金额非0: {amount!r}")
        log_step_end("GPT/Stripe 支付页", ok=False)
        return
    log_step_end("读取订阅金额", extra=f"金额={amount!r}")

    # ---- 短路:等待期间主循环已识别终态(_SUCCESS_FLAG)或金额非 0(_ABORT_FLAG) ----
    if _should_short_circuit():
        log_step_end("GPT/Stripe 支付页", extra="检测到全局完成标志,跳过后续步骤")
        return
    # 再补一次直接探测,防止主循环还没跑到探测节点但终态页已渲染
    end_text_mid2 = _detect_stripe_end_state(page)
    if end_text_mid2:
        _mark_paid_no_action_needed(end_text_mid2, page.url)
        log_step_end("GPT/Stripe 支付页", extra="终态页,无须支付")
        return

    # 实测验证（2026-05-20）：必须用完整事件链派发到 [data-testid="paypal-accordion-item-button"]
    # 这个按钮在 DOM 上是 0×0（a11y 用），但 React onClick 就在它身上。
    # Playwright 的 .click()/force=True 会因为 0×0 被判定不可点。
    target_btn = '[data-testid="paypal-accordion-item-button"]'
    radio_sel = 'input[type="radio"][value="paypal"]'

    def is_paypal_checked() -> bool:
        try:
            return bool(page.evaluate(
                "(sel) => { const r = document.querySelector(sel); return r ? r.checked : false; }",
                radio_sel,
            ))
        except Exception:  # noqa: BLE001
            return False

    # ---- 选择 PayPal 支付方式 ----
    log_step_begin("选择 PayPal 支付方式")
    try:
        page.wait_for_selector(target_btn, state="attached", timeout=10000)
    except Exception as e:  # noqa: BLE001
        # 按钮没出现:很可能页面已经是 Stripe 终态(已完成/会话超时),再探一次
        end_text2 = _detect_stripe_end_state(page)
        if end_text2:
            _mark_paid_no_action_needed(end_text2, page.url)
            log_step_end("选择 PayPal 支付方式", extra="终态页,无须选择")
            log_step_end("GPT/Stripe 支付页", extra="终态页,无须支付")
            return
        log(f"PayPal button never attached: {e}")
        try:
            ids = page.evaluate(
                "() => Array.from(document.querySelectorAll('[data-testid]'))"
                ".map(e => e.getAttribute('data-testid')).slice(0, 30)"
            )
            log(f"  data-testid (first 30): {ids}")
        except Exception:  # noqa: BLE001
            pass
        log_step_end("选择 PayPal 支付方式", ok=False, extra="按钮未挂载")
        log_step_end("GPT/Stripe 支付页", ok=False)
        return

    paypal_selected = False
    for attempt in range(5):
        click_react(page, target_btn, f"paypal-accordion-button (try {attempt + 1})")
        for _ in range(4):
            time.sleep(0.2)
            if is_paypal_checked():
                paypal_selected = True
                break
        if paypal_selected:
            log(f"PayPal selected ✓ (radio.checked=true) after try {attempt + 1}")
            break
        log(f"  radio still unchecked, attempt {attempt + 1}/5")
    if not paypal_selected:
        log("PayPal selection FAILED after 5 attempts; abort handler")
        log_step_end("选择 PayPal 支付方式", ok=False, extra="5 次重试均失败")
        log_step_end("GPT/Stripe 支付页", ok=False)
        return
    log_step_end("选择 PayPal 支付方式")

    # ---- 等 billing 字段渲染 + 填地址 ----
    log_step_begin("填写 GPT 账单地址")
    try:
        page.wait_for_selector(
            "#billingAddressLine1", state="visible", timeout=5000,
        )
    except Exception:  # noqa: BLE001
        log("  #billingAddressLine1 wait timeout, continue anyway")

    log("Filling billing address (batch)...")
    addr = STRIPE_BILLING_ADDRESS
    billing_payload = {
        "billingAddressLine1": addr["street"],
        "billingLocality":     addr["city"],
        "billingPostalCode":   addr["zip"],
    }
    batch_fill_by_id(page, billing_payload)
    fill_select(page, "billingAdministrativeArea", addr["state"])

    # 填后验证:city/zip 经常被 Stripe React 重渲染清空,空则等再填(最多 2 次)
    def _check_empty_billing() -> list:
        try:
            return page.evaluate(
                """() => {
                    const ids = ['billingAddressLine1', 'billingLocality', 'billingPostalCode'];
                    const empty = [];
                    for (const id of ids) {
                        const el = document.getElementById(id);
                        if (!el || !(el.value || '').trim()) empty.push(id);
                    }
                    return empty;
                }"""
            ) or []
        except Exception:  # noqa: BLE001
            return []

    for retry in range(2):
        empty_fields = _check_empty_billing()
        if not empty_fields:
            log("✓ billing 字段全部已填")
            break
        log(f"  ⚠️ billing 字段空: {empty_fields},等 1s 重填 ({retry + 1}/2)")
        page.wait_for_timeout(1000)
        batch_fill_by_id(page, {
            fid: billing_payload[fid] for fid in empty_fields
            if fid in billing_payload
        })
    # 验证 state 是否真的选成功(value 必须是 2 位 state code)
    try:
        state_val = page.evaluate(
            "() => { const el = document.getElementById('billingAdministrativeArea');"
            " return el ? (el.value || '') : ''; }"
        )
        if state_val != addr["state"]:
            log(f"  ⚠️ state value={state_val!r} 不等于 {addr['state']!r},重选")
            fill_select(page, "billingAdministrativeArea", addr["state"])
    except Exception:  # noqa: BLE001
        pass

    # 勾条款
    check_terms_checkbox(page)
    log_step_end("填写 GPT 账单地址")

    # ---- 等 Subscribe 按钮 --complete 并提交 ----
    log_step_begin("提交 Stripe 订阅")
    submit_sel_complete = (
        'button[data-testid="hosted-payment-submit-button"]:not(.SubmitButton--incomplete)'
    )
    try:
        page.wait_for_selector(submit_sel_complete, state="attached", timeout=5000)
        log("Submit button is --complete, ready to click")
    except Exception:  # noqa: BLE001
        try:
            info = page.evaluate(
                "() => { const b = document.querySelector('[data-testid=\"hosted-payment-submit-button\"]');"
                " return b ? { cls: b.className, disabled: b.disabled } : null; }"
            )
            log(f"Submit button still NOT complete after 5s: {info}")
        except Exception:  # noqa: BLE001
            pass

    # 关闭可能弹出的地址 autocomplete 下拉框(参考无卡 fill_stripe 关键步骤):
    # Escape 关下拉,再点页面左上角空白让焦点离开输入框,避免 Subscribe 误触
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        page.locator("body").click(position={"x": 10, "y": 10}, force=True)
        page.wait_for_timeout(500)
    except Exception:  # noqa: BLE001
        pass

    click_submit(page, extra_selectors=('button[data-testid="hosted-payment-submit-button"]',))
    log_step_end("提交 Stripe 订阅")
    log(f"=== Stripe handler elapsed {time.time() - t0:.1f}s ===")
    log_step_end("GPT/Stripe 支付页")

    # ---- 等跳转到 PayPal (主动轮询 30s+30s,中间重点 1 次,跳后等 3s 稳定) ----
    # 参考无卡 fill_stripe 末尾:60s 内 1s 一次检查 URL,30s 没动重点 Subscribe,
    # 跳成功后 wait 3s 让 PayPal 页面 React/DataDome 稳定,降低风控触发率。
    log_step_begin("等待跳转到 PayPal")
    jumped = False
    for attempt in range(2):
        for _ in range(30):
            try:
                if "paypal.com" in page.url:
                    jumped = True
                    break
            except Exception:  # noqa: BLE001
                pass
            page.wait_for_timeout(1000)
        if jumped:
            break
        if attempt == 0:
            # 30s 未跳转: 检查表单错误并重点 Subscribe
            try:
                err_info = page.evaluate(
                    """() => {
                        const errs = Array.from(document.querySelectorAll(
                            '[role="alert"], .Error, [aria-invalid="true"]'
                        )).map(e => (e.textContent || '').trim().slice(0, 80))
                          .filter(Boolean).slice(0, 3);
                        return errs;
                    }"""
                ) or []
                if err_info:
                    log(f"[Stripe] 检测到表单错误: {err_info}")
            except Exception:  # noqa: BLE001
                pass
            log("[Stripe] 30s 未跳转,重点 Subscribe")
            try:
                btn = page.locator(
                    'button[data-testid="hosted-payment-submit-button"]'
                ).first
                if btn.is_visible(timeout=1000) and not btn.is_disabled():
                    btn.click(timeout=5000)
            except Exception as e:  # noqa: BLE001
                log(f"[Stripe] 重点失败: {e}")

    if jumped:
        # 关键:跳成功后等 3s 让 PayPal 页面稳定(无卡同款,降低风控触发)
        page.wait_for_timeout(3000)
        # 无头模式 PayPal 风控严,等到 DOM ready 再 route,避免 query_selector hang
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception as e:  # noqa: BLE001
            log_step(f"⚠️ paypal.com domcontentloaded 等待超时: {e}; 继续")
        try:
            log_step_end("等待跳转到 PayPal", extra=f"url={page.url[:80]}")
        except Exception:  # noqa: BLE001
            log_step_end("等待跳转到 PayPal")
        # 链式调用:wait_for_url 期间持有 _HANDLER_BUSY 锁,paypal.com 的 nav 事件
        # 会被 skip 不会触发 handle_paypal_checkout。直接在当前锁上下文里 route 一次。
        try:
            new_url = page.url
            # 标记已处理,避免后续 nav 事件再跑一遍
            PROCESSED_URLS.add(new_url)

            # === DataDome 绕过:拼 ul=1 + paypal_client_cfci 后缀,
            #     curl HEAD 拿服务端 302 → 直接 page.goto 跳到真实表单页 ===
            if os.environ.get("PP_FAST_PATH", "1") != "0":
                target_302 = _paypal_resolve_fast_path(new_url)
                if target_302 and target_302 != new_url:
                    log_step(f"302 fast-path: page.goto({target_302[:100]})")
                    try:
                        page.goto(
                            target_302, wait_until="domcontentloaded",
                            timeout=30000,
                        )
                        page.wait_for_timeout(2000)
                        PROCESSED_URLS.add(page.url)
                        log_step(f"302 fast-path 跳转后 url={page.url[:100]}")
                    except Exception as e:  # noqa: BLE001
                        log_step(f"⚠️ 302 fast-path page.goto 失败: {e}")

            log_step("链式 route paypal.com")
            route_page(page)
            log_step("链式 route paypal.com 返回")
        except Exception as e:  # noqa: BLE001
            log_step(f"⚠️ 链式 route 失败: {e}")
    else:
        # 60s 未跳转:dump 诊断
        try:
            info = page.evaluate(
                """() => {
                    const b = document.querySelector('[data-testid="hosted-payment-submit-button"]');
                    const errs = Array.from(document.querySelectorAll(
                        '[role="alert"], .Error, [aria-invalid="true"]'
                    )).map(e => (e.textContent || '').trim().slice(0, 120))
                      .filter(Boolean).slice(0, 5);
                    return {
                        url: location.href,
                        button: b ? {
                            cls: b.className.slice(0, 120),
                            disabled: b.disabled,
                        } : null,
                        errors: errs,
                    };
                }"""
            ) or {}
            log(f"  诊断 dump: {info}")
            log_step_end(
                "等待跳转到 PayPal", ok=False,
                extra=f"60s 未跳转 url={info.get('url','?')[:80]}",
            )
        except Exception as e:  # noqa: BLE001
            log_step_end("等待跳转到 PayPal", ok=False, extra=f"诊断失败:{e}")


def handle_paypal_login(page: Page) -> None:
    """PayPal /signin 或 /agreements/approve 页面。
    可能直接显示 #onboardingFlow section（含 #onboardingFlowEmail），
    也可能显示 #login section（需要先点 #startOnboardingFlow 切换）。
    无论哪种情况，最终都要走 onboarding 填邮箱+提交。"""
    log("=== PayPal Login/Agreements page ===")
    time.sleep(1)

    # 情况 A: onboarding 元素已经挂在 DOM 上（PayPal 新版 /agreements/approve 走这条）
    try:
        page.wait_for_selector("#onboardingFlowEmail", state="attached", timeout=2500)
        log("Onboarding section in DOM, going straight to fill+submit")
        handle_paypal_onboarding(page)
        return
    except Exception:  # noqa: BLE001
        pass

    # 情况 B: 当前没 onboarding section，先点 #startOnboardingFlow 切换
    log("#onboardingFlowEmail not in DOM; clicking #startOnboardingFlow to switch")
    click_react(page, "#startOnboardingFlow", "startOnboardingFlow")

    # 等切换后 #onboardingFlowEmail 出现，然后 fall-through 到 onboarding 处理
    try:
        page.wait_for_selector("#onboardingFlowEmail", state="attached", timeout=8000)
        log("Onboarding section now in DOM after switch")
        handle_paypal_onboarding(page)
    except Exception as e:  # noqa: BLE001
        log(f"#onboardingFlowEmail never appeared after click: {e}")


def handle_paypal_onboarding(page: Page) -> None:
    """PayPal 注册引导页（#onboardingFlow section 可见）：
    填 #onboardingFlowEmail 随机 gmail，提交 form[name="beginOnboardingFlow"]。
    提交前等 5-8 秒页面充分加载 + DataDome 就绪（防 captcha）。"""
    log("=== PayPal Onboarding (input email + submit) ===")
    # 进入页面先等 5-8s 加载（PayPal /agreements/approve 风控敏感，给页面足够时间）
    initial_wait = random.uniform(5.0, 8.0)
    log(f"  initial wait {initial_wait:.1f}s for page load")
    time.sleep(initial_wait)
    wait_for_datadome_ready(page, timeout_s=10.0)
    time.sleep(random.uniform(1.2, 2.5))
    email = rand_gmail()
    log(f"Email: {email}")
    fill_by_id(page, "onboardingFlowEmail", email)
    time.sleep(0.5)

    # 提交 form 内唯一的 submit 按钮（用 form name + type 锁定，零文案）
    js = """
    () => {
        const form = document.querySelector('form[name="beginOnboardingFlow"]');
        if (!form) return { ok: false, reason: 'form_not_found' };
        const btn = form.querySelector('button[type="submit"]');
        if (!btn) return { ok: false, reason: 'submit_not_found' };
        try { btn.scrollIntoView({ block: 'center' }); } catch(e) {}
        btn.click();
        return { ok: true };
    }
    """
    try:
        result = page.evaluate(js)
    except Exception as e:  # noqa: BLE001
        log(f"onboarding submit eval error: {e}")
        return
    if result.get("ok"):
        log("Clicked onboarding submit")
    else:
        log(f"onboarding submit failed ({result.get('reason')})")


def wait_for_datadome_ready(page: Page, timeout_s: float = 12.0) -> bool:
    """等 DataDome 反爬探针完成初始化。
    DataDome 跑完后会在 document.cookie 写入 'datadome=...'，
    任何后续 form submit / API 调用才会带上正确的 x-datadome-clientid。
    在它准备好之前点击关键按钮 → 100% captcha。"""
    js_check = """
    () => {
        const hasCookie = /datadome=/.test(document.cookie);
        // PayPal 用的 DataDome 全局
        const hasGlobal = typeof window.ddjskey !== 'undefined'
                       && typeof window.ddoptions !== 'undefined';
        // tags.js 是否已 inject
        const tagsLoaded = !!document.querySelector('script[src*="ddbm2"]')
                       || !!document.querySelector('script[src*="datadome"]');
        return { hasCookie, hasGlobal, tagsLoaded };
    }
    """
    deadline = time.time() + timeout_s
    last_state = {}
    while time.time() < deadline:
        try:
            last_state = page.evaluate(js_check) or {}
            if last_state.get("hasCookie") and last_state.get("hasGlobal"):
                log(f"  DataDome ready ✓ (cookie set, globals present)")
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    log(f"  DataDome wait timeout after {timeout_s}s; state={last_state}")
    return False


def _humanlike_click(page: Page, selector: str, label: str = "?") -> bool:
    """模拟人类点击：鼠标先慢慢移动到按钮中心，悬停一会儿，再点。
    比 click_react 慢但行为指纹更像真人，专门用在 captcha 容易触发的关键点击。"""
    handle = page.query_selector(selector)
    if not handle:
        log(f"  humanlike_click: {selector} not found ({label})")
        return False
    try:
        box = handle.bounding_box()
        if not box:
            log(f"  humanlike_click: no bounding_box ({label})")
            return False
        # 目标坐标：按钮中心 + 微小随机偏移（避免每次点正中心）
        cx = box["x"] + box["width"] / 2 + random.uniform(-box["width"] / 4, box["width"] / 4)
        cy = box["y"] + box["height"] / 2 + random.uniform(-box["height"] / 4, box["height"] / 4)
        # Playwright 自带 steps 参数会生成多个中间 mousemove 事件（带轨迹）
        steps = random.randint(15, 30)
        page.mouse.move(cx, cy, steps=steps)
        # hover 一会儿模拟人在看按钮
        time.sleep(random.uniform(0.4, 1.0))
        page.mouse.down()
        time.sleep(random.uniform(0.05, 0.15))
        page.mouse.up()
        log(f"  humanlike_click: {label} @ ({cx:.0f}, {cy:.0f}) steps={steps}")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"  humanlike_click error ({label}): {e}")
        return False


def handle_paypal_create_account_choice(page: Page) -> None:
    """PayPal 新版 /pay 路径上的 'Pay with Card / Create an Account' 选择页。
    这一步**最容易触发 DataDome captcha**——必须满足两个条件再点：
    1. DataDome 探针完成初始化（写入 datadome= cookie）
    2. 点击动作"像人"（鼠标轨迹 + 停顿）"""
    log("=== PayPal Pay with Card -> Create an Account (humanlike) ===")

    # 1) 等 DataDome 探针准备好（DataDome 没就绪时点击 = 100% captcha）
    if not wait_for_datadome_ready(page, timeout_s=12.0):
        log("  Continuing without confirmed DataDome ready (risky)")

    # 2) 进入页面后随机等 1-3 秒，模拟用户阅读时间
    time.sleep(random.uniform(1.5, 3.0))

    sel = (
        'button[data-atomic-wait-task="login_create_account"]'
        '[data-atomic-wait-viewname="email"]'
    )
    if not page.query_selector(sel):
        log("Create an Account button not present; skip")
        return

    # 3) 先轻微滚动页面（模拟用户浏览）
    try:
        page.mouse.wheel(0, random.randint(50, 150))
        time.sleep(random.uniform(0.3, 0.8))
        page.mouse.wheel(0, -random.randint(20, 80))
        time.sleep(random.uniform(0.3, 0.6))
    except Exception:  # noqa: BLE001
        pass

    # 4) 人类化点击
    for attempt in range(3):
        if _humanlike_click(page, sel, f"create-account-choice (try {attempt + 1})"):
            # 点击后等待 URL/DOM 变化（最多 8 秒）
            for _ in range(16):
                time.sleep(0.5)
                if not page.query_selector(sel):
                    log(f"Create an Account 已跳转 ✓ after try {attempt + 1}")
                    return
        else:
            time.sleep(1)
    log("Create an Account still present after 3 humanlike tries")


def handle_paypal_create_account_form(page: Page) -> None:
    """PayPal 新版 /pay 路径上的 'Create a PayPal account' 邮箱页：
    填 #login_email 随机 gmail，点 button[data-testid="continueButton"]。
    点击前等 DataDome 就绪（防 captcha）。"""
    log("=== PayPal Create Account Form (input email + continue) ===")
    wait_for_datadome_ready(page, timeout_s=10.0)
    time.sleep(random.uniform(1.2, 2.5))

    email = rand_gmail()
    log(f"Email: {email}")
    fill_by_id(page, "login_email", email)
    time.sleep(0.5)

    submit_sel = 'button[data-testid="continueButton"]'
    if not page.query_selector(submit_sel):
        log("continueButton not present; skip")
        return
    for attempt in range(3):
        click_react(page, submit_sel, f"continueButton (try {attempt + 1})")
        time.sleep(1.5)
        if not page.query_selector(submit_sel):
            log(f"continueButton 已跳转 ✓ after try {attempt + 1}")
            break

    # 可能触发 OTP 弹窗
    handle_otp_if_present(page)


def handle_paypal_consent(page: Page) -> None:
    """PayPal /pay/billing 等同意页面：点 #consentButton (Agree and Continue)。
    点击后通常跳回商户站完成结账，或继续到下一步 PayPal 流程。
    点击前等 DataDome 就绪（防 captcha）。"""
    log("=== PayPal Consent (Agree and Continue) ===")
    wait_for_datadome_ready(page, timeout_s=10.0)
    time.sleep(random.uniform(1.2, 2.5))

    if not page.query_selector('#consentButton'):
        log("#consentButton not present; skip")
        return

    for attempt in range(3):
        click_react(page, '#consentButton', f'consentButton (try {attempt + 1})')
        time.sleep(1.5)
        # 点击后页面通常会跳转或 button 消失；DOM 里若 #consentButton 仍存在则重试
        if not page.query_selector('#consentButton'):
            log(f"#consentButton 已消失 / 已跳转 ✓ after try {attempt + 1}")
            break
    else:
        log("#consentButton still present after 3 tries (might need card added)")

    # 同意后可能触发 OTP 弹窗（PayPal 风控）
    handle_otp_if_present(page)


def handle_paypal_error(page: Page) -> None:
    """PayPal 错误终端页（'Something went wrong'）：
    含 #returnToMerchantButton。提取 errorCode / reason 并解码（PayPal 用 base64），
    打日志后**不再操作**，避免错误循环。"""
    import base64

    log("=== PayPal Error Page (terminal) ===")
    try:
        info = page.evaluate(
            """() => {
                const params = new URLSearchParams(location.search);
                const headEl = document.querySelector('h1, h2, .text-legacy-title-medium');
                const bodyEl = document.querySelector('.whitespace-pre-line');
                return {
                    url: location.href,
                    errorCode: params.get('errorCode'),
                    reason: params.get('reason'),
                    heading: headEl ? (headEl.innerText || '').trim() : null,
                    body: bodyEl ? (bodyEl.innerText || '').trim() : null,
                };
            }"""
        )
    except Exception as e:  # noqa: BLE001
        log(f"  failed to extract error info: {e}")
        return

    decoded = {}
    for key in ("errorCode", "reason"):
        val = info.get(key)
        if not val:
            continue
        try:
            # PayPal 用 base64 编码，可能没有 padding
            padded = val + "=" * (-len(val) % 4)
            decoded[key] = base64.b64decode(padded).decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            decoded[key] = "(decode failed)"

    log(f"  url     : {info.get('url')}")
    log(f"  heading : {info.get('heading')}")
    log(f"  body    : {info.get('body')}")
    log(f"  reason  : {info.get('reason')} → {decoded.get('reason', '?')}")
    log(f"  errCode : {info.get('errorCode')} → {decoded.get('errorCode', '?')}")
    log("终止流程，不再自动操作（PayPal 已进入错误终端页）")


# ============================================================================
# PayPal 创建账号 + 绑卡（GPT/Stripe 提交后跳到 PayPal 后的完整流程）
# 完全参考 无卡plus源码/modules/paypal_pay.py 的 fill_paypal 实现（同步版）
# ----------------------------------------------------------------------------
# 关键差异（vs 旧版 handle_paypal_checkout）：
#   - 不再依赖 fill_by_id 固定 ID，改用多选择器兜底（locator + placeholder/
#     name/id/aria-label/autocomplete），适配 PayPal 中英文 / 浮动 label / React 控件
#   - 国家切换用 select_option("US") 触发完整 change 事件链，并验证 + 重试
#   - Street 字段 10 个 selector + label + JS 终极兜底（PayPal 用浮动 label）
#   - 表单完整性 JS 检查 + 缺失字段补填
#   - 卡被拒（"We weren't able to add this card"）检测
#   - 提交后点击 Agree and Continue review 页
# ============================================================================


def _pp_try_fill_first_visible(
    page: Page, selectors_csv: str, value: str, label: str,
    timeout_ms: int = 3000,
) -> bool:
    """从多选择器中找第一个可见元素填入值（参考 fill_paypal 内部填充模式）。"""
    try:
        loc = page.locator(selectors_csv).first
        if loc.is_visible(timeout=timeout_ms):
            try:
                loc.fill("", timeout=2000)
            except Exception:  # noqa: BLE001
                pass
            loc.fill(value, timeout=5000)
            log(f"  填 {label}: ✓")
            return True
    except Exception as e:  # noqa: BLE001
        log(f"  填 {label} 失败: {e}")
    return False


def _pp_click_next_button(page: Page) -> bool:
    """点击 PayPal 邮箱页的"继续/下一步"按钮(零文案,纯结构属性 + JS 兜底)。

    优先级:
      1. data-testid="continueButton" / data-atomic-wait-task / id=btnNext
      2. JS 结构判定:有 password 字段时找非 submit 按钮,否则找 submit 按钮
    """
    attr_selectors = [
        'button[data-testid="continueButton"]',
        'button[data-atomic-wait-task="login_create_account"][data-atomic-wait-viewname="email"]',
        'button#btnNext',
    ]
    for sel in attr_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1500):
                btn.click(timeout=5000)
                log(f"[PayPal] 点击 next: {sel}")
                return True
        except Exception:  # noqa: BLE001
            continue
    # JS 结构兜底
    try:
        ok = page.evaluate(
            """() => {
                const hasPassword = !!document.querySelector('input[type="password"]');
                function isClickable(b) {
                    if (b.disabled) return false;
                    const r = b.getBoundingClientRect();
                    if (r.width < 30 || r.height < 20) return false;
                    const cs = window.getComputedStyle(b);
                    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                    if (Number(cs.opacity || '1') < 0.1) return false;
                    return true;
                }
                const all = Array.from(document.querySelectorAll('button, a[role="button"]'));
                let target = null;
                if (hasPassword) {
                    for (const b of all) {
                        if (b.type === 'submit') continue;
                        if (isClickable(b)) { target = b; break; }
                    }
                } else {
                    for (const b of all) {
                        if (b.type === 'submit' && isClickable(b)) { target = b; break; }
                    }
                    if (!target) for (const b of all) { if (isClickable(b)) { target = b; break; } }
                }
                if (!target) return false;
                target.click();
                return true;
            }"""
        )
        if ok:
            log("[PayPal] 点击 next (JS 结构兜底)")
            return True
    except Exception as e:  # noqa: BLE001
        log(f"[PayPal] JS 结构兜底异常: {e}")
    log("[PayPal] ⚠️ 未找到可点击的下一步按钮")
    return False


def _pp_wait_register_form(
    page: Page, max_iter: int = 30, interval_ms: int = 2000,
) -> bool:
    """等 PayPal 注册表单加载（country select / phone / card 输入框任一出现）。"""
    for _ in range(max_iter):
        page.wait_for_timeout(interval_ms)
        try:
            has_form = page.evaluate(
                """() => {
                    const selects = document.querySelectorAll('select');
                    const hasCountrySelect = Array.from(selects).some(
                        s => s.options.length > 50 || /country/i.test(s.name + s.id)
                    );
                    const hasPhoneInput = !!document.querySelector(
                        'input[name*="phone" i], input[id*="phone" i], '
                        + 'input[placeholder*="Phone" i], input[placeholder*="手机" i]'
                    );
                    const hasCardInput = !!document.querySelector(
                        'input[name*="card" i], input[id*="card" i], '
                        + 'input[placeholder*="Card" i], input[placeholder*="卡号" i]'
                    );
                    return hasCountrySelect || hasPhoneInput || hasCardInput;
                }"""
            )
        except Exception:  # noqa: BLE001
            continue
        if has_form:
            log("[PayPal] 注册表单已加载")
            return True
    log("[PayPal] ⚠️ 等待表单超时")
    return False


def _pp_switch_country_us(page: Page) -> bool:
    """切换国家到 US：先 select_option，失败则 JS 兜底触发 change。"""
    try:
        sel = page.locator(
            'select[name*="country" i], select[id*="country" i]'
        ).first
        if sel.is_visible(timeout=3000):
            sel.select_option("US", timeout=5000)
            log("[PayPal] Country -> US (select_option)")
            return True
    except Exception:  # noqa: BLE001
        pass
    try:
        page.evaluate(
            """() => {
                const selects = Array.from(document.querySelectorAll('select'));
                for (const sel of selects) {
                    const opt = Array.from(sel.options).find(o => o.value === 'US');
                    if (opt && (
                        sel.name.toLowerCase().includes('country') ||
                        sel.id.toLowerCase().includes('country') ||
                        sel.options.length > 50
                    )) {
                        sel.value = 'US';
                        sel.dispatchEvent(new Event('input', {bubbles: true}));
                        sel.dispatchEvent(new Event('change', {bubbles: true}));
                        break;
                    }
                }
            }"""
        )
        log("[PayPal] Country -> US (js fallback)")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"[PayPal] Country switch failed: {e}")
        return False


def _pp_verify_country_us(page: Page) -> bool:
    """确认国家已切到 US，若否再切一次。"""
    try:
        current = page.evaluate(
            """() => {
                const selects = Array.from(document.querySelectorAll('select'));
                for (const sel of selects) {
                    if (
                        sel.name.toLowerCase().includes('country') ||
                        sel.id.toLowerCase().includes('country') ||
                        sel.options.length > 50
                    ) {
                        return sel.value;
                    }
                }
                return '';
            }"""
        )
    except Exception:  # noqa: BLE001
        return False
    if current != "US":
        log(f"[PayPal] ⚠️ 国家仍为 {current}，再次切换")
        _pp_switch_country_us(page)
        page.wait_for_timeout(5000)
        return False
    log("[PayPal] ✓ 国家已确认 US")
    return True


def _pp_fill_street(page: Page, street_value: str) -> bool:
    """填 Street（10 个 selector + label + JS 终极兜底，照搬无卡 fill_paypal 实现）。"""
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
            if loc.is_visible(timeout=1500):
                try:
                    loc.fill("", timeout=2000)
                except Exception:  # noqa: BLE001
                    pass
                loc.fill(street_value, timeout=5000)
                log(f"[PayPal] Street ✓ ({sel})")
                return True
        except Exception:  # noqa: BLE001
            continue
    for label_text in ("Street address", "地址"):
        try:
            loc = page.get_by_label(label_text, exact=False).first
            if loc.is_visible(timeout=2000):
                loc.fill(street_value, timeout=5000)
                log(f"[PayPal] Street ✓ (label={label_text})")
                return True
        except Exception:  # noqa: BLE001
            continue
    try:
        ok = page.evaluate(
            """(street) => {
                const inputs = Array.from(
                    document.querySelectorAll('input[type="text"], input:not([type])')
                );
                for (const inp of inputs) {
                    const rect = inp.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) continue;
                    const label = (
                        inp.placeholder || inp.getAttribute('aria-label')
                        || inp.name || inp.id || ''
                    ).toLowerCase();
                    if (/first|last|city|zip|postal|phone|email|apt|suite|bldg/i.test(label)) continue;
                    if ((inp.value || '').trim()) continue;
                    const proto = HTMLInputElement.prototype;
                    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                    desc?.set?.call(inp, street);
                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                    inp.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }
                return false;
            }""",
            street_value,
        )
        if ok:
            log("[PayPal] Street ✓ (js fallback)")
            return True
    except Exception:  # noqa: BLE001
        pass
    log("[PayPal] ⚠️ Street 填写失败")
    return False


def _pp_fill_state(page: Page, state_code: str, state_full: str) -> bool:
    """填 State：先 select_option(value=code)，再 label=full，最后退化为 input。"""
    try:
        sel = page.locator(
            'select[name="state"], select[name*="state" i], '
            'select[autocomplete="address-level1"], select[aria-label*="State" i]'
        ).first
        if sel.is_visible(timeout=3000):
            try:
                sel.select_option(value=state_code, timeout=5000)
                log(f"[PayPal] State ✓ (value={state_code})")
                return True
            except Exception:  # noqa: BLE001
                try:
                    sel.select_option(label=state_full, timeout=3000)
                    log(f"[PayPal] State ✓ (label={state_full})")
                    return True
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        pass
    try:
        inp = page.locator(
            'input[name*="state" i], input[placeholder*="State" i], '
            'input[autocomplete="address-level1"]'
        ).first
        if inp.is_visible(timeout=2000):
            try:
                inp.fill("", timeout=2000)
            except Exception:  # noqa: BLE001
                pass
            inp.fill(state_full, timeout=5000)
            log(f"[PayPal] State ✓ (input={state_full})")
            return True
    except Exception:  # noqa: BLE001
        pass
    log("[PayPal] ⚠️ State 填写失败")
    return False


def _pp_check_form_complete(page: Page) -> list[str]:
    """检查表单完整性（参考 fill_paypal 的 empty_fields 检测）。返回空字段名列表。"""
    try:
        return page.evaluate(
            """() => {
                const checks = [
                    {name: 'email',     selectors: 'input[name="email"], input[type="email"]'},
                    {name: 'phone',     selectors: 'input[name*="phone" i], input[id*="phone" i]'},
                    {name: 'card',      selectors: 'input[name="cardnumber"], input[id*="card" i]'},
                    {name: 'firstName', selectors: 'input[name="fname"], input[id*="first" i], input[autocomplete="given-name"], input[placeholder*="First" i]'},
                    {name: 'lastName',  selectors: 'input[name="lname"], input[id*="last" i], input[autocomplete="family-name"], input[placeholder*="Last" i]'},
                    {name: 'street',    selectors: 'input[name*="street" i], input[name*="address" i], input[autocomplete="address-line1"], input[placeholder*="Street" i]'},
                    {name: 'city',      selectors: 'input[name="city"], input[name*="city" i], input[autocomplete="address-level2"], input[placeholder*="City" i]'},
                    {name: 'zip',       selectors: 'input[name*="zip" i], input[name*="postal" i], input[autocomplete="postal-code"], input[placeholder*="ZIP" i]'},
                ];
                const empty = [];
                for (const {name, selectors} of checks) {
                    const els = document.querySelectorAll(selectors);
                    let found = false;
                    for (const el of els) {
                        const rect = el.getBoundingClientRect();
                        if (rect.width > 0 && rect.height > 0) {
                            if ((el.value || '').trim()) { found = true; }
                            break;
                        }
                    }
                    if (!found) empty.push(name);
                }
                return empty;
            }"""
        ) or []
    except Exception:  # noqa: BLE001
        return []


def _pp_is_card_rejected(page: Page) -> bool:
    """卡被拒检测（无卡 _is_card_rejected 同步版）。"""
    try:
        text = ""
        try:
            text = page.locator("body").inner_text(timeout=2500)
        except Exception:  # noqa: BLE001
            text = ""
        normalized = (text or "").replace("’", "'").lower()
        for k in (
            "we weren't able to add this card",
            "check all the details are correct",
            "try a different card",
            "unable to add this card",
            "无法添加此卡",
            "添加此卡失败",
            "请尝试其他卡",
        ):
            if k in normalized:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _pp_click_agree_and_continue(page: Page) -> bool:
    """PayPal review 页点 Agree/Continue（零文案,纯结构属性 + JS 兜底）。

    优先级:
      1. #consentButton(/pay/billing 同意页的标准 id)
      2. button[data-testid="..."] 已知属性
      3. JS 结构判定:页面主区域的可见 type=submit 按钮
    """
    attr_selectors = [
        '#consentButton',
        'button[data-testid="agreeAndContinueButton"]',
        'button[data-testid="submitButton"]',
        'button[data-testid="continueButton"]',
    ]
    for sel in attr_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1200):
                try:
                    btn.scroll_into_view_if_needed(timeout=1200)
                except Exception:  # noqa: BLE001
                    pass
                btn.click(timeout=5000)
                log(f"[PayPal] 已点击 agree/continue: {sel}")
                log_step("同意 PayPal 支付协议")
                page.wait_for_timeout(1500)
                return True
        except Exception:  # noqa: BLE001
            continue
    # JS 结构兜底:页面中最大/最显眼的可见 type=submit 按钮
    try:
        ok = page.evaluate(
            """() => {
                function isClickable(b) {
                    if (b.disabled) return false;
                    const r = b.getBoundingClientRect();
                    if (r.width < 60 || r.height < 24) return false;
                    const cs = window.getComputedStyle(b);
                    if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                    if (Number(cs.opacity || '1') < 0.1) return false;
                    return true;
                }
                // 找页面里最大的可点 submit 按钮(review 页的"Agree and Continue"通常最大)
                const submits = Array.from(document.querySelectorAll(
                    'button[type="submit"], button[name="continue"]'
                )).filter(isClickable);
                if (submits.length === 0) return false;
                submits.sort((a, b) => {
                    const ra = a.getBoundingClientRect();
                    const rb = b.getBoundingClientRect();
                    return (rb.width * rb.height) - (ra.width * ra.height);
                });
                try { submits[0].scrollIntoView({ block: 'center' }); } catch(e) {}
                submits[0].click();
                return true;
            }"""
        )
        if ok:
            log("[PayPal] 已点击 agree/continue (JS 结构兜底)")
            log_step("同意 PayPal 支付协议")
            page.wait_for_timeout(1500)
            return True
    except Exception as e:  # noqa: BLE001
        log(f"[PayPal] agree/continue JS 兜底异常: {e}")
    return False


def handle_paypal_checkoutweb_signup(page: Page) -> None:
    """PayPal /checkoutweb/signup 一步式注册 + checkout 表单（加速版）。

    入口：https://www.paypal.com/checkoutweb/signup?...&token=EC-...
    流程：
      1. 进入即无条件切 US（不识别原状态）
      2. 验证 US 切换成功（最多 2 次重试）
      3. 多字段联合守卫（已填 ≥2 字段则跳过填表）
      4. batch_fill_by_id 一次 evaluate 批量填所有字段（5-8 倍速度）
      5. billingState 单独走 select 模糊匹配
      6. click_submit + OTP
    """
    log("=== PayPal /checkoutweb/signup（加速 + 强制切 US）===")
    log_step_begin("PayPal /checkoutweb 注册")

    # ---- 0) 入口护栏: 等 URL + 关键表单元素就绪,不要在空页面上跑 ----
    log_step_begin("等待 /checkoutweb/signup 表单就绪")
    page_ready = False
    try:
        # URL 必须含 /checkoutweb/
        cur_url = page.url or ""
        if "/checkoutweb/" not in cur_url:
            log(f"[checkoutweb] ⚠️ URL 不含 /checkoutweb/: {cur_url[:120]}")
        # 等关键字段任一可见 (国家选择器 / 卡号 / 邮箱),最多 20s
        page.wait_for_selector(
            'select#country, input#cardNumber, input#cc, input#email, input[name="cardNumber"]',
            state="visible", timeout=20000,
        )
        page_ready = True
        log_step_end("等待 /checkoutweb/signup 表单就绪", extra=f"url={cur_url[:80]}")
    except Exception as e:  # noqa: BLE001
        log_step_end(
            "等待 /checkoutweb/signup 表单就绪", ok=False,
            extra=f"20s 内表单未就绪: {type(e).__name__}",
        )
        log(f"[checkoutweb] 当前 URL: {page.url}")
        try:
            body = page.locator("body").inner_text(timeout=2000)
            log(f"[checkoutweb] body 前 200 字: {body[:200]!r}")
        except Exception:  # noqa: BLE001
            pass
        # 表单未渲染 → 主循环重新打开支付链接走一遍
        global _REOPEN_FLAG, _REOPEN_COUNT
        if _TARGET_URL and _REOPEN_COUNT < _REOPEN_MAX:
            _REOPEN_FLAG = True
            log_step_end(
                "PayPal /checkoutweb 注册", ok=False,
                extra=f"表单未渲染,触发重开支付 ({_REOPEN_COUNT + 1}/{_REOPEN_MAX})",
            )
        else:
            log_step_end(
                "PayPal /checkoutweb 注册", ok=False,
                extra="表单未渲染,达到重开上限或无 target_url",
            )
        return

    # ---- 1) 切国家到 US ----
    log_step_begin("切国家到 US")
    set_country_us_if_needed(page)
    time.sleep(2)  # React 重新渲染地址字段

    # 验证 US 切换成功（最多 2 次重试）
    def _read_country() -> str:
        try:
            return page.evaluate(
                "() => { const el = document.getElementById('country'); "
                "return el ? (el.value || '') : ''; }"
            ) or ""
        except Exception:  # noqa: BLE001
            return ""

    cur = _read_country()
    for retry in range(2):
        if cur == "US":
            break
        log(f"[checkoutweb]   当前 country={cur!r}，重试切 US ({retry + 1}/2)")
        set_country_us_if_needed(page)
        time.sleep(2)
        cur = _read_country()
    if cur != "US":
        log(f"[checkoutweb] ⚠️ 国家仍为 {cur!r}，但继续尝试填表")
        log_step_end("切国家到 US", ok=False, extra=f"最终={cur!r}")
    else:
        log_step_end("切国家到 US", extra="确认=US")

    # ---- 2) 守卫:已填则跳过 ----
    try:
        guard = page.evaluate(
            """() => {
                const ids = ['email', 'phone', 'cardNumber', 'firstName',
                             'lastName', 'billingLine1', 'billingCity'];
                const filled = [];
                for (const id of ids) {
                    const el = document.getElementById(id);
                    if (el && (el.value || '').trim()) filled.push(id);
                }
                return filled;
            }"""
        ) or []
        if len(guard) >= 2:
            log(
                f"=== 已填 {len(guard)} 个字段 ({','.join(guard)})，"
                f"跳过填表步骤 ==="
            )
            log_step_end("PayPal /checkoutweb 注册",
                         extra=f"已填 {len(guard)} 字段,跳过")
            return
    except Exception:  # noqa: BLE001
        pass

    # ---- 3) 抓 US profile + 生成账号 ----
    log_step_begin("抓取 US profile (meiguodizhi)")
    profile = fetch_profile()
    email = rand_email()
    password = rand_password()
    log_step_end("抓取 US profile (meiguodizhi)",
                 extra=f"{profile.first_name} {profile.last_name} card****{profile.card_number[-4:]}")

    # ---- 4) 串行填 PayPal 表单 (无卡 plus 风格: 每字段独立 wait_visible + fill) ----
    # 之前用 batch_fill_by_id 一次 evaluate, 页面没渲染好时全部 not_found 假成功。
    # 现在每字段用 fill_first_matching, 内置 is_visible(timeout=1500) 等待。
    log_step_begin("填 PayPal 表单 (每字段独立 wait)")
    phone_val = phone_local(CONFIG["phone"])

    fill_first_matching(page, "email", [
        'input#email', 'input[name="email"]', 'input[type="email"]',
    ], email)

    fill_first_matching(page, "phone", [
        'input#telephone', 'input[name="telephone"]',
        'input#phone', 'input[name="phone"]', 'input[data-testid="phone"]',
    ], phone_val)

    fill_first_matching(page, "cardNumber", [
        'input#cc', 'input[name="cardNumber"]',
        'input#cardNumber', 'input[name="cardnumber"]',
        'input[autocomplete="cc-number"]',
    ], profile.card_number)

    fill_first_matching(page, "cardExpiry", [
        'input#expiry_value', 'input[name="expiry_value"]',
        'input#cardExpiry', 'input[name="exp-date"]',
        'input[autocomplete="cc-exp"]',
    ], profile.card_expiry)

    fill_first_matching(page, "cardCvv", [
        'input#cvv', 'input[name="cvv"]',
        'input#cardCvv', 'input[autocomplete="cc-csc"]',
    ], profile.card_cvv)

    fill_first_matching(page, "firstName", [
        'input#firstName', 'input[name="firstName"]', 'input[name="fname"]',
        'input[autocomplete="given-name"]',
    ], profile.first_name)

    fill_first_matching(page, "lastName", [
        'input#lastName', 'input[name="lastName"]', 'input[name="lname"]',
        'input[autocomplete="family-name"]',
    ], profile.last_name)

    fill_first_matching(page, "billingLine1", [
        'input#billingLine1', 'input[name="billingLine1"]',
        'input[autocomplete="address-line1"]',
    ], profile.street)

    fill_first_matching(page, "billingCity", [
        'input#billingCity', 'input[name="billingCity"]', 'input[name="city"]',
        'input[autocomplete="address-level2"]',
    ], profile.city)

    fill_first_matching(page, "billingPostalCode", [
        'input#billingPostalCode', 'input[name="billingPostalCode"]',
        'input[name*="zip" i]', 'input[name*="postal" i]',
        'input[autocomplete="postal-code"]',
    ], profile.zip)

    fill_first_matching(page, "password", [
        'input#password', 'input[name="password"]',
        'input[type="password"]:not([type="hidden"])',
    ], password)

    # billingState 是 select,单独处理,多候选: string:CA / CA / California
    fill_select(
        page, "billingState",
        f"string:{profile.state_code}",
        profile.state_code,
        profile.state_full,
    )
    log_step_end("填 PayPal 表单 (每字段独立 wait)")

    # ---- 5) 提交 ----
    log_step_begin("提交 PayPal 注册请求")
    time.sleep(0.3)
    click_submit(page)
    log_step_end("提交 PayPal 注册请求")

    # ---- 6) OTP（若 PayPal 风控触发）----
    handle_otp_if_present(page)

    log_step_end("PayPal /checkoutweb 注册")


def handle_paypal_checkout(page: Page) -> None:
    """逐行照搬 无卡plus源码/modules/paypal_pay.py:fill_paypal 的实现（async→sync）。

    入口：https://www.paypal.com/pay?ssrt=...&token=BA-...&ul=1
    适配：
      - CardInfo 字段 → Profile（meiguodizhi.com 抓取）
      - PhoneInfo.number → CONFIG["phone"]，phone.api_url → CONFIG["sms_api_url"]
      - paypal_password → rand_password()
      - email → rand_email()
      - handle_paypal_captcha → 略（依赖太多打码 helper）；用现有 handle_otp_if_present
      - _generate_local_random_card → fetch_profile() 重新抓 US profile
    """
    log("=== PayPal /pay 创建账号 + 绑卡（逐行照搬无卡 fill_paypal）===")
    log_step_begin("PayPal /pay 注册")

    # ---- 入口护栏: 等 URL + 关键元素就绪 ----
    # 注:state="attached" 先放宽,React 渲染期间元素可能 attached 但 visibility 还没 ready;
    # 找到 attached 后再尝试可见性兜底;实在没有就 dump 页面诊断信息
    log_step_begin("等待 /pay 页面就绪")
    key_selectors = (
        'input#email, input[name="email"], input[name="login_email"], '
        'form[data-testid="xo-onboarding-form"], '
        'form#publicCredentialSubmitForm, '
        'input#cardNumber, input#cc, input[type="email"]'
    )
    page_ready = False
    try:
        cur_url = page.url or ""
        if "paypal.com" not in cur_url:
            log_step(f"⚠️ URL 不在 paypal.com: {cur_url[:120]}")
        page.wait_for_selector(key_selectors, state="attached", timeout=20000)
        page_ready = True
        log_step_end("等待 /pay 页面就绪", extra=f"url={cur_url[:80]}")
    except Exception as e:  # noqa: BLE001
        log_step_end(
            "等待 /pay 页面就绪", ok=False,
            extra=f"20s 内页面未就绪: {type(e).__name__}",
        )
    if not page_ready:
        # === dump 详细诊断:让用户看清无头是被风控了/captcha 了/还是别的状态 ===
        try:
            diag = page.evaluate(
                """() => {
                    const inputs = Array.from(document.querySelectorAll('input'))
                        .slice(0, 15).map(i => ({
                            id: i.id, name: i.name, type: i.type,
                            placeholder: i.placeholder,
                            visible: i.offsetParent !== null,
                        }));
                    const forms = Array.from(document.querySelectorAll('form'))
                        .slice(0, 5).map(f => ({
                            id: f.id, testid: f.getAttribute('data-testid'),
                            action: (f.action || '').slice(0, 80),
                        }));
                    const buttons = Array.from(document.querySelectorAll('button'))
                        .slice(0, 10).map(b => ({
                            testid: b.getAttribute('data-testid'),
                            waitTask: b.getAttribute('data-atomic-wait-task'),
                            text: (b.textContent || '').trim().slice(0, 40),
                        }));
                    return {
                        title: document.title,
                        readyState: document.readyState,
                        bodyTextLen: (document.body && document.body.innerText || '').length,
                        hasHCaptcha: !!document.querySelector(
                            'iframe[src*="hcaptcha"], iframe[title*="captcha" i]'
                        ),
                        hasError: !!document.querySelector(
                            '[class*="error" i], [class*="block" i]'
                        ),
                        inputs, forms, buttons,
                    };
                }"""
            ) or {}
        except Exception as ee:  # noqa: BLE001
            diag = {"diag_error": str(ee)}
        log_step(f"[/pay] 当前 URL: {page.url}")
        log_step(f"[/pay] DOM 诊断: title={diag.get('title')!r} "
                 f"readyState={diag.get('readyState')!r} "
                 f"bodyTextLen={diag.get('bodyTextLen')} "
                 f"hasHCaptcha={diag.get('hasHCaptcha')} "
                 f"hasError={diag.get('hasError')}")
        log_step(f"[/pay] inputs: {diag.get('inputs')}")
        log_step(f"[/pay] forms: {diag.get('forms')}")
        log_step(f"[/pay] buttons: {diag.get('buttons')}")
        try:
            body = page.locator("body").inner_text(timeout=2000)
            log_step(f"[/pay] body 前 400 字: {body[:400]!r}")
        except Exception:  # noqa: BLE001
            pass
        # 保存截图便于排查无头模式风控页面 (PAYPAL_DUMP_DIR 默认 /tmp)
        try:
            import os
            dump_dir = os.environ.get("PAYPAL_DUMP_DIR", "/tmp")
            shot_path = os.path.join(
                dump_dir, f"paypal_pay_not_ready_{int(time.time())}.png"
            )
            page.screenshot(path=shot_path, full_page=True, timeout=5000)
            log_step(f"[/pay] 截图已保存: {shot_path}")
        except Exception as e:  # noqa: BLE001
            log_step(f"[/pay] 截图失败: {e}")
        log_step_end(
            "PayPal /pay 注册", ok=False,
            extra="页面未渲染(可能被无头风控),跳过此次执行",
        )
        return

    # 准备数据（原 fill_paypal 是参数注入 card/phone/password，这里从 Profile 派生）
    log_step_begin("抓取 US profile (meiguodizhi)")
    profile = fetch_profile()
    email = rand_email()
    paypal_password = rand_password()
    phone_raw = CONFIG["phone"]  # 形如 "+15729108922"
    log_step_end("抓取 US profile (meiguodizhi)",
                 extra=f"{profile.first_name} {profile.last_name} card****{profile.card_number[-4:]}")

    # 把 "MM / YY" 拆成 exp_month / exp_year（原 CardInfo 的两个字段）
    exp_match = re.match(r"^\s*(\d{1,2})\s*/\s*(\d{1,4})\s*$", profile.card_expiry or "")
    exp_month = exp_match.group(1).zfill(2) if exp_match else "12"
    exp_year = exp_match.group(2)[-2:] if exp_match else "29"

    # 顶层别名(与原 fill_paypal 内的 card.xxx 一一对应)
    card_number = profile.card_number
    card_cvv = profile.card_cvv
    card_first = profile.first_name
    card_last = profile.last_name
    card_street = profile.street
    card_city = profile.city
    card_state = profile.state_code  # "CA"
    card_zip = profile.zip
    log(
        f"  email={email} phone={phone_raw} "
        f"card ****{card_number[-4:]} {exp_month}/{exp_year} "
        f"{card_first} {card_last} {card_street} {card_city} {card_state} {card_zip}"
    )

    # ============ 以下是 fill_paypal 函数体的同步逐行端口 ============
    log_step_begin("处理邮箱/登录页")
    # 智能等待:邮箱输入框出现即继续(原死等 5s,大多数情况只需 1-2s)
    email_selectors = (
        'input[name="email"], input[type="email"], '
        'input[placeholder*="邮箱" i], input[placeholder*="email" i], '
        'input[placeholder*="手机号" i]'
    )
    try:
        page.wait_for_selector(email_selectors, state="visible", timeout=8000)
    except Exception:  # noqa: BLE001
        # 邮箱框 8s 未出现:可能页面还没渲染,补 2s 兜底
        page.wait_for_timeout(2000)

    # 第一步：填邮箱（确保所有可见的 email 字段都被填写）
    log("[PayPal] 填写邮箱...")
    email_filled = False
    email_fields = page.locator(email_selectors)
    count = email_fields.count()
    for i in range(count):
        field = email_fields.nth(i)
        try:
            if field.is_visible(timeout=2000):
                field.fill("", timeout=2000)
                field.fill(email, timeout=3000)
                email_filled = True
                break
        except Exception:  # noqa: BLE001
            pass
    if not email_filled:
        log("[PayPal] ⚠️ 未找到可见的邮箱输入框，尝试备用选择器")
        try:
            page.locator(
                'input[aria-label*="email" i], input[aria-label*="邮箱" i]'
            ).first.fill(email, timeout=5000)
        except Exception:  # noqa: BLE001
            pass

    # 点下一步(纯结构属性识别,零文案):
    #   两种 PayPal /pay 页面变体:
    #     A. 新版 modxo: form#publicCredentialSubmitForm,主按钮"下一页"
    #        data-atomic-wait-task="login_enter_email"
    #        旁边有个第二按钮"创建账户" login_create_account ← 不要点这个
    #     B. 旧版 xo-onboarding: form[data-testid="xo-onboarding-form"] 内 submit
    #        即"Create an Account"
    #   核心策略: 跨语言纯结构属性识别。"Log In" 永远有 login_with_password,排除掉。
    #   优先级:
    #     1. button[data-atomic-wait-task="login_enter_email"] → 新版"下一页"(若存在,优先点)
    #     2. form[data-testid="xo-onboarding-form"] button[type=submit] → 旧版 "Create an Account"
    #     3. data-testid="continueButton" / login_create_account / #btnNext 兜底
    #     4. JS 兜底: 排除 login_with_password,取最大可见 submit
    clicked_next = False
    click_mode = ""

    next_attr_selectors = [
        # 新版 modxo "下一页" 按钮(若存在则优先,避免误点旁边的"创建账户")
        'button[data-atomic-wait-task="login_enter_email"]',
        # 旧版 PayPal "Create an Account" 按钮所在 form (跨语言稳定特征)
        'form[data-testid="xo-onboarding-form"] button[type="submit"]',
        # 邮箱页 / 选择页常用属性
        'button[data-testid="continueButton"]',
        'button[data-atomic-wait-task="login_create_account"][data-atomic-wait-viewname="email"]',
        'button#btnNext',
    ]
    for sel in next_attr_selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=1200):
                btn.click(timeout=5000)
                clicked_next = True
                click_mode = f"attr:{sel}"
                log(f"[PayPal] 点击 next: {sel}")
                break
        except Exception:  # noqa: BLE001
            continue

    # JS 兜底:取最大的可见 submit 按钮,排除登录(login_with_password)和"创建账户"(login_create_account)
    if not clicked_next:
        try:
            result = page.evaluate(
                """() => {
                    function isClickable(b) {
                        if (b.disabled) return false;
                        const r = b.getBoundingClientRect();
                        if (r.width < 60 || r.height < 24) return false;
                        const cs = window.getComputedStyle(b);
                        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                        if (Number(cs.opacity || '1') < 0.1) return false;
                        return true;
                    }
                    // 0) 新版 modxo "下一页" 按钮: data-atomic-wait-task="login_enter_email"
                    //    若存在则最优先(避免误点同页的"创建账户" login_create_account)
                    const nextBtn = document.querySelector(
                        'button[data-atomic-wait-task="login_enter_email"]'
                    );
                    if (nextBtn && isClickable(nextBtn)) {
                        try { nextBtn.scrollIntoView({ block: 'center' }); } catch(e) {}
                        nextBtn.click();
                        return { ok: true, mode: 'modxo_login_enter_email' };
                    }
                    // 1) 旧版 xo-onboarding-form 内 submit (Create an Account 跨语言定位)
                    const onboarding = document.querySelector(
                        'form[data-testid="xo-onboarding-form"]'
                    );
                    if (onboarding) {
                        const btn = onboarding.querySelector('button[type="submit"]');
                        if (btn && isClickable(btn)) {
                            try { btn.scrollIntoView({ block: 'center' }); } catch(e) {}
                            btn.click();
                            return { ok: true, mode: 'onboarding_form_submit' };
                        }
                    }
                    // 2) 所有 submit,排除 Log In(login_with_password) 和 "创建账户"(login_create_account)
                    //    "创建账户"在新版页面是次按钮,若主按钮 login_enter_email 不存在再考虑
                    const submits = Array.from(
                        document.querySelectorAll('button[type="submit"]')
                    )
                        .filter(b => {
                            const t = b.getAttribute('data-atomic-wait-task');
                            return t !== 'login_with_password' && t !== 'login_create_account';
                        })
                        .filter(isClickable);
                    if (submits.length > 0) {
                        submits.sort((a, b) => {
                            const ra = a.getBoundingClientRect();
                            const rb = b.getBoundingClientRect();
                            return (rb.width * rb.height) - (ra.width * ra.height);
                        });
                        try { submits[0].scrollIntoView({ block: 'center' }); } catch(e) {}
                        submits[0].click();
                        return { ok: true, mode: 'largest_non_login_submit' };
                    }
                    return { ok: false };
                }"""
            ) or {}
            if result.get("ok"):
                clicked_next = True
                click_mode = f"js:{result.get('mode')}"
                log(f"[PayPal] 点击 next (JS 兜底: {result.get('mode')})")
        except Exception as e:  # noqa: BLE001
            log(f"[PayPal] next JS 兜底异常: {e}")

    if not clicked_next:
        log("[PayPal] ⚠️ 未找到可点击的下一步按钮")
        return

    # 点完后等真正离开邮箱/登录页:onboarding form 或 password 字段消失才算成功
    log(f"[PayPal] 已点 next ({click_mode}),等离开邮箱/登录步骤...")
    left_step = False
    for _ in range(20):  # 最多 10s
        page.wait_for_timeout(500)
        try:
            still_here = page.evaluate(
                """() => {
                    const onboarding = document.querySelector(
                        'form[data-testid="xo-onboarding-form"]'
                    );
                    const pwd = document.querySelector('input[type="password"]');
                    return !!onboarding || !!pwd;
                }"""
            )
            if not still_here:
                left_step = True
                break
        except Exception:  # noqa: BLE001
            continue
    if left_step:
        log("[PayPal] ✓ 已离开邮箱/登录步骤")
        log_step_end("处理邮箱/登录页")
    else:
        # 卡住时 dump 页面诊断信息: 哪些按钮存在 / 字段状态 / 错误提示
        try:
            diag = page.evaluate(
                """() => {
                    function info(el) {
                        if (!el) return null;
                        const r = el.getBoundingClientRect();
                        const cs = window.getComputedStyle(el);
                        return {
                            tag: el.tagName,
                            type: el.type || '',
                            id: el.id || '',
                            name: el.name || '',
                            testid: el.getAttribute('data-testid') || '',
                            task: el.getAttribute('data-atomic-wait-task') || '',
                            disabled: !!el.disabled,
                            visible: r.width > 0 && r.height > 0
                                && cs.display !== 'none'
                                && cs.visibility !== 'hidden',
                            value: (el.value || '').slice(0, 30),
                        };
                    }
                    const buttons = Array.from(
                        document.querySelectorAll('button, a[role="button"]')
                    ).filter(b => {
                        const r = b.getBoundingClientRect();
                        return r.width > 30 && r.height > 20;
                    }).slice(0, 10).map(info);
                    const errs = Array.from(document.querySelectorAll(
                        '[role="alert"], .Error, [aria-invalid="true"]'
                    )).map(e => (e.textContent || '').trim().slice(0, 80))
                      .filter(Boolean).slice(0, 5);
                    return {
                        url: location.href,
                        email_input: info(document.querySelector('input#email, input[name="email"]')),
                        password_input: info(document.querySelector('input[type="password"]')),
                        has_onboarding_form: !!document.querySelector(
                            'form[data-testid="xo-onboarding-form"]'
                        ),
                        visible_buttons: buttons,
                        errors: errs,
                    };
                }"""
            )
            log("[PayPal] === 卡住诊断 dump ===")
            log(f"  url: {diag.get('url', '')[:120]}")
            log(f"  email_input: {diag.get('email_input')}")
            log(f"  password_input: {diag.get('password_input')}")
            log(f"  has_onboarding_form (Create an Account form): "
                f"{diag.get('has_onboarding_form')}")
            log(f"  visible buttons (前 10 个):")
            for b in diag.get("visible_buttons", []) or []:
                log(f"    - {b}")
            log(f"  errors: {diag.get('errors')}")
            log("[PayPal] === dump 结束 ===")
        except Exception as e:  # noqa: BLE001
            log(f"[PayPal] 诊断 dump 失败: {e}")
        log("[PayPal] ⚠️ 10s 后仍在邮箱/登录页,继续尝试后续流程")
        log_step_end("处理邮箱/登录页", ok=False, extra="10s 未离开")

    # 等待 PayPal 页面加载完成（按钮转圈结束，新页面元素出现）
    log_step_begin("等待 PayPal 注册表单加载")
    found_form = False
    for _ in range(30):
        page.wait_for_timeout(2000)
        try:
            has_form = page.evaluate(
                """() => {
                    const selects = document.querySelectorAll('select');
                    const hasCountrySelect = Array.from(selects).some(
                        s => s.options.length > 50 || /country/i.test(s.name + s.id)
                    );
                    const hasPhoneInput = !!document.querySelector(
                        'input[name*="phone" i], input[id*="phone" i], '
                        + 'input[placeholder*="Phone" i], input[placeholder*="手机" i]'
                    );
                    const hasCardInput = !!document.querySelector(
                        'input[name*="card" i], input[id*="card" i], '
                        + 'input[placeholder*="Card" i], input[placeholder*="卡号" i]'
                    );
                    return hasCountrySelect || hasPhoneInput || hasCardInput;
                }"""
            )
        except Exception:  # noqa: BLE001
            continue
        if has_form:
            found_form = True
            break
    if found_form:
        log_step_end("等待 PayPal 注册表单加载")
    else:
        log_step_end("等待 PayPal 注册表单加载", ok=False, extra="60s 仍未检测到表单")

    page.wait_for_timeout(2000)

    # 第二步：进入注册表单后，先切国家到 US
    log_step_begin("PayPal 切国家到 US")
    try:
        page.locator('select').first.wait_for(state="attached", timeout=10000)
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(2000)

    log("[PayPal] 切换国家到 United States...")
    country_switched = False
    try:
        country_sel = page.locator(
            'select[name*="country" i], select[id*="country" i]'
        ).first
        if country_sel.is_visible(timeout=3000):
            country_sel.select_option("US", timeout=5000)
            country_switched = True
    except Exception:  # noqa: BLE001
        pass
    if not country_switched:
        page.evaluate(
            """() => {
                const selects = Array.from(document.querySelectorAll('select'));
                for (const sel of selects) {
                    const opt = Array.from(sel.options).find(o => o.value === 'US');
                    if (opt && (
                        sel.name.toLowerCase().includes('country') ||
                        sel.id.toLowerCase().includes('country') ||
                        sel.options.length > 50
                    )) {
                        sel.value = 'US';
                        sel.dispatchEvent(new Event('input', {bubbles: true}));
                        sel.dispatchEvent(new Event('change', {bubbles: true}));
                        break;
                    }
                }
            }"""
        )

    # 国家切换后页面会重新渲染表单字段，必须等待足够时间
    log("[PayPal] 等待页面根据新国家重新加载表单...")
    page.wait_for_timeout(5000)

    try:
        page.locator(
            'input[name*="phone" i], input[id*="phone" i], input[placeholder*="Phone" i]'
        ).first.wait_for(state="visible", timeout=10000)
    except Exception:  # noqa: BLE001
        page.wait_for_timeout(3000)

    # 验证国家是否切换成功
    current_country = page.evaluate(
        """() => {
            const selects = Array.from(document.querySelectorAll('select'));
            for (const sel of selects) {
                if (
                    sel.name.toLowerCase().includes('country') ||
                    sel.id.toLowerCase().includes('country') ||
                    sel.options.length > 50
                ) {
                    return sel.value;
                }
            }
            return '';
        }"""
    )
    if current_country != "US":
        log(f"[PayPal] ⚠️ 国家仍为 {current_country}，再次尝试切换...")
        try:
            country_sel = page.locator(
                'select[name*="country" i], select[id*="country" i]'
            ).first
            country_sel.select_option("US", timeout=5000)
            page.wait_for_timeout(5000)
        except Exception:  # noqa: BLE001
            page.evaluate(
                """() => {
                    const selects = Array.from(document.querySelectorAll('select'));
                    for (const sel of selects) {
                        const opt = Array.from(sel.options).find(o => o.value === 'US');
                        if (opt && (
                            sel.name.toLowerCase().includes('country') ||
                            sel.id.toLowerCase().includes('country') ||
                            sel.options.length > 50
                        )) {
                            sel.value = 'US';
                            sel.dispatchEvent(new Event('input', {bubbles: true}));
                            sel.dispatchEvent(new Event('change', {bubbles: true}));
                            break;
                        }
                    }
                }"""
            )
            page.wait_for_timeout(5000)
        log_step_end("PayPal 切国家到 US",
                     ok=(current_country == "US"),
                     extra=f"最终={current_country!r}")
    else:
        log("[PayPal] ✓ 国家已确认切换为 US")
        log_step_end("PayPal 切国家到 US", extra="确认=US")

    # 再次确认邮箱（国家切换后页面可能重置了邮箱字段）
    try:
        email_fields_after = page.locator('input[name="email"], input[type="email"]')
        count_after = email_fields_after.count()
        for i in range(count_after):
            field = email_fields_after.nth(i)
            if field.is_visible(timeout=1000):
                val = (field.input_value() or "").strip()
                if not val:
                    log("[PayPal] 邮箱字段为空，重新填写...")
                    field.fill(email, timeout=3000)
                break
    except Exception:  # noqa: BLE001
        pass

    log_step_begin("填 PayPal 注册表单 (卡号/手机/地址/密码)")
    # 手机号（去除 +1 前缀）
    phone_local_val = (
        phone_raw.lstrip("+1") if phone_raw.startswith("+1")
        else phone_raw.lstrip("+")
    )
    # ===== 一次 evaluate 并发填所有 input 字段 (9 个) =====
    # 之前 9 个 fill_first_matching 串行 + 每个 wait 500ms = 总 ~5s
    # 现在 1 次 Playwright RPC + 浏览器内 JS 同步全填完 = ~0.1s
    fill_results = batch_fill_with_aliases(page, [
        ("手机号", [
            'input#telephone',                   # AngularJS 旧版
            'input[name="telephone"]',
            'input#phone',                        # React 新版
            'input[name="phone"]',
            'input[data-testid="phone"]',
        ], phone_local_val),
        ("卡号", [
            'input#cc',                           # AngularJS 旧版
            'input[name="cardNumber"]',
            'input#cardNumber',                   # React 新版
            'input[name="cardnumber"]',
            'input[autocomplete="cc-number"]',
        ], card_number),
        ("有效期", [
            'input#expiry_value',                 # AngularJS 旧版
            'input[name="expiry_value"]',
            'input#cardExpiry',                   # React 新版
            'input[name="exp-date"]',
            'input[autocomplete="cc-exp"]',
        ], f"{exp_month}/{exp_year}"),
        ("CVV", [
            'input#cvv',
            'input[name="cvv"]',
            'input#cardCvv',
            'input[autocomplete="cc-csc"]',
        ], card_cvv),
        ("First name", [
            'input#firstName',
            'input[name="firstName"]',
            'input[name="fname"]',
            'input[autocomplete="given-name"]',
        ], card_first),
        ("Last name", [
            'input#lastName',
            'input[name="lastName"]',
            'input[name="lname"]',
            'input[autocomplete="family-name"]',
        ], card_last),
        ("Street", [
            'input#billingLine1',
            'input[name="billingLine1"]',
            'input[autocomplete="address-line1"]',
        ], card_street),
        ("City", [
            'input#billingCity',
            'input[name="billingCity"]',
            'input[name="city"]',
            'input[autocomplete="address-level2"]',
        ], card_city),
        ("ZIP", [
            'input#billingPostalCode',
            'input[name="billingPostalCode"]',
            'input[name*="zip" i]',
            'input[name*="postal" i]',
            'input[autocomplete="postal-code"]',
        ], card_zip),
    ])

    # Street 兜底: 如果上面 batch 没命中,用 JS 找 billing 区域第一个空 text input
    street_filled = fill_results.get("Street", {}).get("ok", False)
    if not street_filled:
        # JS 终极兜底
        try:
            ok = page.evaluate(
                """(street) => {
                    const inputs = Array.from(
                        document.querySelectorAll('input[type="text"], input:not([type])')
                    );
                    for (const inp of inputs) {
                        const rect = inp.getBoundingClientRect();
                        if (rect.width <= 0 || rect.height <= 0) continue;
                        const label = (
                            inp.placeholder || inp.getAttribute('aria-label')
                            || inp.name || inp.id || ''
                        ).toLowerCase();
                        if (/first|last|city|zip|postal|phone|email|apt|suite|bldg/i.test(label)) continue;
                        if ((inp.value || '').trim()) continue;
                        const proto = HTMLInputElement.prototype;
                        const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                        desc?.set?.call(inp, street);
                        inp.dispatchEvent(new Event('input', {bubbles: true}));
                        inp.dispatchEvent(new Event('change', {bubbles: true}));
                        return true;
                    }
                    return false;
                }""",
                card_street,
            )
            if ok:
                log_step(f"填 Street: JS 兜底 ✓")
        except Exception:  # noqa: BLE001
            pass
    # 密码也并入一次 batch (再做一次 evaluate,不能合并因 batch 上面已 return)
    batch_fill_with_aliases(page, [
        ("密码", [
            'input#password',
            'input[name="password"]',
            'input[type="password"]:not([type="hidden"])',
        ], paypal_password),
    ])

    # State (select 下拉,AngularJS option value 是 "string:CA" / React 是 "CA")
    state_done = fill_select(
        page, "billingState",
        f"string:{card_state}",   # AngularJS ngOptions
        card_state,                # React 标准 value
        profile.state_full,        # label 兜底
    )
    if state_done:
        log_step(f"填 State: {card_state} ({profile.state_full})")
    else:
        # State 可能是 input 而非 select(罕见)
        fill_first_matching(page, "State (input 兜底)", [
            'input[name="billingState"]',
            'input#billingState',
            'input[autocomplete="address-level1"]',
        ], card_state)

    page.wait_for_timeout(300)

    # 最终检查：确认所有关键字段已填写
    log("[PayPal] 检查表单完整性...")
    empty_fields = page.evaluate(
        """() => {
            const checks = [
                {name: 'email',     selectors: 'input[name="email"], input[type="email"]'},
                {name: 'phone',     selectors: 'input[name*="phone" i], input[id*="phone" i]'},
                {name: 'card',      selectors: 'input[name="cardnumber"], input[id*="card" i]'},
                {name: 'firstName', selectors: 'input[name="fname"], input[id*="first" i], input[autocomplete="given-name"], input[placeholder*="First" i]'},
                {name: 'lastName',  selectors: 'input[name="lname"], input[id*="last" i], input[autocomplete="family-name"], input[placeholder*="Last" i]'},
                {name: 'street',    selectors: 'input[name*="street" i], input[name*="address" i], input[autocomplete="address-line1"], input[placeholder*="Street" i]'},
                {name: 'city',      selectors: 'input[name="city"], input[name*="city" i], input[autocomplete="address-level2"], input[placeholder*="City" i]'},
                {name: 'zip',       selectors: 'input[name*="zip" i], input[name*="postal" i], input[autocomplete="postal-code"], input[placeholder*="ZIP" i]'},
            ];
            const empty = [];
            for (const {name, selectors} of checks) {
                const els = document.querySelectorAll(selectors);
                let found = false;
                for (const el of els) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width > 0 && rect.height > 0) {
                        if ((el.value || '').trim()) { found = true; }
                        break;
                    }
                }
                if (!found) empty.push(name);
            }
            return empty;
        }"""
    ) or []
    if empty_fields:
        log(f"[PayPal] ⚠️ 以下字段仍为空: {empty_fields}，尝试补填...")
        if "email" in empty_fields:
            try:
                page.locator(
                    'input[name="email"], input[type="email"]'
                ).first.fill(email, timeout=3000)
            except Exception:  # noqa: BLE001
                pass
        if "phone" in empty_fields:
            try:
                page.locator(
                    'input[name*="phone" i], input[id*="phone" i]'
                ).first.fill(phone_local_val, timeout=3000)
            except Exception:  # noqa: BLE001
                pass
        if "street" in empty_fields:
            try:
                loc = page.locator(
                    'input[placeholder*="Street" i], input[name*="street" i], '
                    'input[name*="address" i]:not([name*="email" i]), '
                    'input[aria-label*="Street" i], input[id*="street" i], '
                    'input[id*="address" i]:not([id*="email" i])'
                ).first
                if loc.is_visible(timeout=2000):
                    loc.fill(card_street, timeout=3000)
                else:
                    loc = page.get_by_label("Street address", exact=False).first
                    loc.fill(card_street, timeout=3000)
            except Exception:  # noqa: BLE001
                page.evaluate(
                    """(street) => {
                        const inputs = Array.from(
                            document.querySelectorAll('input[type="text"], input:not([type])')
                        );
                        for (const inp of inputs) {
                            const rect = inp.getBoundingClientRect();
                            if (rect.width <= 0 || rect.height <= 0) continue;
                            const label = (
                                inp.placeholder || inp.getAttribute('aria-label')
                                || inp.name || inp.id || ''
                            ).toLowerCase();
                            if (/first|last|city|zip|postal|phone|email|apt|suite|bldg/i.test(label)) continue;
                            if ((inp.value || '').trim()) continue;
                            const proto = HTMLInputElement.prototype;
                            const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                            desc?.set?.call(inp, street);
                            inp.dispatchEvent(new Event('input', {bubbles: true}));
                            inp.dispatchEvent(new Event('change', {bubbles: true}));
                            return;
                        }
                    }""",
                    card_street,
                )
        if "city" in empty_fields:
            try:
                loc = page.locator(
                    'input[placeholder*="City" i], input[name*="city" i]'
                ).first
                loc.fill(card_city, timeout=3000)
            except Exception:  # noqa: BLE001
                pass
        page.wait_for_timeout(1000)

    # ============ 卡补填工具（原 fill_paypal 内 _refill_card_fields 闭包） ============
    def _refill_card_fields(c_number: str, c_exp_m: str, c_exp_y: str, c_cvv: str) -> None:
        try:
            ci = page.locator(
                'input[name="cardnumber"], input[id*="card" i], input[placeholder*="Card" i]'
            ).first
            ci.fill("", timeout=2000)
            ci.fill(c_number, timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(300)
        try:
            ei = page.locator(
                'input[name="exp-date"], input[id*="exp" i], input[placeholder*="Expir" i]'
            ).first
            ei.fill("", timeout=2000)
            ei.fill(f"{c_exp_m}/{c_exp_y}", timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(300)
        try:
            vi = page.locator(
                'input[name="cvv"], input[id*="cvv" i], input[placeholder*="CVV" i]'
            ).first
            vi.fill("", timeout=2000)
            vi.fill(c_cvv, timeout=5000)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(300)

    def _is_card_rejected() -> bool:
        try:
            body_text = ""
            try:
                body_text = page.locator("body").inner_text(timeout=2500)
            except Exception:  # noqa: BLE001
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
            err = page.locator(
                'text=We weren’t able to add this card, '
                'text=We weren\'t able to add this card, '
                'text=try a different card, '
                'text=无法添加此卡, '
                'text=请尝试其他卡'
            ).first
            if err.is_visible(timeout=600):
                return True
            return False
        except Exception:  # noqa: BLE001
            return False

    # Agree & Create Account 提交(零文案,只用结构属性):
    #   PayPal /pay 注册表单的提交按钮就是 form 的 type="submit",JS 取最大可见 submit。
    #   如遇卡被拒,自动换 profile 重抓 US 卡重试一次。
    def _click_create_account_submit() -> bool:
        # 结构属性优先
        for sel in (
            'button[data-testid="submitButton"]',
            'button[data-testid="continueButton"]',
            'button#btnLogin',
        ):
            try:
                b = page.locator(sel).first
                if b.is_visible(timeout=1200):
                    b.click(timeout=8000)
                    log(f"[PayPal] 点击 create-account submit: {sel}")
                    return True
            except Exception:  # noqa: BLE001
                continue
        # JS 兜底:页面里最大的可见 type=submit 按钮
        try:
            ok = page.evaluate(
                """() => {
                    function isClickable(b) {
                        if (b.disabled) return false;
                        const r = b.getBoundingClientRect();
                        if (r.width < 60 || r.height < 24) return false;
                        const cs = window.getComputedStyle(b);
                        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                        if (Number(cs.opacity || '1') < 0.1) return false;
                        return true;
                    }
                    const submits = Array.from(document.querySelectorAll('button[type="submit"]'))
                        .filter(isClickable);
                    if (submits.length === 0) return false;
                    submits.sort((a, b) => {
                        const ra = a.getBoundingClientRect();
                        const rb = b.getBoundingClientRect();
                        return (rb.width * rb.height) - (ra.width * ra.height);
                    });
                    try { submits[0].scrollIntoView({ block: 'center' }); } catch(e) {}
                    submits[0].click();
                    return true;
                }"""
            )
            if ok:
                log("[PayPal] 点击 create-account submit (JS 结构兜底)")
                return True
        except Exception as e:  # noqa: BLE001
            log(f"[PayPal] create-account submit JS 异常: {e}")
        return False

    log_step_end("填 PayPal 注册表单 (卡号/手机/地址/密码)")

    log_step_begin("提交 PayPal Create Account")
    max_regen = 1
    cur_number, cur_m, cur_y, cur_cvv = card_number, exp_month, exp_year, card_cvv
    for regen_idx in range(max_regen + 1):
        if not _click_create_account_submit():
            log("[PayPal] Create Account 提交按钮未找到")
            log_step_end("提交 PayPal Create Account", ok=False, extra="按钮未找到")
            log_step_end("PayPal /pay 注册", ok=False)
            return
        page.wait_for_timeout(2500)

        # 跳过 handle_paypal_captcha；只等一段时间让 PayPal 处理风控
        page.wait_for_timeout(1200)

        rejected = False
        for _ in range(6):
            if _is_card_rejected():
                rejected = True
                break
            page.wait_for_timeout(800)
        if rejected:
            if regen_idx >= max_regen:
                log("[PayPal] 银行卡被拒，且已达到自动换卡上限")
                log_step_end("提交 PayPal Create Account", ok=False, extra="卡被拒")
                log_step_end("PayPal /pay 注册", ok=False)
                return
            log(
                f"[PayPal] 检测到卡被拒，重新抓 US profile 换卡重试 "
                f"({regen_idx + 1}/{max_regen})"
            )
            new_profile = fetch_profile()
            new_exp_match = re.match(
                r"^\s*(\d{1,2})\s*/\s*(\d{1,4})\s*$", new_profile.card_expiry or ""
            )
            cur_number = new_profile.card_number
            cur_m = new_exp_match.group(1).zfill(2) if new_exp_match else "12"
            cur_y = new_exp_match.group(2)[-2:] if new_exp_match else "29"
            cur_cvv = new_profile.card_cvv
            _refill_card_fields(cur_number, cur_m, cur_y, cur_cvv)
            continue
        break
    log_step_end("提交 PayPal Create Account")

    # SMS 验证码 (handle_otp_if_present 内部自带 begin/end)
    page.wait_for_timeout(2000)
    handle_otp_if_present(page)

    # review 页同意按钮(完全用结构属性,见 _pp_click_agree_and_continue 实现)
    page.wait_for_timeout(1500)
    _pp_click_agree_and_continue(page)

    log_step_end("PayPal /pay 注册")


def route_page(page: Page) -> None:
    global _SUCCESS_FLAG
    try:
        url = page.url
    except Exception:  # noqa: BLE001
        return
    parsed = urlparse(url)
    host = parsed.netloc
    path = parsed.path
    log(f"Host: {host} Path: {path}")

    # ============ 支付成功识别：跳到 ChatGPT 域名即视为成功 ============
    # /payments/success?stripe_session_id=... 是 Stripe 成功回调；
    # 任何 chatgpt.com 路径都意味着 PayPal -> Stripe -> OpenAI 链路走通
    if "chatgpt.com" in host or "chat.openai.com" in host:
        log("=" * 60)
        log("🎉 支付成功！已跳转到 ChatGPT")
        log(f"   final url: {url}")
        log("=" * 60)
        log_step("🎉 支付成功!流程结束")
        _SUCCESS_FLAG = True
        return

    if "pay.openai.com" in host or "checkout.stripe.com" in host:
        # 优先:Stripe 终态页 .FullPageMessage(已完成/会话超时)立即识别
        detected = _detect_stripe_end_state(page)
        if detected:
            _mark_paid_no_action_needed(detected, url)
            return

        # PayPal 完成后会回跳 pay.openai.com 带 redirect_status=succeeded,
        # 这只是一个中转 URL (会自动继续跳到 chatgpt.com),不需要再走 stripe handler
        query = parsed.query or ""
        if "redirect_status=succeeded" in query or "redirect_status=success" in query:
            log("Stripe 回跳带 redirect_status=succeeded,等待自动跳 chatgpt.com,跳过 handler")
            return
        handle_openai_stripe(page)
        return

    if "paypal.com" in host:
        # ====================================================================
        # 完全参考 无卡plus源码/modules/paypal_pay.py:fill_paypal 的方法
        # ----------------------------------------------------------------
        # 关键洞察:无卡的做法是 Stripe Subscribe -> 跳到 paypal.com 后:
        #   1. 等 5 秒(让 PayPal 页面完全加载)
        #   2. 直接找邮箱输入框(多 selector)填邮箱
        #   3. 点击"继续付款"/"Continue"按钮
        #   4. 等表单加载、切 US、填手机/卡/地址/密码、提交
        # 不再拆"Pay with Card 选择按钮"+"邮箱页 continueButton"两步,
        # 不再 humanlike click 分步处理 —— 无卡完全跳过这些中间步骤。
        # ====================================================================
        log_step(f"route_page: 进入 paypal.com 分支 (path={path})")

        # 等页面 DOM 就绪,避免无头下 query_selector 卡住 IO
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception as e:  # noqa: BLE001
            log_step(f"⚠️ wait_for_load_state 超时: {e}; 继续")

        # 一次 evaluate 拿 4 个状态信号,避免多次 sync query_selector
        # (无头模式下连续 query_selector 偶发阻塞 PayPal 风控 main thread)
        try:
            sigs = page.evaluate(
                """() => ({
                    returnToMerchant: !!document.querySelector('#returnToMerchantButton'),
                    consent: !!document.querySelector('#consentButton'),
                    onboardingFlowEmail: !!document.querySelector('#onboardingFlowEmail'),
                    startOnboardingFlow: !!document.querySelector('#startOnboardingFlow'),
                })"""
            ) or {}
        except Exception as e:  # noqa: BLE001
            log_step(f"⚠️ paypal 分支信号探测失败: {e}; 默认全 False")
            sigs = {}
        log_step(f"paypal 状态信号: {sigs}")

        # 1) 错误终端页(Something went wrong + #returnToMerchantButton)优先短路
        if sigs.get("returnToMerchant"):
            handle_paypal_error(page)
            return

        # 2) /pay/billing 同意页:点 Agree and Continue
        if sigs.get("consent"):
            handle_paypal_consent(page)
            return

        # 3) 老版 onboarding 流程(/signin / /agreements/approve)
        #    含 #onboardingFlowEmail / #startOnboardingFlow 时走旧路径
        if sigs.get("onboardingFlowEmail"):
            handle_paypal_onboarding(page)
            return
        if sigs.get("startOnboardingFlow"):
            handle_paypal_login(page)
            return

        # 4a) /checkoutweb/ 旧路径 —— 用旧版 fill_by_id 固定 ID 方式（用户指定）
        if "/checkoutweb/" in path:
            handle_paypal_checkoutweb_signup(page)
            return

        # 4b) 新版 modxo 流程 (/pay, /agreements/approve, /signin) ——
        #     完全按无卡 fill_paypal 走。handle_paypal_checkout 内部
        #     用纯结构属性识别"下一页"/"Create an Account"按钮,跨语言、
        #     跨 URL 路径都能用(2026 PayPal modxo 把入口分散在 /pay 和
        #     /agreements/approve,但页面 DOM 结构是一样的)
        if (
            path.startswith("/pay")
            or path.startswith("/agreements/approve")
            or path.startswith("/signin")
        ):
            handle_paypal_checkout(page)
            return

        # 5) 兜底:其他 paypal.com 路径也试一次 handle_paypal_checkout
        #    若入口护栏 20s 未识别到关键元素会自动返回,不会 hang
        log_step(f"未识别的 paypal 路径 {path},兜底尝试 handle_paypal_checkout")
        handle_paypal_checkout(page)
        return

    log("Page not matched")


# ============================ 主程序 ============================
PROCESSED_URLS: set[str] = set()

# ============================================================================
# 全局执行锁:解决"handler 还没跑完,新 URL 事件就触发下一个 handler"的并发竞争
# ----------------------------------------------------------------------------
# 触发场景:/checkoutweb/signup 提交后 PayPal 跳到 /pay → load/dom/nav 事件触发
# → 新 handler 启动 → 切 US + 重渲染表单 → 同时还在跑的 OTP 流程 DOM 被清掉
# → "Filled 0 digits into split OTP inputs"
# 解决:handler 执行时持锁,持锁期间所有事件被丢弃;handler 完成后释放
# 死锁保护:锁超时 600s 自动强制释放(SMS poll 最长 120s,留余量)
# ============================================================================
_HANDLER_BUSY: bool = False
_HANDLER_LOCK_TIME: float = 0.0
_HANDLER_LOCK_NAME: str = ""
_HANDLER_LOCK_TIMEOUT: float = 600.0

# 支付成功标志：route_page 检测到 chatgpt.com 时置 True，main 主循环看到就退出
_SUCCESS_FLAG: bool = False

# 主动终止标志：金额非 0 / 业务前置校验失败时置 True,main 主循环看到就退出(退出码 2)
_ABORT_FLAG: bool = False
_ABORT_REASON: str = ""

# /checkoutweb/signup 表单未渲染时,主循环用 _REOPEN_URL 重新打开支付链接
_TARGET_URL: str = ""
_REOPEN_FLAG: bool = False
_REOPEN_COUNT: int = 0
_REOPEN_MAX: int = 3


def on_load(page: Page, source: str = "?") -> None:
    global _HANDLER_BUSY, _HANDLER_LOCK_TIME, _HANDLER_LOCK_NAME
    try:
        url = page.url
    except Exception:  # noqa: BLE001
        return
    if not url or url == "about:blank":
        return
    log(f"event[{source}] url={url}")

    # ---- 全局执行锁:有 handler 在跑就丢弃本次事件 ----
    if _HANDLER_BUSY:
        held_s = time.time() - _HANDLER_LOCK_TIME
        if held_s > _HANDLER_LOCK_TIMEOUT:
            log(f"  [LOCK] {_HANDLER_LOCK_NAME} held {held_s:.1f}s > "
                f"{_HANDLER_LOCK_TIMEOUT}s, force release")
            _HANDLER_BUSY = False
        else:
            log(f"  [LOCK] {_HANDLER_LOCK_NAME} busy ({held_s:.1f}s), "
                f"skip this event")
            return

    if url in PROCESSED_URLS:
        log("  (already processed, skip)")
        return
    PROCESSED_URLS.add(url)

    # HIDE_CSS 注入(默认开启)—— 隐藏 #captcha-standalone / .captcha-overlay /
    # .captcha-container / .AddressAutocomplete-results 等覆盖层。
    # 如怀疑影响 PayPal 风控可设 HIDE_OVERLAY_CSS=0 关闭。
    if os.environ.get("HIDE_OVERLAY_CSS", "1") != "0":
        try:
            page.add_style_tag(content=HIDE_CSS)
        except Exception as e:  # noqa: BLE001
            log(f"add_style_tag failed: {e}")

    # 获取锁 + 跑 route_page + 保证释放
    _HANDLER_BUSY = True
    _HANDLER_LOCK_TIME = time.time()
    _HANDLER_LOCK_NAME = f"{source}:{url[:60]}"
    try:
        route_page(page)
    except Exception as e:  # noqa: BLE001
        log(f"route_page error: {e}; will retry next event")
        PROCESSED_URLS.discard(url)
    finally:
        held_s = time.time() - _HANDLER_LOCK_TIME
        log(f"  [LOCK] released after {held_s:.1f}s ({_HANDLER_LOCK_NAME})")
        _HANDLER_BUSY = False
        _HANDLER_LOCK_NAME = ""


def _bind(page: Page) -> None:
    """监听 load / domcontentloaded / framenavigated，三者任一触发即尝试。
    Playwright 会把对应对象传给回调，所以用 *args 吞掉。"""
    page.on("load", lambda *a, **kw: on_load(page, "load"))
    page.on("domcontentloaded", lambda *a, **kw: on_load(page, "dom"))

    def _on_frame_nav(frame, *a, **kw):
        # 只处理主 frame 的导航
        try:
            if frame == page.main_frame:
                on_load(page, "nav")
        except Exception:  # noqa: BLE001
            pass

    page.on("framenavigated", _on_frame_nav)
    log(f"Handlers attached to page (current url: {page.url})")


def attach_handlers(context: BrowserContext) -> None:
    context.on("page", lambda page, *a, **kw: _bind(page))
    for page in context.pages:
        _bind(page)


def main() -> None:
    """完全照搬 无卡plus源码/modules/browser.py:BrowserSession 的浏览器配置。

    关键变化（vs 旧版）：
      - launch_persistent_context 持久化 profile（cookies 跨次保留）
      - 仅 4 个 launch args（与 BrowserSession 完全一致）
      - 固定 viewport 1365×900
      - 不再 random fingerprint / UA / timezone / locale
      - 不再 ipinfo 探针
      - 不再 playwright-stealth
      - 不再 init_script 注入 webdriver/platform/chrome/WebRTC patch
      - 用浏览器**真实指纹**（系统真实 UA + 真实 GPU + 真实时区），降低组合不自然信号
    """
    # ---- 运行参数 (argparse) ----
    # 用法:
    #   python paypal_auto_filler.py                       # 用 CONFIG.target_url,有头
    #   python paypal_auto_filler.py URL                   # 指定 URL,有头
    #   python paypal_auto_filler.py --headless            # 无头 + CONFIG.target_url
    #   python paypal_auto_filler.py URL --headless        # 无头 + 指定 URL
    #   python paypal_auto_filler.py --headless URL        # 同上
    #   HEADLESS=1 python paypal_auto_filler.py            # env 形式仍兼容
    import argparse
    parser = argparse.ArgumentParser(
        description="PayPal 自动填表脚本",
        add_help=True,
    )
    parser.add_argument(
        "url", nargs="?", default=None,
        help="目标 URL,缺省用 CONFIG['target_url']",
    )
    parser.add_argument(
        "--headless", "-H", action="store_true",
        help="无头模式启动 (默认有头;也可用 HEADLESS=1 env)",
    )
    parser.add_argument(
        "--proxy", default=None,
        help="业务代理 URL,如 http://user:pass@host:port (覆盖 CONFIG[proxy])",
    )
    parser.add_argument(
        "--incognito", dest="incognito", action="store_true", default=True,
        help="无痕模式: 每次用临时 profile,退出清理,不带历史(默认开启)",
    )
    parser.add_argument(
        "--no-incognito", dest="incognito", action="store_false",
        help="关闭无痕,使用 PROFILE_DIR 持久 profile(保留 cookies/历史)",
    )
    args = parser.parse_args()

    # ---- 应用 --proxy 覆盖 ----
    global PLAYWRIGHT_PROXY, REQUESTS_PROXIES
    if args.proxy is not None:
        CONFIG["proxy"] = args.proxy
        PLAYWRIGHT_PROXY, REQUESTS_PROXIES = parse_proxy_str(args.proxy)
        log_step(f"业务代理: {args.proxy or '(直连)'}")

    target_url = args.url or (CONFIG.get("target_url") or None)
    headless_env = os.environ.get("HEADLESS", "0").lower()
    headless = args.headless or (headless_env in ("1", "true", "yes", "on"))
    log_step(f"启动: mode={'headless' if headless else 'headed'} url={target_url or '(manual)'}")
    log(f"Mode: {'headless' if headless else 'headed'}; target_url={target_url or '(manual)'}")

    if headless and not target_url:
        log("ERROR: headless mode requires a URL argument. "
            "Pass it as the first argv, or unset HEADLESS to navigate manually.")
        sys.exit(2)

    # 无痕模式: 每次用 tempfile.mkdtemp() 创建临时 profile,退出时清理。
    # 关掉无痕(--no-incognito) 时用 PROFILE_DIR env 或默认持久目录。
    incognito_cleanup_dir: str | None = None
    if args.incognito:
        import tempfile
        profile_dir = tempfile.mkdtemp(prefix="paypal_auto_incognito_")
        incognito_cleanup_dir = profile_dir
        log_step(f"🕶️  无痕模式: 临时 profile = {profile_dir}")
    else:
        default_profile = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "profiles", "paypal_pay_default",
        )
        profile_dir = os.environ.get("PROFILE_DIR", default_profile)
        os.makedirs(profile_dir, exist_ok=True)
        log(f"持久 profile: {profile_dir}")

    # ---- 代理状态详细日志 + 出口 IP 探测 ----
    if PLAYWRIGHT_PROXY:
        masked_user = PLAYWRIGHT_PROXY.get("username", "") or ""
        if len(masked_user) > 8:
            masked_user = f"{masked_user[:4]}***{masked_user[-2:]}"
        log_step(
            f"代理配置: server={PLAYWRIGHT_PROXY['server']} "
            f"user={masked_user!r}"
        )
        # 探测代理:用业务相关域 (paypal.com) 而不是 ipinfo.io
        # 原因:动态代理(kookeey/luminati 等)常黑名单 ipinfo/icanhazip 等 IP 检测站,
        # 但业务域 (paypal.com/stripe.com) 一定可达 - 才是真实业务能跑的判据。
        log_step_begin("代理连通性探测 (paypal.com)")
        probe_url = os.environ.get("PROXY_PROBE_URL", "https://www.paypal.com/")
        try:
            probe = requests.head(
                probe_url,
                proxies=REQUESTS_PROXIES,
                timeout=15,
                headers={"User-Agent": "Mozilla/5.0"},
                allow_redirects=False,
            )
            log_step_end(
                "代理连通性探测 (paypal.com)",
                extra=f"HTTP {probe.status_code} via 代理 ✓",
            )
        except Exception as e:  # noqa: BLE001
            log_step_end(
                "代理连通性探测 (paypal.com)", ok=False,
                extra=f"{type(e).__name__}: {str(e)[:120]}",
            )
            log_step(
                "⚠️ 代理探测失败,但仍继续后续流程 (代理可能只是禁了探测目标,"
                "实际业务请求可能能通)。如果业务也失败,见下方 curl 命令排查:"
            )
            _proxy_url = CONFIG.get("proxy", "")
            log_step(f"  curl -x '{_proxy_url}' -v --max-time 15 {probe_url}")
    else:
        log_step("代理: 未配置(直连)")
        log_step(
            "⚠️ 直连模式访问 PayPal/Stripe 通常会因为 IP 非 US 失败"
        )

    with sync_playwright() as p:
        # ==== PayPal/DataDome 无头风控研究记录 ====
        # 有头不被风控 vs 无头被风控,关键差异点:
        #   1. window.outerWidth/outerHeight 在无头下为 0  ← 致命
        #   2. WebGL UNMASKED_RENDERER 在无头下是 "Google SwiftShader" (软件渲染) ← 致命
        #   3. screen.width/height 无头下与 viewport 不一致
        #   4. Notification.permission 无头是 "denied"
        #   5. navigator.connection.rtt/downlink 无头缺字段
        #   6. TLS/HTTP2 指纹(JA3/JA4)、Mouse 事件历史缺失
        # 解决路径(按效力排序):
        #   A. channel="chrome" 用本机真实 Chrome (指纹完全等同有头) ← 最稳
        #   B. --headless=new Chromium 109+ 新无头模式 (默认 stealth)
        #   C. init_script 覆盖 ~15 个 navigator/window/WebGL 指纹
        # 环境变量:
        #   PP_BROWSER_CHANNEL=chrome   用本机 Chrome (最推荐无头跑)
        #   PP_HEADLESS_NEW=1           用 --headless=new (默认开启)
        browser_channel = os.environ.get("PP_BROWSER_CHANNEL", "").strip() or None
        use_headless_new = os.environ.get("PP_HEADLESS_NEW", "1") != "0"

        launch_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-features=IsolateOrigins,site-per-process,AutomationControlled",
            "--disable-site-isolation-trials",
            "--no-default-browser-check",
            "--no-first-run",
            "--disable-infobars",
            "--disable-notifications",
            "--disable-popup-blocking",
            "--disable-extensions",
            "--lang=zh-CN,zh,en-US,en",
            "--window-size=1440,900",
        ]
        # --headless=new 仅在无头模式生效;真实 Chrome 109+ 已默认 new headless
        if headless and use_headless_new and not browser_channel:
            launch_args.append("--headless=new")
            log_step("🚀 启用 --headless=new (新无头模式,反检测能力大幅提升)")
        if browser_channel:
            log_step(f"🚀 使用本机真实浏览器: channel={browser_channel}")

        real_ua = os.environ.get(
            "USER_AGENT",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/143.0.0.0 Safari/537.36",
        )

        ctx_kwargs = dict(
            user_data_dir=profile_dir,
            headless=False if (headless and use_headless_new and not browser_channel) else headless,
            slow_mo=int(os.environ.get("SLOW_MO", "80")),
            viewport={"width": 1440, "height": 900},
            screen={"width": 1440, "height": 900},
            args=launch_args,
            proxy=PLAYWRIGHT_PROXY,
            user_agent=real_ua,
            locale="zh-CN",
            timezone_id="America/Los_Angeles",
            device_scale_factor=2,
            has_touch=False,
            is_mobile=False,
            color_scheme="light",
            reduced_motion="no-preference",
            permissions=[],
        )
        if browser_channel:
            ctx_kwargs["channel"] = browser_channel
        context: BrowserContext = p.chromium.launch_persistent_context(**ctx_kwargs)
        context.set_default_timeout(int(os.environ.get("TIMEOUT_MS", "60000")))
        log("Chromium launched (persistent_context)")

        # ============ Stealth-style 反检测 (针对 DataDome / PayPal 风控) ============
        # 仅注入开销小、跨页有效的指纹覆盖。无头模式必须;有头模式加上也无害。
        if os.environ.get("STEALTH", "1") != "0":
            context.add_init_script(
                """
                // 1) navigator.webdriver -> undefined
                Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                // 2) navigator.plugins 长度伪装
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [
                        {name: 'PDF Viewer', filename: 'internal-pdf-viewer'},
                        {name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer'},
                        {name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer'},
                        {name: 'Microsoft Edge PDF Viewer', filename: 'internal-pdf-viewer'},
                        {name: 'WebKit built-in PDF', filename: 'internal-pdf-viewer'},
                    ],
                });
                // 3) navigator.languages
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['zh-CN', 'zh', 'en-US', 'en'],
                });
                // 4) navigator.platform
                Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});
                // 5) navigator.hardwareConcurrency / deviceMemory
                Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
                Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
                // 6) navigator.maxTouchPoints (Mac 应为 0)
                Object.defineProperty(navigator, 'maxTouchPoints', {get: () => 0});
                // 7) chrome 对象 (真实 Chrome 必有)
                if (!window.chrome) {
                    window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}};
                }
                // 8) permissions.query 修复
                const origQuery = navigator.permissions && navigator.permissions.query;
                if (origQuery) {
                    navigator.permissions.query = (parameters) =>
                        parameters && parameters.name === 'notifications'
                            ? Promise.resolve({state: Notification.permission})
                            : origQuery.call(navigator.permissions, parameters);
                }
                // 9) WebGL1 vendor 伪装 (DataDome 必查 UNMASKED_RENDERER)
                try {
                    const getParameter = WebGLRenderingContext.prototype.getParameter;
                    WebGLRenderingContext.prototype.getParameter = function(p) {
                        if (p === 37445) return 'Intel Inc.';
                        if (p === 37446) return 'Intel(R) Iris(TM) Pro Graphics 6200';
                        if (p === 7937 /* VERSION */) return 'WebGL 1.0 (OpenGL ES 2.0 Chromium)';
                        if (p === 35724 /* SHADING_LANGUAGE_VERSION */)
                            return 'WebGL GLSL ES 1.0 (OpenGL ES GLSL ES 1.0 Chromium)';
                        return getParameter.call(this, p);
                    };
                } catch(e) {}
                // 10) WebGL2 同样伪装
                try {
                    const getParam2 = WebGL2RenderingContext.prototype.getParameter;
                    WebGL2RenderingContext.prototype.getParameter = function(p) {
                        if (p === 37445) return 'Intel Inc.';
                        if (p === 37446) return 'Intel(R) Iris(TM) Pro Graphics 6200';
                        return getParam2.call(this, p);
                    };
                } catch(e) {}
                // 11) 隐藏 cdc_ / __webdriver_* 等自动化痕迹
                for (const k of Object.keys(window)) {
                    if (/^cdc_|^__webdriver_|^_phantom|^callPhantom/.test(k)) {
                        try { delete window[k]; } catch(e) {}
                    }
                }
                // 12) 修复 window.outerWidth/outerHeight (无头默认 0,DataDome 必查)
                try {
                    Object.defineProperty(window, 'outerWidth', {get: () => 1440});
                    Object.defineProperty(window, 'outerHeight', {get: () => 900});
                } catch(e) {}
                // 13) 修复 screen 字段 (无头与 viewport 不一致)
                try {
                    Object.defineProperty(window.screen, 'width', {get: () => 1440});
                    Object.defineProperty(window.screen, 'height', {get: () => 900});
                    Object.defineProperty(window.screen, 'availWidth', {get: () => 1440});
                    Object.defineProperty(window.screen, 'availHeight', {get: () => 875});
                    Object.defineProperty(window.screen, 'colorDepth', {get: () => 30});
                    Object.defineProperty(window.screen, 'pixelDepth', {get: () => 30});
                } catch(e) {}
                // 14) 修复 navigator.connection (无头缺 downlink/rtt)
                try {
                    const conn = navigator.connection || {};
                    Object.defineProperty(navigator, 'connection', {
                        get: () => ({
                            effectiveType: '4g', rtt: 50, downlink: 10,
                            saveData: false, type: 'wifi',
                        }),
                    });
                } catch(e) {}
                // 15) 修复 Notification.permission (无头默认 "denied",真实 Chrome 是 "default")
                try {
                    if (window.Notification) {
                        Object.defineProperty(Notification, 'permission', {
                            get: () => 'default',
                        });
                    }
                } catch(e) {}
                // 16) document.hidden / visibilityState (有头永远 visible)
                try {
                    Object.defineProperty(document, 'hidden', {get: () => false});
                    Object.defineProperty(document, 'visibilityState', {get: () => 'visible'});
                } catch(e) {}
                // 17) chrome.runtime / chrome.app (真实 Chrome 必有)
                if (window.chrome && !window.chrome.runtime) {
                    window.chrome.runtime = {
                        OnInstalledReason: {INSTALL:'install',UPDATE:'update'},
                        OnRestartRequiredReason: {APP_UPDATE:'app_update'},
                        PlatformArch: {ARM:'arm',MIPS:'mips',X86_32:'x86-32',X86_64:'x86-64'},
                        connect: function(){}, sendMessage: function(){},
                    };
                }
                // 18) navigator.userAgentData (Client Hints - PayPal 也会查)
                try {
                    Object.defineProperty(navigator, 'userAgentData', {
                        get: () => ({
                            brands: [
                                {brand: 'Google Chrome', version: '143'},
                                {brand: 'Not?A_Brand', version: '8'},
                                {brand: 'Chromium', version: '143'},
                            ],
                            mobile: false,
                            platform: 'macOS',
                            getHighEntropyValues: () => Promise.resolve({
                                architecture: 'x86', bitness: '64',
                                model: '', platform: 'macOS',
                                platformVersion: '14.0.0', uaFullVersion: '143.0.0.0',
                            }),
                        }),
                    });
                } catch(e) {}
                """
            )
            log_step("🥷 反检测 stealth init_script 已注入 (18 项指纹覆盖)")
        else:
            log_step("⚠️ 反检测 stealth 已关闭 (STEALTH=0),DataDome 可能拦截")

        # ============ 资源拦截(默认开启,需 BLOCK_RESOURCES=0 关闭)============
        # 拦图片/字体/媒体 + 第三方追踪域,加速无头模式页面加载。
        # PayPal/DataDome/reCAPTCHA/Stripe/ChatGPT 白名单放行,完全不影响流程。
        # 如怀疑拦截导致页面问题,设 BLOCK_RESOURCES=0 关闭。
        if os.environ.get("BLOCK_RESOURCES", "1") != "0":
            _block_resource_types = {"image", "font", "media"}
            _block_hosts = (
                "doubleclick.net", "googletagmanager.com", "googletagservices.com",
                "google-analytics.com", "googleadservices.com",
                "googlesyndication.com", "adservice.google.",
                "facebook.net", "facebook.com/tr",
                "fullstory.com", "sentry.io", "newrelic.com",
                "mixpanel.com", "hotjar.com", "segment.io",
                "snowplowanalytics.com", "amplitude.com",
                "intercom.io", "drift.com", "zendesk.com",
            )
            _allow_hosts = (
                "paypal.com", "paypalobjects.com",
                "datadome.co", "datado.me", "ddbm2", "ct.captcha-delivery.com",
                "recaptcha.net", "google.com/recaptcha", "gstatic.com",
                "stripe.com", "stripe.network",
                "chatgpt.com", "openai.com", "oaistatic.com",
            )

            def _route_handler(route):
                req = route.request
                url = req.url
                if any(h in url for h in _allow_hosts):
                    return route.continue_()
                if req.resource_type in _block_resource_types:
                    return route.abort()
                if any(h in url for h in _block_hosts):
                    return route.abort()
                return route.continue_()

            context.route("**/*", _route_handler)
            log("Resource blocker enabled (BLOCK_RESOURCES=1)")
        else:
            log("Resource blocker DISABLED (default; set BLOCK_RESOURCES=1 to enable)")

        # ============ CSS 过渡禁用(默认关闭,需 DISABLE_TRANSITION=1 开启)============
        # 默认让页面正常渲染,避免影响 PayPal/Stripe 的样式状态切换逻辑。
        if os.environ.get("DISABLE_TRANSITION", "0") == "1":
            context.add_init_script(
                """
                (function() {
                    const css = `*, *::before, *::after {
                        transition-duration: 0.001s !important;
                        transition-delay: 0s !important;
                        scroll-behavior: auto !important;
                    }`;
                    function inject() {
                        if (!document.head) return;
                        const st = document.createElement('style');
                        st.textContent = css;
                        document.head.appendChild(st);
                    }
                    if (document.head) inject();
                    else document.addEventListener('DOMContentLoaded', inject);
                })();
                """
            )
            log("CSS transition disabled (DISABLE_TRANSITION=1)")
        else:
            log("CSS transition INTACT (default; set DISABLE_TRANSITION=1 to disable)")

        # ============ CAPTCHA overlay 自动删除(默认开启)============
        # 监听 CAPTCHA 覆盖层及 authchallenge_response 容器出现并 remove,
        # 防止 PayPal 弹出 captcha 阻断流程。
        # 如怀疑影响 PayPal 风控可设 REMOVE_CAPTCHA=0 关闭。
        if os.environ.get("REMOVE_CAPTCHA", "1") == "0":
            log("CAPTCHA overlay auto-removal DISABLED "
                "(set REMOVE_CAPTCHA=1 or unset to enable)")
        else:
            context.add_init_script(
                """
                (function() {
                    const EXACT_REMOVE_SELECTORS = [
                        'div[data-app="authchallenge_response"]',
                    ];
                    const CONTAINER_REMOVE_SELECTORS = [
                        '#captcha-standalone',
                        '#captchaHeading',
                        '.captcha-overlay',
                        '.captcha-container',
                    ];
                    function nuke() {
                        let removed = 0;
                        for (const sel of EXACT_REMOVE_SELECTORS) {
                            document.querySelectorAll(sel).forEach((el) => {
                                try { el.remove(); removed++; } catch(e) {}
                            });
                        }
                        for (const sel of CONTAINER_REMOVE_SELECTORS) {
                            document.querySelectorAll(sel).forEach((el) => {
                                let target = el;
                                const corral = el.closest('.corral, .contentContainerXhr');
                                if (corral) target = corral;
                                try { target.remove(); removed++; } catch(e) {}
                            });
                        }
                        return removed;
                    }
                    nuke();
                    function start() {
                        if (!document.body) return;
                        const obs = new MutationObserver(() => { nuke(); });
                        obs.observe(document.body, { childList: true, subtree: true });
                    }
                    if (document.body) start();
                    else document.addEventListener('DOMContentLoaded', start);
                })();
                """
            )
            log("CAPTCHA overlay auto-removal injected (REMOVE_CAPTCHA=1)")

        attach_handlers(context)

        # 取已有 page，没有就 new_page（与 BrowserSession.current_page 一致）
        page = context.pages[0] if context.pages else context.new_page()

        global _TARGET_URL
        _TARGET_URL = target_url or ""
        if target_url:
            log(f"Navigating to: {target_url}")
            try:
                page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
            except Exception as e:  # noqa: BLE001
                log(f"goto failed: {e}")
        else:
            try:
                page.goto("about:blank")
            except Exception:  # noqa: BLE001
                pass
            log("Browser ready. Navigate manually; events log here.")

        # 主循环
        done_after = float(os.environ.get("DONE_AFTER_SECONDS", "300"))
        # 成功后再等 N 秒退出（让用户/PayPal 看清最终页，0 = 立刻退出）
        # 成功后立即退出(默认 0,不再等浏览器加载 chatgpt.com)
        success_grace = float(os.environ.get("SUCCESS_GRACE_SECONDS", "0"))
        start = time.time()
        last_fullpage_check = 0.0
        global _SUCCESS_FLAG, _REOPEN_FLAG, _REOPEN_COUNT
        try:
            while True:
                if not context.pages:
                    break
                # ==== 主动终止:金额非 0 / 业务校验失败 ====
                if _ABORT_FLAG:
                    log(f"Abort detected: {_ABORT_REASON}")
                    log("Exiting on abort.")
                    break
                # ==== 支付成功 / 终态页:跳到 chatgpt.com 或 Stripe 终态后退出 ====
                if _SUCCESS_FLAG:
                    log_step(f"🎉 支付成功！无须支付！{success_grace}s 后退出")
                    try:
                        context.pages[0].wait_for_timeout(int(success_grace * 1000))
                    except Exception:  # noqa: BLE001
                        pass
                    log_step("流程结束 (exit 0)")
                    break
                # ==== checkoutweb 表单未渲染 → 重新打开支付链接 ====
                if _REOPEN_FLAG and not _HANDLER_BUSY:
                    _REOPEN_FLAG = False
                    _REOPEN_COUNT += 1
                    log_step(
                        f"重开支付链接 ({_REOPEN_COUNT}/{_REOPEN_MAX}) "
                        f"→ {(_TARGET_URL or '')[:80]}"
                    )
                    try:
                        PROCESSED_URLS.clear()
                        cur_page = context.pages[0]
                        cur_page.goto(_TARGET_URL, wait_until="domcontentloaded",
                                      timeout=45000)
                    except Exception as e:  # noqa: BLE001
                        log(f"reopen goto failed: {e}")
                # ==== 周期性 Stripe 终态页探测(每 1s 一次) ====
                # 不依赖 nav 事件,不与 handler 冲突(只读 DOM),不限 host;
                # 命中后由 _mark_paid_no_action_needed 统一打印"支付成功 无须支付"
                now = time.time()
                if now - last_fullpage_check >= 1.0:
                    last_fullpage_check = now
                    for p in list(context.pages):
                        try:
                            if p.is_closed():
                                continue
                        except Exception:  # noqa: BLE001
                            continue
                        detected = _detect_stripe_end_state(p)
                        if detected:
                            try:
                                cur_url = p.url
                            except Exception:  # noqa: BLE001
                                cur_url = None
                            _mark_paid_no_action_needed(detected, cur_url)
                            break
                    if _SUCCESS_FLAG:
                        continue
                if headless and (time.time() - start) > done_after:
                    log(f"Reached DONE_AFTER_SECONDS={done_after}; exiting.")
                    break
                try:
                    context.pages[0].wait_for_timeout(500)
                except Exception:  # noqa: BLE001
                    break
        except KeyboardInterrupt:
            log("Interrupted by user.")
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass

    # ---- 清理无痕临时 profile ----
    if incognito_cleanup_dir:
        import shutil
        try:
            shutil.rmtree(incognito_cleanup_dir, ignore_errors=True)
            log(f"🕶️  无痕 profile 已清理: {incognito_cleanup_dir}")
        except Exception as e:  # noqa: BLE001
            log(f"无痕 profile 清理失败 (可忽略): {e}")

    # 退出码:成功 0,主动 abort(金额非 0 等) 2,其他原因 1
    if _SUCCESS_FLAG:
        sys.exit(0)
    if _ABORT_FLAG:
        sys.exit(2)
    sys.exit(1)


if __name__ == "__main__":
    main()
