import panel_runner


def test_default_config_resolves_next_to_frozen_exe(monkeypatch, tmp_path) -> None:
    exe = tmp_path / "ChatGPTAssistantPanel.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(panel_runner.sys, "frozen", True, raising=False)
    monkeypatch.setattr(panel_runner.sys, "executable", str(exe))

    assert panel_runner.resolve_config_path("config.yaml") == tmp_path / "config.yaml"


def test_default_env_resolves_next_to_frozen_exe(monkeypatch, tmp_path) -> None:
    exe = tmp_path / "ChatGPTAssistantPanel.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(panel_runner.sys, "frozen", True, raising=False)
    monkeypatch.setattr(panel_runner.sys, "executable", str(exe))

    assert panel_runner.resolve_env_path(".env") == tmp_path / ".env"


def test_flow_key_for_actions() -> None:
    assert panel_runner.flow_key_for_action("paypal-flow1") == "flow1"
    assert panel_runner.flow_key_for_action("paypal-auto") == "flow1"
    assert panel_runner.flow_key_for_action("paypal-flow2-nocard") == "flow1"
    assert panel_runner.flow_key_for_action("paypal-flow2-filler") == "flow1"
    assert panel_runner.flow_key_for_action("paypal-auto-nocard") == "flow1"
    assert panel_runner.flow_key_for_action("paypal-auto-filler") == "flow1"
    assert panel_runner.flow_key_for_action("paypal-flow3") == "flow3"


def test_parse_paypal_flow1_args() -> None:
    args = panel_runner.parse_args(["paypal-flow1", "--count", "2", "--workers", "1"])

    assert args.action == "paypal-flow1"
    assert args.count == 2
    assert args.workers == 1


def test_parse_mail_source_and_selected_email_args() -> None:
    args = panel_runner.parse_args(
        [
            "paypal-auto",
            "--mail-source",
            "hotmail",
            "--email",
            "User@Hotmail.com",
        ]
    )

    assert args.mail_source == "hotmail"
    assert args.email == "User@Hotmail.com"


def test_parse_qq_mail_source_args() -> None:
    args = panel_runner.parse_args(["paypal-flow1", "--mail-source", "qq"])

    assert args.mail_source == "qq"


def test_parse_all_supported_actions() -> None:
    for action in (
        "paypal-flow1",
        "paypal-flow2",
        "paypal-flow2-nocard",
        "paypal-flow2-filler",
        "paypal-flow3",
        "paypal-auto",
        "paypal-auto-nocard",
        "paypal-auto-filler",
        "check-config",
    ):
        args = panel_runner.parse_args([action])
        assert args.action == action
        assert args.count == 1
        assert args.workers == 1


def test_result_event_is_json_line() -> None:
    event = panel_runner.result_event("paypal-flow2", "success", "done", account="a@example.com", path="out.txt")

    assert '"type":"result"' in event
    assert '"flow":"paypal-flow2"' in event
    assert '"status":"success"' in event
    assert '"account":"a@example.com"' in event


def test_ignores_benign_playwright_navigation_future_noise() -> None:
    context = {
        "message": "Future exception was never retrieved",
        "exception": RuntimeError("Execution context was destroyed, most likely because of a navigation"),
    }

    assert panel_runner.is_ignorable_playwright_future_noise(context)


def test_does_not_ignore_general_asyncio_errors() -> None:
    context = {
        "message": "Future exception was never retrieved",
        "exception": RuntimeError("real failure"),
    }

    assert not panel_runner.is_ignorable_playwright_future_noise(context)


def test_paypal_flow3_treats_zero_auth_return_code_as_success(monkeypatch, capsys) -> None:
    args = panel_runner.parse_args(["paypal-flow3", "--count", "1", "--workers", "1"])
    monkeypatch.setattr(panel_runner, "_load_panel_config", lambda *args, **kwargs: {})
    monkeypatch.setattr(panel_runner, "_run_paypal_authorize", lambda **kwargs: 0)

    exit_code = panel_runner.run_action(args)

    assert exit_code == 0
    assert '"status":"success"' in capsys.readouterr().out


def test_paypal_flow3_treats_nonzero_auth_return_code_as_failure(monkeypatch, capsys) -> None:
    args = panel_runner.parse_args(["paypal-flow3", "--count", "1", "--workers", "1"])
    monkeypatch.setattr(panel_runner, "_load_panel_config", lambda *args, **kwargs: {})
    monkeypatch.setattr(panel_runner, "_run_paypal_authorize", lambda **kwargs: 1)

    exit_code = panel_runner.run_action(args)

    assert exit_code == 1
    output = capsys.readouterr().out
    assert '"status":"failure"' in output
    assert "flow3 failed code=1" in output
