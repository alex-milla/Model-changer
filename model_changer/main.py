"""Aplicación FastAPI del gestor de modelos."""
import os
import logging
import shlex
from pathlib import Path
from typing import List

import yaml
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .server_manager import LlamaServerManager


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
    defrag_thold: float,
    verbose: int,
    parallel: int,
    extra_args: str,
    mmap: str = Form(""),
    mlock: str = Form(""),
    flash_attn: str = Form(""),
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
    defrag_thold: float = Form(0.1),
    verbose: int = Form(2),
    parallel: int = Form(4),
    extra_args: str = Form(""),
    mmap: str = Form(""),
    mlock: str = Form(""),
    flash_attn: str = Form(""),
):
    profile = _profile_from_form(
        device, n_gpu_layers, ctx_size, threads, batch_size, port, host,
        defrag_thold, verbose, parallel, extra_args, mmap, mlock, flash_attn
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
                <form hx-post="/api/switch" hx-target="#status-panel" hx-swap="innerHTML"
                      class="flex-[2]" onsubmit="htmx.trigger('#status-panel','load')">
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
    <form hx-post="/api/profile-save/{model_name}" hx-target="#profile-result" hx-swap="innerHTML"
          hx-on="htmx:afterRequest: if(event.detail.successful && event.detail.elt === this) {{ document.getElementById('profile-modal').classList.add('hidden'); htmx.trigger('#models-list', 'load'); }}">
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
                           class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white">
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
