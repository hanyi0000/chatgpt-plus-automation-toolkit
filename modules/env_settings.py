from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import unicodedata

from modules.terminal_theme import CYAN, GREEN, MAGENTA, RED, RESET, YELLOW, paint
from modules.utils import resolve_path


@dataclass(frozen=True)
class SettingItem:
    key: str
    label: str
    group: str
    kind: str = "text"
    choices: tuple[str, ...] = ()
    masked: bool = False
    help_text: str = ""
    section: str = ""


SETTINGS: list[SettingItem] = [
    SettingItem("MAIL_SOURCE", "默认邮箱源", "基础设置", "choice", ("icloud_query", "moemail", "hotmail", "qq"), help_text="未配置流程专属邮箱源时使用。"),
    SettingItem("FLOW1_MAIL_SOURCE", "流程一邮箱源", "基础设置", "choice", ("icloud_query", "moemail", "hotmail", "qq"), help_text="流程一注册长链接专用邮箱源；qq 使用 IMAP 授权码。"),
    SettingItem("FLOW3_MAIL_SOURCE", "流程三邮箱源", "基础设置", "choice", ("icloud_query", "moemail", "hotmail", "qq"), help_text="流程三授权专用邮箱源；qq 使用 IMAP 授权码。"),
    SettingItem("FREE_MAIL_SOURCE", "Free 邮箱源", "基础设置", "choice", ("moemail", "hotmail", "icloud_query"), help_text="Free 注册专用邮箱源；当前建议使用 moemail。"),
    SettingItem("USE_PROXY", "浏览器代理", "基础设置", "bool", help_text="开启后浏览器会从代理池取代理。"),
    SettingItem("PROXY_FILE", "代理池文件", "基础设置"),
    SettingItem("MOEMAIL_BASE_URL", "MoeMail 地址", "邮箱设置"),
    SettingItem("MOEMAIL_API_KEY", "MoeMail API Key", "邮箱设置", masked=True),
    SettingItem("MOEMAIL_DOMAIN_WHITELIST", "MoeMail 域名", "邮箱设置"),
    SettingItem("MOEMAIL_DOMAIN_MODE", "MoeMail 域名模式", "邮箱设置", "choice", ("random", "fixed", "rotate")),
    SettingItem("MOEMAIL_FIXED_DOMAIN", "MoeMail 固定域名", "邮箱设置"),
    SettingItem("FREE_MOEMAIL_DOMAIN_MODE", "Free 域名模式", "邮箱设置", "choice", ("random", "fixed", "rotate")),
    SettingItem("FREE_MOEMAIL_FIXED_DOMAIN", "Free 固定域名", "邮箱设置"),
    SettingItem("MOEMAIL_ENABLED", "MoeMail 自动补号", "邮箱设置", "bool"),
    SettingItem("MAIL_ACCOUNT_MODE", "邮箱账号模式", "邮箱设置", "choice", ("api", "pool")),
    SettingItem("MOEMAIL_CREATE_PREFIX", "MoeMail 前缀", "邮箱设置"),
    SettingItem("MOEMAIL_CREATE_MODE", "MoeMail 创建模式", "邮箱设置", "choice", ("human", "random")),
    SettingItem("GOPAY_PHONE_1", "GoPay 手机号 1", "流程二支付", help_text="worker-1 固定使用的 GoPay/WhatsApp 手机号。", section="GoPay 基础"),
    SettingItem("GOPAY_PHONE_2", "GoPay 手机号 2", "流程二支付", help_text="worker-2 固定使用的 GoPay/WhatsApp 手机号。", section="GoPay 基础"),
    SettingItem("GOPAY_PHONE_3", "GoPay 手机号 3", "流程二支付", help_text="worker-3 固定使用的 GoPay/WhatsApp 手机号。", section="GoPay 基础"),
    SettingItem("GOPAY_PHONES", "GoPay 手机号池(兼容旧配置)", "流程二支付", help_text="旧写法仍可用，会按顺序映射到 worker-1/2/3；新配置优先用上面的固定手机号。", section="GoPay 基础"),
    SettingItem("GOPAY_COUNTRY_CODE", "GoPay 国家区号", "流程二支付", section="GoPay 基础"),
    SettingItem("GOPAY_PIN", "GoPay PIN", "流程二支付", masked=True, section="GoPay 基础"),
    SettingItem("WHATSAPP_OTP_AUTO", "WhatsApp 自动取码", "流程二支付", "bool", section="WhatsApp 取码"),
    SettingItem("WHATSAPP_ADB_PATH", "ADB 路径", "流程二支付", help_text="项目内默认路径：tools\\adb\\adb.exe。", section="WhatsApp 取码"),
    SettingItem("WHATSAPP_ENABLED_1", "WhatsApp 设备 1 开关", "流程二支付", "bool", help_text="单独关闭某个 worker 的 WhatsApp 取码；空值则跟随总开关。", section="WhatsApp 取码"),
    SettingItem("WHATSAPP_ADB_DEVICE_1", "WhatsApp 设备 1", "流程二支付", section="WhatsApp 取码"),
    SettingItem("WHATSAPP_PACKAGE_1", "WhatsApp 包名 1", "流程二支付", section="WhatsApp 取码"),
    SettingItem("WHATSAPP_ENABLED_2", "WhatsApp 设备 2 开关", "流程二支付", "bool", help_text="单独关闭某个 worker 的 WhatsApp 取码；空值则跟随总开关。", section="WhatsApp 取码"),
    SettingItem("WHATSAPP_ADB_DEVICE_2", "WhatsApp 设备 2", "流程二支付", section="WhatsApp 取码"),
    SettingItem("WHATSAPP_PACKAGE_2", "WhatsApp 包名 2", "流程二支付", section="WhatsApp 取码"),
    SettingItem("WHATSAPP_ENABLED_3", "WhatsApp 设备 3 开关", "流程二支付", "bool", help_text="单独关闭某个 worker 的 WhatsApp 取码；空值则跟随总开关。", section="WhatsApp 取码"),
    SettingItem("WHATSAPP_ADB_DEVICE_3", "WhatsApp 设备 3", "流程二支付", section="WhatsApp 取码"),
    SettingItem("WHATSAPP_PACKAGE_3", "WhatsApp 包名 3", "流程二支付", section="WhatsApp 取码"),
    SettingItem("WHATSAPP_USE_BRIDGE", "通知桥取码", "流程二支付", "bool", help_text="优先读取项目内 OTP Bridge 监听到的 WhatsApp 通知，速度最快。", section="WhatsApp 取码"),
    SettingItem("WHATSAPP_USE_NOTIFICATIONS", "读取通知", "流程二支付", "bool", section="WhatsApp 取码"),
    SettingItem("WHATSAPP_USE_UI_TEXT", "读取屏幕文本", "流程二支付", "bool", section="WhatsApp 取码"),
    SettingItem("WHATSAPP_USE_OCR", "OCR 取码", "流程二支付", "bool", help_text="截图后用 OCR 识别屏幕文字，通知/界面文本读不到时再开。", section="WhatsApp 取码"),
    SettingItem("WHATSAPP_CODE_INTERVAL", "取码间隔秒数", "流程二支付", "int", help_text="Bridge-only 模式建议 2 秒。", section="性能优化"),
    SettingItem("FLOW2_BILLING_RETRIES", "账单重试次数", "流程二支付", "int", section="性能优化"),
    SettingItem("FLOW2_OTP_TIMEOUT", "OTP 最大等待秒数", "流程二支付", "int", section="性能优化"),
    SettingItem("FLOW2_RETRY_INTERVAL", "异常重试间隔", "流程二支付", "int", section="性能优化"),
    SettingItem("FLOW2_RETRY_TIMEOUT", "异常重试上限", "流程二支付", "int", section="性能优化"),
    SettingItem("FLOW2_MANUAL_SUCCESS_TIMEOUT", "支付成功等待上限", "流程二支付", "int", section="性能优化"),
    SettingItem("FLOW2_SAVE_SUCCESS_SCREENSHOTS", "保留成功截图", "流程二支付", "bool", help_text="关闭后只保留失败/异常截图，降低浏览器和磁盘开销。", section="性能优化"),
    SettingItem("FLOW2_SAVE_HTML", "保留 HTML 现场", "流程二支付", "bool", help_text="关闭后成功路径不写 HTML；失败/异常仍会按规则保留。", section="性能优化"),
    SettingItem("GOPAY_UNLINK_AFTER_SUCCESS", "支付后解绑", "流程二支付", "bool", section="支付后解绑"),
    SettingItem("GOPAY_UNLINK_FAST_TAPS", "解绑快速坐标", "流程二支付", "bool", help_text="720x1280/240dpi 模拟器优先固定坐标操作，失败再回退文字识别。", section="支付后解绑"),
    SettingItem("GOPAY_UNLINK_ENABLED_1", "GoPay 设备 1 开关", "流程二支付", "bool", help_text="单独关闭某个 worker 的支付后解绑；空值则跟随总开关。", section="支付后解绑"),
    SettingItem("GOPAY_ADB_DEVICE_1", "GoPay 设备 1", "流程二支付", section="支付后解绑"),
    SettingItem("GOPAY_UNLINK_ENABLED_2", "GoPay 设备 2 开关", "流程二支付", "bool", help_text="单独关闭某个 worker 的支付后解绑；空值则跟随总开关。", section="支付后解绑"),
    SettingItem("GOPAY_ADB_DEVICE_2", "GoPay 设备 2", "流程二支付", section="支付后解绑"),
    SettingItem("GOPAY_UNLINK_ENABLED_3", "GoPay 设备 3 开关", "流程二支付", "bool", help_text="单独关闭某个 worker 的支付后解绑；空值则跟随总开关。", section="支付后解绑"),
    SettingItem("GOPAY_ADB_DEVICE_3", "GoPay 设备 3", "流程二支付", section="支付后解绑"),
    SettingItem("AUTH_SERVER_UPLOAD", "授权后上传", "流程三授权", "bool", section="授权后上传"),
    SettingItem("AUTH_SERVER_URL", "账号库地址", "流程三授权", section="授权后上传"),
    SettingItem("AUTH_SERVER_API_KEY", "账号库 API Key", "流程三授权", masked=True, section="授权后上传"),
    SettingItem("SESSION_EXPORT_SERVER_UPLOAD", "流程四上传", "流程四导出", "bool", help_text="开启后流程四本地导出成功时，同步上传到 AUTH_SERVER_URL。", section="Session 导出"),
    SettingItem("SMS_ENABLED", "默认接码开关", "流程接码", "bool", help_text="未配置流程专属接码开关时使用。", section="默认接码"),
    SettingItem("SMS_PROVIDER", "默认接码平台", "流程接码", "choice", ("herosms", "grizzly", "fivesim"), help_text="未配置流程专属接码平台时使用。", section="默认接码"),
    SettingItem("FLOW1_SMS_ENABLED", "流程一接码", "流程接码", "bool", help_text="流程一遇到手机号页时是否启用接码。", section="流程一 注册长链接"),
    SettingItem("FLOW1_SMS_PROVIDER", "流程一接码平台", "流程接码", "choice", ("herosms", "grizzly", "fivesim"), section="流程一 注册长链接"),
    SettingItem("FLOW3_SMS_ENABLED", "流程三接码", "流程接码", "bool", help_text="流程三授权遇到手机号页时是否启用接码。", section="流程三 OAuth授权"),
    SettingItem("FLOW3_SMS_PROVIDER", "流程三接码平台", "流程接码", "choice", ("herosms", "grizzly", "fivesim"), section="流程三 OAuth授权"),
    SettingItem("FREE_SMS_ENABLED", "Free 接码", "流程接码", "bool", help_text="Free 注册必须开启，否则不会继续手机号注册。", section="功能五 Free注册"),
    SettingItem("FREE_SMS_PROVIDER", "Free 接码平台", "流程接码", "choice", ("herosms", "grizzly", "fivesim"), section="功能五 Free注册"),
    SettingItem("HERO_SMS_API_KEY", "HeroSMS API Key", "流程一/三接码", masked=True, section="手机号接码"),
    SettingItem("HERO_SMS_SERVICE", "HeroSMS 服务", "流程一/三接码", section="手机号接码"),
    SettingItem("HERO_SMS_COUNTRY_TOP_N", "国家列表数量", "流程一/三接码", "int", section="手机号接码"),
    SettingItem("HERO_SMS_OPERATOR_THRESHOLD", "运营商选择阈值", "流程一/三接码", "int", section="手机号接码"),
    SettingItem("HERO_SMS_PROMPT_OPERATOR_SELECTION", "HeroSMS 选择运营商", "流程一/三接码", "bool", help_text="开启后国家选定后会列出运营商；直接回车使用任何运营商。", section="手机号接码"),
    SettingItem("HERO_SMS_POLL_INTERVAL", "短信轮询秒数", "流程一/三接码", "int", section="手机号接码"),
    SettingItem("HERO_SMS_MAX_ATTEMPTS", "短信轮询次数", "流程一/三接码", "int", section="手机号接码"),
    SettingItem("HERO_SMS_COUNTRY_SELECT", "固定国家", "流程一/三接码", section="手机号接码"),
    SettingItem("HERO_SMS_PROMPT_COUNTRY_SELECTION", "运行时选择国家", "流程一/三接码", "bool", section="手机号接码"),
    SettingItem("GRIZZLY_API_KEY", "Grizzly API Key", "流程一/三接码", masked=True, section="手机号接码"),
    SettingItem("GRIZZLY_SERVICE", "Grizzly 服务", "流程一/三接码", help_text="建议填 auto 自动识别 OpenAI/ChatGPT 服务；识别失败时会回退 dr。", section="手机号接码"),
    SettingItem("GRIZZLY_COUNTRY_TOP_N", "Grizzly 国家数量", "流程一/三接码", "int", section="手机号接码"),
    SettingItem("GRIZZLY_PROVIDER_THRESHOLD", "Grizzly 服务商阈值", "流程一/三接码", "int", section="手机号接码"),
    SettingItem("GRIZZLY_PROMPT_PROVIDER_SELECTION", "Grizzly 选择服务商", "流程一/三接码", "bool", help_text="开启后国家选定后会列出服务商；直接回车使用任何服务商。", section="手机号接码"),
    SettingItem("GRIZZLY_POLL_INTERVAL", "Grizzly 轮询秒数", "流程一/三接码", "int", section="手机号接码"),
    SettingItem("GRIZZLY_MAX_ATTEMPTS", "Grizzly 轮询次数", "流程一/三接码", "int", section="手机号接码"),
    SettingItem("GRIZZLY_COUNTRY_SELECT", "Grizzly 固定国家", "流程一/三接码", section="手机号接码"),
    SettingItem("GRIZZLY_PROMPT_COUNTRY_SELECTION", "Grizzly 运行时选国家", "流程一/三接码", "bool", section="手机号接码"),
    SettingItem("FIVESIM_API_KEY", "5sim API Key", "流程一/三/Free 接码", masked=True, help_text="在 https://5sim.net/settings/security 创建 Bearer JWT。", section="手机号接码"),
    SettingItem("FIVESIM_SERVICE", "5sim 产品", "流程一/三/Free 接码", help_text="OpenAI 注册用 openai。", section="手机号接码"),
    SettingItem("FIVESIM_COUNTRY_TOP_N", "5sim 国家数量", "流程一/三/Free 接码", "int", section="手机号接码"),
    SettingItem("FIVESIM_OPERATOR_THRESHOLD", "5sim 运营商阈值", "流程一/三/Free 接码", "int", section="手机号接码"),
    SettingItem("FIVESIM_PROMPT_OPERATOR_SELECTION", "5sim 选择运营商", "流程一/三/Free 接码", "bool", help_text="5sim 的 operator 通常用 any 即可，这里留空或关闭。", section="手机号接码"),
    SettingItem("FIVESIM_POLL_INTERVAL", "5sim 轮询秒数", "流程一/三/Free 接码", "int", section="手机号接码"),
    SettingItem("FIVESIM_MAX_ATTEMPTS", "5sim 轮询次数", "流程一/三/Free 接码", "int", section="手机号接码"),
    SettingItem("FIVESIM_COUNTRY_SELECT", "5sim 固定国家", "流程一/三/Free 接码", help_text="填 ISO 代码（ID / VN / PH…）或 slug（indonesia）。", section="手机号接码"),
    SettingItem("FIVESIM_PROMPT_COUNTRY_SELECTION", "5sim 运行时选国家", "流程一/三/Free 接码", "bool", section="手机号接码"),
    SettingItem("PAYPAL_ICLOUD_FILE", "iCloud 邮箱池文件", "PayPal Plus", help_text="PayPal 流程1 的 iCloud 邮箱输入文件。", section="PayPal Plus"),
    SettingItem("PAYPAL_CARDS_FILE", "虚拟卡池文件", "PayPal Plus", help_text="PayPal 流程2 的虚拟卡输入文件。", section="PayPal Plus"),
    SettingItem("PAYPAL_PHONES_FILE", "手机号池文件", "PayPal Plus", help_text="PayPal 流程2 的手机号输入文件。", section="PayPal Plus"),
    SettingItem("PAYPAL_PHONE_MAX_USES", "手机号最大使用次数", "PayPal Plus", "int", help_text="每个手机号最多用几次（默认5）。", section="PayPal Plus"),
    SettingItem("PAYPAL_PHONE_RETRY_ON_REJECT", "手机号被拒重试次数", "PayPal Plus", "int", help_text="PayPal 拒绝手机号后换号重试几次（默认3）。", section="PayPal Plus"),
    SettingItem("PAYPAL_BILLING_COUNTRY", "账单国家", "PayPal Plus", help_text="生成长链接时的账单国家（默认 US）。", section="PayPal Plus"),
    SettingItem("PAYPAL_REGISTER_USE_PROXY", "注册阶段代理", "PayPal Plus", "bool", help_text="流程1注册和 Session 补录默认直连；仅明确需要时开启。", section="代理"),
    SettingItem("PAYPAL_REGISTER_PROXY_FILE", "注册代理池文件", "PayPal Plus", help_text="仅在注册阶段代理开启时使用。", section="代理"),
    SettingItem("PAYPAL_USE_PROXY", "支付阶段代理", "PayPal Plus", "bool", help_text="仅控制流程2 PayPal 支付，不会让流程1注册走代理。", section="代理"),
    SettingItem("PAYPAL_PROXY_FILE", "支付代理池文件", "PayPal Plus", help_text="仅在支付阶段代理开启时使用。", section="代理"),
]

