"""
Path manager for the application.
Ensures all required directories exist.
"""
from pathlib import Path

# Root directory of the project
ROOT_DIR = Path(__file__).resolve().parent.parent

# Core directories
DATA_DIR = ROOT_DIR / "data"
PROMPTS_DIR = DATA_DIR / "prompts"
SCREENSHOTS_DIR = DATA_DIR / "screenshots"
LOG_DIR = ROOT_DIR / "logs"
MODELS_DIR = ROOT_DIR / "models"
SKILLS_DIR = ROOT_DIR / "skills"
PLUGINS_DIR = ROOT_DIR / "plugins"

def init_paths() -> None:
    """Creates necessary directories if they do not exist."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    PLUGINS_DIR.mkdir(parents=True, exist_ok=True)

# Call initialization on module load to guarantee paths are ready
init_paths()
