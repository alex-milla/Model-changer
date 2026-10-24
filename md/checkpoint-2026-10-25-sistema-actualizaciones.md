# Checkpoint — Sistema de actualizaciones desde GitHub (port a Python)

**Fecha:** 2026-10-25
**Estado:** Implementado y verificado (smoke + e2e contra release real v1.4.4)
**Origen del patrón:** `md/sistema-actualizaciones-github.md` (M4-CMS, PHP)

## Qué se implementó

Port del patrón de autactualización de M4-CMS a este proyecto (Python/FastAPI):

| M4-CMS (PHP) | Model Changer (Python) |
|---|---|
| `helpers/updater.php` (funciones puras) | `model_changer/updater.py` (stdlib: hashlib, zipfile, shutil) |
| `entrada/update.php` (UI + POST) | Endpoints en `model_changer/main.py` + pestaña HTMX |
| `db/.manifest.json` | `backups/.manifest.json` |
| `db/backups/backup_*` | `backups/backup_YYYYMMDD_HHMMSS[_N]/` |
| `VERSION` | `VERSION` (texto plano, ej. `1.4.4`) |
| `GITHUB_TOKEN` / `.github_token` | `GITHUB_TOKEN` / `env/github.token` |

## Ficheros nuevos/tocados

- **Nuevos:** `model_changer/updater.py`, `VERSION`, `tests/smoke_updater.py`.
- **Modificados:** `model_changer/main.py` (endpoints + renders), `templates/index.html` (pestaña ⬆️ Actualizaciones), `config.yaml` (sección de actualizaciones), `.gitignore` (`env/`, `backups/`), `README.md` (sección Updates).

## Endpoints

- `GET  /api/update/status` — versión instalada, última release, up_to_date, backups.
- `GET  /api/update/backups` — lista de backups.
- `POST /api/update/install` (`force` opcional) — backup → descarga → extracción → sync → `VERSION`.
- `POST /api/update/restore` (`backup`) — restaura un backup (con backup previo del estado actual).
- `GET  /fragments/updates-page`, `GET /fragments/updates` — HTML para HTMX.
- `POST /fragments/service-restart` — reinicio del servicio que devuelve HTML.

## Decisiones de adaptación (sección 12 del doc de referencia)

- **`$sensitive`** → `config.yaml`, `model_profiles.yaml`, `.env`, `.github_token`, `SESSION_LOG.md` (estado del usuario en la instalación del Chuwi).
- **`$skipDirs`** → `.git`, `.github`, `__pycache__`, `venv`, `.venv`, `env`, `logs`, `node_modules`, `backups`, etc.
- **`$forceDelete`** → vacío (no hay utilidades de un solo uso en este proyecto).
- **Raíz del ZIP**: el zipball de GitHub trae `<repo>-<ref>/` como única carpeta; `find_repo_root()` la localiza.
- **Zip-slip**: `extract_zip()` rechaza rutas absolutas y con `..`.
- **Anti path-traversal en restore**: regex `^backup_\d{8}_\d{6}(?:_\d+)?$` + `resolve().relative_to(BACKUP_BASE)`.
- **Colisión de backups del mismo segundo**: sufijo numérico único (`backup_..._2`) — bug encontrado por el smoke test; en M4-CMS no ocurría porque install y restore son acciones separadas en el tiempo.
- **Sin opcache**: equivalente = botón "Reiniciar servicio" (endpoint ya existente `restart_service`).
- **Sin CSRF/admin**: consistente con el resto del panel (sin autenticación); el README ya recomienda proteger el panel con Nginx/VPN.
- **Token** (`env/github.token`): opcional, solo sube rate limit o repos privados. `env/` está en `.gitignore`.

## Verificación realizada

- `python -m py_compile` de `main.py` y `updater.py`.
- `tests/smoke_updater.py`: sync inicial, cambios + stale + sensitive intactos, idempotencia, no-gestionados no se borran, backup/restore, zip-slip, `find_repo_root`. **Todos pasan.**
- TestClient (FastAPI) contra GitHub real: `/api/update/status` detecta `v1.4.4` (fuente: release), `/fragments/updates` renderiza, restore con `../../evil` bloqueado.
- **E2E**: instalación forzada de la release real `v1.4.4` sobre una copia temporal del proyecto → copiados 3 / sin cambios 8, `config.yaml` y `model_profiles.yaml` intactos, `VERSION` = v1.4.4, backup creado, manifiesto de 11 entradas.

## Proceso de release (para que el updater tenga algo que instalar)

1. Bump de `VERSION` en el repo.
2. Commit + tag `vX.Y.Z` + push (con tags).
3. GitHub Release sobre el tag (no draft, no prerelease) con notas.

## Pendiente

- Crear el fichero `env/github.token` en la instalación del Chuwi (el usuario lo generará).
- Opcional: workflow CI con `py_compile` + smoke test (sección 14 del doc de referencia).
