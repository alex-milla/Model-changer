"""Aplicación FastAPI del gestor de modelos."""
import os
import re
import logging
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import List

import httpx
import psutil
import yaml
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .server_manager import LlamaServerManager
from . import updater


# Configurar logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

CONFIG_PATH = os.environ.get("MODEL_CHANGER_CONFIG", "config.yaml")
if not Path(CONFIG_PATH).exists():
    raise FileNotFoundError(f"No se encontró el archivo de configuración: {CONFIG_PATH}")

manager = LlamaServerManager(CONFIG_PATH)

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    raw_config = yaml.safe_load(f) or {}

app = FastAPI(title="Model Changer for Llama.cpp")

templates = Jinja2Templates(directory="templates")

# Servir estáticos si existen
static_dir = Path("static")
if static_dir.exists():
    app.mount("/static", StaticFiles(directory="static"), name="static")


# --- Sistema de actualizaciones desde GitHub ---------------------------------

CONFIG_DIR = Path(CONFIG_PATH).resolve().parent
APP_ROOT = CONFIG_DIR  # la app se actualiza in-situ, donde vive config.yaml

UPDATE_REPO_OWNER = raw_config.get("update_repo_owner", "alex-milla")
UPDATE_REPO_NAME = raw_config.get("update_repo_name", "Model-changer")
VERSION_FILE = APP_ROOT / "VERSION"
BACKUP_BASE = (APP_ROOT / str(raw_config.get("backups_dir", "./backups"))).resolve()
MANIFEST_PATH = BACKUP_BASE / ".manifest.json"
GITHUB_API = "https://api.github.com"


def _github_token() -> str:
    """Token opcional: variable de entorno o fichero en ``env/`` (fuera del repo)."""
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return token
    for name in (".env.txt", "github.token", "github_token", "GITHUB_TOKEN"):
        token_file = APP_ROOT / "env" / name
        if token_file.is_file():
            token = token_file.read_text(encoding="utf-8").strip()
            if token:
                return token
    return ""


def _installed_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def _version_key(version: str):
    """Clave de comparación semver: 'v1.4.4' -> (1, 4, 4)."""
    return tuple(int(p) for p in re.findall(r"\d+", version.strip().lstrip("vV")))


def _github_headers() -> dict:
    headers = {"Accept": "application/vnd.github+json"}
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def get_latest_release() -> dict:
    """Última release publicada; fallback al último tag si no hay releases."""
    repo = f"{UPDATE_REPO_OWNER}/{UPDATE_REPO_NAME}"
    async with httpx.AsyncClient(timeout=15, headers=_github_headers(), follow_redirects=True) as client:
        resp = await client.get(f"{GITHUB_API}/repos/{repo}/releases/latest")
        if resp.status_code == 200:
            data = resp.json()
            return {
                "repo": repo,
                "tag": data.get("tag_name", ""),
                "name": data.get("name", ""),
                "published_at": data.get("published_at", ""),
                "notes": data.get("body", ""),
                "zip_url": data.get("zipball_url", ""),
                "html_url": data.get("html_url", ""),
                "source": "release",
            }
        if resp.status_code == 403:
            return {"error": "Límite de peticiones de la API de GitHub alcanzado (rate limit). "
                             "Configura un token en env/ para subirlo."}
        if resp.status_code != 404:
            return {"error": f"GitHub devolvió {resp.status_code} al consultar la release."}

        # Fallback: sin releases, usar el último tag
        resp = await client.get(f"{GITHUB_API}/repos/{repo}/tags?per_page=1")
        if resp.status_code != 200 or not resp.json():
            return {"error": f"No hay releases ni tags en {repo}."}
        tag = resp.json()[0].get("name", "")
        return {
            "repo": repo,
            "tag": tag,
            "name": tag,
            "published_at": "",
            "notes": "",
            "zip_url": f"https://github.com/{repo}/archive/refs/tags/{tag}.zip",
            "html_url": f"https://github.com/{repo}/releases",
            "source": "tag",
        }


async def do_update(release: dict) -> dict:
    """Instala la release: backup -> descarga -> extrae -> sincroniza -> VERSION."""
    backup = updater.create_backup(APP_ROOT, BACKUP_BASE)
    if not backup["ok"]:
        return {"ok": False, "error": f"No se pudo crear el backup: {backup['errors']}"}
    # Guardar copia del manifiesto dentro del backup: la restauración lo recupera
    if MANIFEST_PATH.is_file():
        try:
            shutil.copy2(MANIFEST_PATH, BACKUP_BASE / backup["dir"] / ".manifest.json")
        except OSError:
            pass

    zip_name = f"model-changer-update-{release['tag'].replace('/', '_')}"
    zip_path = Path(tempfile.gettempdir()) / f"{zip_name}.zip"
    extract_dir = zip_path.parent / f"{zip_name}-extract"
    try:
        async with httpx.AsyncClient(timeout=120, headers=_github_headers(), follow_redirects=True) as client:
            resp = await client.get(release["zip_url"])
            if resp.status_code != 200:
                return {"ok": False, "error": f"Descarga fallida (HTTP {resp.status_code})."}
            zip_path.write_bytes(resp.content)

        if zip_path.stat().st_size < 1024:
            return {"ok": False, "error": "El ZIP descargado está vacío o corrupto (< 1 KB)."}

        updater.rrmdir(extract_dir)
        updater.extract_zip(zip_path, extract_dir)
        src_root = updater.find_repo_root(extract_dir)

        sync = updater.sync_from_source(src_root, APP_ROOT, MANIFEST_PATH, release["tag"])
        if sync["errors"]:
            logging.getLogger("model_changer").warning(
                "Actualización con errores parciales: %s", sync["errors"])

        if sync["copied"] + sync["deleted"] == 0:
            return {"ok": True, "updated": False, "backup": backup["dir"],
                    "message": "No había nada que actualizar.", **sync}

        VERSION_FILE.write_text(release["tag"] + "\n", encoding="utf-8")
        return {"ok": True, "updated": True, "backup": backup["dir"],
                "message": f"Actualizado a {release['tag']}.", **sync}
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"Error de red durante la actualización: {exc}"}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"Error durante la actualización: {exc}"}
    finally:
        zip_path.unlink(missing_ok=True)
        updater.rrmdir(extract_dir)


