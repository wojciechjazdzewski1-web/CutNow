"""Glovaro — panel rezerwacji dla salonów beauty & wellness."""

from __future__ import annotations

import copy
import calendar
import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import smtplib
import time
import uuid
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlparse

from flask import Flask, Response, flash, jsonify, redirect, render_template, request, send_from_directory, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from PIL import Image, ImageOps
except ImportError:  # Pillow jest opcjonalny lokalnie; na produkcji dodany w requirements.
    Image = None
    ImageOps = None

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))

from storage import (
    aktualizuj_raw,
    aktualizuj_salon_raw,
    init_storage,
    tryb_magazynu,
    wczytaj_raw,
    wczytaj_salon_raw,
    wczytaj_salony_raw,
    zapisz_raw,
)

PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "").strip()
SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587").strip() or "587")
SMTP_USERNAME = os.environ.get("SMTP_USERNAME", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").replace(" ", "").strip()
SMTP_FROM = os.environ.get("SMTP_FROM", SMTP_USERNAME or "powiadomienia@glovaro.local").strip()
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
RESEND_FROM = os.environ.get("RESEND_FROM", SMTP_FROM).strip()
REMINDER_SECRET = os.environ.get("REMINDER_SECRET", "").strip()
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()
LEGAL_COMPANY_NAME = os.environ.get("LEGAL_COMPANY_NAME", "Wojciech Jażdżewski").strip()
LEGAL_COMPANY_ADDRESS = os.environ.get("LEGAL_COMPANY_ADDRESS", "Miastecka 6/2").strip()
LEGAL_COMPANY_EMAIL = os.environ.get("LEGAL_COMPANY_EMAIL", "glovaro.pl@glovaro.pl").strip()
LEGAL_COMPANY_NIP = os.environ.get("LEGAL_COMPANY_NIP", "").strip()
LEGAL_PLACEHOLDERS = frozenset(
    {"", "Uzupełnij adres firmy", "Uzupełnij NIP", "kontakt@example.com", "0000000000"}
)
LEGAL_UNREGISTERED_ACTIVITY = (
    os.environ.get("LEGAL_UNREGISTERED_ACTIVITY", "true").strip().lower()
)
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://glovaro.pl").strip().rstrip("/")
REZERWACJA_RATE_LIMIT: dict[str, list[float]] = {}
GODZIN_PO_WIZYCIE_DO_ARCHIWUM = 1
DNI_W_ARCHIWUM_PRZED_USUNIECIEM = 90
MINUTY_NA_OPLACENIE_REZERWACJI = 15
CZYSZCZENIE_REZERWACJI_CO_SEK = 600
KATALOG_CACHE_TTL_SEK = 60
WOLNY_REQUEST_LOG_SEK = 0.75
MAKS_UPLOAD_ZDJECIA_BAJTOW = 2_500_000
MAKS_ZDJECIE_PO_KOMPRESJI_BAJTOW = 650_000
MAKS_WYMIAR_ZDJECIA = 1600
_ostatnie_czyszczenie_rezerwacji = 0.0
_katalog_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}

DNI_TYGODNIA = [
    ("poniedzialek", "Poniedziałek"),
    ("wtorek", "Wtorek"),
    ("sroda", "Środa"),
    ("czwartek", "Czwartek"),
    ("piatek", "Piątek"),
    ("sobota", "Sobota"),
    ("niedziela", "Niedziela"),
]

MIESIACE = [
    "",
    "Styczeń",
    "Luty",
    "Marzec",
    "Kwiecień",
    "Maj",
    "Czerwiec",
    "Lipiec",
    "Sierpień",
    "Wrzesień",
    "Październik",
    "Listopad",
    "Grudzień",
]

DEFAULT_SALON = {
    "nazwa_salonu": "Mój Salon",
    "branza": "beauty",
    "haslo_panelu": "",
    "opis": "",
    "telefon_kontaktowy": "",
    "adres_lokalizacji": "",
    "link_google_maps": "",
    "instagram": "",
    "email_powiadomien": "",
    "logo_url": "",
    "zdjecia_prac": [],
    "pracownicy": [],
    "uslugi": [],
    "motyw_strony": "rozowy",
    "abonament_status": "trial",
    "oplata_miesieczna": 100,
    "oplacone_do": "",
    "notatka_rozliczeniowa": "",
    "tryb_platnosci_wizyty": "w_salonie",
    "konto_bankowe": "",
    "odbiorca_przelewu": "",
    "link_szybkiej_platnosci": "",
    "platnosc_online_wlaczona": False,
    "cena_wizyty": 0,
    "interwal_terminow": 30,
    "automatyczne_terminy": False,
    "przypomnienia_email_wlaczone": True,
    "przypomnienie_godzin_przed": 24,
    "godziny_pracy": {
        key: {"otwarcie": "09:00", "zamkniecie": "18:00", "zamkniety": key == "niedziela"}
        for key, _ in DNI_TYGODNIA
    },
    "wolne_terminy": {},
    "blokady": [],
    "rezerwacje": [],
    "lista_rezerwowa": [],
    "opinie": [],
    "klienci": [],
    "pytania_wywiadu": [],
    "tresc_wywiadu_zdrowotnego": "",
    "wywiad_wlaczony": False,
    "wywiad_przy_rezerwacji": False,
}

PUBLIC_ENDPOINTS = {
    "strona_glowna",
    "dolacz_firma",
    "favicon_root",
    "robots_txt",
    "sitemap_xml",
    "health",
    "rezerwacja_domyslna",
    "rezerwacja_publiczna",
    "rezerwacja_formularz",
    "rezerwacja_potwierdzenie",
    "lista_rezerwowa_formularz",
    "anuluj_rezerwacje_klienta",
    "opinia_klienta",
    "stripe_webhook",
    "regulamin",
    "polityka_prywatnosci",
    "polityka_cookies",
    "panel_login",
    "static",
}

WIDOK_KLIENTA_ENDPOINTS = {
    "rezerwacja_domyslna",
    "rezerwacja_publiczna",
    "rezerwacja_formularz",
    "rezerwacja_potwierdzenie",
    "lista_rezerwowa_formularz",
    "anuluj_rezerwacje_klienta",
    "opinia_klienta",
}

MOTYW_ROZOWY_ENDPOINTS = WIDOK_KLIENTA_ENDPOINTS | {
    "strona_glowna",
    "dolacz_firma",
    "regulamin",
    "polityka_prywatnosci",
    "polityka_cookies",
}

PANEL_ENDPOINTS = frozenset(
    {
        "ustawienia_salonu",
        "godziny_pracy",
        "wolne_terminy",
        "podglad_klienta",
        "stripe_checkout",
        "stripe_sukces",
    }
)


def endpoint_ma_motyw_glovaro(endpoint: str | None) -> bool:
    if not endpoint:
        return False
    if endpoint in MOTYW_ROZOWY_ENDPOINTS:
        return True
    if endpoint in PANEL_ENDPOINTS or endpoint.startswith("panel"):
        return True
    return False


ENDPOINTS_GLOBALNE_BEZ_SALONU = frozenset(
    {
        "strona_glowna",
        "dolacz_firma",
        "regulamin",
        "polityka_prywatnosci",
        "polityka_cookies",
        "panel_lista",
        "panel_login",
        "panel_nowy_salon",
        "panel_wyloguj",
        "health",
        "favicon_root",
        "robots_txt",
        "sitemap_xml",
        "nie_znaleziono",
        "wyslij_przypomnienia",
        "stripe_webhook",
    }
)


def slug_salonu_dla_motywu(salon_slug_z_url: str | None, endpoint: str | None) -> str | None:
    if salon_slug_z_url:
        return salon_slug_z_url
    if not endpoint or endpoint in ENDPOINTS_GLOBALNE_BEZ_SALONU:
        return None
    if endpoint_ma_motyw_glovaro(endpoint):
        return pierwszy_zalogowany_salon_slug()
    return None

MOTYWY_STRONY = frozenset({"rozowy", "neutralny"})
STATUSY_ABONAMENTU = frozenset({"pending_payment", "trial", "active", "suspended"})

BRANZE_DZIALALNOSCI = [
    {
        "slug": "beauty",
        "nazwa": "Beauty / salon urody",
        "opis": "Paznokcie, kosmetologia, makijaż, brwi i rzęsy.",
    },
    {
        "slug": "fryzjer-barber",
        "nazwa": "Fryzjer / barber",
        "opis": "Strzyżenie, koloryzacja, broda i pielęgnacja włosów.",
    },
    {
        "slug": "detailing-samochodowy",
        "nazwa": "Detailing samochodowy",
        "opis": "Mycie premium, korekta lakieru, powłoki i wnętrza.",
    },
    {
        "slug": "fizjoterapia-masaz",
        "nazwa": "Fizjoterapia / masaż",
        "opis": "Zabiegi, masaże, rehabilitacja i konsultacje.",
    },
    {
        "slug": "trener-personalny",
        "nazwa": "Trener personalny",
        "opis": "Treningi indywidualne, konsultacje i plany.",
    },
    {
        "slug": "groomer",
        "nazwa": "Groomer / pielęgnacja zwierząt",
        "opis": "Strzyżenie, kąpiele i pielęgnacja psów oraz kotów.",
    },
    {
        "slug": "fotograf",
        "nazwa": "Fotograf",
        "opis": "Sesje zdjęciowe, studio, plener i konsultacje.",
    },
    {
        "slug": "korepetycje",
        "nazwa": "Korepetycje / lekcje",
        "opis": "Nauka języków, przedmioty szkolne, instrumenty.",
    },
    {
        "slug": "serwis-naprawy",
        "nazwa": "Serwis / naprawy",
        "opis": "Serwis rowerowy, opony, drobne naprawy i konsultacje.",
    },
    {
        "slug": "inne",
        "nazwa": "Inne",
        "opis": "Każda firma, która umawia klientów na konkretną godzinę.",
    },
]
BRANZE_MAP = {branza["slug"]: branza for branza in BRANZE_DZIALALNOSCI}


def slugify(wartosc: str) -> str:
    wartosc = wartosc.lower()
    zamiany = {
        "ą": "a",
        "ć": "c",
        "ę": "e",
        "ł": "l",
        "ń": "n",
        "ó": "o",
        "ś": "s",
        "ż": "z",
        "ź": "z",
    }
    for polski, ascii_znak in zamiany.items():
        wartosc = wartosc.replace(polski, ascii_znak)
    wartosc = re.sub(r"[^a-z0-9]+", "-", wartosc).strip("-")
    return wartosc or "salon"


def domyslny_slug(dane: dict) -> str:
    salony = dane.get("salony", {})
    if "demo" in salony:
        return "demo"
    return next(iter(salony), "demo")


def normalizuj_branze(wartosc: str | None) -> str:
    slug = (wartosc or "beauty").strip()
    return slug if slug in BRANZE_MAP else "beauty"


def etykieta_branzy(wartosc: str | None) -> str:
    return BRANZE_MAP[normalizuj_branze(wartosc)]["nazwa"]


def nowy_salon(nazwa: str = "Mój Salon", haslo: str = "") -> dict:
    salon = copy.deepcopy(DEFAULT_SALON)
    salon["nazwa_salonu"] = nazwa
    salon["haslo_panelu"] = haslo
    return salon


def migracja_danych(dane: dict) -> dict:
    if "salony" in dane:
        for slug, salon in dane["salony"].items():
            salon.setdefault("slug", slug)
            salon.setdefault("branza", "beauty")
            salon.setdefault("haslo_panelu", "")
            salon.setdefault("opis", "")
            salon.setdefault("telefon_kontaktowy", "")
            salon.setdefault("adres_lokalizacji", "")
            salon.setdefault("link_google_maps", "")
            salon.setdefault("instagram", "")
            salon.setdefault("email_powiadomien", "")
            salon.setdefault("logo_url", "")
            salon.setdefault("zdjecia_prac", [])
            salon.setdefault("pracownicy", [])
            salon.setdefault("uslugi", [])
            salon.setdefault("motyw_strony", "rozowy")
            salon.setdefault("abonament_status", "trial")
            salon.setdefault("oplata_miesieczna", 100)
            salon.setdefault("oplacone_do", "")
            salon.setdefault("notatka_rozliczeniowa", "")
            salon.setdefault("tryb_platnosci_wizyty", "w_salonie")
            salon.setdefault("konto_bankowe", "")
            salon.setdefault("odbiorca_przelewu", "")
            salon.setdefault("link_szybkiej_platnosci", "")
            salon.setdefault("platnosc_online_wlaczona", False)
            salon.setdefault("cena_wizyty", 0)
            salon.setdefault("interwal_terminow", 30)
            salon.setdefault("automatyczne_terminy", False)
            salon.setdefault("przypomnienia_email_wlaczone", True)
            salon.setdefault("przypomnienie_godzin_przed", 24)
            salon.setdefault("godziny_pracy", copy.deepcopy(DEFAULT_SALON["godziny_pracy"]))
            salon.setdefault("wolne_terminy", {})
            salon.setdefault("blokady", [])
            salon.setdefault("rezerwacje", [])
            salon.setdefault("lista_rezerwowa", [])
            salon.setdefault("opinie", [])
            salon.setdefault("klienci", [])
            salon.setdefault("pytania_wywiadu", [])
            salon.setdefault("tresc_wywiadu_zdrowotnego", "")
            salon.setdefault("wywiad_wlaczony", False)
            salon.setdefault("wywiad_przy_rezerwacji", False)
            synchronizuj_tresc_wywiadu_z_pytan(salon)
            for rezerwacja in salon["rezerwacje"]:
                rezerwacja.setdefault("status", "potwierdzona")
                rezerwacja.setdefault("token_anulowania", uuid.uuid4().hex)
                rezerwacja.setdefault("token_opinii", uuid.uuid4().hex)
                rezerwacja.setdefault("pracownik", "")
                rezerwacja.setdefault("klient_id", "")
                rezerwacja.setdefault("wywiad_wizyty", {})
            synchronizuj_kartoteke_salonu(salon)
            oczysc_anulowane_rezerwacje_salonu(salon)
        return dane

    # Stary format jednej strony zamieniamy na salon "demo", żeby nie stracić danych.
    salon = nowy_salon(dane.get("nazwa_salonu", "Mój Salon"), PANEL_PASSWORD)
    salon["godziny_pracy"] = dane.get("godziny_pracy", salon["godziny_pracy"])
    salon["wolne_terminy"] = dane.get("wolne_terminy", {})
    salon["rezerwacje"] = dane.get("rezerwacje", [])
    salon["opinie"] = dane.get("opinie", [])
    salon["uslugi"] = dane.get("uslugi", [])
    for rezerwacja in salon["rezerwacje"]:
        rezerwacja.setdefault("status", "potwierdzona")
        rezerwacja.setdefault("token_anulowania", uuid.uuid4().hex)
        rezerwacja.setdefault("token_opinii", uuid.uuid4().hex)
        rezerwacja.setdefault("pracownik", "")
    salon["slug"] = "demo"
    return {"salony": {"demo": salon}}


