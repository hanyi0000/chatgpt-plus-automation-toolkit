# ChatGPT 全流程助手

自动化完成 ChatGPT Plus 账号注册、支付、授权、Session 导出的全链路工具。

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)

## 重要说明

- 本项目仅用于自动化技术学习与工程实践演示。
- 使用前请自行确认目标平台条款、当地法律与数据合规要求。
- 仓库默认不包含真实账号、密钥、卡密、手机号等敏感数据。

## 功能概览

| 编号 | 功能 | 说明 |
|------|------|------|
| 流程一 | 注册账号生成长链接 | 邮箱注册 ChatGPT → 获取 accessToken → 生成 Plus 支付链接 |
| 流程二 | GoPay 支付长链接 | 打开支付链接 → 填写账单 → GoPay 绑定 → OTP → PIN → 支付完成 |
| 流程三 | OAuth 账号授权 | 已支付账号 → OAuth 获取 refresh_token → 落盘 |
| 流程四 | Session 本地导出 | 从浏览器 profile 提取 session → 导出为 sub2api 格式 |
| 流程五 | Free 注册 | 注册免费 ChatGPT 账号（手机号/邮箱方式） |
| PayPal Plus | 注册 + 支付 + 授权 | iCloud 邮箱注册 → PayPal 绑卡支付 → OAuth 授权（独立子流程） |

## 环境要求

- Python 3.10+
- Playwright（自动安装 Chromium）
- Windows / macOS / Linux

## 安装

```bash
# 安装依赖
pip install -r requirements.txt

# 安装 Playwright 浏览器
playwright install chromium
```

## 快速开始

```bash
# 交互式菜单（推荐）
python main.py

# 命令行模式
python main.py --mode register --count 5 --workers 3
python main.py --mode gopay
python main.py --mode authorize
python main.py --mode session-export
python main.py --mode free-register --count 10 --workers 5
```

## 开源版初始化

```bash
# 1) 复制示例配置
cp .env.example .env
cp config.example.yaml config.yaml

# 2) 按需填写 .env 中的邮箱源/代理/API key
# 3) 准备 data/ 下的账号池、代理池、手机号池、卡池示例文件
```

## 文档导航

- 快速上手：`START_HERE_CN.txt`
- 配置清单：`.env.example`、`config.example.yaml`
- 安全策略：`SECURITY.md`
- 贡献指南：`CONTRIBUTING.md`
- 可执行文件：`release/ChatGPTAssistantPanel-portable-lite.zip`

## 交流群

- QQ 群号：`1104779544`

![注册机交流群二维码](docs/images/qq-group-1104779544.jpg)

## 目录结构

```
├── main.py                    # 主入口 + 流程一逻辑
├── authorization_flow.py      # 流程三：OAuth 授权
├── fill_billing_test.py       # 流程二：GoPay 支付
├── config.yaml                # 主配置文件
├── .env                       # 环境变量配置（敏感信息）
├── modules/
│   ├── browser.py             # 浏览器会话管理（指纹、代理）
│   ├── chatgpt_register.py    # ChatGPT 注册核心逻辑
│   ├── checkout.py            # 生成支付长链接
│   ├── free_register.py       # Free 注册流程
│   ├── free_browser_flow.py   # Free 注册浏览器操作
│   ├── paypal_flow.py         # PayPal Plus 主编排
│   ├── paypal_register.py     # PayPal 流程一：注册生成长链接
│   ├── paypal_pay.py          # PayPal 流程二：PayPal 绑卡支付
│   ├── paypal_card_pool.py    # PayPal 虚拟卡池管理
│   ├── paypal_phone_pool.py   # PayPal 手机号池管理
│   ├── billing_provider.py    # 美国地址生成
│   ├── mail_provider.py       # 邮箱验证码获取
│   ├── proxy_pool.py          # 代理池
│   ├── storage.py             # 账号存储管理
│   ├── session_export.py      # Session 导出
│   ├── env_settings.py        # 设置面板
│   ├── utils.py               # 工具函数
│   └── ...
├── data/
│   ├── accounts.txt           # 流程一邮箱账号池
│   ├── mail_pool.txt          # 原始邮箱池
│   ├── proxies/proxies.txt    # 代理列表
│   ├── icloud/                # iCloud 邮箱配置
│   ├── hotmail/               # Hotmail 邮箱配置
│   ├── qq/                    # QQ 邮箱授权码账号池
│   └── paypal/
│       ├── icloud_accounts.txt  # PayPal 流程 iCloud 账号
│       ├── cards.txt            # 虚拟卡池
│       └── phones.txt           # 手机号池
├── output/
│   ├── 流程1_注册成功长链接.txt
│   ├── 流程2_支付成功待授权.txt
│   ├── 授权输出/              # 流程三输出
│   ├── session导出/           # 流程四输出
│   ├── free成品/              # Free 注册输出
│   └── paypal成品/            # PayPal Plus 输出
│       ├── 长链接账号/
│       ├── 待授权账号/
│       └── 授权成功/
└── profiles/                  # 浏览器 profile 缓存
```