def list_backups(limit: int = 15) -> list:
    """Backups disponibles, más recientes primero."""
    if not BACKUP_BASE.is_dir():
        return []
    found = [d.name for d in BACKUP_BASE.iterdir()
             if d.is_dir() and updater.BACKUP_NAME_RE.match(d.name)]
    return sorted(found, reverse=True)[:limit]


def status_to_dict(status):
    return {
        "running": status.running,
        "model": status.model,
        "pid": status.pid,
        "url": status.url,
        "uptime_seconds": status.uptime_seconds,
        "error": status.error,
    }


def _parse_extra_args(value: str) -> List[str]:
    """Convierte una cadena de argumentos extra en una lista."""
    if not value or not value.strip():
        return []
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    result = []
    for line in lines:
        result.extend(shlex.split(line))
    return result


def _to_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).lower() in ("true", "1", "on", "yes")


def _profile_from_form(
    device: str,
    n_gpu_layers: int,
    ctx_size: int,
    threads: int,
    batch_size: int,
    port: int,
    host: str,
    verbose: int,
    parallel: int,
    extra_args: str,
    mmap: str = Form(""),
    mlock: str = Form(""),
    flash_attn: str = Form(""),
    jinja: str = Form(""),
    special: str = Form(""),
    defrag_thold: float = Form(0.1),
) -> dict:
    return {
        "device": device.lower(),
        "n_gpu_layers": n_gpu_layers,
        "ctx_size": ctx_size,
        "threads": threads,
        "batch_size": batch_size,
        "port": port,
        "host": host,
        "mmap": _to_bool(mmap),
        "mlock": _to_bool(mlock),
        "flash_attn": _to_bool(flash_attn),
        "jinja": _to_bool(jinja),
        "special": _to_bool(special),
        "defrag_thold": defrag_thold,
        "verbose": verbose,
        "parallel": parallel,
        "extra_args": _parse_extra_args(extra_args),
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    models = manager.list_models()
    status = manager.status()
    return templates.TemplateResponse(
        request,
        "index.html",
        context={
            "models": models,
            "status": status,
            "config": raw_config,
        },
    )


@app.get("/api/models")
async def api_models():
    return {"models": manager.list_models()}


@app.get("/api/status")
async def api_status():
    return status_to_dict(manager.status())


@app.get("/api/gpu-info")
async def api_gpu_info():
    return manager.get_gpu_info()


@app.get("/api/system-info")
async def api_system_info():
    return manager.get_system_info()


@app.get("/api/gpu-status")
async def api_gpu_status():
    return {
        "nvidia_smi": manager.get_gpu_status_text(),
        "pcie": manager.get_pcie_status_text(),
    }


@app.get("/api/dashboard/gpu")
async def api_dashboard_gpu():
    return manager.get_gpu_metrics()


@app.get("/api/dashboard/system")
async def api_dashboard_system():
    mem = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=0.1)
    return {
        "cpu_percent": cpu,
        "ram_used_gb": round(mem.used / (1024 ** 3), 2),
        "ram_total_gb": round(mem.total / (1024 ** 3), 2),
        "ram_percent": mem.percent,
    }


@app.post("/api/gpu-reset")
async def api_gpu_reset():
    return manager.reset_gpu_modules()


@app.post("/api/service-restart")
async def api_service_restart():
    return manager.restart_service()


@app.get("/api/profile/{model_name}")
async def api_get_profile(model_name: str):
    return {"model": model_name, "profile": manager.get_profile(model_name)}


@app.post("/api/profile/{model_name}")
async def api_set_profile(model_name: str, profile: dict = Form(...)):
    # FastAPI no puede recibir un dict directamente de un formulario HTML;
    # los campos se reciben individualmente en el endpoint de fragmento.
    raise HTTPException(status_code=400, detail="Usa /fragments/profile-form/{model_name} con POST")


@app.post("/api/profile-save/{model_name}", response_class=HTMLResponse)
async def api_save_profile(
    model_name: str,
    device: str = Form("gpu"),
    n_gpu_layers: int = Form(999),
    ctx_size: int = Form(8192),
    threads: int = Form(8),
    batch_size: int = Form(512),
    port: int = Form(8080),
    host: str = Form("0.0.0.0"),

    verbose: int = Form(2),
    parallel: int = Form(4),
    extra_args: str = Form(""),
    mmap: str = Form(""),
    mlock: str = Form(""),
    flash_attn: str = Form(""),
    jinja: str = Form(""),
    special: str = Form(""),
    defrag_thold: float = Form(0.1),
):
    profile = _profile_from_form(
        device, n_gpu_layers, ctx_size, threads, batch_size, port, host,
        verbose, parallel, extra_args, mmap, mlock, flash_attn, jinja, special, defrag_thold
    )
    manager.set_profile(model_name, profile)
    return HTMLResponse(f'<div class="text-sm text-green-400 mb-2">✅ Guardado correctamente.</div>')


@app.get("/api/command/{model_name}")
async def api_command(model_name: str):
    profile = manager.get_profile(model_name)
    cmd = manager.build_command(model_name, profile)
    return {"model": model_name, "command": cmd, "command_str": shlex.join(cmd)}


@app.post("/api/switch")
async def api_switch(model: str = Form(...)):
    if not model:
        raise HTTPException(status_code=400, detail="Debes indicar un modelo")
    result = manager.start(model)
    return status_to_dict(result)


@app.post("/fragments/switch", response_class=HTMLResponse)
async def fragment_switch(model: str = Form(...)):
    if not model:
        return HTMLResponse(_render_status(manager.status()))
    result = manager.start(model)
    return HTMLResponse(_render_status(result))


@app.post("/api/stop")
async def api_stop():
    result = manager.stop()
    return status_to_dict(result)


@app.get("/api/config")
async def api_config():
    return {
        "llama_host": raw_config.get("llama_host", "127.0.0.1"),
        "llama_port": raw_config.get("llama_port", 8080),
        "manager_host": raw_config.get("manager_host", "127.0.0.1"),
        "manager_port": raw_config.get("manager_port", 8081),
        "models_dir": raw_config.get("models_dir", "/mnt/models"),
    }


