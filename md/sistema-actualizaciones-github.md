# Sistema de actualizaciones desde GitHub

**Fecha:** 2026-10-12
**Estado:** Documento de referencia (reutilizable)
**Origen:** M4 CMS (`alex-milla/M4-CMS`)
**Ficheros de origen:** `public_html/helpers/updater.php`, `public_html/entrada/update.php`
**Skill equivalente:** `skills/php-cms-updater/` (`SKILL.md` + plantillas)
**Tags relacionados:** `v0.2` (sistema de actualizaciones) · `v0.3` (hardening + manifiesto SHA-256)

---

## 1. Objetivo

Este documento describe, de forma completa y portable, un **sistema de
autactualización de una aplicación PHP self-hosted** que:

1. Consulta la **última release publicada en GitHub** (repo público).
2. Descarga el **ZIP de la release**.
3. **Sincroniza** los ficheros de la aplicación mediante un **manifiesto
   SHA-256** (copia solo ficheros nuevos o cambiados, borra obsoletos).
4. **Nunca** sobrescribe datos del usuario (`config.php`, `.env`,
   `admin_config.php`, base de datos).
5. Crea un **backup del código antes de cada operación** y permite **restaurarlo**.

El patrón es **agnóstico al stack**: la lógica (release + diff por hash +
exclusión de datos de usuario + backup) se puede portar a cualquier lenguaje o
framework; solo cambian las APIs de hash y de extracción de archivos.

> Este documento está pensado para copiarse a otro proyecto y adaptar los
> nombres/paths. Las secciones 12–15 son las que interesa leer primero para
> portarlo.

---

## 2. Principios de diseño

| Principio | Implementación |
|---|---|
| **La release es la fuente de verdad** | El ZIP del tag/release define el estado deseado. |
| **Manifiesto, no sobrescritura ciega** | Se hashea cada fichero publicado y se compara con el manifiesto instalado (`db/.manifest.json`). |
| **Nunca tocar datos de usuario** | Lista `$sensitive` excluida de copia y de backups. |
| **Backup primero** | Se copia el código actual a `db/backups/backup_<fecha>/` antes de instalar; restaurable desde la UI. |
| **Idempotencia** | Reejecutar la actualización no rompe nada (solo copia lo que difiere). |
| **Token opcional** | Repo público no requiere token; `GITHUB_TOKEN` solo sube el rate limit de la API. |
| **Tolerancia a entornos pobres** | Fallbacks para extracción (`ZipArchive`→`PharData`→`unzip`) y red (`curl`→`file_get_contents`). |
| **El panel admin es renombrable** | Se remapea la carpeta `entrada/` al slug real al aplicar en disco. |

---

## 3. Requisitos del entorno

| Requisito | Detalle |
|---|---|
| PHP | 8.0+ (usa tipos de retorno, arrow functions, `Throwable`). |
| Red | `curl` **o** `allow_url_fopen=On`. |
| ZIP | Extensión `zip` (`ZipArchive`) **o** `PharData` **o** binario `unzip` con `exec` habilitado. |
| Escritura | Permisos de escritura en la raíz de la app (para copiar ficheros y `VERSION`). |
| Extras | `opcache_reset()` si OPcache está activo; `set_time_limit(300)` e `ignore_user_abort(true)` para operaciones largas. |

Si no hay ninguna vía de extracción disponible, la UI debe **decirlo
claramente** en lugar de fallar en silencio (ver `extractZip()`).

---

## 4. Estructura de archivos

```
public_html/
├── VERSION                       # Versión instalada, p. ej. "0.7.5.4"
├── helpers/
│   └── updater.php               # Lógica pura: manifiesto, sync, backup, restore
├── entrada/                      # Carpeta admin (renombrable)
│   └── update.php                # UI + acciones POST (instalar / reinstalar / restaurar)
└── db/
    ├── .manifest.json            # Manifiesto instalado (ruta canónica → sha256) — NO versionado
    └── backups/
        ├── .htaccess             # Deniega acceso web
        ├── index.php             # Guard 403
        └── backup_YYYYMMDD_HHMMSS/   # Copia del código en cada operación
```