def datetime_rezerwacji(rezerwacja: dict) -> datetime | None:
    data_iso = rezerwacja.get("data", "")
    godzina = rezerwacja.get("godzina", "")
    if not waliduj_date_iso(data_iso) or not godzina:
        return None
    try:
        return datetime.strptime(f"{data_iso} {godzina}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def rezerwacja_w_archiwum(rezerwacja: dict) -> bool:
    return bool(rezerwacja.get("zarchiwizowano_at"))


def rezerwacja_aktywna_w_panelu(rezerwacja: dict) -> bool:
    """Wizyty widoczne w panelu salonu (bez anulowanych i odrzuconych)."""
    if rezerwacja_w_archiwum(rezerwacja):
        return False
    return rezerwacja.get("status", "potwierdzona") not in {"anulowana", "odrzucona"}


def usun_rezerwacje_z_salonu(salon: dict, rezerwacja_id: str) -> bool:
    przed = len(salon.get("rezerwacje", []))
    salon["rezerwacje"] = [
        r for r in salon.get("rezerwacje", []) if r.get("id") != rezerwacja_id
    ]
    return len(salon["rezerwacje"]) < przed


def oczysc_anulowane_rezerwacje_salonu(salon: dict) -> bool:
    przed = len(salon.get("rezerwacje", []))
    salon["rezerwacje"] = [
        r
        for r in salon.get("rezerwacje", [])
        if r.get("status") not in {"anulowana", "odrzucona"}
    ]
    return len(salon["rezerwacje"]) < przed


def rezerwacja_gotowa_do_archiwizacji(rezerwacja: dict, teraz: datetime | None = None) -> bool:
    if rezerwacja_w_archiwum(rezerwacja):
        return False
    if rezerwacja.get("status") in {"anulowana", "odrzucona"}:
        return True
    termin = datetime_rezerwacji(rezerwacja)
    if not termin:
        return False
    teraz = teraz or datetime.now()
    return teraz >= termin + timedelta(hours=GODZIN_PO_WIZYCIE_DO_ARCHIWUM)


def archiwizuj_rezerwacje(rezerwacja: dict, teraz: datetime | None = None) -> None:
    if rezerwacja_w_archiwum(rezerwacja):
        return
    teraz = teraz or datetime.now()
    rezerwacja["zarchiwizowano_at"] = teraz.isoformat(timespec="minutes")
    if rezerwacja.get("status") in {"oczekuje", "potwierdzona"}:
        rezerwacja["status"] = "zakonczona"


def rezerwacja_do_usuniecia(rezerwacja: dict, teraz: datetime | None = None) -> bool:
    """Usuń wpisy z archiwum starsze niż 90 dni (liczone od terminu wizyty)."""
    termin = datetime_rezerwacji(rezerwacja)
    if not termin:
        return bool(rezerwacja_w_archiwum(rezerwacja))
    teraz = teraz or datetime.now()
    granica = teraz - timedelta(days=DNI_W_ARCHIWUM_PRZED_USUNIECIEM)
    return termin < granica


def rezerwacja_nieoplacona_do_zwolnienia(salon: dict, rezerwacja: dict, teraz: datetime | None = None) -> bool:
    if rezerwacja.get("status") != "oczekuje" or rezerwacja_oplacona(rezerwacja):
        return False
    wymaga_online = bool(
        rezerwacja.get("wymaga_platnosci_online")
        or (
            rezerwacja.get("zrodlo") == "online"
            and salon.get("platnosc_online_wlaczona")
            and kwota_rezerwacji_zl(salon, rezerwacja) > 0
        )
    )
    if not wymaga_online:
        return False
    try:
        utworzono = datetime.fromisoformat(str(rezerwacja.get("utworzono", "")))
    except ValueError:
        return False
    teraz = teraz or datetime.now()
    return teraz >= utworzono + timedelta(minutes=MINUTY_NA_OPLACENIE_REZERWACJI)


def utrzymuj_rezerwacje(dane: dict) -> tuple[int, int, int]:
    teraz = datetime.now()
    zarchiwizowane = 0
    usuniete = 0
    zwolnione_nieoplacone = 0
    for salon in dane.get("salony", {}).values():
        rezerwacje = []
        for rezerwacja in salon.get("rezerwacje", []):
            if rezerwacja_nieoplacona_do_zwolnienia(salon, rezerwacja, teraz):
                przywroc_wolny_termin(salon, rezerwacja)
                zwolnione_nieoplacone += 1
                continue
            if rezerwacja_gotowa_do_archiwizacji(rezerwacja, teraz):
                archiwizuj_rezerwacje(rezerwacja, teraz)
                zarchiwizowane += 1
            rezerwacje.append(rezerwacja)
        pozostale = [r for r in rezerwacje if not rezerwacja_do_usuniecia(r, teraz)]
        usuniete += len(rezerwacje) - len(pozostale)
        salon["rezerwacje"] = pozostale
    return zarchiwizowane, usuniete, zwolnione_nieoplacone


def moze_wykonac_czyszczenie_rezerwacji() -> bool:
    global _ostatnie_czyszczenie_rezerwacji
    teraz = time.time()
    if teraz - _ostatnie_czyszczenie_rezerwacji < CZYSZCZENIE_REZERWACJI_CO_SEK:
        return False
    _ostatnie_czyszczenie_rezerwacji = teraz
    return True


def wczytaj_dane() -> dict:
    dane = wczytaj_raw()
    if dane is None:
        dane = {"salony": {"demo": {**nowy_salon("Mój Salon", PANEL_PASSWORD), "slug": "demo"}}}
        zapisz_dane(dane)
        return dane

    zmigrowane = migracja_danych(dane)
    zapisz = zmigrowane != dane
    for salon in zmigrowane.get("salony", {}).values():
        if (
            oczysc_uslugi_w_salonie(salon)
            or oczysc_anulowane_rezerwacje_salonu(salon)
            or synchronizuj_tresc_wywiadu_z_pytan(salon)
        ):
            zapisz = True
    if zapisz:
        zapisz_dane(zmigrowane)
    dane = zmigrowane

    if moze_wykonac_czyszczenie_rezerwacji():
        def czyszczenie_atomowe(dane_atomowe: dict):
            wyniki = utrzymuj_rezerwacje(dane_atomowe)
            return wyniki, copy.deepcopy(dane_atomowe)

        (zarchiwizowane, usuniete, zwolnione_nieoplacone), dane = aktualizuj_dane_atomowo(czyszczenie_atomowe)
        if zarchiwizowane or usuniete or zwolnione_nieoplacone:
            app.logger.info(
                "Rezerwacje: zarchiwizowano %s, usunięto %s, zwolniono nieopłacone %s",
                zarchiwizowane,
                usuniete,
                zwolnione_nieoplacone,
            )
    return dane


def zapisz_dane(dane: dict) -> None:
    zapisz_raw(dane)
    wyczysc_cache_katalogu()


def aktualizuj_dane_atomowo(mutator):
    def wrapper(raw: dict | None):
        dane = raw
        if dane is None:
            dane = {"salony": {"demo": {**nowy_salon("Mój Salon", PANEL_PASSWORD), "slug": "demo"}}}
        dane = migracja_danych(dane)
        wynik = mutator(dane)
        return wynik, dane

    wynik = aktualizuj_raw(wrapper)
    wyczysc_cache_katalogu()
    return wynik


def aktualizuj_salon_atomowo(salon_slug: str, mutator):
    def wrapper(raw_salon: dict | None):
        if raw_salon is None:
            return mutator(None), None

        dane = migracja_danych({"salony": {salon_slug: raw_salon}})
        salon = dane["salony"][salon_slug]
        oczysc_uslugi_w_salonie(salon)
        oczysc_anulowane_rezerwacje_salonu(salon)
        synchronizuj_tresc_wywiadu_z_pytan(salon)
        wynik = mutator(salon)
        return wynik, copy.deepcopy(salon)

    wynik = aktualizuj_salon_raw(salon_slug, wrapper)
    wyczysc_cache_katalogu()
    return wynik


def wyczysc_cache_katalogu() -> None:
    _katalog_cache.clear()


def pobierz_salon(dane: dict, salon_slug: str) -> dict | None:
    salon = dane.get("salony", {}).get(salon_slug)
    if salon:
        salon.setdefault("slug", salon_slug)
    return salon


def przygotuj_salon_z_raw(salon_slug: str, raw_salon: dict | None) -> dict | None:
    if raw_salon is None:
        return None
    dane = migracja_danych({"salony": {salon_slug: raw_salon}})
    salon = pobierz_salon(dane, salon_slug)
    if not salon:
        return None
    oczysc_uslugi_w_salonie(salon)
    oczysc_anulowane_rezerwacje_salonu(salon)
    synchronizuj_tresc_wywiadu_z_pytan(salon)
    return salon


def wczytaj_salon_bezposrednio(
    salon_slug: str,
    *,
    data_od: str | None = None,
    data_do: str | None = None,
    include_clients: bool = True,
    include_reservations: bool = True,
    include_free_slots: bool = True,
    include_waitlist: bool = True,
) -> dict | None:
    return przygotuj_salon_z_raw(
        salon_slug,
        wczytaj_salon_raw(
            salon_slug,
            data_od=data_od,
            data_do=data_do,
            include_clients=include_clients,
            include_reservations=include_reservations,
            include_free_slots=include_free_slots,
            include_waitlist=include_waitlist,
        ),
    )


def zakres_publicznej_rezerwacji(data_iso: str | None = None) -> tuple[str, str]:
    start = date.today()
    if data_iso and waliduj_date_iso(data_iso):
        wybrana = datetime.strptime(data_iso, "%Y-%m-%d").date()
        if wybrana > start:
            start = min(start, wybrana)
    koniec = start + timedelta(days=HORYZONT_REZERWACJI_DNI)
    return start.isoformat(), koniec.isoformat()


def wczytaj_dane_katalogowe() -> dict:
    data_od, data_do = zakres_publicznej_rezerwacji()
    dane = wczytaj_salony_raw(
        data_od=data_od,
        data_do=data_do,
        include_clients=False,
        include_reservations=True,
        include_free_slots=True,
        include_waitlist=False,
    )
    if dane is None:
        return wczytaj_dane()
    dane = migracja_danych(dane)
    for salon in dane.get("salony", {}).values():
        oczysc_uslugi_w_salonie(salon)
        oczysc_anulowane_rezerwacje_salonu(salon)
        synchronizuj_tresc_wywiadu_z_pytan(salon)
    return dane


def zakres_panelu_pulpit() -> tuple[str, str]:
    dzisiaj = date.today()
    return dzisiaj.isoformat(), (dzisiaj + timedelta(days=180)).isoformat()


def zakres_miesiaca(data_iso: str) -> tuple[str, str]:
    data_wybrana = datetime.strptime(data_iso, "%Y-%m-%d").date()
    pierwszy = data_wybrana.replace(day=1)
    ostatni = pierwszy.replace(day=calendar.monthrange(data_wybrana.year, data_wybrana.month)[1])
    return pierwszy.isoformat(), ostatni.isoformat()


def zakres_panelu_rezerwacji(widok: str) -> tuple[str, str]:
    dzisiaj = date.today()
    if widok == "archiwum":
        return (dzisiaj - timedelta(days=DNI_W_ARCHIWUM_PRZED_USUNIECIEM)).isoformat(), dzisiaj.isoformat()
    return (dzisiaj - timedelta(days=1)).isoformat(), (dzisiaj + timedelta(days=180)).isoformat()


def zakres_historii_klienta() -> tuple[str, str]:
    dzisiaj = date.today()
    return (dzisiaj - timedelta(days=365)).isoformat(), (dzisiaj + timedelta(days=180)).isoformat()


def salon_wstrzymany(salon: dict) -> bool:
    return salon.get("abonament_status") in {"pending_payment", "suspended"}


def abonament_po_terminie(salon: dict) -> bool:
    oplacone_do = salon.get("oplacone_do", "")
    return bool(oplacone_do and oplacone_do < date.today().isoformat())


def stripe_skonfigurowany() -> bool:
    return bool(STRIPE_SECRET_KEY)


def normalizuj_url_https(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    if 'src="' in url:
        dopasowanie = re.search(r'src="([^"]+)"', url)
        if dopasowanie:
            url = dopasowanie.group(1).strip()
    if url.startswith("//"):
        url = f"https:{url}"
    elif not re.match(r"^https?://", url, re.IGNORECASE):
        url = f"https://{url}"
    parsed = urlparse(url)
    return url if parsed.netloc else ""


def link_google_maps_salonu(salon: dict) -> str:
    """Link do map — z pola salonu lub wyszukiwanie po adresie tekstowym."""
    link = normalizuj_url_https(str(salon.get("link_google_maps", "")))
    if link:
        return link
    adres = (salon.get("adres_lokalizacji") or "").strip()
    if adres:
        return f"https://www.google.com/maps/search/?api=1&query={quote(adres)}"
    return ""


def embed_google_maps_salonu(salon: dict) -> str:
    """URL do osadzenia mapy w iframe (bez klucza API)."""
    adres = (salon.get("adres_lokalizacji") or "").strip()
    if adres:
        return f"https://www.google.com/maps?q={quote(adres)}&hl=pl&z=16&output=embed"
    link = link_google_maps_salonu(salon)
    if not link:
        return ""
    if "output=embed" in link:
        return link
    return f"{link}{'&' if '?' in link else '?'}output=embed"


def kontekst_lokalizacji_salonu(salon: dict) -> dict:
    adres = (salon.get("adres_lokalizacji") or "").strip()
    maps_url = link_google_maps_salonu(salon)
    maps_embed_url = embed_google_maps_salonu(salon)
    return {
        "maps_url": maps_url,
        "maps_embed_url": maps_embed_url,
        "ma_lokalizacje": bool(adres or maps_url),
    }


@app.template_global()
def maps_link_salonu(salon: dict) -> str:
    return link_google_maps_salonu(salon)


def legal_skonfigurowany() -> bool:
    """Czy dane operatora w ENV nie są placeholderami (do banera na stronach prawnych)."""
    return (
        legal_wartosc_uzupelniona(LEGAL_COMPANY_NAME)
        and legal_wartosc_uzupelniona(LEGAL_COMPANY_ADDRESS)
        and legal_wartosc_uzupelniona(LEGAL_COMPANY_EMAIL)
        and "@" in LEGAL_COMPANY_EMAIL
    )


def legal_wartosc_uzupelniona(wartosc: str) -> bool:
    return wartosc not in LEGAL_PLACEHOLDERS


def legal_dzialalnosc_nierejestrowana() -> bool:
    if LEGAL_UNREGISTERED_ACTIVITY in {"1", "true", "tak", "yes"}:
        return True
    if LEGAL_UNREGISTERED_ACTIVITY in {"0", "false", "nie", "no"}:
        return False
    return not legal_wartosc_uzupelniona(LEGAL_COMPANY_NIP)


def kwota_wizyty_zl(salon: dict) -> int:
    try:
        return max(int(salon.get("cena_wizyty", 0)), 0)
    except (TypeError, ValueError):
        return 0


_WYMIAT_CENA_ZERO_W_NAZWIE = re.compile(
    r"(?:\s*[-–—]\s*0\s*(?:zł|zl)\s*|\s*\|\s*0)\s*$",
    re.IGNORECASE,
)


def oczysc_nazwe_uslugi(nazwa: str) -> str:
    """Usuwa pozostałości po starym szablonie (np. „— 0 zł” lub „|0”)."""
    nazwa = (nazwa or "").strip()
    while True:
        oczyszczona = _WYMIAT_CENA_ZERO_W_NAZWIE.sub("", nazwa).strip()
        if oczyszczona == nazwa:
            return nazwa
        nazwa = oczyszczona


def parsuj_czas_uslugi_min(wartosc: str) -> int:
    tekst = (wartosc or "").strip().lower().replace(",", ".")
    if not tekst:
        return 0
    godziny = re.search(r"(\d+(?:\.\d+)?)\s*(?:h|godz)", tekst)
    if godziny:
        return min(max(int(float(godziny.group(1)) * 60), 0), 8 * 60)
    minuty = re.search(r"\d+", tekst)
    if not minuty:
        return 0
    return min(max(int(minuty.group(0)), 0), 8 * 60)


def normalizuj_wpis_uslugi(wpis: dict) -> dict | None:
    if not isinstance(wpis, dict):
        return None
    nazwa = oczysc_nazwe_uslugi(str(wpis.get("nazwa", "")))
    if not nazwa:
        return None
    try:
        cena = max(int(wpis.get("cena_zl", 0) or 0), 0)
    except (TypeError, ValueError):
        cena = 0
    try:
        czas_min = max(int(wpis.get("czas_min", 0) or 0), 0)
    except (TypeError, ValueError):
        czas_min = 0
    return {"nazwa": nazwa, "cena_zl": cena, "czas_min": czas_min}


def uslugi_salonu(salon: dict) -> list[dict]:
    wynik = []
    for wpis in salon.get("uslugi", []):
        znormalizowany = normalizuj_wpis_uslugi(wpis)
        if znormalizowany:
            wynik.append(znormalizowany)
    return wynik


def parsuj_uslugi(wartosc: str) -> list[dict]:
    uslugi = []
    for linia in wartosc.splitlines():
        linia = linia.strip()
        if not linia:
            continue
        if "|" in linia:
            czesci = linia.split("|")
            nazwa = czesci[0]
            cena_txt = czesci[1] if len(czesci) > 1 else "0"
            czas_txt = czesci[2] if len(czesci) > 2 else "0"
        else:
            nazwa, cena_txt = linia, "0"
            czas_txt = "0"
        nazwa = oczysc_nazwe_uslugi(nazwa.strip())
        if not nazwa:
            continue
        try:
            cena = max(int(cena_txt.strip() or "0"), 0)
        except ValueError:
            cena = 0
        czas_min = parsuj_czas_uslugi_min(czas_txt)
        uslugi.append({"nazwa": nazwa, "cena_zl": cena, "czas_min": czas_min})
    return uslugi[:30]


def oczysc_uslugi_w_salonie(salon: dict) -> bool:
    """Zapisuje oczyszczone nazwy usług w danych salonu (np. po migracji)."""
    stare = salon.get("uslugi") or []
    nowe = uslugi_salonu(salon)
    if nowe != stare:
        salon["uslugi"] = nowe
        return True
    return False


TRYBY_PLATNOSCI_WIZYTY = frozenset({"w_salonie", "przelew", "wylaczone"})


def tryb_platnosci_wizyty_salonu(salon: dict) -> str:
    tryb = (salon.get("tryb_platnosci_wizyty") or "w_salonie").strip()
    return tryb if tryb in TRYBY_PLATNOSCI_WIZYTY else "w_salonie"


def tytul_przelewu_rezerwacji(salon: dict, rezerwacja: dict) -> str:
    tytul = f"Rezerwacja {rezerwacja.get('data', '')} {rezerwacja.get('imie', '')}"
    return tytul.strip()[:140]


def rezerwacja_oplacona(rezerwacja: dict) -> bool:
    return bool(rezerwacja.get("oplacona_online") or rezerwacja.get("oplacona_recznie"))


def kontekst_platnosci_wizyty(salon: dict, rezerwacja: dict | None = None) -> dict:
    tryb = tryb_platnosci_wizyty_salonu(salon)
    kwota = kwota_rezerwacji_zl(salon, rezerwacja) if rezerwacja else 0
    konto = (salon.get("konto_bankowe") or "").strip()
    odbiorca = (salon.get("odbiorca_przelewu") or salon.get("nazwa_salonu") or "").strip()
    link_szybki = normalizuj_url_https(str(salon.get("link_szybkiej_platnosci", "")))
    stripe_wizyta = bool(
        rezerwacja
        and stripe_skonfigurowany()
        and salon.get("platnosc_online_wlaczona")
        and kwota > 0
    )
    return {
        "tryb_platnosci_wizyty": tryb,
        "platnosc_w_salonie": tryb == "w_salonie",
        "platnosc_przelewem": tryb == "przelew",
        "platnosc_info_wylaczona": tryb == "wylaczone",
        "konto_bankowe": konto,
        "odbiorca_przelewu": odbiorca,
        "link_szybkiej_platnosci": link_szybki,
        "ma_dane_przelewu": bool(konto),
        "kwota_rezerwacji_zl": kwota,
        "tytul_przelewu": tytul_przelewu_rezerwacji(salon, rezerwacja) if rezerwacja else "",
        "stripe_online_rezerwacje": stripe_wizyta,
        "rezerwacja_oplacona": rezerwacja_oplacona(rezerwacja) if rezerwacja else False,
    }


def kwota_rezerwacji_zl(salon: dict, rezerwacja: dict) -> int:
    try:
        cena_uslugi = max(int(rezerwacja.get("usluga_cena_zl", 0) or 0), 0)
    except (TypeError, ValueError):
        cena_uslugi = 0
    return cena_uslugi or kwota_wizyty_zl(salon)


def czas_trwania_rezerwacji_min(salon: dict, rezerwacja: dict | None = None, domyslnie: int | None = None) -> int:
    if rezerwacja:
        try:
            czas = int(rezerwacja.get("usluga_czas_min", 0) or rezerwacja.get("czas_trwania_min", 0) or 0)
        except (TypeError, ValueError):
            czas = 0
        if czas > 0:
            return min(czas, 8 * 60)
    return domyslnie or interwal_terminow_salonu(salon)


def przedluz_abonament(salon: dict, dni: int = 31) -> str:
    dzisiaj = date.today()
    oplacone_do = salon.get("oplacone_do", "")
    start = dzisiaj
    if oplacone_do and waliduj_date_iso(oplacone_do):
        obecny_koniec = datetime.strptime(oplacone_do, "%Y-%m-%d").date()
        if obecny_koniec > dzisiaj:
            start = obecny_koniec
    nowa_data = (start + timedelta(days=dni)).isoformat()
    salon["abonament_status"] = "active"
    salon["oplacone_do"] = nowa_data
    salon["notatka_rozliczeniowa"] = "Opłacone online przez Stripe"
    return nowa_data


def przekroczono_limit_rezerwacji(salon_slug: str) -> bool:
    identyfikator = f"{request.headers.get('X-Forwarded-For', request.remote_addr)}:{salon_slug}"
    teraz = time.time()
    okno_sekund = 10 * 60
    proby = [t for t in REZERWACJA_RATE_LIMIT.get(identyfikator, []) if teraz - t < okno_sekund]
    if len(proby) >= 6:
        REZERWACJA_RATE_LIMIT[identyfikator] = proby
        return True
    proby.append(teraz)
    REZERWACJA_RATE_LIMIT[identyfikator] = proby
    return False


def waliduj_godzine(wartosc: str) -> bool:
    try:
        datetime.strptime(wartosc, "%H:%M")
        return True
    except ValueError:
        return False


def normalizuj_godzine(wartosc: str) -> str:
    if not waliduj_godzine((wartosc or "").strip()):
        return ""
    godzina, minuta = wartosc.strip().split(":")
    return f"{int(godzina):02d}:{int(minuta):02d}"


def waliduj_date_iso(wartosc: str) -> bool:
    try:
        datetime.strptime(wartosc, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def czas_na_minuty(wartosc: str) -> int:
    godzina, minuta = wartosc.split(":")
    return int(godzina) * 60 + int(minuta)


def minuty_na_czas(minuty: int) -> str:
    return f"{minuty // 60:02d}:{minuty % 60:02d}"


def zakresy_nachodza(start_a: int, koniec_a: int, start_b: int, koniec_b: int) -> bool:
    return start_a < koniec_b and start_b < koniec_a


def daty_w_zakresie(start_iso: str, koniec_iso: str) -> list[str]:
    start = datetime.strptime(start_iso, "%Y-%m-%d").date()
    koniec = datetime.strptime(koniec_iso, "%Y-%m-%d").date()
    if koniec < start:
        start, koniec = koniec, start
    if (koniec - start).days > 62:
        koniec = start + timedelta(days=62)
    dni = []
    aktualna = start
    while aktualna <= koniec:
        dni.append(aktualna.isoformat())
        aktualna += timedelta(days=1)
    return dni


def klucz_dnia_tygodnia(data_iso: str) -> str:
    return [
        "poniedzialek",
        "wtorek",
        "sroda",
        "czwartek",
        "piatek",
        "sobota",
        "niedziela",
    ][datetime.strptime(data_iso, "%Y-%m-%d").weekday()]


def aktywni_pracownicy(salon: dict) -> list[str]:
    return [p.strip() for p in salon.get("pracownicy", []) if isinstance(p, str) and p.strip()]


def aktywne_rezerwacje_dnia(salon: dict, data_iso: str) -> list[dict]:
    return [
        r
        for r in salon.get("rezerwacje", [])
        if not rezerwacja_w_archiwum(r)
        and r.get("data") == data_iso
        and r.get("status", "potwierdzona") not in {"anulowana", "odrzucona"}
    ]


def aktywne_rezerwacje_slotu(salon: dict, data_iso: str, godzina: str) -> list[dict]:
    return [
        r
        for r in aktywne_rezerwacje_dnia(salon, data_iso)
        if r.get("godzina") == godzina
    ]


def rezerwacje_nachodzace_na_slot(salon: dict, data_iso: str, godzina: str, czas_min: int | None = None) -> list[dict]:
    start = czas_na_minuty(godzina)
    koniec = start + czas_trwania_rezerwacji_min(salon, domyslnie=czas_min)
    wynik = []
    for rezerwacja in aktywne_rezerwacje_dnia(salon, data_iso):
        godzina_rezerwacji = rezerwacja.get("godzina", "")
        if not waliduj_godzine(godzina_rezerwacji):
            continue
        r_start = czas_na_minuty(godzina_rezerwacji)
        r_koniec = r_start + czas_trwania_rezerwacji_min(salon, rezerwacja)
        if zakresy_nachodza(start, koniec, r_start, r_koniec):
            wynik.append(rezerwacja)
    return wynik


def pracownik_zajety(salon: dict, data_iso: str, godzina: str, pracownik: str, czas_min: int | None = None) -> bool:
    return any(
        not r.get("pracownik") or r.get("pracownik") == pracownik
        for r in rezerwacje_nachodzace_na_slot(salon, data_iso, godzina, czas_min)
    )


def slot_w_pelni_zajety(salon: dict, data_iso: str, godzina: str, czas_min: int | None = None) -> bool:
    pracownicy = aktywni_pracownicy(salon)
    aktywne = rezerwacje_nachodzace_na_slot(salon, data_iso, godzina, czas_min)
    if not pracownicy:
        return bool(aktywne)
    zajeci_pracownicy = {r.get("pracownik") for r in aktywne if r.get("pracownik")}
    bez_pracownika = any(not r.get("pracownik") for r in aktywne)
    return bez_pracownika or len(zajeci_pracownicy) >= len(pracownicy)


INTERWALY_TERMINOW = frozenset({15, 30, 45, 60})
HORYZONT_REZERWACJI_DNI = 60


def interwal_terminow_salonu(salon: dict) -> int:
    try:
        wartosc = int(salon.get("interwal_terminow", 30) or 30)
    except (TypeError, ValueError):
        wartosc = 30
    return wartosc if wartosc in INTERWALY_TERMINOW else 30


def automatyczne_terminy_wlaczone(salon: dict) -> bool:
    return salon.get("automatyczne_terminy", True) is not False


def harmonogram_dnia(salon: dict, data_iso: str) -> dict:
    return salon.get("godziny_pracy", {}).get(klucz_dnia_tygodnia(data_iso), {})


def sloty_z_harmonogramu(salon: dict, data_iso: str) -> list[str]:
    dzien = harmonogram_dnia(salon, data_iso)
    if dzien.get("zamkniety"):
        return []
    otwarcie = dzien.get("otwarcie", "09:00")
    zamkniecie = dzien.get("zamkniecie", "18:00")
    if not waliduj_godzine(otwarcie) or not waliduj_godzine(zamkniecie):
        return []
    start_min = czas_na_minuty(otwarcie)
    koniec_min = czas_na_minuty(zamkniecie)
    krok = interwal_terminow_salonu(salon)
    if start_min >= koniec_min:
        return []
    return [minuty_na_czas(minuta) for minuta in range(start_min, koniec_min, krok)]


def wszystkie_sloty_dnia(salon: dict, data_iso: str) -> list[str]:
    sloty = set(salon.get("wolne_terminy", {}).get(data_iso, []))
    if automatyczne_terminy_wlaczone(salon):
        sloty.update(sloty_z_harmonogramu(salon, data_iso))
    return sorted(sloty)


def slot_w_przeszlosci(data_iso: str, godzina: str) -> bool:
    dzisiaj = date.today().isoformat()
    if data_iso < dzisiaj:
        return True
    if data_iso > dzisiaj:
        return False
    teraz = datetime.now().hour * 60 + datetime.now().minute
    return czas_na_minuty(godzina) <= teraz


def zajete_godziny(salon: dict, data_iso: str) -> set[str]:
    return {
        godzina
        for godzina in salon.get("wolne_terminy", {}).get(data_iso, [])
        if slot_w_pelni_zajety(salon, data_iso, godzina)
    }


def blokady_dnia(salon: dict, data_iso: str) -> list[dict]:
    return [
        blokada
        for blokada in salon.get("blokady", [])
        if blokada.get("data_od", "") <= data_iso <= blokada.get("data_do", "")
    ]


def godzina_zablokowana(salon: dict, data_iso: str, godzina: str, czas_min: int | None = None) -> bool:
    minuta = czas_na_minuty(godzina)
    koniec_slotu = minuta + czas_trwania_rezerwacji_min(salon, domyslnie=czas_min)
    for blokada in blokady_dnia(salon, data_iso):
        caly_dzien = blokada.get("caly_dzien", False)
        if caly_dzien:
            return True
        start = blokada.get("od_godziny") or "00:00"
        koniec = blokada.get("do_godziny") or "23:59"
        if zakresy_nachodza(minuta, koniec_slotu, czas_na_minuty(start), czas_na_minuty(koniec)):
            return True
    return False


def normalizuj_wolne_terminy_dnia(salon: dict, data_iso: str) -> list[str]:
    terminy = salon.setdefault("wolne_terminy", {}).setdefault(data_iso, [])
    znormalizowane: list[str] = []
    widziane: set[str] = set()
    for godzina in terminy:
        slot = normalizuj_godzine(str(godzina))
        if slot and slot not in widziane:
            widziane.add(slot)
            znormalizowane.append(slot)
    znormalizowane.sort()
    if znormalizowane:
        salon["wolne_terminy"][data_iso] = znormalizowane
    else:
        salon["wolne_terminy"].pop(data_iso, None)
    return znormalizowane


def powod_odbioru_wolnego_terminu(salon: dict, data_iso: str, godzina: str) -> str | None:
    godzina = normalizuj_godzine(godzina)
    if not godzina:
        return "Podaj godzinę w formacie HH:MM (np. 10:30)."
    terminy = normalizuj_wolne_terminy_dnia(salon, data_iso)
    if godzina in terminy:
        return "Ten termin jest już na liście wolnych godzin."
    if godzina_zablokowana(salon, data_iso, godzina):
        return "Ten termin jest zablokowany — usuń lub zmień blokadę w tym widoku."
    if slot_w_pelni_zajety(salon, data_iso, godzina):
        return "Ten termin jest już zajęty przez wizytę w salonie — sprawdź terminarz."
    return None


def dostepne_terminy(salon: dict, data_iso: str) -> list[str]:
    """Tylko godziny dodane przez salon w „Wolne terminy” (nie z harmonogramu otwarcia)."""
    wolne = salon.get("wolne_terminy", {}).get(data_iso, [])
    return sorted(
        godzina
        for godzina in wolne
        if not slot_w_pelni_zajety(salon, data_iso, godzina)
        and not godzina_zablokowana(salon, data_iso, godzina)
        and not slot_w_przeszlosci(data_iso, godzina)
    )


def najblizsze_daty_z_terminami(
    salon: dict,
    limit: int = 8,
    max_dni: int = HORYZONT_REZERWACJI_DNI,
) -> list[dict]:
    """Najbliższe dni, w których salon ma wpisane wolne terminy z dostępnymi godzinami."""
    wynik = []
    dzisiaj = date.today().isoformat()
    kandydaci = sorted(
        data_iso
        for data_iso in salon.get("wolne_terminy", {})
        if data_iso >= dzisiaj
    )
    for data_iso in kandydaci:
        terminy = dostepne_terminy(salon, data_iso)
        if not terminy:
            continue
        wynik.append(
            {
                "data": data_iso,
                "dzien": dict(DNI_TYGODNIA)[klucz_dnia_tygodnia(data_iso)],
                "pierwszy": terminy[0],
                "liczba": len(terminy),
            }
        )
        if len(wynik) >= limit:
            break
    return wynik


def katalog_firm_z_terminami(dane: dict, branza: str | None = None) -> list[dict]:
    wybrana_branza = normalizuj_branze(branza) if branza else ""
    wynik = []
    for slug, salon in dane.get("salony", {}).items():
        if salon_wstrzymany(salon):
            continue
        branza_salonu = normalizuj_branze(salon.get("branza"))
        if wybrana_branza and branza_salonu != wybrana_branza:
            continue
        najblizsze = najblizsze_daty_z_terminami(salon, limit=3)
        wynik.append(
            {
                "slug": slug,
                "nazwa": salon.get("nazwa_salonu", slug),
                "opis": salon.get("opis", ""),
                "logo_url": salon.get("logo_url", ""),
                "adres": salon.get("adres_lokalizacji", ""),
                "branza": branza_salonu,
                "branza_nazwa": etykieta_branzy(branza_salonu),
                "najblizsze": najblizsze,
                "pierwszy_termin": najblizsze[0]["data"] if najblizsze else "9999-99-99",
            }
        )
    return sorted(wynik, key=lambda f: (f["pierwszy_termin"], f["nazwa"].lower()))


def katalog_firm_z_cache(dane: dict, branza: str | None = None) -> list[dict]:
    wybrana_branza = normalizuj_branze(branza) if branza else ""
    salony = dane.get("salony", {})
    fingerprint = "|".join(
        f"{slug}:{salon.get('abonament_status', '')}:{salon.get('branza', '')}:{len(salon.get('wolne_terminy', {}))}:{len(salon.get('rezerwacje', []))}"
        for slug, salon in sorted(salony.items())
    )
    klucz = (wybrana_branza, fingerprint)
    teraz = time.time()
    cached = _katalog_cache.get(klucz)
    if cached and teraz - cached[0] < KATALOG_CACHE_TTL_SEK:
        return copy.deepcopy(cached[1])

    wynik = katalog_firm_z_terminami(dane, wybrana_branza)
    _katalog_cache[klucz] = (teraz, copy.deepcopy(wynik))
    return wynik


def domyslna_data_rezerwacji(salon: dict, preferowana: str | None = None) -> str:
    dzisiaj = date.today().isoformat()
    if preferowana and waliduj_date_iso(preferowana) and preferowana >= dzisiaj:
        if dostepne_terminy(salon, preferowana):
            return preferowana
    najblizsze = najblizsze_daty_z_terminami(salon, limit=1)
    if najblizsze:
        return najblizsze[0]["data"]
    return dzisiaj


def poczatek_tygodnia_iso(data_iso: str) -> str:
    dzien = datetime.strptime(data_iso, "%Y-%m-%d").date()
    poniedzialek = dzien - timedelta(days=dzien.weekday())
    if poniedzialek < date.today():
        return date.today().isoformat()
    return poniedzialek.isoformat()


def dni_tygodnia_od(data_start: str, ile: int = 7) -> list[str]:
    start = datetime.strptime(data_start, "%Y-%m-%d").date()
    return [(start + timedelta(days=i)).isoformat() for i in range(ile)]


def rezerwacje_dnia(salon: dict, data_iso: str) -> list[dict]:
    return sorted(
        [
            r
            for r in salon.get("rezerwacje", [])
            if r.get("data") == data_iso
            and not rezerwacja_w_archiwum(r)
            and r.get("status", "potwierdzona") not in {"anulowana", "odrzucona"}
        ],
        key=lambda r: r.get("godzina", ""),
    )


def utworz_rezerwacje(
    salon: dict,
    *,
    data_iso: str,
    godzina: str,
    imie: str,
    telefon: str,
    email: str = "",
    uwagi: str = "",
    pracownik: str = "",
    usluga_nazwa: str = "",
    status: str = "oczekuje",
    zrodlo: str = "online",
    wywiad_odpowiedzi: dict[str, str] | None = None,
    salon_wymusza: bool = False,
) -> tuple[dict | None, str | None]:
    """Zwraca (rezerwacja, komunikat_bledu)."""
    if not waliduj_date_iso(data_iso):
        return None, "Nieprawidłowa data."
    if data_iso < date.today().isoformat():
        return None, "Nie można dodać wizyty w przeszłości."
    if not waliduj_godzine(godzina):
        return None, "Podaj godzinę w formacie HH:MM."
    uslugi = uslugi_salonu(salon)
    mapa_uslug = {u["nazwa"]: u for u in uslugi}
    if uslugi and usluga_nazwa and usluga_nazwa not in mapa_uslug:
        return None, "Wybierz usługę z listy."
    wybrana_usluga = mapa_uslug.get(usluga_nazwa, {})
    czas_uslugi = czas_trwania_rezerwacji_min(
        salon,
        {"usluga_czas_min": wybrana_usluga.get("czas_min", 0)},
    )
    if not salon_wymusza and godzina not in dostepne_terminy(salon, data_iso):
        return None, "Ten termin nie jest dostępny."
    if salon_wymusza:
        if slot_w_przeszlosci(data_iso, godzina):
            return None, "Nie można dodać wizyty w przeszłości."
        if godzina_zablokowana(salon, data_iso, godzina, czas_uslugi):
            return None, "Ten termin jest zablokowany."
        if (
            not pracownik
            and aktywni_pracownicy(salon)
            and rezerwacje_nachodzace_na_slot(salon, data_iso, godzina, czas_uslugi)
        ):
            return None, "Wybierz wolnego pracownika — ten slot koliduje z inną wizytą."
    if not imie.strip():
        return None, "Podaj imię i nazwisko."
    if not waliduj_telefon(telefon):
        return None, "Podaj poprawny numer telefonu."
    email = email.strip().lower()
    if email and not waliduj_email(email):
        return None, "Podaj poprawny adres e-mail."

    pracownicy = aktywni_pracownicy(salon)
    if pracownicy and pracownik and pracownik not in pracownicy:
        return None, "Nieprawidłowy pracownik."
    if pracownik and pracownik_zajety(salon, data_iso, godzina, pracownik, czas_uslugi):
        return None, "Ten pracownik jest już zajęty o tej godzinie."
    if not pracownik and slot_w_pelni_zajety(salon, data_iso, godzina, czas_uslugi):
        return None, "Ten termin koliduje z inną wizytą."

    klient = utworz_lub_aktualizuj_klienta(salon, imie, telefon, email, wywiad_odpowiedzi)
    rezerwacja_id = uuid.uuid4().hex[:12]
    rezerwacja = {
        "id": rezerwacja_id,
        "token_anulowania": uuid.uuid4().hex,
        "status": status,
        "token_opinii": uuid.uuid4().hex,
        "data": data_iso,
        "godzina": godzina,
        "imie": imie.strip(),
        "telefon": telefon.strip(),
        "email": email,
        "pracownik": pracownik,
        "usluga_nazwa": usluga_nazwa,
        "usluga_cena_zl": wybrana_usluga.get("cena_zl", 0),
        "usluga_czas_min": wybrana_usluga.get("czas_min", 0),
        "uwagi": uwagi.strip(),
        "klient_id": klient["id"],
        "wywiad_wizyty": wywiad_odpowiedzi or {},
        "utworzono": datetime.now().isoformat(timespec="minutes"),
        "zrodlo": zrodlo,
    }
    if status == "potwierdzona":
        rezerwacja["potwierdzono"] = rezerwacja["utworzono"]
    if zrodlo == "online" and salon.get("platnosc_online_wlaczona") and kwota_rezerwacji_zl(salon, rezerwacja) > 0:
        rezerwacja["wymaga_platnosci_online"] = True
        rezerwacja["platnosc_wygasa_at"] = (
            datetime.now() + timedelta(minutes=MINUTY_NA_OPLACENIE_REZERWACJI)
        ).isoformat(timespec="minutes")

    klient["ostatnia_wizyta"] = f"{data_iso} {godzina}"
    salon.setdefault("rezerwacje", []).append(rezerwacja)

    terminy = salon.setdefault("wolne_terminy", {}).setdefault(data_iso, [])
    if godzina in terminy and slot_w_pelni_zajety(salon, data_iso, godzina):
        terminy.remove(godzina)
    if not terminy:
        salon["wolne_terminy"].pop(data_iso, None)

    return rezerwacja, None


def normalizuj_telefon(telefon: str) -> str:
    return re.sub(r"\D", "", telefon)


def waliduj_telefon(telefon: str) -> bool:
    cyfry = normalizuj_telefon(telefon)
    return 9 <= len(cyfry) <= 15


def waliduj_email(email: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", (email or "").strip()))


def parsuj_linki_zdjec(wartosc: str) -> list[str]:
    linki = []
    for linia in wartosc.splitlines():
        link = linia.strip()
        if link.startswith(("http://", "https://")):
            linki.append(link)
    return linki[:12]


def parsuj_pracownikow(wartosc: str) -> list[str]:
    pracownicy = []
    for linia in wartosc.splitlines():
        imie = linia.strip()
        if imie and imie not in pracownicy:
            pracownicy.append(imie)
    return pracownicy[:20]


def parsuj_pytania_wywiadu(wartosc: str) -> list[dict]:
    pytania = []
    for indeks, linia in enumerate(wartosc.splitlines(), start=1):
        linia = linia.strip()
        if not linia:
            continue
        typ = "tekst"
        tresc = linia
        if linia.startswith("? "):
            typ = "tak_nie"
            tresc = linia[2:].strip()
        elif linia.upper().startswith("[T/N] "):
            typ = "tak_nie"
            tresc = linia[6:].strip()
        if not tresc:
            continue
        pytania.append({"id": f"q{indeks}", "tresc": tresc, "typ": typ})
    return pytania[:30]


def pytania_wywiadu_salonu(salon: dict) -> list[dict]:
    wynik = []
    for wpis in salon.get("pytania_wywiadu", []):
        if not isinstance(wpis, dict):
            continue
        tresc = str(wpis.get("tresc", "")).strip()
        if not tresc:
            continue
        typ = wpis.get("typ", "tekst")
        if typ not in {"tekst", "tak_nie"}:
            typ = "tekst"
        wynik.append(
            {
                "id": str(wpis.get("id", f"q{len(wynik) + 1}")),
                "tresc": tresc,
                "typ": typ,
            }
        )
    return wynik


def tresc_wywiadu_salonu(salon: dict) -> str:
    tresc = str(salon.get("tresc_wywiadu_zdrowotnego", "") or "").strip()
    if tresc:
        return tresc
    pytania = pytania_wywiadu_salonu(salon)
    if not pytania:
        return ""
    linie = [
        "Oświadczenie zdrowotne",
        "",
        "Przed wizytą prosimy o zapoznanie się z poniższymi informacjami:",
        "",
    ]
    for indeks, pytanie in enumerate(pytania, start=1):
        linie.append(f"{indeks}. {pytanie['tresc']}")
    linie.extend(
        [
            "",
            "Rezerwując wizytę, potwierdzam zapoznanie się z powyższą treścią.",
        ]
    )
    return "\n".join(linie)


def synchronizuj_tresc_wywiadu_z_pytan(salon: dict) -> bool:
    if str(salon.get("tresc_wywiadu_zdrowotnego", "") or "").strip():
        return False
    pytania = pytania_wywiadu_salonu(salon)
    if not pytania:
        return False
    salon["tresc_wywiadu_zdrowotnego"] = tresc_wywiadu_salonu(salon)
    return True


def wywiad_przy_rezerwacji_wlaczony(salon: dict) -> bool:
    return bool(
        salon.get("wywiad_wlaczony")
        and salon.get("wywiad_przy_rezerwacji")
        and tresc_wywiadu_salonu(salon)
    )


def wywiad_to_oswiadczenie(odpowiedzi: dict | None) -> bool:
    if not odpowiedzi:
        return False
    return odpowiedzi.get("_typ") == "oswiadczenie" or bool(odpowiedzi.get("zaakceptowano"))


def akceptacja_wywiadu_z_rezerwacji() -> tuple[dict[str, str] | None, list[str]]:
    bledy: list[str] = []
    if request.form.get("przeczytalem_wywiad") != "on":
        bledy.append("Potwierdź, że przeczytałeś/aś oświadczenie zdrowotne.")
    if request.form.get("zgoda_wywiad") != "on":
        bledy.append("Zaakceptuj przetwarzanie danych zdrowotnych na potrzeby tej wizyty.")
    if bledy:
        return None, bledy
    teraz = datetime.now().isoformat(timespec="minutes")
    return (
        {
            "_typ": "oswiadczenie",
            "zaakceptowano": teraz,
            "zgoda_rodo": "tak",
        },
        [],
    )


def znajdz_klienta(salon: dict, klient_id: str) -> dict | None:
    for klient in salon.get("klienci", []):
        if klient.get("id") == klient_id:
            return klient
    return None


def znajdz_klienta_po_telefonie(salon: dict, telefon: str) -> dict | None:
    cyfry = normalizuj_telefon(telefon)
    if not cyfry:
        return None
    for klient in salon.get("klienci", []):
        if klient.get("telefon") == cyfry:
            return klient
    return None


def synchronizuj_kartoteke_salonu(salon: dict) -> None:
    """Powiąż istniejące rezerwacje z kartoteką (telefon = klucz)."""
    salon.setdefault("klienci", [])
    mapa = {k.get("telefon"): k for k in salon["klienci"] if k.get("telefon")}
    for rezerwacja in salon.get("rezerwacje", []):
        cyfry = normalizuj_telefon(rezerwacja.get("telefon", ""))
        if not cyfry:
            continue
        klient = mapa.get(cyfry)
        if not klient:
            klient = {
                "id": uuid.uuid4().hex[:12],
                "imie": rezerwacja.get("imie", "").strip(),
                "telefon": cyfry,
                "telefon_wyswietl": rezerwacja.get("telefon", "").strip(),
                "email": "",
                "notatka_wewnetrzna": "",
                "wywiad_zdrowotny": dict(rezerwacja.get("wywiad_wizyty") or {}),
                "wywiad_aktualizacja": rezerwacja.get("utworzono", ""),
                "utworzono": rezerwacja.get("utworzono", datetime.now().isoformat(timespec="minutes")),
                "ostatnia_wizyta": "",
            }
            salon["klienci"].append(klient)
            mapa[cyfry] = klient
        if rezerwacja.get("imie"):
            klient["imie"] = rezerwacja["imie"].strip()
        if rezerwacja.get("telefon"):
            klient["telefon_wyswietl"] = rezerwacja["telefon"].strip()
        if rezerwacja.get("email") and not klient.get("email"):
            klient["email"] = rezerwacja["email"].strip().lower()
        if not rezerwacja.get("klient_id"):
            rezerwacja["klient_id"] = klient["id"]
        termin = f"{rezerwacja.get('data', '')} {rezerwacja.get('godzina', '')}".strip()
        if termin and termin > klient.get("ostatnia_wizyta", ""):
            klient["ostatnia_wizyta"] = termin
        if rezerwacja.get("wywiad_wizyty"):
            klient["wywiad_zdrowotny"] = dict(rezerwacja["wywiad_wizyty"])
            klient["wywiad_aktualizacja"] = rezerwacja.get("utworzono", klient.get("wywiad_aktualizacja", ""))


def utworz_lub_aktualizuj_klienta(
    salon: dict,
    imie: str,
    telefon: str,
    email: str = "",
    wywiad_odpowiedzi: dict | None = None,
) -> dict:
    synchronizuj_kartoteke_salonu(salon)
    cyfry = normalizuj_telefon(telefon)
    klient = znajdz_klienta_po_telefonie(salon, telefon)
    teraz = datetime.now().isoformat(timespec="minutes")
    if not klient:
        klient = {
            "id": uuid.uuid4().hex[:12],
            "imie": imie.strip(),
            "telefon": cyfry,
            "telefon_wyswietl": telefon.strip(),
            "email": "",
            "notatka_wewnetrzna": "",
            "wywiad_zdrowotny": {},
            "wywiad_aktualizacja": "",
            "utworzono": teraz,
            "ostatnia_wizyta": "",
        }
        salon.setdefault("klienci", []).append(klient)
    klient["imie"] = imie.strip() or klient.get("imie", "")
    klient["telefon_wyswietl"] = telefon.strip() or klient.get("telefon_wyswietl", "")
    if email:
        klient["email"] = email.strip().lower()
    if wywiad_odpowiedzi:
        klient["wywiad_zdrowotny"] = wywiad_odpowiedzi
        klient["wywiad_aktualizacja"] = teraz
    return klient


def historia_wizyt_klienta(salon: dict, klient_id: str) -> list[dict]:
    wizyty = [
        r
        for r in salon.get("rezerwacje", [])
        if r.get("klient_id") == klient_id
    ]
    return sorted(wizyty, key=lambda r: (r.get("data", ""), r.get("godzina", "")), reverse=True)


def email_klienta_rezerwacji(salon: dict, rezerwacja: dict) -> str:
    email = (rezerwacja.get("email") or "").strip().lower()
    if email:
        return email
    klient_id = rezerwacja.get("klient_id")
    if klient_id:
        klient = next((k for k in salon.get("klienci", []) if k.get("id") == klient_id), None)
        if klient:
            return (klient.get("email") or "").strip().lower()
    klient = znajdz_klienta_po_telefonie(salon, rezerwacja.get("telefon", ""))
    return (klient.get("email") or "").strip().lower() if klient else ""


def aktywne_zgloszenia_listy_rezerwowej(salon: dict) -> list[dict]:
    return sorted(
        [
            zgloszenie
            for zgloszenie in salon.get("lista_rezerwowa", [])
            if zgloszenie.get("status", "nowe") != "usuniete"
        ],
        key=lambda z: z.get("utworzono", ""),
        reverse=True,
    )


def parsuj_odpowiedzi_wywiadu_z_formularza(salon: dict) -> tuple[dict, list[str]]:
    pytania = pytania_wywiadu_salonu(salon)
    odpowiedzi: dict[str, str] = {}
    bledy: list[str] = []
    for pytanie in pytania:
        pid = pytanie["id"]
        if pytanie["typ"] == "tak_nie":
            wartosc = request.form.get(f"wywiad_{pid}", "").strip().lower()
            if wartosc not in {"tak", "nie"}:
                bledy.append(f"Odpowiedz tak/nie: {pytanie['tresc']}")
            else:
                odpowiedzi[pid] = wartosc
        else:
            wartosc = request.form.get(f"wywiad_{pid}", "").strip()
            if not wartosc:
                bledy.append(f"Uzupełnij pole: {pytanie['tresc']}")
            else:
                odpowiedzi[pid] = wartosc[:500]
    return odpowiedzi, bledy


def etykieta_odpowiedzi_wywiadu(pytania: list[dict], odpowiedzi: dict) -> list[dict]:
    if wywiad_to_oswiadczenie(odpowiedzi):
        return [
            {
                "pytanie": "Oświadczenie zdrowotne",
                "typ": "oswiadczenie",
                "odpowiedz": f"Zaakceptowano: {odpowiedzi.get('zaakceptowano', '—')}",
            }
        ]
    mapa = {p["id"]: p for p in pytania}
    wynik = []
    for pid, odp in odpowiedzi.items():
        if pid.startswith("_"):
            continue
        pytanie = mapa.get(pid)
        if not pytanie:
            continue
        wynik.append(
            {
                "pytanie": pytanie["tresc"],
                "typ": pytanie["typ"],
                "odpowiedz": odp,
            }
        )
    return wynik


def wyszukaj_klientow(salon: dict, fraza: str = "") -> list[dict]:
    synchronizuj_kartoteke_salonu(salon)
    klienci = list(salon.get("klienci", []))
    fraza = fraza.strip().lower()
    if fraza:
        filtrowani = []
        for klient in klienci:
            imie = (klient.get("imie") or "").lower()
            tel = klient.get("telefon_wyswietl") or klient.get("telefon") or ""
            if fraza in imie or fraza in tel.replace(" ", ""):
                filtrowani.append(klient)
        klienci = filtrowani
    return sorted(klienci, key=lambda k: k.get("ostatnia_wizyta", ""), reverse=True)


def zoptymalizuj_upload_zdjecia(dane: bytes, mimetype: str) -> tuple[bytes, str]:
    if Image is None or ImageOps is None:
        return dane, mimetype
    try:
        with Image.open(io.BytesIO(dane)) as obraz:
            if mimetype == "image/gif":
                obraz.verify()
                return dane, mimetype
            obraz = ImageOps.exif_transpose(obraz)
            obraz.thumbnail((MAKS_WYMIAR_ZDJECIA, MAKS_WYMIAR_ZDJECIA))

            if obraz.mode in {"RGBA", "LA", "P"}:
                tlo = Image.new("RGB", obraz.size, (255, 255, 255))
                if obraz.mode == "P":
                    obraz = obraz.convert("RGBA")
                tlo.paste(obraz, mask=obraz.getchannel("A") if "A" in obraz.getbands() else None)
                obraz = tlo
            elif obraz.mode != "RGB":
                obraz = obraz.convert("RGB")

            bufor = io.BytesIO()
            obraz.save(bufor, format="JPEG", quality=82, optimize=True, progressive=True)
            zoptymalizowane = bufor.getvalue()
            if zoptymalizowane and len(zoptymalizowane) < len(dane):
                return zoptymalizowane, "image/jpeg"
    except Exception as exc:
        app.logger.warning("Nie udało się zoptymalizować zdjęcia: %s", exc)
    return b"", mimetype


def upload_ma_poprawna_sygnature(dane: bytes, mimetype: str) -> bool:
    if mimetype == "image/jpeg":
        return dane.startswith(b"\xff\xd8\xff")
    if mimetype == "image/png":
        return dane.startswith(b"\x89PNG\r\n\x1a\n")
    if mimetype == "image/gif":
        return dane.startswith((b"GIF87a", b"GIF89a"))
    if mimetype == "image/webp":
        return dane.startswith(b"RIFF") and dane[8:12] == b"WEBP"
    return False


def parsuj_upload_zdjec(pliki) -> list[str]:
    """Zapisuje małe zdjęcia jako data URL w JSON, bez osobnego hostingu plików."""
    zdjecia = []
    dozwolone_typy = {"image/jpeg", "image/png", "image/webp", "image/gif"}

    for plik in pliki:
        if not plik or not plik.filename:
            continue
        if plik.mimetype not in dozwolone_typy:
            continue
        dane = plik.read()
        if not dane or len(dane) > MAKS_UPLOAD_ZDJECIA_BAJTOW:
            continue
        if not upload_ma_poprawna_sygnature(dane, plik.mimetype):
            continue
        dane, mimetype = zoptymalizuj_upload_zdjecia(dane, plik.mimetype)
        if not dane or len(dane) > MAKS_ZDJECIE_PO_KOMPRESJI_BAJTOW:
            continue
        zakodowane = base64.b64encode(dane).decode("ascii")
        zdjecia.append(f"data:{mimetype};base64,{zakodowane}")
    return zdjecia


def email_skonfigurowany() -> bool:
    return bool(RESEND_API_KEY and RESEND_FROM) or bool(
        SMTP_HOST and SMTP_USERNAME and SMTP_PASSWORD and SMTP_FROM
    )


def utworz_sesje_stripe(salon: dict, salon_slug: str) -> str | None:
    if not stripe_skonfigurowany():
        return None

    kwota_grosze = max(int(salon.get("oplata_miesieczna", 100)), 1) * 100
    payload = urlencode(
        {
            "mode": "subscription",
            "success_url": url_for("stripe_sukces", salon_slug=salon_slug, _external=True),
            "cancel_url": url_for("panel", salon_slug=salon_slug, _external=True),
            "client_reference_id": salon_slug,
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": "pln",
            "line_items[0][price_data][unit_amount]": str(kwota_grosze),
            "line_items[0][price_data][recurring][interval]": "month",
            "line_items[0][price_data][product_data][name]": f"Abonament Glovaro - {salon.get('nazwa_salonu', salon_slug)}",
            "metadata[salon_slug]": salon_slug,
            "subscription_data[metadata][salon_slug]": salon_slug,
        }
    ).encode("utf-8")
    req = urlrequest.Request(
        "https://api.stripe.com/v1/checkout/sessions",
        data=payload,
        headers={
            "Authorization": f"Bearer {STRIPE_SECRET_KEY}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Glovaro/1.0",
        },
        method="POST",
    )

    try:
        with urlrequest.urlopen(req, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
            return body.get("url")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        app.logger.warning("Stripe odrzucił sesję płatności: HTTP %s %s", exc.code, body)
        return None
    except (URLError, json.JSONDecodeError) as exc:
        app.logger.warning("Nie udało się utworzyć sesji Stripe: %s", exc)
        return None


def utworz_sesje_stripe_wizyta(salon: dict, salon_slug: str, rezerwacja: dict) -> str | None:
    if not stripe_skonfigurowany():
        return None
    if not salon.get("platnosc_online_wlaczona"):
        return None

    cena_zl = kwota_rezerwacji_zl(salon, rezerwacja)
    if cena_zl <= 0:
        return None

    kwota_grosze = cena_zl * 100
    rezerwacja_id = rezerwacja.get("id", "")
    payload = urlencode(
        {
            "mode": "payment",
            "success_url": url_for(
                "platnosc_rezerwacji_sukces",
                salon_slug=salon_slug,
                id=rezerwacja_id,
                _external=True,
            ),
            "cancel_url": url_for(
                "rezerwacja_potwierdzenie",
                salon_slug=salon_slug,
                id=rezerwacja_id,
                _external=True,
            ),
            "client_reference_id": f"{salon_slug}:{rezerwacja_id}",
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": "pln",
            "line_items[0][price_data][unit_amount]": str(kwota_grosze),
            "line_items[0][price_data][product_data][name]": f"Wizyta - {salon.get('nazwa_salonu', salon_slug)}",
            "line_items[0][price_data][product_data][description]": (
                f"{rezerwacja.get('data', '')} o {rezerwacja.get('godzina', '')} - {rezerwacja.get('usluga_nazwa', 'Wizyta')}"
            ),
            "metadata[typ_platnosci]": "wizyta",
            "metadata[salon_slug]": salon_slug,
            "metadata[rezerwacja_id]": rezerwacja_id,
            "metadata[kwota_zl]": str(cena_zl),
        }
    ).encode("utf-8")
    req = urlrequest.Request(
        "https://api.stripe.com/v1/checkout/sessions",
        data=payload,
        headers={
            "Authorization": f"Bearer {STRIPE_SECRET_KEY}",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Glovaro/1.0",
        },
        method="POST",
    )

    try:
        with urlrequest.urlopen(req, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
            return body.get("url")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        app.logger.warning("Stripe odrzucił sesję płatności wizyty: HTTP %s %s", exc.code, body)
        return None
    except (URLError, json.JSONDecodeError) as exc:
        app.logger.warning("Nie udało się utworzyć sesji Stripe dla wizyty: %s", exc)
        return None


def podpis_stripe_poprawny(payload: bytes, header: str) -> bool:
    if not STRIPE_WEBHOOK_SECRET:
        return True
    elementy = {}
    for czesc in header.split(","):
        if "=" in czesc:
            klucz, wartosc = czesc.split("=", 1)
            elementy.setdefault(klucz, []).append(wartosc)
    timestamp = elementy.get("t", [""])[0]
    podpisy = elementy.get("v1", [])
    if not timestamp or not podpisy:
        return False
    signed_payload = timestamp.encode("utf-8") + b"." + payload
    oczekiwany = hmac.new(
        STRIPE_WEBHOOK_SECRET.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    return any(hmac.compare_digest(oczekiwany, podpis) for podpis in podpisy)


def wyslij_email_przez_resend(odbiorca: str, temat: str, tresc: str) -> bool:
    if not (RESEND_API_KEY and RESEND_FROM):
        return False

    payload = json.dumps(
        {
            "from": RESEND_FROM,
            "to": [odbiorca],
            "subject": temat,
            "text": tresc,
        }
    ).encode("utf-8")
    req = urlrequest.Request(
        "https://api.resend.com/emails",
        data=payload,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": "Glovaro/1.0",
        },
        method="POST",
    )

    try:
        with urlrequest.urlopen(req, timeout=10) as response:
            if 200 <= response.status < 300:
                app.logger.info("Wysłano e-mail przez Resend do %s", odbiorca)
                return True
            app.logger.warning("Resend zwrócił status HTTP %s", response.status)
            return False
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        app.logger.warning("Resend odrzucił e-mail: HTTP %s %s", exc.code, body)
        if exc.code == 403 and "verify a domain" in body.lower():
            app.logger.warning(
                "Resend (tryb testowy): e-mail może trafić tylko na adres właściciela konta Resend. "
                "Zweryfikuj domenę na resend.com/domains i ustaw RESEND_FROM na adres @twoja-domena.pl"
            )
        return False
    except URLError as exc:
        app.logger.warning("Nie udało się wysłać e-maila przez Resend: %s", exc)
        return False


def wyslij_email_powiadomienie(salon: dict, rezerwacja: dict, salon_slug: str) -> bool:
    odbiorca = salon.get("email_powiadomien", "").strip()
    if not odbiorca:
        app.logger.warning("Nie wysłano e-maila: salon %s nie ma adresu powiadomień.", salon_slug)
        return False
    if not email_skonfigurowany():
        app.logger.warning("Nie wysłano e-maila: brak konfiguracji RESEND_API_KEY/SMTP.")
        return False

    link_panelu = url_for("panel_rezerwacje", salon_slug=salon_slug, _external=True)
    temat = f"Nowa rezerwacja: {rezerwacja['data']} o {rezerwacja['godzina']}"
    czas_trwania = f"{rezerwacja.get('usluga_czas_min')} min" if rezerwacja.get("usluga_czas_min") else "-"
    tresc = f"""Nowa rezerwacja w Glovaro

Salon: {salon['nazwa_salonu']}
Termin: {rezerwacja['data']} o {rezerwacja['godzina']}
Klient: {rezerwacja['imie']}
Telefon: {rezerwacja['telefon']}
E-mail klienta: {rezerwacja.get('email') or '-'}
Usługa: {rezerwacja.get('usluga_nazwa') or '-'}
Czas trwania: {czas_trwania}
Uwagi: {rezerwacja.get('uwagi') or '-'}
Pracownik: {rezerwacja.get('pracownik') or 'Dowolny / nie wybrano'}

Panel rezerwacji:
{link_panelu}
"""

    if wyslij_email_przez_resend(odbiorca, temat, tresc):
        return True

    msg = EmailMessage()
    msg["Subject"] = temat
    msg["From"] = SMTP_FROM
    msg["To"] = odbiorca
    msg.set_content(tresc)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception as exc:
        app.logger.warning("Nie udało się wysłać e-maila z rezerwacją (SMTP): %s", exc)
        if os.environ.get("RENDER"):
            app.logger.warning(
                "Na Renderze SMTP (port 587) często jest zablokowane — użyj Resend z zweryfikowaną domeną zamiast SMTP."
            )
        return False


def wyslij_email_lista_rezerwowa(salon: dict, zgloszenie: dict, salon_slug: str) -> bool:
    odbiorca = salon.get("email_powiadomien", "").strip()
    if not odbiorca or not email_skonfigurowany():
        return False

    link_panelu = url_for("panel_lista_rezerwowa", salon_slug=salon_slug, _external=True)
    temat = f"Lista rezerwowa: {zgloszenie['imie']}"
    tresc = f"""Nowe zgłoszenie na listę rezerwową

Firma: {salon['nazwa_salonu']}
Preferowany dzień: {zgloszenie.get('data_preferowana') or '-'}
Klient: {zgloszenie['imie']}
Telefon: {zgloszenie['telefon']}
E-mail: {zgloszenie.get('email') or '-'}
Usługa: {zgloszenie.get('usluga_nazwa') or '-'}
Uwagi: {zgloszenie.get('uwagi') or '-'}

Lista rezerwowa w panelu:
{link_panelu}
"""

    if wyslij_email_przez_resend(odbiorca, temat, tresc):
        return True

    msg = EmailMessage()
    msg["Subject"] = temat
    msg["From"] = SMTP_FROM
    msg["To"] = odbiorca
    msg.set_content(tresc)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception as exc:
        app.logger.warning("Nie udało się wysłać e-maila z listy rezerwowej: %s", exc)
        return False


def wyslij_email_przypomnienie(salon: dict, rezerwacja: dict, salon_slug: str) -> bool:
    odbiorca = salon.get("email_powiadomien", "").strip()
    if not odbiorca or not email_skonfigurowany():
        return False

    link_panelu = url_for("panel_rezerwacje", salon_slug=salon_slug, _external=True)
    temat = f"Przypomnienie: wizyta {rezerwacja['data']} o {rezerwacja['godzina']}"
    czas_trwania = f"{rezerwacja.get('usluga_czas_min')} min" if rezerwacja.get("usluga_czas_min") else "-"
    tresc = f"""Przypomnienie o nadchodzącej wizycie

Salon: {salon['nazwa_salonu']}
Termin: {rezerwacja['data']} o {rezerwacja['godzina']}
Klient: {rezerwacja['imie']}
Telefon: {rezerwacja['telefon']}
E-mail klienta: {rezerwacja.get('email') or '-'}
Usługa: {rezerwacja.get('usluga_nazwa') or '-'}
Czas trwania: {czas_trwania}
Uwagi: {rezerwacja.get('uwagi') or '-'}
Pracownik: {rezerwacja.get('pracownik') or 'Dowolny / nie wybrano'}

Panel rezerwacji:
{link_panelu}
"""

    if wyslij_email_przez_resend(odbiorca, temat, tresc):
        return True

    msg = EmailMessage()
    msg["Subject"] = temat
    msg["From"] = SMTP_FROM
    msg["To"] = odbiorca
    msg.set_content(tresc)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception as exc:
        app.logger.warning("Nie udało się wysłać przypomnienia e-mail: %s", exc)
        return False


def wyslij_email_przypomnienie_klienta(salon: dict, rezerwacja: dict) -> bool:
    odbiorca = email_klienta_rezerwacji(salon, rezerwacja)
    if not odbiorca or not waliduj_email(odbiorca) or not email_skonfigurowany():
        return False

    czas_trwania = f"{rezerwacja.get('usluga_czas_min')} min" if rezerwacja.get("usluga_czas_min") else "-"
    temat = f"Przypomnienie o wizycie: {rezerwacja['data']} o {rezerwacja['godzina']}"
    tresc = f"""Przypomnienie o nadchodzącej wizycie

Salon: {salon['nazwa_salonu']}
Termin: {rezerwacja['data']} o {rezerwacja['godzina']}
Usługa: {rezerwacja.get('usluga_nazwa') or '-'}
Czas trwania: {czas_trwania}
Pracownik: {rezerwacja.get('pracownik') or 'Dowolny / nie wybrano'}

Jeśli nie możesz przyjść, skontaktuj się z salonem.
Telefon salonu: {salon.get('telefon_kontaktowy') or '-'}
"""

    if wyslij_email_przez_resend(odbiorca, temat, tresc):
        return True

    msg = EmailMessage()
    msg["Subject"] = temat
    msg["From"] = SMTP_FROM
    msg["To"] = odbiorca
    msg.set_content(tresc)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(SMTP_USERNAME, SMTP_PASSWORD)
            smtp.send_message(msg)
        return True
    except Exception as exc:
        app.logger.warning("Nie udało się wysłać przypomnienia do klienta: %s", exc)
        return False


def znajdz_rezerwacje(salon: dict, rezerwacja_id: str) -> dict | None:
    for rezerwacja in salon.get("rezerwacje", []):
        if rezerwacja.get("id") == rezerwacja_id:
            return rezerwacja
    return None


def znajdz_rezerwacje_po_tokenie(salon: dict, token: str) -> dict | None:
    for rezerwacja in salon.get("rezerwacje", []):
        if token and rezerwacja.get("token_anulowania") == token:
            return rezerwacja
    return None


def znajdz_rezerwacje_po_tokenie_opinii(salon: dict, token: str) -> dict | None:
    for rezerwacja in salon.get("rezerwacje", []):
        if token and rezerwacja.get("token_opinii") == token:
            return rezerwacja
    return None


def widoczne_opinie(salon: dict) -> list[dict]:
    return [
        opinia
        for opinia in salon.get("opinie", [])
        if opinia.get("widoczna", True)
    ]


def srednia_ocen(opinie: list[dict]) -> float:
    oceny = [int(o.get("ocena", 0)) for o in opinie if str(o.get("ocena", "")).isdigit()]
    if not oceny:
        return 0
    return round(sum(oceny) / len(oceny), 1)


def przywroc_wolny_termin(salon: dict, rezerwacja: dict) -> None:
    data_iso = rezerwacja["data"]
    godzina = rezerwacja["godzina"]
    salon.setdefault("wolne_terminy", {}).setdefault(data_iso, [])
    if godzina not in salon["wolne_terminy"][data_iso]:
        salon["wolne_terminy"][data_iso].append(godzina)
        salon["wolne_terminy"][data_iso].sort()


def panel_auth_key(salon_slug: str) -> str:
    return f"panel_auth_{salon_slug}"


def pierwszy_zalogowany_salon_slug() -> str | None:
    for key, value in session.items():
        if key.startswith("panel_auth_") and value:
            return key.removeprefix("panel_auth_")
    return None


def ustaw_sesje_salonu(salon_slug: str) -> None:
    for key in list(session.keys()):
        if key.startswith("panel_auth_"):
            session.pop(key, None)
    session[panel_auth_key(salon_slug)] = True


def parsuj_identyfikator_salonu(tekst: str) -> str:
    wartosc = (tekst or "").strip()
    if not wartosc:
        return ""
    lower = wartosc.lower()
    for marker in ("/rezerwacja/", "/panel/"):
        if marker in lower:
            idx = lower.index(marker)
            wartosc = wartosc[idx + len(marker) :]
    wartosc = wartosc.split("?")[0].split("#")[0].strip().strip("/")
    if "@" in wartosc:
        return wartosc.lower()
    return slugify(wartosc)


def salon_slug_z_sciezki(path: str | None) -> str | None:
    if not path:
        return None
    match = re.match(r"^/panel/([^/]+)", path)
    if not match:
        return None
    slug = match.group(1)
    if slug in {"login", "wyloguj", "nowy"}:
        return None
    return slug


def znajdz_salon_do_logowania(identyfikator: str) -> tuple[str, dict] | None | str:
    ident = parsuj_identyfikator_salonu(identyfikator)
    if not ident:
        return None
    dane = wczytaj_salony_raw(
        include_clients=False,
        include_reservations=False,
        include_free_slots=False,
        include_waitlist=False,
    )
    if not dane:
        return None
    salony = dane.get("salony", {})
    if "@" in ident:
        dopasowania = [
            (slug, salon)
            for slug, salon in salony.items()
            if (salon.get("email_powiadomien") or "").strip().lower() == ident
        ]
        if len(dopasowania) == 1:
            slug, salon = dopasowania[0]
            salon.setdefault("slug", slug)
            return slug, salon
        if len(dopasowania) > 1:
            return "wiele_firm"
        return None
    if ident in salony:
        salon = salony[ident]
        salon.setdefault("slug", ident)
        return ident, salon
    return None


def admin_auth_key() -> str:
    return "admin_auth"


def bezpieczny_next_url(url: str | None, *, salon_slug: str | None = None) -> str:
    if salon_slug:
        domyslny = url_for("panel", salon_slug=salon_slug)
    elif session.get(admin_auth_key()):
        domyslny = url_for("panel_lista")
    else:
        zalogowany = pierwszy_zalogowany_salon_slug()
        domyslny = url_for("panel", salon_slug=zalogowany) if zalogowany else url_for("panel_login")
    if not url:
        return domyslny
    parsed = urlparse(url)
    if parsed.netloc or not url.startswith("/") or url.startswith("//"):
        return domyslny
    return url


def haslo_panelu(salon: dict) -> str:
    return (salon.get("haslo_panelu") or PANEL_PASSWORD or "").strip()


def motyw_strony_salonu(salon: dict | None) -> str:
    motyw = str((salon or {}).get("motyw_strony", "rozowy")).strip()
    return motyw if motyw in MOTYWY_STRONY else "rozowy"


def zalogowany_do_salonu(salon_slug: str) -> bool:
    return bool(session.get(panel_auth_key(salon_slug)) or session.get(admin_auth_key()))


@app.before_request
def wymagaj_hasla_panelu():
    request._glovaro_start = time.perf_counter()
    if request.endpoint in PUBLIC_ENDPOINTS:
        return
    if not (request.endpoint and request.endpoint.startswith("panel")):
        return

    if request.endpoint in {"panel_lista", "panel_nowy_salon"}:
        if PANEL_PASSWORD and not session.get(admin_auth_key()):
            return redirect(url_for("panel_login", next=request.path))
        if not PANEL_PASSWORD and os.environ.get("RENDER"):
            flash("Ustaw PANEL_PASSWORD w Render, aby zarządzać salonami.", "error")
            return redirect(url_for("strona_glowna"))
        return

    salon_slug = request.view_args.get("salon_slug") if request.view_args else None
    if not salon_slug:
        return

    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        include_clients=False,
        include_reservations=False,
        include_free_slots=False,
        include_waitlist=False,
    )
    if not salon:
        dane = wczytaj_dane()
        salon = pobierz_salon(dane, salon_slug)
    if not salon:
        return

    if not haslo_panelu(salon):
        if os.environ.get("RENDER"):
            flash("Ten panel nie ma ustawionego hasła.", "error")
            return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug))
        return

    if not zalogowany_do_salonu(salon_slug):
        return redirect(url_for("panel_login", next=request.path))


@app.after_request
def dodaj_naglowki_bezpieczenstwa(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    start = getattr(request, "_glovaro_start", None)
    if start is not None:
        elapsed = time.perf_counter() - start
        if elapsed >= WOLNY_REQUEST_LOG_SEK:
            app.logger.info(
                "Wolny request: %.3fs %s %s -> %s",
                elapsed,
                request.method,
                request.path,
                response.status_code,
            )
    return response


@app.context_processor
def inject_globals():
    salon_slug = request.view_args.get("salon_slug") if request.view_args else None
    widok_klienta = request.endpoint in WIDOK_KLIENTA_ENDPOINTS
    motyw_klienta = endpoint_ma_motyw_glovaro(request.endpoint)
    slug_motywu = slug_salonu_dla_motywu(salon_slug, request.endpoint)
    motyw_strony = "rozowy"
    if slug_motywu:
        data_od, data_do = (
            zakres_publicznej_rezerwacji(request.values.get("data")) if widok_klienta else (None, None)
        )
        salon_motywu = wczytaj_salon_bezposrednio(
            slug_motywu,
            data_od=data_od,
            data_do=data_do,
            include_clients=False,
            include_reservations=False,
            include_free_slots=False,
            include_waitlist=False,
        )
        motyw_strony = motyw_strony_salonu(salon_motywu)
    return {
        "dni_tygodnia": DNI_TYGODNIA,
        "branze_dzialalnosci": BRANZE_DZIALALNOSCI,
        "branze_map": BRANZE_MAP,
        "etykieta_branzy": etykieta_branzy,
        "panel_chroniony_haslem": bool(PANEL_PASSWORD),
        "admin_zalogowany": bool(session.get(admin_auth_key())),
        "zalogowany_salon_slug": pierwszy_zalogowany_salon_slug(),
        "zalogowany_do_panelu": bool(
            session.get(admin_auth_key())
            or (salon_slug and zalogowany_do_salonu(salon_slug))
            or pierwszy_zalogowany_salon_slug()
        ),
        "widok_klienta": widok_klienta,
        "motyw_klienta": motyw_klienta,
        "motyw_strony": motyw_strony,
        "motyw_rozowy": motyw_klienta and motyw_strony == "rozowy",
        "motyw_neutralny": motyw_klienta and motyw_strony == "neutralny",
        "aktywny_salon_slug": salon_slug,
        "stripe_skonfigurowany": stripe_skonfigurowany(),
        "legal": {
            "company_name": LEGAL_COMPANY_NAME,
            "company_address": LEGAL_COMPANY_ADDRESS,
            "company_email": LEGAL_COMPANY_EMAIL,
            "company_nip": LEGAL_COMPANY_NIP,
            "has_name": legal_wartosc_uzupelniona(LEGAL_COMPANY_NAME),
            "has_address": legal_wartosc_uzupelniona(LEGAL_COMPANY_ADDRESS),
            "has_email": legal_wartosc_uzupelniona(LEGAL_COMPANY_EMAIL) and "@" in LEGAL_COMPANY_EMAIL,
            "has_nip": legal_wartosc_uzupelniona(LEGAL_COMPANY_NIP),
            "unregistered_activity": legal_dzialalnosc_nierejestrowana(),
        },
        "legal_skonfigurowany": legal_skonfigurowany(),
        "dni_archiwum_rezerwacji": DNI_W_ARCHIWUM_PRZED_USUNIECIEM,
    }


@app.route("/panel/login", methods=["GET", "POST"])
def panel_login():
    next_url = request.form.get("next") or request.args.get("next") or ""
    identyfikator_prefill = (request.form.get("identyfikator") or request.args.get("identyfikator") or "").strip()
    if not identyfikator_prefill:
        identyfikator_prefill = request.args.get("salon") or ""
    if not identyfikator_prefill:
        slug_z_next = salon_slug_z_sciezki(next_url)
        if slug_z_next:
            identyfikator_prefill = slug_z_next

    if request.method == "POST":
        haslo = request.form.get("haslo", "")
        identyfikator = request.form.get("identyfikator", "").strip()
        if identyfikator:
            wynik = znajdz_salon_do_logowania(identyfikator)
            if wynik == "wiele_firm":
                flash(
                    "Ten e-mail jest przypisany do kilku firm. Podaj link firmy (np. nazwa-firmy) zamiast e-maila.",
                    "error",
                )
            elif not wynik:
                flash("Nie znaleziono firmy. Sprawdź e-mail lub link w panelu.", "error")
            else:
                salon_slug, salon = wynik
                if haslo == haslo_panelu(salon):
                    ustaw_sesje_salonu(salon_slug)
                    flash("Zalogowano do panelu.", "success")
                    return redirect(bezpieczny_next_url(next_url, salon_slug=salon_slug))
                flash("Nieprawidłowe hasło.", "error")
        elif PANEL_PASSWORD and haslo == PANEL_PASSWORD:
            session[admin_auth_key()] = True
            flash("Zalogowano jako administrator.", "success")
            return redirect(bezpieczny_next_url(next_url))
        elif PANEL_PASSWORD:
            flash("Podaj e-mail firmy lub link do panelu.", "error")
        else:
            flash("Podaj dane logowania firmy.", "error")
    else:
        zalogowany_slug = pierwszy_zalogowany_salon_slug()
        if zalogowany_slug and not session.get(admin_auth_key()):
            return redirect(bezpieczny_next_url(next_url, salon_slug=zalogowany_slug))
        if session.get(admin_auth_key()):
            return redirect(bezpieczny_next_url(next_url))

    pokaz_logowanie_admina = bool(PANEL_PASSWORD)
    return render_template(
        "login.html",
        identyfikator_prefill=identyfikator_prefill,
        next_url=next_url,
        pokaz_logowanie_admina=pokaz_logowanie_admina,
    )


@app.route("/panel/wyloguj")
@app.route("/panel/<salon_slug>/wyloguj")
def panel_wyloguj(salon_slug: str | None = None):
    if salon_slug:
        session.pop(panel_auth_key(salon_slug), None)
    else:
        session.clear()
    flash("Wylogowano z panelu.", "success")
    return redirect(url_for("strona_glowna"))


BUILD_ID = "2026-05-27-favicon-transparent"


@app.context_processor
def inject_seo():
    path = request.path or "/"
    canonical = f"{PUBLIC_BASE_URL}{path}" if path != "/" else f"{PUBLIC_BASE_URL}/"
    logo = f"{PUBLIC_BASE_URL}{url_for('static', filename='img/glovaro-icon-192.png')}"
    return {
        "public_base_url": PUBLIC_BASE_URL,
        "seo_canonical_url": canonical,
        "seo_logo_url": logo,
        "favicon_version": BUILD_ID,
    }


@app.route("/favicon.ico")
def favicon_root():
    return send_from_directory(
        Path(app.root_path) / "static" / "img",
        "favicon.ico",
        mimetype="image/vnd.microsoft.icon",
        max_age=86400,
    )


@app.route("/robots.txt")
def robots_txt():
    body = f"User-agent: *\nAllow: /\n\nSitemap: {PUBLIC_BASE_URL}/sitemap.xml\n"
    return Response(body, mimetype="text/plain; charset=utf-8")


@app.route("/sitemap.xml")
def sitemap_xml():
    paths = (
        "/",
        "/dolacz",
        "/regulamin",
        "/polityka-prywatnosci",
        "/polityka-cookies",
    )
    urls = "\n".join(
        f"  <url><loc>{PUBLIC_BASE_URL}{path if path != '/' else '/'}</loc>"
        f"<changefreq>weekly</changefreq><priority>{'1.0' if path == '/' else '0.6'}</priority></url>"
        for path in paths
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>\n"
    )
    return Response(xml, mimetype="application/xml; charset=utf-8")


@app.route("/health")
def health():
    return jsonify(
        {
            "status": "ok",
            "app": "Glovaro",
            "build": BUILD_ID,
            "storage": tryb_magazynu(),
        }
    ), 200


@app.route("/tasks/send-reminders")
def wyslij_przypomnienia():
    if not REMINDER_SECRET or request.args.get("secret") != REMINDER_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    dane = wczytaj_dane()
    teraz = datetime.now()
    wyslane = 0
    sprawdzone = 0

    for salon_slug, salon in dane.get("salony", {}).items():
        if salon.get("przypomnienia_email_wlaczone", True) is False:
            continue
        try:
            godzin_przed = int(salon.get("przypomnienie_godzin_przed", 24) or 24)
        except (TypeError, ValueError):
            godzin_przed = 24
        godzin_przed = min(max(godzin_przed, 1), 168)
        okno_od = timedelta(0)
        okno_do = timedelta(hours=godzin_przed + 1)
        for rezerwacja in salon.get("rezerwacje", []):
            if rezerwacja.get("status", "potwierdzona") != "potwierdzona":
                continue
            try:
                termin = datetime.strptime(
                    f"{rezerwacja.get('data')} {rezerwacja.get('godzina')}", "%Y-%m-%d %H:%M"
                )
            except (TypeError, ValueError):
                continue
            do_wizyty = termin - teraz
            if okno_od <= do_wizyty <= okno_do:
                sprawdzone += 1
                wyslano_teraz = datetime.now().isoformat(timespec="minutes")
                salon_juz_wyslane = rezerwacja.get("przypomnienie_salon_wyslane") or rezerwacja.get("przypomnienie_wyslane")
                if not salon_juz_wyslane and wyslij_email_przypomnienie(salon, rezerwacja, salon_slug):
                    rezerwacja["przypomnienie_salon_wyslane"] = wyslano_teraz
                    rezerwacja["przypomnienie_wyslane"] = wyslano_teraz
                    rezerwacja["przypomnienie_godzin_przed"] = godzin_przed
                    wyslane += 1
                if not rezerwacja.get("przypomnienie_klient_wyslane") and wyslij_email_przypomnienie_klienta(salon, rezerwacja):
                    rezerwacja["przypomnienie_klient_wyslane"] = wyslano_teraz
                    rezerwacja["przypomnienie_klient_godzin_przed"] = godzin_przed
                    wyslane += 1

    if wyslane:
        zapisz_dane(dane)
    return jsonify({"checked": sprawdzone, "sent": wyslane})


@app.route("/tasks/cleanup-reservations")
def wyczysc_rezerwacje_task():
    """Cron: archiwizacja 1 h po wizycie, usuwanie wpisów starszych niż 90 dni."""
    if not REMINDER_SECRET or request.args.get("secret") != REMINDER_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    global _ostatnie_czyszczenie_rezerwacji
    _ostatnie_czyszczenie_rezerwacji = 0.0

    def czyszczenie_atomowe(dane_atomowe: dict):
        return utrzymuj_rezerwacje(dane_atomowe)

    zarchiwizowane, usuniete, zwolnione_nieoplacone = aktualizuj_dane_atomowo(czyszczenie_atomowe)
    return jsonify(
        {
            "archived": zarchiwizowane,
            "deleted": usuniete,
            "released_unpaid": zwolnione_nieoplacone,
        }
    )


@app.route("/")
def strona_glowna():
    dane = wczytaj_dane_katalogowe()
    wybrana_branza = request.args.get("branza", "").strip()
    if wybrana_branza and wybrana_branza not in BRANZE_MAP:
        wybrana_branza = ""
    return render_template(
        "index.html",
        salony=dane.get("salony", {}),
        wybrana_branza=wybrana_branza,
        wybrana_branza_nazwa=etykieta_branzy(wybrana_branza) if wybrana_branza else "",
        firmy_branzy=katalog_firm_z_cache(dane, wybrana_branza),
    )


@app.route("/dolacz", methods=["GET", "POST"])
def dolacz_firma():
    if request.method == "POST":
        dane = wczytaj_dane()
        nazwa = request.form.get("nazwa_salonu", "").strip()
        branza = normalizuj_branze(request.form.get("branza", "beauty"))
        slug = slugify(request.form.get("slug", "").strip() or nazwa)
        telefon = request.form.get("telefon_kontaktowy", "").strip()
        email = request.form.get("email_powiadomien", "").strip().lower()
        haslo = request.form.get("haslo_panelu", "").strip()
        zgoda = request.form.get("zgoda_regulamin") == "on"
        pokaz_instrukcje = request.form.get("pokaz_instrukcje") == "on"

        if not nazwa:
            flash("Podaj nazwę firmy.", "error")
            return redirect(url_for("dolacz_firma"))
        if slug in dane.get("salony", {}):
            flash("Taki link jest już zajęty. Wybierz inną nazwę w linku.", "error")
            return redirect(url_for("dolacz_firma"))
        if not waliduj_email(email):
            flash("Podaj poprawny e-mail właściciela firmy.", "error")
            return redirect(url_for("dolacz_firma"))
        if not waliduj_telefon(telefon):
            flash("Podaj poprawny numer telefonu.", "error")
            return redirect(url_for("dolacz_firma"))
        if len(haslo) < 6:
            flash("Hasło do panelu powinno mieć co najmniej 6 znaków.", "error")
            return redirect(url_for("dolacz_firma"))
        if not zgoda:
            flash("Zaakceptuj regulamin i politykę prywatności.", "error")
            return redirect(url_for("dolacz_firma"))

        salon = nowy_salon(nazwa, haslo)
        salon.update(
            {
                "slug": slug,
                "branza": branza,
                "telefon_kontaktowy": telefon,
                "email_powiadomien": email,
                "abonament_status": "pending_payment",
                "utworzono": datetime.now().isoformat(timespec="minutes"),
                "zrodlo_rejestracji": "publiczny_formularz",
            }
        )
        dane.setdefault("salony", {})[slug] = salon
        zapisz_dane(dane)
        ustaw_sesje_salonu(slug)
        flash("Panel został utworzony. Uzupełnij profil, usługi i terminy, a potem opłać abonament, aby uruchomić rezerwacje.", "success")
        if pokaz_instrukcje:
            return redirect(url_for("panel", salon_slug=slug, tour=1))
        return redirect(url_for("ustawienia_salonu", salon_slug=slug, start=1))

    return render_template("dolacz.html")


@app.route("/regulamin")
def regulamin():
    return render_template("regulamin.html")


@app.route("/polityka-prywatnosci")
def polityka_prywatnosci():
    return render_template("polityka_prywatnosci.html")


@app.route("/polityka-cookies")
def polityka_cookies():
    return render_template("polityka_cookies.html")


@app.errorhandler(404)
def nie_znaleziono(_error):
    dane = wczytaj_dane()
    domyslny = domyslny_slug(dane)
    return render_template("404.html", sciezka=request.path, domyslny_slug=domyslny), 404


@app.route("/panel")
def panel_lista():
    dane = wczytaj_dane()
    return render_template(
        "salony.html",
        salony=dane.get("salony", {}),
        dzisiaj=date.today().isoformat(),
    )


@app.route("/panel/nowy", methods=["POST"])
def panel_nowy_salon():
    dane = wczytaj_dane()
    nazwa = request.form.get("nazwa_salonu", "").strip()
    haslo = request.form.get("haslo_panelu", "").strip()
    branza = normalizuj_branze(request.form.get("branza", "beauty"))
    slug = slugify(request.form.get("slug", "").strip() or nazwa)

    if not nazwa:
        flash("Podaj nazwę salonu.", "error")
        return redirect(url_for("panel_lista"))
    if slug in dane.get("salony", {}):
        flash("Taki link już istnieje. Wybierz inną nazwę.", "error")
        return redirect(url_for("panel_lista"))

    salon = nowy_salon(nazwa, haslo)
    salon["slug"] = slug
    salon["branza"] = branza
    dane.setdefault("salony", {})[slug] = salon
    zapisz_dane(dane)
    flash(f"Dodano salon: {nazwa}.", "success")
    return redirect(url_for("panel", salon_slug=slug))


@app.route("/panel/<salon_slug>/usun", methods=["POST"])
def panel_usun_salon(salon_slug: str):
    if not session.get(admin_auth_key()):
        flash("Salon może usunąć tylko główny administrator Glovaro.", "error")
        return redirect(url_for("panel", salon_slug=salon_slug))

    dane = wczytaj_dane()
    salon = pobierz_salon(dane, salon_slug)
    if not salon:
        flash("Nie znaleziono takiego salonu.", "error")
        return redirect(url_for("panel_lista"))

    potwierdzenie = request.form.get("potwierdz_usuniecie", "").strip()
    if potwierdzenie != salon_slug:
        flash(
            f"Aby usunąć salon, wpisz dokładnie jego link: {salon_slug}.",
            "error",
        )
        return redirect(url_for("panel_lista"))

    nazwa = salon.get("nazwa_salonu", salon_slug)
    dane.get("salony", {}).pop(salon_slug, None)
    zapisz_dane(dane)
    session.pop(panel_auth_key(salon_slug), None)
    flash(f"Usunięto salon: {nazwa}.", "success")
    return redirect(url_for("panel_lista"))


@app.route("/panel/<salon_slug>/rozliczenia", methods=["POST"])
def panel_rozliczenia(salon_slug: str):
    if not session.get(admin_auth_key()):
        flash("Rozliczenia może zmieniać tylko główny administrator Glovaro.", "error")
        return redirect(url_for("panel", salon_slug=salon_slug))

    dane = wczytaj_dane()
    salon = pobierz_salon(dane, salon_slug)
    if not salon:
        flash("Nie znaleziono takiego salonu.", "error")
        return redirect(url_for("panel_lista"))

    status = request.form.get("abonament_status", "trial")
    if status not in STATUSY_ABONAMENTU:
        status = "trial"

    try:
        oplata = int(request.form.get("oplata_miesieczna", "100"))
    except ValueError:
        oplata = 100

    oplacone_do = request.form.get("oplacone_do", "").strip()
    if oplacone_do and not waliduj_date_iso(oplacone_do):
        flash("Nieprawidłowa data opłacenia.", "error")
        return redirect(url_for("panel_lista"))

    salon["abonament_status"] = status
    salon["oplata_miesieczna"] = max(oplata, 0)
    salon["oplacone_do"] = oplacone_do
    salon["notatka_rozliczeniowa"] = request.form.get("notatka_rozliczeniowa", "").strip()
    zapisz_dane(dane)
    flash(f"Zapisano rozliczenia dla: {salon['nazwa_salonu']}.", "success")
    return redirect(url_for("panel_lista"))


@app.route("/panel/<salon_slug>/stripe/checkout", methods=["POST"])
def stripe_checkout(salon_slug: str):
    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        include_clients=False,
        include_reservations=False,
        include_free_slots=False,
        include_waitlist=False,
    )
    if not salon:
        flash("Nie znaleziono takiego salonu.", "error")
        return redirect(url_for("panel_lista"))
    if not stripe_skonfigurowany():
        flash("Płatności Stripe nie są jeszcze skonfigurowane. Dodaj STRIPE_SECRET_KEY w Render.", "error")
        return redirect(url_for("panel", salon_slug=salon_slug))

    checkout_url = utworz_sesje_stripe(salon, salon_slug)
    if not checkout_url:
        flash("Nie udało się utworzyć płatności Stripe. Sprawdź logi Render albo klucz Stripe.", "error")
        return redirect(url_for("panel", salon_slug=salon_slug))
    return redirect(checkout_url)


@app.route("/rezerwacja/<salon_slug>/oplac", methods=["POST"])
def oplac_rezerwacje_online(salon_slug: str):
    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        include_clients=False,
        include_free_slots=False,
        include_waitlist=False,
    )
    if not salon:
        dane = wczytaj_dane()
        return render_template("404.html", sciezka=request.path, domyslny_slug=domyslny_slug(dane)), 404

    rezerwacja_id = request.form.get("id", "").strip()
    rezerwacja = znajdz_rezerwacje(salon, rezerwacja_id)
    if not rezerwacja:
        flash("Nie znaleziono rezerwacji do opłacenia.", "error")
        return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug))
    if rezerwacja.get("status") in {"anulowana", "odrzucona"}:
        flash("Nie można opłacić anulowanej lub odrzuconej rezerwacji.", "error")
        return redirect(url_for("rezerwacja_potwierdzenie", salon_slug=salon_slug, id=rezerwacja_id))
    if rezerwacja.get("oplacona_online"):
        flash("Ta rezerwacja jest już opłacona online.", "success")
        return redirect(url_for("rezerwacja_potwierdzenie", salon_slug=salon_slug, id=rezerwacja_id))
    if not salon.get("platnosc_online_wlaczona"):
        flash("Płatność online jest wyłączona dla tego salonu.", "error")
        return redirect(url_for("rezerwacja_potwierdzenie", salon_slug=salon_slug, id=rezerwacja_id))
    if not stripe_skonfigurowany():
        flash("Płatności online są chwilowo niedostępne.", "error")
        return redirect(url_for("rezerwacja_potwierdzenie", salon_slug=salon_slug, id=rezerwacja_id))
    if kwota_rezerwacji_zl(salon, rezerwacja) <= 0:
        flash("Brak ceny usługi do opłacenia online.", "error")
        return redirect(url_for("rezerwacja_potwierdzenie", salon_slug=salon_slug, id=rezerwacja_id))

    checkout_url = utworz_sesje_stripe_wizyta(salon, salon_slug, rezerwacja)
    if not checkout_url:
        flash("Nie udało się uruchomić płatności online. Spróbuj ponownie za chwilę.", "error")
        return redirect(url_for("rezerwacja_potwierdzenie", salon_slug=salon_slug, id=rezerwacja_id))
    return redirect(checkout_url)