# Fragmentos HTML para HTMX ---------------------------------------------------

@app.get("/fragments/status", response_class=HTMLResponse)
async def fragment_status():
    return HTMLResponse(_render_status(manager.status()))


@app.get("/fragments/models", response_class=HTMLResponse)
async def fragment_models():
    return HTMLResponse(_render_models_fragment(manager.list_models()))


@app.get("/fragments/profile-form/{model_name}", response_class=HTMLResponse)
async def fragment_profile_form(model_name: str):
    profile = manager.get_profile(model_name)
    extra = "\n".join(profile.get("extra_args", []))
    gpu = manager.get_gpu_info()
    system = manager.get_system_info()
    return HTMLResponse(_render_profile_form(model_name, profile, extra, gpu, system))


@app.get("/fragments/diagnostics", response_class=HTMLResponse)
async def fragment_diagnostics():
    return HTMLResponse(_render_diagnostics())


@app.get("/fragments/dashboard-page", response_class=HTMLResponse)
async def fragment_dashboard_page():
    return HTMLResponse(_render_dashboard_page())


@app.get("/fragments/dashboard", response_class=HTMLResponse)
async def fragment_dashboard():
    return HTMLResponse(_render_dashboard())


@app.get("/fragments/models-page", response_class=HTMLResponse)
async def fragment_models_page():
    status = manager.status()
    models = manager.list_models()
    return HTMLResponse(_render_models_page(status, models))


@app.get("/fragments/diagnostics-page", response_class=HTMLResponse)
async def fragment_diagnostics_page():
    return HTMLResponse(_render_diagnostics_page())


# Actualizaciones desde GitHub -----------------------------------------------

@app.get("/api/update/status")
async def api_update_status():
    release = await get_latest_release()
    installed = _installed_version()
    up_to_date = False
    if release.get("tag"):
        up_to_date = _version_key(installed) >= _version_key(release["tag"])
    return {
        "repo": f"{UPDATE_REPO_OWNER}/{UPDATE_REPO_NAME}",
        "installed_version": installed,
        "latest": release,
        "up_to_date": up_to_date,
        "backups": list_backups(),
    }


@app.get("/api/update/backups")
async def api_update_backups():
    return {"backups": list_backups()}


@app.post("/api/update/install", response_class=HTMLResponse)
async def api_update_install(force: str = Form("")):
    release = await get_latest_release()
    if not release.get("tag"):
        return HTMLResponse(_render_update_error(release.get("error", "No se encontró ninguna release.")))

    installed = _installed_version()
    is_force = _to_bool(force)
    if not is_force and _version_key(installed) >= _version_key(release["tag"]):
        return HTMLResponse(_render_update_error(f"Ya estás en la última versión ({installed})."))

    result = await do_update(release)
    if result.get("ok"):
        logging.getLogger("model_changer").info("Actualización instalada: %s", release["tag"])
    return HTMLResponse(_render_update_result(result, release))


@app.post("/api/update/restore", response_class=HTMLResponse)
async def api_update_restore(backup: str = Form(...)):
    # Anti path-traversal: nombre validado y realpath dentro de BACKUP_BASE
    if not updater.BACKUP_NAME_RE.match(backup or ""):
        return HTMLResponse(_render_update_error("Nombre de backup inválido."))
    backup_dir = (BACKUP_BASE / backup).resolve()
    try:
        backup_dir.relative_to(BACKUP_BASE.resolve())
    except ValueError:
        return HTMLResponse(_render_update_error("Backup fuera de la carpeta permitida."))
    if not backup_dir.is_dir():
        return HTMLResponse(_render_update_error(f"No existe el backup {backup}."))

    result = updater.restore_backup(backup_dir, APP_ROOT, BACKUP_BASE)
    # Recuperar el manifiesto guardado en el backup para dejar el estado consistente
    backup_manifest = backup_dir / ".manifest.json"
    if backup_manifest.is_file():
        try:
            shutil.copy2(backup_manifest, MANIFEST_PATH)
        except OSError:
            pass
    if result.get("ok"):
        logging.getLogger("model_changer").info("Backup restaurado: %s", backup)
    return HTMLResponse(_render_restore_result(result, backup))


@app.get("/fragments/updates-page", response_class=HTMLResponse)
async def fragment_updates_page():
    return HTMLResponse(_render_updates_page())


@app.get("/fragments/updates", response_class=HTMLResponse)
async def fragment_updates():
    return HTMLResponse(await _render_updates())


@app.post("/fragments/service-restart", response_class=HTMLResponse)
async def fragment_service_restart():
    """Reinicio del servicio que devuelve HTML para los paneles HTMX."""
    result = manager.restart_service()
    if result.get("ok"):
        return HTMLResponse("""
        <div class="bg-blue-900/50 border border-blue-700 rounded-xl p-4 text-blue-300">
            🔄 Reinicio del servicio solicitado. El panel se quedará unos segundos sin responder.
        </div>""")
    return HTMLResponse(f"""
    <div class="bg-red-900/50 border border-red-700 rounded-xl p-4 text-red-300">
        ⚠ No se pudo reiniciar el servicio: {result.get('error', 'requiere permisos sudo')}
    </div>""")


def _render_status(status):
    if status.running:
        uptime = f"{int(status.uptime_seconds)}s" if status.uptime_seconds else "-"
        return f"""
        <div class="flex items-center justify-between">
            <div>
                <div class="flex items-center gap-2 mb-2">
                    <span class="w-3 h-3 rounded-full bg-green-500 animate-pulse"></span>
                    <span class="text-green-400 font-semibold">En ejecución</span>
                </div>
                <p class="text-lg"><span class="text-gray-400">Modelo:</span> {status.model or 'desconocido'}</p>
                <p class="text-sm text-gray-400">PID: {status.pid or '-'} · Uptime: {uptime}</p>
                <p class="text-sm text-gray-500">URL: {status.url}</p>
            </div>
            <button hx-post="/api/stop" hx-target="#status-panel" hx-swap="innerHTML"
                    class="px-4 py-2 bg-red-600 hover:bg-red-500 rounded-lg font-medium transition">
                ⏹ Detener
            </button>
        </div>
        """
    else:
        return f"""
        <div class="flex items-center justify-between">
            <div>
                <div class="flex items-center gap-2 mb-2">
                    <span class="w-3 h-3 rounded-full bg-gray-500"></span>
                    <span class="text-gray-400 font-semibold">Parado</span>
                </div>
                <p class="text-gray-300">No hay ningún modelo cargado.</p>
                {f'<p class="text-sm text-red-400 mt-1">{status.error}</p>' if status.error else ''}
            </div>
        </div>
        """


