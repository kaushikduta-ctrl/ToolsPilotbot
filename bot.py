import asyncio
import feedparser
import sqlite3
import re
import random
from datetime import datetime
import os
import logging
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.error import TelegramError, TimedOut, RetryAfter, Conflict
import google.generativeai as genai
import time

# ============ LOGGING ============
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============ CONFIGURATION ============
TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TOKEN:
    logger.error("❌ Missing TELEGRAM_TOKEN!")
    exit(1)

# Configure Gemini if API key is provided
USE_GEMINI = GEMINI_API_KEY is not None
if USE_GEMINI:
    genai.configure(api_key=GEMINI_API_KEY)
    gemini_model = genai.GenerativeModel('gemini-1.5-flash')
    logger.info("✅ Gemini API configured!")

SOURCES = {
    "tech": "https://www.reddit.com/r/technology/.rss",
    "crypto": "https://www.reddit.com/r/CryptoCurrency/.rss",
    "sports": "https://www.reddit.com/r/sports/.rss",
    "world": "http://rss.cnn.com/rss/cnn_topstories.rss",
    "bbc": "https://feeds.bbci.co.uk/news/rss.xml"
}

DEFAULT_CATEGORIES = ["tech", "crypto", "sports", "world", "bbc"]
POSTS_PER_HOUR = 5
CHECK_INTERVAL = 600  # 10 minutes
MAX_RETRIES = 3

# ============ DATABASE (Multi-Group) ============
class Database:
    def __init__(self):
        self.conn = sqlite3.connect('news.db', check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._init_tables()
    
    def _init_tables(self):
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS posted 
                 (link TEXT PRIMARY KEY, time TIMESTAMP)''')
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS groups 
                 (chat_id TEXT PRIMARY KEY, 
                  categories TEXT, 
                  last_post TIMESTAMP,
                  is_active INTEGER DEFAULT 1)''')
        self.conn.commit()
    
    def is_posted(self, link):
        try:
            self.cursor.execute("SELECT 1 FROM posted WHERE link = ?", (link,))
            return self.cursor.fetchone() is not None
        except:
            return False
    
    def mark_posted(self, link):
        try:
            self.cursor.execute("INSERT INTO posted (link, time) VALUES (?, ?)", 
                              (link, datetime.now()))
            self.conn.commit()
            return True
        except:
            return False
    
    def add_group(self, chat_id, categories=None):
        if categories is None:
            categories = DEFAULT_CATEGORIES
        categories_str = ",".join(categories)
        try:
            self.cursor.execute(
                "INSERT OR REPLACE INTO groups (chat_id, categories, last_post, is_active) VALUES (?, ?, ?, 1)",
                (str(chat_id), categories_str, datetime.now())
            )
            self.conn.commit()
            return True
        except Exception as e:
            logger.error(f"Failed to add group {chat_id}: {e}")
            return False
    
    def remove_group(self, chat_id):
        try:
            self.cursor.execute("DELETE FROM groups WHERE chat_id = ?", (str(chat_id),))
            self.conn.commit()
            return True
        except:
            return False
    
    def get_all_groups(self):
        try:
            self.cursor.execute("SELECT chat_id, categories, is_active FROM groups WHERE is_active = 1")
            results = self.cursor.fetchall()
            groups = []
            for chat_id, categories_str, active in results:
                categories = categories_str.split(",") if categories_str else DEFAULT_CATEGORIES
                groups.append({
                    'chat_id': int(chat_id),
                    'categories': categories,
                    'is_active': active
                })
            return groups
        except Exception as e:
            logger.error(f"Failed to get groups: {e}")
            return []
    
    def update_last_post(self, chat_id):
        try:
            self.cursor.execute(
                "UPDATE groups SET last_post = ? WHERE chat_id = ?",
                (datetime.now(), str(chat_id))
            )
            self.conn.commit()
        except:
            pass
    
    def cleanup_old(self):
        try:
            self.cursor.execute("DELETE FROM posted WHERE time < datetime('now', '-7 days')")
            self.conn.commit()
        except:
            pass

