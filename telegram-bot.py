#!/usr/bin/env python3
"""
Telegram → PDF → Windows Shared Printer Bot
User sends a PDF → bot asks to confirm → prints on button tap.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass

import img2pdf
import pikepdf
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

CUPS_PRINTER_NAME = os.environ.get("CUPS_PRINTER_NAME")

USE_SAMBA = (os.environ.get("USE_SAMBA") or "").lower() in {"1", "true", "yes"}
SAMBA_HOST = os.environ.get("SAMBA_HOST")
SAMBA_SHARE = os.environ.get("SAMBA_SHARE")
SAMBA_USER = os.environ.get("SAMBA_USER")
SAMBA_PASSWORD = os.environ.get("SAMBA_PASSWORD")

ALLOWED_USER_IDS = frozenset(
    int(uid)
    for uid in (os.environ.get("ALLOWED_USER_IDS") or "").split(",")
    if uid.strip().isdigit()
)

CMD_TIMEOUT = 15
PRINT_TIMEOUT = 30

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


def is_pdf_print_blocked(pdf_path: str) -> bool:
    """True when the PDF needs a password or has printing disabled."""
    info = run_cmd(["pdfinfo", pdf_path])
    combined = info.stdout + info.stderr
    if info.returncode != 0 and "password" in combined.lower():
        logger.info("PDF blocked: pdfinfo requires password")
        return True

    encrypted = False
    for line in info.stdout.splitlines():
        if not line.startswith("Encrypted:"):
            continue
        normalized = line.lower().replace(" ", "")
        if "encrypted:yes" in normalized:
            encrypted = True
        if "print:no" in normalized:
            logger.info("PDF blocked: printing disabled in PDF permissions")
            return True

    try:
        with pikepdf.open(pdf_path) as pdf:
            if pdf.is_encrypted and not pdf.allow.print_lowres and not pdf.allow.print_highres:
                logger.info("PDF blocked: pikepdf print permissions denied")
                return True
    except pikepdf.PasswordError:
        logger.info("PDF blocked: pikepdf requires password")
        return True

    if not encrypted:
        return False

    with tempfile.TemporaryDirectory() as tmpdir:
        prefix = os.path.join(tmpdir, "page")
        render = run_cmd(["pdftoppm", "-f", "1", "-l", "1", "-png", pdf_path, prefix])
        if render.returncode != 0 and "password" in (render.stdout + render.stderr).lower():
            logger.info("PDF blocked: pdftoppm requires password")
            return True

    return False


def print_via_cups(pdf_path: str) -> tuple[bool, str]:
    try:
        result = run_cmd(["lp", "-d", CUPS_PRINTER_NAME, pdf_path], timeout=PRINT_TIMEOUT)
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip() or result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "Print command timed out."
    except OSError as exc:
        return False, str(exc)


def print_via_samba(pdf_path: str) -> tuple[bool, str]:
    try:
        result = run_cmd(
            [
                "smbclient", f"//{SAMBA_HOST}/{SAMBA_SHARE}",
                "-U", f"{SAMBA_USER}%{SAMBA_PASSWORD}",
                "-c", f'print "{pdf_path}"',
            ],
            timeout=PRINT_TIMEOUT,
        )
        if result.returncode == 0:
            return True, "Sent via smbclient."
        return False, result.stderr.strip() or result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "smbclient timed out."
    except OSError as exc:
        return False, str(exc)


def print_pdf(pdf_path: str) -> tuple[bool, str]:
    if USE_SAMBA:
        return print_via_samba(pdf_path)
    return print_via_cups(pdf_path)


async def ask_to_print(message, file_name: str) -> None:
    await message.reply_text(
        f"📄 *{md(file_name)}*\nDo you want to print this?",
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

        if is_pdf_print_blocked(tmp_path):
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
        await status.edit_text(
            f"📄 *{md(file_name)}*\nDo you want to print this?",
            parse_mode="Markdown",
            reply_markup=confirm_keyboard(),
        )
    except Exception:
        logger.exception("Failed to process PDF %s", file_name)
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

    try:
        if print_target is None:
            if not job.file_id:
                await query.edit_message_text("⚠️ Session expired. Please send the file again.")
                return

            await query.edit_message_text(
                f"📥 Downloading *{md(job.file_name)}*…", parse_mode="Markdown"
            )

            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = tmp.name

            tg_file = await context.bot.get_file(job.file_id)
            await tg_file.download_to_drive(tmp_path)

            if job.needs_conversion:
                await query.edit_message_text(
                    f"🔄 Converting *{md(job.file_name)}* to PDF…", parse_mode="Markdown"
                )
                converted_pdf_path = tmp_path + ".pdf"
                with open(converted_pdf_path, "wb") as out:
                    out.write(img2pdf.convert(tmp_path))
                print_target = converted_pdf_path
            else:
                print_target = tmp_path

        await query.edit_message_text(
            f"🖨️ Sending *{md(job.file_name)}* to printer…", parse_mode="Markdown"
        )

        success, message = print_pdf(print_target)
        if success:
            await query.edit_message_text(
                f"✅ *{md(job.file_name)}* sent to printer!\n`{md(message)}`",
                parse_mode="Markdown",
            )
        else:
            await query.edit_message_text(
                f"❌ Failed to print *{md(job.file_name)}*.\nError: `{md(message)}`",
                parse_mode="Markdown",
            )
    finally:
        safe_unlink(job.pdf_path, tmp_path, converted_pdf_path)


# ── Handlers ─────────────────────────────────────────────────────────────────


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Hi! Just send me a PDF and I'll ask you before printing it."
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    doc = update.message.document
    photo = update.message.photo

    if not is_authorized(user.id):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    clear_pending(context)

    if doc:
        mime = doc.mime_type or ""
        if is_pdf_file(doc):
            await process_pdf(
                update,
                context,
                file_id=doc.file_id,
                file_name=doc.file_name or "document.pdf",
            )
            return
        if mime.startswith("image/"):
            set_pending(
                context,
                PendingJob(
                    file_name=doc.file_name or "image",
                    file_id=doc.file_id,
                    needs_conversion=True,
                ),
            )
        else:
            await update.message.reply_text(
                f"⚠️ I can print PDFs and images. Received: `{md(mime)}`",
                parse_mode="Markdown",
            )
            return
    elif photo:
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

    await ask_to_print(update.message, get_pending(context).file_name)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if not is_authorized(query.from_user.id):
        clear_pending(context)
        await query.edit_message_text("⛔ You are not authorized to use this bot.")
        return

    if query.data == "print_cancel":
        clear_pending(context)
        await query.edit_message_text("❌ Print cancelled.")
        return

    if query.data != "print_confirm":
        return

    job = get_pending(context)
    if job is None or not job.is_ready:
        await query.edit_message_text("⚠️ Session expired. Please send the file again.")
        return

    context.user_data.pop("pending", None)
    await print_job(query, context, job)


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise ValueError("Set the TELEGRAM_BOT_TOKEN environment variable.")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_document))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot started. Waiting for PDFs…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, timeout=30, poll_interval=5.0)


if __name__ == "__main__":
    main()
    