SETTINGS_BY_KEY = {item.key: item for item in SETTINGS}

FLOW2_PRESETS: list[tuple[str, str, dict[str, str]]] = [
    (
        "日常高性能",
        "Bridge-only 取码、少截图、少 HTML，适合稳定后的日常三线程。",
        {
            "WHATSAPP_OTP_AUTO": "true",
            "WHATSAPP_USE_BRIDGE": "true",
            "WHATSAPP_USE_NOTIFICATIONS": "false",
            "WHATSAPP_USE_UI_TEXT": "false",
            "WHATSAPP_USE_OCR": "false",
            "WHATSAPP_CODE_INTERVAL": "2",
            "FLOW2_SAVE_SUCCESS_SCREENSHOTS": "false",
            "FLOW2_SAVE_HTML": "false",
            "GOPAY_UNLINK_FAST_TAPS": "true",
        },
    ),
    (
        "稳定保守",
        "保留通知兜底，仍不启用 OCR；适合 Bridge 偶发漏码时。",
        {
            "WHATSAPP_OTP_AUTO": "true",
            "WHATSAPP_USE_BRIDGE": "true",
            "WHATSAPP_USE_NOTIFICATIONS": "true",
            "WHATSAPP_USE_UI_TEXT": "false",
            "WHATSAPP_USE_OCR": "false",
            "WHATSAPP_CODE_INTERVAL": "2",
            "FLOW2_SAVE_SUCCESS_SCREENSHOTS": "false",
            "FLOW2_SAVE_HTML": "false",
            "GOPAY_UNLINK_FAST_TAPS": "true",
        },
    ),
    (
        "调试排查",
        "保留更多现场材料，适合排查新问题；性能开销会更高。",
        {
            "WHATSAPP_OTP_AUTO": "true",
            "WHATSAPP_USE_BRIDGE": "true",
            "WHATSAPP_USE_NOTIFICATIONS": "true",
            "WHATSAPP_USE_UI_TEXT": "true",
            "WHATSAPP_USE_OCR": "false",
            "WHATSAPP_CODE_INTERVAL": "3",
            "FLOW2_SAVE_SUCCESS_SCREENSHOTS": "true",
            "FLOW2_SAVE_HTML": "true",
            "GOPAY_UNLINK_FAST_TAPS": "false",
        },
    ),
]

