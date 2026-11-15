"""Restaure une sauvegarde (.json ou .json.gz) dans une base Sunrise Space.

Utilisation :   DATABASE_URL=postgresql://...  python restore_backup.py  sauvegarde.json.gz  [--force]
Par sécurité, la restauration refuse une base qui contient déjà des comptes (sauf avec --force,
qui efface d'abord le contenu actuel).
"""
import gzip
import json
import os
import sys

os.environ["SUNRISE_SKIP_INIT"] = "1"   # pas de création automatique de l'admin : il vient de la sauvegarde

import app as sunrise  # noqa: E402

ORDER = [t for t, _ in sunrise.BACKUP_TABLES]


def load(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        data = json.load(f)
    if data.get("format") != "sunrise-backup-1" or "tables" not in data:
        raise SystemExit("Ce fichier n'est pas une sauvegarde Sunrise Space.")
    return data


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    force = "--force" in sys.argv
    if not args:
        raise SystemExit(__doc__)
    data = load(args[0])
    sunrise.init_db(create_admin=False)
    db = sunrise.DB()
    try:
        existing = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if existing and not force:
            raise SystemExit(f"La base contient déjà {existing} compte(s). Relancez avec --force pour l'écraser.")
        if existing:
            for table in ["tokens"] + ORDER[::-1]:
                db.execute(f"DELETE FROM {table}")
        for table in ORDER:
            for row in data["tables"].get(table, []):
                cols = list(row)
                db.execute(f"INSERT INTO {table}({', '.join(cols)}) VALUES({', '.join('?' for _ in cols)})",
                           [row[c] for c in cols])
        if sunrise.USE_PG:   # remet les compteurs d'identifiants après les insertions avec id explicite
            for table in ("users", "ride_requests", "reports", "announcements", "messages"):
                db.execute(f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE(MAX(id), 1), MAX(id) IS NOT NULL) FROM {table}")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
    print("Restauration terminée : " + ", ".join(f"{t}: {len(data['tables'].get(t, []))}" for t in ORDER))
    print("Les sessions ne sont pas sauvegardées : chacun devra se reconnecter.")


if __name__ == "__main__":
    main()
