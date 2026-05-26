"""Trwałe przechowywanie danych salonów (plik lub PostgreSQL)."""
from __future__ import annotations

import json
import logging
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
DATA_FILE = DATA_DIR / "salon.json"
BACKUP_DIR = DATA_DIR / "backups"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

_POSTGRES_READY = False
_FILE_LOCK = threading.RLock()


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


def wczytaj_salon_raw(salon_slug: str) -> dict | None:
    if uzywaj_postgres():
        return _wczytaj_salon_postgres(salon_slug)
    dane = _wczytaj_plik() or {}
    salon = (dane.get("salony") or {}).get(salon_slug)
    return salon if isinstance(salon, dict) else None


def zapisz_raw(dane: dict) -> None:
    if uzywaj_postgres():
        _zapisz_postgres(dane)
        _wykonaj_backup_pliku(dane)
        return
    _wykonaj_backup_pliku_przed_zapisem()
    _zapisz_plik(dane)


def aktualizuj_raw(mutator):
    """Atomowo wczytaj, zmień i zapisz stan.

    mutator(dane) musi zwrócić tuple: (wynik, dane_do_zapisu). W PostgreSQL
    zapis odbywa się pod blokadą SELECT ... FOR UPDATE, co chroni przed
    równoczesnym nadpisaniem tego samego terminu.
    """
    if uzywaj_postgres():
        return _aktualizuj_postgres(mutator)

    with _FILE_LOCK:
        dane = _wczytaj_plik()
        wynik, dane_do_zapisu = mutator(dane)
        if dane_do_zapisu is not None:
            _wykonaj_backup_pliku_przed_zapisem()
            _zapisz_plik(dane_do_zapisu)
        return wynik


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS glovaro_salons (
                slug TEXT PRIMARY KEY,
                payload JSONB NOT NULL,
                nazwa_salonu TEXT NOT NULL DEFAULT '',
                branza TEXT NOT NULL DEFAULT '',
                abonament_status TEXT NOT NULL DEFAULT '',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_glovaro_salons_branza ON glovaro_salons (branza)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_glovaro_salons_abonament ON glovaro_salons (abonament_status)"
        )
        conn.commit()
        _migruj_state_do_salonow(conn)


def _wczytaj_postgres() -> dict | None:
    import psycopg

    with psycopg.connect(DATABASE_URL) as conn:
        dane_salonow = _wczytaj_salony_z_tabeli(conn)
        if dane_salonow is not None:
            return dane_salonow
        row = conn.execute("SELECT payload FROM glovaro_state WHERE id = 1").fetchone()
    if not row:
        return None
    payload = row[0]
    if isinstance(payload, dict):
        return payload
    return json.loads(payload)


def _wczytaj_salon_postgres(salon_slug: str) -> dict | None:
    import psycopg

    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT payload FROM glovaro_salons WHERE slug = %s",
            (salon_slug,),
        ).fetchone()
        if row:
            payload = row[0]
            salon = payload if isinstance(payload, dict) else json.loads(payload)
            salon.setdefault("slug", salon_slug)
            return salon

        legacy = conn.execute("SELECT payload FROM glovaro_state WHERE id = 1").fetchone()
        if not legacy:
            return None
        payload = legacy[0]
        dane = payload if isinstance(payload, dict) else json.loads(payload)
        salon = (dane.get("salony") or {}).get(salon_slug)
        if isinstance(salon, dict):
            salon.setdefault("slug", salon_slug)
            return salon
    return None


def _zapisz_postgres(dane: dict) -> None:
    import psycopg
    from psycopg.types.json import Jsonb

    with psycopg.connect(DATABASE_URL) as conn:
        _zapisz_salony_do_tabeli(conn, dane)
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


def _aktualizuj_postgres(mutator):
    import psycopg
    from psycopg.types.json import Jsonb

    with psycopg.connect(DATABASE_URL) as conn:
        dane = _wczytaj_salony_z_tabeli(conn, for_update=True)
        if dane is None:
            row = conn.execute("SELECT payload FROM glovaro_state WHERE id = 1 FOR UPDATE").fetchone()
            if row:
                payload = row[0]
                dane = payload if isinstance(payload, dict) else json.loads(payload)

        wynik, dane_do_zapisu = mutator(dane)
        if dane_do_zapisu is not None:
            _zapisz_salony_do_tabeli(conn, dane_do_zapisu)
            conn.execute(
                """
                INSERT INTO glovaro_state (id, payload, updated_at)
                VALUES (1, %s, NOW())
                ON CONFLICT (id) DO UPDATE
                SET payload = EXCLUDED.payload, updated_at = NOW()
                """,
                (Jsonb(dane_do_zapisu),),
            )
            conn.commit()
            _wykonaj_backup_pliku(dane_do_zapisu)
        return wynik


