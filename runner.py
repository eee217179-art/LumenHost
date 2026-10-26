"""Бэкенд без Docker: проекты запускаются обычными процессами.
Подходит для домашнего ПК, Termux, панелей с Python — VPS не нужен.
ВНИМАНИЕ: код пользователей не изолирован, запускай только доверенным людям."""
import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

from aiohttp import web

import config
from dock import _save_upload, find_entry, pdir, save_upload  # noqa: F401

START_REDEPLOYS = True  # «Старт» = пересборка/перезапуск процесса
PROCS = {}
STATE = os.path.join(config.DATA_DIR, "state.json")
LOG_MAX = 5 * 1024 * 1024
_lock = threading.Lock()
POSIX = os.name == "posix"


def ensure_network():
    pass


def mc_supported():
    return config.ENABLE_MC and shutil.which("java") is not None


# ---------- состояние (что должно работать после перезапуска бота) ----------
def _state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _set(pid, value):
    with _lock:
        s = _state()
        if value is None:
            s.pop(str(pid), None)
        else:
            s[str(pid)] = value
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(s, f)


def wanted(pid):
    return _state().get(str(pid)) is True


def _is_site(pid):
    return os.path.exists(os.path.join(pdir(pid), ".site"))


# ---------- запуск ----------
def _clean_env(d, extra):
    """Не отдаём проектам переменные хоста (BOT_TOKEN и т.д.)."""
    env = {"PATH": os.environ.get("PATH", ""), "HOME": d, "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1"}
    for k in ("SYSTEMROOT", "TEMP", "TMP"):
        if k in os.environ:
            env[k] = os.environ[k]
    env.update(extra)
    return env


def _limits(ram_mb):
    if not POSIX:
        return None

    def f():
        import resource
        lim = ram_mb * 1024 * 1024 * 4  # адресное пространство с запасом
        resource.setrlimit(resource.RLIMIT_AS, (lim, lim))

    return f


def _install(cmd, d, env, log):
    try:
        subprocess.run(cmd, cwd=d, env=env, stdout=log, stderr=subprocess.STDOUT,
                       stdin=subprocess.DEVNULL, timeout=900, check=True)
    except subprocess.CalledProcessError:
        raise ValueError("Не удалось установить зависимости — смотри 📜 Логи.")
    except subprocess.TimeoutExpired:
        raise ValueError("Установка зависимостей заняла больше 15 минут.")


def _set_mc_port(d, port):
    path = os.path.join(d, "server.properties")
    text = open(path, encoding="utf-8").read() if os.path.exists(path) else ""
    if re.search(r"^server-port=.*$", text, re.M):
        text = re.sub(r"^server-port=.*$", f"server-port={port}", text, flags=re.M)
    else:
        text += f"server-port={port}\n"
    open(path, "w", encoding="utf-8").write(text)


def _deploy(p, plan, env):
    pid, t = p["id"], p["type"]
    d = pdir(pid)
    os.makedirs(d, exist_ok=True)
    _stop(pid, keep_state=True)

    logp = os.path.join(d, "run.log")
    if os.path.exists(logp) and os.path.getsize(logp) > LOG_MAX:
        open(logp, "wb").close()

    if t == "site":
        if not os.path.exists(os.path.join(d, "index.html")):
            raise ValueError("В корне проекта нет index.html.")
        open(os.path.join(d, ".site"), "w").close()
        _set(pid, True)
        return

    with open(logp, "ab") as log:
        log.write(f"\n=== запуск {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n".encode())
        log.flush()
        penv = _clean_env(d, env)
        pre = None

        if t == "py":
            entry = find_entry(d)
            if not entry:
                raise ValueError("Не найден файл запуска (main.py / bot.py / app.py / run.py).")
            deps = os.path.join(d, ".deps")
            if os.path.exists(os.path.join(d, "requirements.txt")):
                _install([sys.executable, "-m", "pip", "install", "--no-cache-dir", "-q",
                          "--target", deps, "-r", "requirements.txt"], d, penv, log)
            penv["PYTHONPATH"] = deps
            cmd = [sys.executable, "-u", entry]
            pre = _limits(plan["ram"])

        elif t == "node":
            node, npm = shutil.which("node"), shutil.which("npm")
            if not node:
                raise ValueError("На сервере не установлен Node.js.")
            cmd = [node, "index.js"]
            pj = os.path.join(d, "package.json")
            if os.path.exists(pj):
                if npm:
                    _install([npm, "install", "--no-audit", "--no-fund", "--silent"], d, penv, log)
                try:
                    if npm and "start" in json.load(open(pj, encoding="utf-8")).get("scripts", {}):
                        cmd = [npm, "start"]
                except Exception:
                    pass

        elif t == "mc":
            if not mc_supported():
                raise ValueError("Minecraft недоступен: нужна Java и ENABLE_MC=1.")
            mc = plan["mc_ram"]
            if not mc:
                raise ValueError("Minecraft недоступен на текущем тарифе.")
            jars = [f for f in os.listdir(d) if f.endswith(".jar")]
            jar = "server.jar" if "server.jar" in jars else (jars[0] if len(jars) == 1 else None)
            if not jar:
                raise ValueError("Нужен серверный .jar (например server.jar) в корне проекта.")
            open(os.path.join(d, "eula.txt"), "w").write("eula=true\n")
            _set_mc_port(d, p["port"])
            cmd = [shutil.which("java"), f"-Xmx{mc}M", f"-Xms{min(mc, 512)}M", "-jar", jar, "nogui"]
        else:
            raise ValueError(f"Неизвестный тип проекта: {t}")

        kw = {"start_new_session": True, "preexec_fn": pre} if POSIX else {}
        proc = subprocess.Popen(cmd, cwd=d, env=penv, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, **kw)
    PROCS[pid] = proc
    _set(pid, True)


