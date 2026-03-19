"""
Flow Launcher plugin – Screen OCR
=================================
Captures a screen region via the Windows Snipping Tool, runs OCR through
either a local Ollama model or the HuggingFace serverless router, and copies
the resulting Markdown text to the clipboard.

Architecture
------------
Flow Launcher calls ``capture_and_ocr()`` in the main (plugin host) process.
That immediately spawns a fully detached child process (the "worker") so that
the Snipping Tool overlay and the slow OCR call never block the launcher UI.
The worker runs the full pipeline and reports results via a balloon notification.

Process communication
---------------------
The plugin serialises its settings to a temporary JSON file whose path is
passed to the worker as a CLI argument.  No shared memory or IPC sockets are
needed; the file is deleted by the worker as soon as it is read.
"""

from __future__ import annotations

import base64
import ctypes
import dataclasses
import datetime
import json
import logging
import mimetypes
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from typing import Any, Literal, Optional

# ---------------------------------------------------------------------------
# Plugin path setup – must run before any local-package imports
# ---------------------------------------------------------------------------
_PLUGIN_DIR = os.path.abspath(os.path.dirname(__file__))
for _p in (_PLUGIN_DIR,
           os.path.join(_PLUGIN_DIR, "lib"),
           os.path.join(_PLUGIN_DIR, "plugin")):
    sys.path.append(_p)

from pyflowlauncher import Plugin  # noqa: E402