- `updater.php` **no** hace red ni sesión: son funciones puras → fáciles de
  testear en CLI.
- `update.php` concentra I/O, sesión, CSRF y presentación.
- El manifiesto y los backups viven en `db/` (directorio que el `.htaccess`
  bloquea por completo al web).

---

## 5. Flujo de datos

```
                     ┌─────────────────────────────────────────┐
   UI (update.php)   │ 1. GET /repos/{owner}/{repo}/releases/latest
   ───────────────►  │    (fallback: /tags?per_page=1)
                     │ 2. Lee VERSION instalada
                     │ 3. version_compare(instalada, remota)
                     └───────────────┬─────────────────────────┘
                                     │ POST action=install
                                     ▼
                     ┌─────────────────────────────────────────┐
   doUpdate()        │ 4. m4_create_backup()  → db/backups/…    │
   ───────────────►  │ 5. downloadFile(zipball_url) → temp      │
                     │ 6. extractZip() → temp                    │
                     │ 7. localiza la raíz (repo-sha/public_html)│
                     └───────────────┬─────────────────────────┘
                                     ▼
   m4_sync_from_source()  ──►  manifiesto nuevo (sha256 por fichero)
                          ──►  copia nuevos/cambiados
                          ──►  borra gestionados que ya no están
                          ──►  borra lista $forceDelete
                          ──►  guarda db/.manifest.json
                                     │
                                     ▼
                     Escribe VERSION = versión remota · opcache_reset()
                     · log · flash PRG → redirect
```

---

## 6. Configuración (a adaptar en cada proyecto)

Definida al inicio de `update.php`:

```php
// Repositorio de releases
$repoOwner = 'alex-milla';
$repoName  = 'M4-CMS';

// Token opcional: variable de entorno o fichero FUERA del web root
$githubToken = getenv('GITHUB_TOKEN') ?: '';
$tokenFile   = dirname(__DIR__) . '/.github_token';
if (!$githubToken && @is_file($tokenFile)) {
    $githubToken = trim((string)@file_get_contents($tokenFile));
}

$appRoot     = dirname(__DIR__);        // raíz de la app (web root)
$adminSlug   = basename(__DIR__);       // carpeta admin actual (renombrable)
$versionFile = $appRoot . '/VERSION';
$backupBase  = $appRoot . '/db/backups';

// Nunca se sobrescriben ni entran en los backups
$sensitive = [
    'config.php', '.env', 'admin_config.php', 'cleanup.php', 'install.php',
    'setup.php', '.github_token', 'db/cms.db', 'README.md', '.gitignore',
];
$skipDirs     = ['.git', '.github', 'node_modules', '.release'];
$skipPrefixes = ['db/backups/'];

// Utilidades que se borran SIEMPRE en cada actualización
$forceDelete = ['entrada/reset-admin.php', 'install.php', 'cleanup.php', 'fix-bom.php'];
if (is_file($appRoot . '/.setup_completed')) {
    $forceDelete[] = 'setup.php';   // no reinyectar el instalador en sitios ya instalados
}
```

**Puntos clave al portar:**

- `$sensitive` debe listar todo lo que sea **estado del usuario** (credenciales,
  BD, config local). Rutas **relativas a `$appRoot`**, con `/`.
- `$skipDirs` corta directorios completos (por primer segmento).
- `$skipPrefixes` corta por prefijo de ruta (p. ej. todas las backups).
- `$forceDelete` es la lista negra de utilidades peligrosas/obsoletas.
- `$adminSlug` se calcula con `basename(__DIR__)`: así funciona aunque el
  usuario renombre la carpeta admin.

---

## 7. Referencia — `helpers/updater.php`

Funciones puras (sin red ni sesión). Todas trabajan con **rutas canónicas**
(la carpeta admin se normaliza siempre como `entrada/` y se remapea al aplicar).

