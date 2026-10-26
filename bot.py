import asyncio
import html
import json
import logging
import os
import re
import time

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton as B, InlineKeyboardMarkup as KB, Message

import config
import db

if config.BACKEND == "docker":
    import dock
else:
    import runner as dock

router = Router()
ADMINS = set(config.ADMIN_IDS)
ICON = {"py": "🐍", "node": "🟩", "site": "🌐", "mc": "⛏"}
EXTS = {"py": (".zip", ".py"), "node": (".zip", ".js"), "site": (".zip", ".html"), "mc": (".zip", ".jar")}
HINTS = {
    "py": "ZIP с проектом или один .py. Нужен main.py / bot.py / app.py, зависимости — в requirements.txt.",
    "node": "ZIP с проектом (package.json) или один .js файл.",
    "site": "ZIP с сайтом (index.html в корне) или один .html файл.",
    "mc": "ZIP с файлами сервера или один server.jar (Paper/Vanilla и т.д.). Eula принимается автоматически.",
}
STATUS = {
    "running": "🟢 работает", "exited": "🔴 остановлен", "created": "⚪ создан",
    "restarting": "🟡 перезапускается", "paused": "⏸ пауза", "not_created": "⚪ не запущен",
}
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


class S(StatesGroup):
    name = State()
    file = State()
    env = State()
    upd = State()


def kb(*rows):
    return KB(inline_keyboard=[[B(text=t, callback_data=d) for t, d in r] for r in rows])


MENU = kb(
    [("➕ Создать проект", "create"), ("📂 Мои проекты", "list")],
    [("💎 Тарифы", "plans"), ("ℹ️ Помощь", "help")],
)


async def edit(c: CallbackQuery, text, markup=None):
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass


class BanMW(BaseMiddleware):
    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u and (await db.get_user(u.id))["banned"]:
            return
        return await handler(event, data)


# ---------- вспомогательное ----------
async def check_allowed(p):
    u = await db.get_user(p["user_id"])
    plan = config.PLANS[u["plan"]]
    ids = [x["id"] for x in await db.user_projects(p["user_id"])]
    if ids.index(p["id"]) >= plan["projects"]:
        raise ValueError(f"Проект не помещается в лимит тарифа {plan['title']} ({plan['projects']}).")
    if p["type"] == "mc" and not plan["mc_ram"]:
        raise ValueError("Minecraft недоступен на вашем тарифе.")
    return plan


async def do_deploy(p):
    plan = await check_allowed(p)
    await dock.deploy(p, plan, json.loads(p.get("env") or "{}"))


def site_url(p):
    if config.BACKEND == "docker":
        return f"http://{config.SERVER_IP}:{p['port']}"
    return f"{config.BASE_URL.rstrip('/')}/s/{p['id']}/"


async def card_text(p):
    st = await dock.status(p["id"])
    t = (f"{ICON[p['type']]} <b>{html.escape(p['name'])}</b> · #{p['id']}\n"
         f"Тип: {config.TYPES[p['type']]}\nСтатус: {STATUS.get(st, st)}\n")
    if p["type"] == "site":
        t += f"🔗 {site_url(p)}\n"
    if p["type"] == "mc":
        t += f"🎮 Адрес: <code>{config.SERVER_IP}:{p['port']}</code>\n"
    return t


def card_kb(p):
    i = p["id"]
    rows = [
        [("▶️ Старт", f"p:start:{i}"), ("⏹ Стоп", f"p:stop:{i}"), ("🔄 Рестарт", f"p:restart:{i}")],
        [("📜 Логи", f"p:logs:{i}"), ("⚙️ ENV", f"p:env:{i}"), ("🔃 Статус", f"p:view:{i}")],
    ]
    if p["type"] != "mc" or config.BACKEND != "docker":
        rows.append([("📁 Обновить файлы", f"p:upd:{i}")])
    rows.append([("🗑 Удалить", f"p:del:{i}")])
    rows.append([("⬅️ К проектам", "list")])
    return kb(*rows)


async def show_card(c: CallbackQuery, p):
    await edit(c, await card_text(p), card_kb(p))


async def owned(c: CallbackQuery, pid):
    p = await db.get_project(pid)
    if not p or (p["user_id"] != c.from_user.id and c.from_user.id not in ADMINS):
        await c.answer("Проект не найден", show_alert=True)
        return None
    return p


async def create_project(uid, name, ptype, opts=""):
    need_port = ptype == "mc" or (ptype == "site" and config.BACKEND == "docker")
    port = await db.free_port() if need_port else None
    return await db.add_project(uid, name, ptype, port, opts)


