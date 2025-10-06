import json
import logging
import asyncio
import os
import random
from datetime import datetime, timedelta
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.error import NetworkError, TimedOut, RetryAfter, BadRequest
from flask import Flask, request
import threading
import requests
import time

# --- Enhanced logging for production ---
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.WARNING,
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Suppress verbose network logs
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
        self.retry_delay = 1.5  # Reduced from 2.0
        self.application = None

    def cleanup_old_data(self):
        """Aggressive cleanup for better memory management"""
        self.cleanup_counter += 1
        if self.cleanup_counter % 10 != 0:  # Reduced from 15
            return

        current_time = datetime.now()
        cutoff_time = current_time - timedelta(minutes=30)  # Reduced from 1 hour

        users_to_remove = [
            user_id for user_id, last_seen in self.last_activity.items()
            if last_seen < cutoff_time
        ]

        for user_id in users_to_remove:
            self.user_preferences.pop(user_id, None)
            self.user_states.pop(user_id, None)
            self.last_activity.pop(user_id, None)

        if users_to_remove:
            logger.warning(f"🧹 Cleaned {len(users_to_remove)} inactive users")

    def update_user_activity(self, user_id):
        """Update last activity timestamp"""
        self.last_activity[user_id] = datetime.now()
        self.cleanup_old_data()

    async def safe_send_message(self, chat_id, text, **kwargs):
        """Optimized message sending with reduced timeouts"""
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
                await asyncio.sleep(min(e.retry_after + 1, 5))  # Cap retry delay
                continue
            except BadRequest:
                return None
            except Exception:
                return None
        return None

    async def safe_edit_message(self, message, text, **kwargs):
        """Optimized message editing"""
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
                await asyncio.sleep(min(e.retry_after + 1, 5))
                continue
            except BadRequest:
                return None
            except Exception:
                return None
        return None

    async def safe_send_poll(self, **poll_params):
        """Optimized poll sending"""
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
                await asyncio.sleep(min(e.retry_after + 1, 5))
                continue
            except BadRequest:
                return None
            except Exception:
                return None
        return None

    async def send_quiz_questions(self, questions: list, chat_id: str, is_anonymous: bool = True):
        """Send quiz questions with optimized batch processing"""
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

                # Reduced delay for faster quiz delivery
                await asyncio.sleep(0.03)

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
            await asyncio.sleep(0.05)  # Reduced delay
            _, msg2 = await self.get_welcome_messages()
            await self.safe_send_message(query.message.chat_id, f"{msg2}")
            await asyncio.sleep(0.05)
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

        await asyncio.sleep(0.05)  # Reduced delay
        restart_msg = f"""🎉 **Ready for another quiz?** ✨"""
        result1 = await self.safe_send_message(update.effective_chat.id, restart_msg, parse_mode='Markdown')

        if result1:
            await asyncio.sleep(0.05)
            msg1, _ = await self.get_welcome_messages()
            result2 = await self.safe_send_message(update.effective_chat.id, msg1, parse_mode='Markdown')
            if result2:
                await asyncio.sleep(0.05)
                await self.show_quiz_type_selection(update)

    async def handle_json_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle JSON messages with faster processing"""
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
                await asyncio.sleep(0.3)
                await self.restart_cycle(update)
                return

            # Fast validation logic
            for i, question in enumerate(questions):
                question_text = question.get("q") or question.get("question", "")
                options = question.get("o") or question.get("options", [])
                correct_id = question.get("c")
                if correct_id is None:
                    correct_id = question.get("correct")
                    if correct_id is None:
                        correct_id = question.get("correct_option_id", -1)

                # Quick validation checks
                if not question_text or not options or correct_id is None or correct_id == -1:
                    await self.safe_edit_message(
                        processing_msg,
                        f"❌ **Question {i + 1}: Invalid format** 📝\n\n🔄 **Restarting...** 🔄",
                        parse_mode='Markdown'
                    )
                    await asyncio.sleep(0.2)
                    await self.restart_cycle(update)
                    return

                if not isinstance(options, list) or len(options) < 2 or len(options) > 4:
                    await self.safe_edit_message(
                        processing_msg,
                        f"❌ **Question {i + 1}: Invalid options** 📝\n\n🔄 **Restarting...** 🔄",
                        parse_mode='Markdown'
                    )
                    await asyncio.sleep(0.2)
                    await self.restart_cycle(update)
                    return

                if not isinstance(correct_id, int) or correct_id >= len(options) or correct_id < 0:
                    await self.safe_edit_message(
                        processing_msg,
                        f"❌ **Question {i + 1}: Invalid 'c' value** 🔢\n\n🔄 **Restarting...** 🔄",
                        parse_mode='Markdown'
                    )
                    await asyncio.sleep(0.2)
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
                await asyncio.sleep(0.2)
                await self.restart_cycle(update)

        except json.JSONDecodeError:
            await self.safe_edit_message(
                processing_msg,
                "❌ **Invalid JSON Format!** 📋\n\n🔄 **Let's restart with proper format...** ✨",
                parse_mode='Markdown'
            )
            await asyncio.sleep(0.2)
            await self.restart_cycle(update)
        except Exception:
            await self.safe_edit_message(
                processing_msg,
                "❌ **Error occurred!** ⚠️\n\n🔄 **Restarting...** 🔄",
                parse_mode='Markdown'
            )
            await asyncio.sleep(0.2)
            await self.restart_cycle(update)

    async def setup_application_fast(self):
        """Optimized setup for faster cold starts"""
        try:
            self.application = (Application.builder()
                                .token(self.telegram_token)
                                .pool_timeout(20)          # Reduced from 60
                                .connection_pool_size(2)    # Reduced from 4
                                .get_updates_pool_timeout(30)  # Reduced from 60
                                .read_timeout(15)          # Reduced from 30
                                .write_timeout(15)         # Reduced from 30
                                .connect_timeout(10)       # Reduced from 30
                                .build())

            def error_handler(update, context):
                error = context.error
                if isinstance(error, (NetworkError, TimedOut)):
                    return
                logger.warning(f"Bot error: {type(error).__name__}")

            self.application.add_error_handler(error_handler)

            # Add handlers efficiently
            handlers = [
                CommandHandler("start", self.start_command),
                CommandHandler("help", self.help_command),
                CommandHandler("template", self.template_command),
                CommandHandler("quickstart", self.quick_start_command),
                CommandHandler("status", self.status_command),
                CommandHandler("toggle", self.toggle_command),
                CallbackQueryHandler(self.handle_quiz_type_selection),
                MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_json_message)
            ]

            for handler in handlers:
                self.application.add_handler(handler)

            # Initialize the application
            await self.application.initialize()
            await self.application.start()

            # Set webhook with timeout
            render_url = os.environ.get('RENDER_EXTERNAL_URL', 'https://quiz-bot-tg.onrender.com')
            webhook_url = f"{render_url}/webhook"

            try:
                await self.application.bot.set_webhook(url=webhook_url)
                logger.warning(f"✅ Webhook set to: {webhook_url}")
            except Exception as e:
                logger.warning(f"Webhook setup error: {e}")

        except Exception as e:
            logger.error(f"Application setup failed: {e}")
            raise


# Global bot instance
bot_instance = None


def initialize_bot():
    """Fast bot initialization with better error handling"""
    global bot_instance

    TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
    if not TELEGRAM_TOKEN:
        logger.error("TELEGRAM_TOKEN environment variable not set!")
        return None

    bot_instance = SimpleTelegramQuizBot(TELEGRAM_TOKEN)

    # Optimized async setup
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot_instance.setup_application_fast())
        loop.close()
        logger.warning("🚀 Fast bot init complete!")
        return bot_instance
    except Exception as e:
        logger.error(f"Bot initialization failed: {e}")
        return None


# Initialize bot when module loads
bot_instance = initialize_bot()


@app.route('/webhook', methods=['POST'])
def webhook():
    """Enhanced webhook with better error handling"""
    global bot_instance
    
    if not bot_instance or not bot_instance.application:
        logger.warning("Bot not ready for webhook requests")
        return "Bot initializing", 503
        
    try:
        update_data = request.get_json()
        if not update_data:
            return "No data", 400
            
        update = Update.de_json(update_data, bot_instance.application.bot)
        
        # Use asyncio.run for webhook processing
        asyncio.run(bot_instance.application.process_update(update))
        return "OK", 200
        
    except Exception as e:
        logger.warning(f"Webhook error: {type(e).__name__}")
        return "Processing failed", 500


@app.route('/health', methods=['GET', 'HEAD'])
def health():
    """Enhanced health check for UptimeRobot"""
    global bot_instance

    bot_status = "ready" if (bot_instance and bot_instance.application) else "initializing"
    
    response_data = {
        "status": "healthy",
        "bot": bot_status,
        "uptime": int(time.time()),
        "timestamp": datetime.now().isoformat()
    }

    if request.method == 'HEAD':
        return "", 200

    return response_data, 200


@app.route('/wake', methods=['GET'])
def wake():
    """Fast wake-up endpoint"""
    global bot_instance

    if bot_instance and bot_instance.application:
        return {"status": "awake", "bot": "ready"}, 200
    else:
        return {"status": "waking", "bot": "initializing"}, 202


@app.route('/ping', methods=['GET', 'HEAD'])
def ping():
    """Additional ping endpoint for multiple monitors"""
    return "", 200


@app.route('/heartbeat', methods=['GET'])  
def heartbeat():
    """Heartbeat endpoint"""
    return {"status": "alive", "timestamp": time.time()}, 200


@app.route('/', methods=['GET'])
def home():
    """Home page"""
    global bot_instance
    status = "✅ Ready" if bot_instance and bot_instance.application else "❌ Not Ready"
    active_users = len(bot_instance.user_preferences) if bot_instance else 0
    
    return f"""
    <h1>🎯 Quiz Bot v2.0 - Optimized!</h1>
    <p>Status: {status}</p>
    <p>🔗 Webhook: Ready</p>
    <p>🤖 Telegram Bot: Connected</p>
    <p>👥 Active Users: {active_users}</p>
    <p>⚡ Performance: Enhanced</p>
    <hr>
    <p>Made with ❤️ for creating awesome quizzes!</p>
    """


def enhanced_keep_alive():
    """Multi-endpoint keep-alive for better performance"""
    def ping():
        endpoints = ['/health', '/wake', '/ping', '/heartbeat']
        while True:
            try:
                # Aggressive keep-alive: every 5 minutes with rotation
                time.sleep(5 * 60)
                
                port = os.environ.get('PORT', '10000')
                endpoint = random.choice(endpoints)
                
                response = requests.get(
                    f'http://localhost:{port}{endpoint}',
                    timeout=3,
                    headers={'User-Agent': 'FastKeepAlive-Bot/2.0'}
                )
                
                if response.status_code == 200:
                    logger.warning(f"🔄 Fast-ping {endpoint} successful")
                else:
                    logger.warning(f"⚠️ Fast-ping {endpoint} failed: {response.status_code}")

            except Exception as e:
                logger.warning(f"❌ Fast-ping error: {type(e).__name__}")
                pass

    thread = threading.Thread(target=ping, daemon=True)
    thread.start()
    logger.warning("🛡️ Enhanced keep-alive protection started")


# Start enhanced keep-alive
enhanced_keep_alive()


def main():
    """Main function for direct execution"""
    port = int(os.environ.get('PORT', 10000))
    logger.warning("🚀 Starting optimized Flask server...")
    app.run(host='0.0.0.0', port=port, debug=False)


if __name__ == "__main__":
    main()
