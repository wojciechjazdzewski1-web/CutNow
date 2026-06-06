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


def wczytaj_salon_raw(
    salon_slug: str,
    *,
    data_od: str | None = None,
    data_do: str | None = None,
    include_clients: bool = True,
    include_reservations: bool = True,
    include_free_slots: bool = True,
    include_waitlist: bool = True,
) -> dict | None:
    if uzywaj_postgres():
        return _wczytaj_salon_postgres(
            salon_slug,
            data_od=data_od,
            data_do=data_do,
            include_clients=include_clients,
            include_reservations=include_reservations,
            include_free_slots=include_free_slots,
            include_waitlist=include_waitlist,
        )
    dane = _wczytaj_plik() or {}
    salon = (dane.get("salony") or {}).get(salon_slug)
    return salon if isinstance(salon, dict) else None


def wczytaj_salony_raw(
    *,
    data_od: str | None = None,
    data_do: str | None = None,
    include_clients: bool = True,
    include_reservations: bool = True,
    include_free_slots: bool = True,
    include_waitlist: bool = True,
) -> dict | None:
    if uzywaj_postgres():
        return _wczytaj_postgres(
            data_od=data_od,
            data_do=data_do,
            include_clients=include_clients,
            include_reservations=include_reservations,
            include_free_slots=include_free_slots,
            include_waitlist=include_waitlist,
        )
    return _wczytaj_plik()


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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS glovaro_reservations (
                salon_slug TEXT NOT NULL REFERENCES glovaro_salons(slug) ON DELETE CASCADE,
                id TEXT NOT NULL,
                data_iso TEXT NOT NULL DEFAULT '',
                godzina TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT '',
                klient_id TEXT NOT NULL DEFAULT '',
                telefon TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                payload JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (salon_slug, id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_glovaro_reservations_salon_data ON glovaro_reservations (salon_slug, data_iso, godzina)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_glovaro_reservations_salon_status ON glovaro_reservations (salon_slug, status, data_iso)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS glovaro_free_slots (
                salon_slug TEXT NOT NULL REFERENCES glovaro_salons(slug) ON DELETE CASCADE,
                data_iso TEXT NOT NULL,
                godzina TEXT NOT NULL,
                PRIMARY KEY (salon_slug, data_iso, godzina)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_glovaro_free_slots_salon_data ON glovaro_free_slots (salon_slug, data_iso)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS glovaro_clients (
                salon_slug TEXT NOT NULL REFERENCES glovaro_salons(slug) ON DELETE CASCADE,
                id TEXT NOT NULL,
                telefon TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                ostatnia_wizyta TEXT NOT NULL DEFAULT '',
                payload JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (salon_slug, id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_glovaro_clients_salon_phone ON glovaro_clients (salon_slug, telefon)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS glovaro_waitlist (
                salon_slug TEXT NOT NULL REFERENCES glovaro_salons(slug) ON DELETE CASCADE,
                id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '',
                data_preferowana TEXT NOT NULL DEFAULT '',
                payload JSONB NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (salon_slug, id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_glovaro_waitlist_salon_status ON glovaro_waitlist (salon_slug, status, data_preferowana)"
        )
        conn.commit()
        _migruj_state_do_salonow(conn)
        _uzupelnij_szczegoly_salonow_jesli_puste(conn)


def _wczytaj_postgres(
    *,
    data_od: str | None = None,
    data_do: str | None = None,
    include_clients: bool = True,
    include_reservations: bool = True,
    include_free_slots: bool = True,
    include_waitlist: bool = True,
) -> dict | None:
    import psycopg

    with psycopg.connect(DATABASE_URL) as conn:
        dane_salonow = _wczytaj_salony_z_tabeli(
            conn,
            data_od=data_od,
            data_do=data_do,
            include_clients=include_clients,
            include_reservations=include_reservations,
            include_free_slots=include_free_slots,
            include_waitlist=include_waitlist,
        )
        if dane_salonow is not None:
            return dane_salonow
        row = conn.execute("SELECT payload FROM glovaro_state WHERE id = 1").fetchone()
    if not row:
        return None
    payload = row[0]
    if isinstance(payload, dict):
        return payload
    return json.loads(payload)


def _wczytaj_salon_postgres(
    salon_slug: str,
    *,
    data_od: str | None = None,
    data_do: str | None = None,
    include_clients: bool = True,
    include_reservations: bool = True,
    include_free_slots: bool = True,
    include_waitlist: bool = True,
) -> dict | None:
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
            _wczytaj_szczegoly_salonu_z_tabel(
                conn,
                salon_slug,
                salon,
                data_od=data_od,
                data_do=data_do,
                include_clients=include_clients,
                include_reservations=include_reservations,
                include_free_slots=include_free_slots,
                include_waitlist=include_waitlist,
            )
            return salon

        legacy = conn.execute("SELECT payload FROM glovaro_state WHERE id = 1").fetchone()
        if not legacy:
            return None
        payload = legacy[0]
        dane = payload if isinstance(payload, dict) else json.loads(payload)
        salon = (dane.get("salony") or {}).get(salon_slug)
        if isinstance(salon, dict):
            salon.setdefault("slug", salon_slug)
            _wczytaj_szczegoly_salonu_z_tabel(
                conn,
                salon_slug,
                salon,
                data_od=data_od,
                data_do=data_do,
                include_clients=include_clients,
                include_reservations=include_reservations,
                include_free_slots=include_free_slots,
                include_waitlist=include_waitlist,
            )
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
            _sanityzuj_salon_payload(salon)
            _wczytaj_szczegoly_salonu_z_tabel(conn, salon_slug, salon)
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
                    Jsonb(_lekki_payload_salonu(salon_do_zapisu)),
                    str(salon_do_zapisu.get("nazwa_salonu", "")),
                    str(salon_do_zapisu.get("branza", "")),
                    str(salon_do_zapisu.get("abonament_status", "")),
                ),
            )
            _zapisz_szczegoly_salonu_do_tabel(conn, salon_slug, salon_do_zapisu)
            conn.commit()
        return wynik


def _wczytaj_salony_z_tabeli(
    conn,
    for_update: bool = False,
    *,
    data_od: str | None = None,
    data_do: str | None = None,
    include_clients: bool = True,
    include_reservations: bool = True,
    include_free_slots: bool = True,
    include_waitlist: bool = True,
) -> dict | None:
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
        _wczytaj_szczegoly_salonu_z_tabel(
            conn,
            slug,
            salon,
            data_od=data_od,
            data_do=data_do,
            include_clients=include_clients,
            include_reservations=include_reservations,
            include_free_slots=include_free_slots,
            include_waitlist=include_waitlist,
        )
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
                Jsonb(_lekki_payload_salonu(salon)),
                str(salon.get("nazwa_salonu", "")),
                str(salon.get("branza", "")),
                str(salon.get("abonament_status", "")),
            ),
        )
        _zapisz_szczegoly_salonu_do_tabel(conn, slug, salon)
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


def _uzupelnij_szczegoly_salonow_jesli_puste(conn) -> None:
    istnieja_szczegoly = conn.execute(
        """
        SELECT 1 FROM glovaro_reservations
        UNION ALL SELECT 1 FROM glovaro_free_slots
        UNION ALL SELECT 1 FROM glovaro_clients
        UNION ALL SELECT 1 FROM glovaro_waitlist
        LIMIT 1
        """
    ).fetchone()
    if istnieja_szczegoly:
        return

    rows = conn.execute("SELECT slug, payload FROM glovaro_salons").fetchall()
    uzupelniono = 0
    for slug, payload in rows:
        salon = payload if isinstance(payload, dict) else json.loads(payload)
        if any(salon.get(klucz) for klucz in ("rezerwacje", "wolne_terminy", "klienci", "lista_rezerwowa")):
            _zapisz_szczegoly_salonu_do_tabel(conn, slug, salon)
            uzupelniono += 1

    if uzupelniono:
        logger.info("Uzupełniono tabele szczegółów z glovaro_salons dla %s salonów", uzupelniono)
        return

    row = conn.execute("SELECT payload FROM glovaro_state WHERE id = 1").fetchone()
    if not row:
        return
    payload = row[0]
    dane = payload if isinstance(payload, dict) else json.loads(payload)
    for slug, salon in (dane.get("salony") or {}).items():
        if isinstance(salon, dict):
            _zapisz_szczegoly_salonu_do_tabel(conn, slug, salon)
            uzupelniono += 1
    if uzupelniono:
        logger.info("Uzupełniono tabele szczegółów z glovaro_state dla %s salonów", uzupelniono)


def _lekki_payload_salonu(salon: dict) -> dict:
    payload = dict(salon)
    for klucz in ("rezerwacje", "wolne_terminy", "klienci", "lista_rezerwowa"):
        payload.pop(klucz, None)
    return payload


def _zapisz_szczegoly_salonu_do_tabel(conn, salon_slug: str, salon: dict) -> None:
    from psycopg.types.json import Jsonb

    conn.execute("DELETE FROM glovaro_reservations WHERE salon_slug = %s", (salon_slug,))
    for rezerwacja in salon.get("rezerwacje", []) or []:
        if not isinstance(rezerwacja, dict):
            continue
        rezerwacja_id = str(rezerwacja.get("id") or "")
        if not rezerwacja_id:
            continue
        conn.execute(
            """
            INSERT INTO glovaro_reservations (
                salon_slug, id, data_iso, godzina, status, klient_id, telefon, email, payload, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (salon_slug, id) DO UPDATE
            SET data_iso = EXCLUDED.data_iso,
                godzina = EXCLUDED.godzina,
                status = EXCLUDED.status,
                klient_id = EXCLUDED.klient_id,
                telefon = EXCLUDED.telefon,
                email = EXCLUDED.email,
                payload = EXCLUDED.payload,
                updated_at = NOW()
            """,
            (
                salon_slug,
                rezerwacja_id,
                str(rezerwacja.get("data", "")),
                str(rezerwacja.get("godzina", "")),
                str(rezerwacja.get("status", "")),
                str(rezerwacja.get("klient_id", "")),
                str(rezerwacja.get("telefon", "")),
                str(rezerwacja.get("email", "")),
                Jsonb(rezerwacja),
            ),
        )

    conn.execute("DELETE FROM glovaro_free_slots WHERE salon_slug = %s", (salon_slug,))
    for data_iso, godziny in (salon.get("wolne_terminy", {}) or {}).items():
        if not isinstance(godziny, list):
            continue
        for godzina in godziny:
            if not godzina:
                continue
            conn.execute(
                """
                INSERT INTO glovaro_free_slots (salon_slug, data_iso, godzina)
                VALUES (%s, %s, %s)
                ON CONFLICT (salon_slug, data_iso, godzina) DO NOTHING
                """,
                (salon_slug, str(data_iso), str(godzina)),
            )

    conn.execute("DELETE FROM glovaro_clients WHERE salon_slug = %s", (salon_slug,))
    for klient in salon.get("klienci", []) or []:
        if not isinstance(klient, dict):
            continue
        klient_id = str(klient.get("id") or "")
        if not klient_id:
            continue
        conn.execute(
            """
            INSERT INTO glovaro_clients (
                salon_slug, id, telefon, email, ostatnia_wizyta, payload, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (salon_slug, id) DO UPDATE
            SET telefon = EXCLUDED.telefon,
                email = EXCLUDED.email,
                ostatnia_wizyta = EXCLUDED.ostatnia_wizyta,
                payload = EXCLUDED.payload,
                updated_at = NOW()
            """,
            (
                salon_slug,
                klient_id,
                str(klient.get("telefon", "")),
                str(klient.get("email", "")),
                str(klient.get("ostatnia_wizyta", "")),
                Jsonb(klient),
            ),
        )

    conn.execute("DELETE FROM glovaro_waitlist WHERE salon_slug = %s", (salon_slug,))
    for zgloszenie in salon.get("lista_rezerwowa", []) or []:
        if not isinstance(zgloszenie, dict):
            continue
        zgloszenie_id = str(zgloszenie.get("id") or "")
        if not zgloszenie_id:
            continue
        conn.execute(
            """
            INSERT INTO glovaro_waitlist (
                salon_slug, id, status, data_preferowana, payload, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, NOW())
            ON CONFLICT (salon_slug, id) DO UPDATE
            SET status = EXCLUDED.status,
                data_preferowana = EXCLUDED.data_preferowana,
                payload = EXCLUDED.payload,
                updated_at = NOW()
            """,
            (
                salon_slug,
                zgloszenie_id,
                str(zgloszenie.get("status", "")),
                str(zgloszenie.get("data_preferowana", "")),
                Jsonb(zgloszenie),
            ),
        )


def _normalizuj_godzine_slot(wartosc) -> str:
    tekst = str(wartosc or "").strip()
    if not tekst:
        return ""
    czesci = tekst.split(":")
    if len(czesci) >= 2:
        try:
            return f"{int(czesci[0]):02d}:{int(czesci[1]):02d}"
        except ValueError:
            return tekst
    return tekst


def _filtr_daty(kolumna: str, data_od: str | None, data_do: str | None) -> tuple[str, list[str]]:
    warunki: list[str] = []
    parametry: list[str] = []
    if data_od:
        warunki.append(f"{kolumna} >= %s")
        parametry.append(data_od)
    if data_do:
        warunki.append(f"{kolumna} <= %s")
        parametry.append(data_do)
    return (" AND " + " AND ".join(warunki) if warunki else ""), parametry


def _sanityzuj_salon_payload(salon: dict) -> None:
    for klucz in (
        "zdjecia_prac",
        "pracownicy",
        "uslugi",
        "blokady",
        "rezerwacje",
        "lista_rezerwowa",
        "opinie",
        "klienci",
        "pytania_wywiadu",
    ):
        if not isinstance(salon.get(klucz), list):
            salon[klucz] = []
    if not isinstance(salon.get("wolne_terminy"), dict):
        salon["wolne_terminy"] = {}
    if not isinstance(salon.get("godziny_pracy"), dict):
        salon["godziny_pracy"] = {}


def _wczytaj_szczegoly_salonu_z_tabel(
    conn,
    salon_slug: str,
    salon: dict,
    *,
    data_od: str | None = None,
    data_do: str | None = None,
    include_clients: bool = True,
    include_reservations: bool = True,
    include_free_slots: bool = True,
    include_waitlist: bool = True,
) -> None:
    _sanityzuj_salon_payload(salon)
    filtr, parametry = _filtr_daty("data_iso", data_od, data_do)
    if include_reservations:
        rows = conn.execute(
            f"""
            SELECT data_iso, godzina, payload
            FROM glovaro_reservations
            WHERE salon_slug = %s{filtr}
            ORDER BY data_iso, godzina
            """,
            [salon_slug, *parametry],
        ).fetchall()
        rezerwacje: list[dict] = []
        for data_iso, godzina, payload in rows:
            rezerwacja = payload if isinstance(payload, dict) else json.loads(payload)
            if not isinstance(rezerwacja, dict):
                continue
            if data_iso and not str(rezerwacja.get("data") or "").strip():
                rezerwacja["data"] = str(data_iso)
            if godzina and not str(rezerwacja.get("godzina") or "").strip():
                rezerwacja["godzina"] = str(godzina)
            rezerwacje.append(rezerwacja)
        salon["rezerwacje"] = rezerwacje

    if include_free_slots:
        rows = conn.execute(
            f"""
            SELECT data_iso, godzina
            FROM glovaro_free_slots
            WHERE salon_slug = %s{filtr}
            ORDER BY data_iso, godzina
            """,
            [salon_slug, *parametry],
        ).fetchall()
        wolne: dict[str, list[str]] = {}
        for data_iso, godzina in rows:
            slot = _normalizuj_godzine_slot(godzina)
            if not slot:
                continue
            wolne.setdefault(str(data_iso), []).append(slot)
        for data_key in wolne:
            wolne[data_key] = sorted(set(wolne[data_key]))
        salon["wolne_terminy"] = wolne

    if include_clients:
        rows = conn.execute(
            """
            SELECT payload
            FROM glovaro_clients
            WHERE salon_slug = %s
            ORDER BY ostatnia_wizyta DESC
            """,
            (salon_slug,),
        ).fetchall()
        if rows:
            salon["klienci"] = [
                payload if isinstance(payload, dict) else json.loads(payload)
                for (payload,) in rows
            ]

    if include_waitlist:
        rows = conn.execute(
            """
            SELECT payload
            FROM glovaro_waitlist
            WHERE salon_slug = %s
            ORDER BY data_preferowana DESC
            """,
            (salon_slug,),
        ).fetchall()
        if rows:
            salon["lista_rezerwowa"] = [
                payload if isinstance(payload, dict) else json.loads(payload)
                for (payload,) in rows
            ]


def _migruj_plik_do_postgres_jesli_trzeba() -> None:
    if _wczytaj_postgres() is not None:
        return
    dane_z_pliku = _wczytaj_plik()
    if dane_z_pliku:
        _zapisz_postgres(dane_z_pliku)
        logger.info("Przeniesiono dane z pliku salon.json do PostgreSQL")
