"""Lógica pura del actualizador desde GitHub.

Port del patrón de M4-CMS (`helpers/updater.php`) a Python:

- Manifiesto SHA-256 de los ficheros gestionados (``backups/.manifest.json``).
- Sincronización desde un ZIP de release: copia solo nuevos/cambiados y borra
  los gestionados que ya no existen en la release.
- Nunca toca datos del usuario (``config.yaml``, ``model_profiles.yaml``,
  ``.env``, ``env/``, ``logs/``).
- Backup del código antes de cada operación, restaurable desde el panel.

Solo usa stdlib y no hace red: se puede probar desde CLI.
"""
import hashlib
import json
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

# Nombre válido de carpeta de backup (anti path-traversal al restaurar).
# El sufijo opcional evita colisiones cuando se crean backups en el mismo segundo.
BACKUP_NAME_RE = re.compile(r"^backup_\d{8}_\d{6}(?:_\d+)?$")
DEFAULT_MAX_BACKUPS = 15

# Directorios que nunca se copian ni se respaldan (cortados por primer segmento).
DEFAULT_SKIP_DIRS = {
    ".git", ".github", "__pycache__", "venv", ".venv", "env",
    "logs", "node_modules", "backups", ".release", ".idea", ".vscode",
}
# Prefijos de ruta relativa excluidos (p. ej. todas las backups).
DEFAULT_SKIP_PREFIXES = ("backups/",)
# Estado del usuario: nunca se sobrescriben ni entran en los backups.
DEFAULT_SENSITIVE = (
    "config.yaml", "model_profiles.yaml", ".env", ".github_token",
    "SESSION_LOG.md",
)


def is_excluded(
    rel: str,
    sensitive=DEFAULT_SENSITIVE,
    skip_dirs=DEFAULT_SKIP_DIRS,
    skip_prefixes=DEFAULT_SKIP_PREFIXES,
) -> bool:
    """Devuelve True si una ruta relativa (con '/') debe ignorarse."""
    if rel in sensitive:
        return True
    if rel.split("/", 1)[0] in skip_dirs:
        return True
    return any(rel.startswith(p) for p in skip_prefixes)


def sha256_file(path: Path) -> str:
    """SHA-256 de un fichero, leyendo por bloques."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rrmdir(path: Path) -> None:
    """Borrado recursivo tolerante a fallos."""
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def build_manifest(
    root: Path,
    sensitive=DEFAULT_SENSITIVE,
    skip_dirs=DEFAULT_SKIP_DIRS,
    skip_prefixes=DEFAULT_SKIP_PREFIXES,
) -> dict:
    """Construye ``{ruta_relativa: sha256}`` de un directorio, ordenado."""
    root = Path(root)
    files = {}
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if is_excluded(rel, sensitive, skip_dirs, skip_prefixes):
            continue
        files[rel] = sha256_file(p)
    return dict(sorted(files.items()))


def load_manifest(path: Path) -> dict:
    """Carga el manifiesto; acepta el wrapper ``{version, files}`` o el mapa pelado."""
    path = Path(path)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    if isinstance(data, dict) and isinstance(data.get("files"), dict):
        return data["files"]
    if isinstance(data, dict):
        return data
    return {}


def save_manifest(path: Path, version: str, files: dict) -> bool:
    """Guarda ``{version, generated_at, files}`` en JSON legible."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": version,
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "files": dict(sorted(files.items())),
        }
        path.write_text(json.dumps(payload, indent=4) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def sync_from_source(
    src_root: Path,
    app_root: Path,
    manifest_path: Path,
    version: str,
    sensitive=DEFAULT_SENSITIVE,
    skip_dirs=DEFAULT_SKIP_DIRS,
    skip_prefixes=DEFAULT_SKIP_PREFIXES,
) -> dict:
    """Núcleo: sincroniza ``src_root`` sobre ``app_root``.

    - Copia solo ficheros nuevos o cuyo hash difiera.
    - Borra los ficheros *gestionados* (presentes en el manifiesto antiguo)
      que ya no están en la release. Los no gestionados nunca se tocan.
    - Guarda el manifiesto nuevo con la versión de la release.
    """
    src_root = Path(src_root)
    app_root = Path(app_root)
    result = {"copied": 0, "skipped": 0, "deleted": 0, "errors": [], "manifest": {}}

    new_manifest = build_manifest(src_root, sensitive, skip_dirs, skip_prefixes)
    result["manifest"] = new_manifest

    for rel, digest in new_manifest.items():
        dest = app_root / rel
        try:
            if dest.is_file() and sha256_file(dest) == digest:
                result["skipped"] += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_root / rel, dest)
            result["copied"] += 1
        except OSError as exc:
            result["errors"].append(f"{rel}: {exc}")

    old_manifest = load_manifest(manifest_path)
    for rel in old_manifest:
        if rel in new_manifest:
            continue
        dest = app_root / rel
        try:
            if dest.is_file() or dest.is_symlink():
                dest.unlink()
                result["deleted"] += 1
        except OSError as exc:
            result["errors"].append(f"borrar {rel}: {exc}")

    save_manifest(manifest_path, version, new_manifest)
    return result