## 配置说明

### .env 环境变量

复制 `.env.example` 为 `.env` 并填写：

```bash
cp .env.example .env
```

#### 邮箱源配置

```ini
# 邮箱源选择：moemail / hotmail / icloud_query / qq
MAIL_SOURCE=moemail

# 各流程可单独指定邮箱源（覆盖全局）
FLOW1_MAIL_SOURCE=icloud_query
FREE_MAIL_SOURCE=moemail

# 邮箱账号模式：pool（从文件读取）
MAIL_ACCOUNT_MODE=pool
```

#### QQ 邮箱 IMAP

QQ 邮箱来源适用于流程一及 PayPal 注册/后续授权。先在 QQ 邮箱设置中启用 IMAP/SMTP 服务并生成授权码，然后设置：

```ini
MAIL_SOURCE=qq
# 或仅覆盖流程一
FLOW1_MAIL_SOURCE=qq
```

在 `data/qq/accounts.txt` 与 `data/qq/mail_pool.txt` 中填入相同账号资料：

```text
user@qq.com----QQ邮箱IMAP授权码
```

支持 `@qq.com`、`@foxmail.com` 与 `@vip.qq.com`；第二段为授权码，不是邮箱登录密码。

#### MoeMail 自建邮箱池

```ini
MOEMAIL_ENABLED=false
MOEMAIL_BASE_URL=https://your-moemail-server.com
MOEMAIL_API_KEY=your-api-key
MOEMAIL_DOMAIN_WHITELIST=domain1.com,domain2.com
MOEMAIL_CREATE_PREFIX=openai
MOEMAIL_CREATE_MODE=human
```

#### 代理配置

```ini
# 通用流程一 / Free 注册代理
USE_PROXY=false
PROXY_FILE=data/proxies/proxies.txt

# PayPal 流程一注册与 Session 补录代理（默认直连）
PAYPAL_REGISTER_USE_PROXY=false
PAYPAL_REGISTER_PROXY_FILE=data/proxies/proxies_jp.txt

# PayPal 流程二支付代理
PAYPAL_USE_PROXY=false
PAYPAL_PROXY_FILE=data/proxies/proxies.txt
```

`PAYPAL_USE_PROXY=true` 仅影响 PayPal 支付阶段，不会让注册阶段自动走代理；注册需要代理时单独开启 `PAYPAL_REGISTER_USE_PROXY`。

代理文件格式（每行一个）：
```
http://user:pass@host:port
socks5://host:port
host:port
user:pass@host:port
```

`user:pass@host:port` 会按 HTTP 认证代理处理。当前不支持直接填写 `vless://...`；请先通过本地代理客户端转换为 HTTP 或 SOCKS5 监听地址。

#### 接码平台配置

