from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Sequence

# The control panel captures subprocess text in a GUI widget, not an ANSI terminal.
os.environ.setdefault("CODEX_COLOR_LOGS", "0")
os.environ.setdefault("NO_COLOR", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def install_runtime_project_root() -> None:
    import modules.utils as module_utils

    module_utils.PROJECT_ROOT = runtime_root()


install_runtime_project_root()

from main import apply_env_config
from main import configure_mail_source
from modules.paypal_filler_bridge import run_paypal_filler_flow2
from modules.paypal_flow import _run_paypal_authorize, _run_paypal_session_export
from modules.paypal_pay import PAYPAL_OUTPUT_ROOT, run_paypal_pay
from modules.paypal_register import run_paypal_register
from modules.utils import load_config


VALID_ACTIONS = (
    "paypal-flow1",
    "paypal-flow2",
    "paypal-flow2-nocard",
    "paypal-flow2-filler",
    "paypal-flow3",
    "paypal-flow3-session",
    "paypal-auto",
    "paypal-auto-nocard",
    "paypal-auto-filler",
    "paypal-auto-session",
    "paypal-auto-nocard-session",
    "oauth-login",
    "check-config",
)


def is_ignorable_playwright_future_noise(context: dict) -> bool:
    message = str(context.get("message") or "")
    exception_text = str(context.get("exception") or "")
    combined = f"{message}\n{exception_text}"
    if "Future exception was never retrieved" not in combined:
        return False
    return any(
        marker in combined
        for marker in (
            "Execution context was destroyed",
            "Target page, context or browser has been closed",
        )
    )


def _playwright_noise_filter(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    if is_ignorable_playwright_future_noise(context):
        return
    loop.default_exception_handler(context)


async def run_with_playwright_noise_filter(awaitable):
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_playwright_noise_filter)
    return await awaitable


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Control panel workflow runner")
    parser.add_argument("action", choices=VALID_ACTIONS, help="Workflow action to run")
    parser.add_argument("--count", type=int, default=1, help="Target account count")
    parser.add_argument("--workers", type=int, default=1, help="Worker count")
    parser.add_argument("--config", default="config.yaml", help="Config file path")
    parser.add_argument(
        "--mail-source",
        choices=("default", "moemail", "hotmail", "hotmail_graph", "icloud", "icloud_query", "qq", "qq_imap"),
        default="default",
        help="Override flow mail source",
    )
    parser.add_argument("--email", default="", help="Only use this mailbox for flow1/paypal-auto")
    args, extra = parser.parse_known_args(argv)
    setattr(args, "extra_args", list(extra))
    return args


def result_event(flow: str, status: str, message: str, *, account: str = "", path: str = "") -> str:
    payload = {
        "type": "result",
        "flow": flow,
        "account": account,
        "status": status,
        "message": message,
        "path": path,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def resolve_config_path(config_path: str | Path) -> Path:
    path = Path(config_path)
    if path.is_absolute():
        return path
    return runtime_root() / path


def resolve_env_path(env_path: str | Path = ".env") -> Path:
    path = Path(env_path)
    if path.is_absolute():
        return path
    return runtime_root() / path


def flow_key_for_action(action: str) -> str:
    if action in {
        "paypal-flow1",
        "paypal-flow2",
        "paypal-flow2-nocard",
        "paypal-flow2-filler",
        "paypal-auto",
        "paypal-auto-nocard",
        "paypal-auto-filler",
        "paypal-auto-session",
        "paypal-auto-nocard-session",
        "check-config",
    }:
        return "flow1"
    if action in {"paypal-flow3", "paypal-flow3-session"}:
        return "flow3"
    return ""


def _load_panel_config(config_path: str, flow_key: str = "") -> dict:
    return apply_env_config(
        load_config(resolve_config_path(config_path)),
        env_path=str(resolve_env_path(".env")),
        flow_key=flow_key,
    )


async def _run_async_action(args: argparse.Namespace, cfg: dict) -> int:
    if args.action == "paypal-flow1":
        return await run_paypal_register(
            cfg,
            count=args.count,
            workers=args.workers,
            selected_email=args.email,
        )
    if args.action == "paypal-flow2":
        return await run_paypal_pay(cfg, count=args.count, workers=args.workers, card_source_mode="real")
    if args.action == "paypal-flow2-nocard":
        return await run_paypal_pay(cfg, count=args.count, workers=args.workers, card_source_mode="local_random")
    raise ValueError(f"Unsupported async action: {args.action}")


def run_action(args: argparse.Namespace) -> int:
    flow = args.action
    if flow == "oauth-login":
        from get_oauth_rt import main as oauth_main

        extra_args = list(getattr(args, "extra_args", []) or [])
        if not extra_args:
            print(result_event(flow, "failure", "oauth-login missing arguments"), flush=True)
            return 2
        old_argv = list(sys.argv)
        try:
            sys.argv = [str(runtime_root() / "get_oauth_rt.py"), *extra_args]
            return int(oauth_main() or 0)
        finally:
            sys.argv = old_argv

    cfg = _load_panel_config(args.config, flow_key_for_action(flow))
    if args.mail_source and args.mail_source != "default":
        configure_mail_source(cfg, args.mail_source)
    target = max(1, int(args.count or 1))
    workers = max(1, int(args.workers or 1))
    args.count = target
    args.workers = workers

    try:
        if flow == "check-config":
            mail_cfg = cfg.get("mail", {})
            source = mail_cfg.get("active_source") or mail_cfg.get("source") or ""
            accounts_file = mail_cfg.get("accounts_file") or ""
            print(
                result_event(
                    flow,
                    "success",
                    f"config ok: {resolve_config_path(args.config)} | source={source} | accounts={accounts_file}",
                ),
                flush=True,
            )
            return 0
        if flow in {"paypal-flow1", "paypal-flow2", "paypal-flow2-nocard"}:
            success = asyncio.run(run_with_playwright_noise_filter(_run_async_action(args, cfg)))
        elif flow == "paypal-flow2-filler":
            success = int(
                run_paypal_filler_flow2(
                    cfg,
                    count=target,
                    workers=workers,
                    selected_email=args.email,
                )
                or 0
            )
            if success <= 0:
                detail = f" for selected email {args.email}" if args.email else ""
                print(result_event(flow, "failure", f"filler flow2 produced no pending auth accounts{detail}"), flush=True)
                return 1
        elif flow == "paypal-flow3":
            auth_code = int(_run_paypal_authorize(count=target, workers=workers) or 0)
            if auth_code == 0:
                print(result_event(flow, "success", f"completed success={target}/{target}"), flush=True)
                return 0
            print(result_event(flow, "failure", f"flow3 failed code={auth_code}"), flush=True)
            return auth_code
        elif flow == "paypal-flow3-session":
            output_root = str(PAYPAL_OUTPUT_ROOT / "授权成功")
            export_code = int(_run_paypal_session_export(cfg) or 0)
            if export_code == 0:
                print(result_event(flow, "success", "session export completed", path=output_root), flush=True)
                return 0
            print(result_event(flow, "failure", f"flow3 session export failed code={export_code}", path=output_root), flush=True)
            return export_code
        elif flow in {"paypal-auto", "paypal-auto-nocard", "paypal-auto-filler", "paypal-auto-session", "paypal-auto-nocard-session"}:
            use_filler_flow2 = flow == "paypal-auto-filler"
            use_local_random_mode = flow in {"paypal-auto-nocard", "paypal-auto-nocard-session"}
            use_session_export = flow in {"paypal-auto-session", "paypal-auto-nocard-session"}
            reg_success = asyncio.run(
                run_with_playwright_noise_filter(
                    run_paypal_register(
                        cfg,
                        count=target,
                        workers=workers,
                        selected_email=args.email,
                    )
                )
            )
            if reg_success <= 0:
                detail = f" for selected email {args.email}" if args.email else ""
                print(result_event(flow, "failure", f"flow1 produced no payment links{detail}"), flush=True)
                return 1
            if use_filler_flow2:
                pay_success = int(
                    run_paypal_filler_flow2(
                        cfg,
                        count=reg_success,
                        workers=workers,
                        selected_email=args.email,
                    )
                )
            else:
                pay_mode = "local_random" if use_local_random_mode else "real"
                pay_success = asyncio.run(
                    run_with_playwright_noise_filter(
                        run_paypal_pay(cfg, count=reg_success, workers=workers, card_source_mode=pay_mode)
                    )
                )
            if pay_success <= 0:
                print(result_event(flow, "failure", "flow2 produced no pending auth accounts"), flush=True)
                return 1
            if use_session_export:
                export_code = int(_run_paypal_session_export(cfg) or 0)
                if export_code != 0:
                    print(result_event(flow, "failure", f"flow3 session export failed code={export_code}"), flush=True)
                    return export_code
            else:
                auth_code = int(_run_paypal_authorize(count=pay_success, workers=workers) or 0)
                if auth_code != 0:
                    print(result_event(flow, "failure", f"flow3 failed code={auth_code}"), flush=True)
                    return auth_code
            success = pay_success
        else:
            raise ValueError(f"Unsupported action: {flow}")
    except Exception as exc:
        print(result_event(flow, "failure", str(exc)), flush=True)
        raise

    status = "success" if int(success or 0) > 0 else "failure"
    message = f"completed success={success}/{target}"
    print(result_event(flow, status, message), flush=True)
    return 0 if status == "success" else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return run_action(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