@app.route("/rezerwacja/<salon_slug>/platnosc/sukces")
def platnosc_rezerwacji_sukces(salon_slug: str):
    flash("Dziękujemy! Płatność online została przyjęta.", "success")
    return redirect(url_for("rezerwacja_potwierdzenie", salon_slug=salon_slug, id=request.args.get("id", "")))


@app.route("/panel/<salon_slug>/stripe/sukces")
def stripe_sukces(salon_slug: str):
    flash("Dziękujemy za płatność. Stripe potwierdzi ją automatycznie po webhooku.", "success")
    return redirect(url_for("panel", salon_slug=salon_slug))


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.get_data()
    if not podpis_stripe_poprawny(payload, request.headers.get("Stripe-Signature", "")):
        return jsonify({"error": "invalid signature"}), 400

    try:
        event = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError:
        return jsonify({"error": "invalid json"}), 400

    event_type = event.get("type", "")
    obiekt = event.get("data", {}).get("object", {})
    metadata = obiekt.get("metadata") or {}
    subscription_metadata = obiekt.get("subscription_details", {}).get("metadata") or {}
    salon_slug = (
        metadata.get("salon_slug")
        or subscription_metadata.get("salon_slug")
        or obiekt.get("client_reference_id")
    )

    if event_type == "checkout.session.completed" and metadata.get("typ_platnosci") == "wizyta":
        rezerwacja_id = metadata.get("rezerwacja_id", "")
        if salon_slug and rezerwacja_id:
            def potwierdz_platnosc_wizyty(salon_atomowy: dict | None) -> bool:
                if not salon_atomowy:
                    return False
                rezerwacja = znajdz_rezerwacje(salon_atomowy, rezerwacja_id)
                if not rezerwacja or rezerwacja.get("oplacona_online"):
                    return False
                try:
                    kwota_zl = int(metadata.get("kwota_zl", kwota_wizyty_zl(salon_atomowy)))
                except (TypeError, ValueError):
                    kwota_zl = kwota_wizyty_zl(salon_atomowy)
                rezerwacja["oplacona_online"] = True
                rezerwacja["oplacono_online_at"] = datetime.now().isoformat(timespec="minutes")
                rezerwacja["oplacono_kwota_zl"] = kwota_zl
                return True

            zapisano = aktualizuj_salon_atomowo(salon_slug, potwierdz_platnosc_wizyty)
            if zapisano:
                app.logger.info(
                    "Stripe potwierdził płatność wizyty dla salonu %s, rezerwacja %s",
                    salon_slug,
                    rezerwacja_id,
                )
    elif event_type in {"checkout.session.completed", "invoice.paid"} and salon_slug:
        def potwierdz_abonament(salon_atomowy: dict | None) -> bool:
            if not salon_atomowy:
                return False
            przedluz_abonament(salon_atomowy)
            return True

        if aktualizuj_salon_atomowo(salon_slug, potwierdz_abonament):
            app.logger.info("Stripe potwierdził płatność dla salonu %s", salon_slug)

    return jsonify({"received": True})


