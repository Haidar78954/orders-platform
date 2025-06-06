import aiomysql
import pymysql
import asyncio
import json
import hashlib
import time
import sys
import random
import re
import string
import os
import pytz
import uuid
import math
import logging
from datetime import datetime, timedelta
from urllib.parse import unquote
from apscheduler.schedulers.background import BackgroundScheduler
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import NetworkError, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    CallbackContext,
    filters
)
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import ReplyKeyboardMarkup, Update
from telegram.ext import CallbackContext



file_handler = logging.FileHandler("errors.log")
stream_handler = logging.StreamHandler(sys.stdout)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[file_handler, stream_handler]
)

logger = logging.getLogger(__name__)



# متغير عالمي لتخزين الطلبات إن لزم
user_orders = {}


# محدد معدل الطلبات
class RateLimiter:
    def __init__(self, max_calls, period):
        self.max_calls = max_calls
        self.period = period
        self.calls = deque()

    async def acquire(self):
        now = time.time()

        # إزالة الطلبات القديمة
        while self.calls and self.calls[0] < now - self.period:
            self.calls.popleft()

        # إذا وصلنا للحد الأقصى، انتظر
        if len(self.calls) >= self.max_calls:
            wait_time = self.calls[0] + self.period - now
            await asyncio.sleep(wait_time)

        # تسجيل الطلب الجديد
        self.calls.append(time.time())

# إنشاء محدد معدل للطلبات
telegram_limiter = RateLimiter(max_calls=30, period=1)  # 30 طلب في الثانية

# دالة لإرسال رسالة مع إعادة المحاولة
async def send_message_with_retry(bot, chat_id, text, order_id=None, max_retries=5, **kwargs):
    message_id = str(uuid.uuid4())  # إنشاء معرف فريد للرسالة
    
    # محاولة إرسال الرسالة مع إعادة المحاولة
    for attempt in range(max_retries):
        try:
            # تطبيق محدد معدل الطلبات
            await telegram_limiter.acquire()
            
            # إرسال الرسالة
            sent_message = await bot.send_message(chat_id=chat_id, text=text, **kwargs)
            
            # إرجاع الرسالة المرسلة
            return sent_message
            
        except Exception as e:
            logger.error(f"فشل في إرسال الرسالة (المحاولة {attempt+1}/{max_retries}): {e}")
            
            # انتظار قبل إعادة المحاولة (زيادة وقت الانتظار مع كل محاولة)
            wait_time = 0.5 * (2 ** attempt)  # 0.5, 1, 2, 4, 8 ثواني
            await asyncio.sleep(wait_time)
    
    # رفع استثناء بعد فشل جميع المحاولات
    raise Exception(f"فشلت جميع المحاولات ({max_retries}) لإرسال الرسالة.")



async def show_invalid_choice(update: Update, options: list[str], prompt: str, state: int) -> int:
    """يعيد المستخدم لنفس الحالة في حال كتب شيئًا خارج الخيارات المتاحة."""
    reply_markup = ReplyKeyboardMarkup([[o] for o in options], resize_keyboard=True)
    await update.message.reply_text("حبيبي اختار من الخيارات يلي تحت 👇", reply_markup=reply_markup)
    await update.message.reply_text(prompt, reply_markup=reply_markup)
    return state




# دالة لتحديث حالة الطلب في قاعدة البيانات المشتركة
async def update_order_status(order_id, status, bot_type):
    async with get_db_connection() as conn:
        async with conn.cursor() as cursor:
            current_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            
            # تحديث الحالة وتوقيت آخر مزامنة حسب نوع البوت
            if bot_type == "user":
                await cursor.execute(
                    "INSERT INTO order_status (order_id, status, last_sync_user_bot) "
                    "VALUES (%s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE status = %s, last_sync_user_bot = %s",
                    (order_id, status, current_time, status, current_time)
                )
            else:  # restaurant
                await cursor.execute(
                    "INSERT INTO order_status (order_id, status, last_sync_restaurant_bot) "
                    "VALUES (%s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE status = %s, last_sync_restaurant_bot = %s",
                    (order_id, status, current_time, status, current_time)
                )
            
        await conn.commit()