| Función | Firma | Qué hace |
|---|---|---|
| `m4_remap_path` | `(string $rel, array $remap): string` | Remapea el primer segmento de una ruta relativa (`entrada` → slug real). |
| `m4_rrmdir` | `(string $dir): void` | Borrado recursivo de directorio. |
| `m4_copy_tree_mapped` | `(src, dst, remap, sensitive, skipDirs, skipPrefixes): array` | Copia árbol aplicando remapeo y exclusiones. Devuelve `{copied, errors}`. |
| `m4_build_manifest` | `(dir, sensitive, skipDirs, skipPrefixes): array` | Construye `ruta → sha256` de un directorio (ordenado con `ksort`). |
| `m4_load_manifest` | `(string $path): array` | Carga `.manifest.json`; acepta el wrapper `{files:{…}}` o el mapa pelado. |
| `m4_save_manifest` | `(path, version, files): bool` | Guarda `{version, generated_at, files}` con JSON pretty. |
| `m4_sync_from_source` | `(srcWeb, appRoot, adminSlug, version, sensitive, skipDirs, skipPrefixes, forceDelete): array` | **Núcleo**: sincroniza y devuelve `{copied, skipped, deleted, errors, manifest}`. |
| `m4_ensure_backup_guards` | `(string $dir): void` | Crea `.htaccess` (deny) + `index.php` (403) en la carpeta de backups. |
| `m4_create_backup` | `(appRoot, backupBase, adminSlug, sensitive, skipDirs, skipPrefixes): array` | Copia el código actual a `backup_<fecha>/`; normaliza admin a `entrada`. |
| `m4_restore_backup` | `(backupDir, appRoot, adminSlug, backupBase, sensitive, skipDirs, skipPrefixes): array` | Backup previo + restaura sobre la instalación (remapea `entrada`→slug). |

### Detalles de `m4_sync_from_source`

1. `$installed = is_file($appRoot.'/.setup_completed')`.
2. Construye el manifiesto **nuevo** desde `$srcWeb`.
3. Si ya está instalado, **elimina `setup.php`** del manifiesto nuevo.
4. Quita del manifiesto cualquier entrada de `$forceDelete`.
5. Por cada fichero: si el destino existe y **coincide el hash**, `skipped++`;
   si no, `copy()` (`copied++`) o error.
6. Carga el manifiesto **antiguo** y **borra** los ficheros gestionados que ya
   no están en el nuevo (`deleted++`).
7. Ejecuta `$forceDelete`: borra esas rutas si existen.
8. Guarda el manifiesto nuevo con la versión remota.

> Los ficheros **no gestionados** (los que nunca estuvieron en el manifiesto,
> p. ej. plugins propios del usuario) **nunca** se borran.

---

## 8. Flujo del endpoint `update.php`

Estructura por fases (líneas aproximadas del original):

1. **Bootstrap**: `ob_start()`, `requireAdmin()`, `set_time_limit(300)`,
   `ignore_user_abort(true)`, includes (db, tema, i18n, iconos, updater).
2. **Config**: repositorio, token, rutas, listas (sección 6).
3. **Utilidades de red/ZIP**:
   - `githubApiGet($url, $token)`: `curl` con TLS verificado, timeout 15 s,
     `Accept: application/vnd.github+json`; traduce 403 (rate limit) y 404 (sin
     releases) a mensajes claros; fallback a `file_get_contents`.
   - `downloadFile($url, $dest, $token, &$err)`: vuelca el ZIP a temporal
     (timeout 120 s, sigue redirecciones, máx. 5).
   - `extractZip($zip, $dest, &$err)`: `ZipArchive` → `PharData` → `unzip`
     (con `escapeshellarg`).
4. **`doUpdate(...)`**:
   - Backup **primero**.
   - Descarga el ZIP a `sys_get_temp_dir()`.
   - Valida tamaño (> 1 KB) para detectar ZIP corrupto.
   - Extrae.
   - **Localiza la raíz** dentro del ZIP de GitHub (`repo-sha/public_html`).
   - `m4_sync_from_source(...)`.
   - Limpia temporales; si `copied + deleted == 0` → “nada que actualizar”.
   - Escribe `VERSION` con la versión remota.
