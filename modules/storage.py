from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import random
import re
import string
from threading import RLock

from .utils import LEGACY_OUTPUT_FILES, load_env, migrate_output_file, resolve_path


EXTERNAL_MAIL_FETCH_MODE_ENV = "MAIL_FETCH_SOURCE"
EXTERNAL_MAIL_FETCH_MODE_IMAP163 = {"desktop_imap163", "external_imap163", "imap163"}
EXTERNAL_IMAP163_DIR_ENV = "EXTERNAL_IMAP163_DIR"
QQ_MAIL_DOMAINS = {"qq.com", "foxmail.com", "vip.qq.com"}


@dataclass(frozen=True)
class MailAccount:
    email: str
    password: str | None = None
    client_id: str | None = None
    refresh_token: str | None = None
    mail_url: str | None = None
    raw: str = ""

    @property
    def code_address(self) -> str:
        return self.mail_url or self.email


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_external_mail_env() -> dict[str, str]:
    env = load_env(".env")
    ext_dir = (env.get(EXTERNAL_IMAP163_DIR_ENV) or "").strip()
    if not ext_dir:
        return env
    ext_env_path = Path(ext_dir) / ".env"
    if ext_env_path.exists():
        ext_env = load_env(ext_env_path)
        for key, value in ext_env.items():
            env.setdefault(key, value)
    return env


def _external_imap163_domain() -> str:
    env = _load_external_mail_env()
    mode = (env.get(EXTERNAL_MAIL_FETCH_MODE_ENV) or "").strip().lower()
    if mode not in EXTERNAL_MAIL_FETCH_MODE_IMAP163:
        return ""
    domain = (env.get("IMAP163_FORWARD_DOMAIN") or env.get("MAIL_DOMAIN") or "").strip().lower()
    return domain


def _generate_external_imap163_account() -> MailAccount | None:
    domain = _external_imap163_domain()
    if not domain:
        return None
    prefix = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    email = f"{prefix}@{domain}"
    raw = f"{email}----imap163"
    return MailAccount(email=email, mail_url="imap163", raw=raw)


def is_qq_mail_address(email: str) -> bool:
    domain = email.rsplit("@", 1)[-1].strip().lower() if "@" in email else ""
    return domain in QQ_MAIL_DOMAINS


def parse_mail_line(line: str) -> MailAccount | None:
    line = line.strip()
    email_match = re.search(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}", line)
    if not email_match:
        return None
    email = email_match.group(0)
    url_match = re.search(r"https?://\S+", line)
    mail_url = url_match.group(0).rstrip("，,。;；") if url_match else None
    parts = [part.strip() for part in line.split("----")]
    if len(parts) >= 4:
        return MailAccount(
            email=email,
            password=parts[1] or None,
            client_id=parts[2] or None,
            refresh_token=parts[3] or None,
            mail_url=mail_url,
            raw=line,
        )
    if len(parts) >= 3 and parts[2].startswith(("http://", "https://")):
        return MailAccount(email=email, password=parts[1] or None, mail_url=parts[2], raw=line)
    if mail_url:
        return MailAccount(email=email, mail_url=mail_url, raw=line)
    if len(parts) >= 2 and parts[1].startswith(("http://", "https://")):
        return MailAccount(email=email, mail_url=parts[1], raw=line)
    if len(parts) >= 2 and email.lower().endswith("@icloud.com") and parts[1]:
        return MailAccount(email=email, mail_url=parts[1], raw=line)
    if len(parts) >= 2 and is_qq_mail_address(email) and parts[1]:
        return MailAccount(email=email, mail_url=parts[1], raw=line)
    return MailAccount(email=email, mail_url=mail_url, raw=line)