@app.route("/panel/<salon_slug>")
def panel(salon_slug: str):
    data_od, data_do = zakres_panelu_pulpit()
    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        data_od=data_od,
        data_do=data_do,
        include_clients=False,
    )
    if not salon:
        flash("Nie znaleziono takiego salonu.", "error")
        return redirect(url_for("panel_lista"))

    dzisiaj = date.today().isoformat()
    terminy_dzis = dostepne_terminy(salon, dzisiaj)
    rezerwacje = sorted(
        salon.get("rezerwacje", []),
        key=lambda r: (r.get("data", ""), r.get("godzina", "")),
    )
    aktywne = [r for r in rezerwacje if rezerwacja_aktywna_w_panelu(r)]
    nadchodzace = [
        r
        for r in aktywne
        if r.get("status", "potwierdzona") not in {"zakonczona"}
    ]
    klienci = {normalizuj_telefon(r.get("telefon", "")) for r in aktywne if r.get("telefon")}
    dni_count: dict[str, int] = {}
    for r in aktywne:
        data_rezerwacji = r.get("data", "")
        if waliduj_date_iso(data_rezerwacji):
            dzien = dict(DNI_TYGODNIA)[klucz_dnia_tygodnia(data_rezerwacji)]
            dni_count[dzien] = dni_count.get(dzien, 0) + 1
    najpopularniejszy_dzien = max(dni_count.items(), key=lambda item: item[1])[0] if dni_count else "-"
    za_7_dni = (date.today() + timedelta(days=7)).isoformat()
    rezerwacje_7_dni = [r for r in nadchodzace if r.get("data", "") <= za_7_dni]
    lista_rezerwowa = aktywne_zgloszenia_listy_rezerwowej(salon)
    return render_template(
        "panel.html",
        dane=salon,
        salon_slug=salon_slug,
        dzisiaj=dzisiaj,
        terminy_dzis=terminy_dzis,
        liczba_dni_z_terminami=len(najblizsze_daty_z_terminami(salon, limit=HORYZONT_REZERWACJI_DNI)),
        najblizsze_daty=najblizsze_daty_z_terminami(salon, limit=5),
        liczba_rezerwacji=len(nadchodzace),
        ostatnie_rezerwacje=nadchodzace[:5],
        lista_rezerwowa=lista_rezerwowa[:5],
        liczba_listy_rezerwowej=len(lista_rezerwowa),
        statystyki={
            "wszystkie_rezerwacje": len(aktywne),
            "unikalni_klienci": len([k for k in klienci if k]),
            "najpopularniejszy_dzien": najpopularniejszy_dzien,
            "rezerwacje_7_dni": len(rezerwacje_7_dni),
        },
    )


