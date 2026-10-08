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
import hashlib
import http.client
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from urllib.parse import urlsplit

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
    "flowocr_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".log",
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
OCR_MODEL_HF  = "glm-ocr"

# subprocess flags: no console window; fully detached for the worker process
_CREATE_NO_WINDOW      = 0x08000000
_DETACHED_CREATE_FLAGS = (
    _CREATE_NO_WINDOW
    | getattr(subprocess, "DETACHED_PROCESS", 0)
    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
)

if os.name != "nt":
    raise RuntimeError("This plugin requires Windows.")

# Lock file – prevents concurrent OCR workers from stepping on each other
_LOCK_PATH = os.path.join(tempfile.gettempdir(), "screen-ocr-worker.lock")

# Tracks the SHA-256 of the last processed image to skip duplicates
_LAST_IMAGE_HASH_PATH = os.path.join(tempfile.gettempdir(), "screen-ocr-last-hash.txt")

# ---------------------------------------------------------------------------
# Startup: purge log files older than 2 days to avoid accumulation
# ---------------------------------------------------------------------------
def _purge_old_logs():
    tmp = tempfile.gettempdir()
    cutoff = time.time() - 2 * 86400
    try:
        entries = os.listdir(tmp)
    except OSError:
        return
    for name in entries:
        if name.startswith("flowocr_") and name.endswith(".log"):
            path = os.path.join(tmp, name)
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
            except OSError:
                pass

_purge_old_logs()

# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _try_remove(path):
    """Delete a file, silently ignoring errors (e.g. already deleted)."""
    try:
        os.remove(path)
    except OSError:
        pass


