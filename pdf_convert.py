"""Convert a rendered .docx report to PDF.

Engines, in order of preference:
  * "word"        - Microsoft Word through COM (Windows with Word installed). Identical to
                    what Word shows/prints, so the preview is exact.
  * "libreoffice" - LibreOffice headless (Linux servers / Docker). Very close, but complex
                    floating shapes can be placed slightly differently from Word.

Set PDF_ENGINE=word|libreoffice to force one; LIBREOFFICE_PATH to point at soffice.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

CONVERSION_TIMEOUT_SECONDS = int(os.getenv("PDF_CONVERSION_TIMEOUT", "120"))

# Word automation is not safe to drive from several threads at once.
_word_lock = threading.Lock()


class ConversionUnavailable(RuntimeError):
    """No PDF engine is installed on this machine."""


def _word_available():
    if sys.platform != "win32":
        return False
    try:
        import win32com.client  # noqa: F401
    except ImportError:
        return False
    try:
        import winreg

        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "Word.Application"))
        return True
    except OSError:
        return False


def _soffice_path():
    configured = os.getenv("LIBREOFFICE_PATH")
    if configured and Path(configured).exists():
        return configured
    # The service's PATH may only contain the virtualenv, so also look in the system folders.
    search_path = os.pathsep.join([os.environ.get("PATH", ""), "/usr/local/bin", "/usr/bin", "/bin", "/snap/bin"])
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name, path=search_path)
        if found:
            return found
    for candidate in (
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/usr/lib/libreoffice/program/soffice",
        "/opt/libreoffice/program/soffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ):
        if Path(candidate).exists():
            return candidate
    return None


def available_engine():
    """Name of the engine that will be used, or None."""
    forced = (os.getenv("PDF_ENGINE") or "").strip().lower()
    if forced == "word":
        return "word" if _word_available() else None
    if forced == "libreoffice":
        return "libreoffice" if _soffice_path() else None
    if _word_available():
        return "word"
    if _soffice_path():
        return "libreoffice"
    return None


def _convert_with_word(source, target):
    import pythoncom
    import win32com.client

    with _word_lock:
        pythoncom.CoInitialize()
        word = None
        try:
            word = win32com.client.DispatchEx("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            document = word.Documents.Open(
                str(source), ConfirmConversions=False, ReadOnly=True, AddToRecentFiles=False, Visible=False
            )
            try:
                document.SaveAs2(str(target), FileFormat=17)  # wdFormatPDF
            finally:
                document.Close(SaveChanges=0)
        finally:
            if word is not None:
                try:
                    word.Quit(SaveChanges=0)
                except Exception:
                    pass
            pythoncom.CoUninitialize()


def _libreoffice_env(workdir):
    """Environment for soffice.

    The systemd service limits PATH to the virtualenv, but LibreOffice's start script needs the
    normal system tools, and the service user (www-data) has no writable home directory.
    """
    env = dict(os.environ)
    system_paths = ["/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin"]
    current = [p for p in env.get("PATH", "").split(os.pathsep) if p]
    if os.name != "nt":
        env["PATH"] = os.pathsep.join(current + [p for p in system_paths if p not in current])
        env["HOME"] = str(workdir)
    return env


_libreoffice_lock = threading.Lock()


def _libreoffice_profile_dir():
    """One LibreOffice profile per server worker process, reused for every conversion.

    Creating a fresh profile for each conversion is slow (seconds) and writes tens of MB of
    temporary files. Each gunicorn worker is its own process with its own profile; threads in a
    worker take turns (a profile can only be used by one LibreOffice at a time).
    """
    root = Path(os.getenv("LIBREOFFICE_PROFILE_DIR") or Path(tempfile.gettempdir(), "dsumarry-libreoffice"))
    profile = root / f"profile-{os.getpid()}"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def _convert_with_libreoffice(source, target, workdir):
    soffice = _soffice_path()
    profile = _libreoffice_profile_dir().resolve().as_uri()
    with _libreoffice_lock:
        _run_libreoffice(soffice, profile, source, target, workdir)


def _run_libreoffice(soffice, profile, source, target, workdir):
    result = subprocess.run(
        [
            soffice,
            "--headless",
            "--norestore",
            "--nolockcheck",
            f"-env:UserInstallation={profile}",
            "--convert-to",
            "pdf:writer_pdf_Export",
            "--outdir",
            str(target.parent),
            str(source),
        ],
        capture_output=True,
        timeout=CONVERSION_TIMEOUT_SECONDS,
        check=False,
        env=_libreoffice_env(workdir),
    )
    if result.returncode != 0 or not target.exists():
        message = (result.stderr or result.stdout or b"").decode("utf8", "ignore").strip()
        raise RuntimeError(f"LibreOffice could not convert the document: {message or 'unknown error'}")


def docx_to_pdf(docx_bytes):
    engine = available_engine()
    if engine is None:
        raise ConversionUnavailable(
            "PDF preview is not available on this server (install Microsoft Word or LibreOffice). "
            "You can still download the Word file."
        )

    with tempfile.TemporaryDirectory(prefix="report-") as workdir:
        source = Path(workdir, "report.docx")
        target = Path(workdir, "report.pdf")
        source.write_bytes(docx_bytes)
        convert = _convert_with_word if engine == "word" else (
            lambda src, dst: _convert_with_libreoffice(src, dst, workdir)
        )
        try:
            convert(source, target)
        except Exception:
            # One retry: a conversion can fail on a transient problem (busy file, slow start).
            target.unlink(missing_ok=True)
            convert(source, target)
        return target.read_bytes()
