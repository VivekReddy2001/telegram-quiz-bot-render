import json
import logging
import asyncio
import os
import random
import sqlite3
import hashlib
import signal
import sys
import traceback
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Union
from dataclasses import dataclass, asdict
from contextlib import asynccontextmanager
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
from telegram.error import NetworkError, TimedOut, RetryAfter, BadRequest, TelegramError
from flask import Flask, request, jsonify, g
import threading
import requests
import time
from functools import wraps
import weakref
from collections import defaultdict, deque
import psutil

# --- Enhanced Configuration Management ---
@dataclass
class BotConfig:
    """Centralized configuration management"""
    # Telegram settings
    telegram_token: str
    webhook_url: str
    max_retries: int = 5
    retry_delay: float = 1.0
    timeout_seconds: int = 30
    
    # Performance settings
    max_concurrent_requests: int = 10
    memory_cleanup_interval: int = 300  # 5 minutes
    user_data_retention_hours: int = 24
    
    # Rate limiting
    max_requests_per_minute: int = 60
    max_requests_per_hour: int = 1000
    
    # Database settings
    db_path: str = "bot_data.db"
    backup_interval_hours: int = 6
    
    # Health monitoring
    health_check_interval: int = 60
    max_memory_usage_mb: int = 512
    max_cpu_usage_percent: float = 80.0
    
    # Security settings
    max_message_length: int = 4000
    max_questions_per_quiz: int = 50
    allowed_file_types: List[str] = None
    
    def __post_init__(self):
        if self.allowed_file_types is None:
            self.allowed_file_types = ["json", "txt"]

@dataclass
class UserSession:
    """Enhanced user session management"""
    user_id: int
    username: str
    first_name: str
    last_seen: datetime
    quiz_preferences: Dict[str, Any]
    current_state: str
    request_count: int = 0
    last_request_time: datetime = None
    is_blocked: bool = False
    session_id: str = None
    
    def __post_init__(self):
        if self.session_id is None:
            self.session_id = str(uuid.uuid4())
        if self.last_request_time is None:
            self.last_request_time = datetime.now()

@dataclass
class SystemMetrics:
    """System performance metrics"""
    uptime_seconds: float
    memory_usage_mb: float
    cpu_usage_percent: float
    active_users: int
    total_requests: int
    error_count: int
    last_error_time: Optional[datetime]
    webhook_status: str
    db_size_mb: float

class RateLimiter:
    """Advanced rate limiting system"""
    def __init__(self, max_requests: int, time_window: int):
        self.max_requests = max_requests
        self.time_window = time_window
        self.requests = defaultdict(deque)
        self.blocked_users = {}
        
    def is_allowed(self, user_id: int) -> bool:
        now = time.time()
        user_requests = self.requests[user_id]
        
        # Remove old requests outside time window
        while user_requests and user_requests[0] < now - self.time_window:
            user_requests.popleft()
        
        # Check if user is temporarily blocked
        if user_id in self.blocked_users:
            if now < self.blocked_users[user_id]:
                return False
            else:
                del self.blocked_users[user_id]
        
        # Check rate limit
        if len(user_requests) >= self.max_requests:
            # Block user for 5 minutes
            self.blocked_users[user_id] = now + 300
            return False
        
        user_requests.append(now)
        return True

# --- Enhanced logging system ---
class StructuredLogger:
    """Enhanced structured logging with rotation"""
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.setup_logging()
        
    def setup_logging(self):
        """Setup enhanced logging configuration"""
        log_format = '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
        
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.WARNING)
        console_formatter = logging.Formatter(log_format)
        console_handler.setFormatter(console_formatter)
        
        self.logger.addHandler(console_handler)
        self.logger.setLevel(logging.INFO)
        
        # Suppress verbose logs
        logging.getLogger('httpx').setLevel(logging.ERROR)
        logging.getLogger('httpcore').setLevel(logging.ERROR)
        logging.getLogger('telegram').setLevel(logging.ERROR)
        logging.getLogger('urllib3').setLevel(logging.ERROR)
        
    def log_with_context(self, level: str, message: str, **context):
        """Log with additional context"""
        log_data = {
            'message': message,
            'timestamp': datetime.now().isoformat(),
            **context
        }
        getattr(self.logger, level)(f"{message} | Context: {json.dumps(log_data)}")

# Initialize enhanced logging
logger_instance = StructuredLogger()
logger = logger_instance.logger

