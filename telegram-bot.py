#!/usr/bin/env python3
"""
Telegram → PDF → Windows Shared Printer Bot
User sends a PDF → bot asks to confirm → prints on button tap.
"""

import os
import logging
import subprocess
import tempfile
import img2pdf
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# ── Configuration ────────────────────────────────────────────────────────────

TELEGRAM_BOT_TOKEN = "???????"
CUPS_PRINTER_NAME = "??????"

USE_SAMBA = False
SAMBA_HOST = "192.168.1.XXX"
SAMBA_SHARE = "PrinterShareName"
SAMBA_USER = "windows_user"
SAMBA_PASSWORD = "windows_password"

ALLOWED_USER_IDS: list[int] = []

# ── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────────────────────────

def is_authorized(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return user_id in ALLOWED_USER_IDS


def print_via_cups(pdf_path: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["lp", "-d", CUPS_PRINTER_NAME, pdf_path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, result.stderr.strip()
    except subprocess.TimeoutExpired:
        return False, "Print command timed out."
    except Exception as e:
        return False, str(e)


def print_via_samba(pdf_path: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [
                "smbclient", f"//{SAMBA_HOST}/{SAMBA_SHARE}",
                "-U", f"{SAMBA_USER}%{SAMBA_PASSWORD}",
                "-c", f'print "{pdf_path}"',
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            return True, "Sent via smbclient."
        return False, result.stderr.strip() or result.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "smbclient timed out."
    except Exception as e:
        return False, str(e)


def print_pdf(pdf_path: str) -> tuple[bool, str]:
    if USE_SAMBA:
        return print_via_samba(pdf_path)
    return print_via_cups(pdf_path)

# ── Handlers ─────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "👋 Hi! Just send me a PDF and I'll ask you before printing it."
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    doc = update.message.document
    photo = update.message.photo  # sent as photo (compressed)

    if not is_authorized(user.id):
        await update.message.reply_text("⛔ You are not authorized to use this bot.")
        return

    # Resolve file: document (PDF or image file) or compressed photo
    if doc:
        mime = doc.mime_type or ""
        if mime == "application/pdf":
            file_id = doc.file_id
            file_name = doc.file_name or "document.pdf"
            needs_conversion = False
        elif mime.startswith("image/"):
            file_id = doc.file_id
            file_name = doc.file_name or "image"
            needs_conversion = True
        else:
            await update.message.reply_text(
                f"⚠️ I can print PDFs and images. Received: `{mime}`",
                parse_mode="Markdown",
            )
            return
    elif photo:
        # Telegram compresses photos — grab the highest resolution version
        file_id = photo[-1].file_id
        file_name = "photo.jpg"
        needs_conversion = True
    else:
        return

    context.user_data["pending_file_id"] = file_id
    context.user_data["pending_file_name"] = file_name
    context.user_data["pending_needs_conversion"] = needs_conversion

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Print", callback_data="print_confirm"),
            InlineKeyboardButton("❌ Cancel", callback_data="print_cancel"),
        ]
    ])

    await update.message.reply_text(
        f"📄 *{file_name}*\nDo you want to print this?",
        parse_mode="Markdown",
        reply_markup=keyboard,
    )
    
    
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    if query.data == "print_cancel":
        context.user_data.pop("pending_file_id", None)
        context.user_data.pop("pending_file_name", None)
        context.user_data.pop("pending_needs_conversion", None)
        await query.edit_message_text("❌ Print cancelled.")
        return

    if query.data == "print_confirm":
        file_id = context.user_data.pop("pending_file_id", None)
        file_name = context.user_data.pop("pending_file_name", "document.pdf")
        needs_conversion = context.user_data.pop("pending_needs_conversion", False)

        if not file_id:
            await query.edit_message_text("⚠️ Session expired. Please send the file again.")
            return

        await query.edit_message_text(f"📥 Downloading *{file_name}*…", parse_mode="Markdown")

        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name

        try:
            tg_file = await context.bot.get_file(file_id)
            await tg_file.download_to_drive(tmp_path)

            if needs_conversion:
                await query.edit_message_text(
                    f"🔄 Converting *{file_name}* to PDF…", parse_mode="Markdown"
                )
                pdf_path = tmp_path + ".pdf"
                with open(pdf_path, "wb") as f:
                    f.write(img2pdf.convert(tmp_path))
            else:
                pdf_path = tmp_path

            await query.edit_message_text(
                f"🖨️ Sending *{file_name}* to printer…", parse_mode="Markdown"
            )

            success, message = print_pdf(pdf_path)

            if success:
                await query.edit_message_text(
                    f"✅ *{file_name}* sent to printer!\n`{message}`",
                    parse_mode="Markdown",
                )
            else:
                await query.edit_message_text(
                    f"❌ Failed to print *{file_name}*.\nError: `{message}`",
                    parse_mode="Markdown",
                )
        finally:
            try:
                os.unlink(tmp_path)
                if needs_conversion:
                    os.unlink(pdf_path)
            except OSError:
                pass


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN)
    if not token:
        raise ValueError("Set your TELEGRAM_BOT_TOKEN.")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_document))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot started. Waiting for PDFs…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, timeout=30, poll_interval=5.0)


if __name__ == "__main__":
    main()
