# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

FlowOCR is a Flow Launcher plugin that captures screen regions via Windows Snipping Tool, performs OCR using AI models (Ollama or HuggingFace), and copies the extracted text to the clipboard. The plugin runs OCR in a detached worker process to keep Flow Launcher responsive during capture and inference.

## Architecture

### Process Model
- **Main process**: Handles Flow Launcher plugin interface (`ScreenOCR` class) and spawns detached workers
- **Detached worker**: Runs the full OCR pipeline (capture → OCR → clipboard → notification) independently
- Worker communication: Config passed via temporary JSON file; no shared memory or IPC needed

### Key Components
- `main.py`: Single-file plugin containing all logic (~1000 lines)
- `ScreenOCR` class: Flow Launcher plugin interface with `query()`, `context_menu()`, and `capture_and_ocr()` methods
- `_run_detached_ocr_worker()`: Full OCR pipeline executed in child process
- Backend dispatch: `_ocr_request()` routes to either Ollama or HuggingFace

### Concurrency Control
- **Lock file** (`%TEMP%\screen-ocr-worker.lock`): Prevents concurrent OCR workers; stale locks expire after 5 minutes
- **Duplicate detection** (`%TEMP%\screen-ocr-last-hash.txt`): SHA-256 hash of last processed image; skips re-processing identical images

### OCR Backends
- **Ollama** (default): Local REST API at `http://localhost:11434`, uses `glm-ocr` model
  - Cold-start handling: Blocks on `ollama run` until model loads, polls `/api/ps` for readiness
  - Retry logic: 4 attempts with 5s delays for connection issues during model loading
- **HuggingFace**: Cloud inference via serverless router at `https://router.huggingface.co/zai-org/api/paas/v4/layout_parsing`
  - Uses JSON payload with data URI to avoid Content-Type charset issues
  - Requires `hf_` API token (from settings or `HF_TOKEN` env var)

## Development Commands

### Testing the Plugin
```bash
# Run the plugin directly (for development testing)
python main.py

# Test with Flow Launcher
# 1. Install plugin to %APPDATA%\FlowLauncher\Plugins\FlowOCR-1.0.0\
# 2. Restart Flow Launcher or reload plugins via `fl settings`
# 3. Type "ocr" in Flow Launcher and press Enter
```

### Dependencies
```bash
# Install dependencies (Flow Launcher handles this automatically on first run)
pip install -r requirements.txt

# Only dependency: pyflowlauncher
```

### Ollama Setup (for local backend)
```bash
# Install Ollama from https://ollama.com/
# Pull the required model
ollama pull glm-ocr

# Start Ollama service if not running
ollama serve
```

### Log Files
- Location: `%TEMP%\YYYYMMDD_HHMMSS.log` (one per process)
- Automatic cleanup: Logs older than 2 days are purged on startup
- Check recent logs for detailed error traces during troubleshooting

## Important Implementation Details

### Windows-Specific APIs
- **Clipboard**: Uses ctypes Win32 APIs (`OpenClipboard`, `SetClipboardData`, etc.) for text; PowerShell for image capture
- **Notifications**: Pure ctypes `Shell_NotifyIconW` (no PowerShell round-trip)
- **Screen capture**: `ms-screenclip:` protocol via `os.startfile()`

### HTTP Handling
- Custom `_http_post_bytes()` function to prevent automatic charset injection in Content-Type headers
- Critical for HuggingFace backend: CloudFront proxy appends `;charset=UTF-8` to bare Content-Type, causing rejections

### Error Handling Patterns
- All OCR operations wrapped in try/except with user notifications via `_notify()`
- PowerShell clipboard operations have specific return codes (2=no image, 6=save failure)
- HTTP errors include detailed error message extraction from JSON payloads

### Plugin Settings
- `backend`: "ollama" or "huggingface" (default: "ollama")
- `ollama_entrypoint`: Base URL for local Ollama server (default: "http://localhost:11434")
- `hf_api_key`: HuggingFace API token (required for HF backend, can use `HF_TOKEN` env var instead)

## Common Issues

### Ollama Connection Failures
- Ensure Ollama service is running (`ollama serve`)
- Check that `ollama_entrypoint` setting matches your setup
- Model loading can take 10-30s on cold start; plugin handles this with readiness polling

### HuggingFace Authentication
- Token must start with "hf_"
- Check token permissions at huggingface.co → Settings → Access Tokens
- Can set via `HF_TOKEN` environment variable instead of plugin settings

### Clipboard Issues
- Plugin clears clipboard before opening snipping tool
- Uses PowerShell MemoryStream → base64 to avoid path-encoding issues on non-ASCII user profiles
- Text copied via Win32 APIs for reliability

## File Structure
```
FlowOCR-1.0.0/
├── main.py                # All plugin logic (single file)
├── plugin.json            # Flow Launcher manifest
├── requirements.txt       # Python dependencies
├── SettingsTemplate.yaml  # Settings UI definition
├── README.md             # User documentation
└── Images/
    └── app.png            # Plugin icon
```

## Version Management
- Version defined in `plugin.json` (currently 1.0.2)
- Update version number when making breaking changes or significant features
- Git commit messages should describe the change (e.g., "Enhance OCR functionality with duplicate image check")