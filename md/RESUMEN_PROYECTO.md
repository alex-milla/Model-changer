# 📁 Resumen del proyecto — Model Changer

> Documento generado a partir del análisis del repositorio `J:\Model-changer` (rama `main`, versión v1.4.4).

## ¿Qué es este proyecto?

**Model Changer** es un panel web (FastAPI + HTMX + Tailwind) que permite **cambiar de modelo `.gguf` en un servidor llama.cpp sin usar la terminal**. Está diseñado para un equipo Chuwi con Ubuntu 24.04 y una NVIDIA Tesla P40 24 GB conectada por USB4/eGPU.

## Arquitectura

```
[Navegador] <---> [Model Changer :8081] <-- inicia/detiene --> [llama-server :8080]
                                                                |
                                                                v
                                                        [llama-ui / chat]
```

- `Model Changer` (puerto **8081**) gestiona el ciclo de vida del proceso `llama-server`.
- `llama-server` (puerto **8080**) sirve el modelo y al que apunta `llama-ui`; la URL del chat no cambia al cambiar de modelo.
- Espera a que el nuevo modelo responda en `/health` antes de darlo por cargado.

## Estructura de archivos

| Archivo / carpeta | Líneas | Descripción |
|---|---|---|
| `model_changer/main.py` | 765 | Aplicación FastAPI: rutas API, fragmentos HTML (HTMX), lógica del panel. |
| `model_changer/server_manager.py` | 568 | Clase `LlamaServerManager`: arranque/parada de `llama-server`, perfiles por modelo, guardado de perfiles, logs. |
| `model_changer/__init__.py` | — | Paquete Python. |
| `templates/index.html` | 96 | Interfaz web (Tailwind CSS por CDN + HTMX 1.9.12), pestañas Dashboard / Modelos / Diagnóstico. |
| `static/style.css` | 4 | Estilos adicionales mínimos. |
| `config.yaml` | 27 | Configuración principal: rutas de binario y modelos, puertos, perfiles y logs (rutas relativas resueltas respecto a `config.yaml`). |
| `model_profiles.yaml` | 41 | Perfiles de arranque por modelo (perfil `default` + overrides por modelo). |
| `requirements.txt` | 7 | Dependencias: fastapi, uvicorn, jinja2, pyyaml, psutil, httpx, python-multipart. |
| `install.sh` | 55 | Instalador: venv, dependencias y servicio systemd `model-changer`. |
| `model-changer.service` | — | Unidad systemd de ejemplo. |
| `run.sh` | 6 | Arranque manual sin systemd. |
| `README.md` | — | Documentación completa de instalación, uso y solución de problemas. |
| `SESSION_LOG.md` | 206 | Historial de sesiones y releases publicadas (v1.4.3, v1.4.4...). |
| `md/` | — | Esta carpeta de documentación/resúmenes. |

## Funcionalidades principales

- **Dashboard** con métricas GPU/sistema en tiempo real (uso, VRAM, temperatura, potencia, procesos) vía `psutil` y `nvidia-smi`.
- **Cambio de modelo** `.gguf` con un clic; reinicia `llama-server` y verifica `/health`.
- **Perfiles por modelo**: device (cpu/gpu/auto), `n_gpu_layers`, `ctx_size`, `threads`, `batch_size`, host/puerto, mmap, mlock, flash_attn, jinja, special, parallel, `extra_args`. Editables desde el panel; se persisten en `model_profiles.yaml`.
- **Diagnóstico GPU**: salida de `nvidia-smi`, info PCIe, reinicio de módulos NVIDIA y del propio servicio (requiere sudoers).
- **Detección de GPUs Pascal** (compute 6.1, como la Tesla P40): deshabilita Flash Attention automáticamente.

## Endpoints de la API (resumen)

- `GET /` — Panel web principal.
- `GET /api/models`, `/api/status`, `/api/config` — Estado y listado.
- `GET /api/gpu-info`, `/api/gpu-status`, `/api/system-info`, `/api/dashboard/gpu`, `/api/dashboard/system` — Métricas.
- `POST /api/switch`, `/api/stop` — Cambiar/detener modelo.
- `GET|POST /api/profile/{model}`, `POST /api/profile-save/{model}` — Perfiles.
- `GET /api/command/{model}` — Comando final que se ejecutaría.
- `POST /api/gpu-reset`, `/api/service-restart` — Diagnóstico/reinicio.
- `GET /fragments/*` — Fragmentos HTML para HTMX (dashboard, models, diagnostics, status, switch, profile-form).

## Instalación y uso

```bash
# Instalación con systemd
cd /opt/model-changer && sudo ./install.sh
sudo systemctl start model-changer

# Uso manual
source venv/bin/activate
python3 -m uvicorn model_changer.main:app --host 0.0.0.0 --port 8081
```

Panel disponible en `http://IP_DEL_CHUWI:8081`.

## Configuración clave (`config.yaml`)

```yaml
llama_server_bin: /opt/llama.cpp/build/bin/llama-server
models_dir: /mnt/models
llama_host: 0.0.0.0
llama_port: 8080
manager_host: 0.0.0.0
manager_port: 8081
profiles_path: ./model_profiles.yaml   # relativo a config.yaml
log_dir: ./logs
```

## Historial reciente (git)

| Commit | Descripción |
|---|---|
| v1.4.4 | Añadido `import psutil` faltante en `main.py`. |
| v1.4.3 | Perfiles persistentes: rutas `profiles_path`/`log_dir` resueltas respecto a `config.yaml` + logging al guardar. |
| — | Fix pestaña Modelos: devolver fragmento HTML al cambiar de modelo en vez de JSON. |
| — | Fix botón "load model": añadir ID de status-panel en la pestaña modelos. |
| — | Dashboard como pestaña principal con métricas GPU en tiempo real. |

## Notas

- Stack: **Python 3.12 / FastAPI / Uvicorn / HTMX / Tailwind (CDN) / psutil / httpx / PyYAML**.
- Licencia declarada en el README: MIT / libre.
- El README recomienda no exponer el panel (escucha en `0.0.0.0`) sin Nginx + autenticación básica o túnel VPN/SSH.
