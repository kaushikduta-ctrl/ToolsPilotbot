# 🤖 Telegram News Bot

Auto-posts Tech, Crypto, and Sports news from BBC, CNN, and Reddit to your Telegram group.

## 🚀 Features
- Posts 5 articles per hour
- Supports Tech, Crypto, and Sports categories
- Auto-detects duplicate posts
- Error handling with auto-retry
- Never crashes (production-ready)

## 📦 Deployment on Railway

1. Fork this repo to GitHub
2. Sign up at railway.app
3. Click "New Project" → "Deploy from GitHub repo"
4. Add environment variables:
   - `TELEGRAM_TOKEN` = Your bot token from @BotFather
   - `CHAT_ID` = Your group ID (negative number)

## 🔧 Setup Telegram
1. Message @BotFather → `/newbot` → Get token
2. Add bot to your group
3. Visit: `https://api.telegram.org/bot<TOKEN>/getUpdates` to get CHAT_ID

## 📊 Monitoring
Check Railway logs to see posts in real-time.

## 📝 License
MIT