5. **Versión instalada**: lee `VERSION` (por defecto `0.0.0`).
6. **Última release**: `GET …/releases/latest`; si no hay release, **fallback a
   `/tags?per_page=1`** y construye la URL
   `https://github.com/{owner}/{repo}/archive/refs/tags/{tag}.zip`.
7. **POST (CSRF obligatorio)**:
   - `action=restore`: valida `^backup_\d{8}_\d{6}$` y que `realpath` esté bajo
     `$backupBase` (anti path-traversal), restaura, `opcache_reset()`, log,
     flash y **PRG** (`header Location` + `exit`).
   - `action=install`: si no hay release → error; si `!$force` y
     `version_compare(instalada, remota, '>=')` → “ya estás al día”; si no,
     `doUpdate()`. Éxito → `opcache_reset()`, log, flash, redirect.
   - Todo envuelto en `try/catch (\Throwable)`.
8. **Listado de backups**: `glob(backupBase/backup_*)`, filtrado por patrón,
   ordenado por fecha desc (se muestran 15).
9. **Render**: layout admin compartido (header/sidebar), banners flash/error,
   tabla repo/versión/última/publicada, notas de la release en `<details>`,
   botones *instalar* y *reinstalar forzado*, tabla de backups con *restaurar*.

**Patrón PRG (Post/Redirect/Get)**: tras una acción se guarda un flash en sesión
y se redirige, evitando reenvíos al recargar.

---

## 9. Formato del manifiesto (`db/.manifest.json`)

Guardado por `m4_save_manifest()`; rutas **canónicas** (admin como `entrada/`):

```json
{
    "version": "0.7.5.4",
    "generated_at": "2026-09-27T12:00:00+00:00",
    "files": {
        "index.php": "e3b0c44298fc1c149afbf4c8996fb924…",
        "helpers/updater.php": "9f86d081884c7d659a2feaa0c55ad015…",
        "entrada/update.php": "5891b5b522d5df086d0ff0b110fbd9d2…"
    }
}
```

- `m4_load_manifest` acepta **también** el mapa pelado (sin wrapper), por
  compatibilidad hacia atrás.
- El manifiesto se incluye en los **backups**, de modo que una restauración
  recupera el estado exacto (los ficheros gestionados y su hash).
- **Debe** quedar fuera del web (va en `db/`, bloqueado por `.htaccess`).

---

## 10. Seguridad

- **Acceso a la UI**: `requireAdmin()` + sesión (`m4_session_start`).
- **CSRF**: todas las acciones POST validan token con `csrfValidate()` y los
  formularios lo emiten con `csrfField()`.
- **Anti path-traversal en restore**: nombre validado por regex y `realpath`
  comprobado contra `$backupBase`.
- **TLS verificado**: `CURLOPT_SSL_VERIFYPEER = true`.
- **Datos de usuario intocables**: `$sensitive` se excluye de copia y de backup.
- **Lista negra de utilidades**: `reset-admin.php`, `install.php`,
  `cleanup.php`, `fix-bom.php`, `setup.php` (en sitios instalados) se eliminan
  siempre.
- **Carpeta de backups blindada**: `m4_ensure_backup_guards()` escribe
  `.htaccess` (deny total) e `index.php` (403) si faltan.
- **`.htaccess` de la app**: deniega dotfiles (`.env`, `.setup_completed`,
  `db/.manifest.json`…), bases de datos (`*.db`, `*.sqlite*`, `*.bak`) y
  utilidades (`admin_config.php`, `install.php`, `cleanup.php`, `fix-bom.php`);
  además bloquea por rewrite los directorios `db/`, `helpers/`, `templates/`.
- **Log de auditoría**: cada instalación/restauración llama a
  `logAdminEvent('system_updated'|'system_restored', …)`.