@app.route("/panel/<salon_slug>/instrukcja")
def panel_instrukcja(salon_slug: str):
    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        include_clients=False,
        include_reservations=False,
        include_free_slots=False,
        include_waitlist=False,
    )
    if not salon:
        flash("Nie znaleziono takiej firmy.", "error")
        return redirect(url_for("panel_lista"))
    return render_template("instrukcja_panelu.html", dane=salon, salon_slug=salon_slug)


@app.route("/panel/<salon_slug>/salon", methods=["GET", "POST"])
def ustawienia_salonu(salon_slug: str):
    if request.method == "POST":
        nazwa = request.form.get("nazwa_salonu", "").strip()
        branza = normalizuj_branze(request.form.get("branza", "beauty"))
        haslo = request.form.get("haslo_panelu", "").strip()
        opis = request.form.get("opis", "").strip()
        telefon = request.form.get("telefon_kontaktowy", "").strip()
        logo_z_linku = normalizuj_url_https(request.form.get("logo_url", ""))
        adres_lokalizacji = request.form.get("adres_lokalizacji", "").strip()
        link_google_maps = normalizuj_url_https(request.form.get("link_google_maps", ""))
        instagram = request.form.get("instagram", "").strip()
        email_powiadomien = request.form.get("email_powiadomien", "").strip()
        przypomnienia_email_wlaczone = request.form.get("przypomnienia_email_wlaczone") == "on"
        try:
            przypomnienie_godzin_przed = int(request.form.get("przypomnienie_godzin_przed", "24").strip() or "24")
        except ValueError:
            przypomnienie_godzin_przed = 24
        przypomnienie_godzin_przed = min(max(przypomnienie_godzin_przed, 1), 168)
        motyw_strony = request.form.get("motyw_strony", "rozowy").strip()
        if motyw_strony not in MOTYWY_STRONY:
            motyw_strony = "rozowy"
        tryb_platnosci = request.form.get("tryb_platnosci_wizyty", "w_salonie").strip()
        if tryb_platnosci not in TRYBY_PLATNOSCI_WIZYTY:
            tryb_platnosci = "w_salonie"
        konto_bankowe = request.form.get("konto_bankowe", "").strip()
        odbiorca_przelewu = request.form.get("odbiorca_przelewu", "").strip()
        link_szybkiej_platnosci = normalizuj_url_https(request.form.get("link_szybkiej_platnosci", ""))
        platnosc_online_wlaczona = request.form.get("platnosc_online_wlaczona") == "on"
        try:
            cena_wizyty = int(request.form.get("cena_wizyty", "0").strip() or "0")
        except ValueError:
            cena_wizyty = 0
        cena_wizyty = max(cena_wizyty, 0)
        pracownicy = parsuj_pracownikow(request.form.get("pracownicy", ""))
        uslugi = parsuj_uslugi(request.form.get("uslugi", ""))
        logo_upload = parsuj_upload_zdjec([request.files.get("logo_upload")])
        zdjecia_z_linkow = parsuj_linki_zdjec(request.form.get("zdjecia_prac", ""))
        nowe_zdjecia = parsuj_upload_zdjec(request.files.getlist("zdjecia_upload"))

        def zapisz_ustawienia_atomowo(salon_atomowy: dict | None):
            if not salon_atomowy:
                return "Nie znaleziono takiego salonu.", "error"

            logo_url = logo_upload[0] if logo_upload else logo_z_linku or salon_atomowy.get("logo_url", "")
            if request.form.get("usun_logo") == "on":
                logo_url = ""
            dotychczasowe_uploady = [
                z
                for z in salon_atomowy.get("zdjecia_prac", [])
                if isinstance(z, str) and z.startswith("data:image/")
            ]
            if request.form.get("usun_wgrane_zdjecia") == "on":
                dotychczasowe_uploady = []
            zdjecia = (zdjecia_z_linkow + dotychczasowe_uploady + nowe_zdjecia)[:12]

            salon_atomowy["adres_lokalizacji"] = adres_lokalizacji
            salon_atomowy["link_google_maps"] = link_google_maps
            salon_atomowy["tryb_platnosci_wizyty"] = tryb_platnosci
            salon_atomowy["konto_bankowe"] = konto_bankowe
            salon_atomowy["odbiorca_przelewu"] = odbiorca_przelewu
            salon_atomowy["link_szybkiej_platnosci"] = link_szybkiej_platnosci

            if not nazwa:
                return "Podaj nazwę salonu. Lokalizacja została zapisana.", "error"

            salon_atomowy["nazwa_salonu"] = nazwa
            salon_atomowy["branza"] = branza
            salon_atomowy["opis"] = opis
            salon_atomowy["logo_url"] = logo_url
            salon_atomowy["telefon_kontaktowy"] = telefon
            salon_atomowy["instagram"] = instagram
            salon_atomowy["email_powiadomien"] = email_powiadomien
            salon_atomowy["przypomnienia_email_wlaczone"] = przypomnienia_email_wlaczone
            salon_atomowy["przypomnienie_godzin_przed"] = przypomnienie_godzin_przed
            salon_atomowy["motyw_strony"] = motyw_strony
            salon_atomowy["platnosc_online_wlaczona"] = platnosc_online_wlaczona
            salon_atomowy["cena_wizyty"] = cena_wizyty
            salon_atomowy["pracownicy"] = pracownicy
            salon_atomowy["uslugi"] = uslugi
            salon_atomowy["zdjecia_prac"] = zdjecia
            if haslo:
                salon_atomowy["haslo_panelu"] = haslo
            if tryb_platnosci == "przelew" and not konto_bankowe:
                return "Zapisano. Uzupełnij numer konta — bez niego klienci nie zobaczą danych do przelewu.", "error"
            if adres_lokalizacji or link_google_maps:
                return "Ustawienia zapisane (w tym lokalizacja).", "success"
            return "Ustawienia salonu zostały zapisane.", "success"

        komunikat, kategoria = aktualizuj_salon_atomowo(salon_slug, zapisz_ustawienia_atomowo)
        flash(komunikat, kategoria)
        return redirect(url_for("ustawienia_salonu", salon_slug=salon_slug))

    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        include_clients=False,
        include_reservations=False,
        include_free_slots=False,
        include_waitlist=False,
    )
    if not salon:
        flash("Nie znaleziono takiego salonu.", "error")
        return redirect(url_for("panel_lista"))

    return render_template(
        "salon.html",
        dane=salon,
        salon_slug=salon_slug,
        **kontekst_lokalizacji_salonu(salon),
    )


