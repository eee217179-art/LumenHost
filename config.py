import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
SERVER_IP = os.getenv("SERVER_IP", "127.0.0.1")
SUPPORT = os.getenv("SUPPORT", "@your_support")
DATA_DIR = os.path.abspath(os.getenv("DATA_DIR", "data"))

PORT_MIN, PORT_MAX = 20000, 29999  # внешние порты для сайтов и Minecraft
MAX_UPLOAD_MB = 20  # лимит Telegram Bot API на скачивание файлов

# ram/cpu — для ботов и скриптов; mc_ram/mc_cpu — для Minecraft (0 = недоступно)
PLANS = {
    "free": {"title": "Free", "projects": 1, "ram": 256, "cpu": 0.25, "mc_ram": 0, "mc_cpu": 0},
    "pro": {"title": "Pro", "projects": 3, "ram": 512, "cpu": 0.5, "mc_ram": 1536, "mc_cpu": 1.0},
    "max": {"title": "Max", "projects": 10, "ram": 1024, "cpu": 1.0, "mc_ram": 3072, "mc_cpu": 2.0},
}

TYPES = {
    "py": "🐍 Python-бот / скрипт",
    "node": "🟩 Node.js приложение",
    "site": "🌐 Сайт (HTML)",
    "mc": "⛏ Minecraft-сервер",
}

# ---- режим работы ----
# process — проекты запускаются обычными процессами (не нужен ни Docker, ни VPS: подойдёт ПК, Termux, панель с Python)
# docker  — каждый проект в изолированном контейнере (нужен сервер с Docker)
BACKEND = os.getenv("BACKEND", "process").lower()
WEB_PORT = int(os.getenv("PORT", "8080"))  # встроенный веб-сервер для сайтов (в режиме process)
BASE_URL = os.getenv("BASE_URL", f"http://{SERVER_IP}:{WEB_PORT}")
ENABLE_MC = os.getenv("ENABLE_MC", "0") == "1"  # Minecraft в режиме process: нужна Java и белый IP
# В режиме process чужой код не изолирован — по умолчанию только доверенные (админ выдаёт /give)
OPEN_SIGNUP = os.getenv("OPEN_SIGNUP", "1" if BACKEND == "docker" else "0") == "1"
