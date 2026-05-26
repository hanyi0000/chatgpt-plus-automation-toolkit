from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path


def _ensure_tk_library_paths() -> None:
    """Some portable Python installs do not auto-discover Tcl/Tk on Windows."""
    if os.name != "nt":
        return
    candidates = [
        Path(getattr(sys, "_MEIPASS", "")) / "tcl",
        Path(sys.base_prefix) / "tcl",
        Path(sys.prefix) / "tcl",
        Path(sys.executable).resolve().parent / "tcl",
        Path(sys.executable).resolve().parent / "_internal" / "tcl",
    ]
    for base in candidates:
        tcl_dir = base / "tcl8.6"
        tk_dir = base / "tk8.6"
        if (tcl_dir / "init.tcl").exists() and (tk_dir / "tk.tcl").exists():
            os.environ.setdefault("TCL_LIBRARY", str(tcl_dir))
            os.environ.setdefault("TK_LIBRARY", str(tk_dir))
            return


_ensure_tk_library_paths()

from tkinter import BOTH, END, LEFT, RIGHT, X, Y, filedialog, messagebox, ttk
import tkinter as tk

import panel_runner
from control_panel.env_service import get_known_env_fields, read_env, update_env
from control_panel.file_registry import PanelFile, get_panel_files
from control_panel.text_pool_service import clear_file, dedupe_lines, export_txt, import_txt, read_text, save_text


EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi_for_display(text: str) -> str:
    return ANSI_RE.sub("", text)


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