def initialize_database():
    conn = pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        charset='utf8mb4',
        autocommit=True
    )
    cursor = conn.cursor()

    # إنشاء قاعدة البيانات وضبط الترميز
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS {DB_NAME} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    cursor.execute(f"USE {DB_NAME}")

    # المحافظات
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS provinces (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(255) UNIQUE NOT NULL
        ) ENGINE=InnoDB;
    """)

    # المدن
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cities (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            province_id INT NOT NULL,
            ads_channel VARCHAR(255),
            FOREIGN KEY (province_id) REFERENCES provinces(id) ON DELETE CASCADE
        ) ENGINE=InnoDB;
    """)

    # بيانات المستخدمين
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_data (
            user_id BIGINT PRIMARY KEY,
            name VARCHAR(255),
            phone VARCHAR(20) UNIQUE,
            province_id INT,
            city_id INT,
            location_image VARCHAR(255),
            location_text TEXT,
            latitude DOUBLE,
            longitude DOUBLE,
            FOREIGN KEY (province_id) REFERENCES provinces(id) ON DELETE SET NULL,
            FOREIGN KEY (city_id) REFERENCES cities(id) ON DELETE SET NULL
        ) ENGINE=InnoDB;
    """)

    # المطاعم
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS restaurants (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(255) UNIQUE NOT NULL,
            city_id INT NOT NULL,
            channel VARCHAR(255) NOT NULL,
            open_hour FLOAT NOT NULL,
            close_hour FLOAT NOT NULL,
            is_frozen TINYINT DEFAULT 0,
            FOREIGN KEY (city_id) REFERENCES cities(id) ON DELETE CASCADE
        ) ENGINE=InnoDB;
    """)

    # الفئات
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS categories (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            restaurant_id INT NOT NULL,
            UNIQUE(name, restaurant_id),
            FOREIGN KEY (restaurant_id) REFERENCES restaurants(id) ON DELETE CASCADE
        ) ENGINE=InnoDB;
    """)

    # الوجبات
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS meals (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            price INT,
            category_id INT NOT NULL,
            caption TEXT,
            image_file_id VARCHAR(255),
            size_options TEXT,
            unique_id VARCHAR(255) UNIQUE,
            image_message_id INT,
            UNIQUE(name, category_id),
            FOREIGN KEY (category_id) REFERENCES categories(id) ON DELETE CASCADE
        ) ENGINE=InnoDB;
    """)

    # عداد الطلبات
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS restaurant_order_counter (
            restaurant_id INT PRIMARY KEY,
            last_order_number INT DEFAULT 0 NOT NULL,
            FOREIGN KEY (restaurant_id) REFERENCES restaurants(id) ON DELETE CASCADE
        ) ENGINE=InnoDB;
    """)

    # سجل الطلبات
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_orders (
            order_id VARCHAR(255) PRIMARY KEY,
            user_id BIGINT NOT NULL,
            restaurant_id INT NOT NULL,
            city_id INT NOT NULL,
            order_number INT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES user_data(user_id) ON DELETE CASCADE,
            FOREIGN KEY (restaurant_id) REFERENCES restaurants(id) ON DELETE CASCADE,
            FOREIGN KEY (city_id) REFERENCES cities(id) ON DELETE CASCADE
        ) ENGINE=InnoDB;
    """)

    # التقييمات
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS restaurant_ratings (
            id INT AUTO_INCREMENT PRIMARY KEY,
            restaurant_id INT NOT NULL,
            user_id BIGINT NOT NULL,
            rating INT DEFAULT 0,
            comment TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (restaurant_id) REFERENCES restaurants(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES user_data(user_id) ON DELETE CASCADE,
            UNIQUE(restaurant_id, user_id)
        ) ENGINE=InnoDB;
    """)

    # حالة المحادثة
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversation_states (
            user_id BIGINT PRIMARY KEY,
            state_data JSON,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB;
    """)

    # السلة
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS shopping_carts (
            user_id BIGINT PRIMARY KEY,
            cart_data JSON,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB;
    """)

    # سجل الإلغاء
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cancellation_history (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id BIGINT NOT NULL,
            reason TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX (user_id)
        ) ENGINE=InnoDB;
    """)

    # الإعلانات
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS advertisements (
            id INT AUTO_INCREMENT PRIMARY KEY,
            ad_text TEXT NOT NULL,
            city_id INT,
            restaurant_id INT,
            media_file_id VARCHAR(255),
            media_type VARCHAR(50),
            expire_timestamp BIGINT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (city_id) REFERENCES cities(id) ON DELETE SET NULL,
            FOREIGN KEY (restaurant_id) REFERENCES restaurants(id) ON DELETE SET NULL
        ) ENGINE=InnoDB;
    """)

    conn.close()

# تحميل الإعدادات من ملف .env
load_dotenv()

# استخدام متغيرات بيئية للإعدادات
DB_PATH = os.getenv("DB_PATH", "database.db")




# إضافة هذه المتغيرات بعد تحميل ملف .env
DB_HOST = "localhost"
DB_PORT = 3306
DB_USER = "botuser"
DB_PASSWORD = "strongpassword123"
DB_NAME = "telegram_bot"






DB_PATH = "database.db"

# إنشاء تجمع اتصالات
# استبدال تجمع اتصالات SQLite بتجمع اتصالات MySQL
class DBConnectionPool:
    def __init__(self, max_connections=20):  # ← هنا زدت العدد من 10 إلى 20
        self.max_connections = max_connections
        self.connections = []
        self.semaphore = asyncio.Semaphore(max_connections)

    async def get_connection(self):
        await self.semaphore.acquire()
        if not self.connections:
            conn = await aiomysql.connect(
                host=DB_HOST,
                user=DB_USER,
                password=DB_PASSWORD,
                db=DB_NAME,
                port=DB_PORT,
                charset='utf8mb4',
                autocommit=True  # ✅ تم التفعيل هنا
            )
            # تعيين مستوى العزل إلى READ COMMITTED
            async with conn.cursor() as cursor:
                await cursor.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
            return conn
        return self.connections.pop()


    async def release_connection(self, conn):
        self.connections.append(conn)
        self.semaphore.release()

    @asynccontextmanager
    async def connection(self):
        conn = await self.get_connection()
        try:
            yield conn
        finally:
            await self.release_connection(conn)

# استخدام تجمع الاتصالات
db_pool = DBConnectionPool()


@asynccontextmanager
async def get_db_connection():
    print("🌀 دخلنا get_db_connection")
    conn = await db_pool.get_connection()
    print(f"🔗 نتيجة get_connection: {conn}")

    try:
        yield conn
    finally:
        print("🧹 سيتم تحرير الاتصال")
        await db_pool.release_connection(conn)






async def get_user_lock(user_id):
    """الحصول على قفل خاص بمستخدم معين"""
    if user_id not in user_state_lock:
        user_state_lock[user_id] = Lock()
    return user_state_lock[user_id]

async def get_next_order_number(restaurant_id: int) -> int:
    try:
        async with get_db_connection() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cursor:
                    # قفل الصف
                    await cursor.execute(
                        "SELECT last_order_number FROM restaurant_order_counter WHERE restaurant_id = %s FOR UPDATE",
                        (restaurant_id,)
                    )
                    result = await cursor.fetchone()

                    if result is None:
                        next_number = 1
                        await cursor.execute(
                            "INSERT INTO restaurant_order_counter (restaurant_id, last_order_number) VALUES (%s, %s)",
                            (restaurant_id, next_number)
                        )
                    else:
                        last_number = result[0]
                        next_number = last_number + 1
                        await cursor.execute(
                            "UPDATE restaurant_order_counter SET last_order_number = %s WHERE restaurant_id = %s",
                            (next_number, restaurant_id)
                        )

                await conn.commit()
                return next_number
            except Exception as e:
                await conn.rollback()
                logger.error(f"❌ خطأ أثناء توليد رقم الطلب: {e}")
                raise
    except Exception as e:
        logger.error(f"❌ فشل الاتصال بقاعدة البيانات: {e}")
        raise





async def update_conversation_state(user_id, key, value):
    """تحديث قيمة محددة في حالة المحادثة"""
    user_id = int(user_id)
    try:
        # الحصول على الحالة الحالية
        current_state = await get_conversation_state(user_id)

        # تحديث القيمة
        current_state[key] = value

        # حفظ الحالة المحدثة
        return await save_conversation_state(user_id, current_state)
    except Exception as e:
        logger.error(f"خطأ في تحديث حالة المحادثة: {e}")
        return False


async def verify_data_consistency(user_id):
    """التحقق من اتساق البيانات في قاعدة البيانات"""
    try:
        # التحقق من بيانات المستخدم
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                # التحقق من وجود بيانات المستخدم
                await cursor.execute("SELECT * FROM user_data WHERE user_id = %s", (user_id,))
                user_data = await cursor.fetchone()

                if not user_data:
                    return False

                # التحقق من وجود حالة محادثة
                await cursor.execute("SELECT 1 FROM conversation_states WHERE user_id = %s", (user_id,))
                has_state = await cursor.fetchone()

                if not has_state and user_data:
                    # إنشاء حالة محادثة جديدة من بيانات المستخدم
                    new_state = {
                        "name": user_data[1],
                        "phone": user_data[2],
                        "province_id": user_data[3],
                        "city_id": user_data[4]
                    }
                    await save_conversation_state(user_id, new_state)
                    logger.info(f"تم إنشاء حالة محادثة جديدة للمستخدم {user_id}")

        return True
    except Exception as e:
        logger.error(f"خطأ في التحقق من اتساق البيانات: {e}")
        return False



class RateLimiter:
    def __init__(self, max_calls, period):
        self.max_calls = max_calls
        self.period = period
        self.calls = deque()

    async def acquire(self):
        now = time.time()

        # إزالة الطلبات القديمة
        while self.calls and self.calls[0] < now - self.period:
            self.calls.popleft()

        # إذا وصلنا للحد الأقصى، انتظر
        if len(self.calls) >= self.max_calls:
            wait_time = self.calls[0] + self.period - now
            await asyncio.sleep(wait_time)

        # تسجيل الطلب الجديد
        self.calls.append(time.time())

# إنشاء محدد معدل للطلبات
telegram_limiter = RateLimiter(max_calls=30, period=1)  # 30 طلب في الثانية

# استخدام المحدد قبل كل طلب لـ API تلغرام
async def send_message_with_rate_limit(chat_id, text, **kwargs):
    await telegram_limiter.acquire()
    return await send_message_with_retry(context.bot, chat_id, text=text, **kwargs)




async def save_cart_to_db(user_id, cart_data):
    logger.warning(f"🧠 [save_cart_to_db] user_id = {user_id}, type = {type(user_id)}")
    try:
        # تأكد من أن user_id هو integer
        user_id = int(user_id)
        
        json_data = json.dumps(cart_data, ensure_ascii=False)
        logger.warning(f"📤 [save_cart_to_db] البيانات للحفظ: {json_data}")

        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                # استخدام INSERT ... ON DUPLICATE KEY UPDATE بدلاً من REPLACE INTO
                await cursor.execute(
                    """
                    INSERT INTO shopping_carts (user_id, cart_data)
                    VALUES (%s, %s)
                    ON DUPLICATE KEY UPDATE cart_data = VALUES(cart_data)
                    """,
                    (user_id, json_data)
                )
            await conn.commit()

            # قراءة البيانات مباشرة بعد الكتابة باستخدام نفس الاتصال
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT cart_data FROM shopping_carts WHERE user_id = %s", (user_id,))
                check = await cursor.fetchone()
                logger.warning(f"🔍 تحقق بعد الحفظ: {check}")
                
                # تخزين البيانات المقروءة في متغير عالمي أو في context.user_data
                if check and check[0]:
                    cart_data_read = check[0]
                    if isinstance(cart_data_read, bytes):
                        cart_data_read = cart_data_read.decode("utf-8")
                    cart = json.loads(cart_data_read)
                    # يمكن تخزين هذه القيمة في مكان آمن للاستخدام اللاحق

        return True

    except Exception as e:
        logger.error(f"❌ خطأ أثناء حفظ السلة في قاعدة البيانات: {e}", exc_info=True)
        return False





async def get_cart_from_db(user_id):
    logger.warning(f"🧠 [get_cart_from_db] user_id = {user_id}, type = {type(user_id)}")
    try:
        # تأكد من أن user_id هو integer
        user_id = int(user_id)
        
        # زيادة التأخير قبل القراءة لضمان اكتمال عمليات الكتابة السابقة
        await asyncio.sleep(0.3)
        
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT cart_data FROM shopping_carts WHERE user_id = %s",
                    (user_id,)
                )
                result = await cursor.fetchone()
                
                if result and result[0]:
                    cart_data = result[0]
                    if isinstance(cart_data, bytes):
                        cart_data = cart_data.decode("utf-8")
                    cart = json.loads(cart_data)
                    logger.warning(f"📤 [get_cart_from_db] نتيجة: {result}")
                    logger.warning(f"✅ تم تحويل JSON: {cart}")
                    return cart
                return []
    except Exception as e:
        logger.error(f"❌ خطأ في get_cart_from_db: {e}")
        return []




async def delete_cart_from_db(user_id):
    user_id = int(user_id)
    """حذف سلة التسوق من قاعدة البيانات"""
    try:
        # تأكد من أن user_id هو integer
        user_id = int(user_id)
        
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "DELETE FROM shopping_carts WHERE user_id = %s",
                    (user_id,)
                )
            await conn.commit()
            
            # تحقق من الحذف
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT 1 FROM shopping_carts WHERE user_id = %s", (user_id,))
                check = await cursor.fetchone()
                logger.warning(f"🔍 تحقق بعد الحذف: {check}")
                
        return True
    except Exception as e:
        logger.error(f"خطأ في حذف سلة التسوق: {e}")
        return False





async def retry_with_backoff(func, *args, max_retries=5, initial_wait=0.5, **kwargs):
    """تنفيذ دالة مع إعادة المحاولة في حالة الفشل مع زيادة وقت الانتظار تدريجيًا"""
    retries = 0
    last_exception = None

    while retries < max_retries:
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            last_exception = e
            wait_time = initial_wait * (2 ** retries)  # 0.5, 1, 2, 4, 8 ثواني
            logger.warning(f"فشل الطلب، إعادة المحاولة بعد {wait_time} ثواني. الخطأ: {e}")
            await asyncio.sleep(wait_time)
            retries += 1

    # تسجيل الخطأ النهائي بتفاصيل أكثر
    logger.error(
        f"فشلت جميع المحاولات ({max_retries}) لتنفيذ {func.__name__}. "
        f"آخر خطأ: {last_exception}. "
        f"المعاملات: args={args}, kwargs={kwargs}"
    )

    # إعادة رفع الاستثناء مع معلومات إضافية
    raise Exception(f"فشلت جميع المحاولات بعد {max_retries} محاولات. آخر خطأ: {last_exception}")





async def delete_cart_from_db(user_id):
    """حذف سلة التسوق من قاعدة البيانات"""
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("DELETE FROM user_carts WHERE user_id = %s", (user_id,))
            await conn.commit()
    except Exception as e:
        logger.error(f"خطأ في حذف السلة: {e}")


async def save_conversation_state(user_id, state_data):
    user_id = int(user_id)
    """حفظ حالة المحادثة في قاعدة البيانات"""
    user_lock = await get_user_lock(user_id)
    async with user_lock:  # استخدام قفل خاص بالمستخدم
        # تحويل البيانات المعقدة إلى JSON
        serialized_data = {}
        for k, v in state_data.items():
            if isinstance(v, (dict, list, set)):
                serialized_data[k] = json.dumps(v)
            elif isinstance(v, datetime):
                serialized_data[k] = v.isoformat()
            else:
                serialized_data[k] = str(v)

        # حفظ البيانات في قاعدة البيانات
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cursor:
                    # تحويل البيانات إلى JSON
                    json_data = json.dumps(serialized_data, ensure_ascii=False)

                    # استخدام REPLACE INTO لإضافة أو تحديث البيانات
                    await cursor.execute(
                        "REPLACE INTO conversation_states (user_id, state_data) VALUES (%s, %s)",
                        (user_id, json_data)
                    )
                await conn.commit()
            return True
        except Exception as e:
            logger.error(f"خطأ في حفظ حالة المحادثة: {e}")
            return False






async def get_conversation_state(user_id):
    user_id = int(user_id)
    """استرجاع حالة المحادثة من قاعدة البيانات"""
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT state_data FROM conversation_states WHERE user_id = %s",
                    (user_id,)
                )
                result = await cursor.fetchone()

                if not result:
                    return {}

                state_data = json.loads(result[0])

                # تحويل البيانات من JSON
                deserialized_data = {}
                for k, v in state_data.items():
                    if k in ['location_coords', 'province_data', 'city_data']:
                        try:
                            deserialized_data[k] = json.loads(v)
                        except:
                            deserialized_data[k] = v
                    elif k.endswith('_time') or k.endswith('_date'):
                        try:
                            deserialized_data[k] = datetime.fromisoformat(v)
                        except:
                            deserialized_data[k] = v
                    else:
                        deserialized_data[k] = v

                return deserialized_data
    except Exception as e:
        logger.error(f"خطأ في استرجاع حالة المحادثة: {e}")
        return {}



async def add_cancellation_record(user_id, reason=None):
    """إضافة سجل إلغاء جديد"""
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "INSERT INTO cancellation_history (user_id, reason) VALUES (%s, %s)",
                    (user_id, reason or 'غير محدد')
                )
            await conn.commit()
        return True
    except Exception as e:
        logger.error(f"خطأ في إضافة سجل إلغاء: {e}")
        return False


async def get_cancellation_times(user_id):
    """استرجاع أوقات الإلغاء فقط"""
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT timestamp FROM cancellation_history WHERE user_id = %s ORDER BY timestamp DESC LIMIT 10",
                    (user_id,)
                )
                rows = await cursor.fetchall()
        return [timestamp for (timestamp,) in rows]
    except Exception as e:
        logger.error(f"خطأ في استرجاع أوقات الإلغاء: {e}")
        return []


async def get_cancellation_history(user_id):
    """استرجاع سجل الإلغاء (السبب + الوقت)"""
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT reason, timestamp FROM cancellation_history WHERE user_id = %s ORDER BY timestamp DESC LIMIT 10",
                    (user_id,)
                )
                rows = await cursor.fetchall()
        return [{"reason": reason, "timestamp": timestamp} for reason, timestamp in rows]
    except Exception as e:
        logger.error(f"خطأ في استرجاع سجل الإلغاء: {e}")
        return []

async def get_cancellation_times(user_id):
    """استرجاع أوقات الإلغاء فقط"""
    history = await get_cancellation_history(user_id)
    return [record['timestamp'] for record in history]



# إضافة دعم لأقفال التزامن
from asyncio import Lock

# إنشاء أقفال للعمليات الحرجة
db_lock = Lock()  # قفل للعمليات على قاعدة البيانات

user_state_lock = {}  # قفل لكل مستخدم على حدة



ADMIN_MEDIA_CHANNEL = -1002659459294  
AD_MEDIA_CHANNEL = -1002315567913  # معرف القناة الوسيطة الإعلانية (بالسالب)
ERRORS_CHANNEL = -1001234567890  



# دالة حساب السعر الإجمالي
def calculate_total_price(orders, prices):
    total = 0
    for item, count in orders.items():
        found = False
        for category, items in prices.items():
            if item in items:
                total += count * items[item]
                found = True
                break
        if not found:
            logging.warning(f"العنصر '{item}' غير موجود في قائمة أسعار المطعم.")
    return total




def get_fast_order_cooldown(cancel_times: list) -> tuple[int, str]:
    now = datetime.now()
    count_17 = sum((now - t).total_seconds() <= 1020 for t in cancel_times)
    count_22 = sum((now - t).total_seconds() <= 1320 for t in cancel_times)
    count_40 = sum((now - t).total_seconds() <= 2400 for t in cancel_times)

    if count_40 >= 5:
        return 2 * 24 * 60 * 60, (
            "🚫 تم حظرك من استخدام (اطلب عالسريع) لمدة يومين.\n"
            "استخدامك المتكرر للإلغاء يُعتبر سلوكًا عبثيًا بالخدمة.\n"
            "للاستفسار: @Support"
        )
    elif count_22 >= 4:
        return 10 * 60, (
            "⚠️ تم حظرك من (اطلب عالسريع) لمدة 10 دقائق بسبب تكرار الإلغاء.\n"
            "نرجو الالتزام بالاستخدام المنطقي للخدمة."
        )
    elif count_17 >= 3:
        return 5 * 60, (
            "⚠️ تم إيقاف زر (اطلب عالسريع) مؤقتًا لمدة 5 دقائق بسبب الإلغاء المتكرر.\n"
            "إذا تكرر ذلك سيتم حظرك مؤقتًا."
        )

    return 0, ""




# تعديل دالة generate_order_id
def generate_order_id():
    return str(uuid.uuid4())


    

def get_main_menu():
    return ReplyKeyboardMarkup([
        ["اطلب عالسريع 🔥"],
        ["لا بدي عدل 😐", "التواصل مع الدعم 🎧"],
        ["من نحن 🏢", "أسئلة متكررة ❓"]
    ], resize_keyboard=True)

#_____________________________

# دوال إنشاء الرسائل الموحدة
def create_new_order_message(order_id, order_number, user_name, phone, address, items, total_price, notes=None):
    """إنشاء رسالة طلب جديد بالتنسيق الموحد"""
    
    # بناء قائمة الطلبات
    items_text = ""
    for i, item in enumerate(items, 1):
        items_text += f"  {i}. {item['name']} x{item['quantity']} - {item['price']} ل.س\n"
    
    # بناء الرسالة الكاملة
    message = (
        f"🛒 *طلب جديد*\n\n"
        f"🔢 *رقم الطلب:* `{order_number}`\n"
        f"🆔 *معرف الطلب:* `{order_id}`\n\n"
        f"👤 *اسم المستخدم:* {user_name}\n"
        f"📱 *رقم الهاتف:* {phone}\n"
        f"📍 *العنوان:* {address}\n\n"
        f"📋 *الطلبات:*\n{items_text}\n"
        f"💰 *المجموع:* {total_price} ل.س\n\n"
        f"📝 *ملاحظات:* {notes if notes else 'لا يوجد ملاحظات'}"
    )
    
    return message

def create_rating_message(order_id, order_number, rating, comment=None):
    """إنشاء رسالة تقييم بالتنسيق الموحد"""
    
    stars = "⭐" * rating
    
    message = (
        f"📊 *تقييم جديد*\n\n"
        f"🔢 *رقم الطلب:* `{order_number}`\n"
        f"🆔 *معرف الطلب:* `{order_id}`\n\n"
        f"⭐ *التقييم:* {stars} ({rating}/5)\n"
    )
    
    if comment:
        message += f"💬 *التعليق:* {comment}"
    
    return message











async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    message = update.message
    user_id = update.effective_user.id
    logger.info("✅ تم تنفيذ الدالة start بنجاح")

    # التحقق من اتساق البيانات عند بدء المحادثة
    await verify_data_consistency(user_id)

    # تنظيف أي بيانات سابقة
    context.user_data.clear()

    # الرسالة الترحيبية الأولى
    await message.reply_text("لك يا أهليين ❤️")
    await asyncio.sleep(0.5)

    await message.reply_text("وسهليين ❤️")
    await asyncio.sleep(0.5)

    # الرسائل المتتالية بتأخير
    await message.reply_text("حابب تطلب من هلا وطالع عالسريع ؟ 🔥")
    await asyncio.sleep(0.5)

    await message.reply_text("جاوب عهالأسئلة عالسريع 🔥")
    await asyncio.sleep(0.5)

    await message.reply_text("بس أول مرة 😘")
    await asyncio.sleep(0.5)

    # إرسال الستيكر بعد التأخير
    await context.bot.send_sticker(
        chat_id=update.effective_chat.id,
        sticker="CAACAgIAAxkBAAEBxpNoMeK-Uvn1iWDhyR3mFDJJI_Mj7gACQhAAAjPFKUmQDtQRpypKgjYE"
    )

    # عرض الخيارات
    reply_markup = ReplyKeyboardMarkup([
        ["ليش هالأسئلة ؟ 🧐"],
        ["خلينا نبلش 😁"]
    ], resize_keyboard=True)

    await message.reply_text(
        "نبلش؟ 😄",
        reply_markup=reply_markup
    )

    return ASK_INFO






async def ask_info_details(update: Update, context: CallbackContext) -> int:
    await update.message.reply_text(
        "عزيزي/عزيزتي 👋\n\n"
        "نشكرك على تواصلك معنا 🙏\n"
        "نود أن نؤكد لك أن المعلومات التي نطلبها هي فقط **لغاية تقديم خدمة أفضل لك** 💡\n"
        "جميع البيانات التي نجمعها **محفوظة بشكل آمن 🔐** ولن يتم مشاركتها مع أي جهة،\n"
        "باستثناء *مصدر الخدمة* المحدد مثل المطعم أو خدمة التوصيل 🛵🍔\n\n"
        "في حال كان هناك **أي تغيير في المعلومات** أو إذا رغبت بتعديلها،\n"
        "بكل بساطة اضغط على خيار *\"لا بدي عدل 😐\"* وسنقوم بحذف المعلومات القديمة وبدء عملية جديدة 🔄\n\n"
        "نحن دائمًا هنا لخدمتك 🤝\n"
        "وشكرًا لتفهمك ❤️\n\n"
        "مع أطيب التحيات 🌟",
        parse_mode="Markdown"
    )
    return ASK_INFO

async def handle_info_selection(update: Update, context: CallbackContext) -> int:
    text = update.message.text.strip()
    if text == "ليش هالأسئلة ؟ 🧐":
        return await ask_info_details(update, context)
    elif text == "خلينا نبلش 😁":
        return await ask_name(update, context)
    else:
        # حماية من النصوص غير المتوقعة
        reply_markup = ReplyKeyboardMarkup([
            ["ليش هالأسئلة ؟ 🧐"],
            ["خلينا نبلش 😁"]
        ], resize_keyboard=True)
        await update.message.reply_text("حبيبي اختار من الخيارات يلي تحت 👇", reply_markup=reply_markup)
        return ASK_INFO



async def ask_name(update: Update, context: CallbackContext) -> int:
    reply_markup = ReplyKeyboardMarkup([
        ["عودة ⬅️"]
    ], resize_keyboard=True)
    await update.message.reply_text("اسم الحلو ؟ ☺️", reply_markup=reply_markup)
    return ASK_NAME


async def handle_name(update: Update, context: CallbackContext) -> int:
    context.user_data['name'] = update.message.text
    return await ask_phone(update, context)



async def handle_back_to_info(update: Update, context: CallbackContext) -> int:
    """
    معالجة خيار العودة من مرحلة الاسم إلى المرحلة الأولى.
    """
    reply_markup = ReplyKeyboardMarkup([
        ["ليش هالأسئلة ؟ 🧐"],
        ["خلينا نبلش 😁"]
    ], resize_keyboard=True)

    await update.message.reply_text(
        "تم الرجوع إلى البداية. يمكنك الآن اختيار أحد الخيارات التالية:",
        reply_markup=reply_markup
    )
    return ASK_INFO


async def ask_phone(update: Update, context: CallbackContext) -> int:
    reply_markup = ReplyKeyboardMarkup([
        ["عودة ⬅️"]
    ], resize_keyboard=True)
    await update.message.reply_text("رقم تلفونك تأكد منو منيح رح يوصلك كود تحقق عليه 😉", reply_markup=reply_markup)
    return ASK_PHONE






async def send_verification_code(update: Update, context: CallbackContext) -> int:
    if update.message.text == "عودة ⬅️":
        context.user_data.pop('phone', None)
        context.user_data.pop('verification_code', None)
        return await ask_phone(update, context)

    phone = update.message.text

    if not (phone.isdigit() and len(phone) == 10 and phone.startswith("09")):
        await update.message.reply_text("هأ لازم من يبلش ب 09 ويكون من 10 أرقام 😒")
        return ASK_PHONE

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT phone FROM blacklisted_numbers WHERE phone = %s", (phone,))
                result = await cursor.fetchone()
                if result:
                    await update.message.reply_text(
                        "❌ عذراً، رقمك محظور من قبل إدارة البوت بسبب سلوك سابق.\n"
                        "يرجى التواصل مع الدعم للمزيد من التفاصيل:\n"
                        "📞 0912345678 - 0998765432"
                    )
                    return ASK_PHONE
    except Exception as e:
        logger.error(f"Database error in send_verification_code: {e}")
        await update.message.reply_text("❌ حدث خطأ في التحقق من قاعدة البيانات.")
        return ASK_PHONE

    verification_code = random.randint(10000, 99999)
    context.user_data['phone'] = phone
    context.user_data['verification_code'] = verification_code

    try:
        name = context.user_data['name']
        verification_message = (
            f"عزيزي/عزيزتي {name} 😄\n"
            f"صاحب الرقم {phone} 🌝\n"
            f"إن كود التحقق حتى تطلب عالسريع هو {verification_code} 🤗\n"
            f"مستعدون لخدمتكم عالسريع 🔥"
        )

        await send_message_with_retry(
            bot=context.bot,
            chat_id="@verifycode12345",
            text=verification_message
        )

        reply_markup = ReplyKeyboardMarkup([
            ["عودة ⬅️"]
        ], resize_keyboard=True)
        await update.message.reply_text("هات لنشوف شو الكود يلي وصلك ؟ 🤨", reply_markup=reply_markup)
        return ASK_PHONE_VERIFICATION

    except Exception as e:
        logger.error(f"Error sending verification code: {e}")
        await update.message.reply_text("❌ حدث خطأ أثناء إرسال الكود. يرجى المحاولة مرة أخرى.")
        context.user_data.pop('phone', None)
        return await ask_phone(update, context)





async def verify_code(update: Update, context: CallbackContext) -> int:
    if update.message.text == "عودة ⬅️":
        context.user_data.pop('phone', None)
        context.user_data.pop('verification_code', None)
        return await ask_phone(update, context)

    entered_code = update.message.text
    if entered_code == str(context.user_data['verification_code']):
        await update.message.reply_text("وهي سجلنا رقمك 🙂")
        await asyncio.sleep(1)
        await update.message.reply_text("مارح نعطيه لحدا 😃")

        user_id = update.effective_user.id
        name = context.user_data['name']
        phone = context.user_data['phone']

        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "INSERT INTO user_data (user_id, name, phone) VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE name = %s, phone = %s",
                        (user_id, name, phone, name, phone)
                    )

                    await cursor.execute("SELECT name FROM provinces")
                    rows = await cursor.fetchall()
                    provinces = [row[0] for row in rows]
                    context.user_data["valid_provinces"] = provinces.copy()

                await conn.commit()

        except Exception as e:
            logger.error(f"Database error in verify_code: {e}")
            await update.message.reply_text("❌ حدث خطأ أثناء حفظ بياناتك. حاول لاحقًا.")
            return ASK_PHONE_VERIFICATION

        provinces.append("عودة ➡️")
        reply_markup = ReplyKeyboardMarkup(
            [[p for p in provinces[i:i+3]] for i in range(0, len(provinces), 3)],
            resize_keyboard=True
        )
        await update.message.reply_text("بأي محافظة ؟ 😁", reply_markup=reply_markup)
        return ASK_PROVINCE

    else:
        # ⛔ كود خاطئ، عرض زر عودة لتعديل الرقم
        reply_markup = ReplyKeyboardMarkup([
            ["عودة ⬅️"]
        ], resize_keyboard=True)
        await update.message.reply_text("حط نضارات وارجاع تأكد 🤓", reply_markup=reply_markup)
        return ASK_PHONE_VERIFICATION


async def handle_province(update: Update, context: CallbackContext) -> int:
    province = update.message.text.strip()

    # 🟡 عودة ← نرجع إلى خطوة إدخال رقم الهاتف
    if province == "عودة ➡️":
        context.user_data.pop('phone', None)
        return await ask_phone(update, context)

    # 🛡️ تحقق من الإدخال غير المتوقع
    if province not in context.user_data.get("valid_provinces", []):
        reply_markup = ReplyKeyboardMarkup([[p] for p in context.user_data.get("valid_provinces", [])], resize_keyboard=True)
        await update.message.reply_text("حبيبي اختار من الخيارات يلي تحت 👇", reply_markup=reply_markup)
        return ASK_PROVINCE

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                # جلب معرف المحافظة
                await cursor.execute("SELECT id FROM provinces WHERE name = %s", (province,))
                result = await cursor.fetchone()

                if not result:
                    await update.message.reply_text("⚠️ حدث خطأ أثناء جلب المدن. حاول مرة أخرى.")
                    return ASK_PROVINCE

                province_id = result[0]
                context.user_data['province_id'] = province_id
                context.user_data['province_name'] = province

                # جلب المدن المرتبطة
                await cursor.execute("SELECT id, name FROM cities WHERE province_id = %s", (province_id,))
                rows = await cursor.fetchall()

        cities = [(row[0], row[1]) for row in rows]
        city_names = [row[1] for row in rows]
        city_names += ["وين مدينتي ؟ 😟", "عودة ➡️"]  # ✅ إضافة زر العودة

        context.user_data['city_map'] = {name: cid for cid, name in cities}

        reply_markup = ReplyKeyboardMarkup([[c] for c in city_names], resize_keyboard=True)
        await update.message.reply_text("بأي مدينة ؟ 😁", reply_markup=reply_markup)
        return ASK_CITY

    except Exception as e:
        logger.error(f"Database error in handle_province: {e}")
        await update.message.reply_text("❌ حدث خطأ أثناء معالجة البيانات. حاول لاحقًا.")
        return ASK_PROVINCE





async def handle_city(update: Update, context: CallbackContext) -> int:
    city_name = update.message.text.strip()

    # 🔙 عودة ← نرجع إلى اختيار المحافظة
    if city_name == "عودة ➡️":
        context.user_data.pop('province_id', None)
        context.user_data.pop('province_name', None)

        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT name FROM provinces")
                    rows = await cursor.fetchall()

            provinces = [row[0] for row in rows]
            provinces.append("عودة ➡️")  # ✅ إضافة زر العودة

            reply_markup = ReplyKeyboardMarkup([[p] for p in provinces], resize_keyboard=True)
            await update.message.reply_text("بأي محافظة ؟ 😁", reply_markup=reply_markup)
            return ASK_PROVINCE

        except Exception as e:
            logger.error(f"Database error in handle_city (عودة): {e}")
            await update.message.reply_text("❌ حدث خطأ أثناء تحميل المحافظات. حاول لاحقًا.")
            return ASK_PROVINCE

    # 🟢 المدينة المخصصة يدويًا
    if city_name == "وين مدينتي ؟ 😟":
        reply_markup = ReplyKeyboardMarkup([["عودة ➡️"]], resize_keyboard=True)
        await update.message.reply_text("بدي عذبك تكتبلي اسم مدينتك 😘", reply_markup=reply_markup)
        await asyncio.sleep(0.5)
        await update.message.reply_text("رح نشوف اسم مدينتك ونجي لعندك بأقرب وقت 🫡")
        return ASK_CUSTOM_CITY

    # 🛡️ تحقق من الإدخال غير المتوقع
    if city_name not in context.user_data.get("city_map", {}):
        reply_markup = ReplyKeyboardMarkup(
            [[c] for c in context.user_data.get("city_map", {}).keys()],
            resize_keyboard=True
        )
        await update.message.reply_text("حبيبي اختار من الخيارات يلي تحت 👇", reply_markup=reply_markup)
        return ASK_CITY

    # ✅ تخزين المدينة المختارة ومتابعة
    city_id = context.user_data["city_map"][city_name]
    context.user_data['city_id'] = city_id
    context.user_data['city_name'] = city_name

    # ✅ زر إرسال الموقع
    location_button = KeyboardButton("📍 إرسال موقعي", request_location=True)
    reply_markup = ReplyKeyboardMarkup([[location_button], ["عودة ➡️"]], resize_keyboard=True)

    # ✅ زر الشرح (Inline)
    inline_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("عالسريع كيف ؟ 🔥", callback_data="how_to_send_location")]
    ])

    await update.message.reply_text("اختار ارسال موقعي اذا كنت مفعل خدمة الموقع GPS 📍", reply_markup=reply_markup)
    await update.message.reply_text("اذا ما بتعرف تشغل لوكيشن", reply_markup=inline_markup)
    await asyncio.sleep(0.5)
    await update.message.reply_text("اذا ماكنت مفعل، رح تضطر تدور عموقع وتضغط مطول وترسلو 👇")
    await asyncio.sleep(0.5)
    await update.message.reply_text("ما بدا شي 😄")

    return ASK_LOCATION_IMAGE




async def handle_custom_city(update: Update, context: CallbackContext) -> int:
    text = update.message.text.strip()

    # ✅ التعامل مع خيار العودة
    if text == "عودة ➡️":
        try:
            province = context.user_data.get('province_name')
            if not province:
                await update.message.reply_text("⚠️ لم يتم العثور على المحافظة. يرجى اختيارها من جديد.")
                return ASK_PROVINCE

            async with get_db_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT id FROM provinces WHERE name = %s", (province,))
                    result = await cursor.fetchone()

                    if not result:
                        await update.message.reply_text("⚠️ لم يتم العثور على المحافظة. يرجى اختيارها من جديد.")
                        return ASK_PROVINCE

                    province_id = result[0]
                    await cursor.execute("SELECT name FROM cities WHERE province_id = %s", (province_id,))
                    rows = await cursor.fetchall()

            cities = [row[0] for row in rows]
            city_options = cities + ["وين مدينتي ؟ 😟", "عودة ➡️"]
            reply_markup = ReplyKeyboardMarkup([[city] for city in city_options], resize_keyboard=True)
            await update.message.reply_text("بأي مدينة ؟ 😁", reply_markup=reply_markup)
            return ASK_CITY

        except Exception as e:
            logger.error(f"Database error in handle_custom_city (عودة): {e}")
            await update.message.reply_text("❌ حدث خطأ أثناء تحميل المدن. حاول لاحقاً.")
            return ASK_CITY

    # ✅ إرسال المدينة المقترحة إلى قناة الدعم
    province = context.user_data.get('province_name')
    if not province:
        await update.message.reply_text("⚠️ لم يتم العثور على المحافظة. يرجى اختيارها من جديد.")
        return ASK_PROVINCE

    custom_city_channel = "@Lamtozkar"

    try:
        await context.bot.send_message(
            chat_id=custom_city_channel,
            text=f"📢 مدينة جديدة تم اقتراحها من أحد المستخدمين:\n\n"
                 f"🌍 المحافظة: {province}\n"
                 f"🏙️ المدينة: {text}\n"
                 f"👤 المستخدم: @{update.effective_user.username or 'غير متوفر'}"
        )
    except Exception as e:
        logger.error(f"❌ فشل في إرسال المدينة المقترحة إلى القناة: {e}")
        await update.message.reply_text("❌ حدث خطأ أثناء تنفيذ الطلب. حاول مرة أخرى لاحقاً.")
        return ASK_CITY

    await update.message.reply_text(
        "✅ تم استلام مدينتك! نأمل أن نتمكن من خدمتك قريبًا 🙏.\n"
        "يرجى اختيار المدينة من القائمة:"
    )

    # ✅ إعادة المستخدم لاختيار المدينة من القائمة
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT id FROM provinces WHERE name = %s", (province,))
                result = await cursor.fetchone()

                if not result:
                    await update.message.reply_text("⚠️ لم يتم العثور على المحافظة. يرجى اختيارها من جديد.")
                    return ASK_PROVINCE

                province_id = result[0]
                await cursor.execute("SELECT name FROM cities WHERE province_id = %s", (province_id,))
                rows = await cursor.fetchall()

        cities = [row[0] for row in rows]
        city_options = cities + ["وين مدينتي ؟ 😟", "عودة ➡️"]
        reply_markup = ReplyKeyboardMarkup([[city] for city in city_options], resize_keyboard=True)
        await update.message.reply_text("بأي مدينة ؟ 😁", reply_markup=reply_markup)
        return ASK_CITY

    except Exception as e:
        logger.error(f"Database error in handle_custom_city (إعادة): {e}")
        await update.message.reply_text("❌ حدث خطأ أثناء تحميل المدن. حاول لاحقاً.")
        return ASK_CITY





async def ask_location(update: Update, context: CallbackContext) -> int:
    # إذا اختار "عودة"، نرجع إلى سؤال المدينة
    if update.message.text == "عودة ➡️":
        context.user_data.pop('location_coords', None)  # نحذف الموقع السابق إن وُجد
        city_names = list(context.user_data.get("city_map", {}).keys()) + ["وين مدينتي ؟ 😟", "عودة ➡️"]
        reply_markup = ReplyKeyboardMarkup([[c] for c in city_names], resize_keyboard=True)
        await update.message.reply_text("بأي مدينة ؟ 😁", reply_markup=reply_markup)
        return ASK_CITY

    # الحالة الطبيعية: عرض الطلب لإرسال الموقع
    reply_markup = ReplyKeyboardMarkup([
        ["📍 إرسال موقعي"],
        ["عودة ➡️"]
    ], resize_keyboard=True)

    await update.message.reply_text("اختار إرسال موقعي إذا كنت مفعل خدمة الموقع GPS 📍", reply_markup=reply_markup)
    await asyncio.sleep(0.5)
    await update.message.reply_text("إذا ما كنت مفعل، دور على الموقع واضغط عليه مطولًا، ثم اختر إرسال 👇")
    await asyncio.sleep(0.5)
    await update.message.reply_text("اسمع مني 🔊 شغّل GPS وبس اضغط إرسال موقعي 📍")
    await asyncio.sleep(0.5)

    # ✅ زر شرح الموقع الآن مرفق مع آخر رسالة
    inline_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("عالسريع كيف ؟ 🔥", callback_data="how_to_send_location")]
    ])
    await update.message.reply_text(
        "ما بدا شي 😄\n👇 شوف شرح عسريع:",
        reply_markup=inline_markup
    )

    return ASK_LOCATION_IMAGE



async def explain_location_instruction(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()

    try:
        # إعدادات القناة وصورة التعليم
        MEDIA_CHANNEL_ID = -1002537649967  # ⬅️ غيّر هذا إلى ID القناة السري
        IMAGE_MESSAGE_ID = 2               # ⬅️ غيّر هذا إلى message_id للصورة داخل القناة

        await context.bot.copy_message(
            chat_id=query.from_user.id,
            from_chat_id=MEDIA_CHANNEL_ID,
            message_id=IMAGE_MESSAGE_ID
        )

    except Exception as e:
        logger.error(f"❌ فشل في إرسال صورة التعليم: {e}")
        await context.bot.send_message(chat_id=query.from_user.id, text="❌ لم نتمكن من عرض الشرح الآن.")



async def handle_location(update: Update, context: CallbackContext) -> int:
    if update.message.location:
        latitude = update.message.location.latitude
        longitude = update.message.location.longitude
        context.user_data['location_coords'] = {'latitude': latitude, 'longitude': longitude}
        return await ask_area_name(update, context)
    else:
        location_button = KeyboardButton("📍 إرسال موقعي", request_location=True)
        reply_markup = ReplyKeyboardMarkup([[location_button], ["عودة ➡️"]], resize_keyboard=True)
        await update.message.reply_text("❌ هذا مش موقع حقيقي.\nحبيبي اختار من الخيارات يلي تحت 👇", reply_markup=reply_markup)
        return ASK_LOCATION_IMAGE






async def ask_area_name(update: Update, context: CallbackContext) -> int:
    reply_markup = ReplyKeyboardMarkup([["عودة ➡️"]], resize_keyboard=True)

    await update.message.reply_text("📍 شو اسم المنطقة أو الشارع الذي تسكن فيه ضمن مدينتك؟\n"
                                    "مثلاً: الزراعة، شارع القلعة، أو قرب مدرسة كذا...",
                                    reply_markup=reply_markup)
    await asyncio.sleep(0.5)
    await update.message.reply_text("بدك تنتبه ! اذا كان موقعك ناقص او وهمي رح تنرفض طلبياتك 😥")
    await asyncio.sleep(0.5)
    await update.message.reply_text("سجل موقعك منيح لمرة وحدة بس مشان تريح حالك بعدين 🙂")
    await asyncio.sleep(0.5)
    await update.message.reply_text("قربت تخلص 😁")

    return ASK_AREA_NAME

async def handle_area_name(update: Update, context: CallbackContext) -> int:
    text = update.message.text.strip()

    if text == "عودة ➡️":
        # إذا اختار العودة، نرجع لسؤال إرسال الموقع
        return await ask_location(update, context)

    context.user_data["temporary_area_name"] = text

    reply_markup = ReplyKeyboardMarkup([["عودة ➡️"]], resize_keyboard=True)
    await update.message.reply_text("وين بالضبط ؟ 🤨", reply_markup=reply_markup)
    await asyncio.sleep(0.5)
    await update.message.reply_text("تخيل نفسك تحكي مع الديليفري: بأي بناء؟ معلم مميز؟ بأي طابق؟ كيف يشوفك بسرعة؟")
    await asyncio.sleep(0.9)
    await update.message.reply_text("خلصت هي اخر سؤال 😁")

    return ASK_DETAILED_LOCATION


async def ask_detailed_location(update: Update, context: CallbackContext) -> int:
    reply_markup = ReplyKeyboardMarkup([["عودة ➡️"]], resize_keyboard=True)

    await update.message.reply_text("وين بالضبط ؟ 🤨", reply_markup=reply_markup)
    await asyncio.sleep(0.5)
    await update.message.reply_text("تخيل نفسك تحكي مع الديليفري: بأي بناء؟ معلم مميز؟ بأي طابق؟ كيف يشوفك بسرعة؟")
    await asyncio.sleep(0.5)
    await context.bot.send_sticker(
        chat_id=update.effective_chat.id,
        sticker="CAACAgIAAxkBAAEBxudoMx_S9YodkQJ2aFqbWagsExrgXgAC_g0AAoGJqEhLiXZ1bM9WgDYE"
    )

    return ASK_DETAILED_LOCATION



async def confirm_info(update: Update, context: CallbackContext) -> int:
    if update.message.text == "عودة ➡️":
        context.user_data.pop('detailed_location', None)
        return await ask_area_name(update, context)

    context.user_data['detailed_location'] = update.message.text

    name = context.user_data.get('name', 'غير متوفر')
    phone = context.user_data.get('phone', 'غير متوفر')
    province = context.user_data.get('province_name', 'غير متوفر')
    city = context.user_data.get('city_name', 'غير متوفر')
    area_name = context.user_data.get('temporary_area_name', 'غير متوفر')
    detailed_location = context.user_data.get('detailed_location', 'غير متوفر')
    location_coords = context.user_data.get('location_coords', None)

    info_message = (
        f"🔹 الاسم: {name}\n"
        f"🔹 رقم الهاتف: {phone}\n"
        f"🔹 المحافظة: {province}\n"
        f"🔹 المدينة: {city}\n"
        f"🔹 اسم المنطقة أو الشارع: {area_name}\n"
        f"🔹 وصف تفصيلي للموقع: {detailed_location}"
    )

    reply_markup = ReplyKeyboardMarkup([
        ["اي ولو 😏"],
        ["لا بدي عدل 😐"]
    ], resize_keyboard=True)

    await update.message.reply_text("أخيراا 🔥")

    await context.bot.send_sticker(
        chat_id=update.effective_chat.id,
        sticker="CAACAgIAAxkBAAEBxupoMyA2CN7ETdFf8JloOif7qOc1XQACXhIAAuyZKUl879mlR_dkOzYE"
    )

    # ⏱️ تأخير بسيط قبل إرسال الموقع والمعلومات
    await asyncio.sleep(0.5)

    if location_coords:
        latitude = location_coords.get('latitude')
        longitude = location_coords.get('longitude')
        await update.message.reply_location(latitude=latitude, longitude=longitude)

    await update.message.reply_text(
        f"{info_message}\n\n🔴 متأكد خلص ؟ 🙃",
        parse_mode="Markdown",
        reply_markup=reply_markup
    )

    return CONFIRM_INFO



async def handle_confirmation(update: Update, context: CallbackContext) -> int:
    choice = update.message.text

     # 🛡️ التحقق من الإدخال غير المتوقع
    valid_choices = ["اي ولو 😏", "لا بدي عدل 😐"]
    if choice not in valid_choices:
        reply_markup = ReplyKeyboardMarkup([
            ["اي ولو 😏"],
            ["لا بدي عدل 😐"]
        ], resize_keyboard=True)
        await update.message.reply_text(
            "حبيبي اختار من الخيارات يلي تحت 👇",
            reply_markup=reply_markup
        )
        return CONFIRM_INFO

    if choice == "اي ولو 😏":
        try:
            user_id = update.effective_user.id
            name = context.user_data['name']
            phone = context.user_data['phone']
            province_id = context.user_data['province_id']
            city_id = context.user_data['city_id']
            area_name = context.user_data.get('temporary_area_name') or context.user_data.get('area_name', '')
            detailed_location = context.user_data.get('detailed_location', '')
            full_location = f"{area_name} - {detailed_location}".strip(" -")

            coords = context.user_data.get('location_coords', {})
            latitude = coords.get('latitude')
            longitude = coords.get('longitude')

            async with get_db_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("""
                        INSERT INTO user_data 
                        (user_id, name, phone, province_id, city_id, location_text, latitude, longitude)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        ON DUPLICATE KEY UPDATE
                            name = VALUES(name),
                            phone = VALUES(phone),
                            province_id = VALUES(province_id),
                            city_id = VALUES(city_id),
                            location_text = VALUES(location_text),
                            latitude = VALUES(latitude),
                            longitude = VALUES(longitude)
                    """, (user_id, name, phone, province_id, city_id, full_location, latitude, longitude))
            
                    await cursor.execute("SELECT ads_channel FROM cities WHERE id = %s", (city_id,))
                    result = await cursor.fetchone()
                    ads_channel = result[0] if result and result[0] else None
            
                await conn.commit()


            # ✅ حفظ حالة المستخدم بعد التسجيل
            await save_conversation_state(user_id, {
                "name": name,
                "phone": phone,
                "province_id": province_id,
                "city_id": city_id,
                "location_text": full_location,
                "location_coords": {
                    "latitude": latitude,
                    "longitude": longitude
                }
            })


            reply_markup = ReplyKeyboardMarkup([
                ["اطلب عالسريع 🔥"],
                ["لا بدي عدل 😐", "التواصل مع الدعم 🎧"],
                ["من نحن 🏢", "أسئلة متكررة ❓"]
            ], resize_keyboard=True)

            await update.message.reply_text(
                "هلا صار فيك تطلب عالسريع 🔥",
                reply_markup=reply_markup
            )
    
            await asyncio.sleep(0.7)

            await update.message.reply_text("وأيمت ما بدك فيك تعدل معلوماتك 🌝")

            if ads_channel:
                invite_keyboard = InlineKeyboardMarkup([[ 
                    InlineKeyboardButton("📢 لا تفوّت العروض! انضم لقناتنا", url=f"https://t.me/{ads_channel.lstrip('@')}")
                ]])
                await update.message.reply_text(
                    f"🎉 عروض يومية مخصصة لأهل مدينة {context.user_data['city_name']}!\n"
                    "انضم الآن لقناتنا لتكون أول من يعرف بالعروض 🔥",
                    reply_markup=invite_keyboard
                )

            return MAIN_MENU

        except Exception as e:
            logging.error(f"Error saving user data: {e}")
            await update.message.reply_text("❌ حدث خطأ أثناء حفظ المعلومات. حاول لاحقاً.")
            return MAIN_MENU

    elif choice == "لا بدي عدل 😐":
        return await ask_edit_choice(update, context)

    else:
        await update.message.reply_text("❌ يرجى اختيار أحد الخيارات المتاحة.")
        return CONFIRM_INFO





async def ask_edit_choice(update: Update, context: CallbackContext) -> int:
    reply_markup = ReplyKeyboardMarkup([
        ["✏️ الاسم"],
        ["📱 رقم الهاتف"],
        ["📍 الموقع"],
        ["عودة ⬅️"]
    ], resize_keyboard=True)
    await update.message.reply_text("شو بتحب تعدل؟", reply_markup=reply_markup)
    return EDIT_FIELD_CHOICE



async def handle_edit_field_choice(update: Update, context: CallbackContext) -> int:
    choice = update.message.text

    if choice == "✏️ الاسم":
        return await ask_name_edit(update, context)

    elif choice == "📱 رقم الهاتف":
        return await ask_phone_edit(update, context)

    elif choice == "📍 الموقع":
        return await ask_location_edit(update, context)

    elif choice == "عودة ⬅️":
        return await main_menu(update, context)

    else:
        await update.message.reply_text("❌ يرجى اختيار أحد الخيارات.")
        return EDIT_FIELD_CHOICE

async def ask_name_edit(update: Update, context: CallbackContext) -> int:
    reply_markup = ReplyKeyboardMarkup([["عودة ⬅️"]], resize_keyboard=True)
    await update.message.reply_text("شو الاسم الجديد؟ ✏️", reply_markup=reply_markup)
    return EDIT_NAME

async def handle_name_edit(update: Update, context: CallbackContext) -> int:
    if update.message.text == "عودة ⬅️":
        return await ask_edit_choice(update, context)

    context.user_data["name"] = update.message.text
    return await confirm_info(update, context)


async def ask_phone_edit(update: Update, context: CallbackContext) -> int:
    if update.message.text == "عودة ⬅️":
        return await ask_edit_choice(update, context)

    reply_markup = ReplyKeyboardMarkup([["عودة ⬅️"]], resize_keyboard=True)
    await update.message.reply_text("شو رقمك الجديد؟ 📱", reply_markup=reply_markup)
    return EDIT_PHONE

async def send_verification_code_edit(update: Update, context: CallbackContext) -> int:
    if update.message.text == "عودة ⬅️":
        return await ask_edit_choice(update, context)

    phone = update.message.text

    if not (phone.isdigit() and len(phone) == 10 and phone.startswith("09")):
        await update.message.reply_text("لازم يبلش بـ 09 ويكون من 10 أرقام 😒")
        return EDIT_PHONE

    context.user_data["phone"] = phone
    code = random.randint(10000, 99999)
    context.user_data["verification_code"] = code

    await send_message_with_retry(
        bot=context.bot,
        chat_id="@verifycode12345",  # ← القناة الحقيقية
        text=f"عزيزي المستخدم، رقمك الجديد هو {phone} والكود هو: {code} 🤗"
    )

    reply_markup = ReplyKeyboardMarkup([["عودة ⬅️"]], resize_keyboard=True)
    await update.message.reply_text("شو الكود يلي وصلك؟", reply_markup=reply_markup)
    return EDIT_PHONE_VERIFY

async def verify_code_edit(update: Update, context: CallbackContext) -> int:
    if update.message.text == "عودة ⬅️":
        return await ask_edit_choice(update, context)

    if update.message.text == str(context.user_data.get("verification_code")):
        return await confirm_info(update, context)
    else:
        await update.message.reply_text("❌ كود غلط، جرب مرة تانية.")
        return EDIT_PHONE_VERIFY


async def ask_location_edit(update: Update, context: CallbackContext) -> int:
    location_button = KeyboardButton("📍 إرسال موقعي", request_location=True)
    reply_markup = ReplyKeyboardMarkup([[location_button], ["عودة ⬅️"]], resize_keyboard=True)

    inline_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("عالسريع كيف ؟ 🔥", callback_data="how_to_send_location")]
    ])

    await update.message.reply_text("اختار إرسال موقعي إذا كنت مفعل خدمة الموقع GPS 📍", reply_markup=reply_markup)
    await asyncio.sleep(0.5)
    await update.message.reply_text("إذا ما كنت مفعل، دور على الموقع واضغط عليه مطولًا، ثم اختر إرسال 👇")
    await asyncio.sleep(0.5)
    await update.message.reply_text("اسمع مني 🔊 شغّل GPS وبس اضغط إرسال موقعي 📍")
    await asyncio.sleep(0.5)
    await update.message.reply_text("ما بدا شي 😄\n👇 شوف شرح عسريع:", reply_markup=inline_markup)

    return EDIT_LOCATION



async def handle_location_edit(update: Update, context: CallbackContext) -> int:
    if update.message.location:
        lat = update.message.location.latitude
        lon = update.message.location.longitude
        context.user_data['location_coords'] = {'latitude': lat, 'longitude': lon}
        return await ask_area_name(update, context)
    else:
        await update.message.reply_text("❌ هذا ليس موقعًا حقيقيًا. حاول مرة أخرى.")
        return EDIT_LOCATION

async def ask_location_edit_entry(update: Update, context: CallbackContext) -> int:
    # عرض خيارات إرسال الموقع مع زر "عودة"
    reply_markup = ReplyKeyboardMarkup([
        [KeyboardButton("📍 إرسال موقعي", request_location=True)],
        ["عودة ⬅️"]
    ], resize_keyboard=True)

    inline_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("عالسريع كيف ؟ 🔥", callback_data="how_to_send_location")]
    ])

    await update.message.reply_text("اختار إرسال موقعي إذا كنت مفعل خدمة الموقع GPS 📍", reply_markup=reply_markup)
    await asyncio.sleep(2)
    await update.message.reply_text("اذا ما بتعرف تشغل لوكيشن", reply_markup=inline_markup)

    return EDIT_LOCATION












async def maybe_send_package(update: Update, context: CallbackContext):
    try:
        last_time = context.user_data.get("last_order_time")
        if not last_time:
            return

        if isinstance(last_time, str):
            last_time = datetime.fromisoformat(last_time)

        if (datetime.now() - last_time) >= timedelta(days=5):
            package = random.choice(PACKAGES)
            await update.message.reply_text(package["text"])
            await context.bot.send_sticker(chat_id=update.effective_chat.id, sticker=package["sticker"])

            # تحديث الوقت حتى لا يظهر مرة أخرى مباشرة
            context.user_data["last_order_time"] = datetime.now()
    except Exception as e:
        print(f"خطأ في maybe_send_package: {e}")

PACKAGES = [
    {
        "text": "شو ؟ ",
        "sticker": "CAACAgIAAxkBAAEBxxtoM28OJ54uTXFiDx0pyaTKmcg4mwACNAEAAlKJkSMTzddv9RwHWDYE"
    },
    {
        "text": "اشتقتلك والله",
        "sticker": "CAACAgIAAxkBAAEBxxxoM28On-kDepA-XluW3lEuGyQllAACWBEAAr4RKEkctAGRFeQMEDYE"
    },
    {
        "text": "بحبك وبعرف مشي الي !",
        "sticker": "CAACAgIAAxkBAAEBxx1oM28OYWmOIZbfZN1W6cGCzvRpowACMT0AApxnUUhqOdZ3B7NPkzYE"
    },
    {
        "text": "عم تخوني ؟",
        "sticker": "CAACAgIAAxkBAAEBxyRoM2--odXSfkEWulipUHmbFDCXJgACEjgAAl_lqEikri3n1nbbXTYE"
    }
]




async def main_menu(update: Update, context: CallbackContext) -> int:
    choice = update.message.text
    user_id = update.effective_user.id

    # 🧹 حذف أي رسائل تفاعلية سابقة (من نحن، الدعم، FAQ...)
    for key in ["support_sticker_id", "support_msg_id", "about_us_msg_id", "faq_msg_id", "faq_answer_msg_id"]:
        msg_id = context.user_data.pop(key, None)
        if msg_id:
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
            except:
                pass

    # ✅ إرسال بكج إذا مر وقت طويل بدون طلب
    await maybe_send_package(update, context)

    if choice == "تعديل معلوماتي 🖊":
        return await ask_edit_choice(update, context)

    elif choice == "التواصل مع الدعم 🎧":
        support_button = InlineKeyboardMarkup([[
            InlineKeyboardButton("راسلنا على التلغرام 💬", url="https://t.me/Fast54522")
        ]])
        sent = await send_message_with_retry(
            bot=context.bot,
            chat_id=update.effective_chat.id,
            text="☎️ للتواصل معنا:\n"
                 "- 0999999999\n"
                 "- 0999999998\n\n"
                 "💬 أو تواصل معنا مباشرة عبر تلغرام من الزر أدناه 👇",
            reply_markup=support_button
        )
        sticker_msg = await context.bot.send_sticker(
            chat_id=update.effective_chat.id,
            sticker="CAACAgIAAxkBAAEBxvxoM2NN7whnEdE4ppLdFIao_3FjewACvAwAAocoMEntN5GZWCFoBDYE"
        )
        context.user_data["support_sticker_id"] = sticker_msg.message_id
        context.user_data["support_msg_id"] = sent.message_id
        return MAIN_MENU

    elif choice == "من نحن 🏢":
        try:
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton("📘 فيسبوك", url="https://facebook.com/yourpage"),
                 InlineKeyboardButton("📸 انستغرام", url="https://instagram.com/youraccount")],
                [InlineKeyboardButton("📢 قناتنا على تلغرام", url="https://t.me/yourchannel")]
            ])
            sent = await update.message.reply_text(
                "✅ بوتنا مرخص قانونياً لدى الدولة ويهدف إلى تحسين تجربة الطلبات.\n"
                "👨‍💻 لدينا فريق عمل جاهز للاستماع لنصائحكم دائماً لنتطور ونحسن لكم الخدمة.\n"
                "📲 تواصل معنا عبر المنصات التالية 👇",
                reply_markup=buttons
            )
            context.user_data["about_us_msg_id"] = sent.message_id
        except Exception as e:
            logger.error(f"❌ فشل إرسال رسالة من نحن: {e}")
            await update.message.reply_text("❌ لم نتمكن من عرض معلومات 'من نحن' حالياً. حاول لاحقاً.")
        return MAIN_MENU

    elif choice == "أسئلة متكررة ❓":
        return await handle_faq_entry(update, context)

    elif choice == "اطلب عالسريع 🔥":
        now = datetime.now()
        context.user_data["last_fast_order_time"] = now
        context.user_data["last_order_time"] = now

        cancel_times = context.user_data.get("cancel_history", [])
        cooldown, reason_msg = get_fast_order_cooldown(cancel_times)
        last_try = context.user_data.get("last_fast_order_time")

        if last_try and (now - last_try).total_seconds() < cooldown:
            remaining = int(cooldown - (now - last_try).total_seconds())
            minutes = max(1, remaining // 60)
            await update.message.reply_text(
                f"{reason_msg}\n⏳ الرجاء الانتظار {minutes} دقيقة قبل إعادة المحاولة.",
                reply_markup=ReplyKeyboardMarkup([["القائمة الرئيسية 🪧"]], resize_keyboard=True)
            )
            return MAIN_MENU

        # 🧹 تنظيف البيانات المؤقتة
        for key in [
            'temporary_location_text', 'temporary_location_coords',
            'temporary_total_price', 'temporary_area_name', 'temporary_detailed_location',
            'orders', 'order_confirmed', 'selected_restaurant', 'restaurant_map',
            'selected_restaurant_id', 'selected_restaurant_name',
            'selected_category_id', 'selected_category_name', 'go_ad_restaurant_name',
            'current_meal_messages', 'conversation_state'
        ]:
            context.user_data.pop(key, None)

        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cursor:
                    # 🗑️ حذف السلة
                    await cursor.execute("DELETE FROM shopping_carts WHERE user_id = %s", (user_id,))
                    await conn.commit()

                    # التحقق من الحظر
                    await cursor.execute("SELECT phone FROM user_data WHERE user_id = %s", (user_id,))
                    result = await cursor.fetchone()
                    if not result:
                        await update.message.reply_text("❌ لم يتم العثور على رقم هاتفك. يرجى إعادة التسجيل.")
                        return await start(update, context)

                    phone = result[0]
                    await cursor.execute("SELECT 1 FROM blacklisted_numbers WHERE phone = %s", (phone,))
                    if await cursor.fetchone():
                        await update.message.reply_text("❌ رقمك محظور مؤقتاً من استخدام الخدمة.\nللاستفسار: @Support")
                        return MAIN_MENU

                    # 📍 جلب المدينة
                    await cursor.execute("SELECT city_id FROM user_data WHERE user_id = %s", (user_id,))
                    row = await cursor.fetchone()
                    if not row:
                        await update.message.reply_text("❌ لم يتم العثور على مدينة مسجلة. يرجى التسجيل أولاً.")
                        return await start(update, context)

                    city_id = row[0]
                    await cursor.execute("SELECT id, name, is_frozen FROM restaurants WHERE city_id = %s", (city_id,))
                    rows = await cursor.fetchall()

                    if not rows:
                        await update.message.reply_text("❌ لا يوجد مطاعم حالياً في مدينتك.")
                        return MAIN_MENU

                    # ✅ عرض المطاعم
                    restaurants = []
                    restaurant_map = {}
                    for restaurant_id, name, is_frozen in rows:
                        if is_frozen:
                            continue
                        await cursor.execute(
                            "SELECT COUNT(*), AVG(rating) FROM restaurant_ratings WHERE restaurant_id = %s",
                            (restaurant_id,)
                        )
                        rating_data = await cursor.fetchone()
                        avg = round(rating_data[1], 1) if rating_data and rating_data[0] > 0 else 0
                        label = f"{name} ⭐ ({avg})"
                        restaurants.append(label)
                        restaurant_map[label] = {"id": restaurant_id, "name": name}

            if not restaurants:
                await update.message.reply_text("❌ جميع المطاعم في مدينتك مجمدة حالياً.")
                return MAIN_MENU

            restaurants += ["مطعمي المفضل وينو ؟ 😕", "القائمة الرئيسية 🪧"]
            context.user_data['restaurant_map'] = restaurant_map

            keyboard_buttons = [KeyboardButton(name) for name in restaurants]
            keyboard_layout = chunk_buttons(keyboard_buttons, cols=2)
            reply_markup = ReplyKeyboardMarkup(keyboard_layout, resize_keyboard=True)

            await update.message.reply_text("🔽 اختر المطعم الذي ترغب بالطلب منه:", reply_markup=reply_markup)
            return SELECT_RESTAURANT

        except Exception as e:
            import traceback
            logger.exception(f"❌ Database error in fast order: {e}")
            await update.message.reply_text(f"❌ خطأ داخلي:\n{e}")
            return MAIN_MENU



def chunk_buttons(buttons, cols=2):
    return [buttons[i:i + cols] for i in range(0, len(buttons), cols)]




async def clear_main_menu_context(update: Update, context: CallbackContext):
    for key in [
        "support_sticker_id", "support_msg_id",
        "about_us_msg_id", "faq_msg_id", "faq_answer_msg_id"
    ]:
        msg_id = context.user_data.pop(key, None)
        if msg_id:
            try:
                await context.bot.delete_message(
                    chat_id=update.effective_chat.id,
                    message_id=msg_id
                )
            except:
                pass



async def handle_faq_entry(update: Update, context: CallbackContext) -> int:
    # حذف ستيكر الدعم إن وُجد
    support_sticker_id = context.user_data.pop("support_sticker_id", None)
    if support_sticker_id:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=support_sticker_id)
        except:
            pass

    # حذف رسالة الأسئلة السابقة إن وُجدت
    old_faq_msg_id = context.user_data.get("faq_msg_id")
    if old_faq_msg_id:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=old_faq_msg_id)
        except:
            pass

    # أزرار الأسئلة الشائعة
    faq_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❓ لماذا يتم رفض طلبي", callback_data="faq_refusal")],
        [InlineKeyboardButton("⏱️ ما هو الوقت المتوقع لوصول الطلب", callback_data="faq_eta")],
        [InlineKeyboardButton("🛑 ماذا أفعل إذا واجهت مشكلة في تسجيل الطلب", callback_data="faq_issue")],
        [InlineKeyboardButton("🚫 لماذا حسابي محظور؟", callback_data="faq_ban")],
        [InlineKeyboardButton("📦❌ ماذا لو طلبت ولم أستلم الطلب؟", callback_data="faq_no_delivery")],
        [InlineKeyboardButton("🔁 ماذا لو طلبت وألغيت كثيرًا؟", callback_data="faq_repeat_cancel")]
    ])

    sent = await update.message.reply_text(
        "شو في بالك من هالأسئلة؟ 👇",
        reply_markup=faq_keyboard
    )
    context.user_data["faq_msg_id"] = sent.message_id
    return MAIN_MENU


async def handle_faq_response(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()

    old_faq_msg_id = context.user_data.get("faq_msg_id")
    if old_faq_msg_id:
        try:
            await context.bot.delete_message(chat_id=query.message.chat.id, message_id=old_faq_msg_id)
        except:
            pass

    faq_answers = {
        "faq_refusal": "🚫 يتم رفض الطلب غالبًا بسبب نقص في البيانات أو التوصيل خارج النطاق أو تقييمات سابقة سلبية.",
        "faq_eta": "⏱️ عادةً يتم التوصيل خلال 30 إلى 60 دقيقة حسب الضغط ومسافة المطعم.",
        "faq_issue": "🛑 إذا واجهت مشكلة أثناء الطلب، أعد المحاولة أو تواصل مع الدعم عبر @Support.",
        "faq_ban": "🚫 قد يتم حظر الحساب في حال تكرار الإلغاء أو عدم التواجد لاستلام الطلب.",
        "faq_no_delivery": "📦 إذا لم تستلم الطلب، راجع إشعارات القناة أو تواصل مع المطعم مباشرة.",
        "faq_repeat_cancel": "🔁 الإلغاء المتكرر يسبب مشاكل في النظام، وقد يؤدي إلى حظر مؤقت."
    }

    selected = query.data
    answer = faq_answers.get(selected, "❓ لم يتم العثور على إجابة لهذا السؤال.")

    back_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 عودة للأسئلة المتكررة", callback_data="faq_back")]
    ])

    sent = await query.message.reply_text(
        answer,
        reply_markup=back_keyboard
    )
    context.user_data["faq_answer_msg_id"] = sent.message_id



async def handle_faq_back(update: Update, context: CallbackContext) -> None:
    query = update.callback_query
    await query.answer()

    # حذف رسالة الجواب
    answer_msg_id = context.user_data.get("faq_answer_msg_id")
    if answer_msg_id:
        try:
            await context.bot.delete_message(chat_id=query.message.chat.id, message_id=answer_msg_id)
        except:
            pass

    # عرض الأسئلة مجددًا
    return await handle_faq_entry(update, context)





async def handle_restaurant_selection(update: Update, context: CallbackContext) -> int:
    selected_option = update.message.text
    restaurant_map = context.user_data.get('restaurant_map', {})
    user_id = update.effective_user.id

    # 🛡️ التحقق من الخيارات الخاصة
    if selected_option == "مطعمي المفضل وينو ؟ 😕":
        # معالجة هذا الخيار...
        return ASK_NEW_RESTAURANT_NAME
    
    if selected_option == "القائمة الرئيسية 🪧":
        return await main_menu(update, context)
    
    # 🛡️ التحقق من الإدخال غير المتوقع
    valid_choices = list(restaurant_map.keys()) + ["مطعمي المفضل وينو ؟ 😕", "القائمة الرئيسية 🪧"]
    if selected_option not in valid_choices:
        keyboard_buttons = [KeyboardButton(name) for name in valid_choices]
        keyboard_layout = chunk_buttons(keyboard_buttons, cols=2)
        reply_markup = ReplyKeyboardMarkup(keyboard_layout, resize_keyboard=True)
        
        await update.message.reply_text(
            "حبيبي اختار من الخيارات يلي تحت 👇",
            reply_markup=reply_markup
        )
        return SELECT_RESTAURANT

    if selected_option == "مطعمي المفضل وينو ؟ 😕":
        reply_markup = ReplyKeyboardMarkup([["عودة ➡️"]], resize_keyboard=True)
        await update.message.reply_text("شو اسم مطعم رح نحكيه عالسريع ! 🔥", reply_markup=reply_markup)
        return ASK_NEW_RESTAURANT_NAME

    if selected_option == "القائمة الرئيسية 🪧":
        return await main_menu(update, context)

    restaurant_data = restaurant_map.get(selected_option)
    if not restaurant_data:
        # مطابقة مرنة
        for label, data in restaurant_map.items():
            if selected_option.strip() in label:
                restaurant_data = data
                break

    if not restaurant_data:
        await update.message.reply_text("❌ المطعم الذي اخترته غير موجود. يرجى اختيار مطعم آخر.")
        return SELECT_RESTAURANT

    restaurant_id = restaurant_data["id"]
    restaurant_name = restaurant_data["name"]

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT is_frozen FROM restaurants WHERE id = %s", (restaurant_id,))
                result = await cursor.fetchone()
                if not result:
                    await update.message.reply_text("❌ لم يتم العثور على حالة المطعم.")
                    return SELECT_RESTAURANT

                is_frozen = result[0]
                if is_frozen:
                    await update.message.reply_text(f"❌ المطعم {restaurant_name} خارج الخدمة مؤقتاً.")
                    return SELECT_RESTAURANT

        # ✅ تخزين المطعم والمتابعة
        context.user_data["selected_restaurant_id"] = restaurant_id
        context.user_data["selected_restaurant_name"] = restaurant_name
        context.user_data["selected_restaurant"] = restaurant_name
        
        # حفظ المطعم المختار في حالة المحادثة أيضاً
        await update_conversation_state(user_id, 'selected_restaurant', restaurant_name)

        await show_restaurant_categories(update, context)  # ← تعرض الفئات
        return ORDER_CATEGORY  # ← تحدد المرحلة القادمة

    except Exception as e:
        logger.exception(f"❌ خطأ في handle_restaurant_selection: {e}")
        await update.message.reply_text("❌ حدث خطأ أثناء معالجة المطعم. حاول مرة أخرى.")
        return SELECT_RESTAURANT







async def check_restaurant_availability(restaurant_id: int) -> bool:
    try:
        damascus_time = datetime.now(pytz.timezone("Asia/Damascus"))
        now_hour = damascus_time.hour + damascus_time.minute / 60

        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    SELECT open_hour, close_hour, is_frozen
                    FROM restaurants
                    WHERE id = %s
                """, (restaurant_id,))
                result = await cursor.fetchone()

        if not result:
            logger.warning(f"⚠️ المطعم غير موجود: {restaurant_id}")
            return False

        open_hour, close_hour, is_frozen = result
        if is_frozen:
            return False

        return open_hour <= now_hour < close_hour

    except Exception as e:
        logger.exception(f"❌ خطأ في check_restaurant_availability: {e}")
        return False

