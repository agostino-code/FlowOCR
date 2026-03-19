# FlowOCR – Flow Launcher Plugin

Capture any region of your screen, extract the text with AI-powered OCR, and paste it anywhere — all without leaving your keyboard.

![Flow Launcher](https://img.shields.io/badge/Flow%20Launcher-Plugin-blue)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey)

---

## How it works

1. Type `ocr` in Flow Launcher and press <kbd>Enter</kbd>
2. The Windows Snipping Tool opens — select the area you want to read
3. OCR runs in the background; a notification appears when done
4. The recognised Markdown text is already in your clipboard — just paste

The OCR runs in a fully detached process so Flow Launcher stays responsive during capture and inference.

---

## Backends

| Backend | When to use |
|---|---|
| **Ollama** (default) | Local, private, no API key needed. Requires the `glm-ocr` model. |
| **HuggingFace** | Cloud inference via the HF serverless router. Requires a free API key. |

---

## Requirements

- Windows 10 / 11
- [Flow Launcher](https://www.flowlauncher.com/) ≥ 1.14
- Python 3.10+ (managed by Flow Launcher)

### Ollama backend (default)

1. Install [Ollama](https://ollama.com/)
2. Pull the OCR model:
   ```
   ollama pull glm-ocr
   ```
3. Make sure Ollama is running before you trigger OCR.

### HuggingFace backend

Create a free account at [huggingface.co](https://huggingface.co), then generate a token at **Settings → Access Tokens** (read access is sufficient).

---

## Installation

1. Download or clone this repository into your Flow Launcher plugins folder:
   ```
   %APPDATA%\FlowLauncher\Plugins\DeepSeekOCR-1.0.0\
   ```
2. Restart Flow Launcher (or reload plugins via `fl settings`).
3. Dependencies are installed automatically on first run.

---

## Settings

Open Flow Launcher settings, find **Screen OCR**, and configure:

| Setting | Description | Default |
|---|---|---|
| **Backend OCR** | `ollama` or `huggingface` | `ollama` |
| **Ollama Entrypoint** | Base URL of your local Ollama server | `http://localhost:11434` |
| **Hugging Face API Key** | Your `hf_...` token (only required for the HF backend) | *(empty)* |

You can also set the HF key via the `HF_TOKEN` environment variable instead of storing it in the plugin settings.

---

## Troubleshooting

**"Cannot connect to Ollama"**
Start the Ollama service (`ollama serve`) or check that the Entrypoint URL in settings matches your setup.

**"HuggingFace authentication failed (401)"**
Your token is missing or expired. Regenerate it at huggingface.co and update the plugin settings.

**No text detected**
The selected area may be too small, low-contrast, or contain no readable text. Try a larger capture region.

**Log files**
Each run writes a timestamped log to `%TEMP%\YYYYMMDD_HHMMSS.log`. Check the most recent file for detailed error traces.

---

## Project structure

```
DeepSeekOCR-1.0.0/
├── main.py                # Plugin logic
├── plugin.json            # Flow Launcher plugin manifest
├── requirements.txt       # Python dependencies
├── SettingsTemplate.yaml  # Settings UI definition
└── Images/
    └── app.png            # Plugin icon
```

---

## License

MIT