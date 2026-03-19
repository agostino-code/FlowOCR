"""
Flow Launcher plugin – Screen OCR
Captures a screen region via Windows Snip, runs OCR (Ollama or HuggingFace),
and copies the resulting Markdown text to the clipboard.

Architecture:
  - Flow Launcher calls capture_and_ocr() in the main process.
  - That spawns a fully detached child process (worker) so the snip tool
    and the slow OCR call never block the launcher UI.
  - The worker captures the clipboard image, calls the chosen OCR backend,
    and shows a balloon notification with the result.
"""

import base64
import ctypes
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

# ---------------------------------------------------------------------------
# Plugin path setup – must happen before pyflowlauncher import
# ---------------------------------------------------------------------------
_PLUGIN_DIR = os.path.abspath(os.path.dirname(__file__))
for _p in (_PLUGIN_DIR,
           os.path.join(_PLUGIN_DIR, "lib"),
           os.path.join(_PLUGIN_DIR, "plugin")):
    sys.path.append(_p)

from pyflowlauncher import Plugin  # noqa: E402

# ---------------------------------------------------------------------------
# Logging – one timestamped file per process in %TEMP%
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
# Constants
# ---------------------------------------------------------------------------
BACKEND_OLLAMA = "ollama"
BACKEND_HF     = "huggingface"

# Ollama: local REST API (the CLI cannot handle images non-interactively)
OLLAMA_DEFAULT_URL = "http://localhost:11434"
OCR_MODEL_OLLAMA   = "glm-ocr"
OLLAMA_OCR_PROMPT  = "Text Recognition:"

# HuggingFace: serverless inference router
HF_ROUTER_URL = "https://router.huggingface.co/zai-org/api/paas/v4/layout_parsing"
OCR_MODEL_HF  = "zai-org/GLM-OCR"

# subprocess flags: no console window; fully detached for the worker process
_CREATE_NO_WINDOW      = 0x08000000
_DETACHED_CREATE_FLAGS = (
    _CREATE_NO_WINDOW
    | getattr(subprocess, "DETACHED_PROCESS", 0)
    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
)

if os.name != "nt":
    raise RuntimeError("This plugin requires Windows.")

# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _try_remove(path):
    """Delete a file, silently ignoring errors (e.g. already deleted)."""
    try:
        os.remove(path)
    except OSError:
        pass

# ---------------------------------------------------------------------------
# PowerShell helpers
# ---------------------------------------------------------------------------