def _render_models_fragment(models):
    if not models:
        return '<div class="col-span-full text-gray-400">No se encontraron modelos .gguf en la carpeta configurada.</div>'
    cards = []
    for m in models:
        profile = manager.get_profile(m)
        device = profile.get("device", "gpu").upper()
        ngl = profile.get("n_gpu_layers", 999)
        ctx = profile.get("ctx_size", 8192)
        port = profile.get("port", 8080)
        cards.append(f"""
        <div class="model-card bg-gray-800 border border-gray-700 rounded-xl p-4 hover:border-green-500 transition" id="model-card-{m}">
            <div class="flex items-start justify-between mb-3">
                <div>
                    <h3 class="font-semibold text-green-300 break-all">{m}</h3>
                    <div class="text-xs text-gray-400 mt-1 flex gap-2 flex-wrap">
                        <span class="px-2 py-0.5 bg-gray-700 rounded">{device}</span>
                        <span class="px-2 py-0.5 bg-gray-700 rounded">NGL: {ngl}</span>
                        <span class="px-2 py-0.5 bg-gray-700 rounded">CTX: {ctx}</span>
                        <span class="px-2 py-0.5 bg-gray-700 rounded">PORT: {port}</span>
                    </div>
                </div>
            </div>
            <div class="flex gap-2">
                <button hx-get="/fragments/profile-form/{m}" hx-target="#profile-modal-content" hx-swap="innerHTML"
                        onclick="document.getElementById('profile-modal').classList.remove('hidden')"
                        class="flex-1 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg font-medium transition text-sm">
                    ⚙ Configurar
                </button>
                <form hx-post="/fragments/switch" hx-target="#status-panel" hx-swap="innerHTML"
                      class="flex-[2]">
                    <input type="hidden" name="model" value="{m}">
                    <button type="submit"
                            class="w-full py-2 bg-green-600 hover:bg-green-500 rounded-lg font-medium transition">
                        ▶ Cargar
                    </button>
                </form>
            </div>
        </div>
        """)
    return "\n".join(cards)


