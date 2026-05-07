# Nova Assistant

A desktop AI assistant that runs completely offline using built-in rule-based natural language processing. No external AI services, LLM, or internet connection required.

## Setup

1. Create a virtual environment: `python -m venv venv`
2. Activate virtual environment: `.\venv\Scripts\activate` (Windows)
3. Install requirements: `pip install -r requirements.txt`
4. Copy `.env.example` to `.env`
5. Run the application: `python main.py`

## Features

### Built-in Natural Language Processing

All command processing happens locally using deterministic algorithms:

- **Intent Detection**: Rule-based classification for 50+ intent types
  - App control: open, close, minimize, maximize, focus, restore, toggle
  - Browser actions: search, navigate, tabs, refresh, back/forward
  - System controls: volume, brightness, mute, lock, sleep, shutdown, restart
  - Media: play, pause, next, previous
  - File operations: create, open, delete, move, rename, search
  - Messaging: send WhatsApp messages, make calls
  - Smart commands: screenshot, settings, calculator, weather, time, email, VPN, dark mode

- **Entity Extraction**: Pattern matching to identify
  - App names and aliases
  - URLs and websites
  - Search queries
  - Contacts
  - File paths
  - System controls

- **Multi-action Planning**: Command segmentation for complex commands
  - "open chrome and search weather" → 2-step plan
  - "open notepad then type hello" → 2-step plan
  - Handles connectors: and, then, after that, also, followed by

- **STT Correction**: 5-layer correction pipeline
  - Text normalization
  - Dictionary/alias corrections
  - Vocabulary boosting
  - Validation
  - Safe fallback

### No External Dependencies

- No LLM or AI service required
- No internet connection
- No Ollama or local model setup
- All processing done locally on your machine
- Fast response times

### Supported Commands Examples

```bash
# App control
open chrome
close notepad
minimize window
maximize this
focus vscode

# Browser
search IPL score
open youtube
go to github

# System
turn up volume
set brightness to 50%
mute
lock PC
take a screenshot

# Media
play music
pause
next track

# Smart commands
open calculator
open settings
what time is it
check my email
turn on dark mode

# Multi-step
open chrome and search weather
open spotify and play first song
```

## Testing

Run tests using pytest: `pytest tests/`

## Architecture

```
User Input
    │
    ▼
Normalizer (clean text, fix typos)
    │
    ▼
Intent Detector (classify 50+ intents)
    │
    ▼
Entity Extractor (parse app names, URLs, contacts)
    │
    ▼
Action Planner (create execution steps)
    │
    ▼
Execution Engine (run actions)
```

All components use pattern matching, fuzzy logic, and rule-based algorithms - no machine learning or AI models required.