# ---------------------------------------------------------------------------
# Logging – one timestamped file per process written to %TEMP%
# ---------------------------------------------------------------------------
_LOG_PATH = os.path.join(
    tempfile.gettempdir(),
    datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log",
)
logging.basicConfig(
    filename=_LOG_PATH,
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform guard
# ---------------------------------------------------------------------------
if os.name != "nt":
    raise RuntimeError("This plugin requires Windows.")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
BACKEND_OLLAMA = "ollama"
BACKEND_HF     = "huggingface"

OLLAMA_DEFAULT_URL = "http://localhost:11434"
OCR_MODEL_OLLAMA   = "glm-ocr"
OLLAMA_OCR_PROMPT  = "Text Recognition:"

HF_ROUTER_URL = "https://router.huggingface.co/zai-org/api/paas/v4/layout_parsing"
OCR_MODEL_HF  = "zai-org/GLM-OCR"

# subprocess creation flags: hide console window; fully detach the worker process
_CREATE_NO_WINDOW      = 0x08000000
_DETACHED_CREATE_FLAGS = (
    _CREATE_NO_WINDOW
    | getattr(subprocess, "DETACHED_PROCESS", 0)
    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
)

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class Settings:
    """
    Typed, validated representation of the plugin's user-facing settings.

    All fields mirror the keys defined in ``SettingsTemplate.yaml`` and are
    populated from the raw dict that Flow Launcher passes to the plugin
    (which may be ``None`` when the user has never opened the settings panel).
    """
    backend:           str = BACKEND_OLLAMA
    ollama_entrypoint: str = OLLAMA_DEFAULT_URL
    hf_api_key:        str = ""

    @classmethod
    def from_dict(cls, raw: Optional[dict]) -> Settings:
        """Build a Settings instance from the raw dict provided by Flow Launcher."""
        d: dict = raw or {}
        # Strip before the "or" fallback so all-whitespace values are treated as missing
        return cls(
            backend=           (d.get("backend",           "") or "").strip().lower() or BACKEND_OLLAMA,
            ollama_entrypoint= (d.get("ollama_entrypoint", "") or "").strip()          or OLLAMA_DEFAULT_URL,
            hf_api_key=        (d.get("hf_api_key",        "") or "").strip(),
        )

    def resolve_hf_key(self) -> str:
        """Return the HF API key from settings, falling back to the HF_TOKEN env var."""
        return self.hf_api_key or os.environ.get("HF_TOKEN", "").strip()

    def backend_label(self) -> str:
        """Human-readable backend name shown in the Flow Launcher result list."""
        return "Ollama (local)" if self.backend == BACKEND_OLLAMA else "HuggingFace (GLM-OCR)"

    def to_worker_config(self) -> dict:
        """Serialise to the dict written to the temporary worker config file."""
        return dataclasses.asdict(self)

# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _try_remove(path: str) -> None:
    """Delete a file, silently ignoring errors (e.g. already deleted)."""
    try:
        os.remove(path)
    except OSError:
        pass

# ---------------------------------------------------------------------------
# PowerShell helpers
# ---------------------------------------------------------------------------

def _run_powershell(script: str, *args, timeout: int = 10) -> Optional[subprocess.CompletedProcess]:
    """
    Run a PowerShell one-liner silently (no console window, no profile).
    Returns ``subprocess.CompletedProcess`` or ``None`` on timeout / OS error.
    """
    ps_exe = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
    )
    try:
        result = subprocess.run(
            [ps_exe, "-NoProfile", "-NonInteractive",
             "-WindowStyle", "Hidden", "-STA", "-Command", script, *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
            text=True,
        )
        if result.returncode != 0 and result.stderr:
            log.warning("PowerShell stderr: %s", result.stderr[:200])
        return result
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("PowerShell failed: %s", exc)
        return None


def _ps_quote(value: str) -> str:
    """Escape a value for embedding inside a PowerShell single-quoted string literal."""
    return str(value).replace("'", "''")

# ---------------------------------------------------------------------------
# Windows notifications
# ---------------------------------------------------------------------------

def _notify(title: str, message: str, level: Literal["info", "warning", "error"] = "info") -> None:
    """
    Show a Windows balloon notification, falling back to a modal MessageBox.

    Parameters
    ----------
    level : 'info' | 'warning' | 'error'
    """
    log.info("Notify [%s] %s - %s", level, title, message)
    icon = {"error": "Error", "warning": "Warning"}.get(level, "Info")
    t, m, i = _ps_quote(title), _ps_quote(message), _ps_quote(icon)

    # Balloon tip preferred: non-blocking, disappears automatically
    balloon = _run_powershell(
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        f"$n.Icon = [System.Drawing.SystemIcons]::{i};"
        "$n.Visible = $true;"
        f"$n.BalloonTipTitle = '{t}';"
        f"$n.BalloonTipText  = '{m}';"
        "$n.ShowBalloonTip(4500);"
        "Start-Sleep -Milliseconds 5000;"
        "$n.Dispose();",
        timeout=8,
    )
    if balloon is not None and balloon.returncode == 0:
        return

    # Fallback: blocking MessageBox (used when the notification area is unavailable)
    _run_powershell(
        "Add-Type -AssemblyName System.Windows.Forms;"
        f"$icon    = [System.Windows.Forms.MessageBoxIcon]::{i};"
        "$buttons = [System.Windows.Forms.MessageBoxButtons]::OK;"
        f"[void][System.Windows.Forms.MessageBox]::Show('{m}', '{t}', $buttons, $icon);",
        timeout=10,
    )

# ---------------------------------------------------------------------------
# Clipboard helpers
# ---------------------------------------------------------------------------

def _clear_clipboard() -> None:
    """Clear the clipboard before opening the Snipping Tool so stale images are not picked up."""
    _run_powershell(
        "Add-Type -AssemblyName System.Windows.Forms;"
        "[Windows.Forms.Clipboard]::Clear();"
    )


def _clipboard_image_to_temp_png() -> Optional[str]:
    """
    Read a bitmap from the clipboard and write it to a temporary PNG file.

    The image is transferred as a base64 string through PowerShell stdout to
    avoid path-encoding problems when the Windows user profile contains
    non-ASCII characters (a direct ``$img.Save(path)`` call fails in those cases).

    Returns the temp file path on success, ``None`` if the clipboard contains
    no image or an error occurs.

    PowerShell exit codes
    ---------------------
    2 – clipboard holds no image (expected during the polling loop)
    6 – ``Image.Save()`` raised an exception
    """
    tmp = tempfile.NamedTemporaryFile(prefix="screen-ocr-", suffix=".png", delete=False)
    tmp.close()

    result = _run_powershell(
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$img = [Windows.Forms.Clipboard]::GetImage();"
        "if ($img -eq $null) { exit 2 };"
        "$ms = New-Object System.IO.MemoryStream;"
        "try { $img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png) }"
        "catch { Write-Error $_; exit 6 };"
        "$b64 = [Convert]::ToBase64String($ms.ToArray());"
        "Write-Output $b64;"
        "try { $ms.Dispose(); $img.Dispose() } catch { };",
        timeout=15,
    )

    rc = None if result is None else result.returncode

    if rc is None:
        log.error("PowerShell timed out while reading clipboard")
        _try_remove(tmp.name)
        return None
    if rc == 2:
        log.debug("Clipboard holds no image yet")
        _try_remove(tmp.name)
        return None
    if rc == 6:
        log.error("PowerShell: Image.Save() to MemoryStream failed")
        _try_remove(tmp.name)
        return None
    if rc != 0:
        log.error("PowerShell exited with unexpected code %d", rc)
        _try_remove(tmp.name)
        return None

    try:
        png_bytes = base64.b64decode(result.stdout.strip())
        if not png_bytes:
            log.error("Base64 payload decoded to zero bytes")
            _try_remove(tmp.name)
            return None
        with open(tmp.name, "wb") as fh:
            fh.write(png_bytes)
        log.info("Clipboard image saved: %s (%d bytes)", tmp.name, len(png_bytes))
        return tmp.name
    except Exception as exc:
        log.error("Base64 decode / write error: %s", exc)
        _try_remove(tmp.name)
        return None


def _copy_text_to_clipboard(text: str) -> None:
    """
    Write Unicode text to the Windows clipboard via Win32 API (ctypes).

    A direct ctypes call is used instead of a PowerShell round-trip to handle
    arbitrary Unicode text reliably and to avoid PowerShell quoting edge-cases.

    After ``SetClipboardData`` succeeds the OS owns the global memory handle;
    we must not free it ourselves.
    """
    user32   = ctypes.WinDLL("user32",   use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    CF_UNICODETEXT = 13
    GHND           = 0x0042  # moveable + zero-initialised global memory

    # Explicit argtypes/restype declarations are required for correct ctypes marshalling
    user32.OpenClipboard.argtypes    = [wintypes.HWND]
    user32.OpenClipboard.restype     = wintypes.BOOL
    user32.EmptyClipboard.argtypes   = []
    user32.EmptyClipboard.restype    = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype  = wintypes.HANDLE
    user32.CloseClipboard.argtypes   = []
    user32.CloseClipboard.restype    = wintypes.BOOL
    kernel32.GlobalAlloc.argtypes    = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype     = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes     = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype      = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes   = [wintypes.HGLOBAL]
    kernel32.GlobalUnlock.restype    = wintypes.BOOL
    kernel32.GlobalFree.argtypes     = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype      = wintypes.HGLOBAL

    # CF_UNICODETEXT requires a UTF-16LE buffer with a null terminator
    encoded = text.encode("utf-16-le") + b"\x00\x00"

    if not user32.OpenClipboard(None):
        raise OSError("OpenClipboard failed")

    h_global = None
    ptr = None
    try:
        user32.EmptyClipboard()
        h_global = kernel32.GlobalAlloc(GHND, len(encoded))
        if not h_global:
            raise OSError("GlobalAlloc failed")
        ptr = kernel32.GlobalLock(h_global)
        if not ptr:
            raise OSError("GlobalLock failed")
        ctypes.memmove(ptr, encoded, len(encoded))
        kernel32.GlobalUnlock(h_global)
        ptr = None
        if not user32.SetClipboardData(CF_UNICODETEXT, h_global):
            raise OSError("SetClipboardData failed")
        h_global = None  # clipboard now owns the handle; we must not free it
    finally:
        if ptr:
            kernel32.GlobalUnlock(h_global)
        if h_global:  # only reached if SetClipboardData failed before taking ownership
            kernel32.GlobalFree(h_global)
        user32.CloseClipboard()

# ---------------------------------------------------------------------------
# Screen capture
# ---------------------------------------------------------------------------

def _capture_screen_region(timeout_seconds: float = 60.0) -> Optional[str]:
    """
    Launch the Windows Snipping Tool (``ms-screenclip:``) and wait until the
    user captures a region.  The tool writes the result to the clipboard.

    Polls with exponential back-off (50 ms -> 500 ms) to keep CPU usage low.
    Returns the path to a temporary PNG file, or ``None`` on timeout / cancel.
    """
    _clear_clipboard()
    try:
        os.startfile("ms-screenclip:")
    except OSError as exc:
        log.error("Cannot launch ms-screenclip: %s", exc)
        return None

    deadline   = time.monotonic() + timeout_seconds
    sleep_time = 0.05   # initial poll interval (seconds)
    max_sleep  = 0.5

    while time.monotonic() < deadline:
        path = _clipboard_image_to_temp_png()
        if path:
            log.info("Screen capture ready: %s", path)
            return path
        time.sleep(sleep_time)
        sleep_time = min(sleep_time * 1.3, max_sleep)

    log.warning("Timed out waiting for clipboard image after %.1f s", timeout_seconds)
    return None

# ---------------------------------------------------------------------------
# OCR backend – HuggingFace
# ---------------------------------------------------------------------------

def _hf_has_error(payload: Any) -> bool:
    """
    Return True if the HF router payload signals an error.

    The router always returns HTTP 200 even on failures, so we must inspect
    the JSON body.  The ``error`` field may be a plain string or a nested dict.
    """
    if not isinstance(payload, dict):
        return False
    err = payload.get("error")
    return isinstance(err, dict) or (isinstance(err, str) and bool(err.strip()))


def _hf_error_message(payload: Any, body: str = "") -> str:
    """Extract a human-readable error string from an HF router response payload."""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or body or "unknown error")
        if isinstance(err, str) and err.strip():
            return err.strip()
    return (body or "unknown error").strip()[:220]


def _hf_extract_text(payload: Any) -> str:
    """
    Recursively extract OCR text from an HF router JSON response.

    Handles several response shapes produced by different router/model versions:
    - Plain string
    - List of text blocks
    - Dict with a direct text key (``text``, ``markdown``, ``output``, ...)
    - OpenAI-style ``{"choices": [{"message": {"content": "..."}}]}``
    """
    if isinstance(payload, str):
        return payload.strip()

    if isinstance(payload, list):
        parts = [_hf_extract_text(item) for item in payload]
        return "\n".join(p for p in parts if p).strip()

    if isinstance(payload, dict):
        for key in ("text", "markdown", "output", "output_text", "content", "result"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, (dict, list)):
                nested = _hf_extract_text(value)
                if nested:
                    return nested

        # OpenAI-compatible chat format
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()

    return ""


def _ocr_huggingface(image_path: str, api_key: str) -> str:
    """
    Run OCR via the HuggingFace serverless inference router (GLM-OCR).

    Request strategy (tried in order until one succeeds)
    -----------------------------------------------------
    1. ``multipart/form-data`` with field name ``image``, then ``file``.
       Some router versions are strict about the field name.
    2. Raw bytes body with Content-Type cycling through common MIME types.
       Needed because the router occasionally rejects multipart encoding.

    The router returns HTTP 200 even on errors, so every response is inspected
    for a JSON ``error`` field before being treated as a success.
    """
    import httpx

    log.debug("HF OCR: router=%s model=%s image=%s token=%s...",
              HF_ROUTER_URL, OCR_MODEL_HF, image_path, api_key[:6] if api_key else "")

    with open(image_path, "rb") as fh:
        image_bytes = fh.read()

    # Deduplicated MIME type list; prefer the extension-guessed type
    guessed, _ = mimetypes.guess_type(image_path)
    mime_candidates = list(dict.fromkeys(
        m for m in ("image/png", guessed, "image/jpeg", "application/octet-stream") if m
    ))

    auth = {"Authorization": f"Bearer {api_key}"}
    status_code: int = 0
    body: str = ""
    payload: Optional[dict] = None

    def _parse(resp):
        """Update the shared response state from an httpx response object."""
        nonlocal status_code, body, payload
        status_code = resp.status_code
        body = resp.text or ""
        try:
            payload = resp.json()
        except ValueError:
            payload = None

    # 1) Multipart form-data
    for field_name in ("image", "file"):
        files = {field_name: (os.path.basename(image_path) or "capture.png",
                              image_bytes, "image/png")}
        _parse(httpx.post(HF_ROUTER_URL, headers=auth, files=files, timeout=90.0))
        if status_code < 400 and not _hf_has_error(payload):
            break
        log.warning("HF multipart field '%s' rejected (HTTP %s)", field_name, status_code)

    # 2) Raw bytes fallback
    if status_code >= 400 or _hf_has_error(payload):
        for content_type in mime_candidates:
            _parse(httpx.post(HF_ROUTER_URL,
                              headers={**auth, "Content-Type": content_type},
                              content=image_bytes, timeout=90.0))
            if "content type" in body.lower() and "not supported" in body.lower():
                log.warning("HF router rejects Content-Type '%s', trying next", content_type)
                continue
            if status_code < 400 and not _hf_has_error(payload):
                break

    if status_code == 401:
        raise RuntimeError(
            "HuggingFace authentication failed (401). "
            "Check hf_api_key / HF_TOKEN and token permissions."
        )
    if status_code >= 400 or _hf_has_error(payload):
        raise RuntimeError(f"HF router error: {_hf_error_message(payload, body)}")

    text = _hf_extract_text(payload if payload is not None else body)
    if text:
        return text
    raise RuntimeError(
        f"Empty or unrecognised OCR response from HF router: {body.strip()[:220]}"
    )

# ---------------------------------------------------------------------------
# OCR backend – Ollama (local)
# ---------------------------------------------------------------------------

def _ocr_ollama(image_path: str, base_url: Optional[str] = None) -> str:
    """
    Run OCR via a local Ollama vision model through the REST API (``/api/chat``).

    The Ollama CLI (``ollama run``) is interactive-only and cannot accept image
    file arguments when run as a headless subprocess.  The REST API is the
    correct programmatic interface for non-interactive use.
    """
    import httpx

    ollama_url = (base_url or OLLAMA_DEFAULT_URL).strip() or OLLAMA_DEFAULT_URL
    log.debug("Ollama OCR: url=%s model=%s image=%s", ollama_url, OCR_MODEL_OLLAMA, image_path)

    with open(image_path, "rb") as fh:
        image_b64 = base64.b64encode(fh.read()).decode("utf-8")

    request_body = {
        "model":    OCR_MODEL_OLLAMA,
        "stream":   False,
        "messages": [
            {"role": "user", "content": OLLAMA_OCR_PROMPT, "images": [image_b64]}
        ],
    }

    try:
        response = httpx.post(ollama_url.rstrip("/") + "/api/chat",
                              json=request_body, timeout=120.0)
    except httpx.ConnectError:
        raise RuntimeError(
            f"Cannot connect to Ollama at {ollama_url}. "
            "Ensure the Ollama service is running."
        )

    log.debug("Ollama HTTP %s", response.status_code)
    if response.status_code != 200:
        raise RuntimeError(
            f"Ollama returned HTTP {response.status_code}: {response.text.strip()[:220]}"
        )

    data = response.json()
    # Primary shape: {"message": {"role": "assistant", "content": "..."}}
    # Older Ollama versions use a top-level "response" key instead.
    content = data.get("message", {}).get("content") or data.get("response") or ""
    return content.strip()

# ---------------------------------------------------------------------------
# OCR dispatcher
# ---------------------------------------------------------------------------

def _ocr_request(image_path: str, settings: Settings) -> str:
    """Dispatch the OCR request to the backend specified in *settings*."""
    if settings.backend == BACKEND_OLLAMA:
        return _ocr_ollama(image_path, base_url=settings.ollama_entrypoint)

    if settings.backend == BACKEND_HF:
        key = settings.resolve_hf_key()
        if not key:
            raise ValueError(
                "HuggingFace API key not set. "
                "Add hf_api_key in plugin settings or set the HF_TOKEN env var."
            )
        if not key.startswith("hf_"):
            raise ValueError("Invalid HuggingFace key: must start with 'hf_'.")
        return _ocr_huggingface(image_path, api_key=key)

    raise ValueError(f"Unknown OCR backend: {settings.backend!r}")

# ---------------------------------------------------------------------------
# Detached OCR worker  (runs inside the child process)
# ---------------------------------------------------------------------------

def _run_detached_ocr_worker(config: dict) -> None:
    """
    Execute the full OCR pipeline inside the detached worker process:
    capture screen region -> OCR -> copy result to clipboard -> notify user.
    """
    settings = Settings.from_dict(config)

    image_path = _capture_screen_region()
    if not image_path:
        _notify("Screen OCR", "Capture cancelled: no area selected.", level="warning")
        return

    try:
        markdown = _ocr_request(image_path, settings)
    except Exception as exc:
        log.exception("OCR failed")
        _notify("Screen OCR - Error", f"OCR failed: {str(exc)[:180]}", level="error")
        return
    finally:
        _try_remove(image_path)

    text = (markdown or "").strip()
    if not text:
        _notify("Screen OCR", "No text detected.", level="warning")
        return

    try:
        _copy_text_to_clipboard(markdown)
    except Exception as exc:
        log.exception("Clipboard write failed")
        _notify("Screen OCR - Error", f"Clipboard write failed: {str(exc)[:180]}", level="error")
        return

    _notify("Screen OCR", f"Done: {len(text)} characters copied to clipboard.")

# ---------------------------------------------------------------------------
# Worker process lifecycle
# ---------------------------------------------------------------------------

def _spawn_detached_worker(settings: Settings) -> None:
    """
    Spawn a fully detached child process that will run the OCR pipeline.

    Settings are serialised to a temporary JSON file whose path is passed on
    the command line; the child process deletes the file after reading it.
    """
    fd, config_path = tempfile.mkstemp(prefix="screen-ocr-config-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(settings.to_worker_config(), fh)
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--detached-worker", config_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_DETACHED_CREATE_FLAGS,
            close_fds=True,
        )
    except Exception:
        log.exception("Failed to spawn worker process")
        raise


def _handle_detached_worker_argv() -> bool:
    """
    Check whether this process was launched as a detached worker.
    If so, load the config file, run the pipeline, and return ``True``.
    Returns ``False`` in the normal plugin host process.
    """
    if "--detached-worker" not in sys.argv:
        return False

    try:
        config_path = sys.argv[sys.argv.index("--detached-worker") + 1]
    except (ValueError, IndexError):
        return True  # flag present but path argument missing – nothing to do

    config = {}
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            config = json.load(fh) or {}
    except (OSError, ValueError) as exc:
        log.warning("Cannot read worker config file: %s", exc)
    finally:
        _try_remove(config_path)

    _run_detached_ocr_worker(config)
    return True

# ---------------------------------------------------------------------------
# Flow Launcher plugin class
# ---------------------------------------------------------------------------

class ScreenOCR(Plugin):
    """Flow Launcher plugin entry point."""

    def __init__(self):
        super().__init__()
        self.on_method(self.query)
        self.on_method(self.context_menu)
        self.on_method(self.noop)
        self.on_method(self.capture_and_ocr)

    @staticmethod
    def _response(results: list) -> dict:
        """Wrap a result list in the JSON-RPC envelope Flow Launcher expects."""
        return {"result": results}

    def _settings(self) -> Settings:
        """Parse and validate the current plugin settings into a typed object."""
        return Settings.from_dict(self.settings)

    def query(self, query):
        """Build the result list shown in the Flow Launcher search box."""
        s = self._settings()
        results = []

        # Warn the user when HF backend is selected but no API key is configured
        if s.backend == BACKEND_HF and not s.resolve_hf_key():
            results.append({
                "Title":    "HuggingFace API key not set",
                "SubTitle": "Add hf_api_key in plugin settings or set the HF_TOKEN env var",
                "IcoPath":  "Images/app.png",
                "JsonRPCAction": {"method": "noop", "parameters": []},
            })

        results.append({
            "Title":    "Capture screen region -> OCR -> Markdown",
            "SubTitle": f"Backend: {s.backend_label()} | result auto-copied to clipboard",
            "IcoPath":  "Images/app.png",
            "JsonRPCAction": {
                "method":     "capture_and_ocr",
                "parameters": [],
            },
        })
        return self._response(results)

    def context_menu(self, data):
        return self._response([{
            "Title":    "Async OCR",
            "SubTitle": "Runs in a separate process; result is copied to the clipboard",
            "IcoPath":  "Images/app.png",
            "JsonRPCAction": {"method": "noop", "parameters": []},
        }])

    def noop(self):
        return self._response([])

    def capture_and_ocr(self):
        """Spawn the detached worker and return immediately to Flow Launcher."""
        try:
            _spawn_detached_worker(self._settings())
        except Exception as exc:
            log.exception("Failed to start OCR worker")
            _notify("Screen OCR - Error",
                    f"Cannot start OCR worker: {str(exc)[:180]}", level="error")
        return self._response([])

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not _handle_detached_worker_argv():
        plugin = ScreenOCR()
        plugin.run()