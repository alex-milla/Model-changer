"""Gestión del proceso llama-server con perfiles avanzados por modelo."""
import os
import re
import time
import logging
import subprocess
import shutil
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
        self.logger = logging.getLogger("model_changer")
        self.config = self._load_config(config_path)
        self.profiles_path = Path(self.config.get("profiles_path", "model_profiles.yaml"))
        self.profiles = self._load_profiles(self.profiles_path)
        self.process: Optional[subprocess.Popen] = None
        self.current_model: Optional[str] = None
        self.start_time: Optional[float] = None

        # Asegurar directorio de logs
        log_dir = Path(self.config.get("log_dir", "./logs"))
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir = log_dir

    def _load_config(self, path: str) -> Dict[str, Any]:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _load_profiles(self, path: Path) -> Dict[str, Any]:
        if not path.exists():
            self.logger.warning("No se encontró archivo de perfiles: %s", path)
            return {"default": {}, "profiles": {}}
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        data.setdefault("default", {})
        if data.get("profiles") is None:
            data["profiles"] = {}
        return data

    def _save_profiles(self) -> None:
        with open(self.profiles_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.profiles, f, sort_keys=False, allow_unicode=True)

    def get_profile(self, model_name: str) -> Dict[str, Any]:
        """Devuelve el perfil de un modelo mezclado con los valores por defecto."""
        default = self.profiles.get("default") or {}
        profiles = self.profiles.get("profiles") or {}
        profile = profiles.get(model_name, {})
        merged = {**default, **profile}

        # Fallback a valores globales de config.yaml si no hay perfil
        merged.setdefault("device", "gpu")
        merged.setdefault("n_gpu_layers", self.config.get("n_gpu_layers", 999))
        merged.setdefault("ctx_size", self.config.get("ctx_size", 8192))
        merged.setdefault("threads", self.config.get("threads", 8))
        merged.setdefault("batch_size", 512)
        merged.setdefault("port", self.config.get("llama_port", 8080))
        merged.setdefault("host", self.config.get("llama_host", "0.0.0.0"))
        merged.setdefault("mmap", False)
        merged.setdefault("mlock", False)
        merged.setdefault("flash_attn", False)
        merged.setdefault("jinja", False)
        merged.setdefault("special", False)
        merged.setdefault("defrag_thold", 0.1)
        merged.setdefault("verbose", 2)
        merged.setdefault("parallel", 4)
        merged.setdefault("extra_args", [])

        # Seguridad: forzar flash-attn off en GPUs Pascal (sm6x)
        gpu = self.get_gpu_info()
        if gpu.get("is_pascal") and merged.get("flash_attn"):
            merged["flash_attn"] = False

        return merged

    def set_profile(self, model_name: str, profile: Dict[str, Any]) -> None:
        """Guarda o actualiza el perfil de un modelo."""
        if "profiles" not in self.profiles:
            self.profiles["profiles"] = {}
        self.profiles["profiles"][model_name] = profile
        self._save_profiles()

    def get_gpu_info(self) -> Dict[str, Any]:
        """Obtiene información básica de la GPU NVIDIA si está disponible."""
        info = {
            "available": False,
            "name": None,
            "compute_cap": None,
            "vram_mb": None,
            "is_pascal": False,
        }
        if not shutil.which("nvidia-smi"):
            return info
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, check=False, timeout=5
            )
            if result.returncode != 0 or not result.stdout.strip():
                return info
            lines = result.stdout.strip().splitlines()
            if not lines:
                return info
            parts = lines[0].split(",")
            if len(parts) >= 2:
                info["available"] = True
                info["name"] = parts[0].strip()
                info["vram_mb"] = int(parts[1].strip())

            # Detectar capability (si nvidia-smi lo soporta)
            cap_result = subprocess.run(
                ["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"],
                capture_output=True, text=True, check=False, timeout=5
            )
            if cap_result.returncode == 0 and cap_result.stdout.strip():
                cap = cap_result.stdout.strip().splitlines()[0].strip()
                info["compute_cap"] = cap
                # Pascal es compute capability 6.x
                info["is_pascal"] = cap.startswith("6.")
            else:
                # Fallback por nombre
                name_lower = (info["name"] or "").lower()
                info["is_pascal"] = any(x in name_lower for x in ["pascal", "p40", "p100", "gtx 10"])
        except Exception as exc:
            self.logger.warning("No se pudo obtener información de GPU: %s", exc)
        return info

    def get_system_info(self) -> Dict[str, Any]:
        """Obtiene información básica del sistema."""
        mem = psutil.virtual_memory()
        return {
            "total_ram_gb": round(mem.total / (1024 ** 3), 2),
            "cpu_count": psutil.cpu_count(logical=True),
            "cpu_count_physical": psutil.cpu_count(logical=False),
        }

    def get_gpu_status_text(self) -> str:
        """Devuelve la salida textual de nvidia-smi."""
        if not shutil.which("nvidia-smi"):
            return "nvidia-smi no encontrado."
        try:
            result = subprocess.run(
                ["nvidia-smi"],
                capture_output=True, text=True, check=False, timeout=10
            )
            return result.stdout or result.stderr or "nvidia-smi no devolvió salida."
        except Exception as exc:
            return f"Error al ejecutar nvidia-smi: {exc}"

    def get_pcie_status_text(self) -> str:
        """Devuelve información PCIe de la GPU NVIDIA."""
        if not shutil.which("lspci"):
            return "lspci no encontrado."
        try:
            result = subprocess.run(
                ["lspci", "-vv", "-d", "10de:"],
                capture_output=True, text=True, check=False, timeout=10
            )
            output = result.stdout or result.stderr or "lspci no devolvió salida."
            # Filtrar líneas relevantes (nombre, link capability, link status)
            lines = []
            for line in output.splitlines():
                lower = line.lower()
                if any(k in lower for k in ["nvidia", "tesla", "geforce", "vga", "3d", "link capability", "link status", "ltssm"]):
                    lines.append(line)
            return "\n".join(lines) if lines else output[:2000]
        except Exception as exc:
            return f"Error al ejecutar lspci: {exc}"

    def reset_gpu_modules(self) -> Dict[str, Any]:
        """Intenta reiniciar los módulos de NVIDIA. Requiere privilegios de root."""
        if not shutil.which("sudo"):
            return {"ok": False, "error": "sudo no disponible"}
        try:
            # Detener cualquier llama-server para liberar la GPU
            self.stop()
            # Reiniciar módulos
            result = subprocess.run(
                ["sudo", "bash", "-c", "rmmod nvidia_uvm nvidia && modprobe nvidia_uvm nvidia"],
                capture_output=True, text=True, check=False, timeout=60
            )
            if result.returncode == 0:
                return {"ok": True, "message": "Módulos NVIDIA reiniciados correctamente."}
            return {"ok": False, "error": result.stderr or result.stdout or "Error desconocido"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def restart_service(self) -> Dict[str, Any]:
        """Intenta reiniciar el servicio model-changer. Requiere privilegios de root."""
        if not shutil.which("sudo"):
            return {"ok": False, "error": "sudo no disponible"}
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "restart", "model-changer"],
                capture_output=True, text=True, check=False, timeout=60
            )
            if result.returncode == 0:
                return {"ok": True, "message": "Servicio model-changer reiniciado."}
            return {"ok": False, "error": result.stderr or result.stdout or "Error desconocido"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

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
                    host = self.config.get("llama_host", "127.0.0.1")
                    port = self.config.get("llama_port", 8080)
                    for i, arg in enumerate(cmdline):
                        if arg == "-m" and i + 1 < len(cmdline):
                            model = Path(cmdline[i + 1]).name
                        if arg == "--host" and i + 1 < len(cmdline):
                            host = cmdline[i + 1]
                        if arg == "--port" and i + 1 < len(cmdline):
                            port = cmdline[i + 1]
                        if arg.startswith("--model") and "=" in arg:
                            model = Path(arg.split("=", 1)[1]).name
                    try:
                        uptime = time.time() - proc.create_time()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        uptime = None
                    return ServerStatus(
                        running=True,
                        model=model,
                        pid=proc.info["pid"],
                        url=f"http://{host}:{port}",
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

    def build_command(self, model_name: str, profile: Optional[Dict[str, Any]] = None) -> List[str]:
        """Construye el comando de llama-server para un modelo y perfil dados."""
        bin_path = Path(self.config["llama_server_bin"])
        models_dir = Path(self.config["models_dir"])
        model_path = models_dir / model_name
        if profile is None:
            profile = self.get_profile(model_name)

        device = str(profile.get("device", "gpu")).lower()
        n_gpu_layers = int(profile.get("n_gpu_layers", 999))
        ctx_size = int(profile.get("ctx_size", 8192))
        threads = int(profile.get("threads", 8))
        batch_size = int(profile.get("batch_size", 512))
        host = str(profile.get("host", self.config.get("llama_host", "127.0.0.1")))
        port = int(profile.get("port", self.config.get("llama_port", 8080)))
        mmap = bool(profile.get("mmap", False))
        mlock = bool(profile.get("mlock", False))
        flash_attn = bool(profile.get("flash_attn", False))
        jinja = bool(profile.get("jinja", False))
        special = bool(profile.get("special", False))
        defrag_thold = float(profile.get("defrag_thold", 0.1))
        verbose = int(profile.get("verbose", 2))
        parallel = int(profile.get("parallel", 4))

        cmd = [
            str(bin_path),
            "-m", str(model_path),
            "--host", host,
            "--port", str(port),
            "-c", str(ctx_size),
            "-t", str(threads),
            "-b", str(batch_size),
            "--parallel", str(parallel),
        ]

        if device == "cpu":
            cmd.extend(["-ngl", "0"])
        else:
            cmd.extend(["-ngl", str(n_gpu_layers)])

        if not mmap:
            cmd.append("--no-mmap")
        if mlock:
            cmd.append("--mlock")
        if flash_attn:
            cmd.append("--flash-attn")
        if jinja:
            cmd.append("--jinja")
        if special:
            cmd.append("--special")

        cmd.extend(["--defrag-thold", str(defrag_thold)])

        # Verbosidad: -v o --verbose (llama-server no acepta -vv)
        if verbose > 0:
            cmd.append("--verbose")

        # Argumentos extra opcionales del perfil
        extra = profile.get("extra_args", [])
        if isinstance(extra, str):
            extra = extra.split()
        cmd.extend(extra)

        return cmd

    def _build_command(self, model_name: str) -> List[str]:
        """Compatibilidad con llamadas antiguas."""
        return self.build_command(model_name)

    def start(self, model_name: str, timeout: int = 120) -> ServerStatus:
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

        profile = self.get_profile(model_name)
        cmd = self.build_command(model_name, profile)
        port = int(profile.get("port", self.config.get("llama_port", 8080)))
        host = str(profile.get("host", self.config.get("llama_host", "127.0.0.1")))
        target_url = f"http://{host}:{port}"

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
                running=False, model=None, pid=None, url=target_url,
                uptime_seconds=None, error=f"Error al iniciar llama-server: {exc}"
            )

        self.current_model = model_name
        self.start_time = time.time()

        # Esperar a que responda el healthcheck
        health_url = f"{target_url}/health"
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
                running=False, model=None, pid=None, url=target_url,
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

        # También detener cualquier llama-server externo
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