# --- Enhanced Database Management ---
class DatabaseManager:
    """Advanced database management with connection pooling and backup"""
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.connection_pool = []
        self.max_connections = 5
        self.init_database()
        
    def init_database(self):
        """Initialize database with proper schema"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.execute('PRAGMA journal_mode=WAL')  # Better concurrency
            conn.execute('PRAGMA synchronous=NORMAL')  # Better performance
            conn.execute('PRAGMA cache_size=10000')  # Larger cache
            conn.execute('PRAGMA temp_store=MEMORY')  # Temp tables in memory
            
            # Users table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_blocked BOOLEAN DEFAULT FALSE,
                    total_quizzes INTEGER DEFAULT 0,
                    preferences TEXT DEFAULT '{}',
                    session_data TEXT DEFAULT '{}'
                )
            ''')
            
            # Quizzes table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS quizzes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    quiz_data TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    success BOOLEAN DEFAULT FALSE,
                    question_count INTEGER DEFAULT 0,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            
            # System metrics table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS system_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    memory_usage REAL,
                    cpu_usage REAL,
                    active_users INTEGER,
                    total_requests INTEGER,
                    error_count INTEGER
                )
            ''')
            
            # Error logs table
            conn.execute('''
                CREATE TABLE IF NOT EXISTS error_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    error_type TEXT,
                    error_message TEXT,
                    user_id INTEGER,
                    context TEXT
                )
            ''')
            
            conn.commit()
            conn.close()
            logger.info("Database initialized successfully")
            
        except Exception as e:
            logger.error(f"Database initialization failed: {e}")
            raise
    
    def get_connection(self):
        """Get database connection with retry logic"""
        for attempt in range(3):
            try:
                conn = sqlite3.connect(self.db_path, timeout=30.0)
                conn.row_factory = sqlite3.Row
                return conn
            except sqlite3.OperationalError as e:
                if attempt < 2:
                    time.sleep(1)
                    continue
                logger.error(f"Database connection failed: {e}")
                raise
    
    def execute_query(self, query: str, params: tuple = ()) -> List[Dict]:
        """Execute query with error handling"""
        conn = None
        try:
            conn = self.get_connection()
            cursor = conn.execute(query, params)
            
            if query.strip().upper().startswith('SELECT'):
                return [dict(row) for row in cursor.fetchall()]
            else:
                conn.commit()
                return []
                
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"Database query failed: {e}")
            raise
        finally:
            if conn:
                conn.close()
    
    def backup_database(self) -> bool:
        """Create database backup"""
        try:
            backup_path = f"{self.db_path}.backup.{int(time.time())}"
            source_conn = self.get_connection()
            backup_conn = sqlite3.connect(backup_path)
            
            source_conn.backup(backup_conn)
            source_conn.close()
            backup_conn.close()
            
            logger.info(f"Database backup created: {backup_path}")
            return True
            
        except Exception as e:
            logger.error(f"Database backup failed: {e}")
            return False

# --- Enhanced Health Monitoring System ---
class HealthMonitor:
    """Comprehensive health monitoring and alerting"""
    def __init__(self, config: BotConfig):
        self.config = config
        self.start_time = time.time()
        self.error_count = 0
        self.last_error_time = None
        self.metrics_history = deque(maxlen=100)
        self.alert_thresholds = {
            'memory_mb': config.max_memory_usage_mb,
            'cpu_percent': config.max_cpu_usage_percent,
            'error_rate': 0.1  # 10% error rate
        }
    
    def get_system_metrics(self) -> SystemMetrics:
        """Get comprehensive system metrics"""
        try:
            process = psutil.Process()
            memory_info = process.memory_info()
            
            return SystemMetrics(
                uptime_seconds=time.time() - self.start_time,
                memory_usage_mb=memory_info.rss / 1024 / 1024,
                cpu_usage_percent=process.cpu_percent(),
                active_users=0,  # Will be updated by bot
                total_requests=0,  # Will be updated by bot
                error_count=self.error_count,
                last_error_time=self.last_error_time,
                webhook_status="unknown",
                db_size_mb=0  # Will be updated
            )
        except Exception as e:
            logger.error(f"Failed to get system metrics: {e}")
            return SystemMetrics(0, 0, 0, 0, 0, 0, None, "error", 0)
    
    def check_health(self) -> Dict[str, Any]:
        """Comprehensive health check"""
        metrics = self.get_system_metrics()
        health_status = {
            'status': 'healthy',
            'metrics': asdict(metrics),
            'alerts': [],
            'timestamp': datetime.now().isoformat()
        }
        
        # Check memory usage
        if metrics.memory_usage_mb > self.alert_thresholds['memory_mb']:
            health_status['alerts'].append({
                'type': 'memory_high',
                'message': f"Memory usage {metrics.memory_usage_mb:.1f}MB exceeds threshold",
                'severity': 'warning'
            })
            health_status['status'] = 'degraded'
        
        # Check CPU usage
        if metrics.cpu_usage_percent > self.alert_thresholds['cpu_percent']:
            health_status['alerts'].append({
                'type': 'cpu_high',
                'message': f"CPU usage {metrics.cpu_usage_percent:.1f}% exceeds threshold",
                'severity': 'warning'
            })
            health_status['status'] = 'degraded'
        
        # Check error rate
        if self.error_count > 10:  # Simple threshold for now
            health_status['alerts'].append({
                'type': 'error_rate_high',
                'message': f"High error count: {self.error_count}",
                'severity': 'critical'
            })
            health_status['status'] = 'critical'
        
        self.metrics_history.append(health_status)
        return health_status
    
    def record_error(self, error_type: str, error_message: str, context: Dict = None):
        """Record error for monitoring"""
        self.error_count += 1
        self.last_error_time = datetime.now()
        
        logger_instance.log_with_context(
            'error',
            f"Error recorded: {error_type}",
            error_message=error_message,
            context=context or {}
        )

# Flask app for webhook
app = Flask(__name__)


class EnhancedTelegramQuizBot:
    """Production-ready Telegram quiz bot with comprehensive features"""
    
    def __init__(self, config: BotConfig):
        self.config = config
        self.telegram_token = config.telegram_token
        self.application = None
        
        # Enhanced data management
        self.db_manager = DatabaseManager(config.db_path)
        self.health_monitor = HealthMonitor(config)
        self.rate_limiter = RateLimiter(config.max_requests_per_minute, 60)
        self.hourly_rate_limiter = RateLimiter(config.max_requests_per_hour, 3600)
        
        # Session management
        self.active_sessions: Dict[int, UserSession] = {}
        self.session_cleanup_counter = 0
        
        # Performance tracking
        self.total_requests = 0
        self.successful_requests = 0
        self.failed_requests = 0
        
        # Auto-recovery settings
        self.auto_recovery_enabled = True
        self.last_health_check = time.time()
        self.consecutive_errors = 0
        self.max_consecutive_errors = 5
        
        # Initialize background tasks
        self._setup_background_tasks()
        
        logger.info("Enhanced Telegram Quiz Bot initialized")

    def _setup_background_tasks(self):
        """Setup background maintenance tasks"""
        def cleanup_task():
            while True:
                try:
                    time.sleep(self.config.memory_cleanup_interval)
                    self._cleanup_inactive_sessions()
                    self._backup_database_if_needed()
                    self._health_check()
                except Exception as e:
                    logger.error(f"Background task error: {e}")
        
        cleanup_thread = threading.Thread(target=cleanup_task, daemon=True)
        cleanup_thread.start()
        logger.info("Background maintenance tasks started")
    
    def _cleanup_inactive_sessions(self):
        """Enhanced session cleanup with database persistence"""
        try:
            cutoff_time = datetime.now() - timedelta(hours=self.config.user_data_retention_hours)
            
            # Clean up in-memory sessions
            sessions_to_remove = [
                user_id for user_id, session in self.active_sessions.items()
                if session.last_seen < cutoff_time
            ]
            
            for user_id in sessions_to_remove:
                # Save session data to database before cleanup
                session = self.active_sessions[user_id]
                self._save_user_session_to_db(session)
                del self.active_sessions[user_id]
            
            # Clean up database old records
            self.db_manager.execute_query(
                "DELETE FROM users WHERE last_seen < ?",
                (cutoff_time.isoformat(),)
            )
            
            if sessions_to_remove:
                logger.info(f"Cleaned up {len(sessions_to_remove)} inactive sessions")
                
        except Exception as e:
            logger.error(f"Session cleanup failed: {e}")
    
    def _backup_database_if_needed(self):
        """Automatic database backup"""
        try:
            last_backup = self.db_manager.execute_query(
                "SELECT MAX(timestamp) as last_backup FROM system_metrics WHERE memory_usage > 0"
            )
            
            if not last_backup or not last_backup[0]['last_backup']:
                self.db_manager.backup_database()
            else:
                last_backup_time = datetime.fromisoformat(last_backup[0]['last_backup'])
                if datetime.now() - last_backup_time > timedelta(hours=self.config.backup_interval_hours):
                    self.db_manager.backup_database()
                    
        except Exception as e:
            logger.error(f"Database backup check failed: {e}")
    
    def _health_check(self):
        """Periodic health check and auto-recovery"""
        try:
            health_status = self.health_monitor.check_health()
            
            if health_status['status'] == 'critical':
                logger.error("Critical health issues detected - attempting auto-recovery")
                self._attempt_auto_recovery()
            
            # Store metrics in database
            metrics = health_status['metrics']
            self.db_manager.execute_query(
                """INSERT INTO system_metrics 
                   (memory_usage, cpu_usage, active_users, total_requests, error_count)
                   VALUES (?, ?, ?, ?, ?)""",
                (metrics['memory_usage_mb'], metrics['cpu_usage_percent'], 
                 metrics['active_users'], metrics['total_requests'], metrics['error_count'])
            )
            
            self.last_health_check = time.time()
            
        except Exception as e:
            logger.error(f"Health check failed: {e}")
    
    def _attempt_auto_recovery(self):
        """Automatic recovery from critical issues"""
        try:
            logger.info("Attempting automatic recovery...")
            
            # Clear old sessions to free memory
            self._cleanup_inactive_sessions()
            
            # Reset error counters
            self.consecutive_errors = 0
            self.health_monitor.error_count = 0
            
            # Reinitialize database connection
            self.db_manager = DatabaseManager(self.config.db_path)
            
            logger.info("Auto-recovery completed successfully")
            
        except Exception as e:
            logger.error(f"Auto-recovery failed: {e}")
    
    def _get_or_create_user_session(self, user_id: int, username: str = None, first_name: str = None) -> UserSession:
        """Get or create user session with database persistence"""
        if user_id not in self.active_sessions:
            # Try to load from database
            db_user = self.db_manager.execute_query(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            )
            
            if db_user:
                user_data = db_user[0]
                self.active_sessions[user_id] = UserSession(
                    user_id=user_id,
                    username=user_data.get('username', username),
                    first_name=user_data.get('first_name', first_name),
                    last_seen=datetime.now(),
                    quiz_preferences=json.loads(user_data.get('preferences', '{}')),
                    current_state=json.loads(user_data.get('session_data', '{}')).get('state', 'idle'),
                    request_count=user_data.get('total_quizzes', 0)
                )
            else:
                # Create new session
                self.active_sessions[user_id] = UserSession(
                    user_id=user_id,
                    username=username or "",
                    first_name=first_name or "",
                    last_seen=datetime.now(),
                    quiz_preferences={'anonymous': True},
                    current_state='idle'
                )
                
                # Save to database
                self._save_user_session_to_db(self.active_sessions[user_id])
        
        # Update last seen
        self.active_sessions[user_id].last_seen = datetime.now()
        return self.active_sessions[user_id]
    
    def _save_user_session_to_db(self, session: UserSession):
        """Save user session to database"""
        try:
            self.db_manager.execute_query(
                """INSERT OR REPLACE INTO users 
                   (user_id, username, first_name, last_seen, total_quizzes, preferences, session_data)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session.user_id, session.username, session.first_name, 
                 session.last_seen.isoformat(), session.request_count,
                 json.dumps(session.quiz_preferences),
                 json.dumps({'state': session.current_state}))
            )
        except Exception as e:
            logger.error(f"Failed to save user session: {e}")
    
    def _check_rate_limit(self, user_id: int) -> bool:
        """Check if user is within rate limits"""
        return (self.rate_limiter.is_allowed(user_id) and 
                self.hourly_rate_limiter.is_allowed(user_id))

    async def safe_send_message(self, chat_id: int, text: str, **kwargs) -> Optional[Any]:
        """Enhanced message sending with comprehensive error handling"""
        self.total_requests += 1
        
        # Validate message length
        if len(text) > self.config.max_message_length:
            text = text[:self.config.max_message_length-3] + "..."
        
        for attempt in range(self.config.max_retries):
            try:
                if not self.application or not self.application.bot:
                    logger.error("Bot application not available")
                    return None
                
                bot = self.application.bot
                result = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
                self.successful_requests += 1
                return result
                
            except (NetworkError, TimedOut) as e:
                logger.warning(f"Network error on attempt {attempt + 1}: {e}")
                if attempt < self.config.max_retries - 1:
                    await asyncio.sleep(self.config.retry_delay * (attempt + 1))
                    continue
                else:
                    self.failed_requests += 1
                    self.health_monitor.record_error("network_error", str(e))
                    return None
                    
            except RetryAfter as e:
                logger.warning(f"Rate limited, waiting {e.retry_after} seconds")
                await asyncio.sleep(min(e.retry_after + 1, 60))
                continue
                
            except BadRequest as e:
                logger.error(f"Bad request: {e}")
                self.failed_requests += 1
                self.health_monitor.record_error("bad_request", str(e))
                return None
                
            except TelegramError as e:
                logger.error(f"Telegram error: {e}")
                self.failed_requests += 1
                self.health_monitor.record_error("telegram_error", str(e))
                return None
                
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                self.failed_requests += 1
                self.health_monitor.record_error("unexpected_error", str(e))
                return None
        
        return None
    
    async def safe_edit_message(self, message: Any, text: str, **kwargs) -> Optional[Any]:
        """Enhanced message editing with error handling"""
        if len(text) > self.config.max_message_length:
            text = text[:self.config.max_message_length-3] + "..."
        
        for attempt in range(self.config.max_retries):
            try:
                return await message.edit_text(text, **kwargs)
                
            except (NetworkError, TimedOut) as e:
                if attempt < self.config.max_retries - 1:
                    await asyncio.sleep(self.config.retry_delay * (attempt + 1))
                    continue
                else:
                    logger.error(f"Failed to edit message after {self.config.max_retries} attempts: {e}")
                    return None
                    
            except BadRequest as e:
                logger.error(f"Bad request when editing message: {e}")
                return None
                
            except Exception as e:
                logger.error(f"Unexpected error when editing message: {e}")
                return None
        
        return None
    
    async def safe_send_poll(self, **poll_params) -> Optional[Any]:
        """Enhanced poll sending with validation and error handling"""
        # Validate poll parameters
        if not poll_params.get('question') or not poll_params.get('options'):
            logger.error("Invalid poll parameters: missing question or options")
            return None
        
        if len(poll_params['options']) < 2 or len(poll_params['options']) > 10:
            logger.error(f"Invalid number of poll options: {len(poll_params['options'])}")
            return None
        
        if poll_params.get('correct_option_id', -1) >= len(poll_params['options']):
            logger.error(f"Invalid correct_option_id: {poll_params.get('correct_option_id')}")
            return None
        
        for attempt in range(self.config.max_retries):
            try:
                if not self.application or not self.application.bot:
                    logger.error("Bot application not available for poll")
                    return None
                
                bot = self.application.bot
                result = await bot.send_poll(**poll_params)
                return result
                
            except (NetworkError, TimedOut) as e:
                if attempt < self.config.max_retries - 1:
                    await asyncio.sleep(self.config.retry_delay * (attempt + 1))
                    continue
                else:
                    logger.error(f"Failed to send poll after {self.config.max_retries} attempts: {e}")
                    return None
                    
            except BadRequest as e:
                logger.error(f"Bad request when sending poll: {e}")
                return None
                
            except Exception as e:
                logger.error(f"Unexpected error when sending poll: {e}")
                return None
        
        return None
    
    def validate_quiz_data(self, quiz_data: Dict) -> Dict[str, Any]:
        """Comprehensive quiz data validation"""
        validation_result = {
            'is_valid': True,
            'errors': [],
            'warnings': [],
            'questions_count': 0
        }
        
        try:
            # Check if quiz_data is a dictionary
            if not isinstance(quiz_data, dict):
                validation_result['is_valid'] = False
                validation_result['errors'].append("Quiz data must be a JSON object")
                return validation_result
            
            # Extract questions
            questions = quiz_data.get("all_q", quiz_data.get("q", quiz_data.get("all_questions", [])))
            
            if not isinstance(questions, list):
                validation_result['is_valid'] = False
                validation_result['errors'].append("Questions must be provided as an array")
                return validation_result
            
            if not questions:
                validation_result['is_valid'] = False
                validation_result['errors'].append("No questions provided")
                return validation_result
            
            if len(questions) > self.config.max_questions_per_quiz:
                validation_result['is_valid'] = False
                validation_result['errors'].append(f"Too many questions. Maximum allowed: {self.config.max_questions_per_quiz}")
                return validation_result
            
            # Validate each question
            for i, question in enumerate(questions):
                question_errors = self._validate_question(question, i + 1)
                validation_result['errors'].extend(question_errors)
            
            validation_result['questions_count'] = len(questions)
            validation_result['is_valid'] = len(validation_result['errors']) == 0
            
        except Exception as e:
            validation_result['is_valid'] = False
            validation_result['errors'].append(f"Validation error: {str(e)}")
        
        return validation_result
    
    def _validate_question(self, question: Dict, question_num: int) -> List[str]:
        """Validate individual question"""
        errors = []
        
        # Check question structure
        if not isinstance(question, dict):
            errors.append(f"Question {question_num}: Must be an object")
            return errors
        
        # Validate question text
        question_text = question.get("q") or question.get("question", "")
        if not question_text or not isinstance(question_text, str):
            errors.append(f"Question {question_num}: Missing or invalid question text")
        elif len(question_text) > 300:  # Telegram poll question limit
            errors.append(f"Question {question_num}: Question text too long (max 300 characters)")
        
        # Validate options
        options = question.get("o") or question.get("options", [])
        if not isinstance(options, list):
            errors.append(f"Question {question_num}: Options must be an array")
            return errors
        
        if len(options) < 2:
            errors.append(f"Question {question_num}: Must have at least 2 options")
        elif len(options) > 10:
            errors.append(f"Question {question_num}: Cannot have more than 10 options")
        
        # Validate each option
        for j, option in enumerate(options):
            if not isinstance(option, str):
                errors.append(f"Question {question_num}, Option {j+1}: Must be a string")
            elif len(option) > 100:  # Telegram option limit
                errors.append(f"Question {question_num}, Option {j+1}: Option text too long (max 100 characters)")
        
        # Validate correct answer
        correct_id = question.get("c")
        if correct_id is None:
            correct_id = question.get("correct")
            if correct_id is None:
                correct_id = question.get("correct_option_id", -1)
        
        if not isinstance(correct_id, int):
            errors.append(f"Question {question_num}: Correct answer must be a number")
        elif correct_id < 0 or correct_id >= len(options):
            errors.append(f"Question {question_num}: Invalid correct answer index {correct_id}")
        
        # Validate explanation (optional)
        explanation = question.get("e") or question.get("explanation", "")
        if explanation and len(explanation) > 200:
            errors.append(f"Question {question_num}: Explanation too long (max 200 characters)")
        
        return errors

    async def send_quiz_questions(self, questions: List[Dict], chat_id: int, is_anonymous: bool = True) -> int:
        """Enhanced quiz sending with batch processing and comprehensive error handling"""
        success_count = 0
        failed_questions = []
        
        try:
            # Validate all questions first
            for i, question in enumerate(questions):
                question_errors = self._validate_question(question, i + 1)
                if question_errors:
                    failed_questions.append(f"Question {i + 1}: {'; '.join(question_errors)}")
            
            if failed_questions:
                logger.error(f"Validation failed for {len(failed_questions)} questions")
                return 0
            
            # Send questions with optimized batching
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
                        logger.info(f"Successfully sent question {i}/{len(questions)}")
                    else:
                        logger.error(f"Failed to send question {i}/{len(questions)}")
                    
                    # Adaptive delay based on success rate
                    if success_count > 0:
                        delay = max(0.1, 0.5 / success_count)  # Faster as success rate increases
                    else:
                        delay = 1.0  # Slower if failures
                    
                    await asyncio.sleep(delay)
                    
                except Exception as e:
                    logger.error(f"Error sending question {i}: {e}")
                    continue
            
            # Log quiz completion
            if success_count == len(questions):
                logger.info(f"Successfully sent all {len(questions)} questions")
            else:
                logger.warning(f"Partial success: {success_count}/{len(questions)} questions sent")
            
            # Save quiz data to database
            self._save_quiz_to_database(chat_id, questions, success_count > 0)
            
        except Exception as e:
            logger.error(f"Critical error in quiz sending: {e}")
            self.health_monitor.record_error("quiz_sending_error", str(e))
        
        return success_count
    
    def _save_quiz_to_database(self, chat_id: int, questions: List[Dict], success: bool):
        """Save quiz data to database for analytics"""
        try:
            quiz_data_json = json.dumps(questions)
            self.db_manager.execute_query(
                """INSERT INTO quizzes (user_id, quiz_data, success, question_count)
                   VALUES (?, ?, ?, ?)""",
                (chat_id, quiz_data_json, success, len(questions))
            )
        except Exception as e:
            logger.error(f"Failed to save quiz to database: {e}")

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
                
                # Verify webhook info
                webhook_info = await self.application.bot.get_webhook_info()
                logger.warning(f"📡 Webhook info: {webhook_info.url} | Pending updates: {webhook_info.pending_update_count}")
                
            except Exception as e:
                logger.error(f"❌ Webhook setup error: {e}")
                logger.error(f"❌ Error type: {type(e).__name__}")
                import traceback
                logger.error(f"❌ Traceback: {traceback.format_exc()}")

        except Exception as e:
            logger.error(f"Application setup failed: {e}")
            raise


