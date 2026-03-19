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
import http.client
import os
import subprocess
import sys
import tempfile
import time
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


def _http_post_multipart(url, headers, field_name, file_name, file_bytes, file_mime, timeout):
    """POST multipart/form-data with one file field and return HTTP tuple."""
    boundary = "----FlowOCRBoundary" + os.urandom(12).hex()
    boundary_bytes = boundary.encode("ascii")

    body = bytearray()
    body.extend(b"--" + boundary_bytes + b"\r\n")
    disposition = (
        f'Content-Disposition: form-data; name="{field_name}"; filename="{file_name}"\r\n'
    )
    body.extend(disposition.encode("utf-8"))
    body.extend(f"Content-Type: {file_mime}\r\n\r\n".encode("ascii"))
    body.extend(file_bytes)
    body.extend(b"\r\n--" + boundary_bytes + b"--\r\n")

    final_headers = {
        **(headers or {}),
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    return _http_post_bytes(url, final_headers, bytes(body), timeout)

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
    status_code, body, resp_payload = _http_post_json(
        HF_ROUTER_URL,
        headers=headers,
        payload=payload,
        timeout=90.0,
    )
    log.debug("HF response: HTTP %s, body=%s", status_code, body[:200])

    if status_code == 401:
        raise RuntimeError(
            "HuggingFace authentication failed (401). "
            "Check hf_api_key / HF_TOKEN and token permissions."
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

    Root cause: glm-ocr runs on CPU (~2.1 GiB RAM). While loading, Ollama
    resets every TCP connection. When we previously used Popen (non-blocking),
    the CLI runner and the plugin's REST calls competed for the same runner,
    causing resets even after /api/ps showed the model as loaded.

    Fix: call `ollama run <model>` with empty stdin and BLOCK until it exits.
    The CLI exits once the model is loaded and ready, then the REST server is
    free to accept our request. A 2 s grace period follows before returning.
    """
    import urllib.request
    import shutil

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
            return
        except subprocess.TimeoutExpired:
            log.warning("'ollama run' timed out after %ds; falling back to polling", timeout)
        except Exception as exc:
            log.warning("ollama CLI failed (%s); falling back to /api/ps polling", exc)
    else:
        log.warning("'ollama' not found on PATH; falling back to /api/ps polling")

    # Fallback: poll /api/ps
    import urllib.request
    ps_url = base_url.rstrip("/") + "/api/ps"
    deadline = time.monotonic() + timeout
    model_base = model.split(":")[0]
    log.debug("Polling /api/ps until '%s' is loaded ...", model_base)
    while time.monotonic() < deadline:
        time.sleep(2)
        try:
            with urllib.request.urlopen(ps_url, timeout=5) as resp:
                ps_data = json.loads(resp.read().decode("utf-8", errors="replace"))
            loaded = [m.get("name", "") for m in ps_data.get("models", [])]
            log.debug("Ollama /api/ps loaded: %s", loaded)
            if any(model_base in name for name in loaded):
                log.debug("Model '%s' ready (polling fallback).", model)
                time.sleep(2)
                return
        except Exception as exc:
            log.debug("Polling error: %s", exc)

    log.warning("Timed out waiting for '%s'; attempting OCR anyway", model)



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

    # Step 1: trigger model loading and poll until ready
    _ollama_wait_until_ready(ollama_url, OCR_MODEL_OLLAMA, timeout=180)

    # Give the runner a few extra seconds to finish initialising after appearing
    # in /api/ps — on CPU-only setups the runner reports ready slightly before
    # it can actually serve requests with large image payloads.
    log.debug("Waiting 10 s for runner to fully initialise ...")
    time.sleep(10)

    # Step 2: real OCR request via urllib (handles large payloads better than
    # http.client with a single socket timeout)
    import urllib.request
    import urllib.error

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
            with urllib.request.urlopen(req, timeout=300) as resp:
                response_text = resp.read().decode("utf-8", errors="replace")
            data = json.loads(response_text)
            break
        except Exception as exc:
            last_exc = exc
            log.warning("OCR attempt %d failed, retrying in 5s: %s", attempt, exc)
            time.sleep(5)
    else:
        raise RuntimeError(
            f"Ollama failed after 4 attempts. Last error: {last_exc}"
        )

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