WIZARD_SECTIONS: list[tuple[str, str, tuple[str, ...]]] = [
    (
        "日常开关",
        "最常改的开关：邮箱源 / 接码 / 代理 / 解绑。",
        (
            "FLOW1_MAIL_SOURCE",
            "FLOW1_SMS_ENABLED",
            "FLOW3_MAIL_SOURCE",
            "FLOW3_SMS_ENABLED",
            "FREE_MAIL_SOURCE",
            "FREE_SMS_ENABLED",
            "USE_PROXY",
            "GOPAY_UNLINK_AFTER_SUCCESS",
        ),
    ),
    (
        "流程1 注册长链接",
        "流程一邮箱源与接码平台。",
        (
            "FLOW1_MAIL_SOURCE",
            "FLOW1_SMS_ENABLED",
            "FLOW1_SMS_PROVIDER",
        ),
    ),
    (
        "流程2 GoPay 支付",
        "取码策略、截图/HTML、超时重试、预设。",
        (
            "WHATSAPP_OTP_AUTO",
            "WHATSAPP_USE_BRIDGE",
            "WHATSAPP_USE_NOTIFICATIONS",
            "WHATSAPP_USE_UI_TEXT",
            "WHATSAPP_USE_OCR",
            "WHATSAPP_CODE_INTERVAL",
            "FLOW2_SAVE_SUCCESS_SCREENSHOTS",
            "FLOW2_SAVE_HTML",
            "FLOW2_BILLING_RETRIES",
            "FLOW2_OTP_TIMEOUT",
            "FLOW2_RETRY_INTERVAL",
            "FLOW2_RETRY_TIMEOUT",
            "FLOW2_MANUAL_SUCCESS_TIMEOUT",
        ),
    ),
    (
        "流程2 设备绑定",
        "worker-1/2/3 的手机号、WhatsApp、GoPay 设备。",
        (
            "GOPAY_PHONE_1",
            "GOPAY_PHONE_2",
            "GOPAY_PHONE_3",
            "GOPAY_PHONES",
            "GOPAY_COUNTRY_CODE",
            "GOPAY_PIN",
            "WHATSAPP_ADB_PATH",
            "WHATSAPP_ENABLED_1",
            "WHATSAPP_ADB_DEVICE_1",
            "WHATSAPP_PACKAGE_1",
            "WHATSAPP_ENABLED_2",
            "WHATSAPP_ADB_DEVICE_2",
            "WHATSAPP_PACKAGE_2",
            "WHATSAPP_ENABLED_3",
            "WHATSAPP_ADB_DEVICE_3",
            "WHATSAPP_PACKAGE_3",
            "GOPAY_UNLINK_AFTER_SUCCESS",
            "GOPAY_UNLINK_FAST_TAPS",
            "GOPAY_UNLINK_ENABLED_1",
            "GOPAY_ADB_DEVICE_1",
            "GOPAY_UNLINK_ENABLED_2",
            "GOPAY_ADB_DEVICE_2",
            "GOPAY_UNLINK_ENABLED_3",
            "GOPAY_ADB_DEVICE_3",
        ),
    ),
    (
        "流程3 OAuth 授权",
        "流程三邮箱源、接码、授权后上传。",
        (
            "FLOW3_MAIL_SOURCE",
            "FLOW3_SMS_ENABLED",
            "FLOW3_SMS_PROVIDER",
            "AUTH_SERVER_UPLOAD",
            "AUTH_SERVER_URL",
            "AUTH_SERVER_API_KEY",
            "SESSION_EXPORT_SERVER_UPLOAD",
        ),
    ),
    (
        "流程5 Free 注册",
        "Free 邮箱源、接码、域名策略。",
        (
            "FREE_MAIL_SOURCE",
            "FREE_SMS_ENABLED",
            "FREE_SMS_PROVIDER",
            "FREE_MOEMAIL_DOMAIN_MODE",
            "FREE_MOEMAIL_FIXED_DOMAIN",
        ),
    ),
    (
        "PayPal Plus",
        "注册阶段默认直连，支付阶段代理独立控制。",
        (
            "PAYPAL_ICLOUD_FILE",
            "PAYPAL_CARDS_FILE",
            "PAYPAL_PHONES_FILE",
            "PAYPAL_BILLING_COUNTRY",
            "PAYPAL_REGISTER_USE_PROXY",
            "PAYPAL_REGISTER_PROXY_FILE",
            "PAYPAL_USE_PROXY",
            "PAYPAL_PROXY_FILE",
        ),
    ),
    (
        "邮箱池 & 代理",
        "默认邮箱源、MoeMail、代理池、接码平台凭据。",
        (
            "MAIL_SOURCE",
            "MAIL_ACCOUNT_MODE",
            "MOEMAIL_ENABLED",
            "MOEMAIL_BASE_URL",
            "MOEMAIL_API_KEY",
            "MOEMAIL_DOMAIN_WHITELIST",
            "MOEMAIL_DOMAIN_MODE",
            "MOEMAIL_FIXED_DOMAIN",
            "MOEMAIL_CREATE_PREFIX",
            "MOEMAIL_CREATE_MODE",
            "USE_PROXY",
            "PROXY_FILE",
            "SMS_ENABLED",
            "SMS_PROVIDER",
            "HERO_SMS_API_KEY",
            "HERO_SMS_SERVICE",
            "HERO_SMS_COUNTRY_SELECT",
            "GRIZZLY_API_KEY",
            "GRIZZLY_SERVICE",
            "GRIZZLY_COUNTRY_SELECT",
            "FIVESIM_API_KEY",
            "FIVESIM_SERVICE",
            "FIVESIM_COUNTRY_SELECT",
        ),
    ),
]

