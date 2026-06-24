# 🦙 Model Changer para Llama.cpp

Panel web sencillo para **cambiar de modelo .gguf** en un servidor [llama.cpp](https://github.com/ggerganov/llama.cpp) sin tener que tocar la terminal.

Diseñado para un Chuwi con **Ubuntu 24.04 + NVIDIA Tesla P40 + CUDA + llama.cpp server + llama-ui**.

## ¿Qué hace?

- Lista todos los modelos `.gguf` que tengas en una carpeta.
- Muestra el modelo que está cargado actualmente en `llama-server`.
- Permite **configurar el arranque de cada modelo** por separado: CPU/GPU, capas en GPU, contexto, hilos y argumentos extra.
- Permite **detener** el servidor y **arrancar** otro modelo con un solo clic.
- Espera a que el nuevo modelo responda en `/health` antes de darlo por cargado.
- Lleva logs de `llama-server` en `logs/`.

## Arquitectura

```
[Navegador] <---> [Model Changer :8081] <-- inicia/detiene --> [llama-server :8080]
                                                          |
                                                          v
                                                  [llama-ui / chat]
```

- `llama-server` escucha en el puerto **8080** (o el que configures).
- `Model Changer` escucha en el puerto **8081** y gestiona el proceso anterior.
- Tu `llama-ui` apunta siempre a `http://chuwi-ip:8080`; cuando cambias de modelo, el backend se reinicia pero la URL no cambia.

## Requisitos en el Chuwi

- Ubuntu 24.04
- Python 3.12 (viene por defecto)
- `llama-server` compilado con soporte CUDA
- Driver NVIDIA y CUDA funcionando (`nvidia-smi` debe mostrar la Tesla P40)
- Modelos `.gguf` en una carpeta accesible

## Estructura del proyecto

```
Model-changer/
├── config.yaml              # Configuración principal
├── model_profiles.yaml      # Perfiles de arranque por modelo
├── model_changer/
│   ├── __init__.py
│   ├── main.py              # FastAPI + interfaz web
│   └── server_manager.py    # Control de llama-server
├── templates/
│   └── index.html           # Interfaz web
├── static/
│   └── style.css
├── requirements.txt
├── install.sh               # Instalador con systemd
├── model-changer.service    # Servicio systemd de ejemplo
└── run.sh                   # Arranque manual
```

## Instalación en el Chuwi

### 1. Copiar el proyecto

Desde tu PC de desarrollo, copia esta carpeta al Chuwi, por ejemplo en `/opt/model-changer`:

```bash
# En el Chuwi (vía SSH)
sudo mkdir -p /opt/model-changer
cd /opt/model-changer
# Copia los archivos con scp/rsync o descomprime un zip
```

### 2. Editar configuración

```bash
sudo nano /opt/model-changer/config.yaml
```

Ajusta como mínimo:

```yaml
llama_server_bin: /opt/llama.cpp/build/bin/llama-server
models_dir: /mnt/models
llama_host: 0.0.0.0
llama_port: 8080
manager_host: 0.0.0.0
manager_port: 8081
profiles_path: ./model_profiles.yaml
```

> **Consejo para la Tesla P40:** deja `n_gpu_layers: 999` para que cargue todas las capas en la GPU.

### 3. Ejecutar el instalador

```bash
cd /opt/model-changer
sudo ./install.sh
```

El script:
- Instala `python3-venv` y `python3-pip`.
- Crea un entorno virtual.
- Instala dependencias.
- Crea y habilita el servicio `model-changer.service`.
- Usa tu usuario actual en el servicio systemd.

### 4. Iniciar el servicio

```bash
sudo systemctl start model-changer
sudo systemctl status model-changer
```

### 5. Abrir el panel

Desde cualquier equipo de tu red:

```
http://IP_DEL_CHUWI:8081
```

## Uso manual (sin systemd)

Si prefieres probarlo primero:

```bash
cd /opt/model-changer
source venv/bin/activate
python3 -m uvicorn model_changer.main:app --host 0.0.0.0 --port 8081
```

## Perfiles por modelo

Cada modelo puede tener su propia configuración de arranque en `model_profiles.yaml`. Si un modelo no tiene perfil propio, se usa el perfil `default`.

Ejemplo:

```yaml
default:
  device: gpu
  n_gpu_layers: 999
  ctx_size: 8192
  threads: 8
  batch_size: 512
  port: 8080
  host: 0.0.0.0
  mmap: false
  mlock: false
  flash_attn: false
  verbose: 2
  parallel: 4
  extra_args: []

profiles:
  Qwen3.5-9B-Q4_K_M.gguf:
    device: gpu
    n_gpu_layers: 999
    ctx_size: 8192
    threads: 8
    mlock: true
    extra_args:
      - "--no-mmap"

  DeepSeek-R1-Distill-Qwen-32B-Q4_K_M.gguf:
    device: gpu
    n_gpu_layers: 999
    ctx_size: 4096
    threads: 8
    mlock: true
    extra_args:
      - "--no-mmap"
```

### Campos disponibles

| Campo | Descripción |
|-------|-------------|
| `device` | `gpu`, `cpu` o `auto`. En modo `cpu` se fuerza `-ngl 0`. |
| `n_gpu_layers` | Número de capas a cargar en la GPU. `999` fuerza todas. |
| `ctx_size` | Tamaño del contexto (`-c`). |
| `threads` | Número de hilos (`-t`). |
| `batch_size` | Tamaño de batch (`-b`). |
| `port` | Puerto HTTP del `llama-server`. |
| `host` | Interfaz de red (`--host`). |
| `mmap` | `true` para `--mmap`, `false` para `--no-mmap`. |
| `mlock` | `true` para `--mlock`. |
| `flash_attn` | `true` para `--flash-attn`. Se fuerza a `false` en GPUs Pascal. |
| `jinja` | `true` para `--jinja`. Necesario para algunos modelos como Gemma 4. |
| `special` | `true` para `--special`. Maneja tokens especiales. |
| `defrag_thold` | Umbral de defragmentación KV cache (`--defrag-thold`). Puede estar deprecated. |
| `verbose` | Nivel de verbose: 0 (nada), 1+ (`--verbose`). |
| `parallel` | Slots paralelos (`--parallel`). |
| `extra_args` | Lista de argumentos extra de `llama-server` (uno por línea). |

### Optimizaciones recomendadas para Tesla P40

La Tesla P40 es una GPU Pascal (compute capability 6.1). **No soporta Flash Attention**. El panel detecta esto automáticamente y deshabilita la opción.

Configuración recomendada:

- `device: gpu` con `n_gpu_layers: 999` para cargar todo el modelo en la GPU.
- `ctx_size`: según VRAM disponible:
  - VRAM < 8 GB: máximo 4096
  - VRAM < 16 GB: máximo 8192
  - VRAM >= 16 GB: hasta 16384
- `--mlock` para evitar que el sistema mueva el modelo a swap.
- `--no-mmap` para cargar el modelo completo en RAM antes de pasarlo a la GPU (recomendado si el bus PCIe es lento).

También puedes editar los perfiles directamente desde el panel web pulsando **Configurar** en cada tarjeta de modelo. El panel muestra el comando final que se ejecutará.

## Solución de problemas

### El panel no carga

```bash
sudo journalctl -u model-changer -f
```

### `llama-server` no arranca

Revisa los logs:

```bash
tail -f /opt/model-changer/logs/llama-server.err.log
```

Verifica que el binario tiene permisos de ejecución:

```bash
ls -l /opt/llama.cpp/build/bin/llama-server
```

Y que CUDA funciona:

```bash
nvidia-smi
```

### Error de permisos al acceder a modelos

Asegúrate de que el usuario del servicio tenga permisos de lectura en la carpeta de modelos:

```bash
sudo chown -R $USER:$USER /mnt/models
```

Y actualiza `User=` y `Group=` en `/etc/systemd/system/model-changer.service` si es necesario.

### Diagnóstico GPU desde el panel

El panel incluye una sección **Diagnóstico GPU** que muestra:

- Salida de `nvidia-smi`.
- Información PCIe de la GPU.
- Botón para refrescar el estado.
- Botón para **reiniciar los módulos NVIDIA** (`nvidia_uvm`, `nvidia`).
- Botón para **reiniciar el servicio** Model Changer.

Los botones de reinicio requieren permisos `sudo`. Si el servicio no puede ejecutarlos, configura `sudoers` para que el usuario del servicio pueda ejecutar:

```bash
sudo /sbin/modprobe nvidia_uvm nvidia
sudo /usr/bin/systemctl restart model-changer
```

sin contraseña.

### Seguridad en red

Por defecto el panel escucha en `0.0.0.0`. Para protegerlo en una red compartida, ponlo detrás de **Nginx + autenticación básica** o un **túnel VPN/SSH**. No lo expongas directamente a Internet sin protección.

## Compilar llama.cpp con CUDA (resumen)

Si aún no lo tienes:

```bash
sudo apt install build-essential cmake git libcurl4-openssl-dev
git clone https://github.com/ggerganov/llama.cpp.git /opt/llama.cpp
cd /opt/llama.cpp
cmake -B build -DGGML_CUDA=ON
cmake --build build --config Release -j$(nproc)
```

El binario quedará en `/opt/llama.cpp/build/bin/llama-server`.

## Licencia

MIT / libre. Úsalo como quieras.
