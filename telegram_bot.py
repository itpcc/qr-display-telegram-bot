"""Display the QR code from either text or decoded image."""
import io
import logging
import os
from queue import Empty, Queue
import threading
from threading import Thread, Event
import time
from dotenv import load_dotenv
from PIL import Image
from pyzbar import pyzbar
import qrcode
from telegram import Update, Message
from telegram.ext import Application, ContextTypes, MessageHandler, filters
from st7789v.interface import RaspberryPi
from st7789v import Display

# Global variables
img_queue = Queue()

# Enable logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

# set higher logging level for httpx to avoid all GET and POST requests being logged
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

def thread_display(p2k: Event, q: Queue) -> None:
    """Display thread."""
    logger.info("thread_display | Start.")
    img_dsp_sec = int(os.getenv("IMAGE_DISPLAY_SEC", "10"))

    with RaspberryPi() as rpi:
        display = Display(rpi)

        display.initialize(color_mode=666, inverted=False)
        # Clear display
        display.draw_rgb_bytes([[0, 0, 0]] * 240 * 320)

        t = threading.current_thread()
        is_log_turn_off = False

        logger.info("thread_display | Display init.")
        while getattr(t, "do_run", True) and not p2k.wait(1):
            try:
                data = q.get_nowait()

                logger.info("thread_display | Got data. Displaying")
                is_log_turn_off = False
                data_list = list(data)
                display.turn_on()
                time.sleep(.25)
                display.set_backlight(1)
                display.draw_rgb_bytes(data_list)
                time.sleep(5)
                display.set_color_mode(666)
                display.draw_rgb_bytes(data_list)
                time.sleep(img_dsp_sec)
                logger.debug("thread_display | Display Done")
            except Empty:
                if not is_log_turn_off:
                    logger.info("thread_display | No data data. Turning off.")
                    is_log_turn_off = True
                display.set_backlight(0)
                time.sleep(1)
                display.turn_off()

        logger.info("thread_display | Turning off display.")
        display.set_backlight(0)
        display.turn_off()
        logger.info("thread_display | Stop.")

async def format_image(url: str, msg: Message, img: Image) -> None:
    """Format image to display."""

    canvas = Image.new('RGB', (240, 320), (255, 255, 255))
    canvas.paste(img.resize((240, 240)), (0, int((canvas.size[1] - 240) / 2)))
    buf = io.BytesIO()
    canvas.save(buf, format='PNG')

    logger.debug("format_image | QR enqueue: %s", url)
    img_queue.put(canvas.convert('RGB').getdata())

    logger.info("format_image | Reply image: %s", url)
    await msg.reply_photo(photo=buf.getvalue(), caption=url)

def qr_from_text(url: str) -> Image:
    """Generate QR code from string."""

    logger.debug("qr_from_text | Generaing QR: %s", url)
    # Generate QR code
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=2,
    )
    qr.add_data(url)
    qr.make(fit=True)

    img = qr.make_image(fill_color="black", back_color="white").get_image()

    return img

async def handle_text(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text message."""
    logger.debug("handle_text | Receive: %s", update.message.text)

    url = update.message.text

    await format_image(url, update.message, qr_from_text(url))

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo message."""
    file_id = update.message.photo[-1].file_id
    logger.debug("handle_photo | file_id: %s", file_id)

    # Download file
    logger.debug("handle_photo | Downloading: %s", file_id)
    file = await context.bot.get_file(file_id)
    buf = io.BytesIO()

    await file.download_to_memory(out=buf)
    buf.seek(0)
    logger.debug("handle_photo | Downloaded: %s", file_id)

    img = Image.open(buf)
    logger.debug("handle_photo | Decoding: %s", file_id)
    qr_codes = pyzbar.decode(img)

    first_qr_code = None

    try:
        first_qr_code = next((qr for qr in qr_codes if qr.type == 'QRCODE')) or None
    except StopIteration:
        first_qr_code = None

    if first_qr_code is None:
        logger.warning("handle_photo | No QR code found: %s", file_id)
        await update.message.reply_text("No QR code found")
        return

    url = first_qr_code.data.decode("utf-8")
    logger.info("handle_photo | URL retrieved: %s", url)
    rect = first_qr_code.rect

    qr_img = img.crop( (
        max(10, rect.left - 10),
        max(10, rect.top),
        min(img.width, rect.left + rect.width + 10),
        min(img.height, rect.top + rect.height + 10),
    )) if "bora.dopa" in url else qr_from_text(url)

    await format_image(url, update.message, qr_img)

def main() -> None:
    """Start the bot."""

    logger.info('Main | Application start')

    # Load .env
    load_dotenv()

    telegram_token = os.getenv("TELEGRAM_TOKEN")

    if telegram_token is None:
        logger.error("# Main | TELEGRAM_TOKEN not found in .env")
        return

    # Create the Application and pass it your bot's token.
    application = Application.builder().token(telegram_token).build()
    logger.info('Main | Application built')

    # on non command i.e message - echo the message on Telegram
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text, block=False)
    )
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo, block=False))
    logger.info('Main | Handler added')

    pill2kill = Event()
    thread = Thread(target = thread_display, args = (pill2kill, img_queue, ))
    thread.start()
    logger.info('Main | Thread start')

    # Run the bot until the user presses Ctrl-C
    logger.info('Main | Polling start')
    application.run_polling(allowed_updates=Update.ALL_TYPES)
    logger.info('Main | Polling end')
    thread.do_run = False
    pill2kill.set()
    thread.join()
    logger.info('Main | Thread end')
    logger.info('Main | Stop')

if __name__ == "__main__":
    main()