def _kill(proc, sig):
    try:
        if POSIX:
            os.killpg(proc.pid, sig)
        elif sig == signal.SIGTERM:
            proc.terminate()
        else:
            proc.kill()
    except ProcessLookupError:
        pass


def _stop(pid, keep_state=False):
    proc = PROCS.pop(pid, None)
    if proc and proc.poll() is None:
        _kill(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            _kill(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
    if not keep_state and str(pid) in _state():
        _set(pid, False)


def _remove(pid, wipe=False):
    _stop(pid, keep_state=True)
    _set(pid, None)
    if wipe:
        shutil.rmtree(pdir(pid), ignore_errors=True)


def _status(pid):
    s = _state().get(str(pid))
    if s is None:
        return "not_created"
    if _is_site(pid):
        return "running" if s else "exited"
    proc = PROCS.get(pid)
    return "running" if proc and proc.poll() is None else "exited"


def _logs(pid, n):
    path = os.path.join(pdir(pid), "run.log")
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 12000))
        data = f.read()
    return "\n".join(data.decode(errors="replace").splitlines()[-n:])


def _running_count():
    procs = sum(1 for p in PROCS.values() if p.poll() is None)
    sites = sum(1 for k, v in _state().items() if v is True and _is_site(int(k)))
    return procs + sites


# ---------- async-интерфейс (как у dock.py) ----------
async def deploy(p, plan, env):
    await asyncio.to_thread(_deploy, p, plan, env)


async def start(pid):
    raise RuntimeError("В режиме process «Старт» делает deploy")


async def stop(pid):
    await asyncio.to_thread(_stop, pid)


async def remove(pid, wipe=False):
    await asyncio.to_thread(_remove, pid, wipe)


async def status(pid):
    return await asyncio.to_thread(_status, pid)


async def logs(pid, n=60):
    return await asyncio.to_thread(_logs, pid, n)


async def running_count():
    return await asyncio.to_thread(_running_count)


# ---------- веб-сервер для сайтов: /s/<id>/ ----------
async def _serve(request):
    pid = request.match_info["pid"]
    tail = request.match_info["tail"] or "index.html"
    if not pid.isdigit() or not wanted(int(pid)) or not _is_site(int(pid)):
        raise web.HTTPNotFound()
    if any(part.startswith(".") for part in tail.split("/")):
        raise web.HTTPNotFound()
    root = os.path.realpath(pdir(int(pid)))
    path = os.path.realpath(os.path.join(root, tail))
    if os.path.isdir(path):
        path = os.path.join(path, "index.html")
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def _redir(request):
    raise web.HTTPFound(f"/s/{request.match_info['pid']}/")


async def _root(request):
    return web.Response(text="ok")


async def start_web():
    app = web.Application()
    app.router.add_get("/", _root)
    app.router.add_get("/s/{pid}", _redir)
    app.router.add_get("/s/{pid}/{tail:.*}", _serve)
    r = web.AppRunner(app)
    await r.setup()
    await web.TCPSite(r, "0.0.0.0", config.WEB_PORT).start()
