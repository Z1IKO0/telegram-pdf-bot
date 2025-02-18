import tempfile
from collections import defaultdict
from threading import Lock

from PyPDF2 import PdfFileMerger
from PyPDF2.utils import PdfReadError
from telegram import (
    ChatAction,
    ParseMode,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.ext import (
    CallbackContext,
    CommandHandler,
    ConversationHandler,
    Filters,
    MessageHandler,
)

from pdf_bot.analytics import TaskType
from pdf_bot.consts import (
    CANCEL,
    DONE,
    PDF_INVALID_FORMAT,
    PDF_TOO_LARGE,
    REMOVE_LAST,
    TEXT_FILTER,
)
from pdf_bot.language import set_lang
from pdf_bot.utils import (
    cancel,
    check_pdf,
    check_user_data,
    reply_with_cancel_btn,
    send_file_names,
    write_send_pdf,
)

WAIT_MERGE = 0
MERGE_IDS = "merge_ids"
MERGE_NAMES = "merge_names"

merge_locks = defaultdict(Lock)


def merge_cov_handler() -> ConversationHandler:
    handlers = [
        MessageHandler(Filters.document, check_doc),
        MessageHandler(TEXT_FILTER, check_text),
    ]
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("merge", merge)],
        states={
            WAIT_MERGE: handlers,
            ConversationHandler.WAITING: handlers,
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    return conv_handler


def merge(update: Update, context: CallbackContext) -> int:
    update.effective_message.chat.send_action(ChatAction.TYPING)
    user_id = update.effective_message.from_user.id
    merge_locks[user_id].acquire()
    context.user_data[MERGE_IDS] = []
    context.user_data[MERGE_NAMES] = []
    merge_locks[user_id].release()

    return ask_first_doc(update, context)


def ask_first_doc(update: Update, context: CallbackContext) -> int:
    _ = set_lang(update, context)
    reply_with_cancel_btn(
        update,
        context,
        "{desc_1}\n\n{desc_2}".format(
            desc_1=_("Send me the PDF files that you'll like to merge"),
            desc_2=_(
                "Note that the files will be merged in the order that you send me"
            ),
        ),
    )

    return WAIT_MERGE


def check_doc(update: Update, context: CallbackContext) -> int:
    message = update.effective_message
    message.chat.send_action(ChatAction.TYPING)
    result = check_pdf(update, context, send_msg=False)

    if result in [PDF_INVALID_FORMAT, PDF_TOO_LARGE]:
        return process_invalid_pdf(update, context, result)

    user_id = message.from_user.id
    merge_locks[user_id].acquire()
    context.user_data[MERGE_IDS].append(message.document.file_id)
    context.user_data[MERGE_NAMES].append(message.document.file_name)
    result = ask_next_doc(update, context)
    merge_locks[user_id].release()

    return result


def process_invalid_pdf(
    update: Update, context: CallbackContext, pdf_result: int
) -> int:
    _ = set_lang(update, context)
    if pdf_result == PDF_INVALID_FORMAT:
        text = _("Your file is not a PDF file")
    else:
        text = (
            "{desc_1}\n\n{desc_2}".format(
                desc_1=_("Your file is too large for me to download and process"),
                desc_2=_(
                    "Note that this is a Telegram Bot limitation and there's "
                    "nothing I can do unless Telegram changes this limit"
                ),
            ),
        )

    update.effective_message.reply_text(text)
    user_id = update.effective_message.from_user.id
    merge_locks[user_id].acquire()

    if not context.user_data[MERGE_NAMES]:
        result = ask_first_doc(update, context)
    else:
        result = ask_next_doc(update, context)

    merge_locks[user_id].release()

    return result


def ask_next_doc(update: Update, context: CallbackContext) -> int:
    _ = set_lang(update, context)
    send_file_names(update, context, context.user_data[MERGE_NAMES], _("PDF files"))
    reply_markup = ReplyKeyboardMarkup(
        [[_(DONE)], [_(REMOVE_LAST), _(CANCEL)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    update.effective_message.reply_text(
        _(
            "Press {done} if you've sent me all the PDF files that "
            "you'll like to merge or keep sending me the PDF files"
        ).format(done=f"<b>{_(DONE)}</b>"),
        reply_markup=reply_markup,
        parse_mode=ParseMode.HTML,
    )

    return WAIT_MERGE


def check_text(update: Update, context: CallbackContext) -> int:
    message = update.effective_message
    message.chat.send_action(ChatAction.TYPING)
    _ = set_lang(update, context)
    text = message.text

    if text in [_(REMOVE_LAST), _(DONE)]:
        user_id = message.from_user.id
        lock = merge_locks[user_id]

        if not check_user_data(update, context, MERGE_IDS, lock):
            return ConversationHandler.END

        if text == _(REMOVE_LAST):
            return remove_doc(update, context, lock)
        if text == _(DONE):
            return preprocess_merge_pdf(update, context, lock)
    elif text == _(CANCEL):
        return cancel(update, context)

    return WAIT_MERGE


def remove_doc(update: Update, context: CallbackContext, lock: Lock) -> int:
    _ = set_lang(update, context)
    lock.acquire()
    file_ids = context.user_data[MERGE_IDS]
    file_names = context.user_data[MERGE_NAMES]
    file_ids.pop()
    file_name = file_names.pop()

    update.effective_message.reply_text(
        _("{file_name} has been removed for merging").format(
            file_name=f"<b>{file_name}</b>"
        ),
        parse_mode=ParseMode.HTML,
    )

    if len(file_ids) == 0:
        result = ask_first_doc(update, context)
    else:
        result = ask_next_doc(update, context)

    lock.release()

    return result


def preprocess_merge_pdf(update: Update, context: CallbackContext, lock: Lock) -> int:
    _ = set_lang(update, context)
    lock.acquire()
    num_files = len(context.user_data[MERGE_IDS])

    if num_files == 0:
        update.effective_message.reply_text(_("You haven't sent me any PDF files"))

        result = ask_first_doc(update, context)
    elif num_files == 1:
        update.effective_message.reply_text(_("You've only sent me one PDF file"))

        result = ask_next_doc(update, context)
    else:
        result = merge_pdf(update, context)

    lock.release()

    return result


def merge_pdf(update: Update, context: CallbackContext) -> int:
    _ = set_lang(update, context)
    update.effective_message.reply_text(
        _("Merging your PDF files"), reply_markup=ReplyKeyboardRemove()
    )

    # Setup temporary files
    user_data = context.user_data
    file_ids = user_data[MERGE_IDS]
    file_names = user_data[MERGE_NAMES]
    temp_files = [tempfile.NamedTemporaryFile() for _ in range(len(file_ids))]
    merger = PdfFileMerger()

    # Merge PDF files
    for i, file_id in enumerate(file_ids):
        file_name = temp_files[i].name
        file = context.bot.get_file(file_id)
        file.download(custom_path=file_name)

        try:
            merger.append(open(file_name, "rb"))
        except PdfReadError:
            update.effective_message.reply_text(
                _(
                    "I couldn't merge your PDF files "
                    "as this file is invalid: {file_name}"
                ).format(file_name=file_names[i])
            )

            return ConversationHandler.END

    # Send result file
    write_send_pdf(update, context, merger, "files.pdf", TaskType.merge_pdf)

    # Clean up memory and files
    if user_data[MERGE_IDS] == file_ids:
        del user_data[MERGE_IDS]
    if user_data[MERGE_NAMES] == file_names:
        del user_data[MERGE_NAMES]
    for tf in temp_files:
        tf.close()

    return ConversationHandler.END