class AccountStore:
    def __init__(
        self,
        accounts_file: str,
        raw_pool_file: str,
        success_file: str,
        failed_file: str,
        in_progress_file: str | None = None,
    ):
        self.accounts_file = resolve_path(accounts_file)
        self.raw_pool_file = resolve_path(raw_pool_file)
        self.success_file = migrate_output_file(success_file, LEGACY_OUTPUT_FILES["flow1_success"])
        self.failed_file = migrate_output_file(failed_file, LEGACY_OUTPUT_FILES["flow1_failed"])
        self.in_progress_file = migrate_output_file(
            in_progress_file or "output/gopay注册plus/流程1_注册处理中.txt",
            LEGACY_OUTPUT_FILES["flow1_in_progress"],
        )
        self._lock = RLock()
        for path in [self.accounts_file, self.raw_pool_file, self.success_file, self.failed_file, self.in_progress_file]:
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("", encoding="utf-8")

    def next_email(self) -> str | None:
        lines = _read_lines(self.accounts_file)
        if not lines:
            return None
        account = parse_mail_line(lines[0])
        return account.email if account else None

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for line in _read_lines(self.accounts_file) if parse_mail_line(line))

    def resolve_account(self, email: str) -> MailAccount:
        email_lower = email.lower()
        for line in _read_lines(self.raw_pool_file):
            account = parse_mail_line(line)
            if account and account.email.lower() == email_lower:
                return account
        for line in _read_lines(self.accounts_file):
            account = parse_mail_line(line)
            if account and account.email.lower() == email_lower:
                return account
        raise RuntimeError(f"未在 raw_pool_file/accounts_file 找到邮箱完整信息: {email}")

    def claim_next(self, worker_id: int | str = 1) -> MailAccount | None:
        with self._lock:
            lines = _read_lines(self.accounts_file)
            if not lines:
                generated = _generate_external_imap163_account()
                if not generated:
                    return None
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with self.in_progress_file.open("a", encoding="utf-8") as fh:
                    fh.write(f"{stamp}\tworker-{worker_id}\t{generated.email}\t{generated.raw}\n")
                return generated
            claimed_emails = {
                parts[2].strip().lower()
                for line in _read_lines(self.in_progress_file)
                for parts in [line.split("\t")]
                if len(parts) >= 3 and parts[2].strip()
            }

            selected_line: str | None = None
            selected_account: MailAccount | None = None
            for line in lines:
                account = parse_mail_line(line)
                if not account:
                    continue
                if account.email.lower() in claimed_emails:
                    continue
                if selected_account is None:
                    selected_line = line
                    selected_account = account
                    continue
            if not selected_account:
                return None

            try:
                full_account = self.resolve_account(selected_account.email)
            except RuntimeError:
                full_account = selected_account
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with self.in_progress_file.open("a", encoding="utf-8") as fh:
                fh.write(f"{stamp}\tworker-{worker_id}\t{full_account.email}\t{selected_line or full_account.raw}\n")
            return full_account

    def complete(self, email: str) -> None:
        with self._lock:
            lines = _read_lines(self.accounts_file)
            remaining = []
            for line in lines:
                account = parse_mail_line(line)
                if account and account.email.lower() == email.lower():
                    continue
                remaining.append(line)
            self.accounts_file.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")
            self.finish_claim(email)

    def finish_claim(self, email: str) -> None:
        with self._lock:
            lines = _read_lines(self.in_progress_file)
            remaining = []
            for line in lines:
                parts = line.split("\t")
                claimed_email = parts[2] if len(parts) >= 3 else line
                if claimed_email.lower() == email.lower():
                    continue
                remaining.append(line)
            self.in_progress_file.write_text("\n".join(remaining) + ("\n" if remaining else ""), encoding="utf-8")

    def return_to_pool(self, account: MailAccount) -> None:
        with self._lock:
            lines = _read_lines(self.accounts_file)
            exists = False
            for line in lines:
                item = parse_mail_line(line)
                if item and item.email.lower() == account.email.lower():
                    exists = True
                    break
            if not exists:
                original = account.raw or account.email
                lines.append(original)
                self.accounts_file.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            self.finish_claim(account.email)

    def save_success(self, email: str, code_address: str, payment_link: str) -> None:
        with self._lock:
            text = f"{email}----{code_address}----{payment_link}\n"
            with self.success_file.open("a", encoding="utf-8") as fh:
                fh.write(text)

    def save_failed(self, email: str, reason: str) -> None:
        with self._lock:
            existing = self.failed_file.read_text(encoding="utf-8") if self.failed_file.exists() else ""
            if email.lower() in existing.lower() and reason in existing:
                return
            with self.failed_file.open("a", encoding="utf-8") as fh:
                fh.write(f"{email}\t{reason}\n")