db = Database()

# ============ FETCH NEWS ============
def fetch_feed_sync(url, retry_count=0):
    try:
        feed = feedparser.parse(url, 
            agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        
        if feed.entries:
            return feed
        else:
            if "reddit.com" in url:
                alt_url = url.replace(".rss", ".rss?format=json")
                feed = feedparser.parse(alt_url)
                if feed.entries:
                    return feed
            
            raise Exception("No entries found")
    except Exception as e:
        if retry_count < MAX_RETRIES:
            wait = (2 ** retry_count) + random.random()
            logger.warning(f"Retry {retry_count+1} for {url} in {wait:.1f}s")
            time.sleep(wait)
            return fetch_feed_sync(url, retry_count + 1)
        else:
            logger.error(f"Failed to fetch {url}: {e}")
            return None

def get_news_for_categories(categories):
    articles = []
    
    for category in categories:
        if category not in SOURCES:
            continue
        
        url = SOURCES[category]
        try:
            feed = fetch_feed_sync(url)
            
            if not feed or not feed.entries:
                continue
                
            for entry in feed.entries[:3]:
                link = entry.get('link', '').strip()
                if not link or db.is_posted(link):
                    continue
                
                title = entry.get('title', 'No Title').strip()
                summary = entry.get('summary', '')
                summary = re.sub(r'<[^>]+>', '', summary)[:200] + "..."
                
                articles.append({
                    'title': title,
                    'link': link,
                    'summary': summary,
                    'source': feed.feed.get('title', 'Unknown'),
                    'category': category.capitalize()
                })
                
        except Exception as e:
            logger.error(f"Error processing {url}: {e}")
            continue
    
    return articles[:POSTS_PER_HOUR]

# ============ GEMINI SUMMARIZER ============
async def summarize_with_gemini(title, summary, category):
    if not USE_GEMINI:
        return summary
    
    try:
        prompt = f"""Summarize this news in 2-3 sentences (max 150 characters):
        
Title: {title}
Category: {category}
Content: {summary}

Make it engaging and highlight the key point. Return ONLY the summary, nothing else."""
        
        response = await asyncio.get_event_loop().run_in_executor(
            None, 
            gemini_model.generate_content,
            prompt
        )
        
        if response and response.text:
            return response.text.strip()[:200] + "..."
        else:
            return summary
    except Exception as e:
        logger.error(f"❌ Gemini summarization failed: {e}")
        return summary

# ============ FORMAT MESSAGE ============
async def format_post_with_gemini(article):
    emojis = {"Tech": "💻", "Crypto": "🪙", "Sports": "⚽", "World": "🌍", "Bbc": "📰", "General": "📰"}
    emoji = emojis.get(article['category'], "📰")
    
    final_summary = article['summary']
    if USE_GEMINI:
        final_summary = await summarize_with_gemini(
            article['title'], 
            article['summary'],
            article['category']
        )
    
    return f"""{emoji} <b>{article['title']}</b>

📌 {final_summary}

🔗 <a href="{article['link']}">Read Full Article</a>
🏷️ {article['category']} | 📡 {article['source']}
{'🤖 Summarized by Gemini' if USE_GEMINI else ''}
🕐 {datetime.now().strftime('%H:%M')}"""

# ============ SEND MESSAGE ============
async def send_telegram_message(bot, chat_id, message, retry_count=0):
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode='HTML',
            disable_web_page_preview=False,
            timeout=30
        )
        return True
    except RetryAfter as e:
        logger.warning(f"Rate limited. Waiting {e.retry_after}s")
        await asyncio.sleep(e.retry_after + 1)
        return await send_telegram_message(bot, chat_id, message, retry_count)
    except (TimedOut, TelegramError) as e:
        if retry_count < 3:
            wait = 5 * (retry_count + 1)
            logger.warning(f"Retry {retry_count+1} in {wait}s: {e}")
            await asyncio.sleep(wait)
            return await send_telegram_message(bot, chat_id, message, retry_count + 1)
        else:
            logger.error(f"Failed to send: {e}")
            return False

