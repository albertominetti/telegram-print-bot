#!/usr/bin/env python3
"""
Telegram → PDF → Windows Shared Printer Bot
User sends a PDF → bot asks to confirm → prints on button tap.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import img2pdf
import pikepdf
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

# ── Configuration ────────────────────────────────────────────────────────────

# Load config.env into the process if present (values already set in the
# environment — e.g. by systemd — take precedence).
_CONFIG_ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.env")


def _load_config_env(path: str) -> None:
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ[key] = value


_load_config_env(_CONFIG_ENV)


def _csv_env(name: str) -> tuple[str, ...]:
    return tuple(
        part.strip()
        for part in (os.environ.get(name) or "").split(",")
        if part.strip()
    )


def _int_env(name: str, default: int, *, minimum: int = 0) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


# CUPS queue names from PRINTERS=name1,name2
PRINTERS = _csv_env("PRINTERS")
_default_printer = (os.environ.get("DEFAULT_PRINTER") or "").strip()
DEFAULT_PRINTER = _default_printer or (PRINTERS[0] if PRINTERS else None)

USE_SAMBA = (os.environ.get("USE_SAMBA") or "").lower() in {"1", "true", "yes"}
SAMBA_HOST = os.environ.get("SAMBA_HOST")
SAMBA_SHARE = os.environ.get("SAMBA_SHARE")
SAMBA_USER = os.environ.get("SAMBA_USER")
SAMBA_PASSWORD = os.environ.get("SAMBA_PASSWORD")

# Wake-on-LAN. WOL_MACS is either parallel to PRINTERS or name=MAC pairs.
_WOL_MACS_RAW = (os.environ.get("WOL_MACS") or "").strip()
_WOL_HOSTS_RAW = (os.environ.get("WOL_HOSTS") or "").strip()
SAMBA_WOL_MAC = (os.environ.get("SAMBA_WOL_MAC") or "").strip()
WOL_BROADCAST = (os.environ.get("WOL_BROADCAST") or "255.255.255.255").strip()
WOL_PORT = _int_env("WOL_PORT", 9, minimum=1)
WOL_WAIT_SECONDS = _int_env("WOL_WAIT_SECONDS", 90, minimum=0)
WOL_POLL_SECONDS = _int_env("WOL_POLL_SECONDS", 3, minimum=1)
WOL_READY_GRACE_SECONDS = _int_env("WOL_READY_GRACE_SECONDS", 8, minimum=0)

# Shared by every user. Survives process restarts.
_SELECTED_PRINTER_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "selected_printer"
)
_selected_printer: str | None = None

ALLOWED_USER_IDS = frozenset(
    int(uid)
    for uid in (os.environ.get("ALLOWED_USER_IDS") or "").split(",")
    if uid.strip().isdigit()
)

CMD_TIMEOUT = 15
PRINT_TIMEOUT = 30
JOB_WATCH_SECONDS = 20
JOB_WATCH_POLL = 2

_LP_JOB_ID = re.compile(r"request id is (\S+)")
_LP_PRINTING_JOB = re.compile(r"now printing (\S+?)\.", re.IGNORECASE)
_CUPS_ERROR_HINTS = (
    "unable to connect",
    "not responding",
    "unreachable",
    "timed out",
    "timeout",
    "offline",
    "paused",
    "disabled",
    "stopped",
    "filter failed",
    "access denied",
    "authentication",
    "cifs host",
    "nt_status",
    "connection refused",
    "connection reset",
    "no route",
    "host is down",
    "network is unreachable",
    "name not found",
)

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── Models ───────────────────────────────────────────────────────────────────


@dataclass
class PendingJob:
    file_name: str
    file_id: str | None = None
    pdf_path: str | None = None
    needs_conversion: bool = False

    @property
    def is_ready(self) -> bool:
        return bool(self.pdf_path or self.file_id)


@dataclass
class CupsPrinterState:
    problem: str | None = None
    printing: str | None = None
    extra_msg: str = ""
    connectivity_error: bool = False


@dataclass
class CupsJobState:
    job_id: str
    status: str = ""
    alerts: str = ""

    def outcome(self) -> bool | None:
        """True if CUPS marked the job successful, False if it failed, else None."""
        alerts = self.alerts.lower()
        if "job-completed-successfully" in alerts:
            return True
        if any(
            token in alerts
            for token in ("aborted", "canceled", "cancelled", "with-errors")
        ):
            return False
        if self.status and _looks_like_cups_error(self.status):
            return False
        return None


@dataclass(frozen=True)
class WolTarget:
    name: str
    mac: bytes
    host: str | None


# ── Helpers ──────────────────────────────────────────────────────────────────


def md(text: str) -> str:
    return escape_markdown(text, version=1)


def user_ref(user) -> str:
    if user is None:
        return "user_id=?"
    username = f" username={user.username}" if getattr(user, "username", None) else ""
    return f"user_id={user.id}{username}"


def run_cmd(args: list[str], *, timeout: int = CMD_TIMEOUT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def safe_unlink(*paths: str | None) -> None:
    for path in paths:
        if not path:
            continue
        try:
            os.unlink(path)
        except OSError:
            pass


def confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Print", callback_data="print_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="print_cancel"),
        ]
    ])


def printer_keyboard() -> InlineKeyboardMarkup:
    current = get_selected_printer()
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"{'● ' if name == current else ''}{name}",
                callback_data=f"printer:{i}",
            )
        ]
        for i, name in enumerate(PRINTERS)
    ])


def _load_selected_printer() -> str | None:
    try:
        with open(_SELECTED_PRINTER_PATH, encoding="utf-8") as fh:
            name = fh.read().strip()
    except OSError:
        return None
    return name if name in PRINTERS else None


def _save_selected_printer(name: str) -> None:
    tmp_path = _SELECTED_PRINTER_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(name + "\n")
    os.replace(tmp_path, _SELECTED_PRINTER_PATH)


def get_selected_printer() -> str | None:
    if USE_SAMBA:
        return SAMBA_SHARE
    if _selected_printer in PRINTERS:
        return _selected_printer
    return DEFAULT_PRINTER


def set_selected_printer(name: str) -> None:
    global _selected_printer
    if name not in PRINTERS:
        raise ValueError(f"Unknown printer: {name}")
    _selected_printer = name
    _save_selected_printer(name)


def current_printer_label() -> str:
    return get_selected_printer() or "unset"


_selected_printer = _load_selected_printer() or DEFAULT_PRINTER


def is_authorized(user_id: int) -> bool:
    return user_id in ALLOWED_USER_IDS


def get_pending(context: ContextTypes.DEFAULT_TYPE) -> PendingJob | None:
    job = context.user_data.get("pending")
    return job if isinstance(job, PendingJob) else None


def set_pending(context: ContextTypes.DEFAULT_TYPE, job: PendingJob) -> None:
    context.user_data["pending"] = job


def clear_pending(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.user_data.pop("pending", None)
    if isinstance(job, PendingJob):
        safe_unlink(job.pdf_path)


def is_pdf_file(doc) -> bool:
    mime = doc.mime_type or ""
    if mime == "application/pdf":
        return True
    return (doc.file_name or "").lower().endswith(".pdf")


def is_pdf_print_blocked(pdf_path: str, file_name: str = "") -> bool:
    """True when the PDF needs a password or has printing disabled."""
    label = file_name or pdf_path
    try:
        with pikepdf.open(pdf_path) as pdf:
            if pdf.is_encrypted and not pdf.allow.print_lowres and not pdf.allow.print_highres:
                logger.info("PDF blocked file=%r reason=print permissions denied", label)
                return True
    except pikepdf.PasswordError:
        logger.info("PDF blocked file=%r reason=password required", label)
        return True
    except pikepdf.PdfError as exc:
        logger.info("PDF blocked file=%r reason=unreadable (%s)", label, exc)
        return True
    return False


def _looks_like_cups_error(text: str) -> bool:
    low = text.lower()
    return any(hint in low for hint in _CUPS_ERROR_HINTS)


def inspect_cups_printer(printer: str) -> CupsPrinterState:
    """Queue state. problem is a hard failure (unknown/disabled/stopped/not accepting)."""
    try:
        listed = run_cmd(["lpstat", "-p", printer])
    except subprocess.TimeoutExpired:
        return CupsPrinterState(problem=f"Could not query printer {printer} (timed out).")
    except OSError as exc:
        return CupsPrinterState(problem=f"Could not query printer {printer}: {exc}")

    raw = (listed.stdout or "").strip()
    err = (listed.stderr or "").strip()
    if listed.returncode != 0:
        return CupsPrinterState(problem=err or raw or f"Unknown printer {printer}.")

    first = ""
    extra: list[str] = []
    for line in (listed.stdout or "").splitlines():
        text = line.strip()
        if not text:
            continue
        if text.lower().startswith("printer "):
            first = text
        elif first:
            extra.append(text)

    if not first:
        return CupsPrinterState(problem=err or raw or f"Unknown printer {printer}.")

    low = first.lower()
    printing = None
    match = _LP_PRINTING_JOB.search(first)
    if match:
        printing = match.group(1).rstrip(".")

    extra_msg = extra[0] if extra else ""
    connectivity_error = bool(extra_msg and _looks_like_cups_error(extra_msg))

    if "disabled" in low:
        reason = extra_msg or "disabled"
        return CupsPrinterState(
            problem=f"Printer {printer} is disabled: {reason}",
            printing=printing,
            extra_msg=extra_msg,
            connectivity_error=connectivity_error,
        )
    if re.search(r"\bstopped\b", low):
        reason = extra_msg or "stopped"
        return CupsPrinterState(
            problem=f"Printer {printer} is stopped: {reason}",
            printing=printing,
            extra_msg=extra_msg,
            connectivity_error=connectivity_error,
        )

    try:
        accepting = run_cmd(["lpstat", "-a", printer])
        acc_text = (accepting.stdout or "").lower()
        if "not accepting" in acc_text:
            return CupsPrinterState(
                problem=f"Printer {printer} is not accepting jobs.",
                printing=printing,
                extra_msg=extra_msg,
                connectivity_error=connectivity_error,
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Could not check if %s is accepting jobs: %s", printer, exc)

    return CupsPrinterState(
        printing=printing,
        extra_msg=extra_msg,
        connectivity_error=connectivity_error,
    )


def parse_lpstat_job_listings(text: str) -> dict[str, CupsJobState]:
    """Parse `lpstat -l` output into job_id → status/alerts."""
    jobs: dict[str, CupsJobState] = {}
    current: CupsJobState | None = None
    for raw in text.splitlines():
        if not raw.strip():
            continue
        if not raw[:1].isspace():
            job_id = raw.split(None, 1)[0]
            if not job_id:
                current = None
                continue
            current = CupsJobState(job_id=job_id)
            jobs[job_id] = current
            continue
        if current is None:
            continue
        stripped = raw.strip()
        low = stripped.lower()
        if low.startswith("status:"):
            current.status = stripped.split(":", 1)[1].strip()
        elif low.startswith("alerts:"):
            current.alerts = stripped.split(":", 1)[1].strip()
    return jobs


def inspect_cups_job(job_id: str) -> CupsJobState | None:
    found: CupsJobState | None = None
    for which in ("not-completed", "completed"):
        try:
            result = run_cmd(["lpstat", "-l", "-W", which])
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("Could not list CUPS %s jobs: %s", which, exc)
            continue
        jobs = parse_lpstat_job_listings(result.stdout or "")
        if job_id in jobs:
            found = jobs[job_id]
            if which == "completed" or found.outcome() is not None:
                return found
    return found


def cups_job_queued(job_id: str) -> bool | None:
    """True if still in the queue, False if not, None if lpstat failed."""
    try:
        result = run_cmd(["lpstat", "-W", "not-completed"])
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Could not list CUPS jobs: %s", exc)
        return None
    if result.returncode != 0:
        logger.warning(
            "Could not list CUPS jobs: %s",
            (result.stderr or result.stdout or "").strip(),
        )
        return None
    return any(line.split(None, 1)[:1] == [job_id] for line in (result.stdout or "").splitlines())


def cancel_cups_job(job_id: str) -> None:
    try:
        result = run_cmd(["cancel", job_id])
        if result.returncode != 0:
            logger.warning(
                "Failed to cancel job %s: %s",
                job_id, (result.stderr or result.stdout or "").strip(),
            )
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Failed to cancel job %s: %s", job_id, exc)


def _job_finished_result(
    printer: str,
    job_id: str,
    state: CupsPrinterState,
) -> tuple[bool, str] | None:
    """Return a result if the CUPS job has left the queue, else None."""
    job: CupsJobState | None = None
    for _ in range(3):
        job = inspect_cups_job(job_id)
        if job is not None:
            break
        time.sleep(0.4)

    if job is not None:
        outcome = job.outcome()
        if outcome is False:
            detail = job.status or job.alerts or "job failed"
            logger.warning("CUPS job %s failed: %s", job_id, detail)
            return False, detail
        if outcome is True:
            logger.info("CUPS job %s completed on %s", job_id, printer)
            return True, f"Printed on {printer} ({job_id})."

    if state.problem:
        logger.warning("CUPS job %s gone with printer error: %s", job_id, state.problem)
        return False, state.problem
    if job is None and state.connectivity_error:
        logger.warning(
            "CUPS job %s gone; queue reports %s", job_id, state.extra_msg,
        )
        return False, state.extra_msg or "Printer is unreachable."
    logger.info("CUPS job %s left the queue without a job-level result", job_id)
    return True, f"Printed on {printer} ({job_id})."


def watch_cups_job(printer: str, job_id: str | None) -> tuple[bool, str]:
    """Wait until the job completes, fails, or the share host goes down."""
    deadline = time.monotonic() + JOB_WATCH_SECONDS
    last_problem: str | None = None
    host = cups_probe_host(printer)

    while time.monotonic() < deadline:
        state = inspect_cups_printer(printer)
        queued = cups_job_queued(job_id) if job_id else None
        last_problem = state.problem

        if job_id and queued is False:
            finished = _job_finished_result(printer, job_id, state)
            if finished is not None:
                return finished

        # Stale CIFS text on an enabled queue is not a reason to cancel.
        if state.problem:
            if job_id:
                cancel_cups_job(job_id)
            logger.warning("CUPS job %s aborted: %s", job_id, state.problem)
            return False, state.problem

        if host and not host_is_ready(host):
            if job_id:
                cancel_cups_job(job_id)
            msg = f"PC {host} is not reachable."
            logger.warning("CUPS job %s aborted: %s", job_id, msg)
            return False, msg

        if job_id and state.printing == job_id:
            logger.info("CUPS job %s still printing on %s; waiting for completion", job_id, printer)

        time.sleep(JOB_WATCH_POLL)

    queued = cups_job_queued(job_id) if job_id else None
    if job_id and queued is False:
        return _job_finished_result(printer, job_id, inspect_cups_printer(printer)) or (
            True,
            f"Printed on {printer} ({job_id}).",
        )

    if job_id and queued:
        state = inspect_cups_printer(printer)
        if host and not host_is_ready(host):
            cancel_cups_job(job_id)
            return False, f"PC {host} is not reachable."
        if state.problem:
            cancel_cups_job(job_id)
            return False, state.problem
        if state.printing == job_id:
            return True, f"Printing on {printer} ({job_id})."
        if state.printing and state.printing != job_id:
            logger.info(
                "CUPS job %s still queued behind %s on %s",
                job_id, state.printing, printer,
            )
            return True, f"Queued on {printer} behind {state.printing} ({job_id})."
        cancel_cups_job(job_id)
        detail = state.problem or last_problem or f"job {job_id} did not start printing"
        logger.warning("CUPS job %s did not start: %s", job_id, detail)
        return False, f"Printer {printer} did not start printing ({detail})."

    if job_id and queued is None:
        logger.warning("CUPS job %s status unknown after watch", job_id)
        return False, f"Could not confirm whether job {job_id} printed."

    return True, f"Sent to {printer}."


def print_via_cups(
    pdf_path: str,
    printer: str,
    *,
    ignore_unreachable: bool = False,
) -> tuple[bool, str]:
    state = inspect_cups_printer(printer)
    host = cups_probe_host(printer)

    if host and not host_is_ready(host):
        msg = f"PC {host} is not reachable."
        logger.warning("CUPS pre-check failed printer=%s: %s", printer, msg)
        return False, msg

    if state.connectivity_error:
        logger.info(
            "CUPS queue has leftover error printer=%s host=%s extra=%r; recovering",
            printer, host or "unknown", state.extra_msg,
        )
        recover_cups_queue(printer)
        state = inspect_cups_printer(printer)

    if state.problem:
        if not (ignore_unreachable and state.connectivity_error):
            logger.warning("CUPS pre-check failed printer=%s: %s", printer, state.problem)
            return False, state.problem
        logger.info(
            "CUPS pre-check ignored connectivity problem printer=%s: %s",
            printer, state.problem,
        )

    try:
        logger.info("CUPS submit printer=%s path=%s", printer, pdf_path)
        result = run_cmd(["lp", "-d", printer, pdf_path], timeout=PRINT_TIMEOUT)
        out = result.stdout.strip()
        err = result.stderr.strip()
        if result.returncode != 0:
            logger.warning(
                "CUPS rejected printer=%s rc=%s stdout=%r stderr=%r",
                printer, result.returncode, out, err,
            )
            return False, err or out
        job_id = match.group(1) if (match := _LP_JOB_ID.search(out)) else None
        logger.info("CUPS accepted printer=%s job=%s output=%r", printer, job_id, out)
        return watch_cups_job(printer, job_id)
    except subprocess.TimeoutExpired:
        logger.warning("CUPS timed out printer=%s after %ss", printer, PRINT_TIMEOUT)
        return False, "Print command timed out."
    except OSError as exc:
        logger.warning("CUPS error printer=%s: %s", printer, exc)
        return False, str(exc)


def print_via_samba(pdf_path: str) -> tuple[bool, str]:
    try:
        logger.info("Samba submit host=%s share=%s path=%s", SAMBA_HOST, SAMBA_SHARE, pdf_path)
        result = run_cmd(
            [
                "smbclient", f"//{SAMBA_HOST}/{SAMBA_SHARE}",
                "-U", f"{SAMBA_USER}%{SAMBA_PASSWORD}",
                "-c", f'print "{pdf_path}"',
            ],
            timeout=PRINT_TIMEOUT,
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        if result.returncode == 0:
            logger.info("Samba accepted share=%s rc=0", SAMBA_SHARE)
            return True, "Sent via smbclient."
        logger.warning(
            "Samba rejected share=%s rc=%s stdout=%r stderr=%r",
            SAMBA_SHARE, result.returncode, out, err,
        )
        return False, err or out
    except subprocess.TimeoutExpired:
        logger.warning("Samba timed out share=%s after %ss", SAMBA_SHARE, PRINT_TIMEOUT)
        return False, "smbclient timed out."
    except OSError as exc:
        logger.warning("Samba error share=%s: %s", SAMBA_SHARE, exc)
        return False, str(exc)


def print_pdf(pdf_path: str, *, ignore_unreachable: bool = False) -> tuple[bool, str]:
    if USE_SAMBA:
        return print_via_samba(pdf_path)
    printer = get_selected_printer()
    if not printer:
        return False, "No printer selected."
    return print_via_cups(pdf_path, printer, ignore_unreachable=ignore_unreachable)


# ── Wake-on-LAN ──────────────────────────────────────────────────────────────


_MAC_HEX = re.compile(r"[^0-9A-Fa-f]")


def parse_mac(value: str) -> bytes:
    hex_str = _MAC_HEX.sub("", value)
    if len(hex_str) != 12:
        raise ValueError(f"Invalid MAC address: {value!r}")
    return bytes.fromhex(hex_str)


def format_mac(mac: bytes) -> str:
    return ":".join(f"{b:02X}" for b in mac)


def _assignment_map(raw: str, names: tuple[str, ...], label: str) -> dict[str, str]:
    """Parse `a=x,b=y` or a list parallel to `names`. Empty values are skipped."""
    if not raw:
        return {}
    items = [part.strip() for part in raw.split(",") if part.strip()]
    if not items:
        return {}
    if any("=" in item for item in items):
        result: dict[str, str] = {}
        for item in items:
            if "=" not in item:
                raise ValueError(
                    f"{label}: mix of name=value and bare values is not allowed ({item!r})"
                )
            key, _, value = item.partition("=")
            key, value = key.strip(), value.strip()
            if not key:
                raise ValueError(f"{label}: missing name in {item!r}")
            if key not in names:
                logger.warning("%s has unknown name %r; ignoring", label, key)
                continue
            if value:
                result[key] = value
        return result
    values = tuple(part.strip() for part in raw.split(","))
    if values and values[-1] == "":
        values = values[:-1]
    if len(values) > len(names):
        raise ValueError(
            f"{label} has {len(values)} values but there are only {len(names)} names"
        )
    return {name: value for name, value in zip(names, values) if value}


def cups_device_uri(printer: str) -> str | None:
    """Device URI from `lpstat -v` (smb://, ipp://, socket://)."""
    try:
        result = run_cmd(["lpstat", "-v", printer])
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Could not read device URI for %s: %s", printer, exc)
        return None
    prefix = f"device for {printer}:"
    for line in (result.stdout or "").splitlines():
        text = line.strip()
        if not text.lower().startswith(prefix.lower()):
            continue
        uri = text[len(prefix):].strip()
        return uri or None
    return None


def _host_from_device_uri(uri: str) -> str | None:
    parsed = urlparse(uri)
    if parsed.hostname:
        return parsed.hostname
    path = (parsed.path or "").strip("/")
    if path:
        return path.split("/", 1)[0] or None
    return None


def cups_device_host(printer: str) -> str | None:
    """Best-effort host from `lpstat -v` (smb://, ipp://, socket://)."""
    uri = cups_device_uri(printer)
    return _host_from_device_uri(uri) if uri else None


def cups_probe_host(printer: str) -> str | None:
    """Windows/SMB host we can probe before printing, if the queue has one."""
    target = WOL_BY_NAME.get(printer)
    if target and target.host:
        return target.host
    uri = cups_device_uri(printer)
    if not uri:
        return None
    if urlparse(uri).scheme.lower() not in {"smb", "cifs"}:
        return None
    return _host_from_device_uri(uri)


def build_wol_targets() -> dict[str, WolTarget]:
    if USE_SAMBA:
        mac_str = SAMBA_WOL_MAC
        if not mac_str and _WOL_MACS_RAW and "=" not in _WOL_MACS_RAW and "," not in _WOL_MACS_RAW:
            mac_str = _WOL_MACS_RAW
        if not mac_str:
            return {}
        if not SAMBA_SHARE:
            raise ValueError("SAMBA_WOL_MAC requires SAMBA_SHARE")
        host = (SAMBA_HOST or "").strip() or None
        return {
            SAMBA_SHARE: WolTarget(
                name=SAMBA_SHARE,
                mac=parse_mac(mac_str),
                host=host,
            )
        }

    macs = _assignment_map(_WOL_MACS_RAW, PRINTERS, "WOL_MACS")
    hosts = _assignment_map(_WOL_HOSTS_RAW, PRINTERS, "WOL_HOSTS")
    targets: dict[str, WolTarget] = {}
    for name, mac_str in macs.items():
        host = (hosts.get(name) or "").strip() or None
        targets[name] = WolTarget(name=name, mac=parse_mac(mac_str), host=host)
    return targets


WOL_BY_NAME: dict[str, WolTarget] = {}


def resolve_wol_target(printer: str | None) -> WolTarget | None:
    if not printer:
        return None
    target = WOL_BY_NAME.get(printer)
    if target is None:
        return None
    if target.host:
        return target
    if USE_SAMBA:
        return target
    host = cups_device_host(printer)
    if host:
        return WolTarget(name=target.name, mac=target.mac, host=host)
    return target


def send_magic_packet(mac: bytes) -> None:
    packet = b"\xff" * 6 + mac * 16
    ports = {WOL_PORT, 9, 7}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(2)
        for _ in range(3):
            for port in ports:
                sock.sendto(packet, (WOL_BROADCAST, port))


def ping_host(host: str) -> bool:
    try:
        result = run_cmd(["ping", "-c", "1", "-W", "1", host], timeout=3)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


def tcp_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def host_is_ready(host: str) -> bool:
    """True when the Windows host looks up enough to accept a print job."""
    return tcp_open(host, 445) or tcp_open(host, 139) or ping_host(host)


def recover_cups_queue(printer: str) -> None:
    """Re-enable a queue CUPS disabled after a sleeping CIFS host."""
    state = inspect_cups_printer(printer)
    if not state.problem and not state.connectivity_error:
        return
    if state.problem and not state.connectivity_error:
        return
    for args in (["cupsenable", printer], ["cupsaccept", printer]):
        try:
            result = run_cmd(args)
            if result.returncode != 0:
                logger.warning(
                    "Could not run %s: %s",
                    " ".join(args),
                    (result.stderr or result.stdout or "").strip(),
                )
            else:
                logger.info("Ran %s to recover the CUPS queue", " ".join(args))
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("Could not run %s: %s", " ".join(args), exc)


async def ensure_host_awake(target: WolTarget) -> tuple[bool, str]:
    host = target.host
    mac_label = format_mac(target.mac)
    if host and await asyncio.to_thread(host_is_ready, host):
        logger.info("WoL skipped printer=%s host=%s already reachable", target.name, host)
        return True, "already-up"

    logger.info(
        "WoL sending magic packet printer=%s mac=%s host=%s broadcast=%s",
        target.name, mac_label, host or "unknown", WOL_BROADCAST,
    )
    try:
        await asyncio.to_thread(send_magic_packet, target.mac)
    except OSError as exc:
        logger.warning("WoL send failed printer=%s: %s", target.name, exc)
        return False, f"Could not send Wake-on-LAN packet: {exc}"

    if not host:
        wait = WOL_READY_GRACE_SECONDS
        if WOL_WAIT_SECONDS:
            wait = max(wait, min(WOL_WAIT_SECONDS, 20))
        logger.info("WoL no ping/SMB host printer=%s; waiting %ss then printing", target.name, wait)
        if wait:
            await asyncio.sleep(wait)
        return True, "sent-no-host"

    if WOL_WAIT_SECONDS <= 0:
        if WOL_READY_GRACE_SECONDS:
            await asyncio.sleep(WOL_READY_GRACE_SECONDS)
        return True, "sent"

    deadline = time.monotonic() + WOL_WAIT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(WOL_POLL_SECONDS)
        if await asyncio.to_thread(host_is_ready, host):
            logger.info("WoL host reachable printer=%s host=%s", target.name, host)
            if WOL_READY_GRACE_SECONDS:
                await asyncio.sleep(WOL_READY_GRACE_SECONDS)
            return True, "woken"

    logger.warning(
        "WoL timeout printer=%s host=%s waited=%ss",
        target.name, host, WOL_WAIT_SECONDS,
    )
    return False, (
        f"PC {host} did not wake within {WOL_WAIT_SECONDS}s. "
        "Check Sleep/WoL on Windows and that this machine is on the same LAN."
    )


async def ask_to_print(message, file_name: str) -> None:
    await message.reply_text(
        f"📄 *{md(file_name)}*\n🖨️ `{md(current_printer_label())}`\nDo you want to print this?",
        parse_mode="Markdown",
        reply_markup=confirm_keyboard(),
    )


async def process_pdf(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    file_id: str,
    file_name: str,
) -> None:
    status = await update.message.reply_text(
        f"📥 Checking *{md(file_name)}*…", parse_mode="Markdown"
    )

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        tg_file = await context.bot.get_file(file_id)
        await tg_file.download_to_drive(tmp_path)

        if is_pdf_print_blocked(tmp_path, file_name):
            logger.info(
                "Print rejected %s file=%r reason=protected printer=%s",
                user_ref(update.effective_user), file_name, current_printer_label(),
            )
            await status.edit_text(
                f"🔒 *{md(file_name)}* is protected and can't be printed.\n\n"
                "Please unlock the PDF (remove the password or printing "
                "restrictions) and send it again.",
                parse_mode="Markdown",
            )
            safe_unlink(tmp_path)
            return

        set_pending(
            context,
            PendingJob(file_name=file_name, pdf_path=tmp_path),
        )
        size = os.path.getsize(tmp_path)
        logger.info(
            "Print waiting for confirm %s file=%r bytes=%s printer=%s",
            user_ref(update.effective_user), file_name, size, current_printer_label(),
        )
        await status.edit_text(
            f"📄 *{md(file_name)}*\n🖨️ `{md(current_printer_label())}`\nDo you want to print this?",
            parse_mode="Markdown",
            reply_markup=confirm_keyboard(),
        )
    except Exception:
        logger.exception(
            "Failed to process PDF %s file=%r",
            user_ref(update.effective_user), file_name,
        )
        safe_unlink(tmp_path)
        await status.edit_text(
            f"⚠️ Could not read *{md(file_name)}*.\nPlease try sending it again.",
            parse_mode="Markdown",
        )


async def print_job(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    job: PendingJob,
) -> None:
    tmp_path: str | None = None
    converted_pdf_path: str | None = None
    print_target: str | None = job.pdf_path
    who = user_ref(query.from_user)
    printer = current_printer_label()
    wol = resolve_wol_target(printer)
    wake_task: asyncio.Task[tuple[bool, str]] | None = None

    logger.info(
        "Print confirmed %s file=%r convert=%s printer=%s wol=%s",
        who, job.file_name, job.needs_conversion, printer,
        format_mac(wol.mac) if wol else "off",
    )

    try:
        if wol:
            wake_task = asyncio.create_task(ensure_host_awake(wol), name=f"wol:{printer}")

        if print_target is None:
            if not job.file_id:
                logger.warning("Print aborted %s file=%r reason=missing file_id", who, job.file_name)
                await query.edit_message_text("⚠️ Session expired. Please send the file again.")
                return

            logger.info("Print downloading %s file=%r", who, job.file_name)
            await query.edit_message_text(
                f"📥 Downloading *{md(job.file_name)}*…", parse_mode="Markdown"
            )

            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = tmp.name

            tg_file = await context.bot.get_file(job.file_id)
            await tg_file.download_to_drive(tmp_path)

            if job.needs_conversion:
                logger.info("Print converting to PDF %s file=%r", who, job.file_name)
                await query.edit_message_text(
                    f"🔄 Converting *{md(job.file_name)}* to PDF…", parse_mode="Markdown"
                )
                converted_pdf_path = tmp_path + ".pdf"
                with open(converted_pdf_path, "wb") as out:
                    out.write(img2pdf.convert(tmp_path))
                print_target = converted_pdf_path
            else:
                print_target = tmp_path

        size = os.path.getsize(print_target) if print_target else 0
        logger.info(
            "Print sending %s file=%r bytes=%s printer=%s",
            who, job.file_name, size, printer,
        )

        ignore_unreachable = False
        if wake_task is not None:
            if not wake_task.done():
                await query.edit_message_text(
                    f"💡 Waking the PC for `{md(printer)}`…\n"
                    f"This can take up to {WOL_WAIT_SECONDS}s if it was asleep.",
                    parse_mode="Markdown",
                )
            woke, wol_message = await wake_task
            wake_task = None
            if not woke:
                logger.error(
                    "Print aborted %s file=%r printer=%s reason=wol %s",
                    who, job.file_name, printer, wol_message,
                )
                await query.edit_message_text(
                    f"❌ Failed to print *{md(job.file_name)}*.\n"
                    f"Could not wake the PC: `{md(wol_message)}`",
                    parse_mode="Markdown",
                )
                return
            ignore_unreachable = True
            if not USE_SAMBA and printer:
                await asyncio.to_thread(recover_cups_queue, printer)
            logger.info(
                "Print host ready %s printer=%s wol=%s",
                who, printer, wol_message,
            )

        await query.edit_message_text(
            f"🖨️ Sending *{md(job.file_name)}* to `{md(printer)}`…",
            parse_mode="Markdown",
        )

        success, message = await asyncio.to_thread(
            print_pdf, print_target, ignore_unreachable=ignore_unreachable,
        )
        if success:
            logger.info(
                "Print sent %s file=%r printer=%s result=%r",
                who, job.file_name, printer, message,
            )
            await query.edit_message_text(
                f"✅ *{md(job.file_name)}* sent to `{md(printer)}`!\n`{md(message)}`",
                parse_mode="Markdown",
            )
        else:
            logger.error(
                "Print failed %s file=%r printer=%s error=%r",
                who, job.file_name, printer, message,
            )
            await query.edit_message_text(
                f"❌ Failed to print *{md(job.file_name)}*.\nError: `{md(message)}`",
                parse_mode="Markdown",
            )
    except Exception:
        logger.exception("Print crashed %s file=%r printer=%s", who, job.file_name, printer)
        try:
            await query.edit_message_text(
                f"❌ Failed to print *{md(job.file_name)}*.\nPlease try sending it again.",
                parse_mode="Markdown",
            )
        except Exception:
            logger.exception("Could not notify user after print crash")
    finally:
        if wake_task is not None and not wake_task.done():
            wake_task.cancel()
            try:
                await wake_task
            except (asyncio.CancelledError, Exception):
                pass
        safe_unlink(job.pdf_path, tmp_path, converted_pdf_path)


# ── Handlers ─────────────────────────────────────────────────────────────────


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Hi! Send me a PDF and I'll ask you before printing it.\n"
        "Use /printer to choose the printer everyone prints to."
    )