- **Recomendación adicional** al portar: rechazar entradas del ZIP que
  contengan `..` (defensa extra contra *zip slip*).

---

## 11. Backup y restauración

```php
// Antes de instalar:
$backup = m4_create_backup($appRoot, $backupBase, $adminSlug, $sensitive, $skipDirs, $skipPrefixes);
// → db/backups/backup_20261012_103000/  (+ .htaccess + index.php)

// Restaurar (hace un backup previo del estado actual y lo devuelve en 'pre'):
$res = m4_restore_backup($backupDir, $appRoot, $adminSlug, $backupBase, $sensitive, $skipDirs, $skipPrefixes);
```

- Nombre: `backup_YYYYMMDD_HHMMSS`.
- La carpeta admin se **normaliza a `entrada/`** al guardar y se **remapea al
  slug real** al restaurar (sirve aunque el usuario haya renombrado el panel).
- La restauración incluye `db/.manifest.json`, así que el estado queda consistente.
- Se conservan las 15 más recientes en la tabla de la UI.

---

## 12. Adaptación a otro proyecto (checklist)

1. **Copia `helpers/updater.php`** tal cual (es puro; no depende del resto del
   CMS). Si tu lenguaje no es PHP, reimplementa estas operaciones:
   *hash de árbol*, *diff de manifiestos*, *copia con exclusiones*, *backup/restore*.
2. **Define tu configuración** (sección 6): `$repoOwner`, `$repoName`,
   `$sensitive`, `$skipDirs`, `$skipPrefixes`, `$forceDelete`.
3. **Ajusta `$sensitive`** a tu app: credenciales, config local, BD, ficheros de
   entorno. **Regla de oro:** todo lo que sea estado del usuario va aquí.
4. **Prepara la estructura de la release**: el ZIP debe contener tu *web root*
   (aquí `public_html/`). Si tu proyecto publica el contenido de la web en la
   raíz del repo, simplifica la detección de raíz en `doUpdate()`.
5. **Crea `VERSION`** (texto plano con la versión instalada) y actualízalo en
   cada release.
6. **Copia/adapta `update.php`**: endpoint con `requireAdmin()` + CSRF, PRG y
   render. Reutiliza tus helpers de layout/i18n.
7. **Protege la carpeta de backups** (el `m4_ensure_backup_guards()` ya lo hace)
   y asegúrate de que el manifiesto y la BD quedan fuera del web.
8. **Añade `.htaccess`** con denegación de dotfiles, `*.db`/`*.sqlite*`/`*.bak` y
   utilidades; bloquea `db/`, `helpers/`, `templates/`.
9. **Marca la versión instalada** en la UI y ofrece *instalar* + *reinstalar
   forzado* + *restaurar*.
10. **Automatiza la release**: tag `vX.Y.Z`, GitHub Release, y opcionalmente el
    manifiesto firmado/adjunto generado en CI (sección 14).

**Port a otros stacks:**

- **Node/Python/Go/Ruby**: mismo patrón; usa la librería del lenguaje para
  hash (sha256), descarga (HTTP client) y extracción (unzip nativo).
- **Frameworks con package manager**: prefiere su mecanismo propio y reserva
  este patrón para apps self-hosted “que corren en hosting barato”.
- **Sin acceso a subprocess**: evita la vía `unzip`; exige `ZipArchive`
  (PHP) o equivalente nativo.

---

## 13. Verificación / pruebas

Checklist mínima (parte ya cubierta por `tests/smoke.php` en el proyecto de
origen):

- [ ] `php -l` sin errores en `helpers/updater.php`, `entrada/update.php` y demás PHP tocados.
- [ ] Sin sesión → **302** a `login.php`; con sesión → **200** sin warnings.
- [ ] API real detecta la release (y, sin release, cae a *tags*).
- [ ] POST sin token CSRF → **403**; POST válido **no muta** si no procede.
- [ ] **Test CLI del núcleo** (en sandbox/copia temporal de la BD, **nunca** sobre producción):
  - copia solo ficheros nuevos/cambiados;
  - **stale**: ficheros gestionados desaparecidos se borran;
  - `$sensitive` intactos;
  - `$forceDelete` borrado (`reset-admin`, `setup` en instalado);
  - manifiesto correcto escrito;
  - remapeo `entrada` → `panel` en disco;
  - **backup y restore** producen el estado esperado.