async def show_restaurant_categories(update: Update, context: CallbackContext) -> int:
    restaurant_id = context.user_data.get("selected_restaurant_id")

    if not restaurant_id:
        await update.message.reply_text("❌ لم يتم تحديد المطعم.")
        return MAIN_MENU

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT name FROM restaurants WHERE id = %s", (restaurant_id,))
                row = await cursor.fetchone()
                if not row:
                    await update.message.reply_text("❌ لم يتم العثور على اسم المطعم.")
                    return MAIN_MENU

                restaurant_name = row[0]
                context.user_data["selected_restaurant_name"] = restaurant_name

                await cursor.execute("SELECT id, name FROM categories WHERE restaurant_id = %s ORDER BY name", (restaurant_id,))
                rows = await cursor.fetchall()

        if not rows:
            await update.message.reply_text("❌ لا توجد فئات لهذا المطعم حالياً.")
            return MAIN_MENU

        category_map = {}
        keyboard = []
        for category_id, name in rows:
            keyboard.append([KeyboardButton(name)])
            category_map[name] = category_id

        context.user_data["category_map"] = category_map

        reply_markup = ReplyKeyboardMarkup(keyboard + [["القائمة الرئيسية 🪧"]], resize_keyboard=True)
        await update.message.reply_text(f"📋 اختر الفئة من مطعم {restaurant_name}:", reply_markup=reply_markup)
        return ORDER_CATEGORY

    except Exception as e:
        logger.error(f"❌ Database error in show_restaurant_categories: {e}")
        await update.message.reply_text("❌ حدث خطأ أثناء جلب الفئات.")
        return MAIN_MENU