@app.route("/panel/<salon_slug>/godziny", methods=["GET", "POST"])
def godziny_pracy(salon_slug: str):
    if request.method == "POST":
        nowe_godziny = {}
        for klucz, _ in DNI_TYGODNIA:
            zamkniety = request.form.get(f"zamkniety_{klucz}") == "on"
            otwarcie = request.form.get(f"otwarcie_{klucz}", "09:00")
            zamkniecie = request.form.get(f"zamkniecie_{klucz}", "18:00")

            if not zamkniety and (
                not waliduj_godzine(otwarcie) or not waliduj_godzine(zamkniecie)
            ):
                flash(f"Nieprawidłowy format godzin dla {klucz}. Użyj HH:MM.", "error")
                return redirect(url_for("godziny_pracy", salon_slug=salon_slug))
            if not zamkniety and otwarcie >= zamkniecie:
                flash("Godzina otwarcia musi być wcześniejsza niż zamknięcia.", "error")
                return redirect(url_for("godziny_pracy", salon_slug=salon_slug))

            nowe_godziny[klucz] = {
                "otwarcie": otwarcie,
                "zamkniecie": zamkniecie,
                "zamkniety": zamkniety,
            }
        interwal = request.form.get("interwal_terminow", "30")
        interwal_int = int(interwal) if interwal in {"15", "30", "45", "60"} else 30

        def zapisz_godziny_atomowo(salon_atomowy: dict | None):
            if not salon_atomowy:
                return "Nie znaleziono takiego salonu.", "error"
            salon_atomowy["godziny_pracy"] = nowe_godziny
            salon_atomowy["interwal_terminow"] = interwal_int
            return "Godziny pracy zostały zapisane.", "success"

        komunikat, kategoria = aktualizuj_salon_atomowo(salon_slug, zapisz_godziny_atomowo)
        flash(komunikat, kategoria)
        return redirect(url_for("godziny_pracy", salon_slug=salon_slug))

    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        include_clients=False,
        include_reservations=False,
        include_free_slots=False,
        include_waitlist=False,
    )
    if not salon:
        flash("Nie znaleziono takiego salonu.", "error")
        return redirect(url_for("panel_lista"))

    return render_template("godziny.html", dane=salon, salon_slug=salon_slug)