- [ ] La release publicada **no** contiene utilidades prohibidas (`reset-admin.php`…).
- [ ] ZIP vacío/corrupto (< 1 KB) se rechaza con error claro.
- [ ] Restore con nombre inválido o fuera de `$backupBase` se rechaza.

---

## 14. Proceso de release en GitHub (lado servidor)

Para que el updater tenga algo que instalar:

1. **Bump** de `VERSION` en el repo.
2. Commit + **tag** `vX.Y.Z` (`git tag vX.Y.Z && git push origin main --tags`).
3. **GitHub Release** sobre ese tag (no draft, no prerelease). El updater usa
   `tag_name`, `zipball_url`, `body` (notas) y `published_at`.
4. Opcional: **adjuntar un manifiesto** (`manifest.json`) generado en CI para
   que el cliente confíe en una lista calculada/firmada en lugar de recomputarla.
5. **CI** recomendado (`.github/workflows/ci.yml`): `php -l` sobre todos los
   PHP + smoke test. Ejemplo mínimo de workflow:

   ```yaml
   name: CI
   on:
     push: { branches: [ main ] }
     pull_request: { branches: [ main ] }
   jobs:
     lint-and-smoke:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/checkout@v4
         - uses: shivammathur/setup-php@v2
           with: { php-version: '8.2', extensions: 'pdo_sqlite' }
         - run: find public_html tests -name '*.php' -print0 | xargs -0 -n1 php -l
         - run: php tests/smoke.php
   ```

- Fallback sin releases: el updater usa el **último tag** y la URL
  `…/archive/refs/tags/{tag}.zip`.

---

## 15. Anti-patrones y pitfalls

| Anti-patrón | Por qué evitarlo |
|---|---|
| Sobrescribir `config.php` / `.env` / BD en la actualización | Destruye el estado del usuario. Deben ir en `$sensitive`. |
| Actualizar **sin backup** | No hay rollback. Usa la lista blanca de reconstrucción o desconta. |
| Exigir token para un repo público | Manejo innecesario de secretos. Token solo para subir rate limit. |
| Desplegar el updater **sin** guard admin ni CSRF | Escritura arbitraria de ficheros por cualquiera. |
| Confiar en el ZIP sin validar su estructura/raíz | Path traversal (*zip slip*) y copiar fuera de destino. Verifica `public_html/`. |
| Borrar ficheros **no gestionados** al limpiar obsoletos | Rompe plugins/extensiones del usuario. Borra solo lo que estaba en el manifiesto. |
| Comparar versiones como string | Usa `version_compare` (semver). |
| Depender de `ZipArchive` sin fallback | Muchos hostings comparten lo deshabilitan. Ten 2–3 vías. |
| Guardar el token dentro del web root | Filtración por web. Guárdalo fuera (`dirname(__DIR__)`) o en env. |
| No llamar a `opcache_reset()` tras actualizar | Se sigue sirviendo código viejo en caché. |

---

## 16. Referencias

- Código: `public_html/helpers/updater.php`, `public_html/entrada/update.php`.
- Skill reutilizable: `skills/php-cms-updater/SKILL.md` (+ plantillas).
- Checkpoints de origen:
  - `md/checkpoint-2026-09-26-v0.2-sistema-actualizaciones.md`
  - `md/checkpoint-2026-09-26-v0.3-hardening-updater-manifiesto.md`
- Seguridad de despliegue: `public_html/.htaccess`, `public_html/db/backups/`.
- CI: `.github/workflows/ci.yml`.
- README (`## Updates`) — descripción para el usuario final.