async def send_order_help_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            "🙋  إذا كنت حابب تختار سندويشة + كولا 👈 اختر فئة السندويش 👈 شاورما "
            "بعدين فيك تختار فئة المشروبات ببساطة وتختار كولا 👈 تم ✅\n\n"
            "🙋  إذا غيرت رأيك حتى لو فتت عفئات تانية "
            "فيك ترجع على فئة السندويش 👈 وتضغط على حذف أخر لمسة عند الشاورما 👈 "
            "وهيك بصير طلبك كولا بس 🙂\n\n"
            "🙋  حابب تصفر الطلب وترجع تغير مطعمك 👈 اختر القائمة الرئيسية 🤗"
        )
    )



async def handle_order_flow_help(update: Update, context: CallbackContext):
    query = update.callback_query
    await query.answer()
    await send_order_help_text(update, context)





async def has_active_order(user_id: int) -> bool:
    """
    التحقق من وجود طلب نشط للمستخدم.
    يشمل الحالات:
    - قيد التوصيل
    - بانتظار الكاشير
    - جاري المعالجة
    - pending
    - in_progress
    """
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    SELECT status FROM orders 
                    WHERE user_id = %s 
                    ORDER BY id DESC 
                    LIMIT 1
                """, (user_id,))
                row = await cursor.fetchone()

        if not row:
            return False

        return row[0] in [
            "قيد التوصيل", "بانتظار الكاشير", "جاري المعالجة",
            "pending", "in_progress"
        ]

    except Exception as e:
        logger.error(f"❌ Error checking active order for user {user_id}: {e}")
        return False



async def handle_missing_restaurant(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    text = update.message.text.strip()

    try:
        async with get_db_connection() as conn:
            if text == "عودة ➡️":
                # ✅ جلب city_id من user_data
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT city_id FROM user_data WHERE user_id = %s", (user_id,))
                    row = await cursor.fetchone()
                if not row:
                    await update.message.reply_text("❌ لم يتم العثور على المدينة. يرجى إعادة التسجيل.")
                    return await start(update, context)
                city_id = row[0]

                # ✅ جلب المطاعم المتاحة
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT id, name, is_frozen FROM restaurants WHERE city_id = %s", (city_id,))
                    rows = await cursor.fetchall()

                restaurants = []
                restaurant_map = {}

                for rest_id, name, is_frozen in rows:
                    if is_frozen:
                        continue

                    async with conn.cursor() as cursor:
                        await cursor.execute("SELECT COUNT(*), AVG(rating) FROM restaurant_ratings WHERE restaurant_id = %s", (rest_id,))
                        rating_data = await cursor.fetchone()

                    avg = round(rating_data[1], 1) if rating_data and rating_data[0] > 0 else 0
                    label = f"{name} ⭐ ({avg})"

                    restaurants.append(label)
                    restaurant_map[name] = {"id": rest_id, "name": name}

                restaurants += ["القائمة الرئيسية 🪧", "مطعمي المفضل وينو ؟ 😕"]
                context.user_data["restaurant_map"] = restaurant_map

                reply_markup = ReplyKeyboardMarkup([[r] for r in restaurants], resize_keyboard=True)
                await update.message.reply_text("🔙 اختر المطعم الذي ترغب بالطلب منه:", reply_markup=reply_markup)
                return SELECT_RESTAURANT

            # ✅ المستخدم كتب اسم مطعم غير موجود
            missing_restaurant_name = text
            missing_restaurant_channel = "@Lamtozkar"

            # 🧠 جلب المدينة والمحافظة لعرضها في التقرير
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT city_id, province_id FROM user_data WHERE user_id = %s", (user_id,))
                row = await cursor.fetchone()

            city_name = province_name = "غير معروفة"
            if row:
                city_id, province_id = row

                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT name FROM cities WHERE id = %s", (city_id,))
                    city_row = await cursor.fetchone()
                    if city_row:
                        city_name = city_row[0]

                    await cursor.execute("SELECT name FROM provinces WHERE id = %s", (province_id,))
                    province_row = await cursor.fetchone()
                    if province_row:
                        province_name = province_row[0]

            try:
                await send_message_with_retry(
                    bot=context.bot,
                    chat_id=missing_restaurant_channel,
                    text=(
                        f"📢 زبون جديد اقترح إضافة مطعم:\n\n"
                        f"🏪 اسم المطعم: {missing_restaurant_name}\n"
                        f"🌍 المدينة: {city_name}\n"
                        f"📍 المحافظة: {province_name}\n"
                        f"👤 المستخدم: @{update.effective_user.username or 'غير متوفر'}"
                    )
                )
                await update.message.reply_text("✅ تم إرسال اسم المطعم بنجاح. سنقوم بالتواصل معه قريباً! 🙏")
            except Exception as e:
                logger.error(f"❌ خطأ أثناء إرسال اسم المطعم إلى القناة: {e}")
                await update.message.reply_text("❌ حدث خطأ أثناء إرسال اسم المطعم. يرجى المحاولة لاحقاً.")

            # ✅ بعد إرسال المطعم، نعيد عرض المطاعم كالسابق
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT id, name, is_frozen FROM restaurants WHERE city_id = %s", (city_id,))
                rows = await cursor.fetchall()

            restaurants = []
            restaurant_map = {}

            for rest_id, name, is_frozen in rows:
                if is_frozen:
                    continue

                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT COUNT(*), AVG(rating) FROM restaurant_ratings WHERE restaurant_id = %s", (rest_id,))
                    rating_data = await cursor.fetchone()

                avg = round(rating_data[1], 1) if rating_data and rating_data[0] > 0 else 0
                label = f"{name} ⭐ ({avg})"
                restaurants.append(label)
                restaurant_map[name] = {"id": rest_id, "name": name}

            restaurants += ["القائمة الرئيسية 🪧", "مطعمي المفضل وينو ؟ 😕"]
            context.user_data["restaurant_map"] = restaurant_map

            reply_markup = ReplyKeyboardMarkup([[r] for r in restaurants], resize_keyboard=True)
            await update.message.reply_text("🔙 اختر المطعم الذي ترغب بالطلب منه:", reply_markup=reply_markup)
            return SELECT_RESTAURANT

    except Exception as e:
        logger.exception(f"❌ خطأ في handle_missing_restaurant: {e}")
        await update.message.reply_text("❌ حدث خطأ أثناء جلب بيانات المطاعم. حاول مجددًا لاحقاً.")
        return SELECT_RESTAURANT






async def handle_order_category(update: Update, context: CallbackContext) -> int:
    return await process_category_selection(update, context)

async def process_category_selection(update: Update, context: CallbackContext) -> int:
    category_name = update.message.text
    logger.info(f"📥 اختار المستخدم الفئة: {category_name}")

    if category_name == "القائمة الرئيسية 🪧":
        reply_markup = ReplyKeyboardMarkup([
            ["اطلب عالسريع 🔥"],
            ["لا بدي عدل 😐", "التواصل مع الدعم 🎧"],
            ["من نحن 🏢", "أسئلة متكررة ❓"]
        ], resize_keyboard=True)
        await update.message.reply_text("📋 تمت العودة إلى القائمة الرئيسية.", reply_markup=reply_markup)
        return MAIN_MENU

    category_map = context.user_data.get("category_map", {})
    category_id = category_map.get(category_name)

    if not category_id:
        await update.message.reply_text("❌ الفئة غير موجودة. يرجى اختيار فئة صالحة.")
        return ORDER_CATEGORY

    context.user_data["selected_category_id"] = category_id
    context.user_data["selected_category_name"] = category_name

    # حذف الرسائل السابقة للوجبات إن وجدت
    old_messages = context.user_data.pop("current_meal_messages", [])
    for msg_id in old_messages:
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
        except Exception as e:
            logger.error(f"❌ فشل في حذف رسالة قديمة: {e}")

    # إنشاء قائمة جديدة لتخزين معرفات الرسائل الجديدة
    context.user_data["current_meal_messages"] = []

    # عرض رسالة انتظار
    wait_message = await update.message.reply_text("⏳ جاري تحميل الوجبات...")

    try:
        await show_meals_in_category(update, context)
        
        # حذف رسالة الانتظار
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=wait_message.message_id)
        except:
            pass

        # عرض أزرار الفئات
        restaurant_id = context.user_data.get("selected_restaurant_id")
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT id, name FROM categories WHERE restaurant_id = %s", (restaurant_id,))
                categories = await cursor.fetchall()

        category_map = {}
        for cat_id, name in categories:
            category_map[name] = cat_id
        context.user_data["category_map"] = category_map

        categories = list(category_map.keys())
        keyboard = [[cat] for cat in categories]
        keyboard.append(["تم ✅", "القائمة الرئيسية 🪧"])
        reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="اذا حاطط ببالك مشروب كمان أو أي شي، فيك تختار من القائمة أسفل الشاشة 👇 وبس تخلص اضغط تم 👌",
            reply_markup=reply_markup
        )

        context.user_data["conversation_state"] = ORDER_MEAL
        return ORDER_MEAL

    except Exception as e:
        import traceback
        logger.error(f"❌ خطأ في process_category_selection: {traceback.format_exc()}")
        try:
            await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=wait_message.message_id)
        except:
            pass
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ حدث خطأ أثناء تحميل الوجبات. يرجى المحاولة لاحقاً."
        )
        return ORDER_CATEGORY










# 📸 دالة لاختبار نسخ صورة من القناة
async def test_copy_image(update: Update, context: CallbackContext):
    try:
        await context.bot.copy_message(
            chat_id=update.effective_chat.id,
            from_chat_id=ADMIN_MEDIA_CHANNEL,  # يجب أن يكون رقماً مثل -1001234567890
            message_id=123  # غيّر هذا إلى message_id الفعلي الموجود في القناة
        )
        await update.message.reply_text("✅ تم نسخ الصورة بنجاح.")
    except Exception as e:
        await update.message.reply_text(f"❌ فشل نسخ الصورة: {e}")




async def handle_add_meal_with_size(update: Update, context: CallbackContext) -> int:
    logger.warning("🔥 تم الضغط على زر إضافة وجبة.")
    logger.warning(f"📍 context.user_data['conversation_state'] = {context.user_data.get('conversation_state')}")

    query = update.callback_query
    await query.answer()
    logger.info(f"⬇️ تم الضغط على الزر: {query.data}")

    try:
        _, meal_id_str, size = query.data.split(":")
        meal_id = int(meal_id_str)
        user_id = update.effective_user.id

        # ⬅️ تخزين meal_id مؤقتًا في user_data (في حال احتجناه لاحقًا)
        context.user_data["meal_id_str"] = meal_id_str

        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    SELECT name, price, size_options
                    FROM meals
                    WHERE id = %s
                """, (meal_id,))
                result = await cursor.fetchone()

                if not result:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text="❌ لم يتم العثور على الوجبة."
                    )
                    return ORDER_MEAL

                meal_name, base_price, size_options_json = result

                try:
                    size_options = json.loads(size_options_json or "[]")
                except Exception as e:
                    logger.error(f"❌ فشل في قراءة size_options: {e}")
                    size_options = []

                price = base_price or 0
                if size != "default":
                    for opt in size_options:
                        if opt.get("name") == size:
                            price = opt.get("price", price)
                            break

                # ✅ item_data يتضمن meal_id
                item_data = {
                    "name": meal_name,
                    "size": size,
                    "price": price,
                    "meal_id": meal_id
                }

                logger.info(f"🛒 item_data المحضر للإضافة إلى السلة: {item_data}")

                # ✅ إضافة للسلة
                orders, total_price = await add_item_to_cart(user_id, item_data, context)
                context.user_data["orders"] = orders
                context.user_data["temporary_total_price"] = total_price

                # 🧹 حذف رسالة الملخص السابقة إن وجدت
                msg_id = context.user_data.pop("summary_msg_id", None)
                if msg_id:
                    try:
                        await context.bot.delete_message(update.effective_chat.id, msg_id)
                    except:
                        pass

                # 📝 بناء ملخص جديد
                summary_counter = defaultdict(int)
                for item in orders:
                    label = f"{item['name']} ({item['size']})" if item["size"] != "default" else item["name"]
                    summary_counter[label] += 1

                summary_lines = [f"{count} × {label}" for label, count in summary_counter.items()]
                summary_text = "\n".join(summary_lines)

                text = (
                    f"✅ تمت إضافة: {item_data['name']} ({item_data['size']})\n\n"
                    f"🛒 طلبك حتى الآن:\n{summary_text}\n\n"
                    f"💰 المجموع: {total_price} ل.س\n"
                    f"عندما تنتهي اختر ✅ تم من الأسفل"
                )

                msg = await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=text
                )
                context.user_data["summary_msg_id"] = msg.message_id
                await update_conversation_state(user_id, "summary_msg_id", msg.message_id)

                logger.info("✅ تم تنفيذ handle_add_meal_with_size بنجاح.")
                return ORDER_MEAL

    except Exception as e:
        logger.error(f"❌ استثناء في handle_add_meal_with_size: {e}", exc_info=True)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="❌ حدث خطأ أثناء إضافة الوجبة. حاول لاحقاً."
        )
        return ORDER_MEAL






async def add_item_to_cart(user_id: int, item_data: dict, context: CallbackContext):
    user_id = int(user_id)
    
    try:
        cart = await get_cart_from_db(user_id) or []
        
        # تأكد من تخزين معرف الوجبة في item_data إذا كان متاحاً
        if 'meal_id' not in item_data and 'meal_id_str' in context.user_data:
            item_data['meal_id'] = int(context.user_data['meal_id_str'])
        
        cart.append(item_data)
        
        saved = await save_cart_to_db(user_id, cart)
        if not saved:
            logger.warning(f"⚠️ لم يتم حفظ السلة للمستخدم {user_id}")
        
        # حفظ نسخة محلية من السلة
        context.user_data["cart"] = cart
        
        total_price = sum(item.get("price", 0) for item in cart)
        return cart, total_price
        
    except Exception as e:
        logger.error(f"❌ خطأ داخل add_item_to_cart: {e}", exc_info=True)
        return [], 0









async def handle_remove_specific_meal(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    query = update.callback_query
    await query.answer()
    
    try:
        # استخراج معلومات الوجبة من callback_data
        _, meal_id_str, _ = query.data.split(":")
        meal_id = int(meal_id_str)
        
        # استرجاع السلة من قاعدة البيانات
        cart = await get_cart_from_db(user_id) or []
        
        if not cart:
            await query.answer("❌ لا توجد وجبات في سلتك!")
            return ORDER_MEAL
        
        # استرجاع اسم الوجبة من قاعدة البيانات
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT name FROM meals WHERE id = %s", (meal_id,))
                result = await cursor.fetchone()
                if not result:
                    await query.answer("❌ لم يتم العثور على الوجبة!")
                    return ORDER_MEAL
                meal_name = result[0]
        
        # البحث عن آخر وجبة مطابقة في السلة (من النهاية إلى البداية)
        removed = False
        removed_item = None
        
        # البحث من النهاية للبداية للعثور على آخر وجبة مطابقة
        for i in range(len(cart) - 1, -1, -1):
            item = cart[i]
            if item.get('name') == meal_name or item.get('meal_id') == meal_id:
                removed_item = cart.pop(i)
                removed = True
                break
        
        if not removed:
            await query.answer("❌ لم يتم العثور على الوجبة في سلتك!")
            return ORDER_MEAL
        
        # حفظ السلة المحدثة
        await save_cart_to_db(user_id, cart)
        await asyncio.sleep(0.3)  # تأخير صغير للتأكد من حفظ البيانات
        
        # تحديث النسخة المحلية
        context.user_data["orders"] = cart
        context.user_data["cart"] = cart
        
        # تأكيد الحذف للمستخدم
        size_text = f" ({removed_item.get('size')})" if removed_item.get('size') != "default" else ""
        await query.answer(f"✅ تم حذف: {removed_item.get('name')}{size_text}")
        
        # إعداد ملخص جديد
        summary_counter = defaultdict(int)
        for item in cart:
            label = f"{item['name']} ({item['size']})" if item['size'] != "default" else item['name']
            summary_counter[label] += 1
        
        summary_lines = [f"{count} × {label}" for label, count in summary_counter.items()]
        summary_text = "\n".join(summary_lines)
        total_price = sum(item.get('price', 0) for item in cart)
        
        # حذف رسالة الملخص السابقة إن وُجدت
        old_msg_id = context.user_data.get("summary_msg_id")
        if old_msg_id:
            try:
                await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=old_msg_id)
            except:
                pass
        
        # إرسال رسالة الملخص الجديدة
        if cart:
            text = (
                f"🛒 طلبك بعد الحذف:\n{summary_text}\n\n"
                f"💰 المجموع: {total_price} ل.س\n"
                f"عندما تنتهي اختر ✅ تم من الأسفل"
            )
        else:
            text = "✅ تم حذف آخر وجبة. سلتك الآن فارغة."
        
        msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text
        )
        context.user_data["summary_msg_id"] = msg.message_id
        
        return ORDER_MEAL
        
    except Exception as e:
        logger.error(f"❌ خطأ في handle_remove_specific_meal: {e}", exc_info=True)
        await query.answer("❌ حدث خطأ أثناء حذف الوجبة")
        return ORDER_MEAL