def _render_profile_form(model_name: str, profile: dict, extra: str, gpu: dict, system: dict):
    device = profile.get("device", "gpu")
    gpu_selected = "selected" if device == "gpu" else ""
    cpu_selected = "selected" if device == "cpu" else ""
    auto_selected = "selected" if device == "auto" else ""

    host = profile.get("host", "0.0.0.0")
    host_local = "selected" if host == "127.0.0.1" else ""
    host_all = "selected" if host == "0.0.0.0" else ""

    mmap_checked = "checked" if profile.get("mmap", False) else ""
    mlock_checked = "checked" if profile.get("mlock", False) else ""
    flash_checked = "checked" if profile.get("flash_attn", False) else ""
    jinja_checked = "checked" if profile.get("jinja", False) else ""
    special_checked = "checked" if profile.get("special", False) else ""
    flash_disabled = "disabled" if gpu.get("is_pascal") else ""
    flash_tooltip = ""
    if gpu.get("is_pascal"):
        flash_tooltip = '<p class="text-xs text-yellow-400 mt-1">⚠ Flash Attention no es compatible con Pascal. Forzado a Off.</p>'

    ram_gb = system.get("total_ram_gb", 0)
    vram_mb = gpu.get("vram_mb") or 0
    vram_gb = round(vram_mb / 1024, 1) if vram_mb else "?"

    # Sugerencia de contexto según VRAM
    if vram_mb and vram_mb < 8192:
        ctx_suggest = 4096
    elif vram_mb and vram_mb < 16384:
        ctx_suggest = 8192
    else:
        ctx_suggest = 16384

    return f"""
    <form hx-post="/api/profile-save/{model_name}" hx-target="#profile-result" hx-swap="innerHTML">
        <h3 class="text-lg font-semibold text-green-300 mb-1 break-all">{model_name}</h3>
        <p class="text-xs text-gray-400 mb-4">VRAM detectada: {vram_gb} GB · RAM total: {ram_gb} GB</p>

        <div class="space-y-4 max-h-[70vh] overflow-y-auto pr-1">
            <div class="grid grid-cols-2 gap-3">
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Dispositivo</label>
                    <select name="device" id="pf-device-{model_name}" onchange="updateWarnings('{model_name}')"
                            class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white">
                        <option value="gpu" {gpu_selected}>GPU (CUDA)</option>
                        <option value="cpu" {cpu_selected}>CPU</option>
                        <option value="auto" {auto_selected}>Auto</option>
                    </select>
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Host</label>
                    <select name="host" class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white">
                        <option value="0.0.0.0" {host_all}>0.0.0.0 (todas)</option>
                        <option value="127.0.0.1" {host_local}>127.0.0.1 (local)</option>
                    </select>
                </div>
            </div>

            <div class="grid grid-cols-3 gap-3">
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Capas GPU</label>
                    <input type="number" name="n_gpu_layers" id="pf-ngl-{model_name}" value="{profile.get('n_gpu_layers', 999)}"
                           class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white">
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Contexto</label>
                    <input type="number" name="ctx_size" value="{profile.get('ctx_size', 8192)}"
                           class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white"
                           title="Sugerencia según VRAM: {ctx_suggest}">
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Hilos</label>
                    <input type="number" name="threads" value="{profile.get('threads', 8)}"
                           class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white">
                </div>
            </div>

            <div class="grid grid-cols-3 gap-3">
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Batch</label>
                    <input type="number" name="batch_size" value="{profile.get('batch_size', 512)}"
                           class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white">
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Puerto</label>
                    <input type="number" name="port" value="{profile.get('port', 8080)}"
                           class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white">
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Paralelismo</label>
                    <input type="number" name="parallel" value="{profile.get('parallel', 4)}"
                           class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white">
                </div>
            </div>

            <div class="grid grid-cols-2 gap-3">
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Defrag thold</label>
                    <input type="number" step="0.05" name="defrag_thold" value="{profile.get('defrag_thold', 0.1)}"
                           class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white"
                           title="0.0 desactivado. Algunas versiones de llama.cpp lo marcan como deprecated.">
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Verbose</label>
                    <select name="verbose" class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white">
                        <option value="0" {"selected" if profile.get('verbose', 2) == 0 else ""}>0 - errores</option>
                        <option value="1" {"selected" if profile.get('verbose', 2) == 1 else ""}>1 - warnings</option>
                        <option value="2" {"selected" if profile.get('verbose', 2) == 2 else ""}>2 - info</option>
                        <option value="3" {"selected" if profile.get('verbose', 2) == 3 else ""}>3 - debug</option>
                    </select>
                </div>
            </div>

            <div class="grid grid-cols-3 gap-3">
                <label class="flex items-center gap-2 bg-gray-900 border border-gray-700 rounded p-2 cursor-pointer">
                    <input type="checkbox" name="mmap" {mmap_checked} class="accent-green-500">
                    <span class="text-sm">MMAP</span>
                </label>
                <label class="flex items-center gap-2 bg-gray-900 border border-gray-700 rounded p-2 cursor-pointer">
                    <input type="checkbox" name="mlock" {mlock_checked} class="accent-green-500">
                    <span class="text-sm">MLOCK</span>
                </label>
                <label class="flex items-center gap-2 bg-gray-900 border border-gray-700 rounded p-2 cursor-pointer" title="{ 'No compatible con Pascal' if gpu.get('is_pascal') else 'Flash Attention' }">
                    <input type="checkbox" name="flash_attn" {flash_checked} {flash_disabled} class="accent-green-500">
                    <span class="text-sm">Flash Attn</span>
                </label>
            </div>
            <div class="grid grid-cols-2 gap-3">
                <label class="flex items-center gap-2 bg-gray-900 border border-gray-700 rounded p-2 cursor-pointer" title="Plantillas de chat Jinja (--jinja)">
                    <input type="checkbox" name="jinja" {jinja_checked} class="accent-green-500">
                    <span class="text-sm">Jinja</span>
                </label>
                <label class="flex items-center gap-2 bg-gray-900 border border-gray-700 rounded p-2 cursor-pointer" title="Tokens especiales (--special)">
                    <input type="checkbox" name="special" {special_checked} class="accent-green-500">
                    <span class="text-sm">Special</span>
                </label>
            </div>
            {flash_tooltip}
            <p id="pf-warn-{model_name}" class="text-xs text-yellow-400 hidden"></p>

            <div>
                <label class="block text-sm text-gray-400 mb-1">Argumentos extra (uno por línea)</label>
                <textarea name="extra_args" rows="3"
                          class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white font-mono text-sm"
                          placeholder="--temp 0.7&#10;--top-p 0.9">{extra}</textarea>
            </div>

            <div class="bg-gray-900 border border-gray-700 rounded p-3">
                <label class="block text-sm text-gray-400 mb-1">Comando generado</label>
                <div hx-get="/api/command/{model_name}" hx-trigger="load" class="text-xs font-mono text-green-300 break-all">
                    Cargando...
                </div>
            </div>
        </div>

        <div id="profile-result" class="mt-3"></div>
        <div class="flex gap-2 mt-4">
            <button type="button"
                    onclick="document.getElementById('profile-modal').classList.add('hidden')"
                    class="flex-1 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg font-medium transition">
                Cancelar
            </button>
            <button type="submit"
                    onclick="setTimeout(function() {{ document.getElementById('profile-modal').classList.add('hidden'); htmx.trigger('#models-list', 'load'); }}, 300);"
                    class="flex-1 py-2 bg-blue-600 hover:bg-blue-500 rounded-lg font-medium transition">
                💾 Guardar
            </button>
        </div>
    </form>

    <script>
        function updateWarnings(model) {{
            const device = document.getElementById('pf-device-' + model).value;
            const ngl = document.getElementById('pf-ngl-' + model).value;
            const warn = document.getElementById('pf-warn-' + model);
            let msg = '';
            if (device === 'gpu' && parseInt(ngl) === 0) {{
                msg = '⚠ ngl=0 fuerza CPU pura aunque el dispositivo sea GPU.';
            }}
            warn.textContent = msg;
            warn.classList.toggle('hidden', !msg);
        }}
        updateWarnings('{model_name}');
    </script>
    """


def _render_diagnostics():
    gpu = manager.get_gpu_info()
    nvidia_smi = manager.get_gpu_status_text()
    pcie = manager.get_pcie_status_text()
    return f"""
    <div class="space-y-4">
        <div class="flex flex-wrap gap-2">
            <button hx-get="/fragments/diagnostics" hx-target="#diagnostics-panel" hx-swap="innerHTML"
                    class="px-3 py-1 text-sm bg-gray-700 hover:bg-gray-600 rounded transition">
                🔄 Refrescar
            </button>
            <button hx-post="/api/gpu-reset" hx-target="#diagnostics-result" hx-swap="innerHTML"
                    class="px-3 py-1 text-sm bg-yellow-700 hover:bg-yellow-600 rounded transition"
                    onclick="return confirm('¿Reiniciar módulos NVIDIA? Se detendrá cualquier modelo cargado.')">
                ⚡ Reiniciar módulos NVIDIA
            </button>
            <button hx-post="/api/service-restart" hx-target="#diagnostics-result" hx-swap="innerHTML"
                    class="px-3 py-1 text-sm bg-blue-700 hover:bg-blue-600 rounded transition"
                    onclick="return confirm('¿Reiniciar el servicio Model Changer?')">
                🔄 Reiniciar servicio
            </button>
        </div>

        <div id="diagnostics-result" class="text-sm"></div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div class="bg-gray-900 border border-gray-700 rounded p-3">
                <h4 class="text-sm font-semibold text-green-300 mb-2">nvidia-smi</h4>
                <pre class="text-xs font-mono text-gray-300 overflow-x-auto whitespace-pre-wrap">{nvidia_smi}</pre>
            </div>
            <div class="bg-gray-900 border border-gray-700 rounded p-3">
                <h4 class="text-sm font-semibold text-green-300 mb-2">PCIe (GPU NVIDIA)</h4>
                <pre class="text-xs font-mono text-gray-300 overflow-x-auto whitespace-pre-wrap">{pcie}</pre>
            </div>
        </div>

        <div class="text-xs text-gray-400">
            <p>GPU detectada: {gpu.get('name') or 'No detectada'}</p>
            <p>VRAM: {gpu.get('vram_mb') or '?'} MB · Pascal: {'Sí' if gpu.get('is_pascal') else 'No'}</p>
        </div>
    </div>
    """


