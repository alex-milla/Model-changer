"""Gestión del proceso llama-server."""
import os
import time
import logging
import subprocess
from pathlib import Path
from typing import List, Optional, Dict, Any
from dataclasses import dataclass

import psutil
import httpx
import yaml


@dataclass
class ServerStatus:
    running: bool
    model: Optional[str]
    pid: Optional[int]
    url: str
    uptime_seconds: Optional[float]
    error: Optional[str] = None


class LlamaServerManager:
    def __init__(self, config_path: str = "config.yaml"):
        self.config = self._load_config(config_path)
        self.process: Optional[subprocess.Popen] = None
        self.current_model: Optional[str] = None
        self.start_time: Optional[float] = None
        self.logger = logging.getLogger("model_changer")

        # Asegurar directorio de logs
        log_dir = Path(self.config.get("log_dir", "./logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir = log_dir

    def _load_config(self, path: str) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @property
    def base_url(self) -> str:
        host = self.config.get("llama_host", "127.0.0.1")
        port = self.config.get("llama_port", 8080)
        return f"http://{host}:{port}"

    def list_models(self) -> List[str]:
        models_dir = Path(self.config.get("models_dir", "/mnt/models"))
        if not models_dir.exists():
            self.logger.warning("El directorio de modelos no existe: %s", models_dir)
            return []
        models = sorted(
            p.name for p in models_dir.iterdir()
            if p.is_file() and p.suffix.lower() == ".gguf"
        )
        return models

    def status(self) -> ServerStatus:
        # Si tenemos referencia local, comprobar primero
        if self.process is not None:
            if self.process.poll() is None:
                return ServerStatus(
                    running=True,
                    model=self.current_model,
                    pid=self.process.pid,
                    url=self.base_url,
                    uptime_seconds=time.time() - self.start_time if self.start_time else None,
                )
            else:
                # Ha terminado
                self.process = None
                self.current_model = None
                self.start_time = None

        # Buscar si hay un llama-server corriendo por otro medio
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                if any("llama-server" in part for part in cmdline):
                    # Intentar averiguar el modelo
                    model = None
                    for i, arg in enumerate(cmdline):
                        if arg == "-m" and i + 1 < len(cmdline):
                            model = Path(cmdline[i + 1]).name
                            break
                        if arg.startswith("--model") and "=" in arg:
                            model = Path(arg.split("=", 1)[1]).name
                            break
                    try:
                        uptime = time.time() - proc.create_time()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        uptime = None
                    return ServerStatus(
                        running=True,
                        model=model,
                        pid=proc.info["pid"],
                        url=self.base_url,
                        uptime_seconds=uptime,
                    )
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return ServerStatus(
            running=False,
            model=None,
            pid=None,
            url=self.base_url,
            uptime_seconds=None,
        )

    def _build_command(self, model_name: str) -> List[str]:
        bin_path = Path(self.config["llama_server_bin"])
        models_dir = Path(self.config["models_dir"])
        model_path = models_dir / model_name

        cmd = [
            str(bin_path),
            "-m", str(model_path),
            "--host", str(self.config.get("llama_host", "127.0.0.1")),
            "--port", str(self.config.get("llama_port", 8080)),
            "-c", str(self.config.get("ctx_size", 8192)),
            "-ngl", str(self.config.get("n_gpu_layers", 999)),
            "-t", str(self.config.get("threads", 8)),
        ]

        # Argumentos extra opcionales del usuario
        extra = self.config.get("extra_args", [])
        if isinstance(extra, str):
            extra = extra.split()
        cmd.extend(extra)

        return cmd

    def start(self, model_name: str, timeout: int = 60) -> ServerStatus:
        if not model_name.endswith(".gguf"):
            model_name += ".gguf"

        models_dir = Path(self.config.get("models_dir", "/mnt/models"))
        model_path = models_dir / model_name
        if not model_path.exists():
            return ServerStatus(
                running=False, model=None, pid=None, url=self.base_url,
                uptime_seconds=None, error=f"No se encontró el modelo: {model_path}"
            )

        # Detener servidor actual si está corriendo
        self.stop()

        cmd = self._build_command(model_name)
        self.logger.info("Iniciando llama-server: %s", " ".join(cmd))

        # Preparar logs
        stdout_path = self.log_dir / "llama-server.out.log"
        stderr_path = self.log_dir / "llama-server.err.log"

        self.stdout_file = open(stdout_path, "a", encoding="utf-8")
        self.stderr_file = open(stderr_path, "a", encoding="utf-8")

        env = os.environ.copy()
        # Forzar uso de CUDA si está disponible (útil para Tesla P40)
        env.setdefault("CUDA_VISIBLE_DEVICES", "0")

        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=self.stdout_file,
                stderr=self.stderr_file,
                env=env,
                cwd=str(Path(self.config["llama_server_bin"]).parent),
            )
        except Exception as exc:
            return ServerStatus(
                running=False, model=None, pid=None, url=self.base_url,
                uptime_seconds=None, error=f"Error al iniciar llama-server: {exc}"
            )

        self.current_model = model_name
        self.start_time = time.time()

        # Esperar a que responda el healthcheck
        health_url = f"{self.base_url}/health"
        start_wait = time.time()
        last_error = None
        while time.time() - start_wait < timeout:
            if self.process.poll() is not None:
                break
            try:
                resp = httpx.get(health_url, timeout=2.0)
                if resp.status_code == 200:
                    return self.status()
            except Exception as exc:
                last_error = str(exc)
            time.sleep(0.5)

        # Si llegamos aquí, no arrancó correctamente
        status = self.status()
        if not status.running:
            err = last_error or "El proceso terminó antes de tiempo"
            return ServerStatus(
                running=False, model=None, pid=None, url=self.base_url,
                uptime_seconds=None, error=f"No se pudo arrancar el modelo: {err}"
            )
        return status

    def stop(self) -> ServerStatus:
        if self.process and self.process.poll() is None:
            self.logger.info("Deteniendo llama-server PID %s", self.process.pid)
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            except Exception as exc:
                self.logger.error("Error al detener proceso propio: %s", exc)
            finally:
                self._close_logs()
                self.process = None
                self.current_model = None
                self.start_time = None

        # También detener cualquier llama-server externo que use el puerto configurado
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmdline = proc.info.get("cmdline") or []
                if any("llama-server" in part for part in cmdline):
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except psutil.TimeoutExpired:
                        proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        return self.status()

    def _close_logs(self):
        try:
            if getattr(self, "stdout_file", None):
                self.stdout_file.close()
                self.stdout_file = None
        except Exception:
            pass
        try:
            if getattr(self, "stderr_file", None):
                self.stderr_file.close()
                self.stderr_file = None
        except Exception:
            pass