async def show_meals_in_category(update: Update, context: CallbackContext):
    category_id = context.user_data.get("selected_category_id")

    if not category_id:
        logger.error("❌ لم يتم تحديد الفئة في show_meals_in_category")
        await update.message.reply_text("❌ حدث خطأ: لم يتم تحديد الفئة.")
        return

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    SELECT id, name, caption, image_file_id, size_options, price
                    FROM meals
                    WHERE category_id = %s
                """, (category_id,))
                meals = await cursor.fetchall()

        if not meals:
            await update.message.reply_text("❌ لا توجد وجبات في هذه الفئة.")
            return

        meal_messages = []
        ADMIN_MEDIA_CHANNEL = -1002659459294  # تأكد من تعريفه

        for meal in meals:
            meal_id, name, caption, image_file_id, size_json, price = meal
            try:
                sizes = json.loads(size_json or "[]")
            except:
                sizes = []

            buttons = []

            if sizes:
                # تعديل: وضع جميع أزرار القياسات في صف واحد أفقي مع تبسيط النص
                size_buttons = []
                for opt in sizes:
                    # تبسيط النص ليكون "القياس السعر" فقط بدون رموز
                    size_buttons.append(
                        InlineKeyboardButton(
                            f"{opt['name']} {opt['price']}",
                            callback_data=f"add_meal_with_size:{meal_id}:{opt['name']}"
                        )
                    )
                # إضافة جميع أزرار القياسات في صف واحد
                buttons.append(size_buttons)
            else:
                # في حال لا يوجد قياسات، أضف زر إضافة للسلة
                buttons.append([
                    InlineKeyboardButton(f"➕ أضف للسلة ({price})", callback_data=f"add_meal_with_size:{meal_id}:default")
                ])

            # ✅ زر حذف اللمسة الأخيرة في صف مستقل
            buttons.append([
                InlineKeyboardButton("❌ حذف اللمسة الأخيرة", callback_data=f"remove_specific_meal:{meal_id}:last")
            ])

            # إزالة زر "تم" من أزرار InlineKeyboard كما طلب المستخدم

            reply_markup = InlineKeyboardMarkup(buttons)

            # 🧠 بناء الكابشن
            caption_text = f"🍽️ {name}\n\n{caption}" if caption else f"🍽️ {name}"
            if price:
                caption_text += f"\n💰 السعر: {price} ل.س"

            try:
                if image_file_id:
                    if image_file_id.startswith("AgAC"):  # file_id مباشر
                        photo_msg = await context.bot.send_photo(
                            chat_id=update.effective_chat.id,
                            photo=image_file_id,
                            caption=caption_text,
                            reply_markup=reply_markup,
                            parse_mode="HTML"
                        )
                        meal_messages.append(photo_msg.message_id)
                    else:  # message_id رقمي من القناة
                        copied = await context.bot.copy_message(
                            chat_id=update.effective_chat.id,
                            from_chat_id=ADMIN_MEDIA_CHANNEL,
                            message_id=int(image_file_id)
                        )
                        meal_messages.append(copied.message_id)

                        caption_msg = await context.bot.send_message(
                            chat_id=update.effective_chat.id,
                            text=caption_text,
                            reply_markup=reply_markup,
                            parse_mode="HTML"
                        )
                        meal_messages.append(caption_msg.message_id)
                else:
                    caption_msg = await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=caption_text,
                        reply_markup=reply_markup
                    )
                    meal_messages.append(caption_msg.message_id)
            except Exception as e:
                logger.error(f"❌ فشل في عرض الوجبة '{name}': {e}")
                msg = await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=caption_text,
                    reply_markup=reply_markup
                )
                meal_messages.append(msg.message_id)

        context.user_data["current_meal_messages"] = meal_messages
    except Exception as e:
        logger.error(f"❌ خطأ في show_meals_in_category: {e}")
        await update.message.reply_text("❌ حدث خطأ أثناء عرض الوجبات.")




async def handle_done_adding_meals(update: Update, context: CallbackContext) -> int:
    orders = context.user_data.get("orders", [])
    if not isinstance(orders, list) or not orders:
        await update.message.reply_text("❌ لم تقم بإضافة أي وجبة بعد.")
        return ORDER_MEAL

    total_price = sum(item.get("price", 0) for item in orders)
    context.user_data["temporary_total_price"] = total_price

    # تنظيم الطلبات لتلخيصها
    summary_counter = defaultdict(int)
    for item in orders:
        name = item.get("name", "غير معروف")
        size = item.get("size", "default")
        label = f"{name} ({size})" if size != "default" else name
        summary_counter[label] += 1

    summary_lines = [f"{count} × {label}" for label, count in summary_counter.items()]
    summary_text = "\n".join(summary_lines)

    reply_markup = ReplyKeyboardMarkup(
        [
            ["تخطي ➡️"],
            ["القائمة الرئيسية 🪧"]
        ],
        resize_keyboard=True
    )


    await update.message.reply_text(
        f"🛒 *ملخص طلبك:*\n{summary_text}\n\n"
        f"💰 *المجموع:* {total_price} ل.س\n\n"
        "هل لديك ملاحظات على الطلب (مثل: زيادة جبنة، بلا بصل...)?\n"
        "يمكنك إرسال الملاحظة الآن أو الضغط على 'تخطي ➡️'.",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

    await context.bot.send_sticker(
        chat_id=update.effective_chat.id,
        sticker="CAACAgIAAxkBAAEBxv9oM2VLrcxfq5FSQvvYLQ_TfEs1qQACxREAArCasUhdUZ2-kKVX2jYE"
    )

    return ASK_ORDER_NOTES







async def fixed_orders_from_legacy_dict(orders_dict: dict) -> list:
    fixed_orders = []

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                for key, count in orders_dict.items():
                    try:
                        name, size = key.rsplit(" (", 1)
                        size = size.rstrip(")")
                    except:
                        name, size = key, "default"

                    await cursor.execute("SELECT price, size_options FROM meals WHERE name = %s", (name.strip(),))
                    result = await cursor.fetchone()

                    price = 0
                    if result:
                        base_price, size_options_json = result
                        if size != "default" and size_options_json:
                            try:
                                size_options = json.loads(size_options_json)
                                for opt in size_options:
                                    if opt["name"] == size:
                                        price = opt["price"]
                                        break
                            except:
                                price = base_price or 0
                        else:
                            price = base_price or 0

                    for _ in range(count):
                        fixed_orders.append({
                            "name": name.strip(),
                            "size": size.strip(),
                            "price": price
                        })

    except Exception as e:
        logger.error(f"❌ خطأ أثناء تحويل الطلبات القديمة: {e}")

    return fixed_orders




async def return_to_main_menu(update: Update, context: CallbackContext) -> int:
    reply_markup = ReplyKeyboardMarkup([
        ["اطلب عالسريع 🔥"],
        ["لا بدي عدل 😐", "التواصل مع الدعم 🎧"],
        ["من نحن 🏢", "أسئلة متكررة ❓"]
    ], resize_keyboard=True)

    await update.message.reply_text(
        "📋 تمت العودة إلى القائمة الرئيسية.",
        reply_markup=reply_markup
    )
    return MAIN_MENU




async def handle_order_notes(update: Update, context: CallbackContext) -> int:
    notes = update.message.text.strip()

    # ✅ إذا اختار القائمة الرئيسية، نحذفه من السياق ونعيده
    if notes == "القائمة الرئيسية 🪧":
        context.user_data.pop("orders", None)
        context.user_data.pop("temporary_total_price", None)
        context.user_data.pop("order_notes", None)
        return await return_to_main_menu(update, context)

    # ✅ إذا اختار تخطي، لا نُسجل ملاحظات
    if notes == "تخطي ➡️":
        context.user_data['order_notes'] = "لا توجد ملاحظات."
    else:
        context.user_data['order_notes'] = notes or "لا توجد ملاحظات."

    # 👇 نضيف الزرين معًا
    reply_markup = ReplyKeyboardMarkup([
        ["نفس الموقع يلي عطيتكن ياه بالاول 🌝"],
        ["لاا أنا بمكان تاني 🌚"],
        ["القائمة الرئيسية 🪧"]
    ], resize_keyboard=True)

    await update.message.reply_text(
        "✅ تم \n\n"
        "ويينك هلا ؟",
        reply_markup=reply_markup
    )
    return ASK_ORDER_LOCATION


async def emergency_order_recovery(user_id, context):
    """استرجاع بيانات الطلب في حالات الطوارئ"""
    try:
        # محاولة استرجاع البيانات من قاعدة البيانات
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                # استرجاع آخر طلب للمستخدم
                await cursor.execute("""
                    SELECT r.name, c.cart_data 
                    FROM restaurants r
                    JOIN shopping_carts c ON 1=1
                    WHERE c.user_id = %s
                    ORDER BY c.updated_at DESC
                    LIMIT 1
                """, (user_id,))
                result = await cursor.fetchone()
                
                if result:
                    restaurant_name, cart_data = result
                    context.user_data['selected_restaurant'] = restaurant_name
                    
                    if cart_data:
                        if isinstance(cart_data, bytes):
                            cart_data = cart_data.decode("utf-8")
                        cart = json.loads(cart_data)
                        context.user_data['orders'] = cart
                        return True
        
        return False
    except Exception as e:
        logger.error(f"❌ خطأ في استرجاع بيانات الطلب في حالة الطوارئ: {e}")
        return False


async def ask_order_location(update: Update, context: CallbackContext) -> int:
    choice = update.message.text
    user_id = update.effective_user.id

    orders = context.user_data.get('orders', [])
    if not orders:
        orders = await get_cart_from_db(user_id)
        context.user_data['orders'] = orders

    selected_restaurant = context.user_data.get('selected_restaurant')
    if not selected_restaurant:
        try:
            state = await get_conversation_state(user_id)
            selected_restaurant = state.get('selected_restaurant')
            if selected_restaurant:
                context.user_data['selected_restaurant'] = selected_restaurant
        except Exception as e:
            logger.error(f"❌ خطأ في استرجاع اسم المطعم: {e}")

    if isinstance(orders, dict):
        orders = await fixed_orders_from_legacy_dict(orders)
        context.user_data["orders"] = orders

    if choice == "نفس الموقع يلي عطيتكن ياه بالاول 🌝":
        if not orders:
            logger.error(f"❌ لا توجد طلبات للمستخدم {user_id}")
            await update.message.reply_text("❌ حدث خطأ في استرجاع تفاصيل الطلب: لا توجد وجبات في السلة.")
            return MAIN_MENU

        if not selected_restaurant:
            logger.error(f"❌ لا يوجد مطعم محدد للمستخدم {user_id}")
            await update.message.reply_text("❌ حدث خطأ في استرجاع تفاصيل الطلب: لم يتم تحديد المطعم.")
            return MAIN_MENU

        total_price = sum(item.get("price", 0) for item in orders)
        context.user_data['temporary_total_price'] = total_price

        summary_lines = [
            f"- {item['name']} ({item['size']}) - {item['price']} ل.س"
            for item in orders
        ]
        summary_text = "\n".join(summary_lines)

        location_text = context.user_data.get("location_text")
        if location_text and " - " in location_text:
            area, details = location_text.split(" - ", 1)
            summary_text += f"\n\n🚚 رح نبعتلك طلبيتك عالسريع على:\n📍 {area}\n{details}"
        elif location_text:
            summary_text += f"\n\n🚚 رح نبعتلك طلبيتك عالسريع على:\n📍 {location_text}"

        notes = context.user_data.get("order_notes")
        if notes and notes.strip().lower() != "none":
            summary_text += f"\n📝 *ملاحظات:* {notes.strip()}"
        else:
            summary_text += f"\n📝 لا يوجد ملاحظات."

        reply_markup = ReplyKeyboardMarkup([
            ["يلا عالسريع 🔥"],
            ["لا ماني متأكد 😐"]
        ], resize_keyboard=True)

        await update.message.reply_text(
            f"📋 *ملخص الطلب:*\n{summary_text}\n\n"
            f"💰 *المجموع:* {total_price} ل.س\n\n"
            "صرنا جاهزين  منطلب ؟",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return CONFIRM_FINAL_ORDER

    elif choice == "لاا أنا بمكان تاني 🌚":
        await update.message.reply_text(
            "🗺️ ما اسم المنطقة أو الشارع الذي تسكن فيه؟ (مثلاً: الزراعة - شارع القلعة)",
            reply_markup=ReplyKeyboardMarkup([["عودة ➡️"]], resize_keyboard=True)
        )
        return await ask_new_location(update, context)

    else:
        await update.message.reply_text("❌ يرجى اختيار أحد الخيارات.")
        return ASK_ORDER_LOCATION






async def ask_new_location(update: Update, context: CallbackContext) -> int:
    location_button = KeyboardButton("📍 إرسال موقعي", request_location=True)
    reply_markup = ReplyKeyboardMarkup([[location_button], ["عودة ⬅️"]], resize_keyboard=True)

    await update.message.reply_text(
        "وينك هلا يا حلو 🙉",
        reply_markup=reply_markup
    )
# ⏱️ تأخير بسيط لإضفاء لمسة طبيعية
    await asyncio.sleep(0.5)

    # 📨 الرسالة الثانية بعد ثانية
    await update.message.reply_text("اذا ماكنت مفعل رح تضطر تدور عموقع و اضغط عليه مطولا بعدين اختار اسفل الشاشة الخيار المستطيل إرسال 👇")

    await asyncio.sleep(0.5)

    # 📨 الرسالة الثانية بعد ثانية
    await update.message.reply_text("اسماع مني ونزل البرداية وشغل خدمة الموقع الجغرافي او GPS وبس ارسال موقعي 📍")

    await asyncio.sleep(0.5)

    # 📨 الرسالة الثانية بعد ثانية
    await update.message.reply_text("ما بدا شي 😄")

    return ASK_NEW_LOCATION_IMAGE






async def handle_new_location(update: Update, context: CallbackContext) -> int:
    location = update.message.location

    if location:
        latitude = location.latitude
        longitude = location.longitude

        context.user_data['temporary_location_coords'] = {
            'latitude': latitude,
            'longitude': longitude
        }

        reply_markup = ReplyKeyboardMarkup([["عودة ⬅️"]], resize_keyboard=True)
        await update.message.reply_text(
            "تمام 🐸",
            reply_markup=reply_markup
        )
        await update.message.reply_text(
            "🗺️ ما اسم المنطقة أو الشارع الذي تسكن فيه؟ (مثلاً: الزراعة - شارع القلعة)",
            reply_markup=ReplyKeyboardMarkup([["عودة ⬅️"]], resize_keyboard=True)
        )
        return ASK_NEW_AREA_NAME


    await update.message.reply_text("❌ لم يتم استلام موقع صالح. يرجى استخدام الزر لإرسال موقعك.")
    return ASK_NEW_LOCATION_IMAGE





async def ask_new_area_name(update: Update, context: CallbackContext) -> int:
    # لا نستخدم text مباشرة لأنها قد لا تكون موجودة بعد إرسال الموقع
    if update.message.text and update.message.text == "عودة ➡️":
        return await ask_order_location(update, context)

    context.user_data['temporary_area_name'] = update.message.text.strip() if update.message.text else "غير محدد"

    await update.message.reply_text(
        "✏️ الآن، تخيّل أنك تحكي مع المطعم وتريد أن تحدد له مكانك تمامًا.\n"
        "اكتب وصف دقيق للموقع: طابق، أقرب معلم، بأي بناء ممكن يشوفك الدليفري 👇",
        reply_markup=ReplyKeyboardMarkup([["عودة ➡️"]], resize_keyboard=True)
    )
    return ASK_NEW_DETAILED_LOCATION




async def ask_new_detailed_location(update: Update, context: CallbackContext) -> int:
    if update.message.text == "عودة ➡️":
        return await ask_new_area_name(update, context)

    context.user_data['temporary_detailed_location'] = update.message.text.strip()

    # 🧾 تابع إلى عرض ملخص الطلب
    return await show_order_summary(update, context, is_new_location=True)







async def show_order_summary(update: Update, context: CallbackContext, is_new_location=False) -> int:
    orders = context.user_data.get("orders", [])

    if isinstance(orders, dict):
        # تحويل الطلبات القديمة من dict إلى list of dicts
        converted = []
        for name_size, count in orders.items():
            try:
                name, size = name_size.rsplit(" (", 1)
                size = size.rstrip(")")
            except:
                name, size = name_size, "default"

            price = context.user_data.get('temporary_total_price', 0) // sum(orders.values())
            for _ in range(count):
                converted.append({"name": name.strip(), "size": size.strip(), "price": price})
        orders = converted
        context.user_data["orders"] = orders

    if not orders:
        await update.message.reply_text("❌ لا توجد وجبات في سلتك حالياً.")
        return ORDER_MEAL

    total_price = sum(item['price'] for item in orders)
    context.user_data['temporary_total_price'] = total_price

    # إعداد ملخص الطلب
    summary_counter = defaultdict(int)
    for item in orders:
        label = f"{item['name']} ({item['size']})" if item['size'] != "default" else item['name']
        summary_counter[label] += 1
    summary_lines = [f"{count} × {label}" for label, count in summary_counter.items()]
    summary_text = "\n".join(summary_lines)

    # إعداد وصف الموقع
    if is_new_location:
        area = context.user_data.get("temporary_area_name", "غير محدد")
        details = context.user_data.get("temporary_detailed_location", "غير محدد")
        location_text = f"🚚 رح نبعتلك طلبيتك عالسريع على:\n📍 {area}\n{details}"
        
        # حفظ النص المؤقت للموقع للاستخدام لاحقاً
        context.user_data['temporary_location_text'] = f"{area} - {details}"
    else:
        location_text = "📍 الموقع الأساسي المسجل سابقاً"

    reply_markup = ReplyKeyboardMarkup([
        ["يلا عالسريع 🔥"],
        ["لا ماني متأكد 😐"]
    ], resize_keyboard=True)

    await update.message.reply_text(
        f"📋 *ملخص الطلب:*\n{summary_text}\n\n"
        f"{location_text}\n"
        f"💰 *المجموع:* {total_price} ل.س\n\n"
        "صرنا جاهزين  منطلب ؟",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    await context.bot.send_sticker(
        chat_id=update.effective_chat.id,
        sticker="CAACAgIAAxkBAAEBxwJoM2X4YRXyTB7SEfeJfmTibkBxsAAC4TMAAleOAAFIyLOz0lxKOb02BA"
    )

    return CONFIRM_FINAL_ORDER





async def handle_confirm_final_order(update: Update, context: CallbackContext) -> int:
    return await process_confirm_final_order(update, context)

async def process_confirm_final_order(update, context):
    choice = update.message.text
    user_id = update.effective_user.id

      # 🛡️ التحقق من الإدخال غير المتوقع
    valid_choices = ["يلا عالسريع 🔥", "لا ماني متأكد 😐"]
    if choice not in valid_choices:
        reply_markup = ReplyKeyboardMarkup([
            ["يلا عالسريع 🔥"],
            ["لا ماني متأكد 😐"]
        ], resize_keyboard=True)
        await update.message.reply_text(
            "حبيبي اختار من الخيارات يلي تحت 👇",
            reply_markup=reply_markup
        )
        return CONFIRM_FINAL_ORDER

    # 🛡️ حماية ضد الضغط المكرر أثناء التنفيذ
    if context.user_data.get("is_order_processing"):
        await update.message.reply_text("⏳ يتم تنفيذ طلبك حالياً، انتظر لحظة...")
        return MAIN_MENU

    # 🔒 حماية زمنية: منع الطلب المتكرر خلال 10 ثواني
    last_order_time = context.user_data.get("last_order_timestamp")
    now = datetime.now()
    if last_order_time and (now - last_order_time).total_seconds() < 10:
        await update.message.reply_text("⏳ لقد أرسلت طلباً للتو. انتظر قليلاً قبل إرسال طلب جديد.")
        return MAIN_MENU

    # تفعيل القفل المؤقت
    context.user_data["is_order_processing"] = True

    if choice == "يلا عالسريع 🔥":
        user_state = await get_conversation_state(user_id)
        cart = await get_cart_from_db(user_id) or []

        if not cart:
            await update.message.reply_text("❌ لا توجد وجبات في سلتك حالياً.")
            context.user_data.pop("is_order_processing", None)
            return MAIN_MENU

        order_id = str(uuid.uuid4())
        logger.info(f"🔍 user_id: {user_id}, context.user_data: {context.user_data}")

        # جلب الاسم ورقم الهاتف
        name = phone = None
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT name, phone FROM user_data WHERE user_id = %s", (user_id,))
                    result = await cursor.fetchone()
                    if result:
                        name, phone = result
        except Exception as e:
            logger.error(f"❌ خطأ أثناء جلب الاسم من قاعدة البيانات: {e}")

        name = name or user_state.get("name") or context.user_data.get("name") or "غير متوفر"
        phone = phone or user_state.get("phone") or context.user_data.get("phone") or "غير متوفر"

        # جلب الموقع من قاعدة البيانات
        db_location_text = None
        db_coords = None
        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT location_text, latitude, longitude FROM user_data WHERE user_id = %s", (user_id,))
                    result = await cursor.fetchone()
                    if result:
                        db_location_text, lat, lon = result
                        if lat and lon:
                            db_coords = {"latitude": lat, "longitude": lon}
        except Exception as e:
            logger.error(f"❌ خطأ أثناء جلب الموقع من قاعدة البيانات: {e}")

        db_area_name = db_detailed_location = None
        if db_location_text:
            if " - " in db_location_text:
                db_area_name, db_detailed_location = db_location_text.split(" - ", 1)
            else:
                db_detailed_location = db_location_text

        area_name = context.user_data.get("temporary_area_name") or user_state.get("temporary_area_name") or user_state.get("area_name") or db_area_name
        detailed_location = context.user_data.get("temporary_detailed_location") or user_state.get("temporary_detailed_location") or user_state.get("detailed_location") or db_detailed_location
        location_coords = context.user_data.get("temporary_location_coords") or user_state.get("temporary_location_coords") or user_state.get("location_coords") or db_coords

        location_parts = []
        if area_name:
            location_parts.append(area_name.strip())
        if detailed_location:
            location_parts.append(detailed_location.strip())
        location_text = " - ".join(location_parts) if location_parts else "الموقع غير محدد"

        logger.info(f"📍 Final location: {location_text}")
        logger.info(f"👤 Final name: {name} | 📱 {phone}")

        selected_restaurant = context.user_data.get("selected_restaurant")
        if not selected_restaurant:
            await update.message.reply_text("❌ لم يتم تحديد المطعم.")
            context.user_data.pop("is_order_processing", None)
            return MAIN_MENU

        try:
            async with get_db_connection() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("""
                        SELECT r.id, r.channel, c.id
                        FROM restaurants r
                        JOIN cities c ON r.city_id = c.id
                        WHERE r.name = %s
                    """, (selected_restaurant,))
                    result = await cursor.fetchone()
                    if not result:
                        await update.message.reply_text("❌ لم يتم العثور على المطعم.")
                        context.user_data.pop("is_order_processing", None)
                        return MAIN_MENU

                    restaurant_id, restaurant_channel, city_id = result

                    await asyncio.sleep(0.2)
                    order_number = await get_next_order_number(restaurant_id)

                    await cursor.execute("""
                        INSERT INTO user_orders (order_id, user_id, restaurant_id, city_id, order_number)
                        VALUES (%s, %s, %s, %s, %s)
                    """, (order_id, user_id, restaurant_id, city_id, order_number))

                    logger.info(f"📦 بيانات الإدخال: order_id={order_id}, user_id={user_id}, restaurant_id={restaurant_id}, city_id={city_id}")

                await conn.commit()
                logger.info("✅ تم تنفيذ commit بنجاح.")
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT * FROM user_orders WHERE order_id = %s", (order_id,))
                    row = await cursor.fetchone()
                    logger.info(f"🔍 تحقق من الإدخال: {row}")


            # تجهيز الطلب
            summary_counter = defaultdict(int)
            for item in cart:
                key = (item["name"], item["size"], item["price"])
                summary_counter[key] += 1

            items_for_message = []
            for (name_, size, price), quantity in summary_counter.items():
                label = f"{name_} ({size})" if size != "default" else name_
                items_for_message.append({"name": label, "quantity": quantity, "price": price})

            total_price = sum(item["price"] * item["quantity"] for item in items_for_message)
            notes = user_state.get("order_notes") or context.user_data.get("order_notes", "لا توجد ملاحظات.")

            order_text = create_new_order_message(
                order_id=order_id,
                order_number=order_number,
                user_name=name,
                phone=phone,
                address=location_text,
                items=items_for_message,
                total_price=total_price,
                notes=notes
            )

            if location_coords:
                await context.bot.send_location(
                    chat_id=restaurant_channel,
                    latitude=location_coords["latitude"],
                    longitude=location_coords["longitude"]
                )

            await send_message_with_retry(context.bot, restaurant_channel, text=order_text, parse_mode="Markdown")

            context.user_data["order_data"] = {
                "order_id": order_id,
                "order_number": order_number,
                "selected_restaurant": selected_restaurant,
                "channel_id": restaurant_channel,
                "timestamp": now,
                "total_price": total_price
            }

            # 🧼 تحديث توقيت الطلب الأخير
            context.user_data["last_order_timestamp"] = now
            await delete_cart_from_db(user_id)

            reply_markup = ReplyKeyboardMarkup([
                ["إلغاء ❌ بدي عدل"],
                ["تأخرو عليي ما بعتولي انن بلشو 🫤"]
            ], resize_keyboard=True)

            await update.message.reply_text(
                f"✅ تم إرسال طلبك بنجاح!\n\n"
                f"📋 رقم الطلب: {order_number}\n"
                f"🍽️ المطعم: {selected_restaurant}\n"
                f"💰 المجموع: {total_price} ل.س\n\n"
                f"رح يبعتلك الكاشير بس يبلشو 😉",
                reply_markup=reply_markup
            )

            await context.bot.send_sticker(
                chat_id=update.effective_chat.id,
                sticker="CAACAgIAAxkBAAEBxnFoMQZFcg7tO0yexYxhUK4JLJAc0gACZDQAAqVkGUp0aoPgoYfAATYE"
            )

            context.user_data.pop("is_order_processing", None)
            return MAIN_MENU

        except Exception as e:
            logger.error(f"❌ خطأ أثناء تأكيد الطلب: {e}", exc_info=True)
            await update.message.reply_text("❌ حدث خطأ أثناء تأكيد الطلب. حاول لاحقاً.")
            context.user_data.pop("is_order_processing", None)
            return MAIN_MENU

    elif choice == "لا ماني متأكد 😐":
        await update.message.reply_text(
            "ولا يهمك بس تأكد تاني مرة ☺️",
            reply_markup=ReplyKeyboardMarkup([
                ["اطلب عالسريع 🔥"],
                ["لا بدي عدل 😐", "التواصل مع الدعم 🎧"],
                ["من نحن 🏢", "أسئلة متكررة ❓"]
            ], resize_keyboard=True)
        )
        context.user_data.pop("is_order_processing", None)
        return MAIN_MENU
        

    else:
        await update.message.reply_text("❌ يرجى اختيار أحد الخيارات المتاحة.")
        context.user_data.pop("is_order_processing", None)
        return CONFIRM_FINAL_ORDER


async def get_last_order(user_id):
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    SELECT o.order_id, o.order_number, o.timestamp, o.restaurant_id, r.name
                    FROM user_orders o
                    JOIN restaurants r ON o.restaurant_id = r.id
                    WHERE o.user_id = %s
                    ORDER BY o.timestamp DESC
                    LIMIT 1
                """, (user_id,))
                row = await cursor.fetchone()
                if row:
                    return {
                        "order_id": row[0],
                        "order_number": row[1],
                        "timestamp": row[2],
                        "restaurant_id": row[3],
                        "restaurant_name": row[4]
                    }

    except Exception as e:
        logger.error(f"❌ خطأ في get_last_order: {e}")
    return None