def _render_dashboard_page():
    return """
    <div hx-get="/fragments/dashboard" hx-trigger="load, every 2s" hx-swap="innerHTML">
        <div class="animate-pulse text-gray-400">Cargando dashboard...</div>
    </div>
    """


def _render_dashboard():
    gpu = manager.get_gpu_metrics()
    system = manager.get_system_info()
    if not gpu.get("available"):
        return '<div class="text-red-400">No se pudo obtener métricas de la GPU.</div>'

    vram_pct = int(gpu["vram_used_mb"] / gpu["vram_total_mb"] * 100) if gpu["vram_total_mb"] else 0
    vram_gb = round(gpu["vram_used_mb"] / 1024, 1)
    vram_total_gb = round(gpu["vram_total_mb"] / 1024, 1)

    proc_rows = ""
    for p in gpu.get("processes", []):
        proc_rows += f"""
        <tr class="border-b border-gray-700">
            <td class="py-1 px-2 text-xs font-mono">{p.get('pid', '')}</td>
            <td class="py-1 px-2 text-xs">{p.get('name', '')}</td>
            <td class="py-1 px-2 text-xs text-right">{p.get('sm', '0')}%</td>
            <td class="py-1 px-2 text-xs text-right">{p.get('mem', '0')}%</td>
        </tr>
        """
    if not proc_rows:
        proc_rows = '<tr><td colspan="4" class="py-2 px-2 text-xs text-gray-500">No hay procesos usando la GPU</td></tr>'

    return f"""
    <div class="space-y-4">
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div class="bg-gray-800 rounded-xl p-4 border border-gray-700">
                <div class="text-xs text-gray-400 mb-1">GPU</div>
                <div class="text-2xl font-bold text-green-400">{gpu['utilization_gpu']}%</div>
                <div class="w-full bg-gray-700 rounded-full h-2 mt-2">
                    <div class="bg-green-500 h-2 rounded-full" style="width: {gpu['utilization_gpu']}%"></div>
                </div>
            </div>
            <div class="bg-gray-800 rounded-xl p-4 border border-gray-700">
                <div class="text-xs text-gray-400 mb-1">VRAM</div>
                <div class="text-2xl font-bold text-blue-400">{vram_pct}%</div>
                <div class="text-xs text-gray-500">{vram_gb} / {vram_total_gb} GB</div>
                <div class="w-full bg-gray-700 rounded-full h-2 mt-2">
                    <div class="bg-blue-500 h-2 rounded-full" style="width: {vram_pct}%"></div>
                </div>
            </div>
            <div class="bg-gray-800 rounded-xl p-4 border border-gray-700">
                <div class="text-xs text-gray-400 mb-1">Temperatura</div>
                <div class="text-2xl font-bold text-yellow-400">{gpu['temperature_c']}°C</div>
                <div class="text-xs text-gray-500">{gpu['name'] or 'GPU'}</div>
            </div>
            <div class="bg-gray-800 rounded-xl p-4 border border-gray-700">
                <div class="text-xs text-gray-400 mb-1">Potencia</div>
                <div class="text-2xl font-bold text-purple-400">{gpu['power_draw_w']:.1f}W</div>
                <div class="text-xs text-gray-500">Límite: {gpu['power_limit_w']:.1f}W · {gpu['clock_mhz']} MHz</div>
            </div>
        </div>

        <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div class="bg-gray-800 rounded-xl p-4 border border-gray-700">
                <h3 class="text-sm font-semibold text-green-300 mb-3">Procesos en GPU</h3>
                <table class="w-full text-left">
                    <thead>
                        <tr class="border-b border-gray-600 text-xs text-gray-400">
                            <th class="py-1 px-2">PID</th>
                            <th class="py-1 px-2">Proceso</th>
                            <th class="py-1 px-2 text-right">SM</th>
                            <th class="py-1 px-2 text-right">Mem</th>
                        </tr>
                    </thead>
                    <tbody>{proc_rows}</tbody>
                </table>
            </div>

            <div class="bg-gray-800 rounded-xl p-4 border border-gray-700">
                <h3 class="text-sm font-semibold text-green-300 mb-3">Sistema</h3>
                <div class="space-y-3">
                    <div>
                        <div class="flex justify-between text-xs text-gray-400 mb-1">
                            <span>CPU</span>
                            <span id="cpu-val">--%</span>
                        </div>
                        <div class="w-full bg-gray-700 rounded-full h-2">
                            <div id="cpu-bar" class="bg-red-500 h-2 rounded-full" style="width: 0%"></div>
                        </div>
                    </div>
                    <div>
                        <div class="flex justify-between text-xs text-gray-400 mb-1">
                            <span>RAM</span>
                            <span id="ram-val">-- / -- GB</span>
                        </div>
                        <div class="w-full bg-gray-700 rounded-full h-2">
                            <div id="ram-bar" class="bg-orange-500 h-2 rounded-full" style="width: 0%"></div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>
    <script>
        fetch('/api/dashboard/system')
            .then(r => r.json())
            .then(data => {{
                document.getElementById('cpu-val').textContent = data.cpu_percent + '%';
                document.getElementById('cpu-bar').style.width = data.cpu_percent + '%';
                document.getElementById('ram-val').textContent = data.ram_used_gb + ' / ' + data.ram_total_gb + ' GB';
                document.getElementById('ram-bar').style.width = data.ram_percent + '%';
            }})
            .catch(() => {{}});
    </script>
    """


