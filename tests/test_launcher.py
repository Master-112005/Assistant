import pytest
import os
from unittest.mock import patch, MagicMock
from core.launcher import AppLauncher, LaunchResult
from core.app_index import AppRecord

@pytest.fixture
def launcher():
    launcher = AppLauncher()
    # Mock indexer for testing
    launcher.indexer.apps = {
        "google chrome": AppRecord("Google Chrome", "google chrome", "mock/path/chrome.exe", "executable", "test"),
        "whatsapp": AppRecord("WhatsApp", "whatsapp", "mock/path/whatsapp.exe", "executable", "test"),
        "spotify": AppRecord("Spotify", "spotify", "mock/path/spotify.exe", "executable", "test"),
        "calculator": AppRecord("Calculator", "calculator", "mock/path/calc.exe", "executable", "test")
    }
    return launcher

def test_find_best_match_exact(launcher):
    record = launcher.find_best_match("google chrome")
    assert record is not None
    assert record.name == "Google Chrome"

def test_find_best_match_alias(launcher):
    # alias maps "chrome" to "Google Chrome"
    query = launcher.aliases.resolve_alias("chrome")
    record = launcher.find_best_match(query)
    assert record is not None
    assert record.name == "Google Chrome"

def test_find_best_match_partial(launcher):
    record = launcher.find_best_match("whatsapp")
    assert record is not None
    assert record.name == "WhatsApp"

def test_unknown_app(launcher):
    res = launcher.launch_by_name("NonExistentApp123")
    assert res.success is False
    assert "couldn't find" in res.message.lower()

@patch('os.path.exists')
@patch('subprocess.Popen')
def test_launch_success(mock_popen, mock_exists, launcher):
    mock_exists.return_value = True
    
    mock_process = MagicMock()
    mock_process.pid = 9999
    mock_popen.return_value = mock_process
    
    res = launcher.launch_by_name("chrome")
    assert res.success is True
    assert res.pid == 9999
    assert res.verified is True
    assert res.app_name == "Google Chrome"

@patch('os.path.exists')
def test_launch_broken_path(mock_exists, launcher):
    mock_exists.return_value = False
    
    res = launcher.launch_by_name("chrome")
    assert res.success is False
    assert "path not found" in res.message.lower()
