from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.app_launcher import DesktopAppLauncher
from core.executor import CommandExecutor
from core.launcher import AppLauncher, LaunchResult


def _make_desktop_launcher() -> DesktopAppLauncher:
    legacy = AppLauncher()
    legacy.launch_by_name = MagicMock(
        return_value=LaunchResult(
            success=False,
            app_name="whatsapp",
            matched_name="",
            message="I couldn't find an installed app named whatsapp.",
            error="app_not_found",
        )
    )
    return DesktopAppLauncher(legacy)


@patch("core.app_launcher.glob.glob", side_effect=PermissionError("Access denied"))
@patch("core.app_launcher.shutil.which", return_value=None)
@patch("core.app_launcher.subprocess.run")
@patch("core.app_launcher.subprocess.Popen")
def test_desktop_launcher_launches_start_app_when_known_path_is_inaccessible(
    mock_popen,
    mock_run,
    _mock_which,
    _mock_glob,
):
    mock_popen.return_value.pid = 9999
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout='[{"Name":"WhatsApp","AppID":"5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"}]',
        stderr="",
    )
    launcher = _make_desktop_launcher()

    result = launcher.launch_app("whatsapp")

    assert result.success is True
    assert result.pid == -1
    assert result.verified is False
    assert result.data["launch_source"] == "start_apps"
    assert result.data["start_app_id"] == "5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"
    mock_popen.assert_called_once()


@patch("core.app_launcher.glob.glob", side_effect=PermissionError("Access denied"))
@patch("core.app_launcher.shutil.which", return_value=None)
@patch("core.app_launcher.subprocess.run")
def test_desktop_launcher_is_installed_uses_start_apps_catalog(
    mock_run,
    _mock_which,
    _mock_glob,
):
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout='[{"Name":"WhatsApp","AppID":"5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App"}]',
        stderr="",
    )
    launcher = _make_desktop_launcher()

    assert launcher.is_installed("whatsapp") is True


@patch("psutil.process_iter")
def test_command_executor_open_app_verifies_using_process_names(mock_process_iter):
    launcher = MagicMock()
    launcher.launch_app.return_value = LaunchResult(
        success=True,
        app_name="Visual Studio Code",
        matched_name="Visual Studio Code",
        path=r"C:\Users\rakes\AppData\Local\Programs\Microsoft VS Code\Code.exe",
        pid=1234,
        message="Opening Visual Studio Code.",
    )
    launcher.process_names_for.return_value = ("code.exe",)

    process = MagicMock()
    process.info = {
        "name": "Code.exe",
        "exe": r"C:\Users\rakes\AppData\Local\Programs\Microsoft VS Code\Code.exe",
    }
    mock_process_iter.return_value = [process]

    executor = CommandExecutor(
        launcher=launcher,
        window_controller=MagicMock(),
        system_controller=MagicMock(),
        browser_controller=MagicMock(),
        browser_skill=MagicMock(),
        file_skill=MagicMock(),
    )

    result = executor.execute("open_app", {"app": "vscode", "requested_app": "vscode"}, "open vscode")

    assert result["success"] is True
    assert result["data"]["verified"] is True


@patch("psutil.process_iter")
def test_command_executor_open_app_focuses_visible_window_after_launch(mock_process_iter):
    launcher = MagicMock()
    launcher.launch_app.return_value = LaunchResult(
        success=True,
        app_name="Visual Studio Code",
        matched_name="Visual Studio Code",
        path=r"C:\Users\rakes\AppData\Local\Programs\Microsoft VS Code\Code.exe",
        pid=1234,
        message="Opening Visual Studio Code.",
    )
    launcher.process_names_for.return_value = ("code.exe",)

    process = MagicMock()
    process.info = {
        "pid": 1234,
        "name": "Code.exe",
        "exe": r"C:\Users\rakes\AppData\Local\Programs\Microsoft VS Code\Code.exe",
    }
    mock_process_iter.return_value = [process]

    window_controller = MagicMock()
    window_controller.find_windows.return_value = [SimpleNamespace(hwnd=101)]
    window_controller.restore_app.return_value = SimpleNamespace(success=True)
    window_controller.focus_app.return_value = SimpleNamespace(success=True)

    executor = CommandExecutor(
        launcher=launcher,
        window_controller=window_controller,
        system_controller=MagicMock(),
        browser_controller=MagicMock(),
        browser_skill=MagicMock(),
        file_skill=MagicMock(),
    )

    result = executor.execute("open_app", {"app": "vscode", "requested_app": "vscode"}, "open vscode")

    assert result["success"] is True
    assert result["data"]["verified"] is True
    assert result["action_result"]["data"]["window_found"] is True
    assert result["action_result"]["data"]["window_focused"] is True
    window_controller.restore_app.assert_called_once_with("vscode")
    window_controller.focus_app.assert_called_once_with("vscode")