def _render_models_page(status, models):
    status_html = _render_status(status)
    models_html = _render_models_fragment(models)
    return f"""
    <div class="space-y-6">
        <section id="status-panel" class="bg-gray-800 rounded-xl p-6 shadow-lg">
            {status_html}
        </section>

        <section>
            <div class="flex items-center justify-between mb-4">
                <h2 class="text-xl font-semibold">Modelos disponibles</h2>
                <button hx-get="/fragments/models" hx-target="#models-list" hx-swap="innerHTML"
                        class="px-3 py-1 text-sm bg-gray-700 hover:bg-gray-600 rounded transition">
                    🔄 Refrescar
                </button>
            </div>
            <div id="models-list" class="grid grid-cols-1 md:grid-cols-2 gap-4">
                {models_html}
            </div>
        </section>
    </div>
    """


def _render_diagnostics_page():
    diagnostics_html = _render_diagnostics()
    return f"""
    <section class="bg-gray-800 rounded-xl p-6 shadow-lg">
        {diagnostics_html}
    </section>
    """


# Renders de la pestaña Actualizaciones ----------------------------------------

def _render_updates_page():
    return """
    <div hx-get="/fragments/updates" hx-trigger="load" hx-swap="innerHTML">
        <div class="animate-pulse text-gray-400">Comprobando actualizaciones...</div>
    </div>
    """


async def _render_updates():
    installed = _installed_version()
    release = await get_latest_release()
    error = release.get("error")
    latest_tag = release.get("tag", "")
    up_to_date = bool(latest_tag) and _version_key(installed) >= _version_key(latest_tag)

    if error:
        banner = f"""
        <div class="bg-red-900/50 border border-red-700 rounded-xl p-4 mb-4 text-red-300">
            ⚠ {error}
        </div>"""
    elif up_to_date:
        banner = f"""
        <div class="bg-green-900/50 border border-green-700 rounded-xl p-4 mb-4 text-green-300">
            ✅ Estás en la última versión ({installed}).
        </div>"""
    else:
        banner = f"""
        <div class="bg-blue-900/50 border border-blue-700 rounded-xl p-4 mb-4 text-blue-300">
            🔄 Hay una actualización disponible: <strong>{latest_tag}</strong> (instalada: {installed}).
        </div>"""

    notes = (release.get("notes") or "").strip()
    notes_html = ""
    if notes:
        import html as html_mod
        notes_html = f"""
        <details class="bg-gray-900 border border-gray-700 rounded p-3 mt-4">
            <summary class="text-sm text-green-300 cursor-pointer">Notas de la release</summary>
            <pre class="text-xs text-gray-300 whitespace-pre-wrap mt-2">{html_mod.escape(notes)}</pre>
        </details>"""

    published = (release.get("published_at") or "").replace("T", " ").replace("Z", " UTC")
    source_label = "release" if release.get("source") == "release" else "último tag"
    install_label = "Instalar actualización" if not up_to_date else "Reinstalar (forzar)"

    backups = list_backups()
    backup_rows = ""
    for name in backups:
        backup_rows += f"""
        <tr class="border-b border-gray-700">
            <td class="py-1.5 px-2 text-xs font-mono">{name}</td>
            <td class="py-1.5 px-2 text-right">
                <button hx-post="/api/update/restore" hx-vals='{{"backup": "{name}"}}' hx-target="#updates-panel" hx-swap="innerHTML"
                        class="px-2 py-1 text-xs bg-yellow-700 hover:bg-yellow-600 rounded transition"
                        onclick="return confirm('¿Restaurar el backup {name}? Se hará un backup del estado actual antes.')">
                    Restaurar
                </button>
            </td>
        </tr>"""
    if not backup_rows:
        backup_rows = '<tr><td colspan="2" class="py-2 px-2 text-xs text-gray-500">Todavía no hay backups.</td></tr>'

    return f"""
    <div id="updates-panel" class="space-y-4">
        {banner}

        <section class="bg-gray-800 rounded-xl p-6 shadow-lg">
            <div class="flex items-center justify-between mb-4">
                <h2 class="text-xl font-semibold">⬆️ Actualizaciones</h2>
                <button hx-get="/fragments/updates" hx-target="#updates-panel" hx-swap="innerHTML"
                        class="px-3 py-1 text-sm bg-gray-700 hover:bg-gray-600 rounded transition">
                    🔄 Comprobar
                </button>
            </div>

            <div class="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
                <div class="bg-gray-900 border border-gray-700 rounded p-3">
                    <span class="text-gray-400">Repositorio:</span>
                    <a class="text-blue-400 hover:underline" href="https://github.com/{release.get('repo', f'{UPDATE_REPO_OWNER}/{UPDATE_REPO_NAME}')}" target="_blank">
                        {release.get('repo', f'{UPDATE_REPO_OWNER}/{UPDATE_REPO_NAME}')}
                    </a>
                </div>
                <div class="bg-gray-900 border border-gray-700 rounded p-3">
                    <span class="text-gray-400">Versión instalada:</span>
                    <span class="font-mono text-green-300">{installed}</span>
                </div>
                <div class="bg-gray-900 border border-gray-700 rounded p-3">
                    <span class="text-gray-400">Última publicada ({source_label}):</span>
                    <span class="font-mono text-blue-300">{latest_tag or '-'}</span>
                    <span class="text-xs text-gray-500">{published}</span>
                </div>
                <div class="bg-gray-900 border border-gray-700 rounded p-3">
                    <span class="text-gray-400">Token GitHub:</span>
                    <span class="{'text-green-400' if _github_token() else 'text-gray-500'}">
                        {'configurado' if _github_token() else 'no configurado (solo necesario para repos privados o rate limit)'}
                    </span>
                </div>
            </div>
            {notes_html}

            <div class="flex flex-wrap gap-2 mt-5">
                <button hx-post="/api/update/install" hx-target="#updates-panel" hx-swap="innerHTML"
                        class="px-4 py-2 bg-green-600 hover:bg-green-500 rounded-lg font-medium transition"
                        onclick="return confirm('¿Instalar {latest_tag or 'la última versión'}? Se creará un backup antes de aplicar cambios.')">
                    ⬇️ {install_label}
                </button>
                <button hx-post="/api/update/install" hx-vals='{{"force": "true"}}' hx-target="#updates-panel" hx-swap="innerHTML"
                        class="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg font-medium transition"
                        onclick="return confirm('¿Reinstalar por la fuerza {latest_tag or 'la última versión'}? Se sobrescribirán los ficheros gestionados (con backup previo).')">
                    🔁 Reinstalar forzado
                </button>
                <button hx-post="/fragments/service-restart" hx-target="#updates-panel" hx-swap="innerHTML"
                        class="px-4 py-2 bg-blue-700 hover:bg-blue-600 rounded-lg font-medium transition"
                        onclick="return confirm('¿Reiniciar el servicio Model Changer para aplicar el nuevo código?')">
                    🔄 Reiniciar servicio
                </button>
            </div>
            <p class="text-xs text-gray-500 mt-3">
                Tras instalar, usa <em>Reiniciar servicio</em> para que el panel ejecute el código nuevo.
                Tus ficheros <code>config.yaml</code>, <code>model_profiles.yaml</code>, <code>env/</code> y <code>logs/</code> nunca se tocan.
            </p>
        </section>

        <section class="bg-gray-800 rounded-xl p-6 shadow-lg">
            <h2 class="text-lg font-semibold mb-3">📦 Backups</h2>
            <table class="w-full text-left">
                <thead>
                    <tr class="border-b border-gray-600 text-xs text-gray-400">
                        <th class="py-1 px-2">Backup</th>
                        <th class="py-1 px-2 text-right">Acción</th>
                    </tr>
                </thead>
                <tbody>{backup_rows}</tbody>
            </table>
            <p class="text-xs text-gray-500 mt-2">Se conservan los {updater.DEFAULT_MAX_BACKUPS} más recientes. Los backups excluyen tus datos de usuario.</p>
        </section>
    </div>
    """


