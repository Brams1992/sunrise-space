"""Sauvegarde complète de la base Sunrise Space dans un fichier .json.gz.

Utilisation :   DATABASE_URL=postgresql://...  python backup_db.py  [fichier.json.gz]
(sans DATABASE_URL, sauvegarde la base SQLite locale)
"""
import gzip
import json
import os
import sys
from datetime import datetime, timezone

os.environ["SUNRISE_SKIP_INIT"] = "1"   # on lit la base, on ne la modifie pas

import app as sunrise  # noqa: E402


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else "sunrise-backup-%s.json.gz" % datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    db = sunrise.DB()
    try:
        data = sunrise.export_all(db)
    finally:
        db.close()
    with gzip.open(name, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    counts = ", ".join(f"{t}: {len(rows)}" for t, rows in data["tables"].items())
    print(f"Sauvegarde écrite : {name}\n  {counts}")


if __name__ == "__main__":
    main()