# ============ POST NEWS ============
async def post_news(bot):
    try:
        groups = db.get_all_groups()
        
        if not groups:
            logger.info("No active groups found")
            return
        
        for group in groups:
            chat_id = group['chat_id']
            categories = group['categories']
            
            articles = get_news_for_categories(categories)
            
            if not articles:
                logger.info(f"No new articles for group {chat_id}")
                continue
            
            logger.info(f"Found {len(articles)} new articles for group {chat_id}")
            
            for article in articles:
                message = await format_post_with_gemini(article)
                success = await send_telegram_message(bot, chat_id, message)
                
                if success:
                    db.mark_posted(article['link'])
                    logger.info(f"✅ Posted to {chat_id}: {article['title'][:50]}...")
                    await asyncio.sleep(3)
                else:
                    logger.error(f"❌ Failed to post to {chat_id}")
            
            db.update_last_post(chat_id)
                
        if random.random() < 0.01:
            db.cleanup_old()
            
    except Exception as e:
        logger.error(f"❌ Critical error in post_news: {e}")

# ============ BOT COMMANDS ============
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_type = update.effective_chat.type
    
    if chat_type in ["group", "supergroup"]:
        db.add_group(chat_id)
        
        welcome = f"""🤖 <b>News Bot Activated!</b>

✅ This group is now registered for news updates!

📰 <b>Categories:</b> Tech, Crypto, Sports, World, BBC
📤 <b>Posts:</b> 5 articles per hour
{"🤖 <b>Gemini AI:</b> Enabled (better summaries)" if USE_GEMINI else ""}

<b>Commands:</b>
/start - Register this group
/categories - Show/change categories
/stop - Stop news in this group
/help - Show this message

📰 First news will arrive in 10 minutes!"""
        
        await update.message.reply_text(welcome, parse_mode='HTML')
        logger.info(f"✅ Group {chat_id} registered successfully!")
    else:
        await update.message.reply_text(
            "🤖 Please add me to a group to start receiving news!",
            parse_mode='HTML'
        )

async def categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    groups = db.get_all_groups()
    current_categories = []
    for group in groups:
        if group['chat_id'] == chat_id:
            current_categories = group['categories']
            break
    
    if not current_categories:
        current_categories = DEFAULT_CATEGORIES
    
    category_list = "\n".join([f"• {cat.capitalize()}" for cat in current_categories])
    all_categories = "\n".join([f"• {cat.capitalize()}" for cat in SOURCES.keys()])
    
    await update.message.reply_text(
        f"""📰 <b>Current Categories:</b>
{category_list}

<b>Available Categories:</b>
{all_categories}

<b>Usage:</b>
To change categories, use:
/categories tech,crypto,sports

(Comma-separated, no spaces)""",
        parse_mode='HTML'
    )

async def set_categories(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    args = context.args
    if not args:
        await update.message.reply_text(
            "❌ Please specify categories.\nExample: /categories tech,crypto,sports",
            parse_mode='HTML'
        )
        return
    
    categories_str = args[0]
    new_categories = [cat.strip().lower() for cat in categories_str.split(",")]
    
    valid_categories = [cat for cat in new_categories if cat in SOURCES]
    invalid_categories = [cat for cat in new_categories if cat not in SOURCES]
    
    if not valid_categories:
        await update.message.reply_text(
            f"❌ No valid categories found. Available: {', '.join(SOURCES.keys())}",
            parse_mode='HTML'
        )
        return
    
    db.add_group(chat_id, valid_categories)
    
    category_list = "\n".join([f"• {cat.capitalize()}" for cat in valid_categories])
    await update.message.reply_text(
        f"""✅ <b>Categories Updated!</b>

New categories:
{category_list}

{'⚠️ Invalid categories ignored: ' + ', '.join(invalid_categories) if invalid_categories else ''}

News will now include only these categories!""",
        parse_mode='HTML'
    )
    logger.info(f"✅ Group {chat_id} updated categories: {valid_categories}")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    
    db.remove_group(chat_id)
    
    await update.message.reply_text(
        "🛑 <b>News Stopped!</b>\n\nThis group will no longer receive news updates.\n\nTo restart, use /start",
        parse_mode='HTML'
    )
    logger.info(f"🛑 Group {chat_id} stopped receiving news")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        """🤖 <b>News Bot Help</b>

<b>Commands:</b>
/start - Register this group for news
/categories - Show current categories
/categories tech,crypto - Change categories (comma-separated)
/stop - Stop news in this group
/help - Show this message

<b>Available Categories:</b>
• Tech - Technology news
• Crypto - Cryptocurrency news
• Sports - Sports news
• World - World news (CNN)
• BBC - BBC News

<b>Features:</b>
• Posts 5 articles per hour
• {"🤖 AI-powered summaries (Gemini)" if USE_GEMINI else "RSS feed news"}
• Auto-detects groups
• Works for multiple groups simultaneously

<b>Add this bot to any group!</b>
Simply add me and use /start""",
        parse_mode='HTML'
    )