def _render_update_error(message: str) -> str:
    return f"""
    <div id="updates-panel" class="space-y-4">
        <div class="bg-red-900/50 border border-red-700 rounded-xl p-4 text-red-300">⚠ {message}</div>
        <div>
            <button hx-get="/fragments/updates" hx-target="#updates-panel" hx-swap="innerHTML"
                    class="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg font-medium transition">
                ← Volver
            </button>
        </div>
    </div>
    """


def _render_update_result(result: dict, release: dict) -> str:
    if not result.get("ok"):
        return _render_update_error(result.get("error", "Error desconocido durante la actualización."))

    if not result.get("updated"):
        return f"""
        <div id="updates-panel" class="space-y-4">
            <div class="bg-gray-700/50 border border-gray-600 rounded-xl p-4 text-gray-200">
                ℹ {result.get('message', 'No había nada que actualizar.')}
                <span class="text-xs text-gray-400">(backup: {result.get('backup', '-')})</span>
            </div>
            <div>
                <button hx-get="/fragments/updates" hx-target="#updates-panel" hx-swap="innerHTML"
                        class="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg font-medium transition">
                    ← Volver
                </button>
            </div>
        </div>"""

    errors = result.get("errors") or []
    errors_html = ""
    if errors:
        import html as html_mod
        items = "".join(f"<li>{html_mod.escape(e)}</li>" for e in errors[:10])
        errors_html = f"""
        <div class="bg-yellow-900/50 border border-yellow-700 rounded-xl p-4 mt-3 text-yellow-300">
            <strong>Errores parciales:</strong>
            <ul class="list-disc list-inside text-xs mt-1">{items}</ul>
        </div>"""

    return f"""
    <div id="updates-panel" class="space-y-4">
        <div class="bg-green-900/50 border border-green-700 rounded-xl p-4 text-green-300">
            ✅ {result.get('message', 'Actualización instalada.')}
            <div class="text-xs text-green-200/80 mt-1">
                Copiados: {result.get('copied', 0)} · Sin cambios: {result.get('skipped', 0)} ·
                Borrados: {result.get('deleted', 0)} · Backup: {result.get('backup', '-')}
            </div>
        </div>
        {errors_html}
        <div class="flex flex-wrap gap-2">
            <button hx-post="/fragments/service-restart" hx-target="#updates-panel" hx-swap="innerHTML"
                    class="px-4 py-2 bg-blue-600 hover:bg-blue-500 rounded-lg font-medium transition"
                    onclick="return confirm('¿Reiniciar el servicio Model Changer para aplicar el código nuevo?')">
                🔄 Reiniciar servicio
            </button>
            <button hx-get="/fragments/updates" hx-target="#updates-panel" hx-swap="innerHTML"
                    class="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg font-medium transition">
                ← Volver
            </button>
        </div>
    </div>
    """


def _render_restore_result(result: dict, backup: str) -> str:
    if not result.get("ok"):
        return _render_update_error(f"No se pudo restaurar {backup}: {result.get('errors')}")

    pre = (result.get("pre") or {}).get("dir", "-")
    return f"""
    <div id="updates-panel" class="space-y-4">
        <div class="bg-green-900/50 border border-green-700 rounded-xl p-4 text-green-300">
            ✅ Backup <span class="font-mono">{backup}</span> restaurado ({result.get('copied', 0)} ficheros).
            <div class="text-xs text-green-200/80 mt-1">Backup previo del estado actual: {pre}</div>
        </div>
        <div class="flex flex-wrap gap-2">
            <button hx-post="/fragments/service-restart" hx-target="#updates-panel" hx-swap="innerHTML"
                    class="px-4 py-2 bg-blue-600 hover:bg-blue-500 rounded-lg font-medium transition"
                    onclick="return confirm('¿Reiniciar el servicio Model Changer?')">
                🔄 Reiniciar servicio
            </button>
            <button hx-get="/fragments/updates" hx-target="#updates-panel" hx-swap="innerHTML"
                    class="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg font-medium transition">
                ← Volver
            </button>
        </div>
    </div>
    """
