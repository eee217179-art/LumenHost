"""Работа с Docker: каждый проект пользователя — отдельный контейнер с лимитами."""
import asyncio
import os
import shlex
import shutil
import tempfile
import zipfile

try:
    import docker
    from docker.errors import NotFound
except ImportError:  # режим process работает без docker
    docker = None
    NotFound = Exception

import config

class _LazyClient:
    """Подключаемся к Docker только при первом обращении."""
    _c = None

    def __getattr__(self, name):
        if _LazyClient._c is None:
            _LazyClient._c = docker.from_env()
        return getattr(_LazyClient._c, name)


client = _LazyClient()
NET = "hostnet"
ENTRY_CANDIDATES = ("main.py", "bot.py", "app.py", "run.py")
MC_CORES = {"PAPER", "PURPUR", "VANILLA", "FABRIC"}

IMG_PY = "python:3.12-slim"
IMG_NODE = "node:20-slim"
IMG_SITE = "nginxinc/nginx-unprivileged:alpine"
IMG_MC = "itzg/minecraft-server"


def pdir(pid):
    return os.path.join(config.DATA_DIR, "projects", str(pid))


def cname(pid):
    return f"host_p{pid}"


def ensure_network():
    """Своя сеть без связи контейнеров между собой (icc=false)."""
    try:
        client.networks.get(NET)
    except NotFound:
        client.networks.create(
            NET, driver="bridge", options={"com.docker.network.bridge.enable_icc": "false"}
        )


# ---------- файлы ----------
def _safe_unzip(zpath, dest):
    root = os.path.realpath(dest)
    total = 0
    with zipfile.ZipFile(zpath) as z:
        infos = z.infolist()
        if len(infos) > 3000:
            raise ValueError("В архиве слишком много файлов")
        for i in infos:
            total += i.file_size
            if total > 200 * 1024 * 1024:
                raise ValueError("Архив слишком большой после распаковки (лимит 200 МБ)")
            target = os.path.realpath(os.path.join(dest, i.filename))
            if target != root and not target.startswith(root + os.sep):
                raise ValueError("Небезопасный путь внутри архива")
        z.extractall(dest)
    items = [x for x in os.listdir(dest) if x != "__MACOSX"]
    if len(items) == 1 and os.path.isdir(os.path.join(dest, items[0])):
        inner = os.path.join(dest, items[0])
        for n in os.listdir(inner):
            shutil.move(os.path.join(inner, n), dest)
        os.rmdir(inner)


def _fix_perms(d):
    for root, dirs, files in os.walk(d):
        for x in dirs:
            os.chmod(os.path.join(root, x), 0o755)
        for x in files:
            os.chmod(os.path.join(root, x), 0o644)


def _save_upload(pid, ptype, src, filename):
    d = pdir(pid)
    os.makedirs(d, exist_ok=True)
    name = os.path.basename(filename or "file").replace(" ", "_")
    if name.lower().endswith(".zip"):
        tmp = tempfile.mkdtemp(dir=config.DATA_DIR)
        try:
            _safe_unzip(src, tmp)
            shutil.copytree(tmp, d, dirs_exist_ok=True)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        target = {"site": "index.html", "node": "index.js"}.get(ptype, name)
        shutil.copyfile(src, os.path.join(d, target))
    _fix_perms(d)


def find_entry(d):
    for n in ENTRY_CANDIDATES:
        if os.path.exists(os.path.join(d, n)):
            return n
    pys = [f for f in os.listdir(d) if f.endswith(".py")]
    return pys[0] if len(pys) == 1 else None


# ---------- контейнеры ----------
def _remove(pid, wipe=False):
    try:
        client.containers.get(cname(pid)).remove(force=True)
    except NotFound:
        pass
    if wipe:
        shutil.rmtree(pdir(pid), ignore_errors=True)