@patch("psutil.process_iter")
def test_command_executor_open_app_fails_if_window_cannot_be_focused(mock_process_iter):
    launcher = MagicMock()
    launcher.launch_app.return_value = LaunchResult(
        success=True,
        app_name="Visual Studio Code",
        matched_name="Visual Studio Code",
        path=r"C:\Users\rakes\AppData\Local\Programs\Microsoft VS Code\Code.exe",
        pid=1234,
        message="Opening Visual Studio Code.",
    )
    launcher.process_names_for.return_value = ("code.exe",)

    process = MagicMock()
    process.info = {
        "pid": 1234,
        "name": "Code.exe",
        "exe": r"C:\Users\rakes\AppData\Local\Programs\Microsoft VS Code\Code.exe",
    }
    mock_process_iter.return_value = [process]

    window_controller = MagicMock()
    window_controller.find_windows.return_value = [SimpleNamespace(hwnd=101)]
    window_controller.restore_app.return_value = SimpleNamespace(success=False)
    window_controller.focus_app.return_value = SimpleNamespace(success=False)

    executor = CommandExecutor(
        launcher=launcher,
        window_controller=window_controller,
        system_controller=MagicMock(),
        browser_controller=MagicMock(),
        browser_skill=MagicMock(),
        file_skill=MagicMock(),
    )

    result = executor.execute("open_app", {"app": "vscode", "requested_app": "vscode"}, "open vscode")

    assert result["success"] is False
    assert result["error"] == "window_not_focused"
    assert result["data"]["verified"] is False
    assert result["action_result"]["data"]["window_found"] is True
    assert result["action_result"]["data"]["window_focused"] is False


def test_command_executor_open_app_recovers_known_website_targets():
    launcher = MagicMock()
    browser = MagicMock()
    browser.open_url.return_value = MagicMock(
        success=True,
        verified=True,
        browser_id="chrome",
        message="YouTube opened successfully.",
        data={"launch_method": "direct_process"},
        error="",
    )

    executor = CommandExecutor(
        launcher=launcher,
        window_controller=MagicMock(),
        system_controller=MagicMock(),
        browser_controller=browser,
        browser_skill=MagicMock(),
        file_skill=MagicMock(),
    )

    result = executor.execute("open_app", {"app": "youtube", "requested_app": "youtube"}, "open youtube")

    assert result["success"] is True
    assert result["action_result"]["action"] == "open_website"
    browser.open_url.assert_called_once_with("https://www.youtube.com")


def test_command_executor_open_app_uses_whatsapp_web_fallback_when_desktop_launch_fails():
    launcher = MagicMock()
    launcher.launch_app.return_value = LaunchResult(
        success=False,
        app_name="whatsapp",
        matched_name="WhatsApp",
        message="I couldn't find an installed app named whatsapp.",
        error="app_not_found",
    )
    browser = MagicMock()
    browser.open_url.return_value = MagicMock(
        success=True,
        verified=True,
        browser_id="chrome",
        message="WhatsApp Web opened successfully.",
        data={"launch_method": "direct_process"},
        error="",
    )

    executor = CommandExecutor(
        launcher=launcher,
        window_controller=MagicMock(),
        system_controller=MagicMock(),
        browser_controller=browser,
        browser_skill=MagicMock(),
        file_skill=MagicMock(),
    )

    result = executor.execute("open_app", {"app": "whatsapp", "requested_app": "whatsapp"}, "open whatsapp")

    assert result["success"] is True
    assert result["data"]["fallback_mode"] == "web"
    assert result["data"]["route"] == "launcher"
    browser.open_url.assert_called_once_with("https://web.whatsapp.com")