def aktualizuj_salon_raw(salon_slug: str, mutator):
    """Atomowo aktualizuje tylko jeden panel firmy, bez blokowania wszystkich salonów.

    mutator(salon) zwraca tuple: (wynik, salon_do_zapisu). Dla PostgreSQL
    blokowany jest wyłącznie rekord glovaro_salons.slug, co istotnie zmniejsza
    konflikt przy równoczesnych rezerwacjach w różnych firmach.
    """
    if uzywaj_postgres():
        return _aktualizuj_salon_postgres(salon_slug, mutator)

    with _FILE_LOCK:
        dane = _wczytaj_plik() or {"salony": {}}
        salony = dane.setdefault("salony", {})
        wynik, salon_do_zapisu = mutator(salony.get(salon_slug))
        if salon_do_zapisu is not None:
            salony[salon_slug] = salon_do_zapisu
            _wykonaj_backup_pliku_przed_zapisem()
            _zapisz_plik(dane)
        return wynik


def _aktualizuj_salon_postgres(salon_slug: str, mutator):
    import psycopg
    from psycopg.types.json import Jsonb

    with psycopg.connect(DATABASE_URL) as conn:
        row = conn.execute(
            "SELECT payload FROM glovaro_salons WHERE slug = %s FOR UPDATE",
            (salon_slug,),
        ).fetchone()
        if row:
            payload = row[0]
            salon = payload if isinstance(payload, dict) else json.loads(payload)
        else:
            legacy = conn.execute("SELECT payload FROM glovaro_state WHERE id = 1 FOR UPDATE").fetchone()
            dane = None
            if legacy:
                payload = legacy[0]
                dane = payload if isinstance(payload, dict) else json.loads(payload)
                _zapisz_salony_do_tabeli(conn, dane)
                salon = (dane.get("salony") or {}).get(salon_slug)
            else:
                salon = None

        wynik, salon_do_zapisu = mutator(salon)
        if salon_do_zapisu is not None:
            conn.execute(
                """
                INSERT INTO glovaro_salons (
                    slug, payload, nazwa_salonu, branza, abonament_status, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (slug) DO UPDATE
                SET payload = EXCLUDED.payload,
                    nazwa_salonu = EXCLUDED.nazwa_salonu,
                    branza = EXCLUDED.branza,
                    abonament_status = EXCLUDED.abonament_status,
                    updated_at = NOW()
                """,
                (
                    salon_slug,
                    Jsonb(salon_do_zapisu),
                    str(salon_do_zapisu.get("nazwa_salonu", "")),
                    str(salon_do_zapisu.get("branza", "")),
                    str(salon_do_zapisu.get("abonament_status", "")),
                ),
            )
            conn.commit()
        return wynik


def _wczytaj_salony_z_tabeli(conn, for_update: bool = False) -> dict | None:
    query = "SELECT slug, payload FROM glovaro_salons"
    if for_update:
        query += " FOR UPDATE"
    rows = conn.execute(query).fetchall()
    if not rows:
        return None
    salony = {}
    for slug, payload in rows:
        salon = payload if isinstance(payload, dict) else json.loads(payload)
        salon.setdefault("slug", slug)
        salony[slug] = salon
    return {"salony": salony}


def _zapisz_salony_do_tabeli(conn, dane: dict) -> None:
    from psycopg.types.json import Jsonb

    salony = dane.get("salony", {}) if isinstance(dane, dict) else {}
    aktualne_slugi = set()
    for slug, salon in salony.items():
        if not isinstance(salon, dict):
            continue
        aktualne_slugi.add(slug)
        salon = {**salon, "slug": salon.get("slug") or slug}
        conn.execute(
            """
            INSERT INTO glovaro_salons (
                slug, payload, nazwa_salonu, branza, abonament_status, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (slug) DO UPDATE
            SET payload = EXCLUDED.payload,
                nazwa_salonu = EXCLUDED.nazwa_salonu,
                branza = EXCLUDED.branza,
                abonament_status = EXCLUDED.abonament_status,
                updated_at = NOW()
            """,
            (
                slug,
                Jsonb(salon),
                str(salon.get("nazwa_salonu", "")),
                str(salon.get("branza", "")),
                str(salon.get("abonament_status", "")),
            ),
        )
    if aktualne_slugi:
        conn.execute("DELETE FROM glovaro_salons WHERE NOT (slug = ANY(%s))", (list(aktualne_slugi),))
    else:
        conn.execute("DELETE FROM glovaro_salons")


def _migruj_state_do_salonow(conn) -> None:
    istnieja_salony = conn.execute("SELECT 1 FROM glovaro_salons LIMIT 1").fetchone()
    if istnieja_salony:
        return
    row = conn.execute("SELECT payload FROM glovaro_state WHERE id = 1").fetchone()
    if not row:
        return
    payload = row[0]
    dane = payload if isinstance(payload, dict) else json.loads(payload)
    _zapisz_salony_do_tabeli(conn, dane)
    logger.info("Przeniesiono dane z glovaro_state do tabeli glovaro_salons")


def _migruj_plik_do_postgres_jesli_trzeba() -> None:
    if _wczytaj_postgres() is not None:
        return
    dane_z_pliku = _wczytaj_plik()
    if dane_z_pliku:
        _zapisz_postgres(dane_z_pliku)
        logger.info("Przeniesiono dane z pliku salon.json do PostgreSQL")