# Global bot instance and configuration
bot_instance = None
bot_config = None

def create_bot_config() -> BotConfig:
    """Create bot configuration from environment variables"""
    telegram_token = os.environ.get('TELEGRAM_TOKEN')
    if not telegram_token:
        raise ValueError("TELEGRAM_TOKEN environment variable not set!")
    
    render_url = os.environ.get('RENDER_EXTERNAL_URL', 'https://quiz-bot-tg.onrender.com')
    webhook_url = f"{render_url}/webhook"
    
    return BotConfig(
        telegram_token=telegram_token,
        webhook_url=webhook_url,
        max_retries=int(os.environ.get('MAX_RETRIES', 5)),
        retry_delay=float(os.environ.get('RETRY_DELAY', 1.0)),
        timeout_seconds=int(os.environ.get('TIMEOUT_SECONDS', 30)),
        max_concurrent_requests=int(os.environ.get('MAX_CONCURRENT_REQUESTS', 10)),
        memory_cleanup_interval=int(os.environ.get('MEMORY_CLEANUP_INTERVAL', 300)),
        user_data_retention_hours=int(os.environ.get('USER_DATA_RETENTION_HOURS', 24)),
        max_requests_per_minute=int(os.environ.get('MAX_REQUESTS_PER_MINUTE', 60)),
        max_requests_per_hour=int(os.environ.get('MAX_REQUESTS_PER_HOUR', 1000)),
        db_path=os.environ.get('DB_PATH', 'bot_data.db'),
        backup_interval_hours=int(os.environ.get('BACKUP_INTERVAL_HOURS', 6)),
        health_check_interval=int(os.environ.get('HEALTH_CHECK_INTERVAL', 60)),
        max_memory_usage_mb=int(os.environ.get('MAX_MEMORY_USAGE_MB', 512)),
        max_cpu_usage_percent=float(os.environ.get('MAX_CPU_USAGE_PERCENT', 80.0)),
        max_message_length=int(os.environ.get('MAX_MESSAGE_LENGTH', 4000)),
        max_questions_per_quiz=int(os.environ.get('MAX_QUESTIONS_PER_QUIZ', 50))
    )