def _http_post_bytes(url, headers, data, timeout):
    """
    POST raw bytes and return (status_code, text_body, json_payload_or_none).

    Uses the low-level putrequest/putheader/endheaders API instead of
    conn.request() to prevent http.client from silently appending
    ';charset=UTF-8' to the Content-Type header — which would turn
    'image/jpeg' into 'image/jpeg;charset=UTF-8' and cause the HF router
    to reject the request with a 'Content type not supported' error.
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = parts.hostname
    if not host:
        raise ConnectionError(f"Invalid URL: {url}")

    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    if scheme == "https":
        conn = http.client.HTTPSConnection(host, parts.port or 443, timeout=timeout)
    elif scheme == "http":
        conn = http.client.HTTPConnection(host, parts.port or 80, timeout=timeout)
    else:
        raise ConnectionError(f"Unsupported URL scheme: {parts.scheme!r}")

    try:
        # Build the request manually so no automatic header injection occurs
        conn.putrequest("POST", path, skip_accept_encoding=True)
        conn.putheader("Host", host)
        conn.putheader("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        conn.endheaders()
        conn.send(data)

        resp = conn.getresponse()
        status = resp.status
        raw = resp.read() or b""
    except OSError as exc:
        raise ConnectionError(str(exc)) from exc
    finally:
        try:
            conn.close()
        except Exception:
            pass

    body = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    return status, body, payload


def _http_post_json(url, headers, payload, timeout):
    """POST a JSON payload and return (status_code, text_body, json_payload_or_none)."""
    final_headers = {"Content-Type": "application/json", **(headers or {})}
    data = json.dumps(payload).encode("utf-8")
    return _http_post_bytes(url, final_headers, data, timeout)


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

# ---------------------------------------------------------------------------
# Windows notifications – pure ctypes, no PowerShell round-trip
# ---------------------------------------------------------------------------

# ctypes structures for Shell_NotifyIconW
_NIIF_INFO    = 0x00000001
_NIIF_WARNING = 0x00000002
_NIIF_ERROR   = 0x00000003
_NIF_MESSAGE  = 0x00000001
_NIF_ICON     = 0x00000002
_NIF_TIP      = 0x00000004
_NIF_INFO     = 0x00000010
_NIM_ADD      = 0x00000000
_NIM_MODIFY   = 0x00000001
_NIM_DELETE   = 0x00000002
_WM_USER      = 0x0400

class _NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize",           wintypes.DWORD),
        ("hWnd",             wintypes.HWND),
        ("uID",              wintypes.UINT),
        ("uFlags",           wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon",            wintypes.HICON),
        ("szTip",            wintypes.WCHAR * 128),
        ("dwState",          wintypes.DWORD),
        ("dwStateMask",      wintypes.DWORD),
        ("szInfo",           wintypes.WCHAR * 256),
        ("uVersion",         wintypes.UINT),
        ("szInfoTitle",      wintypes.WCHAR * 64),
        ("dwInfoFlags",      wintypes.DWORD),
    ]

def _notify(title, message, level="info", block=False):
    """
    Show a Windows tray balloon notification using Shell_NotifyIconW.
    If block is False, runs in a background daemon thread so OCR inference isn't delayed.
    If block is True, waits up to 2 seconds for the balloon to display before returning.
    """
    log.info("Notify [%s] %s – %s", level, title, message)

    def _worker():
        try:
            niif = {
                "error":   _NIIF_ERROR,
                "warning": _NIIF_WARNING,
            }.get(level, _NIIF_INFO)

            # Load a stock icon matching the level
            _IDI = {"error": 32513, "warning": 32515}.get(level, 32516)  # IDI_ERROR/WARNING/INFO
            shell32  = ctypes.windll.shell32
            user32   = ctypes.windll.user32
            hIcon = user32.LoadIconW(None, ctypes.c_int(_IDI))

            nid = _NOTIFYICONDATAW()
            nid.cbSize      = ctypes.sizeof(_NOTIFYICONDATAW)
            nid.hWnd        = user32.GetDesktopWindow()
            nid.uID         = 0xF10C  # arbitrary unique ID for this plugin
            nid.uFlags      = _NIF_ICON | _NIF_TIP | _NIF_INFO
            nid.hIcon       = hIcon
            nid.szTip       = "Screen OCR"[:127]
            nid.szInfoTitle = title[:63]
            nid.szInfo      = message[:255]
            nid.dwInfoFlags = niif

            shell32.Shell_NotifyIconW(_NIM_ADD,    ctypes.byref(nid))
            time.sleep(3)
            shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(nid))
        except Exception as exc:
            log.warning("Notification failed: %s", exc)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    if block:
        t.join(timeout=2.0)


# ---------------------------------------------------------------------------
# Clipboard & Win32 – ctypes declarations at module level (avoids re-declaring per call)
# ---------------------------------------------------------------------------
_user32   = ctypes.WinDLL("user32",   use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

_CF_BITMAP      = 2
_CF_DIB         = 8
_CF_UNICODETEXT = 13
_CF_DIBV5       = 17
_GHND           = 0x0042

_user32.OpenClipboard.argtypes              = [wintypes.HWND]
_user32.OpenClipboard.restype               = wintypes.BOOL
_user32.EmptyClipboard.argtypes             = []
_user32.EmptyClipboard.restype              = wintypes.BOOL
_user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
_user32.IsClipboardFormatAvailable.restype  = wintypes.BOOL
_user32.SetClipboardData.argtypes           = [wintypes.UINT, wintypes.HANDLE]
_user32.SetClipboardData.restype            = wintypes.HANDLE
_user32.CloseClipboard.argtypes             = []
_user32.CloseClipboard.restype              = wintypes.BOOL
_kernel32.GlobalAlloc.argtypes              = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalAlloc.restype               = wintypes.HGLOBAL
_kernel32.GlobalLock.argtypes               = [wintypes.HGLOBAL]
_kernel32.GlobalLock.restype                = wintypes.LPVOID
_kernel32.GlobalUnlock.argtypes             = [wintypes.HGLOBAL]
_kernel32.GlobalUnlock.restype              = wintypes.BOOL
_kernel32.GlobalFree.argtypes               = [wintypes.HGLOBAL]
_kernel32.GlobalFree.restype                = wintypes.HGLOBAL

_kernel32.OpenProcess.argtypes              = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype               = wintypes.HANDLE
_kernel32.GetExitCodeProcess.argtypes       = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetExitCodeProcess.restype        = wintypes.BOOL
_kernel32.CloseHandle.argtypes              = [wintypes.HANDLE]
_kernel32.CloseHandle.restype               = wintypes.BOOL


def _is_pid_running(pid):
    """Check if a process with given PID is currently active on Windows."""
    if pid <= 0:
        return False
    handle = _kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if _kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return exit_code.value == 259  # 259 is STILL_ACTIVE
        return True
    finally:
        _kernel32.CloseHandle(handle)


def _image_hash(path):
    """Return a short SHA-256 hex digest of a file's contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_duplicate_image(path):
    """Return True if this image was already processed in the last run."""
    current = _image_hash(path)
    try:
        with open(_LAST_IMAGE_HASH_PATH) as f:
            if f.read().strip() == current:
                return True
    except OSError:
        pass
    try:
        with open(_LAST_IMAGE_HASH_PATH, "w") as f:
            f.write(current)
    except OSError:
        pass
    return False