支持 HeroSMS / GrizzlySMS / 5sim 三个平台：

```ini
# 全局开关
SMS_ENABLED=true
SMS_PROVIDER=herosms

# HeroSMS
HERO_SMS_API_KEY=your-key
HERO_SMS_SERVICE=dr
HERO_SMS_COUNTRY_TOP_N=10

# GrizzlySMS
GRIZZLY_API_KEY=your-key
GRIZZLY_SERVICE=auto

# 5sim
FIVESIM_API_KEY=your-key
FIVESIM_SERVICE=openai
```

#### 流程二 GoPay 配置

```ini
# GoPay 手机号（支持多线程分别绑定）
GOPAY_PHONE_1=your-phone-1
GOPAY_PHONE_2=your-phone-2
GOPAY_COUNTRY_CODE=+62
GOPAY_PIN=123456

# 账单重试次数
FLOW2_BILLING_RETRIES=5
FLOW2_OTP_TIMEOUT=90

# WhatsApp OTP 自动取码（需要 ADB）
WHATSAPP_OTP_AUTO=false
WHATSAPP_ADB_PATH=tools\adb\adb.exe
```

#### PayPal 流程配置

```ini
# PayPal 虚拟卡和手机号文件
PAYPAL_CARDS_FILE=data/paypal/cards.txt
PAYPAL_PHONES_FILE=data/paypal/phones.txt
PAYPAL_PHONE_MAX_USES=5

# PayPal 注册国家
PAYPAL_BILLING_COUNTRY=US

# PayPal iCloud 账号文件
PAYPAL_ICLOUD_FILE=data/paypal/icloud_accounts.txt
```

#### 人机验证码处理

```ini
# 模式：manual（暂停等手动处理）/ api（打码平台）
PAYPAL_CAPTCHA_MODE=manual
PAYPAL_CAPTCHA_TIMEOUT=180

# 打码平台（api 模式）
CAPTCHA_API_PROVIDER=capsolver
CAPSOLVER_API_KEY=your-key
# 或 2Captcha
# CAPTCHA_API_PROVIDER=twocaptcha
# TWOCAPTCHA_API_KEY=your-key
```

#### 授权服务器上传

```ini
AUTH_SERVER_UPLOAD=false
AUTH_SERVER_URL=http://127.0.0.1:8790
AUTH_SERVER_API_KEY=your-key
```

### config.yaml

主配置文件，包含浏览器参数、ChatGPT 入口、注册 profile 等。一般不需要修改。

### 数据文件格式

#### accounts.txt（流程一邮箱池）

```
email@domain.com----query_code
email2@domain.com----query_code2
```

#### data/paypal/cards.txt（PayPal 虚拟卡）

```
KW-ID----卡号----有效期----CVV----手机号----持卡人----地址----API地址
```

示例：
```
KW-XXXX----4859540153209563----2030/4----254----+16319163234----AMY MCDONALD----201 CENTER AVE S,MONTROSE 55363,US----http://api.example.com/get_sms?key=xxx
```

#### data/paypal/phones.txt（PayPal 手机号池）

```
+1xxxxxxxxxx----http://sms-api-url
```

#### data/paypal/icloud_accounts.txt（PayPal iCloud 账号）

```
email@icloud.com----query_code
```

#### data/qq/accounts.txt（QQ 邮箱账号）

```
user@qq.com----QQ邮箱IMAP授权码
```

#### data/proxies/proxies.txt（代理列表）

```
http://user:pass@ip:port
socks5://ip:port
ip:port
user:pass@ip:port
```

## 输出文件说明

### 流程一输出

- `output/流程1_注册成功长链接.txt` — 格式：`邮箱----查询码----支付链接`

### 流程二输出

- `output/流程2_支付成功待授权.txt` — 格式：`邮箱----查询码----支付链接`

### 流程三输出