class ResourcePage(ttk.Frame):
    def __init__(self, master: tk.Widget, panel_file: PanelFile):
        super().__init__(master)
        self.panel_file = panel_file
        self._build()
        self.refresh()

    def _build(self) -> None:
        header = ttk.Frame(self)
        header.pack(fill=X, padx=10, pady=(10, 4))
        ttk.Label(header, text=self.panel_file.label, font=("Microsoft YaHei UI", 12, "bold")).pack(side=LEFT)
        ttk.Label(header, text=str(self.panel_file.path), foreground="#555").pack(side=LEFT, padx=(14, 0))

        toolbar = ttk.Frame(self)
        toolbar.pack(fill=X, padx=10, pady=(0, 8))
        ttk.Button(toolbar, text="刷新", command=self.refresh).pack(side=LEFT, padx=2)
        ttk.Button(toolbar, text="保存", command=self.save).pack(side=LEFT, padx=2)
        ttk.Button(toolbar, text="导入 TXT", command=self.import_file).pack(side=LEFT, padx=2)
        ttk.Button(toolbar, text="导出 TXT", command=self.export_file).pack(side=LEFT, padx=2)
        ttk.Button(toolbar, text="去重", command=self.dedupe).pack(side=LEFT, padx=2)
        ttk.Button(toolbar, text="清空", command=self.clear).pack(side=LEFT, padx=2)

        text_frame = ttk.Frame(self)
        text_frame.pack(fill=BOTH, expand=True, padx=10, pady=(0, 10))
        self.text = tk.Text(text_frame, wrap="none", undo=True)
        y_scroll = ttk.Scrollbar(text_frame, orient="vertical", command=self.text.yview)
        x_scroll = ttk.Scrollbar(text_frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)

    def refresh(self) -> None:
        self.text.delete("1.0", END)
        self.text.insert("1.0", read_text(self.panel_file.path))

    def save(self) -> None:
        save_text(self.panel_file.path, self.text.get("1.0", END).rstrip("\n") + "\n")
        messagebox.showinfo("保存成功", f"已保存到:\n{self.panel_file.path}")

    def import_file(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        content = Path(path).read_text(encoding="utf-8-sig")
        added = import_txt(self.panel_file.path, content, append=True)
        self.refresh()
        messagebox.showinfo("导入完成", f"已导入 {added} 行")

    def export_file(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".txt", filetypes=[("Text files", "*.txt")])
        if not path:
            return
        Path(path).write_text(export_txt(self.panel_file.path), encoding="utf-8")
        messagebox.showinfo("导出完成", f"已导出到:\n{path}")

    def dedupe(self) -> None:
        removed = dedupe_lines(self.panel_file.path)
        self.refresh()
        messagebox.showinfo("去重完成", f"已移除 {removed} 行重复内容")

    def clear(self) -> None:
        if not messagebox.askyesno("确认清空", f"确定清空 {self.panel_file.label} 吗？"):
            return
        clear_file(self.panel_file.path)
        self.refresh()


class EnvPage(ttk.Frame):
    def __init__(self, master: tk.Widget, env_path: Path):
        super().__init__(master)
        self.env_path = env_path
        self.vars: dict[str, tk.StringVar] = {}
        self._build()
        self.refresh()

    def _build(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill=X, padx=10, pady=10)
        ttk.Label(toolbar, text=".env 常用配置", font=("Microsoft YaHei UI", 12, "bold")).pack(side=LEFT)
        ttk.Label(toolbar, text=str(self.env_path), foreground="#555").pack(side=LEFT, padx=(14, 0))
        ttk.Button(toolbar, text="保存", command=self.save).pack(side=RIGHT, padx=2)
        ttk.Button(toolbar, text="刷新", command=self.refresh).pack(side=RIGHT, padx=2)

        canvas = tk.Canvas(self, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self.form = ttk.Frame(canvas)
        self.form.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.form, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=LEFT, fill=BOTH, expand=True, padx=(10, 0), pady=(0, 10))
        scrollbar.pack(side=RIGHT, fill=Y, padx=(0, 10), pady=(0, 10))

        for row, key in enumerate(get_known_env_fields()):
            ttk.Label(self.form, text=key, width=36).grid(row=row, column=0, sticky="w", padx=6, pady=3)
            var = tk.StringVar()
            self.vars[key] = var
            ttk.Entry(self.form, textvariable=var, width=88, show="").grid(row=row, column=1, sticky="ew", padx=6, pady=3)
        self.form.columnconfigure(1, weight=1)

    def refresh(self) -> None:
        values = read_env(self.env_path)
        for key, var in self.vars.items():
            var.set(values.get(key, ""))

    def save(self) -> None:
        update_env(self.env_path, {key: var.get() for key, var in self.vars.items()})
        messagebox.showinfo("保存成功", f"已更新:\n{self.env_path}")


class RunPage(ttk.Frame):
    def __init__(self, master: tk.Widget, root_path: Path):
        super().__init__(master)
        self.root_path = root_path
        self.process: subprocess.Popen[str] | None = None
        self.queue: queue.Queue[str] = queue.Queue()
        self.success_count = 0
        self.failure_count = 0
        self._build()
        self.after(150, self._drain_queue)

    def _build(self) -> None:
        controls = ttk.LabelFrame(self, text="流程控制")
        controls.pack(fill=X, padx=10, pady=10)

        input_row = ttk.Frame(controls)
        input_row.pack(fill=X, padx=8, pady=(6, 3))
        button_row_1 = ttk.Frame(controls)
        button_row_1.pack(fill=X, padx=8, pady=(3, 2))
        button_row_2 = ttk.Frame(controls)
        button_row_2.pack(fill=X, padx=8, pady=(2, 6))

        ttk.Label(input_row, text="数量").pack(side=LEFT, padx=(0, 2))
        self.count_var = tk.StringVar(value="1")
        ttk.Entry(input_row, textvariable=self.count_var, width=8).pack(side=LEFT, padx=4)
        ttk.Label(input_row, text="并发").pack(side=LEFT, padx=(10, 2))
        self.workers_var = tk.StringVar(value="1")
        ttk.Entry(input_row, textvariable=self.workers_var, width=8).pack(side=LEFT, padx=4)
        ttk.Label(input_row, text="邮箱源").pack(side=LEFT, padx=(10, 2))
        self.mail_source_var = tk.StringVar(value="hotmail")
        self.mail_source_combo = ttk.Combobox(
            input_row,
            textvariable=self.mail_source_var,
            values=("default", "hotmail", "moemail", "icloud", "qq"),
            width=10,
            state="readonly",
        )
        self.mail_source_combo.pack(side=LEFT, padx=4)
        ttk.Label(input_row, text="指定邮箱").pack(side=LEFT, padx=(10, 2))
        self.email_var = tk.StringVar(value="")
        ttk.Entry(input_row, textvariable=self.email_var, width=36).pack(side=LEFT, padx=4)
        ttk.Button(button_row_1, text="流程1 生成长链接", command=lambda: self.start("paypal-flow1")).pack(side=LEFT, padx=4)
        ttk.Button(button_row_1, text="流程2 真实卡PayPal", command=lambda: self.start("paypal-flow2")).pack(side=LEFT, padx=4)
        ttk.Button(button_row_1, text="流程2 无卡PayPal", command=lambda: self.start("paypal-flow2-nocard")).pack(side=LEFT, padx=4)
        ttk.Button(button_row_1, text="流程2 Filler脚本", command=lambda: self.start("paypal-flow2-filler")).pack(side=LEFT, padx=4)
        ttk.Button(button_row_1, text="流程3 授权落盘", command=lambda: self.start("paypal-flow3")).pack(side=LEFT, padx=4)
        ttk.Button(button_row_1, text="流程3 Session快捷导出", command=lambda: self.start("paypal-flow3-session")).pack(side=LEFT, padx=4)

        ttk.Button(button_row_2, text="全自动 真实卡", command=lambda: self.start("paypal-auto")).pack(side=LEFT, padx=4)
        ttk.Button(button_row_2, text="全自动 无卡", command=lambda: self.start("paypal-auto-nocard")).pack(side=LEFT, padx=4)
        ttk.Button(button_row_2, text="全自动 Filler脚本", command=lambda: self.start("paypal-auto-filler")).pack(side=LEFT, padx=4)
        ttk.Button(button_row_2, text="全自动真实卡Session", command=lambda: self.start("paypal-auto-session")).pack(side=LEFT, padx=4)
        ttk.Button(button_row_2, text="全自动无卡Session", command=lambda: self.start("paypal-auto-nocard-session")).pack(side=LEFT, padx=4)
        ttk.Button(button_row_2, text="停止任务", command=self.stop).pack(side=RIGHT, padx=8)

        self.summary_var = tk.StringVar(value="成功 0 / 失败 0")
        ttk.Label(self, textvariable=self.summary_var).pack(fill=X, padx=10)

        result_frame = ttk.LabelFrame(self, text="结果输出")
        result_frame.pack(fill=BOTH, expand=True, padx=10, pady=(6, 4))
        columns = ("time", "flow", "account", "status", "message", "path")
        self.results = ttk.Treeview(result_frame, columns=columns, show="headings", height=7)
        headings = {"time": "时间", "flow": "流程", "account": "账号/邮箱", "status": "状态", "message": "结果", "path": "输出路径"}
        widths = {"time": 110, "flow": 110, "account": 210, "status": 70, "message": 360, "path": 280}
        for col in columns:
            self.results.heading(col, text=headings[col])
            self.results.column(col, width=widths[col], anchor="w")
        self.results.pack(fill=BOTH, expand=True, padx=4, pady=4)
        ttk.Button(result_frame, text="清空结果", command=self.clear_results).pack(anchor="e", padx=4, pady=(0, 4))

        log_frame = ttk.LabelFrame(self, text="实时日志")
        log_frame.pack(fill=BOTH, expand=True, padx=10, pady=(4, 10))
        self.log_text = tk.Text(log_frame, height=12, wrap="word")
        self.log_text.pack(fill=BOTH, expand=True, padx=4, pady=4)

    def _runner_command(self, action: str) -> list[str]:
        count = self.count_var.get().strip() or "1"
        workers = self.workers_var.get().strip() or "1"
        base_args = ["--runner", action, "--count", count, "--workers", workers]
        mail_source = self.mail_source_var.get().strip()
        if mail_source and mail_source != "default":
            base_args.extend(["--mail-source", mail_source])
        selected_email = self.email_var.get().strip()
        if selected_email:
            base_args.extend(["--email", selected_email])
        if getattr(sys, "frozen", False):
            return [sys.executable, *base_args]
        return [sys.executable, str(Path(__file__).resolve()), *base_args]

    def start(self, action: str) -> None:
        if self.process and self.process.poll() is None:
            messagebox.showwarning("任务运行中", "请先停止当前任务")
            return
        cmd = self._runner_command(action)
        self._append_log(f"> {' '.join(cmd)}\n")
        self.process = subprocess.Popen(
            cmd,
            cwd=str(self.root_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env={
                **os.environ,
                "CODEX_COLOR_LOGS": "0",
                "NO_COLOR": "1",
                "PYTHONIOENCODING": "utf-8",
            },
        )
        threading.Thread(target=self._reader_thread, daemon=True).start()

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self._append_log("[panel] 已请求停止任务\n")

    def clear_results(self) -> None:
        for item in self.results.get_children():
            self.results.delete(item)
        self.success_count = 0
        self.failure_count = 0
        self._update_summary()

    def _reader_thread(self) -> None:
        assert self.process and self.process.stdout
        for line in self.process.stdout:
            self.queue.put(line)
        code = self.process.wait()
        self.queue.put(f"[panel] 子进程退出，code={code}\n")

    def _drain_queue(self) -> None:
        while True:
            try:
                line = self.queue.get_nowait()
            except queue.Empty:
                break
            clean_line = strip_ansi_for_display(line)
            self._append_log(clean_line)
            self._parse_result_line(clean_line)
        self.after(150, self._drain_queue)

    def _append_log(self, text: str) -> None:
        self.log_text.insert(END, text)
        self.log_text.see(END)

    def _parse_result_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and payload.get("type") == "result":
            self._add_result(
                flow=str(payload.get("flow") or ""),
                account=str(payload.get("account") or ""),
                status=str(payload.get("status") or ""),
                message=str(payload.get("message") or ""),
                path=str(payload.get("path") or ""),
            )
            return

        failure_markers = ("[FAIL]", "失败", "failed:", "RuntimeError", "Traceback")
        success_markers = ("[OK]", "成功", "link ok", "支付成功", "授权成功")
        if any(marker in stripped for marker in failure_markers):
            self._add_result("", self._extract_email(stripped), "failure", stripped[:360], "")
        elif any(marker in stripped for marker in success_markers):
            self._add_result("", self._extract_email(stripped), "success", stripped[:360], "")

    def _extract_email(self, text: str) -> str:
        match = EMAIL_RE.search(text)
        return match.group(0) if match else ""

    def _add_result(self, flow: str, account: str, status: str, message: str, path: str) -> None:
        normalized = status.lower()
        if normalized == "success":
            self.success_count += 1
        elif normalized == "failure":
            self.failure_count += 1
        now = datetime.now().strftime("%H:%M:%S")
        self.results.insert("", END, values=(now, flow, account, status, message, path))
        self._update_summary()

    def _update_summary(self) -> None:
        self.summary_var.set(f"成功 {self.success_count} / 失败 {self.failure_count}")


class ControlPanelApp(tk.Tk):
    def __init__(self, root_path: Path):
        super().__init__()
        self.root_path = root_path
        self.title("ChatGPT Assistant 控制面板 - 作者：hanyiz2")
        self.geometry("1220x780")
        self.minsize(1040, 680)
        self.files = get_panel_files(root_path)
        self.pages: dict[str, ttk.Frame] = {}
        self._configure_style()
        self._build()

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Nav.TButton", anchor="w", padding=(10, 7))

    def _build(self) -> None:
        main = ttk.Frame(self)
        main.pack(fill=BOTH, expand=True)
        nav = ttk.Frame(main, width=220)
        nav.pack(side=LEFT, fill=Y, padx=(8, 4), pady=8)
        content = ttk.Frame(main)
        content.pack(side=RIGHT, fill=BOTH, expand=True, padx=(4, 8), pady=8)
        self.content = content

        ttk.Label(nav, text="ChatGPT Assistant", font=("Microsoft YaHei UI", 12, "bold")).pack(fill=X, padx=8, pady=(4, 8))
        ttk.Label(nav, text="作者：hanyiz2", foreground="#666").pack(fill=X, padx=8, pady=(0, 10))
        self._add_nav(nav, "运行流程", lambda: self.show_page("run"))
        self._add_nav(nav, "API Key / 常用配置", lambda: self.show_page("env"))
        resource_groups = [
            ("代理池", ["proxy_default", "proxy_jp", "proxy_us"]),
            ("卡密池", ["paypal_card_codes", "paypal_card_codes_used", "paypal_card_codes_failed"]),
            ("虚拟卡池", ["paypal_cards"]),
            ("手机号池", ["paypal_phones"]),
            ("邮箱池", ["hotmail_accounts", "hotmail_mail_pool", "icloud_accounts", "icloud_mail_pool", "qq_accounts", "qq_mail_pool", "mail_accounts", "mail_pool"]),
            ("长链接池", ["paypal_links"]),
            ("授权账号/输出", ["paypal_pending_auth", "paypal_authorized_rt", "paypal_authorized_sub"]),
        ]
        for title, keys in resource_groups:
            self._add_nav(nav, title, lambda keys=keys, title=title: self.show_resource_group(title, keys))

        self.pages["run"] = RunPage(content, self.root_path)
        self.pages["env"] = EnvPage(content, self.root_path / ".env")
        self.show_page("run")

    def _add_nav(self, nav: ttk.Frame, text: str, command) -> None:
        ttk.Button(nav, text=text, command=command, style="Nav.TButton").pack(fill=X, padx=6, pady=3)

    def _clear_content(self) -> None:
        for child in self.content.winfo_children():
            child.pack_forget()

    def show_page(self, key: str) -> None:
        self._clear_content()
        self.pages[key].pack(fill=BOTH, expand=True)

    def show_resource_group(self, title: str, keys: list[str]) -> None:
        page_key = "group:" + ",".join(keys)
        if page_key not in self.pages:
            frame = ttk.Frame(self.content)
            tabs = ttk.Notebook(frame)
            tabs.pack(fill=BOTH, expand=True)
            for key in keys:
                panel_file = self.files[key]
                tabs.add(ResourcePage(tabs, panel_file), text=panel_file.label)
            self.pages[page_key] = frame
        self._clear_content()
        self.pages[page_key].pack(fill=BOTH, expand=True)


def main() -> int:
    root_path = app_root()
    app = ControlPanelApp(root_path)
    app.mainloop()
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--runner":
        raise SystemExit(panel_runner.main(sys.argv[2:]))
    raise SystemExit(main())