async def handle_cashier_interaction(update: Update, context: CallbackContext) -> None:
    logger.info("🚨 دخلنا فعليًا دالة handle_cashier_interaction")

    """📩 يلتقط رد الكاشير من القناة ويحدد المستخدم صاحب الطلب لإرسال الإشعار إليه"""

    channel_post = update.channel_post
    if not channel_post or not channel_post.text:
        return

    text = channel_post.text
    logger.info(f"📩 استلمنا رسالة جديدة من القناة: {text}")

    # ✅ استخراج معرف الطلب باستخدام الدالة الموحدة
    order_id = extract_order_id(text)
    if not order_id:
        logger.warning("⚠️ لم يتم العثور على معرف الطلب في الرسالة!")
        return

    logger.info(f"🔍 البحث عن الطلب ID: {order_id}")

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT user_id FROM user_orders WHERE order_id = %s", (order_id,))
                user_result = await cursor.fetchone()

        if not user_result:
            logger.warning(f"⚠️ لم يتم العثور على مستخدم لهذا الطلب: {order_id}")
            return

        user_id = user_result[0]
        logger.info(f"📩 سيتم إرسال رسالة إلى المستخدم: {user_id}")

        # ✅ حالة: رفض الطلب
        if "تم رفض الطلب" in text:
            message_text = (
                "❌ *منعتذر، ما انقبل طلبك.*\n\n"
                "السبب: ممكن معلوماتك ما مكتملة أو منطقتك بعيدة عن المطعم كتير.\n"
                "فيك تعدل معلوماتك أو حاول من مطعم تاني.\n\n"
                "🔥 إذا حسيت في شي غلط، اختار *التواصل مع الدعم 🎧* من القائمة وبيعالجولك وضعك عالسريع.\n\n"
                f"📌 *معرف الطلب:* `{order_id}`"
            )
            await context.bot.send_sticker(
                chat_id=user_id,
                sticker="CAACAgIAAxkBAAEBxxFoM2f1BDjNy-9ivZQXi9S_YqTLaAACSDsAAhNy-UgXWLa5FO4pTzYE"
            )
            reply_markup = ReplyKeyboardMarkup([
                ["اطلب عالسريع 🔥"],
                ["لا بدي عدل 😐", "التواصل مع الدعم 🎧"],
                ["من نحن 🏢", "أسئلة متكررة ❓"]
            ], resize_keyboard=True)

            await context.bot.send_message(
                chat_id=user_id,
                text=message_text,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )

        # ✅ حالة: قبول الطلب أو جاري تحضيره
        elif "تم قبول الطلب" in text or "جاري تحضير الطلب" in text:
            # استخراج رقم الطلب ووقت التوصيل من النص باستخدام الدالة الموحدة
            order_number = extract_order_number(text)
            match_time = re.search(r"⏱️ وقت التوصيل المتوقع:\s*(\d+)\s*دقيقة", text)
        
            delivery_time = match_time.group(1) if match_time else "غير محدد"
        
            message_text = (
                "بسلم عليك المطعم 😄\n"
                "وبقلك بلشنا بطلبك عالسريع 🔥\n\n"
                f"🔢 رقم طلبك: {order_number}\n"
                f"⏱️ وقت التوصيل المتوقع: {delivery_time} دقيقة\n\n"
                "رح نبعتلك مين بدو يوصلك ياه لعندك 🚴‍♂️ بس يجهز 🔥\n\n"
                f"📌 *معرف الطلب:* `{order_id}`"
            )
            
            reply_markup = ReplyKeyboardMarkup([
                ["وصل طلبي شكرا لكم 🙏"],
                ["إلغاء الطلب بسبب مشكلة 🫢"]
            ], resize_keyboard=True)

            await context.bot.send_message(
                chat_id=user_id,
                text=message_text,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )
            await asyncio.sleep(0.2)  # ⏱️ تأخير بسيط لتحسين ترتيب العرض

            await context.bot.send_sticker(
                chat_id=user_id,
                sticker="CAACAgIAAxkBAAEBxwtoM2b-lusvTTS2gHaC6p567Ri8QAAC6TkAAquXoElIPA20liWcHzYE"
            )

        # ✅ حالة: الطلب جاهز للتوصيل
        elif "الطلب أصبح جاهزًا للتوصيل" in text:
            # 🔍 استخراج اسم ورقم الدليفري
            match_name = re.search(r"الدليفري:\s*(.*?)\s*\(", text)
            match_phone = re.search(r"\((\d+)\)", text)
        
            delivery_name = match_name.group(1) if match_name else "غير معروف"
            delivery_phone = match_phone.group(1) if match_phone else "لا يوجد رقم"
        
            message_text = (
                "🚚 *طلبك جاي عالسريع 🔥*\n\n"
                f"مع *{delivery_name}*\n"
                f"وهي رقمو 📞 `{delivery_phone}`\n\n"
                "هلا طلع من المطعم وجاي لعندك خليك بمكانك جاهز لتلفونو 🤙\n"
                "عطيناك رقمو اذا حبيت تسأل عن طلبك لسبب ضروري.\n"
                "حاول ماتحكيه وهو عم يسوق ممكن تعرضو للخطر 🫣\n\n"
                f"📌 *معرف الطلب:* `{order_id}`"
            )
            await context.bot.send_sticker(
                chat_id=user_id,
                sticker="CAACAgIAAxkBAAEBxw5oM2c2g216QRpeJjVncTYMihrQswACdhEAAsMAASlJLbkjGWa6Dog2BA"
            )
            await context.bot.send_message(
                chat_id=user_id,
                text=message_text,
                parse_mode="Markdown"
            )

        # ✅ حالة أخرى (تحديث عام)
        else:
            message_text = f"🍽️ *تحديث عن طلبك!*\n\n{text}"
            reply_markup = ReplyKeyboardMarkup([
                ["وصل طلبي شكرا لكم 🙏"],
                ["إلغاء الطلب بسبب مشكلة 🫢"]
            ], resize_keyboard=True)

            await context.bot.send_message(
                chat_id=user_id,
                text=message_text,
                parse_mode="Markdown",
                reply_markup=reply_markup
            )

    except Exception as e:
        logger.error(f"❌ خطأ أثناء معالجة تفاعل الكاشير: {e}")







async def handle_order_cancellation(update: Update, context: CallbackContext) -> int:
    order_data = context.user_data.get("order_data")
    if not order_data:
        await update.message.reply_text("❌ لا يمكن العثور على تفاصيل الطلب.")
        return MAIN_MENU

    reply_markup = ReplyKeyboardMarkup([
        ["اي اي متاكد 🥱"],
        ["معلش رجعني 🙃"]
    ], resize_keyboard=True)

    await update.message.reply_text(
        "متاكد ؟ 🫤",
        reply_markup=reply_markup
    )
    await context.bot.send_sticker(
        chat_id=update.effective_chat.id,
        sticker="CAACAgIAAxkBAAEBxxRoM2gxP-n-YZEzCpwaLn85iOY6FAAChS4AAgnbcEtYq7na6YNylzYE"
    )


    return CANCEL_ORDER_OPTIONS





async def handle_confirm_cancellation(update: Update, context: CallbackContext) -> int:
    choice = update.message.text
    order_data = context.user_data.get("order_data")

    if not order_data:
        await update.message.reply_text("❌ لم يتم العثور على تفاصيل الطلب. يمكنك البدء بطلب جديد.")
        return MAIN_MENU

    if choice == "اي اي متاكد 🥱":
        now = datetime.now()
    
        # ✅ سجل وقت الإلغاء مع تنظيف الإدخالات الأقدم من ساعة
        if "cancel_history" not in context.user_data:
            context.user_data["cancel_history"] = []
    
        context.user_data["cancel_history"] = [
            t for t in context.user_data["cancel_history"]
            if (now - t).total_seconds() <= 3600
        ]
        context.user_data["cancel_history"].append(now)
    
        # ✅ استخراج بيانات الطلب للإبلاغ
        order_id = order_data.get("order_id", "غير معروف")
        order_number = order_data.get("order_number", "؟")
        user_name = update.effective_user.full_name
        restaurant_channel_id = order_data.get("channel_id")
    
        if restaurant_channel_id:
            await context.bot.send_message(
                chat_id=restaurant_channel_id,
                text=(
                    f"🚫 تم إلغاء الطلب رقم {order_number} من قبل الزبون.\n"
                    f"📌 معرف الطلب: `{order_id}`\n"
                    f"👤 الزبون: {user_name}\n"
                    f"📍 السبب: تأخر بالموافقة.\n\n"
                    f"يرجى الانتباه في المرات القادمة.\n"
                    f"نحن سنعتذر منه وندعوه للطلب لاحقًا بسبب ضغط الطلبات."
                ),
                parse_mode="Markdown"
            )
        else:
            logger.warning("⚠️ لم يتم تحديد channel_id لإرسال رسالة الإلغاء.")
    
        # حذف بيانات الطلب
        for key in ['orders', 'selected_restaurant', 'order_data']:
            context.user_data.pop(key, None)
    
        reply_markup = ReplyKeyboardMarkup([
            ["اطلب عالسريع 🔥"],
            ["لا بدي عدل 😐", "التواصل مع الدعم 🎧"],
            ["من نحن 🏢", "أسئلة متكررة ❓"]
        ], resize_keyboard=True)
        await update.message.reply_text(
            "تم إلغاء طلبك، بتمنى منك تنتبه أكتر تاني مرة قبل التأكيد ☺️",
            reply_markup=reply_markup
        )
        return MAIN_MENU

    elif choice == "معلش رجعني 🙃":
        reply_markup = ReplyKeyboardMarkup([
            ["إلغاء ❌ بدي عدل"],
            ["تأخرو عليي ما بعتولي انن بلشو 🫤"]
        ], resize_keyboard=True)
        await update.message.reply_text(
            "رجعنا 😌",
            reply_markup=reply_markup
        )
        return CANCEL_ORDER_OPTIONS

    else:
        await update.message.reply_text("❌ يرجى اختيار أحد الخيارات المتاحة.")
        return CANCEL_ORDER_OPTIONS





async def handle_cancellation_reason(update: Update, context: CallbackContext) -> int:
    if context.user_data.get("cancel_step") != "awaiting_cancellation_reason":
        await update.message.reply_text("❌ لم يتم تحديد طلب لإلغائه.")
        return MAIN_MENU

    # 🟢 احفظ سبب الإلغاء
    cancel_reason = update.message.text
    context.user_data["cancel_reason"] = cancel_reason
    context.user_data["cancel_step"] = None

    order_data = context.user_data.get("order_data")
    if not order_data:
        await update.message.reply_text("❌ لم يتم العثور على تفاصيل الطلب.")
        return MAIN_MENU

    order_number = order_data.get("order_number", "غير متوفر")
    order_id = order_data.get("order_id", None)
    selected_restaurant = order_data.get("selected_restaurant", "غير متوفر")

    cursor = db_conn.cursor()

    # ✅ تأكد من وجود المطعم في جدول العدادات
    cursor.execute("SELECT last_order_number FROM restaurant_order_counter WHERE restaurant = ?", (selected_restaurant,))
    if not cursor.fetchone():
        cursor.execute("INSERT INTO restaurant_order_counter (restaurant, last_order_number) VALUES (?, ?)", (selected_restaurant, 0))
        db_conn.commit()

    # ✅ جلب قناة المطعم
    cursor.execute("SELECT channel FROM restaurants WHERE name = ?", (selected_restaurant,))
    result = cursor.fetchone()
    restaurant_channel = result[0] if result else None

    if not restaurant_channel:
        await update.message.reply_text("❌ لم يتم العثور على القناة المرتبطة بالمطعم.")
        return MAIN_MENU

    try:
        cancellation_text = (
            f"🚫 تم إلغاء الطلب رقم {order_number}\n"
            f"📌 معرف الطلب: `{order_id}`\n"
            f"📍 السبب: {cancel_reason}"
        )
        await context.bot.send_message(
            chat_id=restaurant_channel,
            text=cancellation_text,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"❌ فشل إرسال رسالة الإلغاء إلى القناة: {e}")

    keys_to_remove = ['orders', 'selected_restaurant', 'order_data']
    for key in keys_to_remove:
        context.user_data.pop(key, None)

    reply_markup = ReplyKeyboardMarkup([
        ["اطلب عالسريع 🔥"],
        ["لا بدي عدل 😐", "التواصل مع الدعم 🎧"],
        ["من نحن 🏢", "أسئلة متكررة ❓"]
    ], resize_keyboard=True)
    await update.message.reply_text(
        "ألغينالك الطلب ، اذا في مشكلة حكينا 🫠",
        reply_markup=reply_markup
    )
    return MAIN_MENU






async def handle_no_confirmation(update: Update, context: CallbackContext) -> int:
    # التحقق من بيانات الطلب
    order_data = context.user_data.get("order_data", None)
    if not order_data:
        await update.message.reply_text("❌ لا يمكن العثور على تفاصيل الطلب. يرجى التأكد من أنك قمت بتأكيد الطلب مسبقًا.")
        return MAIN_MENU

    # التحقق من مرور الوقت منذ تأكيد الطلب
    order_timestamp = order_data.get("timestamp", datetime.now())
    time_elapsed = (datetime.now() - order_timestamp).total_seconds() / 60  # بالدقائق

    if time_elapsed < 5:
        reply_markup = ReplyKeyboardMarkup([
            ["إلغاء ❌ بدي عدل"],
            ["تأخرو عليي ما بعتولي انن بلشو 🫤"]
        ], resize_keyboard=True)
        await update.message.reply_text(
            "حبيبي بدك تطول بالك 5 دقايق عالأقل 🤧\n"
            "ممكن في زحمة طلبات قبلك 🫨",
            reply_markup=reply_markup
        )
        
        return MAIN_MENU

    # إذا مر أكثر من 5 دقائق
    reply_markup = ReplyKeyboardMarkup([
        ["ذكرلي المطعم بطلبي 🙋"],
        ["تأخرو كتير إلغاء عالسريع 😡"],
    ], resize_keyboard=True)
    await update.message.reply_text(
        "شكله المطعم مشغول، نحن عملنا يلي علينا وطلبك وصل عالسريع 🔥\n"
        "بتحب فيك تنكشو للكاشير أو فيك تشوف مطعم غيره 😊",
        reply_markup=reply_markup
    )
    await context.bot.send_sticker(
        chat_id=update.effective_chat.id,
        sticker="CAACAgIAAxkBAAEBxxhoM2v_6q5ji4WFJqVQn9zuBPsMnwACmkYAAuLPsUgK6Mn07keZgjYE"
    )
    return CANCEL_ORDER_OPTIONS





async def handle_reminder(update: Update, context: CallbackContext) -> int:
    order_data = context.user_data.get("order_data")
    if not order_data:
        await update.message.reply_text("❌ لا يمكن العثور على تفاصيل الطلب.")
        return MAIN_MENU

    if context.user_data.get("reminder_sent", False):
        await update.message.reply_text("فيك تذكر مرة بس 😔")
        return CANCEL_ORDER_OPTIONS


    context.user_data["reminder_sent"] = True

    order_id = order_data.get("order_id")
    order_number = order_data.get("order_number")
    selected_restaurant = order_data.get("selected_restaurant")

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                # ✅ جلب restaurant_id أولاً
                await cursor.execute("SELECT id, channel FROM restaurants WHERE name = %s", (selected_restaurant,))
                result = await cursor.fetchone()

                if not result:
                    await update.message.reply_text("❌ لم يتم العثور على المطعم.")
                    return MAIN_MENU

                restaurant_id, restaurant_channel = result

                # ✅ تأكد من وجود المطعم في جدول العدادات
                await cursor.execute(
                    "SELECT last_order_number FROM restaurant_order_counter WHERE restaurant_id = %s",
                    (restaurant_id,)
                )
                if not await cursor.fetchone():
                    await cursor.execute(
                        "INSERT INTO restaurant_order_counter (restaurant_id, last_order_number) VALUES (%s, %s)",
                        (restaurant_id, 0)
                    )
                    await conn.commit()

        if restaurant_channel and order_number:
            await context.bot.send_message(
                chat_id=restaurant_channel,
                text=f"🔔 تذكير من الزبون: الطلب رقم {order_number} قيد الانتظار. نرجو الاستعجال في الموافقة على الطلب فورا !! 🙏.",
            )
            await update.message.reply_text("حكينالك ياه لازم يستحي ع دمه 🤨")
        else:
            await update.message.reply_text("❌ لم يتم العثور على قناة المطعم أو رقم الطلب.")

    except Exception as e:
        logger.error(f"❌ خطأ أثناء تنفيذ الطلب: {e}")
        await update.message.reply_text("❌ حدث خطأ أثناء تنفيذ الطلب. حاول مرة أخرى لاحقاً.")

    return CANCEL_ORDER_OPTIONS






async def handle_final_cancellation(update: Update, context: CallbackContext) -> int:
    choice = update.message.text
    user_id = update.effective_user.id

       # 🛡️ التحقق من الإدخال غير المتوقع
    valid_choices = ["اي متاكد 🥱", "لا خلص منرجع ومننتظر 🥲"]
    if choice not in valid_choices:
        reply_markup = ReplyKeyboardMarkup([
            ["اي متاكد 🥱"],
            ["لا خلص منرجع ومننتظر 🥲"]
        ], resize_keyboard=True)
        await update.message.reply_text(
            "حبيبي اختار من الخيارات يلي تحت 👇",
            reply_markup=reply_markup
        )
        return CANCEL_ORDER_OPTIONS


    if choice == "اي متاكد 🥱":
        order_data = context.user_data.get("order_data")
        if not order_data:
            await update.message.reply_text("❌ لم يتم تحديد طلب لإلغائه.")
            # العودة إلى القائمة الرئيسية
            reply_markup = ReplyKeyboardMarkup([
                ["اطلب عالسريع 🔥"],
                ["لا بدي عدل 😐", "التواصل مع الدعم 🎧"],
                ["من نحن 🏢", "أسئلة متكررة ❓"]
            ], resize_keyboard=True)
            await update.message.reply_text("تم العودة إلى القائمة الرئيسية", reply_markup=reply_markup)
            return MAIN_MENU

        # باقي الكود كما هو...
        order_id = order_data.get("order_id", "غير معروف")
        order_number = order_data.get("order_number", "؟")
        restaurant_channel = order_data.get("channel_id")
        restaurant_id = order_data.get("restaurant_id")
        user_name = update.effective_user.first_name or "مستخدم"

        # نحذف البيانات من السياق
        context.user_data.pop("order_data", None)

        # محاولة جلب القناة من قاعدة البيانات إذا لم تكن موجودة
        if not restaurant_channel and restaurant_id:
            try:
                async with get_db_connection() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute("SELECT channel FROM restaurants WHERE id = %s", (restaurant_id,))
                        result = await cursor.fetchone()
                        if result:
                            restaurant_channel = result[0]
            except Exception as e:
                logger.error(f"❌ خطأ أثناء جلب قناة المطعم: {e}")

        # إرسال إشعار الإلغاء
        if restaurant_channel:
            cancellation_text = (
                f"🚫 تم إلغاء الطلب رقم {order_number} من قبل الزبون.\n"
                f"📌 معرف الطلب: `{order_id}`\n"
                f"👤 الزبون: {user_name}\n"
                f"📍 السبب: تأخر بالموافقة.\n\n"
                f"يرجى الانتباه في المرات القادمة.\n"
                f"نحن سنعتذر منه وندعوه للطلب لاحقًا بسبب ضغط الطلبات."
            )
            try:
                await context.bot.send_message(
                    chat_id=restaurant_channel,
                    text=cancellation_text,
                    parse_mode="Markdown"
                )
                await update.message.reply_text("ألغينالك الطلب ، اذا في مشكلة حكينا 🫠")
            except Exception as e:
                logger.error(f"❌ خطأ أثناء إرسال إشعار الإلغاء: {e}")
                await update.message.reply_text("❌ حدث خطأ أثناء إلغاء الطلب. يرجى المحاولة مرة أخرى.")
        else:
            await update.message.reply_text("❌ لم يتم العثور على قناة المطعم.")

        # العودة إلى القائمة الرئيسية
        reply_markup = ReplyKeyboardMarkup([
            ["اطلب عالسريع 🔥"],
            ["لا بدي عدل 😐", "التواصل مع الدعم 🎧"],
            ["من نحن 🏢", "أسئلة متكررة ❓"]
        ], resize_keyboard=True)
        await update.message.reply_text("تم العودة إلى القائمة الرئيسية", reply_markup=reply_markup)
        return MAIN_MENU

    elif choice == "لا خلص منرجع ومننتظر 🥲":
        reply_markup = ReplyKeyboardMarkup([
            ["ذكرلي المطعم بطلبي 🙋"],
            ["تأخرو كتير إلغاء عالسريع 😡"]
        ], resize_keyboard=True)
        await update.message.reply_text(
            "ماشي منرجع ومننطر، تقدر تنكش المطعم بأي وقت 😉",
            reply_markup=reply_markup
        )
        return CANCEL_ORDER_OPTIONS


    else:
        await update.message.reply_text("❌ يرجى اختيار أحد الخيارات المتاحة.")
        return CANCEL_ORDER_OPTIONS








async def handle_order_issue(update: Update, context: CallbackContext) -> int:
    warning_message = (
        "🔒 *نحن نهتم بحمايتك أولًا:*\n\n"
        "إذا شعرت أن الطلب تأخر أكثر من اللازم أو لم يتم الرد عليك، فاعلم أن الخطأ قد يكون من المطعم، "
        "ونحن هنا لضمان حقك بالكامل. فور تأكيدك للطلب، يتم إرساله مباشرة إلى المطعم المعني بدون أي تدخل منك، "
        "وأي تأخير بعد ذلك هو مسؤولية المطعم وحده.\n\n"
        "🔍 في حال قررت الإلغاء، يتم توليد تقرير تلقائي يحتوي على كل التفاصيل: رقم الطلب، وقت الطلب، وقت الإلغاء، والمطعم المطلوب.\n\n"
        "⚠️ *تنبيه هام:* في حال تبين أن الإلغاء تم بهدف العبث أو إزعاج النظام، فسنضطر آسفين لحظرك من استخدام البوت بشكل دائم، "
        "مع إمكانية اتخاذ إجراءات قانونية وفقًا للقوانين المعمول بها.\n\n"
        "🫶 هدفنا دائمًا هو تقديم خدمة محترمة وآمنة لك، ونثق أنك معنا لتحقيق ذلك. شكرًا لتعاونك وتفهمك ❤️"
    )


    reply_markup = ReplyKeyboardMarkup([
        ["تذكير المطعم بطلبي 👋", "كم يتبقى لطلبي"],
        ["إلغاء وإرسال تقرير ❌", "العودة والانتظار 🙃"]
    ], resize_keyboard=True)

    await update.message.reply_text(
        warning_message,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )
    return CANCEL_ORDER_OPTIONS






async def handle_report_issue(update: Update, context: CallbackContext) -> int:
    now = datetime.now()
    order_data = context.user_data.get("order_data")

    if not order_data:
        await update.message.reply_text("❌ لا يمكن العثور على تفاصيل الطلب. يرجى التأكد من أنك قمت بتأكيد الطلب مسبقًا.")
        return MAIN_MENU

    order_timestamp = order_data.get("timestamp", now)
    time_elapsed = (now - order_timestamp).total_seconds() / 60

    if time_elapsed < 29:
        remaining_time = int(30 - time_elapsed)
        await update.message.reply_text(
            f"⏳ لا يمكنك إرسال تقرير إلا بعد مرور نصف ساعة على الطلب.\n"
            f"يرجى المحاولة بعد {remaining_time} دقيقة."
        )
        return CANCEL_ORDER_OPTIONS

    reply_markup = ReplyKeyboardMarkup([
        ["إلغاء وإنشاء تقرير ❌"],
        ["منرجع ومننطر 🙃"]
    ], resize_keyboard=True)
    await update.message.reply_text(
        "⚠️ سيتم إرسال تقرير بكل تفاصيل الطلب:\n\n"
        "1️⃣ رقم الطلب.\n"
        "2️⃣ وقت الطلب ووقت الإلغاء.\n"
        "3️⃣ تفاصيل الطلب وبيانات الزبون.\n\n"
        "📄 سيتم إرسال التقرير إلى فريق الدعم لاتخاذ الإجراءات اللازمة.\n\n"
        "اختر أحد الخيارات:",
        reply_markup=reply_markup
    )
    return CANCEL_ORDER_OPTIONS





async def handle_report_cancellation(update: Update, context: CallbackContext) -> int:
    # ❗️تحويل المستخدم لسؤال السبب قبل الإرسال
    return await ask_report_reason(update, context)


async def ask_report_reason(update: Update, context: CallbackContext) -> int:
    text = update.message.text.strip()

    if text == "عودة ➡️":
        return await handle_return_and_wait(update, context)

    context.user_data["report_reason"] = text
    context.user_data["cancel_step"] = None

    return await process_report_cancellation(update, context)