# ============ MAIN FUNCTION ============
async def main():
    # Create application
    application = Application.builder().token(TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("categories", categories))
    application.add_handler(CommandHandler("stop", stop))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("setcategories", set_categories))
    
    # Initialize
    await application.initialize()
    await application.start()
    
    # Start polling with proper error handling
    try:
        # Delete webhook to ensure clean start
        await application.bot.delete_webhook()
        logger.info("✅ Webhook deleted")
    except Exception as e:
        logger.warning(f"Could not delete webhook: {e}")
    
    # Start polling with a shorter timeout to prevent conflicts
    await application.updater.start_polling(timeout=30, read_timeout=30)
    
    bot = application.bot
    
    # Health check
    try:
        me = await bot.get_me()
        logger.info(f"✅ Bot connected: @{me.username}")
    except Exception as e:
        logger.error(f"❌ Health check failed: {e}")
        return
    
    # Send restart notifications
    groups = db.get_all_groups()
    for group in groups:
        try:
            await bot.send_message(
                chat_id=group['chat_id'],
                text="🤖 <b>Bot Restarted!</b>\n\nI'm back online and will continue posting news.",
                parse_mode='HTML'
            )
            logger.info(f"✅ Restart notification sent to {group['chat_id']}")
        except Exception as e:
            logger.warning(f"Could not send restart notification to {group['chat_id']}: {e}")
            if "chat not found" in str(e).lower():
                db.remove_group(group['chat_id'])
    
    logger.info("🚀 Multi-Group News Bot Started Successfully!")
    logger.info(f"📡 Monitoring {len(SOURCES)} categories")
    logger.info(f"📤 Will post {POSTS_PER_HOUR} articles per hour")
    logger.info(f"👥 Active groups: {len(groups)}")
    logger.info(f"🧠 Gemini AI: {'ENABLED' if USE_GEMINI else 'DISABLED'}")
    logger.info("🔄 Bot is running...")
    
    # Main news posting loop
    consecutive_failures = 0
    max_failures = 10
    
    while True:
        try:
            await post_news(bot)
            consecutive_failures = 0
            
        except Exception as e:
            consecutive_failures += 1
            logger.error(f"❌ Main loop error ({consecutive_failures}/{max_failures}): {e}")
            
            if consecutive_failures >= max_failures:
                logger.critical("🚨 Too many failures! Sending alerts...")
                groups = db.get_all_groups()
                for group in groups:
                    try:
                        await bot.send_message(
                            chat_id=group['chat_id'],
                            text="⚠️ <b>Bot Alert:</b> Experiencing repeated errors. Check logs.",
                            parse_mode='HTML'
                        )
                    except:
                        pass
                consecutive_failures = 0
        
        await asyncio.sleep(CHECK_INTERVAL)

# ============ ENTRY POINT ============
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Bot stopped by user")
    except Exception as e:
        logger.critical(f"💀 Fatal error: {e}")
        exit(1)