def test_command_executor_open_app_accepts_unverified_web_fallback_when_browser_launch_succeeds():
    launcher = MagicMock()
    launcher.launch_app.return_value = LaunchResult(
        success=False,
        app_name="whatsapp",
        matched_name="WhatsApp",
        message="I couldn't find an installed app named whatsapp.",
        error="app_not_found",
    )
    browser = MagicMock()
    browser.open_url.return_value = MagicMock(
        success=True,
        verified=False,
        browser_id="chrome",
        message="Opening https://web.whatsapp.com.",
        state=None,
        data={"launch_method": "direct_webbrowser"},
        error="",
    )

    executor = CommandExecutor(
        launcher=launcher,
        window_controller=MagicMock(),
        system_controller=MagicMock(),
        browser_controller=browser,
        browser_skill=MagicMock(),
        file_skill=MagicMock(),
    )

    result = executor.execute("open_app", {"app": "whatsapp", "requested_app": "whatsapp"}, "open whatsapp")

    assert result["success"] is True
    assert result["data"]["fallback_mode"] == "web"
    assert result["action_result"]["verified"] is False
    browser.open_url.assert_called_once_with("https://web.whatsapp.com")


@patch("psutil.process_iter")
def test_command_executor_open_app_verifies_whatsapp_store_process_name(mock_process_iter):
    launcher = MagicMock()
    launcher.launch_app.return_value = LaunchResult(
        success=True,
        app_name="whatsapp",
        matched_name="WhatsApp",
        path=r"shell:AppsFolder\5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App",
        pid=-1,
        message="Opening WhatsApp.",
        verified=False,
        data={"launch_source": "start_apps"},
    )
    launcher.process_names_for.return_value = ("whatsapp.exe", "whatsapp.root.exe")

    process = MagicMock()
    process.info = {
        "pid": 4321,
        "name": "WhatsApp.Root.exe",
        "exe": r"C:\Program Files\WindowsApps\5319275A.WhatsAppDesktop\WhatsApp.Root.exe",
    }
    mock_process_iter.return_value = [process]

    window_controller = MagicMock()
    window_controller.find_windows.return_value = [SimpleNamespace(hwnd=202)]
    window_controller.restore_app.return_value = SimpleNamespace(success=True)
    window_controller.focus_app.return_value = SimpleNamespace(success=True)
    browser = MagicMock()

    executor = CommandExecutor(
        launcher=launcher,
        window_controller=window_controller,
        system_controller=MagicMock(),
        browser_controller=browser,
        browser_skill=MagicMock(),
        file_skill=MagicMock(),
    )

    result = executor.execute("open_app", {"app": "whatsapp", "requested_app": "whatsapp"}, "open whatsapp")

    assert result["success"] is True
    assert result["data"]["verified"] is True
    assert result["action_result"]["data"]["window_found"] is True
    browser.open_url.assert_not_called()


def test_command_executor_open_app_focuses_existing_window_before_relaunch():
    launcher = MagicMock()
    window_controller = MagicMock()
    window_controller.find_windows.return_value = [SimpleNamespace(hwnd=404)]
    window_controller.restore_app.return_value = SimpleNamespace(success=True)
    window_controller.focus_app.return_value = SimpleNamespace(success=True)
    browser = MagicMock()

    executor = CommandExecutor(
        launcher=launcher,
        window_controller=window_controller,
        system_controller=MagicMock(),
        browser_controller=browser,
        browser_skill=MagicMock(),
        file_skill=MagicMock(),
    )

    result = executor.execute("open_app", {"app": "whatsapp", "requested_app": "whatsapp"}, "open whatsapp")

    assert result["success"] is True
    assert result["data"]["existing_instance"] is True
    assert result["data"]["route"] == "window_control"
    launcher.launch_app.assert_not_called()
    browser.open_url.assert_not_called()


def test_command_executor_open_browser_app_uses_launcher_path_instead_of_browser_branch():
    launcher = MagicMock()
    launcher.launch_app.return_value = LaunchResult(
        success=True,
        app_name="Chrome",
        matched_name="Chrome",
        path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        pid=1234,
        message="Opening Chrome.",
        verified=True,
    )
    window_controller = MagicMock()
    window_controller.find_windows.return_value = []
    browser = MagicMock()

    executor = CommandExecutor(
        launcher=launcher,
        window_controller=window_controller,
        system_controller=MagicMock(),
        browser_controller=browser,
        browser_skill=MagicMock(),
        file_skill=MagicMock(),
    )

    result = executor.execute("open_app", {"app": "chrome", "requested_app": "chrome"}, "open chrome")

    assert result["success"] is True
    launcher.launch_app.assert_called_once_with("chrome")
    browser.open_browser.assert_not_called()