@app.route("/panel/<salon_slug>/terminy", methods=["GET", "POST"])
def wolne_terminy(salon_slug: str):
    wybrana_data = request.args.get("data") or request.form.get("data") or date.today().isoformat()
    if not waliduj_date_iso(wybrana_data):
        wybrana_data = date.today().isoformat()
        flash("Nieprawidłowa data — pokazuję dzisiejszy dzień.", "error")

    if request.method == "POST":
        akcja = request.form.get("akcja")
        godzina = request.form.get("godzina", "").strip()

        def zmien_wolne_terminy_atomowo(salon_atomowy: dict | None):
            if not salon_atomowy:
                return "Nie znaleziono takiego salonu.", "error", wybrana_data

            if akcja == "generuj":
                data_od = request.form.get("data_od", wybrana_data)
                data_do = request.form.get("data_do", data_od)
                od_godziny = request.form.get("od_godziny", "")
                do_godziny = request.form.get("do_godziny", "")
                interwal = request.form.get("interwal", "30")
                nadpisz = request.form.get("nadpisz") == "on"

                if (
                    not waliduj_date_iso(data_od)
                    or not waliduj_date_iso(data_do)
                    or not waliduj_godzine(od_godziny)
                    or not waliduj_godzine(do_godziny)
                    or interwal not in {"15", "30", "45", "60"}
                ):
                    return "Uzupełnij poprawnie zakres generowania terminów.", "error", wybrana_data

                bazowy_start_min = czas_na_minuty(od_godziny)
                bazowy_koniec_min = czas_na_minuty(do_godziny)
                krok = int(interwal)
                if bazowy_start_min >= bazowy_koniec_min:
                    return "Godzina startu musi być wcześniejsza niż końca.", "error", wybrana_data

                dodane = 0
                for data_key in daty_w_zakresie(data_od, data_do):
                    dzien_key = klucz_dnia_tygodnia(data_key)
                    gh = salon_atomowy.get("godziny_pracy", {}).get(dzien_key, {})
                    if gh.get("zamkniety"):
                        continue
                    dzien_otwarcie = gh.get("otwarcie") or od_godziny
                    dzien_zamkniecie = gh.get("zamkniecie") or do_godziny
                    start_min = bazowy_start_min
                    koniec_min = bazowy_koniec_min
                    if waliduj_godzine(dzien_otwarcie) and waliduj_godzine(dzien_zamkniecie):
                        start_min = czas_na_minuty(dzien_otwarcie)
                        koniec_min = czas_na_minuty(dzien_zamkniecie)
                    if start_min >= koniec_min:
                        continue
                    if nadpisz:
                        salon_atomowy["wolne_terminy"][data_key] = []
                    salon_atomowy.setdefault("wolne_terminy", {}).setdefault(data_key, [])
                    for minuta in range(start_min, koniec_min, krok):
                        slot = minuty_na_czas(minuta)
                        if not powod_odbioru_wolnego_terminu(salon_atomowy, data_key, slot):
                            salon_atomowy["wolne_terminy"][data_key].append(slot)
                            dodane += 1
                    salon_atomowy["wolne_terminy"][data_key].sort()
                    if not salon_atomowy["wolne_terminy"][data_key]:
                        salon_atomowy["wolne_terminy"].pop(data_key, None)

                return f"Wygenerowano {dodane} nowych terminów.", "success", data_od

            if akcja == "dodaj":
                godzina = normalizuj_godzine(godzina)
                blad_dodania = powod_odbioru_wolnego_terminu(salon_atomowy, wybrana_data, godzina)
                if blad_dodania:
                    return blad_dodania, "error", wybrana_data
                terminy = normalizuj_wolne_terminy_dnia(salon_atomowy, wybrana_data)
                terminy.append(godzina)
                terminy.sort()
                salon_atomowy["wolne_terminy"][wybrana_data] = terminy
                return f"Dodano wolny termin: {godzina}.", "success", wybrana_data

            if akcja == "usun":
                terminy = salon_atomowy.get("wolne_terminy", {}).get(wybrana_data, [])
                if godzina in terminy:
                    terminy.remove(godzina)
                    if not terminy:
                        salon_atomowy["wolne_terminy"].pop(wybrana_data, None)
                    return f"Usunięto termin: {godzina}.", "success", wybrana_data
                return "Nie znaleziono takiego wolnego terminu.", "error", wybrana_data

            if akcja == "blokuj":
                data_od = request.form.get("blokada_data_od", wybrana_data)
                data_do = request.form.get("blokada_data_do", data_od)
                powod = request.form.get("powod", "Przerwa").strip() or "Przerwa"
                opis = request.form.get("opis_blokady", "").strip()
                caly_dzien = request.form.get("caly_dzien") == "on"
                od_godziny = request.form.get("blokada_od_godziny", "")
                do_godziny = request.form.get("blokada_do_godziny", "")

                if not waliduj_date_iso(data_od) or not waliduj_date_iso(data_do):
                    return "Podaj poprawny zakres dat blokady.", "error", wybrana_data
                if not caly_dzien and (
                    not waliduj_godzine(od_godziny) or not waliduj_godzine(do_godziny) or od_godziny >= do_godziny
                ):
                    return "Podaj poprawny zakres godzin blokady.", "error", wybrana_data

                salon_atomowy.setdefault("blokady", []).append(
                    {
                        "id": uuid.uuid4().hex[:12],
                        "data_od": data_od,
                        "data_do": data_do,
                        "caly_dzien": caly_dzien,
                        "od_godziny": "" if caly_dzien else od_godziny,
                        "do_godziny": "" if caly_dzien else do_godziny,
                        "powod": powod,
                        "opis": opis,
                        "utworzono": datetime.now().isoformat(timespec="minutes"),
                    }
                )
                return "Dodano blokadę terminów.", "success", wybrana_data

            if akcja == "usun_blokade":
                blokada_id = request.form.get("blokada_id", "")
                przed = len(salon_atomowy.get("blokady", []))
                salon_atomowy["blokady"] = [b for b in salon_atomowy.get("blokady", []) if b.get("id") != blokada_id]
                if len(salon_atomowy["blokady"]) < przed:
                    return "Usunięto blokadę.", "success", wybrana_data
                return "Nie znaleziono blokady.", "error", wybrana_data

            return "Nieznana akcja terminów.", "error", wybrana_data

        komunikat, kategoria, data_redirect = aktualizuj_salon_atomowo(salon_slug, zmien_wolne_terminy_atomowo)
        flash(komunikat, kategoria)
        return redirect(url_for("wolne_terminy", salon_slug=salon_slug, data=data_redirect))

    data_od, data_do = zakres_miesiaca(wybrana_data)
    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        data_od=data_od,
        data_do=data_do,
        include_clients=False,
        include_reservations=True,
        include_free_slots=True,
        include_waitlist=False,
    )
    if not salon:
        flash("Nie znaleziono takiego salonu.", "error")
        return redirect(url_for("panel_lista"))

    data_wybrana = datetime.strptime(wybrana_data, "%Y-%m-%d").date()
    pierwszy_dzien_miesiaca = data_wybrana.replace(day=1)
    liczba_dni = calendar.monthrange(data_wybrana.year, data_wybrana.month)[1]
    dni_kalendarza = []
    for przesuniecie in range(liczba_dni):
        data_iso = (pierwszy_dzien_miesiaca + timedelta(days=przesuniecie)).isoformat()
        wolne_dnia = dostepne_terminy(salon, data_iso)
        zajete_dnia = sorted(zajete_godziny(salon, data_iso))
        blokady = blokady_dnia(salon, data_iso)
        dni_kalendarza.append(
            {
                "data": data_iso,
                "dzien": dict(DNI_TYGODNIA)[klucz_dnia_tygodnia(data_iso)],
                "numer": int(data_iso[8:10]),
                "wolne": wolne_dnia,
                "zajete": zajete_dnia,
                "blokady": blokady,
                "zamkniety": harmonogram_dnia(salon, data_iso).get("zamkniety", False),
                "aktywny": data_iso == wybrana_data,
            }
        )

    poprzedni_miesiac_data = (pierwszy_dzien_miesiaca - timedelta(days=1)).replace(day=1)
    if data_wybrana.month == 12:
        nastepny_miesiac_data = data_wybrana.replace(year=data_wybrana.year + 1, month=1, day=1)
    else:
        nastepny_miesiac_data = data_wybrana.replace(month=data_wybrana.month + 1, day=1)

    return render_template(
        "terminy.html",
        dane=salon,
        salon_slug=salon_slug,
        wybrana_data=wybrana_data,
        terminy=dostepne_terminy(salon, wybrana_data),
        zajete=sorted(zajete_godziny(salon, wybrana_data)),
        blokady=blokady_dnia(salon, wybrana_data),
        wszystkie_terminy=salon.get("wolne_terminy", {}),
        dni_kalendarza=dni_kalendarza,
        puste_przed=pierwszy_dzien_miesiaca.weekday(),
        miesiac_label=f"{MIESIACE[data_wybrana.month]} {data_wybrana.year}",
        poprzedni_miesiac=poprzedni_miesiac_data.isoformat(),
        nastepny_miesiac=nastepny_miesiac_data.isoformat(),
    )


def kontekst_rezerwacji(salon: dict, salon_slug: str, wybrana_data: str) -> dict:
    if not waliduj_date_iso(wybrana_data):
        wybrana_data = date.today().isoformat()
    dzien_tygodnia = klucz_dnia_tygodnia(wybrana_data)
    godziny = salon["godziny_pracy"].get(dzien_tygodnia, {})
    terminy = dostepne_terminy(salon, wybrana_data)
    najblizsze = najblizsze_daty_z_terminami(salon)
    opinie = widoczne_opinie(salon)
    return {
        "dane": salon,
        "salon_slug": salon_slug,
        "wybrana_data": wybrana_data,
        "dzisiaj": date.today().isoformat(),
        "dzien_tygodnia": dzien_tygodnia,
        "godziny": godziny,
        "terminy": terminy,
        "najblizsze_daty": najblizsze,
        "najblizszy_inny": next((d for d in najblizsze if d["data"] != wybrana_data), None),
        "dni_tygodnia": dict(DNI_TYGODNIA),
        "pracownicy": aktywni_pracownicy(salon),
        "uslugi": uslugi_salonu(salon),
        "opinie": sorted(opinie, key=lambda o: o.get("utworzono", ""), reverse=True)[:6],
        "srednia_ocena": srednia_ocen(opinie),
        "tresc_wywiadu": tresc_wywiadu_salonu(salon),
        "wywiad_przy_rezerwacji": wywiad_przy_rezerwacji_wlaczony(salon),
        **kontekst_lokalizacji_salonu(salon),
    }


@app.route("/rezerwacja")
def rezerwacja_domyslna():
    dane = wczytaj_dane()
    return redirect(url_for("rezerwacja_publiczna", salon_slug=domyslny_slug(dane), **request.args))


@app.route("/rezerwacja/<salon_slug>")
def rezerwacja_publiczna(salon_slug: str):
    data_od, data_do = zakres_publicznej_rezerwacji(request.args.get("data"))
    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        data_od=data_od,
        data_do=data_do,
        include_clients=False,
    )
    if not salon:
        dane = wczytaj_dane()
        return render_template("404.html", sciezka=request.path, domyslny_slug=domyslny_slug(dane)), 404
    if salon_wstrzymany(salon):
        return render_template("abonament_wstrzymany.html", dane=salon), 403
    preferowana = request.args.get("data", "").strip()
    if preferowana and waliduj_date_iso(preferowana):
        wybrana_data = preferowana
    else:
        wybrana_data = domyslna_data_rezerwacji(salon)
    if wybrana_data < date.today().isoformat():
        wybrana_data = domyslna_data_rezerwacji(salon)
    return render_template("podglad.html", **kontekst_rezerwacji(salon, salon_slug, wybrana_data))


@app.route("/rezerwacja/<salon_slug>/nowa", methods=["GET", "POST"])
def rezerwacja_formularz(salon_slug: str):
    data_zapytania = request.values.get("data", date.today().isoformat())
    data_od, data_do = zakres_publicznej_rezerwacji(data_zapytania)
    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        data_od=data_od,
        data_do=data_do,
        include_clients=False,
    )
    if not salon:
        dane = wczytaj_dane()
        return render_template("404.html", sciezka=request.path, domyslny_slug=domyslny_slug(dane)), 404
    if salon_wstrzymany(salon):
        return render_template("abonament_wstrzymany.html", dane=salon), 403

    data_iso = request.values.get("data", date.today().isoformat())
    godzina = request.values.get("godzina", "").strip()
    if not waliduj_date_iso(data_iso):
        flash("Nieprawidłowa data.", "error")
        return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug))

    if request.method == "GET":
        if not godzina or godzina not in dostepne_terminy(salon, data_iso):
            flash("Wybierz dostępny termin z listy.", "error")
            return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug, data=data_iso))
        ctx = kontekst_rezerwacji(salon, salon_slug, data_iso)
        ctx["godzina"] = godzina
        return render_template("rezerwacja_form.html", **ctx)

    imie = request.form.get("imie", "").strip()
    telefon = request.form.get("telefon", "").strip()
    email = request.form.get("email", "").strip().lower()
    uwagi = request.form.get("uwagi", "").strip()
    godzina = request.form.get("godzina", "").strip()
    pracownik_formularz = request.form.get("pracownik", "").strip()
    pracownik = pracownik_formularz
    usluga_nazwa = request.form.get("usluga", "").strip()
    pracownicy = aktywni_pracownicy(salon)
    uslugi = uslugi_salonu(salon)
    mapa_uslug = {u["nazwa"]: u for u in uslugi}
    wybrana_usluga = mapa_uslug.get(usluga_nazwa, {})
    czas_uslugi = czas_trwania_rezerwacji_min(
        salon,
        {"usluga_czas_min": wybrana_usluga.get("czas_min", 0)},
    )

    if przekroczono_limit_rezerwacji(salon_slug):
        flash("Zbyt wiele prób rezerwacji. Spróbuj ponownie za kilka minut.", "error")
        return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug, data=data_iso))
    if pracownicy and pracownik == "__dowolny__":
        pracownik = next((p for p in pracownicy if not pracownik_zajety(salon, data_iso, godzina, p, czas_uslugi)), "")
    if pracownicy and pracownik not in pracownicy:
        flash("Wybierz pracownika z listy.", "error")
        return redirect(url_for("rezerwacja_formularz", salon_slug=salon_slug, data=data_iso, godzina=godzina))
    if uslugi and usluga_nazwa not in mapa_uslug:
        flash("Wybierz usługę z listy.", "error")
        return redirect(url_for("rezerwacja_formularz", salon_slug=salon_slug, data=data_iso, godzina=godzina))
    if pracownik and pracownik_zajety(salon, data_iso, godzina, pracownik, czas_uslugi):
        flash("Ten pracownik jest już zajęty o tej godzinie. Wybierz inną osobę albo termin.", "error")
        return redirect(url_for("rezerwacja_formularz", salon_slug=salon_slug, data=data_iso, godzina=godzina))
    if not imie:
        flash("Podaj imię i nazwisko.", "error")
        return redirect(url_for("rezerwacja_formularz", salon_slug=salon_slug, data=data_iso, godzina=godzina))
    if not waliduj_telefon(telefon):
        flash("Podaj poprawny numer telefonu (min. 9 cyfr).", "error")
        return redirect(url_for("rezerwacja_formularz", salon_slug=salon_slug, data=data_iso, godzina=godzina))
    if not waliduj_email(email):
        flash("Podaj poprawny adres e-mail — wyślemy na niego przypomnienie o wizycie.", "error")
        return redirect(url_for("rezerwacja_formularz", salon_slug=salon_slug, data=data_iso, godzina=godzina))
    if request.form.get("zgoda_rodo") != "on":
        flash("Zaakceptuj informację o przetwarzaniu danych, aby złożyć rezerwację.", "error")
        return redirect(url_for("rezerwacja_formularz", salon_slug=salon_slug, data=data_iso, godzina=godzina))
    wywiad_odpowiedzi: dict[str, str] = {}
    if wywiad_przy_rezerwacji_wlaczony(salon):
        wywiad_odpowiedzi, bledy_wywiad = akceptacja_wywiadu_z_rezerwacji()
        if bledy_wywiad:
            flash(bledy_wywiad[0], "error")
            return redirect(url_for("rezerwacja_formularz", salon_slug=salon_slug, data=data_iso, godzina=godzina))
    def utworz_atomowo(salon_atomowy: dict | None):
        if not salon_atomowy:
            return None, "Nie znaleziono takiej firmy.", None
        if salon_wstrzymany(salon_atomowy):
            return None, "Rezerwacje dla tej firmy są chwilowo niedostępne.", None

        final_pracownik = pracownik_formularz
        final_pracownicy = aktywni_pracownicy(salon_atomowy)
        final_uslugi = uslugi_salonu(salon_atomowy)
        final_mapa_uslug = {u["nazwa"]: u for u in final_uslugi}
        if final_uslugi and usluga_nazwa not in final_mapa_uslug:
            return None, "Wybierz usługę z listy.", None
        final_usluga = final_mapa_uslug.get(usluga_nazwa, {})
        final_czas = czas_trwania_rezerwacji_min(
            salon_atomowy,
            {"usluga_czas_min": final_usluga.get("czas_min", 0)},
        )
        if final_pracownicy and final_pracownik == "__dowolny__":
            final_pracownik = next(
                (
                    p
                    for p in final_pracownicy
                    if not pracownik_zajety(salon_atomowy, data_iso, godzina, p, final_czas)
                ),
                "",
            )

        rezerwacja_atomowa, blad_atomowy = utworz_rezerwacje(
            salon_atomowy,
            data_iso=data_iso,
            godzina=godzina,
            imie=imie,
            telefon=telefon,
            email=email,
            uwagi=uwagi,
            pracownik=final_pracownik,
            usluga_nazwa=usluga_nazwa,
            status="oczekuje",
            zrodlo="online",
            wywiad_odpowiedzi=wywiad_odpowiedzi or None,
        )
        return rezerwacja_atomowa, blad_atomowy, copy.deepcopy(salon_atomowy)

    rezerwacja, blad, salon_po_zapisie = aktualizuj_salon_atomowo(salon_slug, utworz_atomowo)
    if blad:
        flash(blad, "error")
        return redirect(url_for("rezerwacja_formularz", salon_slug=salon_slug, data=data_iso, godzina=godzina))

    wyslij_email_powiadomienie(salon_po_zapisie or salon, rezerwacja, salon_slug)
    return redirect(url_for("rezerwacja_potwierdzenie", salon_slug=salon_slug, id=rezerwacja["id"]))


@app.route("/rezerwacja/<salon_slug>/lista-rezerwowa", methods=["POST"])
def lista_rezerwowa_formularz(salon_slug: str):
    data_od, data_do = zakres_publicznej_rezerwacji(request.form.get("data_preferowana"))
    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        data_od=data_od,
        data_do=data_do,
        include_clients=False,
    )
    if not salon:
        dane = wczytaj_dane()
        return render_template("404.html", sciezka=request.path, domyslny_slug=domyslny_slug(dane)), 404
    if salon_wstrzymany(salon):
        return render_template("abonament_wstrzymany.html", dane=salon), 403

    data_preferowana = request.form.get("data_preferowana", date.today().isoformat()).strip()
    if not waliduj_date_iso(data_preferowana) or data_preferowana < date.today().isoformat():
        data_preferowana = date.today().isoformat()
    imie = request.form.get("lista_imie", "").strip()
    telefon = request.form.get("lista_telefon", "").strip()
    email = request.form.get("lista_email", "").strip().lower()
    usluga_nazwa = request.form.get("lista_usluga", "").strip()
    uwagi = request.form.get("lista_uwagi", "").strip()[:600]

    if not imie:
        flash("Podaj imię i nazwisko do listy rezerwowej.", "error")
        return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug, data=data_preferowana))
    if not waliduj_telefon(telefon):
        flash("Podaj poprawny numer telefonu do listy rezerwowej.", "error")
        return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug, data=data_preferowana))
    if email and not waliduj_email(email):
        flash("Podaj poprawny adres e-mail albo zostaw pole puste.", "error")
        return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug, data=data_preferowana))
    if request.form.get("zgoda_lista_rezerwowa") != "on":
        flash("Zaakceptuj zgodę na kontakt w sprawie listy rezerwowej.", "error")
        return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug, data=data_preferowana))

    uslugi = uslugi_salonu(salon)
    nazwy_uslug = {u["nazwa"] for u in uslugi}
    if uslugi and usluga_nazwa and usluga_nazwa not in nazwy_uslug:
        flash("Wybierz usługę z listy albo zostaw pole puste.", "error")
        return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug, data=data_preferowana))

    zgloszenie = {
        "id": uuid.uuid4().hex[:12],
        "status": "nowe",
        "data_preferowana": data_preferowana,
        "imie": imie,
        "telefon": telefon,
        "email": email,
        "usluga_nazwa": usluga_nazwa,
        "uwagi": uwagi,
        "utworzono": datetime.now().isoformat(timespec="minutes"),
    }
    def dopisz_atomowo(salon_atomowy: dict | None):
        if not salon_atomowy:
            return None, "Nie znaleziono takiej firmy.", None
        if salon_wstrzymany(salon_atomowy):
            return None, "Rezerwacje dla tej firmy są chwilowo niedostępne.", None
        final_uslugi = uslugi_salonu(salon_atomowy)
        final_nazwy_uslug = {u["nazwa"] for u in final_uslugi}
        if final_uslugi and usluga_nazwa and usluga_nazwa not in final_nazwy_uslug:
            return None, "Wybierz usługę z listy albo zostaw pole puste.", None
        salon_atomowy.setdefault("lista_rezerwowa", []).append(zgloszenie)
        return zgloszenie, "", copy.deepcopy(salon_atomowy)

    zapisane_zgloszenie, blad, salon_po_zapisie = aktualizuj_salon_atomowo(salon_slug, dopisz_atomowo)
    if blad:
        flash(blad, "error")
        return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug, data=data_preferowana))

    wyslij_email_lista_rezerwowa(salon_po_zapisie or salon, zapisane_zgloszenie, salon_slug)
    flash("Dopisano Cię do listy rezerwowej. Firma skontaktuje się, gdy zwolni się termin.", "success")
    return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug, data=data_preferowana))


@app.route("/rezerwacja/<salon_slug>/potwierdzenie")
def rezerwacja_potwierdzenie(salon_slug: str):
    salon = wczytaj_salon_bezposrednio(salon_slug)
    if not salon:
        dane = wczytaj_dane()
        return render_template("404.html", sciezka=request.path, domyslny_slug=domyslny_slug(dane)), 404

    rezerwacja = znajdz_rezerwacje(salon, request.args.get("id", ""))
    if not rezerwacja:
        flash("Nie znaleziono rezerwacji.", "error")
        return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug))
    dzien = klucz_dnia_tygodnia(rezerwacja["data"])
    return render_template(
        "rezerwacja_potwierdzenie.html",
        dane=salon,
        salon_slug=salon_slug,
        rezerwacja=rezerwacja,
        dzien_nazwa=dict(DNI_TYGODNIA)[dzien],
        **kontekst_lokalizacji_salonu(salon),
        **kontekst_platnosci_wizyty(salon, rezerwacja),
    )


@app.route("/rezerwacja/<salon_slug>/opinia/<token>", methods=["GET", "POST"])
def opinia_klienta(salon_slug: str, token: str):
    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        include_clients=False,
        include_reservations=True,
        include_free_slots=False,
        include_waitlist=False,
    )
    if not salon:
        dane = wczytaj_dane()
        return render_template("404.html", sciezka=request.path, domyslny_slug=domyslny_slug(dane)), 404

    rezerwacja = znajdz_rezerwacje_po_tokenie_opinii(salon, token)
    if not rezerwacja:
        flash("Nie znaleziono linku do opinii.", "error")
        return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug))

    if rezerwacja.get("status") in {"anulowana", "odrzucona"}:
        flash("Nie można wystawić opinii do anulowanej wizyty.", "error")
        return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug))

    if rezerwacja.get("opinia_id"):
        flash("Opinia dla tej wizyty została już dodana. Dziękujemy!", "success")
        return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug))

    if request.method == "POST":
        ocena = request.form.get("ocena", "").strip()
        komentarz = request.form.get("komentarz", "").strip()
        if ocena not in {"1", "2", "3", "4", "5"}:
            flash("Wybierz ocenę od 1 do 5.", "error")
            return redirect(url_for("opinia_klienta", salon_slug=salon_slug, token=token))
        if len(komentarz) > 600:
            flash("Komentarz może mieć maksymalnie 600 znaków.", "error")
            return redirect(url_for("opinia_klienta", salon_slug=salon_slug, token=token))

        def dodaj_opinie_atomowo(salon_atomowy: dict | None):
            if not salon_atomowy:
                return "Nie znaleziono firmy.", "error"
            rezerwacja_atomowa = znajdz_rezerwacje_po_tokenie_opinii(salon_atomowy, token)
            if not rezerwacja_atomowa:
                return "Nie znaleziono linku do opinii.", "error"
            if rezerwacja_atomowa.get("status") in {"anulowana", "odrzucona"}:
                return "Nie można wystawić opinii do anulowanej wizyty.", "error"
            if rezerwacja_atomowa.get("opinia_id"):
                return "Opinia dla tej wizyty została już dodana. Dziękujemy!", "success"

            opinia_id = uuid.uuid4().hex[:12]
            opinia = {
                "id": opinia_id,
                "rezerwacja_id": rezerwacja_atomowa["id"],
                "ocena": int(ocena),
                "komentarz": komentarz,
                "imie": rezerwacja_atomowa.get("imie", ""),
                "pracownik": rezerwacja_atomowa.get("pracownik", ""),
                "data_wizyty": rezerwacja_atomowa.get("data", ""),
                "widoczna": True,
                "utworzono": datetime.now().isoformat(timespec="minutes"),
            }
            salon_atomowy.setdefault("opinie", []).append(opinia)
            rezerwacja_atomowa["opinia_id"] = opinia_id
            return "Dziękujemy za opinię!", "success"

        komunikat, kategoria = aktualizuj_salon_atomowo(salon_slug, dodaj_opinie_atomowo)
        flash(komunikat, kategoria)
        return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug))

    dzien = klucz_dnia_tygodnia(rezerwacja["data"])
    return render_template(
        "opinia_form.html",
        dane=salon,
        salon_slug=salon_slug,
        rezerwacja=rezerwacja,
        dzien_nazwa=dict(DNI_TYGODNIA)[dzien],
    )


