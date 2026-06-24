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
    # Soporta tanto líneas separadas como espacios (estilo shell)
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    result = []
    for line in lines:
        result.extend(shlex.split(line))
    return result


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


@app.get("/api/profile/{model_name}")
async def api_get_profile(model_name: str):
    return {"model": model_name, "profile": manager.get_profile(model_name)}


@app.post("/api/profile/{model_name}")
async def api_set_profile(
    model_name: str,
    device: str = Form("gpu"),
    n_gpu_layers: int = Form(999),
    ctx_size: int = Form(8192),
    threads: int = Form(8),
    extra_args: str = Form(""),
):
    profile = {
        "device": device.lower(),
        "n_gpu_layers": n_gpu_layers,
        "ctx_size": ctx_size,
        "threads": threads,
        "extra_args": _parse_extra_args(extra_args),
    }
    manager.set_profile(model_name, profile)
    return {"ok": True, "model": model_name, "profile": profile}


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
    return HTMLResponse(_render_profile_form(model_name, profile, extra))


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
        cards.append(f"""
        <div class="model-card bg-gray-800 border border-gray-700 rounded-xl p-4 hover:border-green-500 transition" id="model-card-{m}">
            <div class="flex items-start justify-between mb-3">
                <div>
                    <h3 class="font-semibold text-green-300 break-all">{m}</h3>
                    <div class="text-xs text-gray-400 mt-1 flex gap-2 flex-wrap">
                        <span class="px-2 py-0.5 bg-gray-700 rounded">{device}</span>
                        <span class="px-2 py-0.5 bg-gray-700 rounded">NGL: {ngl}</span>
                        <span class="px-2 py-0.5 bg-gray-700 rounded">CTX: {ctx}</span>
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


def _render_profile_form(model_name: str, profile: dict, extra: str):
    device = profile.get("device", "gpu")
    gpu_selected = "selected" if device == "gpu" else ""
    cpu_selected = "selected" if device == "cpu" else ""
    return f"""
    <form hx-post="/api/profile/{model_name}" hx-target="#profile-result" hx-swap="innerHTML"
          onsubmit="setTimeout(() => htmx.trigger('#models-list','load'), 300)">
        <h3 class="text-lg font-semibold text-green-300 mb-4 break-all">{model_name}</h3>
        <div class="space-y-4">
            <div>
                <label class="block text-sm text-gray-400 mb-1">Dispositivo</label>
                <select name="device" class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white">
                    <option value="gpu" {gpu_selected}>GPU (CUDA)</option>
                    <option value="cpu" {cpu_selected}>CPU</option>
                </select>
            </div>
            <div class="grid grid-cols-3 gap-3">
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Capas GPU</label>
                    <input type="number" name="n_gpu_layers" value="{profile.get('n_gpu_layers', 999)}"
                           class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white">
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Contexto</label>
                    <input type="number" name="ctx_size" value="{profile.get('ctx_size', 8192)}"
                           class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white">
                </div>
                <div>
                    <label class="block text-sm text-gray-400 mb-1">Hilos</label>
                    <input type="number" name="threads" value="{profile.get('threads', 8)}"
                           class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white">
                </div>
            </div>
            <div>
                <label class="block text-sm text-gray-400 mb-1">Argumentos extra (uno por línea)</label>
                <textarea name="extra_args" rows="3"
                          class="w-full bg-gray-900 border border-gray-700 rounded p-2 text-white font-mono text-sm"
                          placeholder="--flash-attn&#10;--mlock">{extra}</textarea>
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
    """