CONFIG_CENTER_MENU: list[tuple[str, str]] = [
    ("Dashboard 首页", "展示关键状态"),
    ("按场景配置", "大多数情况进这里"),
    ("全量查找/修改", f"{len(SETTINGS)} 项全开，搜索 key"),
    ("套用流程2预设", "日常 / 稳定 / 调试"),
    ("体检", "只读诊断"),
    ("恢复备份", "查看/回滚历史 .env"),
]


def read_env_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


def parse_env(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def snapshot_known_values(values: dict[str, str]) -> dict[str, str]:
    return {item.key: values.get(item.key, "") for item in SETTINGS}


def changed_values(before: dict[str, str], after: dict[str, str]) -> list[tuple[SettingItem, str, str]]:
    changes: list[tuple[SettingItem, str, str]] = []
    for item in SETTINGS:
        old = before.get(item.key, "")
        new = after.get(item.key, "")
        if old != new:
            changes.append((item, old, new))
    return changes


def write_env_values(path: Path, updates: dict[str, str]) -> None:
    lines = read_env_lines(path)
    seen: set[str] = set()
    output: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, _value = stripped.split("=", 1)
            key = key.strip()
            if key in updates:
                output.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        output.append(raw)
    missing = [item for item in SETTINGS if item.key in updates and item.key not in seen]
    if missing:
        if output and output[-1].strip():
            output.append("")
        output.append("# 通过内置设置面板新增")
        for item in missing:
            output.append(f"{item.key}={updates[item.key]}")
    path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")


def backup_env(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup_dir = path.parent / "output" / "env_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f".env.backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    shutil.copy2(path, backup_path)
    return backup_path


def mask_value(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}***{value[-4:]}"


def display_value(item: SettingItem, value: str) -> str:
    if item.masked:
        return mask_value(value)
    if item.kind == "bool":
        return "开启" if is_true(value) else "关闭"
    return value


def raw_value_for_help(item: SettingItem, value: str) -> str:
    if item.masked:
        return mask_value(value)
    return value


def display_diff_value(item: SettingItem, value: str) -> str:
    if item.masked:
        return mask_value(value)
    if value == "":
        return "(空)"
    return value


def display_width(value: object) -> int:
    width = 0
    for char in str(value):
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return width


def pad_display(value: object, width: int, align: str = "left") -> str:
    text = str(value)
    padding = max(0, width - display_width(text))
    if align == "right":
        return " " * padding + text
    return text + " " * padding


def colored_cell(value: object, color: str, width: int, *, bold: bool = False) -> str:
    return paint(pad_display(value, width), color, bold=bold)


def is_true(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "y", "是", "启用"}


def cycle_value(item: SettingItem, value: str, step: int) -> str:
    if item.kind == "bool":
        return "false" if is_true(value) else "true"
    if item.choices:
        choices = list(item.choices)
        current = value if value in choices else choices[0]
        index = choices.index(current)
        return choices[(index + step) % len(choices)]
    if item.kind == "int":
        try:
            number = int(value or "0")
        except ValueError:
            number = 0
        return str(max(0, number + step))
    return value


def get_key() -> str:
    if os.name == "nt":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            nxt = msvcrt.getwch()
            return {
                "H": "up",
                "P": "down",
                "K": "left",
                "M": "right",
                "G": "home",
                "O": "end",
            }.get(nxt, "")
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\t":
            return "tab"
        if ch == "\x1b":
            return "esc"
        if ch in {"q", "Q"}:
            return "quit"
        if ch in {"s", "S"}:
            return "save"
        if ch in {"a", "A"}:
            return "adb"
        return ch
    ch = sys.stdin.read(1)
    if ch == "\x1b":
        seq = sys.stdin.read(2)
        return {"[A": "up", "[B": "down", "[D": "left", "[C": "right"}.get(seq, "esc")
    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\t":
        return "tab"
    if ch in {"q", "Q"}:
        return "quit"
    if ch in {"s", "S"}:
        return "save"
    if ch in {"a", "A"}:
        return "adb"
    return ch


def read_choice_key() -> str:
    show_cursor()
    raw = input("请选择：").strip()
    hide_cursor()
    return raw


def enter_alt_screen() -> None:
    sys.stdout.write("\033[?1049h")
    sys.stdout.flush()


def exit_alt_screen() -> None:
    sys.stdout.write("\033[?1049l")
    sys.stdout.flush()


def clear_screen() -> None:
    sys.stdout.write("\033[H\033[J")
    sys.stdout.flush()


def hide_cursor() -> None:
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()


def show_cursor() -> None:
    sys.stdout.write("\033[?25h")
    sys.stdout.flush()


def grouped_settings() -> list[tuple[str, list[int]]]:
    groups: list[tuple[str, list[int]]] = []
    seen: dict[str, list[int]] = {}
    for index, item in enumerate(SETTINGS):
        if item.group not in seen:
            seen[item.group] = []
            groups.append((item.group, seen[item.group]))
        seen[item.group].append(index)
    return groups


def setting_indexes_for_keys(keys: tuple[str, ...]) -> list[int]:
    indexes: list[int] = []
    for key in keys:
        for index, item in enumerate(SETTINGS):
            if item.key == key:
                indexes.append(index)
                break
    return indexes


def dirty_count(original_values: dict[str, str], values: dict[str, str]) -> int:
    return len(changed_values(original_values, values))


def config_status(values: dict[str, str]) -> dict[str, str]:
    bridge = is_true(values.get("WHATSAPP_USE_BRIDGE", ""))
    ui_text = is_true(values.get("WHATSAPP_USE_UI_TEXT", ""))
    ocr = is_true(values.get("WHATSAPP_USE_OCR", ""))
    notifications = is_true(values.get("WHATSAPP_USE_NOTIFICATIONS", ""))
    if bridge and not ui_text and not ocr and not notifications:
        flow2_mode = "高性能"
    elif bridge and not ui_text and not ocr:
        flow2_mode = "稳定"
    elif bridge:
        flow2_mode = "调试/兜底"
    else:
        flow2_mode = "非 Bridge"
    whatsapp_devices = sum(1 for index in range(1, 4) if values.get(f"WHATSAPP_ADB_DEVICE_{index}", "").strip())
    gopay_devices = sum(1 for index in range(1, 4) if values.get(f"GOPAY_ADB_DEVICE_{index}", "").strip())
    phones = sum(1 for index in range(1, 4) if values.get(f"GOPAY_PHONE_{index}", "").strip())
    return {
        "Flow2": flow2_mode,
        "Bridge": "开" if bridge else "关",
        "ADB": f"WA {whatsapp_devices}/3, GoPay {gopay_devices}/3",
        "PIN": "已配置" if len(values.get("GOPAY_PIN", "").strip()) == 6 else "未配置",
        "手机号": f"{phones}/3",
        "截图": "成功保留" if is_true(values.get("FLOW2_SAVE_SUCCESS_SCREENSHOTS", "")) else "仅失败",
        "HTML": "保留" if is_true(values.get("FLOW2_SAVE_HTML", "")) else "少写",
        "代理": "开" if is_true(values.get("USE_PROXY", "")) else "关",
        "邮箱源": values.get("MAIL_SOURCE", "") or "-",
        "流程一": flow_summary(values, "FLOW1"),
        "流程三": flow_summary(values, "FLOW3"),
        "Free": flow_summary(values, "FREE"),
    }


def inherited_value(values: dict[str, str], key: str, fallback_key: str, default: str = "-") -> str:
    return values.get(key, "").strip() or values.get(fallback_key, "").strip() or default


def flow_summary(values: dict[str, str], flow_key: str) -> str:
    mail = inherited_value(values, f"{flow_key}_MAIL_SOURCE", "MAIL_SOURCE", "-")
    sms_enabled = inherited_value(values, f"{flow_key}_SMS_ENABLED", "SMS_ENABLED", "true")
    sms_provider = inherited_value(values, f"{flow_key}_SMS_PROVIDER", "SMS_PROVIDER", "herosms")
    sms = "接码开" if is_true(sms_enabled) else "接码关"
    return f"{mail} / {sms} / {sms_provider}"


def render_header(title: str, original_values: dict[str, str], values: dict[str, str]) -> None:
    status = config_status(values)
    dirty = dirty_count(original_values, values)
    print(paint("Config Center", MAGENTA, bold=True), paint(f"  {title}", CYAN, bold=True))
    print(
        f"状态: Flow2={status['Flow2']} | Bridge={status['Bridge']} | ADB={status['ADB']} | "
        f"PIN={status['PIN']} | 流程一={status['流程一']} | Free={status['Free']} | 未保存={dirty}"
    )
    print()


def render_dashboard(path: Path, values: dict[str, str], original_values: dict[str, str]) -> None:
    clear_screen()
    status = config_status(values)
    dirty = dirty_count(original_values, values)
    print(paint("Config Center", MAGENTA, bold=True), paint("  /  Dashboard", CYAN, bold=True))
    print()
    width = 64
    print(paint("关键状态", MAGENTA, bold=True))
    print(paint("-" * width, MAGENTA))

    def _flow_mail(flow: str) -> str:
        return inherited_value(values, f"{flow}_MAIL_SOURCE", "MAIL_SOURCE", "-")

    def _flow_sms(flow: str) -> str:
        enabled = is_true(inherited_value(values, f"{flow}_SMS_ENABLED", "SMS_ENABLED", "true"))
        provider = inherited_value(values, f"{flow}_SMS_PROVIDER", "SMS_PROVIDER", "herosms")
        state = paint("开", GREEN, bold=True) if enabled else paint("关", YELLOW)
        return f"{state}({provider})" if enabled else state

    mail_line = (
        f"流程1:{paint(_flow_mail('FLOW1'), GREEN)}  "
        f"流程3:{paint(_flow_mail('FLOW3'), GREEN)}  "
        f"Free:{paint(_flow_mail('FREE'), GREEN)}"
    )
    sms_line = f"流程1:{_flow_sms('FLOW1')}  流程3:{_flow_sms('FLOW3')}  Free:{_flow_sms('FREE')}"

    flow2_color = GREEN if status["Flow2"] == "高性能" else YELLOW
    flow2_line = paint(status["Flow2"], flow2_color, bold=True)
    if status["Flow2"] == "高性能":
        flow2_line += paint("  (Bridge-only)", CYAN)

    phones = sum(1 for index in range(1, 4) if values.get(f"GOPAY_PHONE_{index}", "").strip())
    device_line = f"WA {status['ADB'].split(',')[0].split()[-1]}  GoPay {status['ADB'].split(',')[1].strip().split()[-1]}  手机号 {phones}/3"

    pin_color = GREEN if status["PIN"] == "已配置" else YELLOW
    proxy_color = GREEN if status["代理"] == "开" else YELLOW

    rows = [
        ("邮箱源", mail_line, ""),
        ("接码", sms_line, ""),
        ("流程2 模式", flow2_line, paint("P 切预设", CYAN)),
        ("设备绑定", device_line, paint("A 绑定 ADB", CYAN)),
        ("GoPay PIN", paint(status["PIN"], pin_color, bold=True), paint("(不显示明文)", CYAN)),
        ("代理", paint(status["代理"], proxy_color, bold=True), ""),
    ]
    for label, value, hint in rows:
        line = f"  {pad_display(label, 12)}  {value}"
        if hint:
            line += f"    {hint}"
        print(line)
    print(paint("-" * width, MAGENTA))
    dirty_text = (
        paint(f"未保存修改: {dirty}", YELLOW, bold=True)
        if dirty
        else paint("未保存修改: 0", GREEN)
    )
    print(dirty_text)
    print()
    print(paint("功能入口", MAGENTA, bold=True))
    menu_items = [
        ("1", "按场景配置", "大多数场景进这里"),
        ("2", "全量查找/修改", f"{len(SETTINGS)} 项全开"),
        ("3", "套用流程2预设", "日常 / 稳定 / 调试"),
        ("4", "体检", "只读诊断"),
        ("5", "恢复备份", "回滚历史 .env"),
        ("S", "保存", "diff + 自动备份"),
        ("Q", "返回", ""),
    ]
    for key_hint, name, desc in menu_items:
        line = f"  {key_hint}. {pad_display(name, 18)}"
        if desc:
            line += f"  {desc}"
        print(line)
    print()
    print(paint(f".env: {path}", CYAN))


def render_settings(values: dict[str, str], group_index: int, selected_in_group: int, dirty: bool) -> None:
    clear_screen()
    print(paint("设置面板 (.env)", MAGENTA, bold=True))
    print("↑↓ 选择项目，←→ 调整当前值，Tab 切换分组，Enter 编辑，P 流程2预设，C 体检，A 绑定ADB，S 保存，Q 返回。")
    if dirty:
        print(paint("有未保存修改", YELLOW, bold=True))
    print()
    groups = grouped_settings()
    tabs = []
    for index, (name, _indexes) in enumerate(groups):
        tabs.append(paint(f" {name} ", MAGENTA, bold=True) if index == group_index else f" {name} ")
    print("|".join(tabs))
    print()
    group_name, indexes = groups[group_index]
    print(paint(f"[{group_name}]", CYAN, bold=True))
    last_section = ""
    for row_index, item_index in enumerate(indexes):
        item = SETTINGS[item_index]
        if item.section and item.section != last_section:
            if last_section:
                print(paint("  " + "-" * 54, MAGENTA))
            print(paint(f"  {item.section}", MAGENTA, bold=True))
            last_section = item.section
        selected = row_index == selected_in_group
        marker = ">" if selected else " "
        key_color = GREEN if selected else ""
        value = display_value(item, values.get(item.key, ""))
        line = f"{marker} {pad_display(item.label, 18)} {value}"
        if selected:
            print(paint(line, key_color or GREEN, bold=True))
            if item.help_text:
                print(paint(f"  {item.help_text}", YELLOW))
            if item.kind == "bool":
                print(paint(f"  当前写入: {item.key}={values.get(item.key, '')}。按 ←/→ 切换开启/关闭。", YELLOW))
            elif item.choices:
                print(paint(f"  可选：{' / '.join(item.choices)}。当前写入: {item.key}={values.get(item.key, '')}", YELLOW))
            elif item.kind == "int":
                print(paint(f"  当前写入: {item.key}={values.get(item.key, '')}。按 ←/→ 微调，Enter 输入指定数字。", YELLOW))
            else:
                print(paint(f"  当前写入: {item.key}={raw_value_for_help(item, values.get(item.key, ''))}。按 Enter 编辑。", YELLOW))
        else:
            print(line)
    print()
    print(paint(f"当前分组 {group_index + 1}/{len(groups)}，共 {len(indexes)} 项。", CYAN))
    print(paint("敏感值只做显示遮罩，保存时仍保留真实值。", CYAN))


def render_item_editor(
    title: str,
    indexes: list[int],
    values: dict[str, str],
    selected: int,
    original_values: dict[str, str],
    subtitle: str = "",
) -> None:
    clear_screen()
    render_header(title, original_values, values)
    if subtitle:
        print(paint(subtitle, YELLOW))
        print()
    print(paint("配置项", MAGENTA, bold=True))
    print(paint("-" * 76, MAGENTA))
    for row_index, item_index in enumerate(indexes):
        item = SETTINGS[item_index]
        marker = ">" if row_index == selected else " "
        value = display_value(item, values.get(item.key, ""))
        color = GREEN if row_index == selected else ""
        line = f"{marker} {pad_display(item.label, 22)} {pad_display(value, 18)} {item.key}"
        print(paint(line, color, bold=row_index == selected) if row_index == selected else line)
    print(paint("-" * 76, MAGENTA))
    if indexes:
        item = SETTINGS[indexes[selected]]
        print()
        print(paint("当前项", CYAN, bold=True))
        print(f"{item.label}  {paint(item.key, CYAN)}")
        if item.help_text:
            print(item.help_text)
        if item.kind == "bool":
            print(f"写入: {item.key}={values.get(item.key, '')} | ←/→ 切换")
        elif item.choices:
            print(f"可选: {' / '.join(item.choices)} | 写入: {item.key}={values.get(item.key, '')}")
        elif item.kind == "int":
            print(f"写入: {item.key}={values.get(item.key, '')} | ←/→ 微调，Enter 输入")
        else:
            print(f"写入: {item.key}={raw_value_for_help(item, values.get(item.key, ''))} | Enter 编辑")
    print()
    print("↑↓ 选择 | ←→ 调整 | Enter 编辑 | S 保存 | B 返回首页 | Q 返回")


def edit_value(item: SettingItem, values: dict[str, str]) -> str:
    show_cursor()
    print()
    current = values.get(item.key, "")
    shown = mask_value(current) if item.masked and current else current
    print(paint(f"编辑 {item.label} ({item.key})，当前值: {shown}", CYAN, bold=True))
    if item.masked:
        raw = input("输入新值；直接回车保留原值：").strip()
        return current if raw == "" else raw
    raw = input("输入新值；直接回车可设为空：")
    return raw.strip()


def confirm_and_save(path: Path, original_values: dict[str, str], values: dict[str, str]) -> bool:
    changes = changed_values(original_values, values)
    show_cursor()
    print()
    if not changes:
        print(paint("没有配置变化，无需保存。", CYAN, bold=True))
        input("按回车继续。")
        hide_cursor()
        return True
    print(paint("保存前确认：本次将修改以下 .env 配置", MAGENTA, bold=True))
    for item, old, new in changes:
        print(
            f"- {item.key}: "
            f"{paint(display_diff_value(item, old), YELLOW)} -> "
            f"{paint(display_diff_value(item, new), GREEN)}"
        )
    print()
    answer = input("确认保存？输入 y 保存，其他键取消：").strip().lower()
    if answer not in {"y", "yes", "是"}:
        print(paint("已取消保存。", YELLOW, bold=True))
        input("按回车继续。")
        hide_cursor()
        return False
    backup_path = backup_env(path)
    write_env_values(path, values)
    if backup_path:
        print(paint(f"已保存 .env，备份: {backup_path}", GREEN, bold=True))
    else:
        print(paint("已保存 .env。原文件不存在，本次未生成备份。", GREEN, bold=True))
    input("按回车继续。")
    hide_cursor()
    return True


def apply_flow2_preset(values: dict[str, str]) -> bool:
    show_cursor()
    print()
    print(paint("流程 2 预设模式", MAGENTA, bold=True))
    for index, (name, description, updates) in enumerate(FLOW2_PRESETS, start=1):
        changed = sum(1 for key, value in updates.items() if values.get(key, "") != value)
        print(f"{index}. {name} - {description}（将调整 {changed} 项）")
    print("0. 返回")
    raw = input("请选择预设：").strip()
    if raw in {"", "0"}:
        hide_cursor()
        return False
    try:
        index = int(raw)
    except ValueError:
        print(paint("输入无效。", RED, bold=True))
        input("按回车继续。")
        hide_cursor()
        return False
    if index < 1 or index > len(FLOW2_PRESETS):
        print(paint("选项超出范围。", RED, bold=True))
        input("按回车继续。")
        hide_cursor()
        return False
    name, _description, updates = FLOW2_PRESETS[index - 1]
    print()
    print(paint(f"将应用预设：{name}", CYAN, bold=True))
    for key, new in updates.items():
        item = SETTINGS_BY_KEY.get(key, SettingItem(key, key, "流程二支付"))
        old = values.get(key, "")
        if old != new:
            print(f"- {key}: {display_diff_value(item, old)} -> {display_diff_value(item, new)}")
    answer = input("确认应用到当前未保存配置？输入 y 应用：").strip().lower()
    if answer in {"y", "yes", "是"}:
        values.update(updates)
        hide_cursor()
        return True
    hide_cursor()
    return False


def check_path(value: str) -> tuple[bool, str]:
    if not value.strip():
        return False, "未填写"
    path = resolve_path(value)
    return path.exists(), str(path)


def flow2_health_rows(path: Path, values: dict[str, str]) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    adb_value = values.get("WHATSAPP_ADB_PATH", "") or "tools\\adb\\adb.exe"
    adb_ok, adb_detail = check_path(adb_value)
    rows.append(("ADB 路径", "OK" if adb_ok else "WARN", adb_detail))
    if adb_ok:
        try:
            result = subprocess.run(
                [str(resolve_path(adb_value)), "devices"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            device_lines = [
                line.strip()
                for line in result.stdout.splitlines()
                if line.strip() and not line.lower().startswith("list of devices")
            ]
            ready = [line for line in device_lines if line.endswith("\tdevice") or " device" in line]
            rows.append(("ADB devices", "OK" if ready else "WARN", f"device={len(ready)} | raw={len(device_lines)}"))
        except Exception as exc:  # noqa: BLE001
            rows.append(("ADB devices", "WARN", f"执行失败: {exc}"))
    bridge = is_true(values.get("WHATSAPP_USE_BRIDGE", ""))
    ui_text = is_true(values.get("WHATSAPP_USE_UI_TEXT", ""))
    ocr = is_true(values.get("WHATSAPP_USE_OCR", ""))
    notifications = is_true(values.get("WHATSAPP_USE_NOTIFICATIONS", ""))
    if bridge and not ui_text and not ocr:
        rows.append(("WhatsApp 取码", "OK", "Bridge-only，性能优先"))
    elif bridge:
        rows.append(("WhatsApp 取码", "WARN", f"Bridge 开启，但 UI_TEXT={ui_text}, OCR={ocr}, 通知={notifications}"))
    else:
        rows.append(("WhatsApp 取码", "WARN", "未启用 Bridge，可能增加 ADB/页面扫描开销"))
    interval = values.get("WHATSAPP_CODE_INTERVAL", "")
    rows.append(("取码间隔", "OK" if interval in {"1", "2", "3"} else "WARN", interval or "未填写"))
    pin = values.get("GOPAY_PIN", "")
    rows.append(("GoPay PIN", "OK" if len(pin.strip()) == 6 and pin.strip().isdigit() else "WARN", "已配置 6 位" if pin else "未填写"))
    phones = [values.get(f"GOPAY_PHONE_{index}", "").strip() for index in range(1, 4)]
    phone_count = sum(1 for item in phones if item)
    rows.append(("GoPay 手机号", "OK" if phone_count else "WARN", f"固定槽位={phone_count}/3"))
    whatsapp_devices = [values.get(f"WHATSAPP_ADB_DEVICE_{index}", "").strip() for index in range(1, 4)]
    rows.append(("WhatsApp 设备绑定", "OK" if any(whatsapp_devices) else "WARN", f"已绑定={sum(1 for item in whatsapp_devices if item)}/3"))
    gopay_devices = [values.get(f"GOPAY_ADB_DEVICE_{index}", "").strip() for index in range(1, 4)]
    rows.append(("GoPay 解绑设备", "OK" if any(gopay_devices) else "WARN", f"已绑定={sum(1 for item in gopay_devices if item)}/3"))
    success_file = resolve_path("output/gopay注册plus/流程1_注册成功长链接.txt")
    paid_file = resolve_path("output/gopay注册plus/流程2_支付成功待授权.txt")
    rows.append(("流程1长链接池", "OK" if success_file.exists() and success_file.read_text(encoding="utf-8", errors="ignore").strip() else "WARN", str(success_file)))
    rows.append(("流程2成功池", "OK" if paid_file.exists() else "WARN", str(paid_file)))
    if is_true(values.get("USE_PROXY", "")):
        proxy_ok, proxy_detail = check_path(values.get("PROXY_FILE", ""))
        rows.append(("代理池", "OK" if proxy_ok else "WARN", proxy_detail))
    if is_true(values.get("PAYPAL_REGISTER_USE_PROXY", "")):
        proxy_ok, proxy_detail = check_path(values.get("PAYPAL_REGISTER_PROXY_FILE", ""))
        rows.append(("PayPal 注册代理池", "OK" if proxy_ok else "WARN", proxy_detail))
    if is_true(values.get("PAYPAL_USE_PROXY", "")):
        proxy_ok, proxy_detail = check_path(values.get("PAYPAL_PROXY_FILE", ""))
        rows.append(("PayPal 支付代理池", "OK" if proxy_ok else "WARN", proxy_detail))
    if path.exists():
        rows.append((".env 文件", "OK", str(path)))
    else:
        rows.append((".env 文件", "WARN", "当前不存在，保存后会创建"))
    return rows


def show_health_check(path: Path, values: dict[str, str]) -> None:
    show_cursor()
    print()
    print(paint("配置体检", MAGENTA, bold=True))
    rows = flow2_health_rows(path, values)
    for label, status, detail in rows:
        color = GREEN if status == "OK" else YELLOW
        print(f"{paint(status, color, bold=True):<18} {label:<16} {detail}")
    print()
    print(paint("体检只做提示，不会自动修改配置。", CYAN))
    input("按回车继续。")
    hide_cursor()


def run_item_editor(
    title: str,
    indexes: list[int],
    values: dict[str, str],
    original_values: dict[str, str],
    path: Path,
    subtitle: str = "",
) -> tuple[bool, dict[str, str]]:
    if not indexes:
        return False, original_values
    selected = 0
    dirty = False
    while True:
        render_item_editor(title, indexes, values, selected, original_values, subtitle)
        key = get_key()
        if key == "up":
            selected = (selected - 1) % len(indexes)
        elif key == "down":
            selected = (selected + 1) % len(indexes)
        elif key == "home":
            selected = 0
        elif key == "end":
            selected = len(indexes) - 1
        elif key in {"left", "right", " "}:
            item = SETTINGS[indexes[selected]]
            step = -1 if key == "left" else 1
            values[item.key] = cycle_value(item, values.get(item.key, ""), step)
            dirty = True
        elif key == "enter":
            item = SETTINGS[indexes[selected]]
            values[item.key] = edit_value(item, values)
            dirty = True
        elif key == "save":
            if confirm_and_save(path, original_values, values):
                original_values = snapshot_known_values(values)
                dirty = False
        elif key in {"b", "B", "quit", "esc"}:
            return dirty, original_values


def choose_wizard_section(
    values: dict[str, str],
    original_values: dict[str, str],
    path: Path,
) -> tuple[bool, dict[str, str]]:
    clear_screen()
    render_header("按场景配置", original_values, values)
    print(paint("按用途配置，不需要记 .env key。", CYAN))
    print()
    for index, (name, description, keys) in enumerate(WIZARD_SECTIONS, start=1):
        changed = sum(1 for key in keys if values.get(key, "") != original_values.get(key, ""))
        suffix = f" | 未保存 {changed}" if changed else ""
        print(f"{index}. {pad_display(name, 24)}  {description}{suffix}")
    print("0. 返回首页")
    raw = read_choice_key()
    if raw in {"", "0"}:
        return False, original_values
    try:
        index = int(raw)
    except ValueError:
        return False, original_values
    if index < 1 or index > len(WIZARD_SECTIONS):
        return False, original_values
    name, description, keys = WIZARD_SECTIONS[index - 1]
    indexes = setting_indexes_for_keys(keys)
    return run_item_editor(f"Wizard / {name}", indexes, values, original_values, path, description)


def run_advanced_settings(
    path: Path,
    values: dict[str, str],
    original_values: dict[str, str],
) -> tuple[bool, dict[str, str]]:
    groups = grouped_settings()
    group_index = 0
    selected_in_group = 0
    dirty = False
    while True:
        render_settings(values, group_index, selected_in_group, dirty)
        key = get_key()
        _group_name, indexes = groups[group_index]
        if key == "up":
            selected_in_group = (selected_in_group - 1) % len(indexes)
        elif key == "down":
            selected_in_group = (selected_in_group + 1) % len(indexes)
        elif key == "home":
            selected_in_group = 0
        elif key == "end":
            selected_in_group = len(indexes) - 1
        elif key == "tab":
            group_index = (group_index + 1) % len(groups)
            selected_in_group = min(selected_in_group, len(groups[group_index][1]) - 1)
        elif key == "left":
            item = SETTINGS[indexes[selected_in_group]]
            values[item.key] = cycle_value(item, values.get(item.key, ""), -1)
            dirty = True
        elif key == "right":
            item = SETTINGS[indexes[selected_in_group]]
            values[item.key] = cycle_value(item, values.get(item.key, ""), 1)
            dirty = True
        elif key == "[":
            group_index = (group_index - 1) % len(groups)
            selected_in_group = min(selected_in_group, len(groups[group_index][1]) - 1)
        elif key == "]":
            group_index = (group_index + 1) % len(groups)
            selected_in_group = min(selected_in_group, len(groups[group_index][1]) - 1)
        elif key == " ":
            item = SETTINGS[indexes[selected_in_group]]
            values[item.key] = cycle_value(item, values.get(item.key, ""), 1)
            dirty = True
        elif key == "enter":
            item = SETTINGS[indexes[selected_in_group]]
            values[item.key] = edit_value(item, values)
            hide_cursor()
            dirty = True
        elif key == "save":
            if confirm_and_save(path, original_values, values):
                original_values = snapshot_known_values(values)
                dirty = False
            render_settings(values, group_index, selected_in_group, dirty)
        elif key in {"p", "P"}:
            if apply_flow2_preset(values):
                dirty = True
        elif key in {"c", "C"}:
            show_health_check(path, values)
        elif key == "adb":
            show_cursor()
            exit_alt_screen()
            from modules.adb_device_manager import adb_binding_wizard

            adb_binding_wizard(path)
            values.update(parse_env(read_env_lines(path)))
            original_values = snapshot_known_values(values)
            enter_alt_screen()
            hide_cursor()
            dirty = False
        elif key in {"b", "B", "quit", "esc"}:
            return dirty, original_values


def backup_files(path: Path) -> list[Path]:
    backup_dir = path.parent / "output" / "env_backups"
    if not backup_dir.exists():
        return []
    return sorted(backup_dir.glob(".env.backup.*"), key=lambda item: item.stat().st_mtime, reverse=True)


def restore_backup(path: Path, values: dict[str, str]) -> bool:
    show_cursor()
    print()
    files = backup_files(path)
    if not files:
        print(paint("暂无 .env 备份。", YELLOW, bold=True))
        input("按回车继续。")
        hide_cursor()
        return False
    print(paint("Backup 回滚", MAGENTA, bold=True))
    for index, backup in enumerate(files[:10], start=1):
        stamp = datetime.fromtimestamp(backup.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"{index}. {backup.name}  {stamp}")
    print("0. 返回")
    raw = input("选择要恢复的备份：").strip()
    if raw in {"", "0"}:
        hide_cursor()
        return False
    try:
        index = int(raw)
    except ValueError:
        hide_cursor()
        return False
    if index < 1 or index > min(10, len(files)):
        hide_cursor()
        return False
    selected = files[index - 1]
    answer = input(f"确认用 {selected.name} 覆盖当前 .env？输入 y 确认：").strip().lower()
    if answer not in {"y", "yes", "是"}:
        hide_cursor()
        return False
    backup_env(path)
    shutil.copy2(selected, path)
    values.clear()
    values.update(parse_env(read_env_lines(path)))
    print(paint("已恢复备份，并为恢复前 .env 生成了新备份。", GREEN, bold=True))
    input("按回车继续。")
    hide_cursor()
    return True


def settings_panel(env_path: str | Path = ".env") -> bool:
    path = resolve_path(env_path)
    values = parse_env(read_env_lines(path))
    original_values = snapshot_known_values(values)
    enter_alt_screen()
    hide_cursor()
    try:
        while True:
            render_dashboard(path, values, original_values)
            key = get_key()
            if key == "1":
                _dirty, original_values = choose_wizard_section(values, original_values, path)
            elif key == "2":
                _dirty, original_values = run_advanced_settings(path, values, original_values)
            elif key == "3" or key in {"p", "P"}:
                apply_flow2_preset(values)
            elif key == "4" or key in {"c", "C"}:
                show_health_check(path, values)
            elif key == "5":
                if restore_backup(path, values):
                    original_values = snapshot_known_values(values)
            elif key == "save":
                if confirm_and_save(path, original_values, values):
                    original_values = snapshot_known_values(values)
            elif key == "adb":
                show_cursor()
                exit_alt_screen()
                from modules.adb_device_manager import adb_binding_wizard

                adb_binding_wizard(path)
                values = parse_env(read_env_lines(path))
                original_values = snapshot_known_values(values)
                enter_alt_screen()
                hide_cursor()
            elif key in {"quit", "esc"}:
                if dirty_count(original_values, values):
                    show_cursor()
                    print()
                    answer = input("有未保存修改，输入 y 保存后返回；其他键放弃：").strip().lower()
                    if answer in {"y", "yes", "是"}:
                        confirm_and_save(path, original_values, values)
                        return True
                    return False
                return True
    finally:
        show_cursor()
        exit_alt_screen()