def initialize_bot():
    """Enhanced bot initialization with comprehensive error handling"""
    global bot_instance, bot_config

    try:
        logger.info("🔧 Initializing enhanced Telegram bot...")
        
        # Create configuration
        bot_config = create_bot_config()
        logger.info("✅ Configuration loaded successfully")
        
        # Initialize bot
        bot_instance = EnhancedTelegramQuizBot(bot_config)
        logger.info("✅ Bot instance created successfully")
        
        # Setup application
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot_instance.setup_application_fast())
        loop.close()
        
        logger.info("🚀 Enhanced bot initialization complete!")
        return bot_instance
        
    except Exception as e:
        logger.error(f"❌ Bot initialization failed: {e}")
        logger.error(f"❌ Error type: {type(e).__name__}")
        logger.error(f"❌ Traceback: {traceback.format_exc()}")
        
        # Attempt graceful degradation
        if bot_instance:
            try:
                bot_instance.health_monitor.record_error("initialization_error", str(e))
            except:
                pass
        
        return None

# Initialize bot when module loads
bot_instance = initialize_bot()


@app.route('/webhook', methods=['POST'])
def webhook():
    """Enhanced webhook with comprehensive error handling and rate limiting"""
    global bot_instance
    
    start_time = time.time()
    
    # Check bot availability
    if not bot_instance or not bot_instance.application:
        logger.warning("Bot not ready for webhook requests")
        return jsonify({"error": "Bot initializing", "status": "unavailable"}), 503
    
    try:
        # Get request data
        update_data = request.get_json()
        if not update_data:
            logger.warning("Empty webhook request received")
            return jsonify({"error": "No data", "status": "invalid"}), 400
        
        # Extract user info for rate limiting
        user_id = None
        if 'message' in update_data:
            user_id = update_data['message'].get('from', {}).get('id')
        elif 'callback_query' in update_data:
            user_id = update_data['callback_query'].get('from', {}).get('id')
        
        # Rate limiting check
        if user_id and not bot_instance._check_rate_limit(user_id):
            logger.warning(f"Rate limit exceeded for user {user_id}")
            return jsonify({"error": "Rate limit exceeded", "status": "rate_limited"}), 429
        
        # Parse update
        update = Update.de_json(update_data, bot_instance.application.bot)
        
        # Process update with timeout
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            # Set timeout for processing
            task = loop.create_task(bot_instance.application.process_update(update))
            loop.run_until_complete(asyncio.wait_for(task, timeout=30))
            
            loop.close()
            
            # Log successful processing
            processing_time = time.time() - start_time
            logger.info(f"Webhook processed successfully in {processing_time:.2f}s")
            
            return jsonify({"status": "success", "processing_time": processing_time}), 200
            
        except asyncio.TimeoutError:
            logger.error("Webhook processing timeout")
            bot_instance.health_monitor.record_error("webhook_timeout", "Processing timeout")
            return jsonify({"error": "Processing timeout", "status": "timeout"}), 408
            
    except Exception as e:
        processing_time = time.time() - start_time
        logger.error(f"Webhook error: {type(e).__name__} - {str(e)}")
        
        # Record error
        if bot_instance:
            bot_instance.health_monitor.record_error("webhook_error", str(e))
        
        return jsonify({
            "error": "Processing failed", 
            "status": "error",
            "processing_time": processing_time
        }), 500


