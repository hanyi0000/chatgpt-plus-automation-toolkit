from __future__ import annotations

import base64
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import LEGACY_OUTPUT_FILES, log, migrate_output_file, output_file, resolve_path, safe_filename


CACHE_PATH = "output/gopay注册plus/session导出/session_cache.jsonl"
EXPORT_ROOT = "output/gopay注册plus/session导出"


def env_bool(value: str | None, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on", "y", "是", "启用"}:
        return True
    if text in {"0", "false", "no", "off", "n", "否", "禁用"}:
        return False
    return default


def read_env(path: str | Path = ".env") -> dict[str, str]:
    env_path = resolve_path(path)
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def session_export_upload_enabled(env: dict[str, str]) -> bool:
    return env_bool(env.get("SESSION_EXPORT_SERVER_UPLOAD"), default=False)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_jwt_payload(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1]
    payload += "=" * (-len(payload) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("utf-8")))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def epoch_from_value(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return float(text)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def iso_from_epoch(value: Any) -> str:
    epoch = epoch_from_value(value)
    if epoch <= 0:
        epoch = time.time()
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def token_expired(token: str, expires: Any = "", skew_seconds: int = 300) -> bool:
    exp = epoch_from_value(expires)
    if exp <= 0:
        exp = epoch_from_value(parse_jwt_payload(token).get("exp"))
    if exp <= 0:
        return True
    return time.time() >= exp - skew_seconds


def token_preview(token: str) -> str:
    token = str(token or "")
    if len(token) <= 18:
        return token or "-"
    return f"{token[:10]}...{token[-6:]}"


def extract_session_record(
    session: dict[str, Any],
    *,
    email: str,
    mail_source: str = "",
    source_format: str = "",
    code_address: str = "",
    payment_link: str = "",
    profile_dir: str = "",
    source: str = "main_flow1",
) -> dict[str, Any]:
    user = session.get("user") if isinstance(session.get("user"), dict) else {}
    account = session.get("account") if isinstance(session.get("account"), dict) else {}
    access_token = str(session.get("accessToken") or session.get("access_token") or "").strip()
    session_token = str(session.get("sessionToken") or session.get("session_token") or "").strip()
    token_claims = parse_jwt_payload(access_token)
    record_email = str(email or user.get("email") or user.get("email_address") or account.get("email") or "").strip()
    account_id = str(account.get("id") or session.get("account_id") or token_claims.get("account_id") or "").strip()
    plan_type = str(account.get("planType") or account.get("plan_type") or session.get("plan_type") or "unknown").strip() or "unknown"
    expires = session.get("expires") or session.get("expired") or token_claims.get("exp") or ""
    return {
        "email": record_email,
        "mail_source": mail_source,
        "source_format": source_format,
        "code_address": code_address,
        "payment_link": payment_link,
        "access_token": access_token,
        "session_token": session_token,
        "account_id": account_id,
        "user_id": str(user.get("id") or token_claims.get("sub") or "").strip(),
        "plan_type": plan_type,
        "expires": iso_from_epoch(expires) if epoch_from_value(expires) else str(expires or ""),
        "profile_dir": profile_dir,
        "created_at": utc_now(),
        "source": source,
    }


def load_session_cache(path: str | Path = CACHE_PATH) -> list[dict[str, Any]]:
    cache_path = resolve_path(path)
    if not cache_path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in cache_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except Exception:
            continue
        if isinstance(item, dict) and item.get("email"):
            records.append(item)
    return records


def upsert_session_cache(record: dict[str, Any], path: str | Path = CACHE_PATH) -> Path:
    email = str(record.get("email") or "").strip().lower()
    if not email:
        raise RuntimeError("session 缓存缺少邮箱")
    if not record.get("access_token"):
        raise RuntimeError("session 缓存缺少 access_token")
    cache_path = resolve_path(path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    records = [item for item in load_session_cache(cache_path) if str(item.get("email") or "").strip().lower() != email]
    existing = find_cache_record(email, records)
    if existing and existing.get("created_at") and not record.get("created_at"):
        record["created_at"] = existing["created_at"]
    record["updated_at"] = utc_now()
    records.append(record)
    records.sort(key=lambda item: str(item.get("email") or "").lower())
    text = "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in records)
    cache_path.write_text(text + ("\n" if text else ""), encoding="utf-8")
    return cache_path


def write_session_cache(records: list[dict[str, Any]], path: str | Path = CACHE_PATH) -> Path:
    cache_path = resolve_path(path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    records.sort(key=lambda item: str(item.get("email") or "").lower())
    text = "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in records)
    cache_path.write_text(text + ("\n" if text else ""), encoding="utf-8")
    return cache_path


def remove_session_cache_records(emails: set[str], path: str | Path = CACHE_PATH) -> int:
    targets = {str(email or "").strip().lower() for email in emails if str(email or "").strip()}
    if not targets:
        return 0
    records = load_session_cache(path)
    remaining = [
        record
        for record in records
        if str(record.get("email") or "").strip().lower() not in targets
    ]
    removed = len(records) - len(remaining)
    if removed:
        write_session_cache(remaining, path)
    return removed


def find_cache_record(email: str, records: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    needle = str(email or "").strip().lower()
    if not needle:
        return None
    for item in records if records is not None else load_session_cache():
        if str(item.get("email") or "").strip().lower() == needle:
            return item
    return None


def read_paid_records(path: str | Path = output_file("flow2_paid_success")) -> list[dict[str, str]]:
    input_path = migrate_output_file(path, LEGACY_OUTPUT_FILES["flow2_paid_success"])
    if not input_path.exists():
        return []
    records: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in input_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or "----" not in line:
            continue
        parts = [part.strip() for part in line.split("----", 3)]
        account = parts[0] if parts else ""
        if not re.fullmatch(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", account):
            continue
        if account.lower() in seen:
            continue
        seen.add(account.lower())
        record: dict[str, str] = {"account": account, "raw": line}
        if len(parts) == 4:
            record.update(
                {
                    "password": parts[1],
                    "client_id": parts[2],
                    "refresh_token": parts[3],
                    "code_address": account,
                    "source_format": "hotmail_graph",
                }
            )
        elif len(parts) >= 2:
            source_format = "code_address"
            if account.lower().endswith("@icloud.com"):
                source_format = "icloud_query"
            elif account.lower().endswith(("@qq.com", "@foxmail.com", "@vip.qq.com")):
                source_format = "qq_imap"
            record.update({"code_address": parts[1], "source_format": source_format})
        records.append(record)
    return records


def write_paid_records(records: list[dict[str, str]], path: str | Path = output_file("flow2_paid_success")) -> Path:
    output_path = migrate_output_file(path, LEGACY_OUTPUT_FILES["flow2_paid_success"])
    lines: list[str] = []
    for record in records:
        account = str(record.get("account") or "").strip()
        if not account:
            continue
        if record.get("password") and record.get("client_id") and record.get("refresh_token"):
            lines.append(f"{account}----{record['password']}----{record['client_id']}----{record['refresh_token']}")
        elif record.get("code_address"):
            lines.append(f"{account}----{record['code_address']}")
    output_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return output_path


def build_synthetic_id_token(record: dict[str, Any]) -> str:
    now = int(time.time())
    exp = int(epoch_from_value(record.get("expires")) or (now + 3600))
    auth_info = {"chatgpt_account_id": record.get("account_id", "")}
    if record.get("plan_type"):
        auth_info["chatgpt_plan_type"] = record.get("plan_type", "")
    if record.get("user_id"):
        auth_info["chatgpt_user_id"] = record.get("user_id", "")
        auth_info["user_id"] = record.get("user_id", "")
    payload = {
        "iat": now,
        "exp": exp,
        "email": record.get("email", ""),
        "https://api.openai.com/auth": auth_info,
    }
    header = {"alg": "none", "typ": "JWT", "cpa_synthetic": True}
    return f"{base64_json(header)}.{base64_json(payload)}."


def base64_json(value: dict[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def auth_file_payload(record: dict[str, Any]) -> dict[str, Any]:
    expired = iso_from_epoch(record.get("expires"))
    return {
        "access_token": record.get("access_token", ""),
        "account_id": record.get("account_id", ""),
        "disabled": False,
        "email": record.get("email", ""),
        "expired": expired,
        "id_token": build_synthetic_id_token(record),
        "last_refresh": utc_now(),
        "refresh_token": "",
        "type": "codex",
        "websockets": False,
        "id_token_synthetic": True,
        "source": "chatgpt_web_session",
        "session_token": record.get("session_token", ""),
    }


def account_session_line(record: dict[str, Any]) -> str:
    email = str(record.get("email") or "").strip()
    update_code = str(record.get("code_address") or record.get("mail_url") or "").strip()
    if not update_code:
        update_code = str(record.get("session_token") or "").strip()
    return f"{email}----{update_code}"


def server_upload_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = auth_file_payload(record)
    payload.update(
        {
            "account_type": "session_export",
            "source": "session_export",
            "plan_type": record.get("plan_type", ""),
            "code_address": record.get("code_address", ""),
            "mail_source": record.get("mail_source", ""),
            "source_format": record.get("source_format", ""),
        }
    )
    return payload


def upload_session_to_server(record: dict[str, Any], env: dict[str, str] | None = None) -> bool:
    env = env or read_env(".env")
    if not session_export_upload_enabled(env):
        return False
    base_url = (env.get("AUTH_SERVER_URL") or "").strip().rstrip("/")
    api_key = (env.get("AUTH_SERVER_API_KEY") or "").strip()
    if not base_url or not api_key:
        log("流程四服务器上传已开启，但 AUTH_SERVER_URL/AUTH_SERVER_API_KEY 未配置")
        return False
    try:
        import requests

        resp = requests.post(
            f"{base_url}/api/accounts/upsert",
            json=server_upload_payload(record),
            headers={"X-API-Key": api_key},
            timeout=15,
        )
        if resp.status_code not in {200, 201}:
            log(f"流程四服务器上传失败: HTTP {resp.status_code} {resp.text[:200]}")
            return False
        log(f"流程四已上传服务器: {record.get('email')}")
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"流程四服务器上传异常，不影响本地导出: {exc}")
        return False


def sub2api_account_payload(record: dict[str, Any]) -> dict[str, Any]:
    expired_epoch = epoch_from_value(record.get("expires"))
    expires_in = max(0, int(round(expired_epoch - time.time()))) if expired_epoch else 0
    return {
        "name": record.get("email") or record.get("account_id") or "unknown",
        "platform": "openai",
        "type": "oauth",
        "credentials": {
            "_token_version": int(time.time() * 1000),
            "access_token": record.get("access_token", ""),
            "chatgpt_account_id": record.get("account_id", ""),
            "chatgpt_user_id": record.get("user_id", ""),
            "email": record.get("email", ""),
            "expires_at": int(expired_epoch) if expired_epoch else 0,
            "expires_in": expires_in,
            "id_token": build_synthetic_id_token(record),
            "organization_id": "",
            "refresh_token": "",
        },
        "extra": {
            "email": record.get("email", ""),
        },
        "concurrency": 10,
        "priority": 1,
        "rate_multiplier": 1,
        "auto_pause_on_expired": True,
    }


def load_sub_store(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"accounts": [], "exported_at": utc_now(), "proxies": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"accounts": [], "exported_at": utc_now(), "proxies": []}
    if isinstance(data, dict) and isinstance(data.get("accounts"), list):
        if "proxies" not in data or not isinstance(data.get("proxies"), list):
            data["proxies"] = []
        if "exported_at" not in data:
            data["exported_at"] = data.get("updated_at") or utc_now()
        data.pop("version", None)
        data.pop("updated_at", None)
        return data
    return {"accounts": [], "exported_at": utc_now(), "proxies": []}


def single_sub_store(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "accounts": [sub2api_account_payload(record)],
        "exported_at": utc_now(),
        "proxies": [],
    }


def single_sub_output_dir(output_root: str | Path) -> Path:
    return resolve_path(output_root) / "sub2api_session"


def write_single_sub_output(directory: Path, record: dict[str, Any]) -> Path:
    identifier = safe_filename(str(record.get("email") or record.get("account_id") or "unknown"))
    output_path = directory / f"{identifier}.json"
    directory.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(single_sub_store(record), ensure_ascii=False, indent=2), encoding="utf-8")
    return output_path


def upsert_sub_output(path: Path, record: dict[str, Any]) -> None:
    data = load_sub_store(path)
    email = str(record.get("email") or "").strip().lower()
    account_id = str(record.get("account_id") or "").strip().lower()
    accounts = []
    for item in data.get("accounts", []):
        credentials = item.get("credentials", {}) if isinstance(item, dict) else {}
        item_email = str(item.get("name") or item.get("extra", {}).get("email") or credentials.get("email") or "").strip().lower()
        item_id = str(credentials.get("chatgpt_account_id") or "").strip().lower()
        if (email and item_email == email) or (account_id and item_id == account_id):
            continue
        accounts.append(item)
    accounts.append(sub2api_account_payload(record))
    accounts.sort(key=lambda item: str(item.get("name") or item.get("credentials", {}).get("email") or "").lower())
    data.pop("version", None)
    data.pop("updated_at", None)
    data["exported_at"] = utc_now()
    data["proxies"] = data.get("proxies") if isinstance(data.get("proxies"), list) else []
    data["accounts"] = accounts
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_session_outputs(record: dict[str, Any], output_root: str | Path = EXPORT_ROOT) -> dict[str, Path]:
    root = resolve_path(output_root)
    token_dir = root / "tokens"
    token_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{stamp}_{safe_filename(str(record.get('email') or 'unknown'))}_{safe_filename(str(record.get('plan_type') or 'session'))}.json"
    token_path = token_dir / filename
    token_path.write_text(json.dumps(auth_file_payload(record), ensure_ascii=False, indent=2), encoding="utf-8")

    account_session_path = root / "account-session.txt"
    existing = account_session_path.read_text(encoding="utf-8") if account_session_path.exists() else ""
    email = str(record.get("email") or "").strip()
    lines = [line for line in existing.splitlines() if not line.lower().startswith(f"{email.lower()}----")]
    lines.append(account_session_line(record))
    account_session_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    sub_path = root / "session_sub2api_accounts.json"
    upsert_sub_output(sub_path, record)
    sub_single_path = write_single_sub_output(single_sub_output_dir(root), record)
    return {"token": token_path, "account_session": account_session_path, "sub": sub_path, "sub_single": sub_single_path}


def export_paid_sessions(
    *,
    paid_file: str | Path = output_file("flow2_paid_success"),
    cache_path: str | Path = CACHE_PATH,
    output_root: str | Path = EXPORT_ROOT,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    paid_records = read_paid_records(paid_file)
    cache_records = load_session_cache(cache_path)
    env_values = env if env is not None else read_env(".env")
    upload_enabled = session_export_upload_enabled(env_values)
    success_emails: set[str] = set()
    skipped: list[dict[str, str]] = []
    outputs: list[dict[str, str]] = []
    uploaded = 0
    upload_failed = 0
    cache_removed = 0

    for paid in paid_records:
        email = paid["account"]
        cached = find_cache_record(email, cache_records)
        if not cached:
            skipped.append({"email": email, "reason": "未找到流程一 session 缓存"})
            continue
        if not cached.get("access_token"):
            skipped.append({"email": email, "reason": "缓存缺少 accessToken"})
            continue
        if token_expired(str(cached.get("access_token") or ""), cached.get("expires")):
            skipped.append({"email": email, "reason": "accessToken 已过期"})
            continue
        paths = write_session_outputs(cached, output_root)
        if upload_enabled:
            if upload_session_to_server(cached, env_values):
                uploaded += 1
            else:
                upload_failed += 1
        success_emails.add(email.lower())
        outputs.append({"email": email, **{key: str(value) for key, value in paths.items()}})

    if success_emails:
        remaining = [record for record in paid_records if record["account"].lower() not in success_emails]
        write_paid_records(remaining, paid_file)
        cache_removed = remove_session_cache_records(success_emails, cache_path)

    return {
        "total": len(paid_records),
        "success": len(outputs),
        "skipped": skipped,
        "outputs": outputs,
        "output_root": str(resolve_path(output_root)),
        "removed": len(success_emails),
        "upload_enabled": upload_enabled,
        "uploaded": uploaded,
        "upload_failed": upload_failed,
        "cache_removed": cache_removed,
    }


def interactive_session_export() -> int:
    print()
    print("流程四：Session 本地导出")
    result = export_paid_sessions()
    upload_text = "关闭"
    if result.get("upload_enabled"):
        upload_text = f"开启，成功={result.get('uploaded', 0)}，失败={result.get('upload_failed', 0)}"
    log(
        f"Session 导出完成: 成功={result['success']}/{result['total']}，"
        f"跳过={len(result['skipped'])}，已清理缓存={result.get('cache_removed', 0)}，服务器上传={upload_text}"
    )
    if result["outputs"]:
        last = result["outputs"][-1]
        log(f"已写入本地 JSON: {last['token']}")
        log(f"已更新聚合文件: {last['sub']}")
        if last.get("sub_single"):
            log(f"已写入单账号目录: {Path(last['sub_single']).parent}")
    for item in result["skipped"][:20]:
        log(f"[{item['email']}] 跳过: {item['reason']}")
    if len(result["skipped"]) > 20:
        log(f"还有 {len(result['skipped']) - 20} 个跳过账号未展示，详见待处理池和缓存文件")
    return 0 if result["success"] or result["total"] == 0 else 1