def _clear_clipboard():
    """Clear the Windows clipboard using native Win32 APIs (no PowerShell overhead)."""
    for _ in range(5):
        if _user32.OpenClipboard(None):
            try:
                _user32.EmptyClipboard()
                return
            finally:
                _user32.CloseClipboard()
        time.sleep(0.02)
    log.warning("Failed to open clipboard to clear it.")


def _has_clipboard_image():
    """Fast check whether clipboard contains bitmap data before launching PowerShell."""
    return bool(
        _user32.IsClipboardFormatAvailable(_CF_DIB)
        or _user32.IsClipboardFormatAvailable(_CF_BITMAP)
        or _user32.IsClipboardFormatAvailable(_CF_DIBV5)
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
        "try {"
        "  $img = [Windows.Forms.Clipboard]::GetImage();"
        "  if ($img -eq $null) { exit 2 };"
        "  $ms = New-Object System.IO.MemoryStream;"
        "  try {"
        "    $img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png);"
        "    $b64 = [Convert]::ToBase64String($ms.ToArray());"
        "    Write-Output $b64;"
        "  } finally {"
        "    try { $ms.Dispose() } catch { };"
        "    try { $img.Dispose() } catch { };"
        "  }"
        "  exit 0;"
        "} catch {"
        "  Write-Error $_;"
        "  exit 6;"
        "}",
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
        log.error("PowerShell: Failed to process clipboard image")
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
    """Write Unicode text to the Windows clipboard using pre-declared Win32 APIs."""
    encoded = text.encode("utf-16-le") + b"\x00\x00"  # null-terminate UTF-16LE

    if not _user32.OpenClipboard(None):
        raise OSError("Cannot open clipboard")

    h_global = None
    ptr = None
    try:
        _user32.EmptyClipboard()
        h_global = _kernel32.GlobalAlloc(_GHND, len(encoded))
        if not h_global:
            raise OSError("GlobalAlloc failed")
        ptr = _kernel32.GlobalLock(h_global)
        if not ptr:
            raise OSError("GlobalLock failed")
        ctypes.memmove(ptr, encoded, len(encoded))
        _kernel32.GlobalUnlock(h_global)
        ptr = None
        if not _user32.SetClipboardData(_CF_UNICODETEXT, h_global):
            raise OSError("SetClipboardData failed")
        h_global = None  # ownership transferred to clipboard
    finally:
        if ptr:
            _kernel32.GlobalUnlock(h_global)
        if h_global:
            _kernel32.GlobalFree(h_global)
        _user32.CloseClipboard()

# ---------------------------------------------------------------------------
# Screen capture
# ---------------------------------------------------------------------------

def _capture_screen_region(timeout_seconds=60):
    """
    Open the Windows Snipping Tool (ms-screenclip:) and wait for the user to
    select a region. The tool places the result on the clipboard automatically.

    Fast Win32 clipboard format polling is used before invoking PowerShell
    to extract the PNG, saving CPU and eliminating process-spawn lag.
    Returns the path to a temporary PNG file, or None on timeout/cancel.
    """
    _clear_clipboard()
    time.sleep(0.05)
    try:
        os.startfile("ms-screenclip:")
    except OSError as exc:
        log.error("Cannot open ms-screenclip: %s", exc)
        return None

    deadline   = time.monotonic() + timeout_seconds
    sleep_time = 0.05   # start fast, ramp up
    max_sleep  = 0.3

    while time.monotonic() < deadline:
        if _has_clipboard_image():
            image_path = _clipboard_image_to_temp_png()
            if image_path:
                log.info("Screen capture ready: %s", image_path)
                return image_path
        time.sleep(sleep_time)
        sleep_time = min(sleep_time * 1.2, max_sleep)

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
    Extract OCR text from the GLM-OCR layout_parsing response.

    Primary format returned by the API:
        {
          "layout_details": [          # one entry per page
            [                          # one entry per detected block
              {"bbox_2d": [...], "content": "<markdown text>"},
              ...
            ]
          ]
        }

    All block content strings are joined in document order with a blank line
    between blocks. Falls back to a generic scan for other response shapes.
    """
    if isinstance(payload, str):
        return payload.strip()

    if isinstance(payload, dict):
        # Primary: GLM-OCR layout_details[[{content}]]
        layout_details = payload.get("layout_details")
        if isinstance(layout_details, list):
            parts = []
            for page in layout_details:
                if not isinstance(page, list):
                    continue
                for block in page:
                    if isinstance(block, dict):
                        text = (block.get("content") or "").strip()
                        if text:
                            parts.append(text)
            if parts:
                return "\n\n".join(parts)

        # Fallback: common direct text keys
        for key in ("text", "markdown", "output", "output_text", "content", "result"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, (dict, list)):
                nested = _hf_extract_text(value)
                if nested:
                    return nested

        # Fallback: OpenAI-style choices[]
        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            msg = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(msg, dict):
                text = (msg.get("content") or "").strip()
                if text:
                    return text

    if isinstance(payload, list):
        parts = [_hf_extract_text(item) for item in payload]
        return "\n".join(p for p in parts if p).strip()

    return ""


def _ocr_huggingface(image_path, api_key):
    """
    Run OCR via the HuggingFace serverless inference router (GLM-OCR / MaaS).

    The router's upstream API requires the image as a JSON body with a
    'file' field containing a data URI:

        POST /zai-org/api/paas/v4/layout_parsing
        Authorization: Bearer <token>
        Content-Type: application/json

        {"file": "data:image/png;base64,<b64>", "model": "glm-ocr"}

    Despite the advertised curl example using raw bytes + Content-Type: image/jpeg,
    the CloudFront/nginx proxy in front of the inference backend appends
    ';charset=UTF-8' to any bare Content-Type, turning 'image/jpeg' into
    'image/jpeg;charset=UTF-8', which the backend then rejects (HTTP 200 + error body).
    Sending the image as a data URI inside a JSON payload bypasses this entirely.
    """
    log.debug("HF OCR: router=%s model=%s image=%s token=%s...",
              HF_ROUTER_URL, OCR_MODEL_HF, image_path, api_key[:6] if api_key else "")

    with open(image_path, "rb") as fh:
        raw_bytes = fh.read()

    # Encode as a data URI – the MaaS API accepts data:<mime>;base64,<data>
    b64 = base64.b64encode(raw_bytes).decode("ascii")
    data_uri = f"data:image/png;base64,{b64}"

    payload = {"file": data_uri, "model": OCR_MODEL_HF}
    headers = {"Authorization": f"Bearer {api_key}"}

    log.debug("HF POST JSON: image %d bytes as data URI", len(raw_bytes))

    # Use shorter timeout for initial request to fail fast on auth errors
    status_code, body, resp_payload = _http_post_json(
        HF_ROUTER_URL,
        headers=headers,
        payload=payload,
        timeout=30.0,  # Reduced from 90 to 30 seconds for faster failure
    )
    log.debug("HF response: HTTP %s, body=%s", status_code, body[:200])

    if status_code == 401:
        raise RuntimeError(
            "HuggingFace authentication failed (401). "
            "Check hf_api_key / HF_TOKEN and token permissions."
        )
    if status_code == 403:
        raise RuntimeError(
            "HuggingFace authorization failed (403). "
            "Your token may not have access to this model."
        )
    if status_code >= 400 or _hf_has_error(resp_payload):
        raise RuntimeError(f"HF router error: {_hf_error_message(resp_payload, body)}")

    text = _hf_extract_text(resp_payload if resp_payload is not None else body)
    if text:
        return text
    raise RuntimeError(
        f"Empty or unrecognised OCR response from HF router: {body.strip()[:220]}"
    )
# ---------------------------------------------------------------------------
# OCR backend – Ollama (local)
# ---------------------------------------------------------------------------

def _ollama_wait_until_ready(base_url, model, timeout=180):
    """
    Load the model and wait until Ollama is ready to serve REST requests.
    Returns True if the model was already loaded (warm), False if it had to be loaded (cold).

    Root cause: glm-ocr runs on CPU (~2.1 GiB RAM). While loading, Ollama
    resets every TCP connection. The CLI uses internal IPC and survives.
    We block on `ollama run <model>` with empty stdin; the CLI exits once
    the model is fully loaded and ready. A 2 s grace period follows.
    """
    # Check if already loaded via /api/ps before doing anything
    ps_url = base_url.rstrip("/") + "/api/ps"
    model_base = model.split(":")[0]

    def _is_loaded():
        try:
            with urllib.request.urlopen(ps_url, timeout=5) as resp:
                ps_data = json.loads(resp.read().decode("utf-8", errors="replace"))
            loaded = [m.get("name", "") for m in ps_data.get("models", [])]
            return any(model_base in name for name in loaded)
        except Exception:
            return False

    def _is_ollama_running():
        """Quick check if Ollama server is responding at all."""
        try:
            with urllib.request.urlopen(base_url.rstrip("/") + "/api/tags", timeout=3) as resp:
                return resp.status == 200
        except Exception:
            return False

    if _is_loaded():
        log.debug("Model '%s' already in memory — skipping warm-up.", model)
        return True  # warm

    # Check if Ollama is even running before attempting to load the model
    if not _is_ollama_running():
        raise RuntimeError(
            "Ollama server is not running. "
            "Start it with 'ollama serve' or check your ollama_entrypoint setting."
        )

    ollama_exe = shutil.which("ollama") or "ollama"
    if shutil.which("ollama"):
        try:
            log.debug("Blocking on 'ollama run %s' until model is loaded ...", model)
            proc = subprocess.run(
                [ollama_exe, "run", model],
                input=b"",            # empty stdin → CLI loads model then exits
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                creationflags=_CREATE_NO_WINDOW,
            )
            log.debug("'ollama run' exited (code %d); waiting 2 s grace ...", proc.returncode)
            time.sleep(2)
            return False  # cold start
        except subprocess.TimeoutExpired:
            log.warning("'ollama run' timed out after %ds; falling back to polling", timeout)
        except Exception as exc:
            log.warning("ollama CLI failed (%s); falling back to /api/ps polling", exc)
    else:
        log.warning("'ollama' not found on PATH; falling back to /api/ps polling")

    # Fallback: poll /api/ps with shorter timeout
    deadline = time.monotonic() + min(timeout, 60)  # Cap polling at 60 seconds
    log.debug("Polling /api/ps until '%s' is loaded ...", model_base)
    while time.monotonic() < deadline:
        time.sleep(2)
        if _is_loaded():
            log.debug("Model '%s' ready (polling fallback).", model)
            time.sleep(2)
            return False  # cold start via fallback
        log.debug("Ollama /api/ps: model not yet loaded")

    raise RuntimeError(
        f"Timed out waiting for Ollama model '{model}' to load. "
        "Ensure Ollama is running and the model is available."
    )



def _ocr_ollama(image_path, base_url=None):
    """
    Run OCR via a local Ollama vision model using the REST API (/api/chat).

    glm-ocr runs on CPU (~2.1 GiB RAM). During cold-start Ollama resets every
    TCP connection while loading the model. We trigger loading via the CLI,
    poll /api/ps until the model is ready, then send the OCR request.
    A short retry loop handles the rare race where the model just appeared in
    /api/ps but the runner isn't fully accepting requests yet.
    """
    ollama_url = (base_url or OLLAMA_DEFAULT_URL or "").strip() or OLLAMA_DEFAULT_URL

    log.debug("Ollama OCR: url=%s model=%s image=%s",
              ollama_url, OCR_MODEL_OLLAMA, image_path)

    with open(image_path, "rb") as fh:
        image_b64 = base64.b64encode(fh.read()).decode("utf-8")

    # Step 1: trigger model loading and poll until ready.
    # Returns True if the model was already loaded (no wait needed).
    already_loaded = _ollama_wait_until_ready(ollama_url, OCR_MODEL_OLLAMA, timeout=180)

    # Give the runner extra time only on a cold start. If the model was already
    # in memory the runner is immediately ready — no sleep needed.
    if not already_loaded:
        log.debug("Cold start detected, waiting 10 s for runner to stabilise ...")
        time.sleep(10)

    # Step 2: real OCR request via urllib (handles large payloads better than
    # http.client with a single socket timeout)
    url = ollama_url.rstrip("/") + "/api/chat"
    body = {
        "model": OCR_MODEL_OLLAMA,
        "stream": False,
        "messages": [{"role": "user", "content": OLLAMA_OCR_PROMPT, "images": [image_b64]}],
    }
    payload_bytes = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload_bytes,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    last_exc = None
    for attempt in range(1, 5):
        try:
            log.debug("OCR attempt %d ...", attempt)
            with urllib.request.urlopen(req, timeout=60) as resp:  # Reduced from 300 to 60 seconds
                response_text = resp.read().decode("utf-8", errors="replace")
            data = json.loads(response_text)
            break
        except urllib.error.URLError as exc:
            last_exc = exc
            log.warning("OCR attempt %d failed (connection error), retrying in 5s: %s", attempt, exc)
            time.sleep(5)
        except Exception as exc:
            last_exc = exc
            log.warning("OCR attempt %d failed, retrying in 5s: %s", attempt, exc)
            time.sleep(5)
    else:
        error_msg = f"Ollama failed after 4 attempts"
        if last_exc:
            error_msg += f". Last error: {last_exc}"
        raise RuntimeError(error_msg)

    log.debug("Ollama response received (%d bytes)", len(response_text))
    if not isinstance(data, dict):
        raise RuntimeError(
            f"Invalid JSON from Ollama: {response_text.strip()[:220]}"
        )
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

    Guards:
      - Lock file with PID check: only one worker runs at a time. If another is
        actively running, the new request is ignored. If a previous worker died,
        the lock is cleared immediately without waiting 5 minutes.
      - Duplicate image: if the clipboard contains the same image as the last
        processed one, the request is skipped to avoid re-running OCR on a
        stale clipboard.
    """
    # --- lock: one worker at a time with PID check ---
    if os.path.exists(_LOCK_PATH):
        locked_pid = None
        try:
            with open(_LOCK_PATH, "r", encoding="utf-8") as f:
                content = f.read().strip()
            locked_pid = int(content) if content.isdigit() else None
        except Exception:
            locked_pid = None

        try:
            age = time.time() - os.path.getmtime(_LOCK_PATH)
        except OSError:
            age = 0

        if locked_pid and _is_pid_running(locked_pid):
            if age < 300:
                log.info("Worker PID %d is still running (lock age %.0fs), exiting.", locked_pid, age)
                _notify("Screen OCR", "Already processing a request…", level="warning", block=True)
                return
            log.warning("Worker PID %d exceeded 300s, assuming stale.", locked_pid)
        else:
            log.info("Previous worker PID %s is not active (lock age %.0fs), overriding lock.", locked_pid, age)

    try:
        with open(_LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass

    try:
        image_path = _capture_screen_region()
        if not image_path:
            _notify("Screen OCR", "Capture cancelled: no area selected.", level="warning", block=True)
            return

        # --- duplicate image guard ---
        if _is_duplicate_image(image_path):
            log.info("Image is identical to last processed one, skipping.")
            _notify("Screen OCR", "Same image as last time — skipped.", level="warning", block=True)
            _try_remove(image_path)
            return

        # Non-blocking notification so OCR request starts immediately
        _notify("Screen OCR", "Processing… please wait.", level="info", block=False)

        try:
            markdown = _ocr_request(
                image_path=image_path,
                backend=config.get("backend", BACKEND_OLLAMA),
                hf_api_key=config.get("hf_api_key", "").strip(),
                ollama_entrypoint=config.get("ollama_entrypoint", OLLAMA_DEFAULT_URL),
            )
        except Exception as exc:
            log.exception("OCR failed")
            _notify("Screen OCR – Error", f"OCR failed: {str(exc)[:180]}", level="error", block=True)
            return
        finally:
            _try_remove(image_path)

        text = (markdown or "").strip()
        if not text:
            _notify("Screen OCR", "No text detected.", level="warning", block=True)
            return

        try:
            _copy_text_to_clipboard(markdown)
        except Exception as exc:
            log.exception("Clipboard write failed")
            _notify("Screen OCR – Error", f"Clipboard write failed: {str(exc)[:180]}", level="error", block=True)
            return

        _notify("Screen OCR", f"Done – {len(text)} characters copied to clipboard.", block=True)

    finally:
        _try_remove(_LOCK_PATH)

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