async def deploy_and_show(msg: Message, pid):
    p = await db.get_project(pid)
    try:
        await do_deploy(p)
    except Exception as e:
        await msg.answer(f"❌ Не удалось запустить:\n<code>{html.escape(str(e))[:700]}</code>")
    await msg.answer(await card_text(p), reply_markup=card_kb(p))


async def download(m: Message, bot: Bot, ptype):
    d = m.document
    name = d.file_name or "file"
    if not name.lower().endswith(EXTS[ptype]):
        await m.answer("Нужен файл: " + ", ".join(EXTS[ptype]))
        return None
    if d.file_size and d.file_size > config.MAX_UPLOAD_MB * 1024 * 1024:
        await m.answer(f"Файл больше {config.MAX_UPLOAD_MB} МБ.")
        return None
    os.makedirs(os.path.join(config.DATA_DIR, "tmp"), exist_ok=True)
    tmp = os.path.join(config.DATA_DIR, "tmp", f"{m.from_user.id}_{int(time.time())}_{os.path.basename(name)}")
    await bot.download(d, destination=tmp)
    return tmp


# ---------- меню ----------
@router.message(CommandStart())
@router.message(Command("cancel"))
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await db.get_user(m.from_user.id)
    await m.answer(
        "👋 <b>Хостинг-бот</b>\nРазмещай Telegram-ботов, скрипты, сайты и Minecraft-серверы прямо из Telegram.",
        reply_markup=MENU,
    )


@router.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(c, "Главное меню:", MENU)


@router.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    await edit(
        c,
        "ℹ️ <b>Как это работает</b>\n\n"
        "1. «Создать проект» → выбери тип → отправь файлы (ZIP).\n"
        "2. Бот запустит проект в изолированном контейнере.\n"
        "3. Токены и пароли задавай через ⚙️ ENV (переменные окружения), а не в коде.\n"
        "4. Логи, старт/стоп/рестарт — в карточке проекта.\n\n"
        f"Поддержка: {config.SUPPORT}",
        kb([("⬅️ Меню", "menu")]),
    )


@router.callback_query(F.data == "plans")
async def cb_plans(c: CallbackQuery):
    u = await db.get_user(c.from_user.id)
    lines = []
    for k, p in config.PLANS.items():
        mc = f", Minecraft {p['mc_ram']} МБ" if p["mc_ram"] else ", без Minecraft"
        lines.append(f"<b>{p['title']}</b> — проектов: {p['projects']}, RAM: {p['ram']} МБ, CPU: {p['cpu']}{mc}")
    until = ""
    if u["plan"] != "free" and u["plan_until"]:
        until = " (до " + time.strftime("%d.%m.%Y", time.localtime(u["plan_until"])) + ")"
    await edit(
        c,
        "💎 <b>Тарифы</b>\n\n" + "\n".join(lines)
        + f"\n\nТвой тариф: <b>{config.PLANS[u['plan']]['title']}</b>{until}\n"
        f"Купить / продлить: {config.SUPPORT}",
        kb([("⬅️ Меню", "menu")]),
    )