def create_backup(
    app_root: Path,
    backup_base: Path,
    sensitive=DEFAULT_SENSITIVE,
    skip_dirs=DEFAULT_SKIP_DIRS,
    skip_prefixes=DEFAULT_SKIP_PREFIXES,
) -> dict:
    """Copia el código actual a ``backup_<fecha>/`` (excluye estado del usuario)."""
    app_root = Path(app_root)
    backup_base = Path(backup_base)
    base_name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    name = base_name
    seq = 2
    while (backup_base / name).exists():
        name = f"{base_name}_{seq}"
        seq += 1
    dest = backup_base / name
    result = {"ok": False, "dir": name, "copied": 0, "errors": []}
    try:
        backup_base.mkdir(parents=True, exist_ok=True)
        for p in sorted(app_root.rglob("*")):
            if not p.is_file() and not p.is_symlink():
                continue
            rel = p.relative_to(app_root).as_posix()
            if is_excluded(rel, sensitive, skip_dirs, skip_prefixes):
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if p.is_symlink():
                target.symlink_to(os_readlink(p))
            else:
                shutil.copy2(p, target)
            result["copied"] += 1
        result["ok"] = True
    except OSError as exc:
        result["errors"].append(str(exc))
    prune_backups(backup_base)
    return result


def os_readlink(p: Path) -> str:
    import os
    return os.readlink(p)


def prune_backups(backup_base: Path, keep: int = DEFAULT_MAX_BACKUPS) -> None:
    """Conserva solo las ``keep`` backups más recientes."""
    backup_base = Path(backup_base)
    if not backup_base.is_dir():
        return
    backups = sorted(
        (d for d in backup_base.iterdir() if d.is_dir() and BACKUP_NAME_RE.match(d.name)),
        key=lambda d: d.name,
        reverse=True,
    )
    for old in backups[keep:]:
        rrmdir(old)


def restore_backup(
    backup_dir: Path,
    app_root: Path,
    backup_base: Path,
    sensitive=DEFAULT_SENSITIVE,
    skip_dirs=DEFAULT_SKIP_DIRS,
    skip_prefixes=DEFAULT_SKIP_PREFIXES,
) -> dict:
    """Restaura un backup sobre la instalación (haciendo backup previo).

    Solo copia los ficheros presentes en el backup; no borra nada más.
    """
    backup_dir = Path(backup_dir)
    app_root = Path(app_root)
    result = {"ok": False, "copied": 0, "errors": [], "pre": None}

    pre = create_backup(app_root, backup_base, sensitive, skip_dirs, skip_prefixes)
    result["pre"] = pre

    try:
        for p in sorted(backup_dir.rglob("*")):
            if not p.is_file() and not p.is_symlink():
                continue
            rel = p.relative_to(backup_dir).as_posix()
            if is_excluded(rel, sensitive, skip_dirs, skip_prefixes):
                continue
            target = app_root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if p.is_symlink():
                if target.exists() or target.is_symlink():
                    target.unlink()
                target.symlink_to(os_readlink(p))
            else:
                shutil.copy2(p, target)
            result["copied"] += 1
        result["ok"] = True
    except OSError as exc:
        result["errors"].append(str(exc))
    return result


def extract_zip(zip_path: Path, dest_dir: Path) -> list:
    """Extrae un ZIP de forma segura (rechaza rutas con ``..`` o absolutas).

    Devuelve la lista de rutas extraídas (relativas a ``dest_dir``).
    """
    extracted = []
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            # Anti zip-slip
            if name.startswith("/") or ".." in Path(name).parts:
                continue
            target = dest_dir / name
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            extracted.append(name)
    return extracted


def find_repo_root(extract_dir: Path) -> Path:
    """Localiza la raíz del proyecto dentro del ZIP de GitHub.

    El zipball de GitHub contiene una única carpeta ``<repo>-<ref>/``;
    si el contenido está en la raíz, se devuelve tal cual.
    """
    extract_dir = Path(extract_dir)
    entries = [d for d in extract_dir.iterdir() if d.is_dir()]
    files = [f for f in extract_dir.iterdir() if f.is_file()]
    if len(entries) == 1 and not files:
        return entries[0]
    return extract_dir