async def printer_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not is_authorized(user.id):
        logger.warning("Unauthorized /printer from user_id=%s", user.id)
        await update.message.reply_text(
            f"⛔ You are not authorized to use this bot.\nYour Telegram user ID: `{user.id}`",
            parse_mode="Markdown",
        )
        return

    if USE_SAMBA:
        await update.message.reply_text(
            f"Samba mode uses a single shared printer: `{md(SAMBA_SHARE or 'unset')}`",
            parse_mode="Markdown",
        )
        return

    if not PRINTERS:
        await update.message.reply_text("No printers configured. Set PRINTERS in config.env.")
        return

    current = current_printer_label()
    logger.info("Printer menu opened %s current=%s", user_ref(user), current)
    await update.message.reply_text(
        f"Current printer for everyone: `{md(current)}`\nChoose one:",
        parse_mode="Markdown",
        reply_markup=printer_keyboard(),
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    doc = update.message.document
    photo = update.message.photo

    if not is_authorized(user.id):
        logger.warning("Unauthorized document from user_id=%s", user.id)
        await update.message.reply_text(
            f"⛔ You are not authorized to use this bot.\nYour Telegram user ID: `{user.id}`",
            parse_mode="Markdown",
        )
        return

    clear_pending(context)

    if doc:
        mime = doc.mime_type or ""
        file_name = doc.file_name or "document.pdf"
        if is_pdf_file(doc):
            logger.info(
                "Print request %s file=%r type=pdf size=%s printer=%s",
                user_ref(user), file_name, doc.file_size, current_printer_label(),
            )
            await process_pdf(
                update,
                context,
                file_id=doc.file_id,
                file_name=file_name,
            )
            return
        if mime.startswith("image/"):
            file_name = doc.file_name or "image"
            logger.info(
                "Print request %s file=%r type=image mime=%s size=%s printer=%s",
                user_ref(user), file_name, mime, doc.file_size, current_printer_label(),
            )
            set_pending(
                context,
                PendingJob(
                    file_name=file_name,
                    file_id=doc.file_id,
                    needs_conversion=True,
                ),
            )
        else:
            logger.info(
                "Print rejected %s file=%r reason=unsupported mime=%s",
                user_ref(user), file_name, mime,
            )
            await update.message.reply_text(
                f"⚠️ I can print PDFs and images. Received: `{md(mime)}`",
                parse_mode="Markdown",
            )
            return
    elif photo:
        logger.info(
            "Print request %s file=%r type=photo size=%s printer=%s",
            user_ref(user), "photo.jpg", photo[-1].file_size, current_printer_label(),
        )
        set_pending(
            context,
            PendingJob(
                file_name="photo.jpg",
                file_id=photo[-1].file_id,
                needs_conversion=True,
            ),
        )
    else:
        return

    pending = get_pending(context)
    logger.info(
        "Print waiting for confirm %s file=%r printer=%s",
        user_ref(user), pending.file_name, current_printer_label(),
    )
    await ask_to_print(update.message, pending.file_name)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not is_authorized(query.from_user.id):
        logger.warning("Unauthorized callback %s data=%r", user_ref(query.from_user), query.data)
        clear_pending(context)
        await query.edit_message_text("⛔ You are not authorized to use this bot.")
        return

    if query.data == "print_cancel":
        job = get_pending(context)
        logger.info(
            "Print cancelled %s file=%r",
            user_ref(query.from_user), job.file_name if job else None,
        )
        clear_pending(context)
        await query.edit_message_text("❌ Print cancelled.")
        return

    if query.data.startswith("printer:"):
        if USE_SAMBA:
            await query.edit_message_text("Samba mode uses a single shared printer.")
            return
        try:
            index = int(query.data.split(":", 1)[1])
            name = PRINTERS[index]
        except (ValueError, IndexError):
            logger.warning(
                "Printer select failed %s data=%r",
                user_ref(query.from_user), query.data,
            )
            await query.edit_message_text("⚠️ Unknown printer. Use /printer again.")
            return
        previous = current_printer_label()
        set_selected_printer(name)
        logger.info(
            "Printer changed %s from=%s to=%s",
            user_ref(query.from_user), previous, name,
        )
        await query.edit_message_text(
            f"🖨️ Printer set to `{md(name)}`.\nEveryone will print here until someone changes it.",
            parse_mode="Markdown",
        )
        return

    if query.data != "print_confirm":
        logger.info("Ignored callback %s data=%r", user_ref(query.from_user), query.data)
        return

    job = get_pending(context)
    if job is None or not job.is_ready:
        logger.warning("Print aborted %s reason=session expired", user_ref(query.from_user))
        await query.edit_message_text("⚠️ Session expired. Please send the file again.")
        return

    context.user_data.pop("pending", None)
    await print_job(query, context, job)


# ── Main ─────────────────────────────────────────────────────────────────────


BOT_COMMANDS = [
    BotCommand("start", "How to use this bot"),
    BotCommand("printer", "Choose the printer everyone prints to"),
]


async def post_init(app: Application) -> None:
    await app.bot.set_my_commands(BOT_COMMANDS)
    logger.info("Telegram command menu: /%s", ", /".join(c.command for c in BOT_COMMANDS))


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("Set the TELEGRAM_BOT_TOKEN environment variable.")
    if not ALLOWED_USER_IDS:
        raise ValueError(
            "Set ALLOWED_USER_IDS (comma-separated Telegram user IDs). "
            "Without it, every user is rejected."
        )
    if not USE_SAMBA:
        if not PRINTERS:
            raise ValueError(
                "Set PRINTERS (comma-separated CUPS queue names)."
            )
        if _default_printer and _default_printer not in PRINTERS:
            raise ValueError(
                f"DEFAULT_PRINTER={_default_printer!r} is not in PRINTERS."
            )

    global WOL_BY_NAME
    WOL_BY_NAME = build_wol_targets()

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("printer", printer_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_document))
    app.add_handler(CallbackQueryHandler(handle_callback))

    wol_desc = [
        f"{target.name}={format_mac(target.mac)}"
        + (f"@{target.host}" if target.host else "")
        for target in WOL_BY_NAME.values()
    ]
    logger.info(
        "Bot started. Allowed users: %s. Printers: %s. Selected: %s. Wake-on-LAN: %s",
        sorted(ALLOWED_USER_IDS),
        list(PRINTERS) if not USE_SAMBA else [SAMBA_SHARE],
        current_printer_label(),
        wol_desc or "off",
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES, timeout=30, poll_interval=5.0)


if __name__ == "__main__":
    main()