async def process_report_cancellation(update: Update, context: CallbackContext) -> int:
    order_data = context.user_data.get("order_data")
    if not order_data:
        await update.message.reply_text("❌ لا يمكن العثور على تفاصيل الطلب.")
        return MAIN_MENU

    reason = context.user_data.get("report_reason", "لم يُذكر السبب.")

    user_id = update.effective_user.id
    name = context.user_data.get("name", "غير متوفر")
    phone = context.user_data.get("phone", "غير متوفر")
    order_number = order_data.get("order_number", "غير متوفر")
    order_id = order_data.get("order_id", "غير متوفر")
    selected_restaurant = order_data.get("selected_restaurant", "غير متوفر")
    order_time = order_data.get("timestamp", datetime.now())
    cancel_time = datetime.now()

    # 📨 إرسال تقرير إلى قناة الإدارة
    report_message = (
        f"📝 تقرير إلغاء طلب:\n\n"
        f"👤 الزبون:\n"
        f" - الاسم: {name}\n"
        f" - رقم الهاتف: {phone}\n"
        f" - رقم التلغرام: {user_id}\n\n"
        f"🛒 الطلب:\n"
        f"رقم الطلب: {order_number}\n"
        f"معرف الطلب: {order_id}\n"
        f"المطعم: {selected_restaurant}\n"
        f"⏱️ وقت الطلب: {order_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"⏱️ وقت الإلغاء: {cancel_time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"💬 سبب الإلغاء:\n{reason}"
    )

    await send_message_with_retry(context.bot, "@reports_cancel", text=report_message)

    # 📣 إشعار قناة المطعم
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT channel FROM restaurants WHERE name = %s", (selected_restaurant,))
                result = await cursor.fetchone()
                restaurant_channel = result[0] if result else None
    except Exception as e:
        logger.error(f"❌ فشل في جلب قناة المطعم: {e}")
        restaurant_channel = None

    if restaurant_channel:
        await context.bot.send_message(
            chat_id=restaurant_channel,
            text=(
                f"🚫 تم إلغاء الطلب رقم: {order_number}\n"
                f"📌 معرف الطلب: `{order_id}`\n"
                f"📍 السبب المذكور من الزبون:\n{reason}\n\n"
                "📄 تم إنشاء تقرير وسنراجع الملاحظات مع الزبون.\n"
                "📞 يمكنكم التواصل معه مباشرة إذا لزم الأمر."
            ),
            parse_mode="Markdown"
        )


    # 🧹 حذف البيانات المؤقتة المرتبطة بالطلب
    for key in ["order_data", "orders", "selected_restaurant", "report_reason"]:
        context.user_data.pop(key, None)

    # ✅ عرض القائمة الرئيسية
    reply_markup = ReplyKeyboardMarkup([
        ["اطلب عالسريع 🔥"],
        ["لا بدي عدل 😐", "التواصل مع الدعم 🎧"],
        ["من نحن 🏢", "أسئلة متكررة ❓"]
    ], resize_keyboard=True)

    await update.message.reply_text(
        "✅ تم إلغاء طلبك وإرسال تقرير بالمشكلة. شكراً لتفهمك ❤️.",
        reply_markup=reply_markup
    )

    return MAIN_MENU






async def handle_return_and_wait(update: Update, context: CallbackContext) -> int:
    """
    إعادة عرض الخيارات "تذكير المطعم بطلبي 👋" و"إلغاء وإرسال تقرير ❌".
    """
    await update.message.reply_text(
        "👌 عدنا إلى الخيارات السابقة. اختر ما تريد:",
        reply_markup = ReplyKeyboardMarkup([
            ["تذكير المطعم بطلبي 👋", "كم يتبقى لطلبي"],
            ["إلغاء وإرسال تقرير ❌", "العودة والانتظار 🙃"]
        ], resize_keyboard=True)
    )
    return CANCEL_ORDER_OPTIONS





async def handle_reminder_order_request(update: Update, context: CallbackContext) -> int:
    """
    معالجة خيار 'تذكير المطعم بطلبي 👋' مع التأكد من إبقاء الخيارات الأخرى فعالة.
    """
    order_data = context.user_data.get("order_data")
    logging.info(f"user_data: {context.user_data}")

    if not order_data:
        await update.message.reply_text("❌ لا يمكن العثور على تفاصيل الطلب. يرجى التأكد من أنك قمت بتأكيد الطلب مسبقًا.")
        return CANCEL_ORDER_OPTIONS

    now = datetime.now()
    last_reminder_request_time = context.user_data.get("last_reminder_request_time")

    if last_reminder_request_time:
        time_elapsed = (now - last_reminder_request_time).total_seconds() / 60
        if time_elapsed < 15:
            await update.message.reply_text(
                f"❌ لا يمكنك تذكير المطعم إلا مرة كل 15 دقيقة. "
                f"يرجى المحاولة بعد {15 - int(time_elapsed)} دقيقة."
            )
            return CANCEL_ORDER_OPTIONS

    context.user_data["last_reminder_request_time"] = now

    order_number = order_data.get("order_number")
    selected_restaurant = order_data.get("selected_restaurant")
    restaurant_channel = None

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT channel FROM restaurants WHERE name = %s", (selected_restaurant,))
                result = await cursor.fetchone()
                restaurant_channel = result[0] if result else None
    except Exception as e:
        logging.error(f"❌ خطأ أثناء جلب قناة المطعم: {e}")
        await update.message.reply_text("❌ حدث خطأ أثناء التحقق من قناة المطعم.")
        return CANCEL_ORDER_OPTIONS

    if restaurant_channel:
        try:
            await context.bot.send_message(
                chat_id=restaurant_channel,
                text=f"🔔 تذكير من الزبون: الطلب رقم {order_number} قيد الانتظار. "
                     f"نرجو الاستعجال في التحضير 🙏."
            )
            await update.message.reply_text("وصل 😁ِ")
        except Exception as e:
            logging.error(f"❌ خطأ أثناء إرسال رسالة التذكير: {e}")
            await update.message.reply_text("❌ حدث خطأ أثناء إرسال التذكير. يرجى المحاولة مرة أخرى.")
    else:
        await update.message.reply_text("❌ لم يتم العثور على قناة المطعم.")
        logging.error(f"❌ القناة غير موجودة للمطعم: {selected_restaurant}")

    reply_markup = ReplyKeyboardMarkup([
        ["تذكير المطعم بطلبي 👋", "كم يتبقى لطلبي"],
        ["إلغاء وإرسال تقرير ❌", "العودة والانتظار 🙃"]
    ], resize_keyboard=True)

    await update.message.reply_text("🔄 اختر أحد الخيارات المتاحة:", reply_markup=reply_markup)
    return CANCEL_ORDER_OPTIONS






async def handle_back_and_wait(update: Update, context: CallbackContext) -> int:
    """
    معالجة خيار العودة والانتظار 🙃 وإعادة المستخدم إلى الخيارات الأساسية في الإلغاء.
    """
    reply_markup = ReplyKeyboardMarkup([
        ["وصل طلبي شكرا لكم 🙏"],
        ["إلغاء الطلب بسبب مشكلة 🫢"]
    ], resize_keyboard=True)

    await update.message.reply_text(
        "👌 عدنا إلى الخيارات الأساسية. اختر ما يناسبك:",
        reply_markup=reply_markup
    )
    return MAIN_MENU




async def ask_remaining_time(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    order_data = context.user_data.get("order_data")

    if not order_data:
        await update.message.reply_text("❌ لا يمكن العثور على تفاصيل الطلب.")
        return CANCEL_ORDER_OPTIONS

    order_number = order_data.get("order_number")
    selected_restaurant = order_data.get("selected_restaurant")

    success, error_message = await send_remaining_time_request_to_channel(context, user_id, order_number, selected_restaurant)

    if success:
        await update.message.reply_text("سألتلك ياهن قدي اسا بدو طلبك ناطر منن جواب 😁")
    else:
        await update.message.reply_text(error_message or "❌ حدث خطأ أثناء إرسال الطلب.")
    
    return CANCEL_ORDER_OPTIONS




# ✅ دالة مساعدة موحدة لإرسال طلب "كم يتبقى؟"
async def send_remaining_time_request_to_channel(context, user_id, order_number, selected_restaurant, reply_to=None):
    try:
        restaurant_channel = None

        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT channel FROM restaurants WHERE name = %s", (selected_restaurant,))
                result = await cursor.fetchone()
                restaurant_channel = result[0] if result else None

        if not restaurant_channel:
            return False, "❌ لم يتم العثور على قناة المطعم."

        message = await context.bot.send_message(
            chat_id=restaurant_channel,
            text=f"🔔 كم يتبقى لتحضير الطلب رقم {order_number}؟",
            reply_to_message_id=reply_to
        )

        context.bot_data[message.message_id] = {
            "user_id": user_id,
            "order_number": order_number,
            "selected_restaurant": selected_restaurant,
        }

        asyncio.create_task(remind_cashier_after_delay(context, message.message_id, restaurant_channel))
        return True, None

    except Exception as e:
        logging.error(f"❌ فشل في إرسال طلب 'كم يتبقى؟' إلى قناة المطعم {selected_restaurant}: {e}")
        return False, "❌ حدث خطأ أثناء إرسال الطلب إلى المطعم."





async def handle_remaining_time_for_order(update: Update, context: CallbackContext) -> int:
    # 1️⃣ إذا كانت الرسالة من القناة (رد الكاشير)
    if update.channel_post and update.channel_post.reply_to_message:
        channel_post = update.channel_post
        reply_to_message_id = channel_post.reply_to_message.message_id
        logging.info(f"Reply to message ID: {reply_to_message_id}")

        order_data = context.bot_data.get(reply_to_message_id)
        if not order_data:
            logging.warning(f"No order data found for reply_to_message_id: {reply_to_message_id}")
            return

        user_id = order_data["user_id"]
        order_number = order_data["order_number"]

        try:
            remaining_time = int(''.join(filter(str.isdigit, channel_post.text)))
            if remaining_time < 0 or remaining_time > 150:
                await channel_post.reply_text("❌ يرجى إدخال رقم بين 0 و150 دقيقة.")
                return
        except ValueError:
            await channel_post.reply_text("❌ يرجى الرد برقم صحيح فقط.")
            return

        await context.bot.send_message(
            chat_id=user_id,
            text=f"بسلم عليك الكاشير وبقلك باقي لطلبيتك {remaining_time} دقيقة، ما بطول حبيبي 😘"
        )
        logging.info(f"✅ Notified user {user_id} with remaining time {remaining_time} for order {order_number}.")
        return

    # 2️⃣ إذا كانت الرسالة من المستخدم في المحادثة
    if update.effective_user:  # تحقق من وجود effective_user قبل الوصول إلى id
        user_id = update.effective_user.id
        order_data = context.user_data.get("order_data", {})
        order_number = order_data.get("order_number")
        selected_restaurant = order_data.get("selected_restaurant")

        if not order_number or not selected_restaurant:
            await update.message.reply_text("❌ لا يمكن العثور على رقم الطلب. يرجى المحاولة مرة أخرى.")
            return MAIN_MENU

        # ✅ استخدام الدالة الموحدة للإرسال
        success, error_message = await send_remaining_time_request_to_channel(
            context=context,
            user_id=user_id,
            order_number=order_number,
            selected_restaurant=selected_restaurant
        )

        if success:
            await update.message.reply_text(" سألتلك ياهن قدي بدو طلبك، ناطر منن جواب 😁")
        else:
            await update.message.reply_text(error_message or "❌ حدث خطأ أثناء إرسال الطلب.")

        return CANCEL_ORDER_OPTIONS
    
    # إذا لم يكن هناك effective_user ولا channel_post.reply_to_message
    logging.warning("⚠️ تم استدعاء handle_remaining_time_for_order بدون effective_user أو channel_post.reply_to_message")
    return



async def remind_cashier_after_delay(context: CallbackContext, message_id: int, restaurant_channel: str) -> None:
    """تذكير المطعم تلقائيًا بعد 5 دقائق إذا لم يتم الرد."""
    await asyncio.sleep(300)  # الانتظار 5 دقائق
    order_data = context.bot_data.get(message_id)

    if order_data:  # إذا لم يتم الرد على الرسالة
        order_number = order_data.get("order_number")
        await context.bot.send_message(
            chat_id=restaurant_channel,
            text=f"⏰ تذكير: لم يتم الرد على طلب الزبون لمعرفة مدة التحضير للطلب رقم {order_number}. يرجى الرد."
        )







async def show_relevant_ads(update: Update, context: CallbackContext):
    user_id = update.effective_user.id
    cursor = db_conn.cursor()
    cursor.execute("SELECT city, province FROM user_data WHERE user_id = ?", (user_id,))
    user_info = cursor.fetchone()

    if not user_info:
        return

    city, province = user_info
    cursor.execute("""
    SELECT content FROM advertisements
    WHERE region_type = 'all'
       OR (region_type = 'city' AND region_name = ?)
       OR (region_type = 'province' AND region_name = ?)
    """, (city, province))
    ads = cursor.fetchall()

    for ad in ads:
        await update.message.reply_text(ad[0])


async def handle_order_received(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    for key in ['order_data', 'orders', 'selected_restaurant', 'temporary_total_price', 'order_notes']:
        context.user_data.pop(key, None)
    context.user_data['came_from_delivery'] = True

    order_info = await get_last_order(user_id)
    if not order_info:
        await update.message.reply_text("❌ لم نعثر على طلب سابق لتقييمه.")
        return MAIN_MENU

    context.user_data["order_data"] = {
        "order_id": order_info["order_id"],
        "order_number": order_info["order_number"],
        "restaurant_id": order_info["restaurant_id"],
        "restaurant_name": order_info["restaurant_name"]
    }

    context.user_data["temp_rating"] = None

    reply_markup = ReplyKeyboardMarkup([
        ["⭐", "⭐⭐", "⭐⭐⭐"],
        ["⭐⭐⭐⭐", "⭐⭐⭐⭐⭐"],
        ["حلو عني 😒"]
    ], resize_keyboard=True)

    await update.message.reply_text(
        f"✨ كيف كانت تجربتك مع مطعم {order_info['restaurant_name']}؟\n"
        "يرجى اختيار عدد النجوم للتقييم، أو اختر 'حلو عني 😒' لتخطي التقييم.",
        reply_markup=reply_markup
    )

    return ASK_RATING




async def handle_rating(update: Update, context: CallbackContext) -> int:
    text = update.message.text

    # إذا اختار المستخدم تخطي التقييم
    if text == "حلو عني 😒":
        reply_markup = ReplyKeyboardMarkup([
            ["اطلب عالسريع 🔥"],
            ["لا بدي عدل 😐", "التواصل مع الدعم 🎧"],
            ["من نحن 🏢", "أسئلة متكررة ❓"]
        ], resize_keyboard=True)
        await update.message.reply_text("ماشي 😔 رجعناك للقائمة الرئيسية.", reply_markup=reply_markup)
        return MAIN_MENU

    # تحويل الرموز إلى رقم
    rating_map = {"⭐": 1, "⭐⭐": 2, "⭐⭐⭐": 3, "⭐⭐⭐⭐": 4, "⭐⭐⭐⭐⭐": 5}
    rating = rating_map.get(text, 0)

    if rating == 0:
        await update.message.reply_text("❌ يرجى اختيار تقييم صحيح من القائمة.")
        return ASK_RATING

    # تخزين البيانات مؤقتًا
    order_data = context.user_data.get("order_data", {})
    context.user_data["pending_rating"] = {
        "order_id": order_data.get("order_id"),
        "order_number": order_data.get("order_number"),
        "restaurant_id": order_data.get("restaurant_id"),
        "stars": rating
    }

    # سؤال عن التعليق
    reply_markup = ReplyKeyboardMarkup([["حلو عني 😒"]], resize_keyboard=True)
    await update.message.reply_text(
        "شكراً على تقييمك! 🙏\nهل ترغب بترك تعليق لتحسين الخدمة؟ (أو اختر 'حلو عني 😒' لتخطي التعليق)",
        reply_markup=reply_markup
    )
    return ASK_RATING_COMMENT





async def request_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text

    if text == "حلو عني 😒":
        reply_markup = ReplyKeyboardMarkup([
            ["اطلب عالسريع 🔥"],
            ["لا بدي عدل 😐", "التواصل مع الدعم 🎧"],
            ["من نحن 🏢", "أسئلة متكررة ❓"]
        ], resize_keyboard=True)
        await update.message.reply_text("ماشي 😒 رجعناك للقائمة الرئيسية.", reply_markup=reply_markup)
        return MAIN_MENU

    order_info = await get_last_order(user_id)
    if not order_info:
        await update.message.reply_text("ليس لديك طلبات سابقة للتقييم.")
        return MAIN_MENU

    # ✅ تخزين البيانات في conversation_state للاحتياط
    await update_conversation_state(user_id, "rating_order_id", order_info["order_id"])
    await update_conversation_state(user_id, "rating_order_number", order_info["order_number"])
    await update_conversation_state(user_id, "rating_restaurant_id", order_info["restaurant_id"])

    # ✅ تخزين البيانات محليًا داخل السياق لتستخدم لاحقًا في pending_rating
    context.user_data["order_data"] = {
        "order_id": order_info["order_id"],
        "order_number": order_info["order_number"],
        "restaurant_id": order_info["restaurant_id"],
        "restaurant_name": order_info["restaurant_name"]
    }

    keyboard = [
        ["⭐", "⭐⭐", "⭐⭐⭐"],
        ["⭐⭐⭐⭐", "⭐⭐⭐⭐⭐"],
        ["حلو عني 😒"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        f"يرجى تقييم طلبك رقم {order_info['order_number']} من مطعم {order_info['restaurant_name']}:",
        reply_markup=reply_markup
    )
    return ASK_RATING



async def handle_rating_comment(update: Update, context: CallbackContext) -> int:
    user_id = update.effective_user.id
    text = update.message.text
    comment = None if text == "حلو عني 😒" else text

    pending = context.user_data.get("pending_rating")
    if not pending:
        # محاولة جلب البيانات من قاعدة البيانات المؤقتة
        try:
            state = await get_conversation_state(user_id)
            pending = {
                "restaurant_id": state.get("rating_restaurant_id"),
                "order_id": state.get("rating_order_id"),
                "order_number": state.get("rating_order_number"),
                "stars": state.get("rating_stars") or context.user_data.get("temp_rating")
            }
        except Exception as e:
            logger.error(f"❌ فشل في جلب بيانات التقييم الاحتياطية: {e}")
            await update.message.reply_text("❌ لم نتمكن من متابعة التقييم. يرجى المحاولة لاحقاً.")
            return MAIN_MENU

    # استخراج البيانات
    restaurant_id = pending.get("restaurant_id")
    order_id = pending.get("order_id")
    order_number = pending.get("order_number")
    rating = pending.get("stars")

    if not all([restaurant_id, order_id, order_number, rating]):
        await update.message.reply_text("❌ البيانات غير مكتملة. حاول مرة أخرى لاحقاً.")
        return MAIN_MENU

    success = await send_rating_to_restaurant(
        bot=context.bot,
        user_id=user_id,
        order_id=order_id,
        order_number=order_number,
        restaurant_id=restaurant_id,
        rating=rating,
        comment=comment
    )

    context.user_data.pop("pending_rating", None)

    reply_markup = ReplyKeyboardMarkup([
        ["اطلب عالسريع 🔥"],
        ["لا بدي عدل 😐", "التواصل مع الدعم 🎧"],
        ["من نحن 🏢", "أسئلة متكررة ❓"]
    ], resize_keyboard=True)

    if success:
        if comment is None:
            await update.message.reply_text("حلينا 😒 رجعناك للقائمة الرئيسية.", reply_markup=reply_markup)
        else:
            await update.message.reply_text("شكرا يا قلبي ❤️", reply_markup=reply_markup)
    else:
        await update.message.reply_text("❌ فشل في إرسال التقييم. يرجى المحاولة لاحقاً.", reply_markup=reply_markup)

    return MAIN_MENU







async def send_rating_to_restaurant(bot, user_id, order_id, order_number, restaurant_id, rating, comment=None):
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                # تحقق من وجود تقييم سابق للمستخدم لهذا المطعم
                await cursor.execute(
                    "SELECT id FROM restaurant_ratings WHERE restaurant_id = %s AND user_id = %s",
                    (restaurant_id, user_id)
                )
                existing_rating = await cursor.fetchone()
                
                if existing_rating:
                    # تحديث التقييم الموجود
                    await cursor.execute(
                        "UPDATE restaurant_ratings SET rating = %s, comment = %s, created_at = NOW() WHERE restaurant_id = %s AND user_id = %s",
                        (rating, comment, restaurant_id, user_id)
                    )
                else:
                    # إضافة تقييم جديد
                    await cursor.execute(
                        "INSERT INTO restaurant_ratings (restaurant_id, user_id, rating, comment) VALUES (%s, %s, %s, %s)",
                        (restaurant_id, user_id, rating, comment)
                    )
                
                await conn.commit()
                
                # جلب قناة المطعم لإرسال التقييم
                await cursor.execute("SELECT channel FROM restaurants WHERE id = %s", (restaurant_id,))
                result = await cursor.fetchone()

        if not result:
            return False

        channel_id = result[0]
        stars = "⭐" * rating
        message = f"المستخدم {user_id} استلم طلبه رقم {order_number} وقام بتقييمه بـ {stars}\n🆔 معرف الطلب: {order_id}\n"
        if comment and comment.strip():
            message += f"💬 التعليق: {comment}"

        await bot.send_message(chat_id=channel_id, text=message, parse_mode="Markdown")
        return True

    except Exception as e:
        logger.error(f"خطأ في إرسال التقييم: {e}")
        return False




async def handle_report_based_cancellation(update: Update, context: CallbackContext):
    """📩 يلتقط إشعار إلغاء الطلب بسبب شكوى ويرسل إشعار للمستخدم"""

    try:
        text = update.channel_post.text
        logger.info(f"📥 استلمنا رسالة من القناة لمعالجتها كشكوى:\n{text}")

        # ✅ استخراج معرف الطلب والسبب من الرسالة (بدون backticks)
        match = re.search(r"📌 معرف الطلب: ([\w-]+)\s+📍 السبب: (.+)", text)
        if not match:
            logger.warning(f"⚠️ لم يتم العثور على معرف الطلب أو السبب في الرسالة:\n{text}")
            return

        order_id = match.group(1)
        reason = match.group(2)

        logger.info(f"🔍 تم استخراج معرف الطلب: {order_id} والسبب: {reason}")

        # ✅ استخدام القاموس بدل قاعدة البيانات
        user_id = user_orders.get(order_id)
        if not user_id:
            logger.warning(f"⚠️ لم يتم العثور على مستخدم لهذا الطلب في الذاكرة: {order_id}")
            return

        # ✅ إعداد الرسالة للمستخدم
        message = (
            f"🚫 *تم إلغاء طلبك بسبب شكوى من الكاشير.*\n\n"
            f"📌 *سبب الإلغاء:* {reason}\n\n"
            f"نعتذر منك عن هذا الإزعاج.\n"
            f"يرجى التعاون عند التواصل معك لتحديد المشكلة.\n"
            f"يمكنك تعديل بياناتك أو اختيار مطعم آخر إن أردت.\n"
            f"وإذا واجهت مشكلة، لا تتردد بالتواصل معنا عبر الدعم. 🙏"
        )

        # ✅ كيبورد مخصص للرجوع للقائمة
        reply_markup = ReplyKeyboardMarkup([
            ["اطلب عالسريع 🔥"],
            ["لا بدي عدل 😐", "التواصل مع الدعم 🎧"],
            ["من نحن 🏢", "أسئلة متكررة ❓"]
        ], resize_keyboard=True)

        # ✅ إرسال الرسالة للمستخدم مع الكيبورد المخصص
        await context.bot.send_message(
            chat_id=user_id,
            text=message,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )

        # ✅ تنظيف بيانات المستخدم المؤقتة
        user_orders.pop(order_id, None)
        if "conversation_states" in context.bot_data:
            context.bot_data["conversation_states"].pop(user_id, None)
        if "shopping_carts" in context.bot_data:
            context.bot_data["shopping_carts"].pop(user_id, None)

        logger.info(f"✅ تم إعلام المستخدم {user_id} بإلغاء الطلب {order_id} وإعادته للقائمة الرئيسية.")

    except Exception as e:
        logger.error(f"❌ حدث خطأ أثناء معالجة الشكوى أو إرسالها للمستخدم: {e}")






async def reset_user_and_restart(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("DELETE FROM user_data WHERE user_id = %s", (user_id,))
            await conn.commit()

        await context.bot.send_message(
            chat_id=user_id,
            text="❌ انتهت خدمتنا في منطقتك مؤقتًا.\n"
                 "سنعود بأقرب وقت ممكن بإذن الله 🙏\n"
                 "سنبدأ من جديد الآن."
        )

        # تخصيص دالة start لتقبل user_id وحده (تحتاج لتعديل دالة start لدعم هذا إن لم تكن تدعمه)
        await start(update=None, context=context, user_id=user_id)

    except Exception as e:
        logger.exception(f"❌ خطأ أثناء تنفيذ reset_user_and_restart للمستخدم {user_id}: {e}")






async def safe_reply(update, text, **kwargs):
    for _ in range(3):  # عدد المحاولات
        try:
            return await update.message.reply_text(text, **kwargs)
        except NetworkError:
            await asyncio.sleep(2)  # انتظر قبل إعادة المحاولة
    raise NetworkError("حدث خطأ في الشبكة بعد عدة محاولات.")

async def safe_request(bot_func, *args, **kwargs):
    for _ in range(3):  # عدد المحاولات
        try:
            return await bot_func(*args, **kwargs)
        except telegram.error.TimedOut:
            await asyncio.sleep(5)  # الانتظار قبل المحاولة مرة أخرى
    raise telegram.error.TimedOut("فشل الاتصال بعد عدة محاولات.")




async def handle_vip_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args

    if not args or not args[0].startswith("vip_"):
        await update.message.reply_text("❌ رابط الإعلان غير صالح.")
        return ConversationHandler.END

    try:
        _, city_id_str, restaurant_id_str = args[0].split("_")
        city_id = int(city_id_str)
        restaurant_id = int(restaurant_id_str)
    except Exception:
        await update.message.reply_text("❌ رابط الإعلان غير صالح.")
        return ConversationHandler.END

    # ✅ التحقق من التسجيل
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT city_id FROM users WHERE user_id = %s", (user_id,))
                row = await cursor.fetchone()

        if not row:
            await update.message.reply_text("👋 لازم تسجل معلوماتك أولاً من خيار 'خلينا نبلش 😁'.")
            return ConversationHandler.END

        if row[0] != city_id:
            await update.message.reply_text("❌ هذا الإعلان موجه لمدينة أخرى.")
            return ConversationHandler.END

        # ✅ التحقق من الطلبات الجارية
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT id FROM orders WHERE user_id = %s AND status = 'قيد التنفيذ'", (user_id,))
                active_order = await cursor.fetchone()

        if active_order:
            await update.message.reply_text("🚫 لا يمكنك فتح عرض جديد أثناء وجود طلب قيد التنفيذ.")
            return ConversationHandler.END

        # ✅ تفريغ السلة والانتقال للفئات
        context.user_data["orders"] = []
        context.user_data["selected_restaurant_id"] = restaurant_id

        return await show_restaurant_categories(update, context, from_ad=True)

    except Exception as e:
        logger.error(f"❌ خطأ أثناء تنفيذ handle_vip_start: {e}")
        await update.message.reply_text("❌ حدث خطأ أثناء معالجة الإعلان.")
        return ConversationHandler.END

BOT_USERNAME = "@YALA_FAST_bot"




async def handle_vip_broadcast_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.chat.type != ChatType.CHANNEL:
        return

    if not update.message.reply_markup or not update.message.reply_markup.inline_keyboard:
        return

    try:
        button = update.message.reply_markup.inline_keyboard[0][0]
        url = button.url
        if not url or "start=vip_" not in url:
            return

        # استخراج city_id و restaurant_id من الرابط
        parts = url.split("start=vip_")[1].split("_")
        city_id = int(parts[0])
        restaurant_id = int(parts[1]) if len(parts) > 1 else None
    except Exception:
        return

    # 🔄 جلب المستخدمين المسجلين في المدينة
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT user_id FROM users WHERE city_id = %s", (city_id,))
                rows = await cursor.fetchall()
                users = [row[0] for row in rows]
    except Exception as e:
        logger.error(f"❌ فشل في جلب المستخدمين للإعلان VIP: {e}")
        return

    # 🎯 تجهيز الزر إن وُجد مطعم
    button_markup = None
    if restaurant_id:
        vip_link = f"https://t.me/{BOT_USERNAME}?start=vip_{city_id}_{restaurant_id}"
        button_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("اكبسني 😉", url=vip_link)]
        ])

    # 📨 إرسال الرسائل للمستخدمين
    for user_id in users:
        try:
            if update.message.photo:
                await context.bot.send_photo(
                    chat_id=user_id,
                    photo=update.message.photo[-1].file_id,
                    caption=update.message.caption,
                    parse_mode="Markdown",
                    reply_markup=button_markup
                )
            elif update.message.video:
                await context.bot.send_video(
                    chat_id=user_id,
                    video=update.message.video.file_id,
                    caption=update.message.caption,
                    parse_mode="Markdown",
                    reply_markup=button_markup
                )
            else:
                await context.bot.send_message(
                    chat_id=user_id,
                    text=update.message.text or "📢 إعلان جديد",
                    parse_mode="Markdown",
                    reply_markup=button_markup
                )
        except Exception as e:
            logger.warning(f"⚠️ لم نتمكن من إرسال الإعلان للمستخدم {user_id}: {e}")




async def handle_ad_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    message = update.message

    if message and message.text and message.text.startswith("/start "):
        arg = message.text.split("/start ", 1)[1].strip()
        print(f"handle_ad_start: Processing /start with arguments: '{arg}' for user {user_id}")

        now = datetime.now()
        last_click = context.user_data.get("last_ad_click_time")
        if last_click and (now - last_click).total_seconds() < 2:
            return ConversationHandler.END
        context.user_data["last_ad_click_time"] = now

        # ✅ إعلان go_
        if arg.startswith("go_"):
            restaurant_name = arg.replace("go_", "").strip()
            context.user_data["go_ad_restaurant_name"] = restaurant_name

            await message.reply_text(
                "📢 *يالله عالسرييع 🔥*\n\n"
                "وصلت من إعلان، لتكمل:\n"
                "➤ اضغط على زر *اطلب عالسريع 🔥* في الأسفل\n"
                "➤ ثم اختر المطعم اللي شفته\n\n"
                "👇 وبلّش شوف العروض 👇",
                parse_mode="Markdown"
            )
            return ConversationHandler.END

        # ✅ إعلان VIP
        elif arg.startswith("vip_"):
            try:
                _, city_id_str, restaurant_id_str = arg.split("_", 2)
                city_id = int(city_id_str)

                async with get_db_connection() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute("SELECT city_id FROM user_data WHERE user_id = %s", (user_id,))
                        row = await cursor.fetchone()

                if not row:
                    await message.reply_text("❌ لم نجد بياناتك. يرجى التسجيل أولاً بالضغط على /start.")
                    return ConversationHandler.END

                if row[0] != city_id:
                    await message.reply_text("❌ هذا الإعلان غير موجه لمدينتك.")
                    return ConversationHandler.END

                context.user_data["selected_restaurant_id"] = int(restaurant_id_str)
                context.user_data["orders"] = []
                return await show_restaurant_categories(update, context, from_ad=True)

            except ValueError:
                print(f"❌ خطأ في تحليل باراميتر إعلان VIP: {arg}")
                await message.reply_text("❌ رابط إعلان VIP غير صالح.")
                return ConversationHandler.END
            except Exception as e:
                print(f"❌ خطأ في معالجة إعلان VIP: {e}")
                await message.reply_text("❌ حدث خطأ أثناء معالجة الإعلان.")
                return ConversationHandler.END

        else:
            await message.reply_text("⚠️ هذا النوع من الإعلانات غير مدعوم.")
            return ConversationHandler.END

    else:
        print(f"handle_ad_start: Plain /start detected for user {user_id}.")
        return await start(update, context)






async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    import traceback

    # طباعة كاملة في التيرمنال
    logger.error("🔥🔥🔥 حدث استثناء!", exc_info=context.error)

    # توليد نص Stack Trace
    traceback_text = ''.join(traceback.format_exception(None, context.error, context.error.__traceback__))

    try:
        await context.bot.send_message(
            chat_id=ERRORS_CHANNEL,
            text=f"🚨 *حدث خطأ في البوت!*\n```{traceback_text}```",
            parse_mode="Markdown"
        )
    except:
        pass

    # إبلاغ المستخدم برسالة مناسبة
    if getattr(update, "message", None):
        try:
            await update.message.reply_text("❌ حدث خطأ أثناء تنفيذ الطلب. حاول مرة أخرى لاحقاً.")
        except:
            pass
    elif getattr(update, "callback_query", None):
        try:
            await update.callback_query.answer("❌ حدث خطأ أثناء معالجة الزر. حاول لاحقاً.", show_alert=True)
        except:
            pass



# يمكنك وضع معرف القناة هنا (مثال: -1001234567890)
DEVELOPER_CHAT_ID = -1002586617686  # 👈 ضع معرف قناتك هنا

async def check_and_refresh_mysql():
    """التحقق من أن الاتصال بقاعدة البيانات ما زال فعالًا"""
    global pool
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT 1")
                await cursor.fetchone()
        logger.info("✅ اتصال MySQL سليم.")
    except Exception as e:
        logger.warning(f"⚠️ فقد الاتصال بقاعدة البيانات: {e}")

        # محاولة إعادة الاتصال
        try:
            pool.close()
            await pool.wait_closed()
            logger.info("🔄 إعادة إنشاء اتصال MySQL...")

            pool = await aiomysql.create_pool(
                host=DB_HOST,
                port=DB_PORT,
                user=DB_USER,
                password=DB_PASSWORD,
                db=DB_NAME,
                autocommit=True,
                minsize=1,
                maxsize=10,
                pool_recycle=3600
            )

            logger.info("✅ تم إنشاء الاتصال الجديد بنجاح.")

            # إرسال إشعار بالنجاح
            if 'app' in globals():
                await app.bot.send_message(
                    chat_id=DEVELOPER_CHAT_ID,
                    text="✅ تمت استعادة الاتصال بقاعدة البيانات بنجاح."
                )

        except Exception as err:
            logger.error(f"❌ فشل في إعادة الاتصال بقاعدة البيانات: {err}")

            # إرسال إشعار بالفشل
            if 'app' in globals():
                await app.bot.send_message(
                    chat_id=DEVELOPER_CHAT_ID,
                    text=f"❌ فشل في إعادة الاتصال بقاعدة البيانات:\n{err}"
                )


def schedule_mysql_watchdog(application):
    scheduler = BackgroundScheduler()
    scheduler.add_job(check_and_refresh_mysql, "interval", minutes=5)
    scheduler.start()






async def reset_order_counters():
    logger.info("🔄 بدء تصفير عدادات الطلبات")
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("UPDATE restaurants SET order_counter = 0")
                await conn.commit()
                logger.info("✅ تم تصفير العدادات لجميع المطاعم")
    except Exception as e:
        logger.error(f"❌ خطأ أثناء تصفير العدادات: {e}", exc_info=True)


async def dev_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    context.user_data.clear()

    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cursor:
                # محاولة الحصول على رقم الهاتف الحالي للمستخدم
                await cursor.execute("SELECT phone FROM user_data WHERE user_id = %s", (user_id,))
                result = await cursor.fetchone()
                phone = result[0] if result else None

                # حذف جميع بيانات المستخدم
                await cursor.execute("DELETE FROM conversation_states WHERE user_id = %s", (user_id,))
                await cursor.execute("DELETE FROM shopping_carts WHERE user_id = %s", (user_id,))
                await cursor.execute("DELETE FROM user_data WHERE user_id = %s", (user_id,))

                if phone:
                    await cursor.execute("DELETE FROM user_data WHERE phone = %s", (phone,))

            await conn.commit()

        await update.message.reply_text("✅ تم مسح بياناتك بالكامل، جاري البدء من جديد 🔄")
        # إعادة التوجيه إلى start() مباشرة
        return await start(update, context)

    except Exception as e:
        logger.error(f"❌ خطأ أثناء dev_reset: {e}")
        await update.message.reply_text("❌ حدث خطأ أثناء إعادة التعيين. حاول لاحقاً.")
        return ConversationHandler.END





ASK_INFO, ASK_NAME, ASK_PHONE, ASK_PHONE_VERIFICATION, ASK_PROVINCE, ASK_CITY, ASK_LOCATION_IMAGE, CONFIRM_INFO, EDIT_NAME, EDIT_PHONE, EDIT_PHONE_VERIFY, EDIT_LOCATION, MAIN_MENU, ORDER_CATEGORY, ORDER_MEAL, CONFIRM_ORDER, SELECT_RESTAURANT, ASK_ORDER_LOCATION, CONFIRM_FINAL_ORDER, ASK_NEW_LOCATION_IMAGE, ASK_NEW_LOCATION_TEXT, CANCEL_ORDER_OPTIONS, ASK_CUSTOM_CITY, ASK_NEW_RESTAURANT_NAME, ASK_ORDER_NOTES, ASK_REPORT_REASON, ASK_AREA_NAME,  EDIT_FIELD_CHOICE, ASK_NEW_AREA_NAME, ASK_DETAILED_LOCATION, ASK_NEW_DETAILED_LOCATION, ASK_RATING_COMMENT, ASK_RATING, RATING_COMMENT     = range(34)




conv_handler = ConversationHandler(
    entry_points=[CommandHandler("start", handle_ad_start)],
    states={
        ASK_INFO: [
            MessageHandler(filters.Regex("^ليش هالأسئلة ؟ 🧐$"), ask_info_details),
            MessageHandler(filters.Regex("^أسئلة متكررة ❓$"), handle_faq_entry),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_info_selection),
            MessageHandler(filters.Regex("^خلينا نبلش 😁$"), ask_name)
        ],
        ASK_NAME: [
            MessageHandler(filters.Regex("^عودة ⬅️$"), handle_back_to_info),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_name)
        ],
        ASK_PHONE: [
            MessageHandler(filters.Regex("^عودة ⬅️$"), ask_name),
            MessageHandler(filters.TEXT & ~filters.COMMAND, send_verification_code)
        ],
        ASK_PHONE_VERIFICATION: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, verify_code)
        ],
        ASK_PROVINCE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_province)],
        ASK_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_city)],
        ASK_CUSTOM_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_custom_city)],
        ASK_LOCATION_IMAGE: [
            MessageHandler(filters.LOCATION, handle_location),
            MessageHandler(filters.Regex("عودة ➡️"), ask_location),
            MessageHandler(filters.TEXT & ~filters.COMMAND, ask_location)  # ← لمنع تجاهل الضغط النصي على "📍 إرسال موقعي"
        ],

        ASK_AREA_NAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_area_name),
            MessageHandler(filters.Regex("عودة ➡️"), ask_location)
        ],
        ASK_DETAILED_LOCATION: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, confirm_info),
            MessageHandler(filters.Regex("عودة ➡️"), ask_area_name)
        ],
        CONFIRM_INFO: [
            MessageHandler(filters.Regex("اي ولو 😏"), handle_confirmation),
            MessageHandler(filters.Regex("لا بدي عدل 😐"), ask_edit_choice)
        ],
        MAIN_MENU: [
            MessageHandler(filters.Regex("اطلب عالسريع 🔥"), main_menu),
            MessageHandler(filters.Regex("^لا بدي عدل 😐$"), ask_edit_choice),
            MessageHandler(filters.Regex("^تعديل معلوماتي 🖊$"), ask_edit_choice),
            MessageHandler(filters.Regex("أسئلة متكررة ❓"), handle_faq_entry),
            MessageHandler(filters.Regex("التواصل مع الدعم 🎧"), main_menu),
            MessageHandler(filters.Regex("^من نحن 🏢$"), main_menu),
            MessageHandler(filters.Regex("وصل طلبي شكرا لكم 🙏"), request_rating),
            MessageHandler(filters.Regex("إلغاء الطلب بسبب مشكلة 🫢"), handle_order_issue),
            MessageHandler(filters.Regex("تأخرو عليي ما بعتولي انن بلشو 🫤"), handle_no_confirmation),
            MessageHandler(filters.Regex("ذكرلي المطعم بطلبي 🙋"), handle_reminder),
            MessageHandler(filters.Regex("تذكير المطعم بطلبي 👋"), handle_reminder_order_request),
            MessageHandler(filters.Regex("كم يتبقى لطلبي"), ask_remaining_time),
            MessageHandler(filters.Regex("إلغاء ❌ بدي عدل"), handle_order_cancellation),
            MessageHandler(filters.TEXT & ~filters.COMMAND, main_menu)
        ],
        EDIT_FIELD_CHOICE: [
            MessageHandler(filters.Regex("^✏️ الاسم$"), handle_edit_field_choice),
            MessageHandler(filters.Regex("^📱 رقم الهاتف$"), handle_edit_field_choice),
            MessageHandler(filters.Regex("^📍 الموقع$"), handle_edit_field_choice),
            MessageHandler(filters.Regex("^عودة ⬅️$"), confirm_info),
            MessageHandler(filters.TEXT, handle_edit_field_choice)
        ],
        EDIT_NAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_name_edit)
        ],
        EDIT_PHONE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, send_verification_code_edit)
        ],
        EDIT_PHONE_VERIFY: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, verify_code_edit)
        ],
        EDIT_LOCATION: [
            MessageHandler(filters.LOCATION, handle_location_edit),
            MessageHandler(filters.Regex("عودة ⬅️"), ask_edit_choice)
        ],
        SELECT_RESTAURANT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_restaurant_selection)
        ],
        ASK_NEW_RESTAURANT_NAME: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_missing_restaurant)
        ],
        ORDER_CATEGORY: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_order_category),
            MessageHandler(filters.Regex("^القائمة الرئيسية 🪧$"), return_to_main_menu)
        ],
        ORDER_MEAL: [
            CallbackQueryHandler(handle_add_meal_with_size, pattern="^add_meal_with_size:"),
            CallbackQueryHandler(handle_remove_specific_meal, pattern="^remove_specific_meal:"),
            CallbackQueryHandler(handle_done_adding_meals, pattern="^done_adding_meals$"),
            MessageHandler(filters.Regex("^القائمة الرئيسية 🪧$"), return_to_main_menu),
            MessageHandler(filters.Regex("^تم ✅$"), handle_done_adding_meals),
            MessageHandler(filters.TEXT & ~filters.COMMAND, process_category_selection)
        ],
        ASK_ORDER_NOTES: [
            MessageHandler(filters.Regex("^عودة ➡️$"), handle_order_category),
            MessageHandler(filters.Regex("^القائمة الرئيسية 🪧$"), return_to_main_menu),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_order_notes)
        ],
        CONFIRM_ORDER: [
            MessageHandler(filters.Regex("إلغاء ❌"), handle_final_cancellation),
            MessageHandler(filters.Regex("^القائمة الرئيسية 🪧$"), return_to_main_menu)
        ],
        ASK_ORDER_LOCATION: [
            MessageHandler(filters.Regex("لاا أنا بمكان تاني 🌚"), ask_new_location),
            MessageHandler(filters.Regex("نفس الموقع يلي عطيتكن ياه بالاول 🌝"), ask_order_location)
        ],
        ASK_NEW_LOCATION_IMAGE: [
            MessageHandler(filters.LOCATION, handle_new_location),
            MessageHandler(filters.Regex("عودة ⬅️"), ask_new_location)
        ],
        ASK_NEW_AREA_NAME: [
            MessageHandler(filters.Regex("عودة ⬅️"), ask_order_location),  # رجوع للخطوة الأصلية
            MessageHandler(filters.TEXT & ~filters.COMMAND, ask_new_area_name)
        ],
        ASK_NEW_DETAILED_LOCATION: [
            MessageHandler(filters.Regex("عودة ⬅️"), ask_new_area_name),  # رجوع للمنطقة
            MessageHandler(filters.TEXT & ~filters.COMMAND, ask_new_detailed_location)
        ],
        CANCEL_ORDER_OPTIONS: [
            MessageHandler(filters.Regex("ذكرلي المطعم بطلبي 🙋"), handle_reminder),
            MessageHandler(filters.Regex("تذكير المطعم بطلبي 👋"), handle_reminder_order_request),
            MessageHandler(filters.Regex("كم يتبقى لطلبي"), handle_remaining_time_for_order),
            MessageHandler(filters.Regex("إلغاء وإرسال تقرير ❌"), handle_report_issue),
            MessageHandler(filters.Regex("إلغاء وإنشاء تقرير ❌"), ask_report_reason),
            MessageHandler(filters.Regex("منرجع ومننطر 🙃"), handle_return_and_wait),
            MessageHandler(filters.Regex("العودة والانتظار 🙃"), handle_back_and_wait),
            MessageHandler(filters.Regex("إلغاء متأكد ❌"), handle_confirm_cancellation),
            MessageHandler(filters.Regex("^اي اي متاكد 🥱$"), handle_confirm_cancellation),
            MessageHandler(filters.Regex("^اي متاكد 🥱$"), handle_final_cancellation),
            MessageHandler(filters.Regex("^لا خلص منرجع ومننتظر 🥲$"), handle_final_cancellation),
            MessageHandler(filters.Regex("إلغاء ❌ بدي عدل"), handle_order_cancellation),
            MessageHandler(filters.Regex("تأخرو عليي ما بعتولي انن بلشو 🫤"), handle_no_confirmation),
            MessageHandler(filters.Regex("معلش رجعني 🙃"), handle_confirm_cancellation),
            MessageHandler(filters.Regex("وصل طلبي شكرا لكم 🙏"), request_rating),
            MessageHandler(filters.Regex("إلغاء الطلب بسبب مشكلة 🫢"), handle_order_issue),
            MessageHandler(filters.Regex("^تأخرو كتير إلغاء عالسريع 😡$"), handle_final_cancellation),
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_cancellation_reason)
        ],
        CONFIRM_FINAL_ORDER: [
            MessageHandler(filters.Regex("يلا عالسريع 🔥|لا ماني متأكد 😐"), handle_confirm_final_order)
        ],
        ASK_REPORT_REASON: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_report_cancellation)
        ],
        ASK_RATING: [
            MessageHandler(filters.Regex(r"⭐.*"), handle_rating),
            MessageHandler(filters.Regex("حلو عني 😒"), handle_rating)
        ],
        ASK_RATING_COMMENT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_rating_comment)
        ]

    },
     fallbacks=[CommandHandler("cancel", start)]
)


