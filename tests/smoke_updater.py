"""Smoke test del núcleo del actualizador (nunca sobre la instalación real).

Crea una app falsa en temp, simula una release nueva y verifica:
- copia solo nuevos/cambiados
- stale: gestionados desaparecidos se borran
- $sensitive intactos
- backup y restore correctos
- zip-slip rechazado
"""
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from model_changer import updater


def build_tree(root: Path, files: dict):
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="mc-updater-test-"))

    # --- Instalación "actual" ------------------------------------------------
    app = tmp / "app"
    build_tree(app, {
        "model_changer/main.py": "v1",
        "model_changer/updater.py": "v1",
        "templates/index.html": "v1",
        "config.yaml": "MI CONFIG",
        "model_profiles.yaml": "MIS PERFILES",
        "logs/llama.log": "log",
        "VERSION": "v1.0.0",
        "env/github.token": "secret",
    })

    backup_base = app / "backups"
    manifest_path = backup_base / ".manifest.json"

    # Release v1 -> primer sync (instalación inicial gestionada)
    rel1 = tmp / "rel1"
    build_tree(rel1, {
        "model_changer/main.py": "v1",
        "model_changer/updater.py": "v1",
        "templates/index.html": "v1",
        "config.yaml": "default",
        "VERSION": "v1.0.0",
    })
    r = updater.sync_from_source(rel1, app, manifest_path, "v1.0.0")
    assert r["skipped"] == 4 and not r["errors"], r  # idénticos: nada que copiar
    print("sync inicial ok:", {k: r[k] for k in ("copied", "skipped", "deleted")})

    # --- Backup ---------------------------------------------------------------
    b = updater.create_backup(app, backup_base)
    assert b["ok"] and b["copied"] > 0, b
    backup_dir = backup_base / b["dir"]
    assert not (backup_dir / "config.yaml").exists(), "sensitive no debe entrar al backup"
    assert not (backup_dir / "logs").exists()
    print("backup ok:", b["dir"], b["copied"], "ficheros; sensitive excluidos OK")

    # --- Release v2: cambio main.py, borra templates/index.html, añade nuevo ---
    rel2 = tmp / "rel2"
    build_tree(rel2, {
        "model_changer/main.py": "v2 CAMBIADO",
        "model_changer/updater.py": "v1",
        "model_changer/nuevo_modulo.py": "nuevo",
        "VERSION": "v2.0.0",
    })
    r2 = updater.sync_from_source(rel2, app, manifest_path, "v2.0.0")
    assert (app / "model_changer/main.py").read_text() == "v2 CAMBIADO"
    assert (app / "model_changer/nuevo_modulo.py").exists()
    assert not (app / "templates/index.html").exists(), "stale debe borrarse"
    assert (app / "config.yaml").read_text() == "MI CONFIG", "sensitive intacto"
    assert (app / "model_profiles.yaml").read_text() == "MIS PERFILES"
    m = updater.load_manifest(manifest_path)
    assert m["model_changer/main.py"] == updater.sha256_file(app / "model_changer/main.py")
    assert "templates/index.html" not in m
    print("sync v2 ok:", {k: r2[k] for k in ("copied", "skipped", "deleted")},
          "· sensitive intactos · stale borrado · manifiesto coherente")

    # Fichero NO gestionado del usuario: nunca se borra
    (app / "plugin_user.py").write_text("mio", encoding="utf-8")
    rel3 = tmp / "rel3"
    build_tree(rel3, {"model_changer/main.py": "v2 CAMBIADO"})
    r3 = updater.sync_from_source(rel3, app, manifest_path, "v2.0.1")
    assert (app / "plugin_user.py").exists(), "no gestionado no se borra"
    assert r3["copied"] == 0 and r3["skipped"] == 1 and r3["deleted"] == 3, r3
    print("idempotencia ok: nada nuevo que copiar, no gestionados intactos")

    # --- Restore ---------------------------------------------------------------
    rr = updater.restore_backup(backup_dir, app, backup_base)
    assert rr["ok"], rr
    assert (app / "templates/index.html").exists(), "restore recupera stale"
    assert (app / "model_changer/main.py").read_text() == "v1", "restore recupera version vieja"
    assert (app / "config.yaml").read_text() == "MI CONFIG"
    print("restore ok:", rr["copied"], "ficheros · pre-backup:", rr["pre"]["dir"])

    # --- Zip slip --------------------------------------------------------------
    evil = tmp / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../../escaped.txt", "boom")
        zf.writestr("normal.txt", "ok")
    dest = tmp / "extract"
    updater.extract_zip(evil, dest)
    assert (dest / "normal.txt").exists()
    assert not (tmp / "escaped.txt").exists(), "zip slip debe bloquearse"
    print("zip-slip bloqueado OK")

    # --- find_repo_root --------------------------------------------------------
    extract2 = tmp / "extract2"
    build_tree(extract2 / "Model-changer-abc1234", {"model_changer/main.py": "v1"})
    assert updater.find_repo_root(extract2).name == "Model-changer-abc1234"
    assert updater.find_repo_root(rel2).name == "rel2"  # raíz con varios archivos
    print("find_repo_root ok")

    print("\nTODOS LOS TESTS PASARON ✔")


if __name__ == "__main__":
    main()
