import json
import logging
import asyncio
import os
from datetime import datetime, timedelta
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.error import NetworkError, TimedOut, RetryAfter, BadRequest
from flask import Flask, request
import threading
import requests
import time

# --- Robust logging ---
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.WARNING,
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Suppress network error logs
logging.getLogger('httpx').setLevel(logging.ERROR)
logging.getLogger('httpcore').setLevel(logging.ERROR)
logging.getLogger('telegram').setLevel(logging.ERROR)

# Flask app for webhook
app = Flask(__name__)


class SimpleTelegramQuizBot:
    def __init__(self, telegram_token: str):
        self.telegram_token = telegram_token
        self.user_preferences = {}
        self.user_states = {}
        self.last_activity = {}
        self.cleanup_counter = 0
        self.max_retries = 3
        self.retry_delay = 2.0
        self.application = None

    def cleanup_old_data(self):
        """Clean up old user data aggressively"""
        self.cleanup_counter += 1
        if self.cleanup_counter % 15 != 0:
            return

        current_time = datetime.now()
        cutoff_time = current_time - timedelta(hours=1)

        users_to_remove = [
            user_id for user_id, last_seen in self.last_activity.items()
            if last_seen < cutoff_time
        ]

        for user_id in users_to_remove:
            self.user_preferences.pop(user_id, None)
            self.user_states.pop(user_id, None)
            self.last_activity.pop(user_id, None)

    def update_user_activity(self, user_id):
        """Update last activity timestamp"""
        self.last_activity[user_id] = datetime.now()
        self.cleanup_old_data()

    async def safe_send_message(self, chat_id, text, **kwargs):
        """Send message with retry logic"""
        for attempt in range(self.max_retries):
            try:
                bot = self.application.bot
                return await bot.send_message(chat_id=chat_id, text=text, **kwargs)
            except (NetworkError, TimedOut) as e:
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                    continue
                else:
                    return None
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                continue
            except BadRequest:
                return None
            except Exception:
                return None
        return None

    async def safe_edit_message(self, message, text, **kwargs):
        """Edit message with retry logic"""
        for attempt in range(self.max_retries):
            try:
                return await message.edit_text(text, **kwargs)
            except (NetworkError, TimedOut) as e:
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                    continue
                else:
                    return None
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                continue
            except BadRequest:
                return None
            except Exception:
                return None
        return None

    async def safe_send_poll(self, **poll_params):
        """Send poll with retry logic"""
        for attempt in range(self.max_retries):
            try:
                bot = self.application.bot
                return await bot.send_poll(**poll_params)
            except (NetworkError, TimedOut) as e:
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
                    continue
                else:
                    return None
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                continue
            except BadRequest:
                return None
            except Exception:
                return None
        return None

    async def send_quiz_questions(self, questions: list, chat_id: str, is_anonymous: bool = True):
        """Send quiz questions"""
        success_count = 0

        for i, question_data in enumerate(questions, 1):
            try:
                question_text = (question_data.get("q") or question_data.get("question", ""))
                options = (question_data.get("o") or question_data.get("options", []))

                correct_id = question_data.get("c")
                if correct_id is None:
                    correct_id = question_data.get("correct")
                    if correct_id is None:
                        correct_id = question_data.get("correct_option_id", 0)

                explanation = (question_data.get("e") or question_data.get("explanation", ""))

                poll_params = {
                    "chat_id": chat_id,
                    "question": question_text,
                    "options": options,
                    "type": "quiz",
                    "correct_option_id": correct_id,
                    "is_anonymous": is_anonymous
                }

                if explanation:
                    poll_params["explanation"] = explanation

                result = await self.safe_send_poll(**poll_params)
                if result:
                    success_count += 1

                await asyncio.sleep(0.05)

            except Exception:
                pass

        return success_count

    async def get_welcome_messages(self):
        """Get welcome messages"""
        message1 = """🎯 **Simple Quiz Bot** ⚡

✨ Create MCQ quizzes instantly!

💡 **Rules:**
• `q` = question, `o` = options, `c` = correct, `e` = explanation  
• `c` starts from 0 (0=A, 1=B, 2=C, 3=D)
• 2-4 options allowed per question
• Keep short to fit Telegram limits

🚀 **Fast • Reliable • Professional** 🎓"""

        message2 = """{"all_q":[{"q":"Capital of France? 🇫🇷","o":["London","Paris","Berlin","Madrid"],"c":1,"e":"Paris is the capital and largest city of France 🗼"},{"q":"What is 2+2? 🔢","o":["3","4","5","6"],"c":1,"e":"Basic addition: 2+2=4 ✅"}]}"""

        return message1, message2

    async def get_quiz_type_selection_message(self):
        """Get quiz type selection message"""
        return """🎭 **Choose Your Quiz Style:**

🔒 **Anonymous Quiz:**
✅ Can forward to channels and groups
✅ Voters remain private
✅ Perfect for public sharing

👤 **Non-Anonymous Quiz:**  
✅ Shows who answered each question
✅ Great for tracking participation
❌ Cannot be forwarded to channels

**Which style do you prefer?** 👇✨"""

    async def get_json_request_message(self, is_anonymous: bool):
        """Get JSON request message"""
        quiz_type = "🔒 Anonymous" if is_anonymous else "👤 Non-Anonymous"
        return f"""✅ **{quiz_type} Quiz Selected!** 🎉

📝 **Next Steps:**
1️⃣ Copy the above JSON template
2️⃣ Give it to ChatGPT/AI 🤖
3️⃣ Ask to customize with your questions in our format

🚀 **Then send me your customized JSON:** 👇⚡"""

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user_id = update.effective_user.id
        user_name = update.effective_user.first_name or "Friend"

        self.update_user_activity(user_id)
        self.user_states[user_id] = "choosing_type"

        msg1, _ = await self.get_welcome_messages()
        result = await self.safe_send_message(
            update.effective_chat.id,
            f"👋 Hello **{user_name}**! 🌟\n\n{msg1}",
            parse_mode='Markdown'
        )

        if result:
            await self.show_quiz_type_selection(update)

    async def show_quiz_type_selection(self, update):
        """Show quiz type selection"""
        keyboard = [
            [InlineKeyboardButton("🔒 Anonymous Quiz (Can forward to channels)", callback_data="anonymous_true")],
            [InlineKeyboardButton("👤 Non-Anonymous Quiz (Shows who voted)", callback_data="anonymous_false")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        selection_msg = await self.get_quiz_type_selection_message()

        if hasattr(update, 'message') and update.message:
            await self.safe_send_message(
                update.effective_chat.id,
                selection_msg,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )
        else:
            await self.safe_send_message(
                update.effective_chat.id,
                selection_msg,
                reply_markup=reply_markup,
                parse_mode='Markdown'
            )

    async def handle_quiz_type_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle quiz type selection"""
        query = update.callback_query

        try:
            await query.answer()
        except Exception:
            pass

        user_id = query.from_user.id
        is_anonymous = query.data == "anonymous_true"

        self.update_user_activity(user_id)
        self.user_preferences[user_id] = is_anonymous
        self.user_states[user_id] = "waiting_for_json"

        quiz_type = "🔒 Anonymous" if is_anonymous else "👤 Non-Anonymous"

        result = await self.safe_edit_message(
            query.message,
            f"✅ **{quiz_type} Quiz Selected!** 🎉\n\n⏭️ **Next:** JSON template coming... ⚡",
            parse_mode='Markdown'
        )

        if result:
            await asyncio.sleep(0.1)
            _, msg2 = await self.get_welcome_messages()
            await self.safe_send_message(query.message.chat_id, f"{msg2}")
            await asyncio.sleep(0.1)
            json_request = await self.get_json_request_message(is_anonymous)
            await self.safe_send_message(query.message.chat_id, json_request, parse_mode='Markdown')

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        user_id = update.effective_user.id
        self.update_user_activity(user_id)

        help_text = """🆘 **Quiz Bot Help** 📚

🤖 **Commands:**
• `/start` ⭐ - Begin quiz creation
• `/quickstart` ⚡ - Quick 5-step guide
• `/template` 📋 - Get JSON template
• `/help` 🆘 - Show this help
• `/status` 📊 - Check settings
• `/toggle` 🔄 - Switch quiz types

📚 **JSON Format:**
• `all_q` 📝 - Questions array
• `q` ❓ - Question text
• `o` 📝 - Answer options (2-4 choices)
• `c` ✅ - Correct answer (0=A, 1=B, 2=C, 3=D)
• `e` 💡 - Explanation (optional)

💡 **Pro Tip:** Use `/quickstart` for fastest setup! 🚀"""

        await self.safe_send_message(update.effective_chat.id, help_text, parse_mode='Markdown')

    async def template_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /template command"""
        user_id = update.effective_user.id
        self.update_user_activity(user_id)

        template_msg = """📋 **4-Option JSON Template:** 🎯"""
        result1 = await self.safe_send_message(update.effective_chat.id, template_msg, parse_mode='Markdown')

        if result1:
            _, json_template = await self.get_welcome_messages()
            result2 = await self.safe_send_message(update.effective_chat.id, json_template)
            if result2:
                await self.safe_send_message(
                    update.effective_chat.id,
                    "💡 **Copy above template → Give to ChatGPT → Ask to customize with your questions!** 🤖✨",
                    parse_mode='Markdown'
                )

    async def quick_start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /quickstart command"""
        user_id = update.effective_user.id
        self.update_user_activity(user_id)

        quick_msg = """⚡ **Quick Start Guide:** 🚀

1️⃣ Use `/template` to get 4-option JSON format 📋
2️⃣ Copy template → Give to AI (ChatGPT) 🤖  
3️⃣ Ask AI: "Customize with my questions in this format" 💭
4️⃣ Send customized JSON to me 📤
5️⃣ Get instant interactive quizzes! 🎯✨

**Need help?** Use `/help` for detailed guide 📚"""

        await self.safe_send_message(update.effective_chat.id, quick_msg, parse_mode='Markdown')

    async def toggle_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /toggle command"""
        user_id = update.effective_user.id
        self.update_user_activity(user_id)

        keyboard = [
            [InlineKeyboardButton("🔒 Switch to Anonymous", callback_data="anonymous_true")],
            [InlineKeyboardButton("👤 Switch to Non-Anonymous", callback_data="anonymous_false")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        current_type = "🔒 Anonymous" if self.user_preferences.get(user_id, True) else "👤 Non-Anonymous"

        await self.safe_send_message(
            update.effective_chat.id,
            f"⚙️ **Current Setting:** {current_type} 📊\n\n🔄 **Quick Toggle:** Choose your preferred quiz type: 👇✨",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /status command"""
        user_chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        user_name = update.effective_user.first_name or "User"
        self.update_user_activity(user_id)

        is_anonymous = self.user_preferences.get(user_id, True)
        quiz_type = "🔒 Anonymous" if is_anonymous else "👤 Non-Anonymous"
        status_emoji = "🟢" if is_anonymous else "🔵"
        active_users = len(self.user_preferences)

        await self.safe_send_message(
            user_chat_id,
            f"{status_emoji} **Bot Status: Active & Ready!** ⚡\n\n"
            f"👤 **User:** {user_name} 🌟\n"
            f"📍 **Chat ID:** `{user_chat_id}` 🔢\n"
            f"🎯 **Quiz Type:** {quiz_type} 🎭\n"
            f"{'🔐 Perfect for channels & forwarding 📡' if is_anonymous else '👁️ Shows voter participation 📊'}\n"
            f"📊 **Active Users:** {active_users} 👥\n\n"
            f"🚀 **Ready to create amazing quizzes!** ✨",
            parse_mode='Markdown'
        )

    async def restart_cycle(self, update: Update):
        """Restart the welcome cycle"""
        user_id = update.effective_user.id
        self.update_user_activity(user_id)
        self.user_states[user_id] = "choosing_type"

        await asyncio.sleep(0.1)
        restart_msg = f"""🎉 **Ready for another quiz?** ✨"""
        result1 = await self.safe_send_message(update.effective_chat.id, restart_msg, parse_mode='Markdown')

        if result1:
            await asyncio.sleep(0.1)
            msg1, _ = await self.get_welcome_messages()
            result2 = await self.safe_send_message(update.effective_chat.id, msg1, parse_mode='Markdown')
            if result2:
                await asyncio.sleep(0.1)
                await self.show_quiz_type_selection(update)

    async def handle_json_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle JSON messages"""
        user_message = update.message.text.strip()
        user_chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        user_name = update.effective_user.first_name or "User"

        self.update_user_activity(user_id)

        if self.user_states.get(user_id) != "waiting_for_json":
            result = await self.safe_send_message(user_chat_id, "🔄 **Let's start properly!** ✨", parse_mode='Markdown')
            if result:
                await self.start_command(update, None)
            return

        is_anonymous = self.user_preferences.get(user_id, True)
        processing_msg = await self.safe_send_message(user_chat_id, "🔄 **Processing your quiz JSON...** ⚡🎯")

        if not processing_msg:
            return

        try:
            quiz_data = json.loads(user_message)
            questions = quiz_data.get("all_q", quiz_data.get("q", quiz_data.get("all_questions", [])))

            if not questions:
                await self.safe_edit_message(
                    processing_msg,
                    "❌ **No questions found!** 🔍\n\n🔄 **Let's restart with proper format...** 📋",
                    parse_mode='Markdown'
                )
                await asyncio.sleep(0.5)
                await self.restart_cycle(update)
                return

            # Validation logic (same as original)
            for i, question in enumerate(questions):
                question_text = question.get("q") or question.get("question", "")
                options = question.get("o") or question.get("options", [])
                correct_id = question.get("c")
                if correct_id is None:
                    correct_id = question.get("correct")
                    if correct_id is None:
                        correct_id = question.get("correct_option_id", -1)

                # Validation checks
                if not question_text or not options or correct_id is None or correct_id == -1:
                    await self.safe_edit_message(
                        processing_msg,
                        f"❌ **Question {i + 1}: Invalid format** 📝\n\n🔄 **Restarting...** 🔄",
                        parse_mode='Markdown'
                    )
                    await asyncio.sleep(0.3)
                    await self.restart_cycle(update)
                    return

                if not isinstance(options, list) or len(options) < 2 or len(options) > 4:
                    await self.safe_edit_message(
                        processing_msg,
                        f"❌ **Question {i + 1}: Invalid options** 📝\n\n🔄 **Restarting...** 🔄",
                        parse_mode='Markdown'
                    )
                    await asyncio.sleep(0.3)
                    await self.restart_cycle(update)
                    return

                if not isinstance(correct_id, int) or correct_id >= len(options) or correct_id < 0:
                    await self.safe_edit_message(
                        processing_msg,
                        f"❌ **Question {i + 1}: Invalid 'c' value** 🔢\n\n🔄 **Restarting...** 🔄",
                        parse_mode='Markdown'
                    )
                    await asyncio.sleep(0.3)
                    await self.restart_cycle(update)
                    return

            quiz_type = "anonymous" if is_anonymous else "non-anonymous"
            await self.safe_edit_message(
                processing_msg,
                f"✅ **{len(questions)} questions validated!** 🎯\n🚀 Sending {quiz_type} polls... ⚡",
                parse_mode='Markdown'
            )

            success_count = await self.send_quiz_questions(questions, user_chat_id, is_anonymous)

            if success_count == len(questions):
                quiz_type_text = "🔒 Anonymous" if is_anonymous else "👤 Non-Anonymous"
                completion_msg = f"🎯 **{success_count} {quiz_type_text} quizzes sent successfully!** ✅🎉"
                await self.safe_edit_message(processing_msg, completion_msg, parse_mode='Markdown')
                logger.warning(f"Served MCQs to {user_name}")
                await self.restart_cycle(update)
            else:
                await self.safe_edit_message(
                    processing_msg,
                    f"⚠️ **Partial Success:** {success_count}/{len(questions)} questions sent 📊\n\n🔄 **Restarting...** 🔄",
                    parse_mode='Markdown'
                )
                await asyncio.sleep(0.3)
                await self.restart_cycle(update)

        except json.JSONDecodeError:
            await self.safe_edit_message(
                processing_msg,
                "❌ **Invalid JSON Format!** 📋\n\n🔄 **Let's restart with proper format...** ✨",
                parse_mode='Markdown'
            )
            await asyncio.sleep(0.3)
            await self.restart_cycle(update)
        except Exception:
            await self.safe_edit_message(
                processing_msg,
                "❌ **Error occurred!** ⚠️\n\n🔄 **Restarting...** 🔄",
                parse_mode='Markdown'
            )
            await asyncio.sleep(0.3)
            await self.restart_cycle(update)

    async def setup_application(self):
        """Setup Telegram application"""
        self.application = (Application.builder()
                            .token(self.telegram_token)
                            .pool_timeout(60)
                            .connection_pool_size(4)
                            .get_updates_pool_timeout(60)
                            .read_timeout(30)
                            .write_timeout(30)
                            .connect_timeout(30)
                            .build())

        def error_handler(update, context):
            error = context.error
            if isinstance(error, (NetworkError, TimedOut)):
                return
            logger.warning(f"Bot error: {type(error).__name__}")

        self.application.add_error_handler(error_handler)

        # Add all handlers
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("help", self.help_command))
        self.application.add_handler(CommandHandler("template", self.template_command))
        self.application.add_handler(CommandHandler("quickstart", self.quick_start_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("toggle", self.toggle_command))
        self.application.add_handler(CallbackQueryHandler(self.handle_quiz_type_selection))
        self.application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_json_message))

        # Initialize the application
        await self.application.initialize()

        # Set webhook
        render_url = os.environ.get('RENDER_EXTERNAL_URL', 'https://telegram-quiz-bot-render.onrender.com')
        webhook_url = f"{render_url}/webhook"

        try:
            await self.application.bot.set_webhook(url=webhook_url)
            logger.warning(f"✅ Webhook set to: {webhook_url}")
        except Exception as e:
            logger.warning(f"Webhook setup error: {e}")


# Global bot instance - Initialize immediately when module loads
bot_instance = None


def initialize_bot():
    """Initialize bot synchronously"""
    global bot_instance

    TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN environment variable not set!")
        return None

    bot_instance = SimpleTelegramQuizBot(TELEGRAM_TOKEN)

    # Run async setup in event loop
    try:
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot_instance.setup_application())
        logger.warning("🚀 Bot initialized successfully!")
        return bot_instance
    except Exception as e:
        logger.error(f"Bot initialization failed: {e}")
        return None


# Initialize bot when module loads (works with both Gunicorn and direct run)
bot_instance = initialize_bot()


@app.route('/webhook', methods=['POST'])
def webhook():
    """Handle Telegram webhook"""
    global bot_instance
    if bot_instance and bot_instance.application:
        try:
            update_data = request.get_json()
            if update_data:
                update = Update.de_json(update_data, bot_instance.application.bot)
                # Run async function in event loop
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(bot_instance.application.process_update(update))
                loop.close()
            return "OK", 200
        except Exception as e:
            logger.warning(f"Webhook error: {type(e).__name__}")
            return "Error", 500
    return "Bot not ready", 503


@app.route('/health', methods=['GET'])
def health():
    """Health check for UptimeRobot"""
    return "Bot is healthy and running!", 200


@app.route('/', methods=['GET'])
def home():
    """Home page"""
    global bot_instance
    status = "✅ Ready" if bot_instance and bot_instance.application else "❌ Not Ready"
    return f"""
    <h1>🎯 Quiz Bot is Running!</h1>
    <p>Status: {status}</p>
    <p>🔗 Webhook: Ready</p>
    <p>🤖 Telegram Bot: Connected</p>
    <hr>
    <p>Made with ❤️ for creating awesome quizzes!</p>
    """


def keep_alive():
    """Optimized keep-alive for UptimeRobot + Render"""
    def ping():
        while True:
            try:
                # Ping every 12 minutes (shorter than 15min timeout)
                # This works WITH UptimeRobot (every 5min) for redundancy
                time.sleep(12 * 60)
                
                port = os.environ.get('PORT', '10000')
                response = requests.get(
                    f'http://localhost:{port}/health', 
                    timeout=5,
                    headers={'User-Agent': 'KeepAlive-Bot/1.0'}
                )
                
                if response.status_code == 200:
                    logger.warning("🔄 Self-ping successful")
                else:
                    logger.warning(f"⚠️ Self-ping failed: {response.status_code}")
                    
            except Exception as e:
                logger.warning(f"❌ Self-ping error: {type(e).__name__}")
                # Continue running even if ping fails
                pass
    
    thread = threading.Thread(target=ping, daemon=True)
    thread.start()
    logger.warning("🛡️ Keep-alive protection started")

@app.route('/health', methods=['GET', 'HEAD'])
def health():
    """Enhanced health check for UptimeRobot"""
    global bot_instance
    
    # Quick health check
    bot_status = "ready" if (bot_instance and bot_instance.application) else "initializing"
    uptime = time.time()  # Simple uptime indicator
    
    response_data = {
        "status": "healthy",
        "bot": bot_status,
        "uptime": int(uptime),
        "timestamp": datetime.now().isoformat()
    }
    
    # For UptimeRobot HEAD requests (faster)
    if request.method == 'HEAD':
        return "", 200
    
    # For GET requests (debugging)
    return response_data, 200

@app.route('/wake', methods=['GET'])
def wake():
    """Special endpoint for faster wake-up"""
    global bot_instance
    
    if bot_instance and bot_instance.application:
        return {"status": "awake", "bot": "ready"}, 200
    else:
        return {"status": "waking", "bot": "initializing"}, 202
