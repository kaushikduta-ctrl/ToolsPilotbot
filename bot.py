import asyncio
import feedparser
import sqlite3
import re
import random
from datetime import datetime
import os
import logging
from telegram import Bot
from telegram.error import TelegramError, TimedOut, RetryAfter

# ============ LOGGING ============
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============ CONFIGURATION ============
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not TOKEN or not CHAT_ID:
    logger.error("❌ Missing environment variables!")
    exit(1)

SOURCES = [
    "http://rss.cnn.com/rss/cnn_topstories.rss",
    "https://feeds.bbci.co.uk/news/rss.xml",
    "https://www.reddit.com/r/technology/.rss",
    "https://www.reddit.com/r/CryptoCurrency/.rss",
    "https://www.reddit.com/r/sports/.rss"
]

POSTS_PER_HOUR = 5
CHECK_INTERVAL = 600  # 10 minutes
MAX_RETRIES = 5

# ============ DATABASE ============
class Database:
    def __init__(self):
        self.conn = sqlite3.connect('news.db', check_same_thread=False)
        self.cursor = self.conn.cursor()
        self._init_table()
    
    def _init_table(self):
        self.cursor.execute('''CREATE TABLE IF NOT EXISTS posted 
                 (link TEXT PRIMARY KEY, time TIMESTAMP)''')
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
    
    def cleanup_old(self):
        """Remove old records to keep database small"""
        try:
            self.cursor.execute("DELETE FROM posted WHERE time < datetime('now', '-7 days')")
            self.conn.commit()
        except:
            pass

db = Database()

# ============ FETCH NEWS ============
async def fetch_feed(url, retry_count=0):
    try:
        feed = feedparser.parse(url)
        if feed.entries:
            return feed
        else:
            raise Exception("No entries found")
    except Exception as e:
        if retry_count < MAX_RETRIES:
            wait = (2 ** retry_count) + random.random()
            logger.warning(f"Retry {retry_count+1} for {url} in {wait:.1f}s")
            await asyncio.sleep(wait)
            return await fetch_feed(url, retry_count + 1)
        else:
            logger.error(f"Failed to fetch {url}: {e}")
            return None

def get_news():
    articles = []
    
    for url in SOURCES:
        try:
            feed = asyncio.run_coroutine_threadsafe(fetch_feed(url), asyncio.get_event_loop())
            feed = feed.result(timeout=30)
            
            if not feed:
                continue
                
            for entry in feed.entries[:3]:
                link = entry.get('link', '').strip()
                if not link or db.is_posted(link):
                    continue
                
                title = entry.get('title', 'No Title').strip()
                summary = entry.get('summary', '')
                summary = re.sub(r'<[^>]+>', '', summary)[:200] + "..."
                
                category = "Tech"
                if "crypto" in url.lower():
                    category = "Crypto"
                elif "sports" in url.lower():
                    category = "Sports"
                
                articles.append({
                    'title': title,
                    'link': link,
                    'summary': summary,
                    'source': feed.feed.get('title', 'Unknown'),
                    'category': category
                })
                
        except Exception as e:
            logger.error(f"Error processing {url}: {e}")
            continue
    
    return articles[:POSTS_PER_HOUR]

# ============ FORMAT MESSAGE ============
def format_post(article):
    emojis = {"Tech": "💻", "Crypto": "🪙", "Sports": "⚽", "General": "📰"}
    emoji = emojis.get(article['category'], "📰")
    
    return f"""{emoji} <b>{article['title']}</b>

📌 {article['summary']}

🔗 <a href="{article['link']}">Read Full Article</a>
🏷️ {article['category']} | 📡 {article['source']}
🕐 {datetime.now().strftime('%H:%M')}"""

# ============ SEND MESSAGE ============
async def send_telegram_message(bot, message, retry_count=0):
    try:
        await bot.send_message(
            chat_id=CHAT_ID,
            text=message,
            parse_mode='HTML',
            disable_web_page_preview=False,
            timeout=30
        )
        return True
    except RetryAfter as e:
        logger.warning(f"Rate limited. Waiting {e.retry_after}s")
        await asyncio.sleep(e.retry_after + 1)
        return await send_telegram_message(bot, message, retry_count)
    except (TimedOut, TelegramError) as e:
        if retry_count < 3:
            wait = 5 * (retry_count + 1)
            logger.warning(f"Retry {retry_count+1} in {wait}s: {e}")
            await asyncio.sleep(wait)
            return await send_telegram_message(bot, message, retry_count + 1)
        else:
            logger.error(f"Failed to send: {e}")
            return False

# ============ POST NEWS ============
async def post_news(bot):
    try:
        articles = get_news()
        
        if not articles:
            logger.info("No new articles found")
            return
        
        logger.info(f"Found {len(articles)} new articles")
        
        for article in articles:
            message = format_post(article)
            success = await send_telegram_message(bot, message)
            
            if success:
                db.mark_posted(article['link'])
                logger.info(f"✅ Posted: {article['title'][:50]}...")
                await asyncio.sleep(5)
            else:
                logger.error(f"❌ Failed to post: {article['title'][:30]}...")
                
        if random.random() < 0.01:
            db.cleanup_old()
            
    except Exception as e:
        logger.error(f"❌ Critical error in post_news: {e}")

# ============ HEALTH CHECK ============
async def health_check(bot):
    try:
        me = await bot.get_me()
        logger.info(f"✅ Bot connected: @{me.username}")
        return True
    except Exception as e:
        logger.error(f"❌ Health check failed: {e}")
        return False

# ============ MAIN LOOP ============
async def main():
    bot = Bot(token=TOKEN)
    
    if not await health_check(bot):
        logger.error("❌ Bot failed health check. Exiting...")
        return
    
    logger.info("🚀 News Bot Started Successfully!")
    logger.info(f"📡 Monitoring {len(SOURCES)} sources")
    logger.info(f"📤 Will post {POSTS_PER_HOUR} articles per hour")
    logger.info("🔄 Bot is running...")
    
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
                logger.critical("🚨 Too many failures! Sending alert...")
                try:
                    await bot.send_message(
                        chat_id=CHAT_ID,
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
