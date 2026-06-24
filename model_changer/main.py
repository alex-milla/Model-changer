"""Aplicación FastAPI del gestor de modelos."""
import os
import logging
from pathlib import Path

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
        cards.append(f"""
        <div class="model-card bg-gray-800 border border-gray-700 rounded-xl p-4 hover:border-green-500 transition">
            <div class="flex items-start justify-between">
                <div>
                    <h3 class="font-semibold text-green-300 break-all">{m}</h3>
                </div>
            </div>
            <form hx-post="/api/switch" hx-target="#status-panel" hx-swap="innerHTML"
                  class="mt-4" onsubmit="htmx.trigger('#status-panel','load')">
                <input type="hidden" name="model" value="{m}">
                <button type="submit"
                        class="w-full py-2 bg-green-600 hover:bg-green-500 rounded-lg font-medium transition">
                    ▶ Cargar modelo
                </button>
            </form>
        </div>
        """)
    return "\n".join(cards)