def _deploy(p, plan, env):
    pid, t, d = p["id"], p["type"], pdir(pid)
    os.makedirs(d, exist_ok=True)
    ensure_network()
    _remove(pid)
    ram, cpu = plan["ram"], plan["cpu"]
    base = dict(
        name=cname(pid),
        detach=True,
        labels={"hosting": "1", "project": str(pid)},
        restart_policy={"Name": "unless-stopped"},
        security_opt=["no-new-privileges"],
        pids_limit=256,
        network=NET,
    )
    uid = str(os.getuid())

    if t == "py":
        entry = find_entry(d)
        if not entry:
            raise ValueError("Не найден файл запуска (main.py / bot.py / app.py / run.py).")
        cmd = (
            "[ -f requirements.txt ] && pip install --user --no-cache-dir -q -r requirements.txt; "
            f"exec python -u {shlex.quote(entry)}"
        )
        client.containers.run(
            IMG_PY, ["sh", "-c", cmd], working_dir="/app", user=uid,
            volumes={d: {"bind": "/app", "mode": "rw"}},
            environment={**env, "HOME": "/app", "PYTHONUNBUFFERED": "1",
                         "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
            mem_limit=f"{ram}m", memswap_limit=f"{ram}m", nano_cpus=int(cpu * 1e9),
            cap_drop=["ALL"], **base,
        )

    elif t == "node":
        cmd = (
            "[ -f package.json ] && npm install --no-audit --no-fund --silent; "
            "if [ -f package.json ] && grep -q '\"start\"' package.json; "
            "then exec npm start; else exec node index.js; fi"
        )
        client.containers.run(
            IMG_NODE, ["sh", "-c", cmd], working_dir="/app", user=uid,
            volumes={d: {"bind": "/app", "mode": "rw"}},
            environment={**env, "HOME": "/app"},
            mem_limit=f"{ram}m", memswap_limit=f"{ram}m", nano_cpus=int(cpu * 1e9),
            cap_drop=["ALL"], **base,
        )

    elif t == "site":
        if not os.path.exists(os.path.join(d, "index.html")):
            raise ValueError("В корне проекта нет index.html.")
        client.containers.run(
            IMG_SITE,
            volumes={d: {"bind": "/usr/share/nginx/html", "mode": "ro"}},
            ports={"8080/tcp": p["port"]},
            mem_limit="64m", memswap_limit="64m", nano_cpus=int(0.2e9),
            cap_drop=["ALL"], **base,
        )

    elif t == "mc":
        mc = plan["mc_ram"]
        if not mc:
            raise ValueError("Minecraft недоступен на текущем тарифе.")
        core = p["opts"] if p["opts"] in MC_CORES else "PAPER"
        forced = {"EULA": "TRUE", "TYPE": core, "MEMORY": f"{mc}M",
                  "UID": uid, "GID": str(os.getgid())}
        client.containers.run(
            IMG_MC,
            environment={**env, **forced},
            volumes={d: {"bind": "/data", "mode": "rw"}},
            ports={"25565/tcp": p["port"]},
            mem_limit=f"{mc + 640}m", memswap_limit=f"{mc + 640}m",
            nano_cpus=int(plan["mc_cpu"] * 1e9),
            cap_drop=["ALL"],
            cap_add=["CHOWN", "SETUID", "SETGID", "DAC_OVERRIDE", "FOWNER"],
            **base,
        )
    else:
        raise ValueError(f"Неизвестный тип проекта: {t}")


def _get(pid):
    return client.containers.get(cname(pid))


def _status(pid):
    try:
        c = _get(pid)
        c.reload()
        return c.status
    except NotFound:
        return "not_created"


def _logs(pid, n):
    try:
        return _get(pid).logs(tail=n, stdout=True, stderr=True).decode(errors="replace")
    except NotFound:
        return ""


def _stop(pid):
    try:
        _get(pid).stop(timeout=20)
    except NotFound:
        pass


def _start(pid):
    _get(pid).start()


def _running_count():
    return len(client.containers.list(filters={"label": "hosting=1"}))


# ---------- async-обёртки ----------
async def save_upload(pid, ptype, src, filename):
    await asyncio.to_thread(_save_upload, pid, ptype, src, filename)


async def deploy(p, plan, env):
    await asyncio.to_thread(_deploy, p, plan, env)


async def start(pid):
    await asyncio.to_thread(_start, pid)


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