@router.callback_query(F.data == "list")
async def cb_list(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = await db.user_projects(c.from_user.id)
    if not rows:
        return await edit(c, "У тебя пока нет проектов.", kb([("➕ Создать проект", "create")], [("⬅️ Меню", "menu")]))
    btns = [[(f"{ICON[p['type']]} {p['name']}", f"p:view:{p['id']}")] for p in rows]
    btns += [[("➕ Создать проект", "create")], [("⬅️ Меню", "menu")]]
    await edit(c, "📂 <b>Твои проекты:</b>", kb(*btns))


# ---------- создание ----------
@router.callback_query(F.data == "create")
async def cb_create(c: CallbackQuery, state: FSMContext):
    await state.clear()
    u = await db.get_user(c.from_user.id)
    plan = config.PLANS[u["plan"]]
    if not config.OPEN_SIGNUP and c.from_user.id not in ADMINS and u["plan"] == "free":
        return await edit(
            c, "🔒 Хостинг работает в режиме доверенных пользователей. "
            f"Попроси доступ у админа: {config.SUPPORT}", kb([("⬅️ Меню", "menu")]))
    if len(await db.user_projects(c.from_user.id)) >= plan["projects"]:
        return await edit(
            c, f"⛔ Лимит проектов на тарифе {plan['title']}: {plan['projects']}.",
            kb([("💎 Тарифы", "plans")], [("⬅️ Меню", "menu")]),
        )
    rows = [[(label, f"ct:{k}")] for k, label in config.TYPES.items()] + [[("⬅️ Меню", "menu")]]
    await edit(c, "Что хочешь разместить?", kb(*rows))


@router.callback_query(F.data.startswith("ct:"))
async def cb_type(c: CallbackQuery, state: FSMContext):
    t = c.data.split(":")[1]
    plan = config.PLANS[(await db.get_user(c.from_user.id))["plan"]]
    if t == "mc" and not plan["mc_ram"]:
        return await c.answer("Minecraft доступен на тарифах Pro и Max", show_alert=True)
    if t == "mc" and not getattr(dock, "mc_supported", lambda: True)():
        return await c.answer("Minecraft на этом сервере не включён (нужна Java и ENABLE_MC=1)", show_alert=True)
    await state.set_state(S.name)
    await state.update_data(type=t)
    await edit(c, "Придумай название проекта (3–30 символов):")


@router.message(S.name, F.text)
async def got_name(m: Message, state: FSMContext):
    name = m.text.strip()
    if not 3 <= len(name) <= 30:
        return await m.answer("Название должно быть от 3 до 30 символов.")
    data = await state.get_data()
    await state.update_data(name=name)
    if data["type"] == "mc" and config.BACKEND == "docker":
        await m.answer(
            "Выбери ядро сервера:",
            reply_markup=kb([("Paper", "core:PAPER"), ("Purpur", "core:PURPUR")],
                            [("Vanilla", "core:VANILLA"), ("Fabric", "core:FABRIC")]),
        )
    else:
        await state.set_state(S.file)
        await m.answer(f"Отправь файл проекта (до {config.MAX_UPLOAD_MB} МБ).\n{HINTS[data['type']]}")


@router.callback_query(F.data.startswith("core:"))
async def cb_core(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("type") != "mc" or "name" not in data:
        return await c.answer("Начни создание заново", show_alert=True)
    await state.clear()
    await c.answer()
    await edit(c, "⏳ Создаю сервер… Первый запуск скачивает образ и мир — это 1–3 минуты.")
    pid = await create_project(c.from_user.id, data["name"], "mc", c.data.split(":")[1])
    await deploy_and_show(c.message, pid)


@router.message(S.file, F.document)
async def got_file(m: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    tmp = await download(m, bot, data["type"])
    if not tmp:
        return
    await state.clear()
    pid = await create_project(m.from_user.id, data["name"], data["type"])
    try:
        await dock.save_upload(pid, data["type"], tmp, m.document.file_name)
    except Exception as e:
        await dock.remove(pid, wipe=True)
        await db.delete_project(pid)
        return await m.answer(f"❌ Не удалось обработать файл: <code>{html.escape(str(e))[:500]}</code>")
    finally:
        os.remove(tmp)
    await m.answer("⏳ Запускаю…")
    await deploy_and_show(m, pid)


@router.message(S.file)
@router.message(S.upd)
async def need_file(m: Message):
    await m.answer("Нужно отправить файл (как документ). Отмена: /cancel")


# ---------- действия с проектом ----------
@router.callback_query(F.data.startswith("p:"))
async def project_action(c: CallbackQuery, state: FSMContext):
    _, act, pid = c.data.split(":")
    pid = int(pid)
    p = await owned(c, pid)
    if not p:
        return

    if act == "logs":
        text = (await dock.logs(pid))[-3000:]
        await c.message.answer(f"<pre>{html.escape(text) if text.strip() else 'Логов пока нет'}</pre>")
        return await c.answer()

    if act == "env":
        env = json.loads(p.get("env") or "{}")
        keys = ", ".join(env) or "пусто"
        await state.set_state(S.env)
        await state.update_data(pid=pid)
        await c.message.answer(
            f"⚙️ Переменные окружения. Сейчас: <code>{html.escape(keys)}</code>\n\n"
            "Отправь строки вида <code>KEY=VALUE</code> (по одной на строку) — они заменят текущие.\n"
            "<code>-</code> — очистить. Сообщение с секретами я удалю.\nОтмена: /cancel"
        )
        return await c.answer()

    if act == "upd":
        await state.set_state(S.upd)
        await state.update_data(pid=pid)
        await c.message.answer(f"Отправь новый файл проекта.\n{HINTS[p['type']]}\nДанные (БД и т.п.) сохранятся.")
        return await c.answer()

    if act == "del":
        await edit(c, f"Удалить проект <b>{html.escape(p['name'])}</b> вместе с файлами? Это необратимо.",
                   kb([("✅ Да, удалить", f"p:delok:{pid}")], [("❌ Отмена", f"p:view:{pid}")]))
        return await c.answer()

    if act == "delok":
        await dock.remove(pid, wipe=True)
        await db.delete_project(pid)
        await c.answer("Удалено")
        return await cb_list(c, state)

    try:
        if act == "start":
            if getattr(dock, "START_REDEPLOYS", False) or await dock.status(pid) == "not_created":
                await do_deploy(p)
            else:
                await check_allowed(p)
                await dock.start(pid)
        elif act == "stop":
            await dock.stop(pid)
        elif act == "restart":
            await c.answer("Пересоздаю контейнер…")
            await do_deploy(p)
    except Exception as e:
        await c.message.answer(f"❌ Ошибка: <code>{html.escape(str(e))[:700]}</code>")
    await c.answer()
    await show_card(c, p)


@router.message(S.env, F.text)
async def env_set(m: Message, state: FSMContext):
    pid = (await state.get_data()).get("pid")
    p = await db.get_project(pid)
    await state.clear()
    if not p or p["user_id"] != m.from_user.id:
        return
    env = {}
    if m.text.strip() != "-":
        for line in m.text.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                if ENV_KEY.match(k):
                    env[k] = v.strip()
    await db.set_env(pid, json.dumps(env))
    try:
        await m.delete()
    except TelegramBadRequest:
        pass
    await m.answer(f"✅ Сохранено переменных: {len(env)}. Нажми 🔄 Рестарт, чтобы применить.",
                   reply_markup=kb([("📂 К проекту", f"p:view:{pid}")]))


@router.message(S.upd, F.document)
async def upd_file(m: Message, state: FSMContext, bot: Bot):
    pid = (await state.get_data()).get("pid")
    p = await db.get_project(pid)
    if not p or p["user_id"] != m.from_user.id:
        return await state.clear()
    tmp = await download(m, bot, p["type"])
    if not tmp:
        return
    await state.clear()
    try:
        await dock.save_upload(pid, p["type"], tmp, m.document.file_name)
    except Exception as e:
        return await m.answer(f"❌ Не удалось обработать файл: <code>{html.escape(str(e))[:500]}</code>")
    finally:
        os.remove(tmp)
    await m.answer("⏳ Файлы обновлены, перезапускаю…")
    await deploy_and_show(m, pid)


# ---------- админка ----------
@router.message(Command("give"), F.from_user.id.in_(ADMINS))
async def adm_give(m: Message, bot: Bot):
    try:
        _, uid, plan, days = m.text.split()
        uid, days = int(uid), int(days)
        assert plan in config.PLANS and days > 0
    except Exception:
        return await m.answer("Использование: /give <user_id> <free|pro|max> <дней>")
    until = 0 if plan == "free" else int(time.time()) + days * 86400
    await db.set_plan(uid, plan, until)
    await m.answer(f"✅ {uid}: тариф {plan} на {days} дн.")
    try:
        await bot.send_message(uid, f"💎 Тебе выдан тариф <b>{config.PLANS[plan]['title']}</b> на {days} дн.")
    except Exception:
        pass


@router.message(Command("ban"), F.from_user.id.in_(ADMINS))
@router.message(Command("unban"), F.from_user.id.in_(ADMINS))
async def adm_ban(m: Message):
    parts = m.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        return await m.answer("Использование: /ban <id> или /unban <id>")
    ban = 1 if parts[0].startswith("/ban") else 0
    await db.set_ban(int(parts[1]), ban)
    await m.answer("🚫 Заблокирован" if ban else "✅ Разблокирован")


@router.message(Command("stats"), F.from_user.id.in_(ADMINS))
async def adm_stats(m: Message):
    users, projects, paid = await db.stats()
    running = await dock.running_count()
    await m.answer(f"📊 Пользователей: {users} (платных: {paid})\nПроектов: {projects}\nКонтейнеров запущено: {running}")


# ---------- фоновые задачи ----------
async def expiry_loop(bot: Bot):
    while True:
        try:
            for u in await db.expired_users():
                await db.set_plan(u["id"], "free", 0)
                for p in await db.user_projects(u["id"]):
                    await dock.remove(p["id"], wipe=False)  # файлы остаются
                try:
                    await bot.send_message(
                        u["id"],
                        "⌛ Тариф закончился. Проекты остановлены — продли тариф "
                        f"({config.SUPPORT}) или запусти разрешённые проекты заново.",
                    )
                except Exception:
                    pass
        except Exception:
            logging.exception("expiry_loop")
        await asyncio.sleep(600)


async def resume_all():
    """После перезапуска бота поднимаем проекты, которые были запущены (режим process)."""
    if not hasattr(dock, "wanted"):
        return
    for p in await db.all_projects():
        if dock.wanted(p["id"]):
            try:
                await do_deploy(p)
            except Exception:
                logging.exception("resume project %s", p["id"])


async def main():
    logging.basicConfig(level=logging.INFO)
    if not config.BOT_TOKEN:
        raise SystemExit("Задай BOT_TOKEN в .env")
    await db.init()
    await asyncio.to_thread(dock.ensure_network)
    if hasattr(dock, "start_web"):
        await dock.start_web()
    await resume_all()
    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(BanMW())
    dp.include_router(router)
    task = asyncio.create_task(expiry_loop(bot))  # noqa: F841
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