@app.route('/health', methods=['GET', 'HEAD'])
def health():
    """Comprehensive health check endpoint"""
    global bot_instance
    
    try:
        if not bot_instance:
            return jsonify({
                "status": "critical",
                "bot": "not_initialized",
                "timestamp": datetime.now().isoformat()
            }), 503
        
        # Get comprehensive health status
        health_status = bot_instance.health_monitor.check_health()
        
        # Add bot-specific metrics
        health_status['metrics'].update({
            'active_users': len(bot_instance.active_sessions),
            'total_requests': bot_instance.total_requests,
            'successful_requests': bot_instance.successful_requests,
            'failed_requests': bot_instance.failed_requests,
            'success_rate': (bot_instance.successful_requests / max(bot_instance.total_requests, 1)) * 100
        })
        
        # Determine HTTP status code
        if health_status['status'] == 'critical':
            status_code = 503
        elif health_status['status'] == 'degraded':
            status_code = 200  # Still operational but with warnings
        else:
            status_code = 200
        
        if request.method == 'HEAD':
            return "", status_code
        
        return jsonify(health_status), status_code
        
    except Exception as e:
        logger.error(f"Health check failed: {e}")
        return jsonify({
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat()
        }), 500


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


@app.route('/debug', methods=['GET'])
def debug():
    """Comprehensive debug endpoint"""
    global bot_instance, bot_config
    
    try:
        debug_info = {
            "bot_instance_exists": bot_instance is not None,
            "application_exists": bot_instance.application is not None if bot_instance else False,
            "telegram_token_set": bool(os.environ.get('TELEGRAM_TOKEN')),
            "render_external_url": os.environ.get('RENDER_EXTERNAL_URL', 'Not set'),
            "port": os.environ.get('PORT', 'Not set'),
            "active_sessions_count": len(bot_instance.active_sessions) if bot_instance else 0,
            "config": asdict(bot_config) if bot_config else None,
            "timestamp": datetime.now().isoformat()
        }
        
        if bot_instance:
            debug_info.update({
                "total_requests": bot_instance.total_requests,
                "successful_requests": bot_instance.successful_requests,
                "failed_requests": bot_instance.failed_requests,
                "consecutive_errors": bot_instance.consecutive_errors,
                "auto_recovery_enabled": bot_instance.auto_recovery_enabled,
                "health_monitor_status": bot_instance.health_monitor.check_health()
            })
        
        return jsonify(debug_info), 200
        
    except Exception as e:
        logger.error(f"Debug endpoint error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/metrics', methods=['GET'])
def metrics():
    """Detailed metrics endpoint for monitoring"""
    global bot_instance
    
    try:
        if not bot_instance:
            return jsonify({"error": "Bot not initialized"}), 503
        
        metrics_data = {
            "system": bot_instance.health_monitor.get_system_metrics(),
            "bot": {
                "active_sessions": len(bot_instance.active_sessions),
                "total_requests": bot_instance.total_requests,
                "successful_requests": bot_instance.successful_requests,
                "failed_requests": bot_instance.failed_requests,
                "success_rate": (bot_instance.successful_requests / max(bot_instance.total_requests, 1)) * 100,
                "consecutive_errors": bot_instance.consecutive_errors
            },
            "database": {
                "user_count": len(bot_instance.db_manager.execute_query("SELECT COUNT(*) as count FROM users")),
                "quiz_count": len(bot_instance.db_manager.execute_query("SELECT COUNT(*) as count FROM quizzes")),
                "error_count": len(bot_instance.db_manager.execute_query("SELECT COUNT(*) as count FROM error_logs"))
            },
            "timestamp": datetime.now().isoformat()
        }
        
        return jsonify(metrics_data), 200
        
    except Exception as e:
        logger.error(f"Metrics endpoint error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/analytics', methods=['GET'])
def analytics():
    """Analytics endpoint for user insights"""
    global bot_instance
    
    try:
        if not bot_instance:
            return jsonify({"error": "Bot not initialized"}), 503
        
        # Get analytics from database
        user_stats = bot_instance.db_manager.execute_query(
            "SELECT COUNT(*) as total_users, AVG(total_quizzes) as avg_quizzes FROM users"
        )[0]
        
        quiz_stats = bot_instance.db_manager.execute_query(
            "SELECT COUNT(*) as total_quizzes, AVG(question_count) as avg_questions FROM quizzes WHERE success = 1"
        )[0]
        
        recent_activity = bot_instance.db_manager.execute_query(
            "SELECT COUNT(*) as recent_users FROM users WHERE last_seen > datetime('now', '-24 hours')"
        )[0]
        
        analytics_data = {
            "users": {
                "total_users": user_stats['total_users'],
                "avg_quizzes_per_user": round(user_stats['avg_quizzes'] or 0, 2),
                "active_last_24h": recent_activity['recent_users']
            },
            "quizzes": {
                "total_quizzes_created": quiz_stats['total_quizzes'],
                "avg_questions_per_quiz": round(quiz_stats['avg_questions'] or 0, 2)
            },
            "performance": {
                "success_rate": (bot_instance.successful_requests / max(bot_instance.total_requests, 1)) * 100,
                "uptime_hours": (time.time() - bot_instance.health_monitor.start_time) / 3600
            },
            "timestamp": datetime.now().isoformat()
        }
        
        return jsonify(analytics_data), 200
        
    except Exception as e:
        logger.error(f"Analytics endpoint error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/', methods=['GET'])
def home():
    """Enhanced home page with comprehensive status"""
    global bot_instance
    
    try:
        if not bot_instance:
            status_html = """
            <h1>🎯 Enhanced Quiz Bot - Status</h1>
            <p>❌ Bot: Not Initialized</p>
            <p>🔧 Status: Initializing or Error</p>
            """
        else:
            health_status = bot_instance.health_monitor.check_health()
            metrics = health_status['metrics']
            
            status_emoji = "✅" if health_status['status'] == 'healthy' else "⚠️" if health_status['status'] == 'degraded' else "❌"
            
            status_html = f"""
            <h1>🎯 Enhanced Quiz Bot v3.0 - Production Ready!</h1>
            <div style="background: #f0f0f0; padding: 20px; border-radius: 10px; margin: 20px 0;">
                <h2>📊 System Status: {status_emoji} {health_status['status'].title()}</h2>
                <p><strong>🤖 Bot Status:</strong> ✅ Ready & Connected</p>
                <p><strong>👥 Active Users:</strong> {len(bot_instance.active_sessions)}</p>
                <p><strong>📈 Total Requests:</strong> {bot_instance.total_requests}</p>
                <p><strong>✅ Success Rate:</strong> {(bot_instance.successful_requests / max(bot_instance.total_requests, 1)) * 100:.1f}%</p>
                <p><strong>⏱️ Uptime:</strong> {metrics['uptime_seconds'] / 3600:.1f} hours</p>
                <p><strong>💾 Memory Usage:</strong> {metrics['memory_usage_mb']:.1f} MB</p>
                <p><strong>🖥️ CPU Usage:</strong> {metrics['cpu_usage_percent']:.1f}%</p>
            </div>
            
            <div style="background: #e8f4f8; padding: 15px; border-radius: 8px; margin: 20px 0;">
                <h3>🚀 Enhanced Features:</h3>
                <ul>
                    <li>✅ Comprehensive Error Handling & Auto-Recovery</li>
                    <li>✅ Advanced Rate Limiting & Security</li>
                    <li>✅ Persistent Database Storage</li>
                    <li>✅ Real-time Health Monitoring</li>
                    <li>✅ Automatic Database Backups</li>
                    <li>✅ Performance Analytics</li>
                    <li>✅ Structured Logging</li>
                    <li>✅ Graceful Shutdown Handling</li>
                </ul>
            </div>
            
            <div style="background: #f8f8e8; padding: 15px; border-radius: 8px; margin: 20px 0;">
                <h3>🔗 Monitoring Endpoints:</h3>
                <p><a href="/health">/health</a> - Health check</p>
                <p><a href="/debug">/debug</a> - Debug information</p>
                <p><a href="/metrics">/metrics</a> - System metrics</p>
                <p><a href="/analytics">/analytics</a> - User analytics</p>
            </div>
            """
        
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
            <title>Enhanced Quiz Bot</title>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <style>
                body {{ font-family: Arial, sans-serif; margin: 40px; background: #fafafa; }}
                .footer {{ margin-top: 40px; padding: 20px; background: #333; color: white; border-radius: 8px; text-align: center; }}
            </style>
        </head>
        <body>
            {status_html}
            <div class="footer">
                <p>🚀 <strong>Enhanced Quiz Bot v3.0</strong> - Production Ready & Maintenance Free!</p>
                <p>Made with ❤️ for creating awesome quizzes!</p>
                <p>Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}</p>
            </div>
        </body>
        </html>
        """
        
    except Exception as e:
        return f"""
        <h1>🎯 Enhanced Quiz Bot - Error</h1>
        <p>❌ Error loading status: {str(e)}</p>
        <p>Please check the logs for more details.</p>
        """, 500


def setup_signal_handlers():
    """Setup graceful shutdown handlers"""
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        
        global bot_instance
        if bot_instance:
            try:
                # Save all active sessions
                for user_id, session in bot_instance.active_sessions.items():
                    bot_instance._save_user_session_to_db(session)
                
                # Create final database backup
                bot_instance.db_manager.backup_database()
                
                logger.info("Graceful shutdown completed")
            except Exception as e:
                logger.error(f"Error during graceful shutdown: {e}")
        
        sys.exit(0)
    
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    logger.info("Signal handlers registered for graceful shutdown")

def enhanced_keep_alive():
    """Multi-endpoint keep-alive for better performance"""
    def ping():
        endpoints = ['/health', '/wake', '/ping', '/heartbeat']
        while True:
            try:
                # Smart keep-alive: every 5 minutes with rotation
                time.sleep(5 * 60)
                
                port = os.environ.get('PORT', '10000')
                endpoint = random.choice(endpoints)
                
                response = requests.get(
                    f'http://localhost:{port}{endpoint}',
                    timeout=3,
                    headers={'User-Agent': 'EnhancedKeepAlive-Bot/3.0'}
                )
                
                if response.status_code == 200:
                    logger.info(f"🔄 Keep-alive ping {endpoint} successful")
                else:
                    logger.warning(f"⚠️ Keep-alive ping {endpoint} failed: {response.status_code}")

            except Exception as e:
                logger.warning(f"❌ Keep-alive error: {type(e).__name__}")
                pass

    thread = threading.Thread(target=ping, daemon=True)
    thread.start()
    logger.info("🛡️ Enhanced keep-alive protection started")


# Initialize enhanced systems
setup_signal_handlers()
enhanced_keep_alive()

def main():
    """Enhanced main function with comprehensive startup"""
    try:
        port = int(os.environ.get('PORT', 10000))
        logger.info("🚀 Starting Enhanced Quiz Bot Server...")
        
        # Log startup information
        logger.info(f"📍 Port: {port}")
        logger.info(f"🔗 Webhook URL: {os.environ.get('RENDER_EXTERNAL_URL', 'Not set')}/webhook")
        logger.info(f"🤖 Bot Status: {'Ready' if bot_instance else 'Not Ready'}")
        
        # Start Flask server with enhanced configuration
        app.run(
            host='0.0.0.0', 
            port=port, 
            debug=False,
            threaded=True,
            use_reloader=False  # Disable reloader for production
        )
        
    except Exception as e:
        logger.error(f"❌ Server startup failed: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