ORDER_ID_PATTERNS = [
    r"معرف الطلب:?\s*[`\"']?([\w\d\-]+)[`\"']?",
    r"🆔.*?معرف الطلب:?\s*[`\"']?([\w\d\-]+)[`\"']?",
    r"🆔\s*معرف الطلب:\s*`([^`]+)`",  # تنسيق جديد
    r"order_id:?\s*[`\"']?([\w\d\-]+)[`\"']?"
]


ORDER_NUMBER_PATTERNS = [
    r"رقم الطلب:?\s*[`\"']?(\d+)[`\"']?",
    r"🔢.*?رقم الطلب:?\s*[`\"']?(\d+)[`\"']?",
    r"🔢\s*رقم الطلب:\s*`(\d+)`",  # تنسيق جديد
    r"order_number:?\s*[`\"']?(\d+)[`\"']?"
]

def extract_order_id(text):
    """استخراج معرف الطلب من النص"""
    for pattern in ORDER_ID_PATTERNS:
        match = re.search(pattern, text)
        if match:
            logger.info(f"🎯 تم التطابق مع: {pattern}")
            return match.group(1)
    logger.warning(f"⚠️ لم يتم استخراج معرف الطلب من:\n{text}")
    return None

def extract_order_number(text):
    """استخراج رقم الطلب من النص"""
    for pattern in ORDER_NUMBER_PATTERNS:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None


    

def run_user_bot () :
    application = Application.builder().token("8035364090:AAFlQC5slPnNBMnFUxyyZzxS5ltWkWZZ6CM").build()
    print("✅ logging initialized")
    logger.info("🚀 تم تشغيل البوت بنجاح وهو جاهز لاستقبال الأوامر.")

    

    application.add_handler(CommandHandler("start_dev_reset1147", dev_reset))
    

    # المعالجات
    application.add_handler(conv_handler)
    application.add_handler(CommandHandler("start", start))

    
    application.add_handler(CommandHandler("testimage", test_copy_image))
   
    application.add_handler(MessageHandler(
    filters.ChatType.CHANNEL & filters.Regex(r"^\d{1,3}$"),
    handle_remaining_time_for_order
))


   
    # 2. رسائل الإلغاء بسبب شكوى (تتضمن "بسبب شكوى")
    application.add_handler(MessageHandler(
        filters.ChatType.CHANNEL & filters.Regex(r"بسبب شكوى"),
        handle_report_based_cancellation
    ))
    
    # 3. باقي الرسائل النصية من القناة (fallback handler)
    application.add_handler(MessageHandler(
        filters.ChatType.CHANNEL & filters.TEXT,
        handle_cashier_interaction
    ))


    application.add_handler(MessageHandler(
        filters.Chat(username="vip_ads_channel") & filters.Regex(r"/start vip_\\d+_\\d+"),
        handle_vip_broadcast_message
    ))
    application.add_handler(CallbackQueryHandler(handle_faq_response, pattern="^faq_(refusal|eta|issue|ban|no_delivery|repeat_cancel)$"))
    application.add_handler(CallbackQueryHandler(handle_faq_back, pattern="^faq_back$"))
    application.add_handler(MessageHandler(filters.ChatType.CHANNEL, handle_vip_broadcast_message))

    application.add_handler(CallbackQueryHandler(explain_location_instruction, pattern="^how_to_send_location$"))
    application.add_handler(CallbackQueryHandler(handle_order_flow_help, pattern="^help_with_order_flow$"))


    
    # الأخطاء
    application.add_error_handler(error_handler)

    # قاعدة البيانات إن وُجدت
    initialize_database()  # إذا كانت غير async

    # المهام المجدولة
    scheduler = BackgroundScheduler()
    scheduler.add_job(reset_order_counters, CronTrigger(hour=0, minute=0))
    scheduler.start()

    schedule_mysql_watchdog(application)

    # تشغيل البوت
    application.run_polling()

if __name__ == "__main__":
    run_user_bot()


