"""
Tests for configuration management.
"""
import os
from core.config import load_config, Config

def test_config_defaults():
    """Test that configuration loads with expected defaults."""
    # Temporarily remove APP_NAME from env if it exists
    original_app_name = os.environ.get("APP_NAME")
    if "APP_NAME" in os.environ:
        del os.environ["APP_NAME"]
        
    config = load_config()
    assert config.APP_NAME == "Nova Assistant"
    
    # Restore
    if original_app_name:
        os.environ["APP_NAME"] = original_app_name

def test_config_loads_successfully():
    """Test that configuration loads without raising an error."""
    config = load_config()
    assert isinstance(config, Config)
