"""Trwałe przechowywanie danych salonów (plik lub PostgreSQL)."""
from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
DATA_FILE = DATA_DIR / "salon.json"
BACKUP_DIR = DATA_DIR / "backups"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

_POSTGRES_READY = False


def uzywaj_postgres() -> bool:
    return bool(DATABASE_URL)


def tryb_magazynu() -> str:
    return "postgres" if uzywaj_postgres() else "plik"


def init_storage() -> None:
    """Wywołaj przy starcie aplikacji (wsgi)."""
    global _POSTGRES_READY
    if uzywaj_postgres():
        _init_postgres()
        _POSTGRES_READY = True
        _migruj_plik_do_postgres_jesli_trzeba()
        logger.info("Magazyn danych: PostgreSQL")
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        logger.info("Magazyn danych: plik %s", DATA_FILE)


def wczytaj_raw() -> dict | None:
    if uzywaj_postgres():
        return _wczytaj_postgres()
    return _wczytaj_plik()


def zapisz_raw(dane: dict) -> None:
    if uzywaj_postgres():
        _zapisz_postgres(dane)
        _wykonaj_backup_pliku(dane)
        return
    _wykonaj_backup_pliku_przed_zapisem()
    _zapisz_plik(dane)


def _wczytaj_plik() -> dict | None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        return None
    with DATA_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def _zapisz_plik(dane: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(dane, f, ensure_ascii=False, indent=2)


def _wykonaj_backup_pliku_przed_zapisem() -> None:
    if not DATA_FILE.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_file = BACKUP_DIR / f"salon-{timestamp}.json"
    shutil.copy2(DATA_FILE, backup_file)
    kopie = sorted(BACKUP_DIR.glob("salon-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stara_kopia in kopie[30:]:
        stara_kopia.unlink(missing_ok=True)


def _wykonaj_backup_pliku(dane: dict) -> None:
    """Kopia zapasowa na dysku (gdy DATA_DIR jest na trwałym wolumenie Render)."""
    if not DATA_DIR.exists():
        return
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_file = BACKUP_DIR / f"salon-{timestamp}.json"
        with backup_file.open("w", encoding="utf-8") as f:
            json.dump(dane, f, ensure_ascii=False, indent=2)
        kopie = sorted(BACKUP_DIR.glob("salon-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for stara_kopia in kopie[30:]:
            stara_kopia.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Nie udało się zapisać kopii na dysku: %s", exc)


def _init_postgres() -> None:
    import psycopg

    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS glovaro_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.commit()


def _wczytaj_postgres() -> dict | None:
    import psycopg

    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute("SELECT payload FROM glovaro_state WHERE id = 1").fetchone()
    if not row:
        return None
    payload = row[0]
    if isinstance(payload, dict):
        return payload
    return json.loads(payload)


def _zapisz_postgres(dane: dict) -> None:
    import psycopg
    from psycopg.types.json import Jsonb

    with psycopg.connect(DATABASE_URL) as conn:
        conn.execute(
            """
            INSERT INTO glovaro_state (id, payload, updated_at)
            VALUES (1, %s, NOW())
            ON CONFLICT (id) DO UPDATE
            SET payload = EXCLUDED.payload, updated_at = NOW()
            """,
            (Jsonb(dane),),
        )
        conn.commit()


def _migruj_plik_do_postgres_jesli_trzeba() -> None:
    if _wczytaj_postgres() is not None:
        return
    dane_z_pliku = _wczytaj_plik()
    if dane_z_pliku:
        _zapisz_postgres(dane_z_pliku)
        logger.info("Przeniesiono dane z pliku salon.json do PostgreSQL")