def _run_powershell(script, *args, timeout=10):
    """Run a PowerShell one-liner silently. Returns CompletedProcess or None on timeout/error."""
    powershell_exe = os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"),
        "System32", "WindowsPowerShell", "v1.0", "powershell.exe",
    )
    try:
        result = subprocess.run(
            [powershell_exe, "-NoProfile", "-NonInteractive",
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


def _ps_quote(value):
    """Escape a value for use inside a PowerShell single-quoted string."""
    return str(value).replace("'", "''")

# ---------------------------------------------------------------------------
# Windows notifications
# ---------------------------------------------------------------------------

def _notify(title, message, level="info"):
    """
    Show a Windows balloon notification, falling back to a MessageBox.
    level: 'info' | 'warning' | 'error'
    """
    log.info("Notify [%s] %s - %s", level, title, message)
    icon = {"error": "Error", "warning": "Warning"}.get(level, "Info")

    t, m, i = _ps_quote(title), _ps_quote(message), _ps_quote(icon)

    # Try balloon tip first (non-blocking, nicer UX)
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

    # Fallback: modal MessageBox
    _run_powershell(
        "Add-Type -AssemblyName System.Windows.Forms;"
        f"$icon    = [System.Windows.Forms.MessageBoxIcon]::{i};"
        "$buttons = [System.Windows.Forms.MessageBoxButtons]::OK;"
        f"[void][System.Windows.Forms.MessageBox]::Show('{m}', '{t}', $buttons, $icon);",
        timeout=10,
    )

# ---------------------------------------------------------------------------
# Clipboard read/write
# ---------------------------------------------------------------------------

def _clear_clipboard():
    """Clear the Windows clipboard (removes any existing image before snipping)."""
    _run_powershell(
        "Add-Type -AssemblyName System.Windows.Forms;"
        "[Windows.Forms.Clipboard]::Clear();"
    )


def _clipboard_image_to_temp_png():
    """
    Read an image from the clipboard and save it as a temporary PNG file.

    Uses PowerShell MemoryStream -> base64 to avoid path-encoding issues with
    direct file saves from PowerShell on non-ASCII user profiles.

    Returns the file path on success, or None if the clipboard holds no image.
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
        log.error("PowerShell timeout accessing clipboard")
        _try_remove(tmp.name)
        return None
    if rc == 2:
        log.debug("Clipboard does not contain an image (yet)")
        _try_remove(tmp.name)
        return None
    if rc == 6:
        log.error("PowerShell: Save() to MemoryStream failed")
        _try_remove(tmp.name)
        return None
    if rc != 0:
        log.error("PowerShell error (returncode=%d)", rc)
        _try_remove(tmp.name)
        return None

    try:
        png_bytes = base64.b64decode(result.stdout.strip())
        if not png_bytes:
            log.error("Base64 decoded but empty")
            _try_remove(tmp.name)
            return None
        with open(tmp.name, "wb") as fh:
            fh.write(png_bytes)
        log.info("Clipboard image saved: %s (%d bytes)", tmp.name, len(png_bytes))
        return tmp.name
    except Exception as exc:
        log.error("Base64 decode/write error: %s", exc)
        _try_remove(tmp.name)
        return None


def _copy_text_to_clipboard(text):
    """
    Write Unicode text to the Windows clipboard using Win32 APIs directly.
    Using ctypes avoids a PowerShell round-trip and handles arbitrary text safely.
    """
    user32   = ctypes.WinDLL("user32",   use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    CF_UNICODETEXT = 13
    GHND           = 0x0042

    # Declare arg/return types for all functions we use (required for correct marshalling)
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

    encoded = text.encode("utf-16-le") + b"\x00\x00"  # null-terminate UTF-16LE

    if not user32.OpenClipboard(None):
        raise OSError("Cannot open clipboard")

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
        h_global = None  # ownership transferred to clipboard; must not free
    finally:
        if ptr:
            kernel32.GlobalUnlock(h_global)
        if h_global:  # free only if SetClipboardData never took ownership
            kernel32.GlobalFree(h_global)
        user32.CloseClipboard()

# ---------------------------------------------------------------------------
# Screen capture
# ---------------------------------------------------------------------------

def _capture_screen_region(timeout_seconds=60):
    """
    Open the Windows Snipping Tool (ms-screenclip:) and wait for the user to
    select a region. The tool places the result on the clipboard automatically.

    Polls the clipboard with exponential back-off to minimise CPU usage.
    Returns the path to a temporary PNG file, or None on timeout/cancel.
    """
    _clear_clipboard()
    try:
        os.startfile("ms-screenclip:")
    except OSError as exc:
        log.error("Cannot open ms-screenclip: %s", exc)
        return None

    deadline   = time.monotonic() + timeout_seconds
    sleep_time = 0.05   # start fast, ramp up
    max_sleep  = 0.5

    while time.monotonic() < deadline:
        image_path = _clipboard_image_to_temp_png()
        if image_path:
            log.info("Screen capture ready: %s", image_path)
            return image_path
        time.sleep(sleep_time)
        sleep_time = min(sleep_time * 1.3, max_sleep)

    log.warning("Clipboard wait timed out after %.1f s", timeout_seconds)
    return None

# ---------------------------------------------------------------------------
# OCR backend – HuggingFace
# ---------------------------------------------------------------------------

def _hf_has_error(payload):
    """Return True if the HF router JSON payload contains an error field (str or dict).
    The router returns HTTP 200 even for errors, so we must inspect the body."""
    if not isinstance(payload, dict):
        return False
    err = payload.get("error")
    return isinstance(err, dict) or (isinstance(err, str) and bool(err.strip()))


def _hf_error_message(payload, body=""):
    """Extract a human-readable error message from an HF router response."""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            return err.get("message") or body or "unknown error"
        if isinstance(err, str) and err.strip():
            return err.strip()
    return (body or "unknown error").strip()[:220]


def _hf_extract_text(payload):
    """
    Recursively extract OCR text from the HF router JSON response.
    Handles multiple response shapes: plain string, list of blocks,
    dict with common text keys, and OpenAI-style choices[].
    """
    if isinstance(payload, str):
        return payload.strip()

    if isinstance(payload, list):
        parts = [_hf_extract_text(item) for item in payload]
        return "\n".join(p for p in parts if p).strip()

    if isinstance(payload, dict):
        # Try common direct text keys first
        for key in ("text", "markdown", "output", "output_text", "content", "result"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, (dict, list)):
                nested = _hf_extract_text(value)
                if nested:
                    return nested

        # OpenAI-style: {"choices": [{"message": {"content": "..."}}]}
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(msg, dict):
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()

    return ""


def _ocr_huggingface(image_path, api_key):
    """
    Run OCR via the HuggingFace serverless inference router.

    Strategy:
      1. Try multipart/form-data with field names 'image' then 'file'
         (some router versions require a specific field name).
      2. Fall back to raw bytes with several Content-Type values.
         The router returns HTTP 200 even for errors, so we check the
         JSON body for an 'error' field rather than relying on status code.
    """
    import httpx

    log.debug("HF OCR: router=%s model=%s image=%s token=%s...",
              HF_ROUTER_URL, OCR_MODEL_HF, image_path, api_key[:6] if api_key else "")

    with open(image_path, "rb") as fh:
        image_bytes = fh.read()

    # Build a deduplicated MIME candidate list for the raw-bytes fallback
    guessed, _ = mimetypes.guess_type(image_path)
    mime_candidates = list(dict.fromkeys(
        m for m in ("image/png", guessed, "image/jpeg", "application/octet-stream") if m
    ))

    auth_header = {"Authorization": f"Bearer {api_key}"}
    status_code, body, payload = 0, "", None

    def _parse(resp):
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
        _parse(httpx.post(HF_ROUTER_URL, headers=auth_header, files=files, timeout=90.0))
        if status_code < 400 and not _hf_has_error(payload):
            break
        log.warning("HF multipart field '%s' failed (HTTP %s)", field_name, status_code)

    # 2) Raw bytes fallback, only if still failing
    if status_code >= 400 or _hf_has_error(payload):
        for content_type in mime_candidates:
            headers = {**auth_header, "Content-Type": content_type}
            _parse(httpx.post(HF_ROUTER_URL, headers=headers, content=image_bytes, timeout=90.0))
            if "content type" in body.lower() and "not supported" in body.lower():
                log.warning("HF router rejects Content-Type '%s', trying next", content_type)
                continue
            if status_code < 400 and not _hf_has_error(payload):
                break

    # Final error handling
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

def _ocr_ollama(image_path, base_url=None):
    """
    Run OCR via a local Ollama vision model using the REST API (/api/chat).

    Note: the Ollama CLI ('ollama run') is interactive-only and cannot accept
    image files as arguments in a non-TTY subprocess. The REST API is the
    correct programmatic interface.
    """
    import httpx

    ollama_url = (base_url or OLLAMA_DEFAULT_URL or "").strip() or OLLAMA_DEFAULT_URL

    log.debug("Ollama OCR: url=%s model=%s image=%s",
              ollama_url, OCR_MODEL_OLLAMA, image_path)

    with open(image_path, "rb") as fh:
        image_b64 = base64.b64encode(fh.read()).decode("utf-8")

    url = ollama_url.rstrip("/") + "/api/chat"
    body = {
        "model": OCR_MODEL_OLLAMA,
        "stream": False,
        "messages": [{"role": "user", "content": OLLAMA_OCR_PROMPT, "images": [image_b64]}],
    }

    try:
        response = httpx.post(url, json=body, timeout=120.0)
    except httpx.ConnectError:
        raise RuntimeError(
            f"Cannot connect to Ollama at {ollama_url}. "
            "Make sure the Ollama service is running."
        )

    log.debug("Ollama HTTP %s", response.status_code)
    if response.status_code != 200:
        raise RuntimeError(
            f"Ollama returned HTTP {response.status_code}: {response.text.strip()[:220]}"
        )

    data = response.json()
    # Response shape: {"message": {"role": "assistant", "content": "..."}}
    # Fallback to "response" key used by older Ollama versions.
    content = data.get("message", {}).get("content") or data.get("response") or ""
    return content.strip()

# ---------------------------------------------------------------------------
# OCR dispatcher
# ---------------------------------------------------------------------------

def _ocr_request(image_path, backend, hf_api_key, ollama_entrypoint=None):
    """Route the OCR request to the appropriate backend."""
    backend = (backend or BACKEND_OLLAMA).strip().lower()

    if backend == BACKEND_OLLAMA:
        return _ocr_ollama(image_path, base_url=ollama_entrypoint)

    if backend == BACKEND_HF:
        if not hf_api_key:
            raise ValueError(
                "HuggingFace API key not set. "
                "Add hf_api_key in plugin settings or set the HF_TOKEN env var."
            )
        if not hf_api_key.startswith("hf_"):
            raise ValueError("Invalid HuggingFace key: must start with 'hf_'.")
        return _ocr_huggingface(image_path, api_key=hf_api_key)

    raise ValueError(f"Unknown OCR backend: {backend!r}")

# ---------------------------------------------------------------------------
# Detached OCR worker (runs in the child process)
# ---------------------------------------------------------------------------

def _run_detached_ocr_worker(config):
    """
    Full OCR pipeline executed inside the detached child process:
    capture -> OCR -> copy to clipboard -> notify user.
    """
    image_path = _capture_screen_region()
    if not image_path:
        _notify("Screen OCR", "Capture cancelled: no area selected.", level="warning")
        return

    try:
        markdown = _ocr_request(
            image_path=image_path,
            backend=config.get("backend", BACKEND_OLLAMA),
            hf_api_key=config.get("hf_api_key", "").strip(),
            ollama_entrypoint=config.get("ollama_entrypoint", OLLAMA_DEFAULT_URL),
        )
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

def _spawn_detached_worker(config):
    """
    Launch a fully detached child process to run the OCR pipeline.
    Config is serialised to a temporary JSON file passed as a CLI argument,
    so no shared memory or IPC is needed.
    """
    fd, config_path = tempfile.mkstemp(prefix="screen-ocr-config-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "--detached-worker", config_path],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_DETACHED_CREATE_FLAGS,
            close_fds=True,
        )
    except Exception:
        log.exception("Failed to launch worker process")
        raise


def _handle_detached_worker_argv():
    """
    Detect whether this process was launched as a detached worker.
    If so, run the OCR pipeline and return True; otherwise return False.
    """
    if "--detached-worker" not in sys.argv:
        return False

    try:
        config_path = sys.argv[sys.argv.index("--detached-worker") + 1]
    except (ValueError, IndexError):
        return True  # flag present but no path; nothing to do

    config = {}
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            config = json.load(fh) or {}
    except (OSError, ValueError) as exc:
        log.warning("Failed to read worker config: %s", exc)
    finally:
        _try_remove(config_path)

    _run_detached_ocr_worker(config)
    return True

# ---------------------------------------------------------------------------
# Flow Launcher plugin
# ---------------------------------------------------------------------------

class ScreenOCR(Plugin):

    def __init__(self):
        super().__init__()
        self.on_method(self.query)
        self.on_method(self.context_menu)
        self.on_method(self.noop)
        self.on_method(self.capture_and_ocr)

    @staticmethod
    def _response(results):
        """Wrap results in the JSON-RPC envelope Flow Launcher expects."""
        return {"result": results}

    def _safe_settings(self):
        """Return plugin settings, defaulting to {} if Flow Launcher passes None."""
        return self.settings or {}

    def _backend(self):
        s = self._safe_settings()
        return (s.get("backend", BACKEND_OLLAMA) or BACKEND_OLLAMA).strip().lower()

    def _hf_api_key(self):
        """Read the HF key from plugin settings, falling back to the HF_TOKEN env var."""
        value = (self._safe_settings().get("hf_api_key") or "").strip()
        return value or os.environ.get("HF_TOKEN", "").strip()

    def _ollama_entrypoint(self):
        """Read Ollama base URL from plugin settings, with a safe default."""
        value = (self._safe_settings().get("ollama_entrypoint") or "").strip()
        return value or OLLAMA_DEFAULT_URL

    def query(self, query):
        backend = self._backend()
        backend_label = "Ollama (local)" if backend == BACKEND_OLLAMA else "HuggingFace (GLM-OCR)"
        results = []

        if backend == BACKEND_HF and not self._hf_api_key():
            results.append({
                "Title": "HuggingFace API key not set",
                "SubTitle": "Add hf_api_key in settings or set the HF_TOKEN env var",
                "IcoPath": "Images/app.png",
                "JsonRPCAction": {"method": "noop", "parameters": []},
            })

        results.append({
            "Title": "Capture screen region -> OCR -> Markdown",
            "SubTitle": f"Backend: {backend_label} · auto-copy to clipboard",
            "IcoPath": "Images/app.png",
            "JsonRPCAction": {
                "method": "capture_and_ocr",
                "parameters": [backend, self._hf_api_key()],
            },
        })
        return self._response(results)

    def context_menu(self, data):
        return self._response([{
            "Title": "Async OCR",
            "SubTitle": "Runs in a separate process and copies text to clipboard",
            "IcoPath": "Images/app.png",
            "JsonRPCAction": {"method": "noop", "parameters": []},
        }])

    def noop(self):
        return self._response([])

    def capture_and_ocr(self, backend=None, hf_api_key=None):
        try:
            _spawn_detached_worker({
                "backend":    (backend or self._backend() or BACKEND_OLLAMA).strip().lower(),
                "hf_api_key": hf_api_key if hf_api_key is not None else self._hf_api_key(),
                "ollama_entrypoint": self._ollama_entrypoint(),
            })
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