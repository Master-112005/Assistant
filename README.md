# Nova Assistant

Phase 9: Local LLM Integration

## Setup
1. Create a virtual environment: `python -m venv venv`
2. Activate virtual environment: `.\venv\Scripts\activate` (Windows)
3. Install requirements: `pip install -r requirements.txt`
4. Copy `.env.example` to `.env`
5. Install Ollama for Windows from `https://ollama.com/download/windows`
6. Start Ollama. The local API should be available at `http://localhost:11434`
7. Pull a local model:
   `ollama pull llama3`
   Fallback options:
   `ollama pull mistral`
   `ollama pull phi3`
8. Run the application: `python main.py`

## Local LLM
- Provider: `Ollama`
- Default host: `http://localhost:11434`
- Preferred model: `llama3`
- Structured tasks: STT correction, intent extraction, multi-step planning
- Fallback behavior: if Ollama or the model is unavailable, the assistant returns `Local AI model unavailable. Using standard command mode.`

Prompt templates live in `data/prompts/` and deterministic response cache entries are stored in `data/llm_cache.json`.

## Testing
Run tests using pytest: `pytest tests/`

The LLM tests use mocked Ollama HTTP responses, so they do not require a live model. The UI test runs the processor in a background thread to confirm the window does not block during local inference.