@app.route("/rezerwacja/<salon_slug>/anuluj/<token>", methods=["GET", "POST"])
def anuluj_rezerwacje_klienta(salon_slug: str, token: str):
    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        include_clients=False,
        include_reservations=True,
        include_free_slots=False,
        include_waitlist=False,
    )
    if not salon:
        dane = wczytaj_dane()
        return render_template("404.html", sciezka=request.path, domyslny_slug=domyslny_slug(dane)), 404

    rezerwacja = znajdz_rezerwacje_po_tokenie(salon, token)
    if not rezerwacja:
        flash("Nie znaleziono rezerwacji do anulowania.", "error")
        return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug))

    if request.method == "POST":
        def anuluj_atomowo(salon_atomowy: dict | None):
            if not salon_atomowy:
                return "Nie znaleziono firmy.", "error"
            rezerwacja_atomowa = znajdz_rezerwacje_po_tokenie(salon_atomowy, token)
            if not rezerwacja_atomowa:
                return "Nie znaleziono rezerwacji do anulowania.", "error"
            if rezerwacja_atomowa.get("status") not in {"anulowana", "odrzucona"}:
                rezerwacja_atomowa["status"] = "anulowana"
                rezerwacja_atomowa["anulowano"] = datetime.now().isoformat(timespec="minutes")
                rezerwacja_atomowa["anulowal"] = "klient"
                przywroc_wolny_termin(salon_atomowy, rezerwacja_atomowa)
                usun_rezerwacje_z_salonu(salon_atomowy, rezerwacja_atomowa["id"])
                return "Rezerwacja została anulowana.", "success"
            return "Ta rezerwacja jest już anulowana albo odrzucona.", "success"

        komunikat, kategoria = aktualizuj_salon_atomowo(salon_slug, anuluj_atomowo)
        flash(komunikat, kategoria)
        return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug))

    dzien = klucz_dnia_tygodnia(rezerwacja["data"])
    return render_template(
        "anuluj_rezerwacje.html",
        dane=salon,
        salon_slug=salon_slug,
        rezerwacja=rezerwacja,
        dzien_nazwa=dict(DNI_TYGODNIA)[dzien],
    )


@app.route("/panel/<salon_slug>/podglad")
def podglad_klienta(salon_slug: str):
    return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug, **request.args))


@app.route("/panel/<salon_slug>/terminarz", methods=["GET", "POST"])
def panel_terminarz(salon_slug: str):
    wybrana_data = request.values.get("data") or date.today().isoformat()
    if not waliduj_date_iso(wybrana_data):
        wybrana_data = date.today().isoformat()

    if request.method == "POST" and request.form.get("akcja") == "dodaj_wizyte":
        wizyta_data = request.form.get("wizyta_data", wybrana_data).strip()

        def dodaj_wizyte_atomowo(salon_atomowy: dict | None):
            if not salon_atomowy:
                return None, "Nie znaleziono takiego salonu."
            return utworz_rezerwacje(
                salon_atomowy,
                data_iso=wizyta_data,
                godzina=request.form.get("wizyta_godzina", "").strip(),
                imie=request.form.get("wizyta_imie", "").strip(),
                telefon=request.form.get("wizyta_telefon", "").strip(),
                email=request.form.get("wizyta_email", "").strip().lower(),
                uwagi=request.form.get("wizyta_uwagi", "").strip(),
                pracownik=request.form.get("wizyta_pracownik", "").strip(),
                usluga_nazwa=request.form.get("wizyta_usluga", "").strip(),
                status=request.form.get("wizyta_status", "potwierdzona"),
                zrodlo="salon",
                salon_wymusza=True,
            )

        rezerwacja, blad = aktualizuj_salon_atomowo(salon_slug, dodaj_wizyte_atomowo)
        if blad:
            flash(blad, "error")
        else:
            flash(f"Dodano wizytę: {rezerwacja['imie']} — {rezerwacja['data']} o {rezerwacja['godzina']}.", "success")
            wybrana_data = rezerwacja["data"]
        return redirect(url_for("panel_terminarz", salon_slug=salon_slug, data=wybrana_data))

    data_od, data_do = zakres_miesiaca(wybrana_data)
    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        data_od=data_od,
        data_do=data_do,
        include_clients=False,
    )
    if not salon:
        flash("Nie znaleziono takiego salonu.", "error")
        return redirect(url_for("panel_lista"))

    data_wybrana = datetime.strptime(wybrana_data, "%Y-%m-%d").date()
    pierwszy_dzien_miesiaca = data_wybrana.replace(day=1)
    liczba_dni = calendar.monthrange(data_wybrana.year, data_wybrana.month)[1]
    dni_kalendarza = []
    for przesuniecie in range(liczba_dni):
        data_iso = (pierwszy_dzien_miesiaca + timedelta(days=przesuniecie)).isoformat()
        dni_kalendarza.append(
            {
                "data": data_iso,
                "dzien": dict(DNI_TYGODNIA)[klucz_dnia_tygodnia(data_iso)],
                "numer": int(data_iso[8:10]),
                "rezerwacje": rezerwacje_dnia(salon, data_iso),
                "wolne": dostepne_terminy(salon, data_iso),
                "zamkniety": harmonogram_dnia(salon, data_iso).get("zamkniety", False),
                "otwarcie": harmonogram_dnia(salon, data_iso).get("otwarcie", ""),
                "zamkniecie": harmonogram_dnia(salon, data_iso).get("zamkniecie", ""),
                "aktywny": data_iso == wybrana_data,
            }
        )

    poprzedni_miesiac_data = (pierwszy_dzien_miesiaca - timedelta(days=1)).replace(day=1)
    if data_wybrana.month == 12:
        nastepny_miesiac_data = data_wybrana.replace(year=data_wybrana.year + 1, month=1, day=1)
    else:
        nastepny_miesiac_data = data_wybrana.replace(month=data_wybrana.month + 1, day=1)

    dzien = next((d for d in dni_kalendarza if d["data"] == wybrana_data), None)
    if not dzien:
        wybrana_data = dni_kalendarza[0]["data"]
        for item in dni_kalendarza:
            item["aktywny"] = item["data"] == wybrana_data
        dzien = dni_kalendarza[0]
    return render_template(
        "terminarz.html",
        dane=salon,
        salon_slug=salon_slug,
        wybrana_data=wybrana_data,
        dni_kalendarza=dni_kalendarza,
        puste_przed=pierwszy_dzien_miesiaca.weekday(),
        miesiac_label=f"{MIESIACE[data_wybrana.month]} {data_wybrana.year}",
        dzien_szczegoly=dzien,
        poprzedni_miesiac=poprzedni_miesiac_data.isoformat(),
        nastepny_miesiac=nastepny_miesiac_data.isoformat(),
        uslugi=uslugi_salonu(salon),
        pracownicy=aktywni_pracownicy(salon),
        interwal=interwal_terminow_salonu(salon),
    )


@app.route("/panel/<salon_slug>/lista-rezerwowa", methods=["GET", "POST"])
def panel_lista_rezerwowa(salon_slug: str):
    if request.method == "POST":
        zgloszenie_id = request.form.get("id", "")
        akcja = request.form.get("akcja", "")

        def zmien_liste_rezerwowa_atomowo(salon_atomowy: dict | None):
            if not salon_atomowy:
                return "Nie znaleziono takiej firmy.", "error"
            zgloszenie = next(
                (z for z in salon_atomowy.get("lista_rezerwowa", []) if z.get("id") == zgloszenie_id),
                None,
            )
            if not zgloszenie:
                return "Nie znaleziono zgłoszenia z listy rezerwowej.", "error"
            if akcja == "kontakt":
                zgloszenie["status"] = "kontakt"
                zgloszenie["kontakt_at"] = datetime.now().isoformat(timespec="minutes")
                return f"Oznaczono kontakt: {zgloszenie.get('imie', '')}.", "success"
            if akcja == "nowe":
                zgloszenie["status"] = "nowe"
                zgloszenie.pop("kontakt_at", None)
                return f"Przywrócono jako nowe: {zgloszenie.get('imie', '')}.", "success"
            if akcja == "usun":
                zgloszenie["status"] = "usuniete"
                zgloszenie["usunieto_at"] = datetime.now().isoformat(timespec="minutes")
                return "Usunięto zgłoszenie z listy rezerwowej.", "success"
            return "Nieznana akcja listy rezerwowej.", "error"

        komunikat, kategoria = aktualizuj_salon_atomowo(salon_slug, zmien_liste_rezerwowa_atomowo)
        flash(komunikat, kategoria)
        return redirect(url_for("panel_lista_rezerwowa", salon_slug=salon_slug))

    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        include_clients=False,
        include_reservations=False,
        include_free_slots=False,
        include_waitlist=True,
    )
    if not salon:
        flash("Nie znaleziono takiej firmy.", "error")
        return redirect(url_for("panel_lista"))

    zgloszenia = aktywne_zgloszenia_listy_rezerwowej(salon)
    return render_template(
        "lista_rezerwowa.html",
        dane=salon,
        salon_slug=salon_slug,
        zgloszenia=zgloszenia,
    )


@app.route("/panel/<salon_slug>/rezerwacje", methods=["GET", "POST"])
def panel_rezerwacje(salon_slug: str):
    if request.method == "POST":
        akcja = request.form.get("akcja")
        rezerwacja_id = request.form.get("id", "")

        def zmien_rezerwacje_atomowo(salon_atomowy: dict | None):
            if not salon_atomowy:
                return "Nie znaleziono takiego salonu.", "error"
            rezerwacja = znajdz_rezerwacje(salon_atomowy, rezerwacja_id)
            if not rezerwacja:
                return "Nie znaleziono rezerwacji.", "error"

            if akcja == "oznacz_oplacone":
                rezerwacja["oplacona_recznie"] = True
                rezerwacja["oplacono_recznie_at"] = datetime.now().isoformat(timespec="minutes")
                return f"Oznaczono jako opłacone: {rezerwacja['imie']}.", "success"
            if akcja == "cofnij_oplacone":
                rezerwacja.pop("oplacona_recznie", None)
                rezerwacja.pop("oplacono_recznie_at", None)
                return f"Cofnięto oznaczenie opłaty: {rezerwacja['imie']}.", "success"
            if akcja == "potwierdz":
                rezerwacja["status"] = "potwierdzona"
                rezerwacja["potwierdzono"] = datetime.now().isoformat(timespec="minutes")
                return f"Potwierdzono rezerwację: {rezerwacja['imie']}.", "success"
            if akcja == "odrzuc":
                imie_klienta = rezerwacja["imie"]
                przywroc_wolny_termin(salon_atomowy, rezerwacja)
                usun_rezerwacje_z_salonu(salon_atomowy, rezerwacja_id)
                return f"Odrzucono rezerwację: {imie_klienta}. Termin wrócił do wolnych.", "success"
            if akcja == "anuluj":
                imie_klienta = rezerwacja["imie"]
                przywroc_wolny_termin(salon_atomowy, rezerwacja)
                usun_rezerwacje_z_salonu(salon_atomowy, rezerwacja_id)
                return f"Anulowano rezerwację: {imie_klienta}. Termin wrócił do wolnych.", "success"
            return "Nieznana akcja rezerwacji.", "error"

        komunikat, kategoria = aktualizuj_salon_atomowo(salon_slug, zmien_rezerwacje_atomowo)
        flash(komunikat, kategoria)
        widok = request.args.get("widok", "nadchodzace")
        return redirect(url_for("panel_rezerwacje", salon_slug=salon_slug, widok=widok))

    widok = request.args.get("widok", "nadchodzace")
    if widok not in {"nadchodzace", "archiwum"}:
        widok = "nadchodzace"

    data_od, data_do = zakres_panelu_rezerwacji(widok)
    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        data_od=data_od,
        data_do=data_do,
        include_clients=False,
    )
    if not salon:
        flash("Nie znaleziono takiego salonu.", "error")
        return redirect(url_for("panel_lista"))

    rezerwacje = sorted(
        salon.get("rezerwacje", []),
        key=lambda r: (r.get("data", ""), r.get("godzina", "")),
    )
    nadchodzace = [r for r in rezerwacje if rezerwacja_aktywna_w_panelu(r)]
    archiwum = sorted(
        [
            r
            for r in rezerwacje
            if rezerwacja_w_archiwum(r)
            and r.get("status") not in {"anulowana", "odrzucona"}
        ],
        key=lambda r: (r.get("zarchiwizowano_at", ""), r.get("data", ""), r.get("godzina", "")),
        reverse=True,
    )

    return render_template(
        "rezerwacje.html",
        dane=salon,
        salon_slug=salon_slug,
        widok=widok,
        nadchodzace=nadchodzace,
        archiwum=archiwum[:200],
        dni_tygodnia=dict(DNI_TYGODNIA),
        dni_archiwum=DNI_W_ARCHIWUM_PRZED_USUNIECIEM,
        tryb_platnosci_wizyty=tryb_platnosci_wizyty_salonu(salon),
    )


@app.route("/panel/<salon_slug>/opinie", methods=["GET", "POST"])
def panel_opinie(salon_slug: str):
    if request.method == "POST":
        opinia_id = request.form.get("id", "")
        akcja = request.form.get("akcja", "")

        def zmien_opinie_atomowo(salon_atomowy: dict | None):
            if not salon_atomowy:
                return "Nie znaleziono takiego salonu.", "error"
            opinia = next((o for o in salon_atomowy.get("opinie", []) if o.get("id") == opinia_id), None)
            if not opinia:
                return "Nie znaleziono opinii.", "error"
            if akcja == "ukryj":
                opinia["widoczna"] = False
                return "Opinia została ukryta na stronie klienta.", "success"
            if akcja == "pokaz":
                opinia["widoczna"] = True
                return "Opinia jest znowu widoczna na stronie klienta.", "success"
            return "Nieznana akcja opinii.", "error"

        komunikat, kategoria = aktualizuj_salon_atomowo(salon_slug, zmien_opinie_atomowo)
        flash(komunikat, kategoria)
        return redirect(url_for("panel_opinie", salon_slug=salon_slug))

    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        include_clients=False,
        include_reservations=False,
        include_free_slots=False,
        include_waitlist=False,
    )
    if not salon:
        flash("Nie znaleziono takiego salonu.", "error")
        return redirect(url_for("panel_lista"))

    opinie = sorted(salon.get("opinie", []), key=lambda o: o.get("utworzono", ""), reverse=True)
    widoczne = widoczne_opinie(salon)
    return render_template(
        "opinie.html",
        dane=salon,
        salon_slug=salon_slug,
        opinie=opinie,
        srednia_ocena=srednia_ocen(widoczne),
        liczba_widocznych=len(widoczne),
    )


@app.route("/panel/<salon_slug>/klienci")
def panel_klienci(salon_slug: str):
    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        include_clients=True,
        include_reservations=False,
        include_free_slots=False,
        include_waitlist=False,
    )
    if not salon:
        flash("Nie znaleziono takiego salonu.", "error")
        return redirect(url_for("panel_lista"))

    fraza = request.args.get("q", "").strip()
    klienci = wyszukaj_klientow(salon, fraza)

    return render_template(
        "klienci.html",
        dane=salon,
        salon_slug=salon_slug,
        klienci=klienci,
        fraza=fraza,
        liczba_klientow=len(salon.get("klienci", [])),
    )


@app.route("/panel/<salon_slug>/klienci/<klient_id>", methods=["GET", "POST"])
def panel_klient_szczegoly(salon_slug: str, klient_id: str):
    admin_rodo_ograniczony = bool(session.get(admin_auth_key()) and not session.get(panel_auth_key(salon_slug)))

    if request.method == "POST":
        akcja = request.form.get("akcja", "zapisz")
        if akcja == "zapisz":
            def zapisz_klienta_atomowo(salon_atomowy: dict | None):
                if not salon_atomowy:
                    return "Nie znaleziono takiego salonu.", "error"
                synchronizuj_kartoteke_salonu(salon_atomowy)
                klient_atomowy = znajdz_klienta(salon_atomowy, klient_id)
                if not klient_atomowy:
                    return "Nie znaleziono klienta w kartotece.", "error"
                klient_atomowy["notatka_wewnetrzna"] = request.form.get("notatka_wewnetrzna", "").strip()[:2000]
                klient_atomowy["email"] = request.form.get("email", "").strip()[:120]
                if not admin_rodo_ograniczony and request.form.get("wywiad_zaakceptowany_salon") == "on":
                    klient_atomowy["wywiad_zdrowotny"] = {
                        "_typ": "oswiadczenie",
                        "zaakceptowano": datetime.now().isoformat(timespec="minutes"),
                        "zgoda_rodo": "tak",
                        "potwierdzil": "salon",
                    }
                    klient_atomowy["wywiad_aktualizacja"] = klient_atomowy["wywiad_zdrowotny"]["zaakceptowano"]
                return "Zapisano kartotekę klienta.", "success"

            komunikat, kategoria = aktualizuj_salon_atomowo(salon_slug, zapisz_klienta_atomowo)
            flash(komunikat, kategoria)
        return redirect(url_for("panel_klient_szczegoly", salon_slug=salon_slug, klient_id=klient_id))

    data_od, data_do = zakres_historii_klienta()
    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        data_od=data_od,
        data_do=data_do,
        include_clients=True,
        include_reservations=True,
        include_free_slots=False,
        include_waitlist=False,
    )
    if not salon:
        flash("Nie znaleziono takiego salonu.", "error")
        return redirect(url_for("panel_lista"))

    klient = znajdz_klienta(salon, klient_id)
    if not klient:
        flash("Nie znaleziono klienta w kartotece.", "error")
        return redirect(url_for("panel_klienci", salon_slug=salon_slug))

    pytania = pytania_wywiadu_salonu(salon)
    wizyty = historia_wizyt_klienta(salon, klient_id)
    wywiad_etykiety = [] if admin_rodo_ograniczony else etykieta_odpowiedzi_wywiadu(pytania, klient.get("wywiad_zdrowotny") or {})
    pytania_map = {p["id"]: p["tresc"] for p in pytania}

    return render_template(
        "klient.html",
        dane=salon,
        salon_slug=salon_slug,
        klient=klient,
        wizyty=wizyty,
        pytania=pytania,
        pytania_map=pytania_map,
        wywiad_etykiety=wywiad_etykiety,
        tresc_wywiadu="" if admin_rodo_ograniczony else tresc_wywiadu_salonu(salon),
        wywiad_oswiadczenie=False if admin_rodo_ograniczony else wywiad_to_oswiadczenie(klient.get("wywiad_zdrowotny")),
        admin_rodo_ograniczony=admin_rodo_ograniczony,
        dni_tygodnia=dict(DNI_TYGODNIA),
    )


@app.route("/panel/<salon_slug>/wywiad", methods=["GET", "POST"])
def panel_wywiad(salon_slug: str):
    if request.method == "POST":
        wywiad_wlaczony = request.form.get("wywiad_wlaczony") == "on"
        wywiad_przy_rezerwacji = request.form.get("wywiad_przy_rezerwacji") == "on"
        tresc = request.form.get("tresc_wywiadu_zdrowotnego", "").strip()[:12000]

        def zapisz_wywiad_atomowo(salon_atomowy: dict | None):
            if not salon_atomowy:
                return "Nie znaleziono takiego salonu.", "error"
            salon_atomowy["wywiad_wlaczony"] = wywiad_wlaczony
            salon_atomowy["wywiad_przy_rezerwacji"] = wywiad_przy_rezerwacji
            salon_atomowy["tresc_wywiadu_zdrowotnego"] = tresc
            if salon_atomowy["wywiad_przy_rezerwacji"] and not tresc_wywiadu_salonu(salon_atomowy):
                return "Zapisano. Dodaj treść oświadczenia, aby pojawiło się przy rezerwacji.", "error"
            return "Ustawienia wywiadu zdrowotnego zapisane.", "success"

        komunikat, kategoria = aktualizuj_salon_atomowo(salon_slug, zapisz_wywiad_atomowo)
        flash(komunikat, kategoria)
        return redirect(url_for("panel_wywiad", salon_slug=salon_slug))

    salon = wczytaj_salon_bezposrednio(
        salon_slug,
        include_clients=False,
        include_reservations=False,
        include_free_slots=False,
        include_waitlist=False,
    )
    if not salon:
        flash("Nie znaleziono takiego salonu.", "error")
        return redirect(url_for("panel_lista"))

    return render_template(
        "wywiad.html",
        dane=salon,
        salon_slug=salon_slug,
        tresc_wywiadu=tresc_wywiadu_salonu(salon),
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(debug=debug, host="0.0.0.0", port=port)