- `output/授权输出/account-rt.txt` — 格式：`邮箱----查询码----refresh_token`
- `output/授权输出/sub2api_accounts.json` — sub2api 格式 JSON
- `output/授权输出/tokens/` — 每个账号的完整 token JSON

### PayPal Plus 输出

- `output/paypal成品/长链接账号/account.txt` — 格式：`邮箱----查询码----支付链接`
- `output/paypal成品/待授权账号/account.txt` — 格式：`邮箱----查询码`
- `output/paypal成品/授权成功/account-rt.txt` — 格式：`邮箱----查询码----refresh_token`
- `output/paypal成品/授权成功/sub2api_accounts.json` — sub2api 格式 JSON
- `output/paypal成品/授权成功/tokens/` — 每个账号的完整 token JSON

## 反检测特性

### 浏览器指纹

每个账号自动生成一致的浏览器指纹（基于邮箱 seed）：

- 随机 User-Agent（真实 Chrome UA 池）
- 随机屏幕分辨率（7 种常见分辨率）
- 随机美国时区
- 反自动化检测脚本注入（隐藏 webdriver、伪造 plugins/languages 等）
- 同一邮箱在不同流程阶段指纹保持一致

### 代理支持

- 全局代理池：流程一、Free 注册可用
- PayPal 独立代理池：PayPal 流程单独控制
- 多线程自动轮询分配不同代理
- 支持 HTTP / SOCKS5 / 带认证代理

### 人机验证码

- 自动检测 PayPal 验证码弹窗
- 手动模式：暂停等待人工处理，完成后自动继续
- API 模式：支持 CapSolver / 2Captcha（适用于标准 reCAPTCHA/hCaptcha）

## 命令行参数

```
python main.py [OPTIONS]

选项：
  --mode {register,gopay,authorize,session-export,free-register}
                        功能模式
  --config CONFIG       配置文件路径（默认 config.yaml）
  --workers WORKERS     并发浏览器数量
  --count COUNT         目标成功数量
  --country COUNTRY     接码国家（ISO 代码或平台 ID）
  --sms-provider {herosms,grizzly,fivesim}
                        接码平台
  --mail-source {moemail,hotmail,hotmail_graph,icloud,icloud_query,qq,qq_imap}
                        邮箱源
  --register-mode {phone,email}
                        Free 注册方式（默认 phone）
```

## 多线程说明

- 流程一：支持多线程并发注册，每个线程独立浏览器实例
- 流程二：支持多线程并发支付，每个线程绑定不同 GoPay 手机号/设备
- 流程三：支持多线程并发授权
- PayPal Plus：支持多线程并发（注册/支付分别可设并发数）

## 常见问题

### Q: PayPal 注册时出现人机验证码怎么办？

默认手动模式，脚本会暂停等待你在浏览器中手动完成验证码，完成后自动继续。可通过以下方式降低触发率：
- 使用住宅代理 IP
- 降低同 IP 注册频率
- 不使用 headless 模式

### Q: 流程二 GoPay 支付需要什么准备？

1. 印尼 GoPay 账号（已绑定手机号）
2. WhatsApp 接收 OTP（可选自动取码，需要 ADB 连接手机）
3. GoPay 6 位支付 PIN

### Q: 邮箱源怎么选？

- **iCloud 查询码**：稳定，需要提前准备 iCloud 邮箱 + 查询码
- **MoeMail 自建**：自动创建邮箱，需要部署 MoeMail 服务
- **Hotmail/Outlook**：需要 Microsoft Graph API 配置

### Q: 代理格式支持哪些？

```
# HTTP 代理
http://ip:port
http://user:pass@ip:port

# SOCKS5 代理
socks5://ip:port
socks5://user:pass@ip:port

# 简写（默认 HTTP）
ip:port
user:pass@ip:port
```

`vless://...` 不能直接交给当前使用的 Playwright / HTTP 请求客户端，需转换为 HTTP 或 SOCKS5 代理后填写。

## 许可证

本项目使用 [MIT License](./LICENSE)。
