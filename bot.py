#!/usr/bin/env python3
"""
Telegram → PDF → Windows Shared Printer Bot
User sends a PDF → bot asks to confirm → prints on button tap.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass

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


# CUPS queue names from PRINTERS=name1,name2
PRINTERS = _csv_env("PRINTERS")
_default_printer = (os.environ.get("DEFAULT_PRINTER") or "").strip()
DEFAULT_PRINTER = _default_printer or (PRINTERS[0] if PRINTERS else None)

USE_SAMBA = (os.environ.get("USE_SAMBA") or "").lower() in {"1", "true", "yes"}
SAMBA_HOST = os.environ.get("SAMBA_HOST")
SAMBA_SHARE = os.environ.get("SAMBA_SHARE")
SAMBA_USER = os.environ.get("SAMBA_USER")
SAMBA_PASSWORD = os.environ.get("SAMBA_PASSWORD")

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


def inspect_cups_printer(printer: str) -> tuple[str | None, str | None]:
    """Return (problem, printing_job_id). problem is None if the queue looks usable."""
    try:
        listed = run_cmd(["lpstat", "-p", printer])
    except subprocess.TimeoutExpired:
        return f"Could not query printer {printer} (timed out).", None
    except OSError as exc:
        return f"Could not query printer {printer}: {exc}", None

    raw = (listed.stdout or "").strip()
    err = (listed.stderr or "").strip()
    if listed.returncode != 0:
        return err or raw or f"Unknown printer {printer}.", None

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
        return err or raw or f"Unknown printer {printer}.", None

    low = first.lower()
    printing = None
    match = _LP_PRINTING_JOB.search(first)
    if match:
        printing = match.group(1).rstrip(".")

    extra_msg = extra[0] if extra else ""

    if "disabled" in low:
        reason = extra_msg or "disabled"
        return f"Printer {printer} is disabled: {reason}", printing
    if re.search(r"\bstopped\b", low):
        reason = extra_msg or "stopped"
        return f"Printer {printer} is stopped: {reason}", printing

    try:
        accepting = run_cmd(["lpstat", "-a", printer])
        acc_text = (accepting.stdout or "").lower()
        if "not accepting" in acc_text:
            return f"Printer {printer} is not accepting jobs.", printing
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Could not check if %s is accepting jobs: %s", printer, exc)

    if extra_msg and _looks_like_cups_error(extra_msg):
        return f"Printer {printer} is unreachable: {extra_msg}", printing

    return None, printing


def cups_job_queued(job_id: str) -> bool:
    try:
        result = run_cmd(["lpstat", "-W", "not-completed"])
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Could not list CUPS jobs: %s", exc)
        return False
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


def watch_cups_job(printer: str, job_id: str | None) -> tuple[bool, str]:
    """Wait until the job prints, vanishes, or the queue reports a failure."""
    deadline = time.monotonic() + JOB_WATCH_SECONDS
    last_problem: str | None = None

    while time.monotonic() < deadline:
        problem, printing = inspect_cups_printer(printer)
        queued = cups_job_queued(job_id) if job_id else False
        last_problem = problem

        if job_id and not queued:
            if problem:
                logger.warning("CUPS job %s gone with printer error: %s", job_id, problem)
                return False, problem
            logger.info("CUPS job %s left the queue (printed or completed)", job_id)
            return True, f"Printed on {printer} ({job_id})."

        if problem:
            if job_id:
                cancel_cups_job(job_id)
            logger.warning("CUPS job %s aborted: %s", job_id, problem)
            return False, problem

        if job_id and printing == job_id:
            logger.info("CUPS job %s is printing on %s", job_id, printer)
            return True, f"Printing on {printer} ({job_id})."

        time.sleep(JOB_WATCH_POLL)

    if job_id and cups_job_queued(job_id):
        problem, printing = inspect_cups_printer(printer)
        if printing == job_id:
            return True, f"Printing on {printer} ({job_id})."
        if printing and printing != job_id:
            logger.info(
                "CUPS job %s still queued behind %s on %s",
                job_id, printing, printer,
            )
            return True, f"Queued on {printer} behind {printing} ({job_id})."
        if job_id:
            cancel_cups_job(job_id)
        detail = problem or last_problem or f"job {job_id} did not start printing"
        logger.warning("CUPS job %s did not start: %s", job_id, detail)
        return False, f"Printer {printer} did not start printing ({detail})."

    return True, f"Sent to {printer}."


def print_via_cups(pdf_path: str, printer: str) -> tuple[bool, str]:
    problem, _printing = inspect_cups_printer(printer)
    if problem:
        logger.warning("CUPS pre-check failed printer=%s: %s", printer, problem)
        return False, problem

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


def print_pdf(pdf_path: str) -> tuple[bool, str]:
    if USE_SAMBA:
        return print_via_samba(pdf_path)
    printer = get_selected_printer()
    if not printer:
        return False, "No printer selected."
    return print_via_cups(pdf_path, printer)


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

    logger.info(
        "Print confirmed %s file=%r convert=%s printer=%s",
        who, job.file_name, job.needs_conversion, printer,
    )

    try:
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
        await query.edit_message_text(
            f"🖨️ Sending *{md(job.file_name)}* to `{md(printer)}`…",
            parse_mode="Markdown",
        )

        success, message = print_pdf(print_target)
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

    app = Application.builder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("printer", printer_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_document))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info(
        "Bot started. Allowed users: %s. Printers: %s. Selected: %s",
        sorted(ALLOWED_USER_IDS),
        list(PRINTERS) if not USE_SAMBA else [SAMBA_SHARE],
        current_printer_label(),
    )
    app.run_polling(allowed_updates=Update.ALL_TYPES, timeout=30, poll_interval=5.0)


if __name__ == "__main__":
    main()