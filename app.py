"""Glovaro — panel rezerwacji dla wielu salonów/fryzjerów."""

from __future__ import annotations

import copy
import base64
import hashlib
import hmac
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
from urllib.parse import urlencode, urlparse

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 8 * 1024 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))

from storage import init_storage, tryb_magazynu, wczytaj_raw, zapisz_raw

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
LEGAL_COMPANY_NAME = os.environ.get("LEGAL_COMPANY_NAME", "Glovaro").strip()
LEGAL_COMPANY_ADDRESS = os.environ.get("LEGAL_COMPANY_ADDRESS", "Uzupełnij adres firmy").strip()
LEGAL_COMPANY_EMAIL = os.environ.get("LEGAL_COMPANY_EMAIL", "kontakt@example.com").strip()
LEGAL_COMPANY_NIP = os.environ.get("LEGAL_COMPANY_NIP", "Uzupełnij NIP").strip()
REZERWACJA_RATE_LIMIT: dict[str, list[float]] = {}

DNI_TYGODNIA = [
    ("poniedzialek", "Poniedziałek"),
    ("wtorek", "Wtorek"),
    ("sroda", "Środa"),
    ("czwartek", "Czwartek"),
    ("piatek", "Piątek"),
    ("sobota", "Sobota"),
    ("niedziela", "Niedziela"),
]

DEFAULT_SALON = {
    "nazwa_salonu": "Mój Salon",
    "haslo_panelu": "",
    "opis": "",
    "telefon_kontaktowy": "",
    "instagram": "",
    "email_powiadomien": "",
    "zdjecia_prac": [],
    "pracownicy": [],
    "abonament_status": "trial",
    "oplata_miesieczna": 100,
    "oplacone_do": "",
    "notatka_rozliczeniowa": "",
    "godziny_pracy": {
        key: {"otwarcie": "09:00", "zamkniecie": "18:00", "zamkniety": key == "niedziela"}
        for key, _ in DNI_TYGODNIA
    },
    "wolne_terminy": {},
    "blokady": [],
    "rezerwacje": [],
    "opinie": [],
}

PUBLIC_ENDPOINTS = {
    "strona_glowna",
    "health",
    "rezerwacja_domyslna",
    "rezerwacja_publiczna",
    "rezerwacja_formularz",
    "rezerwacja_potwierdzenie",
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
    "anuluj_rezerwacje_klienta",
    "opinia_klienta",
}

MOTYW_ROZOWY_ENDPOINTS = WIDOK_KLIENTA_ENDPOINTS | {
    "strona_glowna",
    "regulamin",
    "polityka_prywatnosci",
    "polityka_cookies",
}


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


def nowy_salon(nazwa: str = "Mój Salon", haslo: str = "") -> dict:
    salon = copy.deepcopy(DEFAULT_SALON)
    salon["nazwa_salonu"] = nazwa
    salon["haslo_panelu"] = haslo
    return salon


def migracja_danych(dane: dict) -> dict:
    if "salony" in dane:
        for slug, salon in dane["salony"].items():
            salon.setdefault("slug", slug)
            salon.setdefault("haslo_panelu", "")
            salon.setdefault("opis", "")
            salon.setdefault("telefon_kontaktowy", "")
            salon.setdefault("instagram", "")
            salon.setdefault("email_powiadomien", "")
            salon.setdefault("zdjecia_prac", [])
            salon.setdefault("pracownicy", [])
            salon.setdefault("abonament_status", "trial")
            salon.setdefault("oplata_miesieczna", 100)
            salon.setdefault("oplacone_do", "")
            salon.setdefault("notatka_rozliczeniowa", "")
            salon.setdefault("godziny_pracy", copy.deepcopy(DEFAULT_SALON["godziny_pracy"]))
            salon.setdefault("wolne_terminy", {})
            salon.setdefault("blokady", [])
            salon.setdefault("rezerwacje", [])
            salon.setdefault("opinie", [])
            for rezerwacja in salon["rezerwacje"]:
                rezerwacja.setdefault("status", "potwierdzona")
                rezerwacja.setdefault("token_anulowania", uuid.uuid4().hex)
                rezerwacja.setdefault("token_opinii", uuid.uuid4().hex)
                rezerwacja.setdefault("pracownik", "")
        return dane

    # Stary format jednej strony zamieniamy na salon "demo", żeby nie stracić danych.
    salon = nowy_salon(dane.get("nazwa_salonu", "Mój Salon"), PANEL_PASSWORD)
    salon["godziny_pracy"] = dane.get("godziny_pracy", salon["godziny_pracy"])
    salon["wolne_terminy"] = dane.get("wolne_terminy", {})
    salon["rezerwacje"] = dane.get("rezerwacje", [])
    salon["opinie"] = dane.get("opinie", [])
    for rezerwacja in salon["rezerwacje"]:
        rezerwacja.setdefault("status", "potwierdzona")
        rezerwacja.setdefault("token_anulowania", uuid.uuid4().hex)
        rezerwacja.setdefault("token_opinii", uuid.uuid4().hex)
        rezerwacja.setdefault("pracownik", "")
    salon["slug"] = "demo"
    return {"salony": {"demo": salon}}


def wczytaj_dane() -> dict:
    dane = wczytaj_raw()
    if dane is None:
        dane = {"salony": {"demo": {**nowy_salon("Mój Salon", PANEL_PASSWORD), "slug": "demo"}}}
        zapisz_dane(dane)
        return dane

    zmigrowane = migracja_danych(dane)
    if zmigrowane != dane:
        zapisz_dane(zmigrowane)
    return zmigrowane


def zapisz_dane(dane: dict) -> None:
    zapisz_raw(dane)


def pobierz_salon(dane: dict, salon_slug: str) -> dict | None:
    salon = dane.get("salony", {}).get(salon_slug)
    if salon:
        salon.setdefault("slug", salon_slug)
    return salon


def salon_wstrzymany(salon: dict) -> bool:
    return salon.get("abonament_status") == "suspended"


def abonament_po_terminie(salon: dict) -> bool:
    oplacone_do = salon.get("oplacone_do", "")
    return bool(oplacone_do and oplacone_do < date.today().isoformat())


def stripe_skonfigurowany() -> bool:
    return bool(STRIPE_SECRET_KEY)


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


def aktywne_rezerwacje_slotu(salon: dict, data_iso: str, godzina: str) -> list[dict]:
    return [
        r
        for r in salon.get("rezerwacje", [])
        if r.get("data") == data_iso
        and r.get("godzina") == godzina
        and r.get("status", "potwierdzona") not in {"anulowana", "odrzucona"}
    ]


def pracownik_zajety(salon: dict, data_iso: str, godzina: str, pracownik: str) -> bool:
    return any(r.get("pracownik") == pracownik for r in aktywne_rezerwacje_slotu(salon, data_iso, godzina))


def slot_w_pelni_zajety(salon: dict, data_iso: str, godzina: str) -> bool:
    pracownicy = aktywni_pracownicy(salon)
    aktywne = aktywne_rezerwacje_slotu(salon, data_iso, godzina)
    if not pracownicy:
        return bool(aktywne)
    zajeci_pracownicy = {r.get("pracownik") for r in aktywne if r.get("pracownik")}
    bez_pracownika = any(not r.get("pracownik") for r in aktywne)
    return bez_pracownika or len(zajeci_pracownicy) >= len(pracownicy)


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


def godzina_zablokowana(salon: dict, data_iso: str, godzina: str) -> bool:
    minuta = czas_na_minuty(godzina)
    for blokada in blokady_dnia(salon, data_iso):
        caly_dzien = blokada.get("caly_dzien", False)
        if caly_dzien:
            return True
        start = blokada.get("od_godziny") or "00:00"
        koniec = blokada.get("do_godziny") or "23:59"
        if zakresy_nachodza(minuta, minuta + 1, czas_na_minuty(start), czas_na_minuty(koniec)):
            return True
    return False


def dostepne_terminy(salon: dict, data_iso: str) -> list[str]:
    wolne = salon.get("wolne_terminy", {}).get(data_iso, [])
    return sorted(
        g
        for g in wolne
        if not slot_w_pelni_zajety(salon, data_iso, g)
        and not godzina_zablokowana(salon, data_iso, g)
    )


def normalizuj_telefon(telefon: str) -> str:
    return re.sub(r"\D", "", telefon)


def waliduj_telefon(telefon: str) -> bool:
    cyfry = normalizuj_telefon(telefon)
    return 9 <= len(cyfry) <= 15


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


def parsuj_upload_zdjec(pliki) -> list[str]:
    """Zapisuje małe zdjęcia jako data URL w JSON, bez osobnego hostingu plików."""
    zdjecia = []
    dozwolone_typy = {"image/jpeg", "image/png", "image/webp", "image/gif"}
    maks_bajtow = 1_500_000

    for plik in pliki:
        if not plik or not plik.filename:
            continue
        if plik.mimetype not in dozwolone_typy:
            continue
        dane = plik.read()
        if not dane or len(dane) > maks_bajtow:
            continue
        zakodowane = base64.b64encode(dane).decode("ascii")
        zdjecia.append(f"data:{plik.mimetype};base64,{zakodowane}")
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
    tresc = f"""Nowa rezerwacja w Glovaro

Salon: {salon['nazwa_salonu']}
Termin: {rezerwacja['data']} o {rezerwacja['godzina']}
Klient: {rezerwacja['imie']}
Telefon: {rezerwacja['telefon']}
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
        app.logger.warning("Nie udało się wysłać e-maila z rezerwacją: %s", exc)
        return False


def wyslij_email_przypomnienie(salon: dict, rezerwacja: dict, salon_slug: str) -> bool:
    odbiorca = salon.get("email_powiadomien", "").strip()
    if not odbiorca or not email_skonfigurowany():
        return False

    link_panelu = url_for("panel_rezerwacje", salon_slug=salon_slug, _external=True)
    temat = f"Przypomnienie: wizyta jutro o {rezerwacja['godzina']}"
    tresc = f"""Przypomnienie o nadchodzącej wizycie

Salon: {salon['nazwa_salonu']}
Termin: {rezerwacja['data']} o {rezerwacja['godzina']}
Klient: {rezerwacja['imie']}
Telefon: {rezerwacja['telefon']}
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


def admin_auth_key() -> str:
    return "admin_auth"


def bezpieczny_next_url(url: str | None) -> str:
    if not url:
        return url_for("panel_lista")
    parsed = urlparse(url)
    if parsed.netloc or not url.startswith("/") or url.startswith("//"):
        return url_for("panel_lista")
    return url


def haslo_panelu(salon: dict) -> str:
    return (salon.get("haslo_panelu") or PANEL_PASSWORD or "").strip()


def zalogowany_do_salonu(salon_slug: str) -> bool:
    return bool(session.get(panel_auth_key(salon_slug)) or session.get(admin_auth_key()))


@app.before_request
def wymagaj_hasla_panelu():
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
        return redirect(url_for("panel_login", salon=salon_slug, next=request.path))


@app.after_request
def dodaj_naglowki_bezpieczenstwa(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


@app.context_processor
def inject_globals():
    salon_slug = request.view_args.get("salon_slug") if request.view_args else None
    return {
        "dni_tygodnia": DNI_TYGODNIA,
        "panel_chroniony_haslem": bool(PANEL_PASSWORD),
        "zalogowany_do_panelu": bool(salon_slug and zalogowany_do_salonu(salon_slug)),
        "widok_klienta": request.endpoint in WIDOK_KLIENTA_ENDPOINTS,
        "motyw_rozowy": request.endpoint in MOTYW_ROZOWY_ENDPOINTS,
        "aktywny_salon_slug": salon_slug,
        "stripe_skonfigurowany": stripe_skonfigurowany(),
        "legal": {
            "company_name": LEGAL_COMPANY_NAME,
            "company_address": LEGAL_COMPANY_ADDRESS,
            "company_email": LEGAL_COMPANY_EMAIL,
            "company_nip": LEGAL_COMPANY_NIP,
        },
    }


@app.route("/panel/login", methods=["GET", "POST"])
def panel_login():
    dane = wczytaj_dane()
    salon_slug = request.args.get("salon") or request.form.get("salon") or ""
    salon = pobierz_salon(dane, salon_slug) if salon_slug else None
    wymagane_haslo = haslo_panelu(salon) if salon else PANEL_PASSWORD

    if not wymagane_haslo:
        return redirect(url_for("panel", salon_slug=salon_slug) if salon else url_for("panel_lista"))

    if request.method == "POST":
        haslo = request.form.get("haslo", "")
        if haslo == wymagane_haslo:
            if salon:
                session[panel_auth_key(salon_slug)] = True
            else:
                session[admin_auth_key()] = True
            flash("Zalogowano do panelu.", "success")
            return redirect(bezpieczny_next_url(request.form.get("next") or request.args.get("next")))
        flash("Nieprawidłowe hasło.", "error")

    return render_template("login.html", salon=salon, salon_slug=salon_slug)


@app.route("/panel/wyloguj")
@app.route("/panel/<salon_slug>/wyloguj")
def panel_wyloguj(salon_slug: str | None = None):
    if salon_slug:
        session.pop(panel_auth_key(salon_slug), None)
    else:
        session.clear()
    flash("Wylogowano z panelu.", "success")
    return redirect(url_for("strona_glowna"))


@app.route("/health")
def health():
    return jsonify({"status": "ok", "app": "Glovaro", "storage": tryb_magazynu()}), 200


@app.route("/tasks/send-reminders")
def wyslij_przypomnienia():
    if not REMINDER_SECRET or request.args.get("secret") != REMINDER_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    dane = wczytaj_dane()
    teraz = datetime.now()
    okno_do = teraz + timedelta(hours=26)
    wyslane = 0

    for salon_slug, salon in dane.get("salony", {}).items():
        for rezerwacja in salon.get("rezerwacje", []):
            if rezerwacja.get("przypomnienie_wyslane"):
                continue
            if rezerwacja.get("status", "potwierdzona") != "potwierdzona":
                continue
            try:
                termin = datetime.strptime(
                    f"{rezerwacja.get('data')} {rezerwacja.get('godzina')}", "%Y-%m-%d %H:%M"
                )
            except (TypeError, ValueError):
                continue
            if teraz <= termin <= okno_do and wyslij_email_przypomnienie(salon, rezerwacja, salon_slug):
                rezerwacja["przypomnienie_wyslane"] = datetime.now().isoformat(timespec="minutes")
                wyslane += 1

    if wyslane:
        zapisz_dane(dane)
    return jsonify({"sent": wyslane})


@app.route("/")
def strona_glowna():
    dane = wczytaj_dane()
    return render_template("index.html", salony=dane.get("salony", {}))


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
    slug = slugify(request.form.get("slug", "").strip() or nazwa)

    if not nazwa:
        flash("Podaj nazwę salonu.", "error")
        return redirect(url_for("panel_lista"))
    if slug in dane.get("salony", {}):
        flash("Taki link już istnieje. Wybierz inną nazwę.", "error")
        return redirect(url_for("panel_lista"))

    salon = nowy_salon(nazwa, haslo)
    salon["slug"] = slug
    dane.setdefault("salony", {})[slug] = salon
    zapisz_dane(dane)
    flash(f"Dodano salon: {nazwa}.", "success")
    return redirect(url_for("panel", salon_slug=slug))


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
    if status not in {"trial", "active", "suspended"}:
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
    dane = wczytaj_dane()
    salon = pobierz_salon(dane, salon_slug)
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

    if event_type in {"checkout.session.completed", "invoice.paid"} and salon_slug:
        dane = wczytaj_dane()
        salon = pobierz_salon(dane, salon_slug)
        if salon:
            przedluz_abonament(salon)
            zapisz_dane(dane)
            app.logger.info("Stripe potwierdził płatność dla salonu %s", salon_slug)

    return jsonify({"received": True})


@app.route("/panel/<salon_slug>")
def panel(salon_slug: str):
    dane = wczytaj_dane()
    salon = pobierz_salon(dane, salon_slug)
    if not salon:
        flash("Nie znaleziono takiego salonu.", "error")
        return redirect(url_for("panel_lista"))

    dzisiaj = date.today().isoformat()
    terminy_dzis = dostepne_terminy(salon, dzisiaj)
    rezerwacje = sorted(
        salon.get("rezerwacje", []),
        key=lambda r: (r.get("data", ""), r.get("godzina", "")),
    )
    nadchodzace = [r for r in rezerwacje if r.get("data", "") >= dzisiaj]
    klienci = {normalizuj_telefon(r.get("telefon", "")) for r in rezerwacje if r.get("telefon")}
    dni_count: dict[str, int] = {}
    for r in rezerwacje:
        data_rezerwacji = r.get("data", "")
        if waliduj_date_iso(data_rezerwacji):
            dzien = dict(DNI_TYGODNIA)[klucz_dnia_tygodnia(data_rezerwacji)]
            dni_count[dzien] = dni_count.get(dzien, 0) + 1
    najpopularniejszy_dzien = max(dni_count.items(), key=lambda item: item[1])[0] if dni_count else "-"
    za_7_dni = (date.today() + timedelta(days=7)).isoformat()
    rezerwacje_7_dni = [r for r in nadchodzace if r.get("data", "") <= za_7_dni]
    return render_template(
        "panel.html",
        dane=salon,
        salon_slug=salon_slug,
        dzisiaj=dzisiaj,
        terminy_dzis=terminy_dzis,
        liczba_dni_z_terminami=len(salon.get("wolne_terminy", {})),
        liczba_rezerwacji=len(nadchodzace),
        ostatnie_rezerwacje=nadchodzace[:5],
        statystyki={
            "wszystkie_rezerwacje": len(rezerwacje),
            "unikalni_klienci": len([k for k in klienci if k]),
            "najpopularniejszy_dzien": najpopularniejszy_dzien,
            "rezerwacje_7_dni": len(rezerwacje_7_dni),
        },
    )


@app.route("/panel/<salon_slug>/salon", methods=["GET", "POST"])
def ustawienia_salonu(salon_slug: str):
    dane = wczytaj_dane()
    salon = pobierz_salon(dane, salon_slug)
    if not salon:
        flash("Nie znaleziono takiego salonu.", "error")
        return redirect(url_for("panel_lista"))

    if request.method == "POST":
        nazwa = request.form.get("nazwa_salonu", "").strip()
        haslo = request.form.get("haslo_panelu", "").strip()
        opis = request.form.get("opis", "").strip()
        telefon = request.form.get("telefon_kontaktowy", "").strip()
        instagram = request.form.get("instagram", "").strip()
        email_powiadomien = request.form.get("email_powiadomien", "").strip()
        pracownicy = parsuj_pracownikow(request.form.get("pracownicy", ""))
        zdjecia_z_linkow = parsuj_linki_zdjec(request.form.get("zdjecia_prac", ""))
        nowe_zdjecia = parsuj_upload_zdjec(request.files.getlist("zdjecia_upload"))
        dotychczasowe_uploady = [
            z
            for z in salon.get("zdjecia_prac", [])
            if isinstance(z, str) and z.startswith("data:image/")
        ]
        if request.form.get("usun_wgrane_zdjecia") == "on":
            dotychczasowe_uploady = []
        zdjecia = (zdjecia_z_linkow + dotychczasowe_uploady + nowe_zdjecia)[:12]
        if nazwa:
            salon["nazwa_salonu"] = nazwa
            salon["opis"] = opis
            salon["telefon_kontaktowy"] = telefon
            salon["instagram"] = instagram
            salon["email_powiadomien"] = email_powiadomien
            salon["pracownicy"] = pracownicy
            salon["zdjecia_prac"] = zdjecia
            if haslo:
                salon["haslo_panelu"] = haslo
            zapisz_dane(dane)
            flash("Ustawienia salonu zostały zapisane.", "success")
        else:
            flash("Podaj nazwę salonu.", "error")
        return redirect(url_for("ustawienia_salonu", salon_slug=salon_slug))
    return render_template("salon.html", dane=salon, salon_slug=salon_slug)


@app.route("/panel/<salon_slug>/godziny", methods=["GET", "POST"])
def godziny_pracy(salon_slug: str):
    dane = wczytaj_dane()
    salon = pobierz_salon(dane, salon_slug)
    if not salon:
        flash("Nie znaleziono takiego salonu.", "error")
        return redirect(url_for("panel_lista"))

    if request.method == "POST":
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

            salon["godziny_pracy"][klucz] = {
                "otwarcie": otwarcie,
                "zamkniecie": zamkniecie,
                "zamkniety": zamkniety,
            }
        zapisz_dane(dane)
        flash("Godziny pracy zostały zapisane.", "success")
        return redirect(url_for("godziny_pracy", salon_slug=salon_slug))

    return render_template("godziny.html", dane=salon, salon_slug=salon_slug)


@app.route("/panel/<salon_slug>/terminy", methods=["GET", "POST"])
def wolne_terminy(salon_slug: str):
    dane = wczytaj_dane()
    salon = pobierz_salon(dane, salon_slug)
    if not salon:
        flash("Nie znaleziono takiego salonu.", "error")
        return redirect(url_for("panel_lista"))

    wybrana_data = request.args.get("data") or request.form.get("data") or date.today().isoformat()
    if not waliduj_date_iso(wybrana_data):
        wybrana_data = date.today().isoformat()
        flash("Nieprawidłowa data — pokazuję dzisiejszy dzień.", "error")

    if request.method == "POST":
        akcja = request.form.get("akcja")
        godzina = request.form.get("godzina", "").strip()
        salon.setdefault("wolne_terminy", {}).setdefault(wybrana_data, [])
        terminy = salon["wolne_terminy"][wybrana_data]

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
                flash("Uzupełnij poprawnie zakres generowania terminów.", "error")
                return redirect(url_for("wolne_terminy", salon_slug=salon_slug, data=wybrana_data))

            start_min = czas_na_minuty(od_godziny)
            koniec_min = czas_na_minuty(do_godziny)
            krok = int(interwal)
            if start_min >= koniec_min:
                flash("Godzina startu musi być wcześniejsza niż końca.", "error")
                return redirect(url_for("wolne_terminy", salon_slug=salon_slug, data=wybrana_data))

            dodane = 0
            for data_key in daty_w_zakresie(data_od, data_do):
                dzien_key = klucz_dnia_tygodnia(data_key)
                if salon.get("godziny_pracy", {}).get(dzien_key, {}).get("zamkniety"):
                    continue
                if nadpisz:
                    salon["wolne_terminy"][data_key] = []
                salon.setdefault("wolne_terminy", {}).setdefault(data_key, [])
                zajete = zajete_godziny(salon, data_key)
                for minuta in range(start_min, koniec_min, krok):
                    slot = minuty_na_czas(minuta)
                    if (
                        slot not in salon["wolne_terminy"][data_key]
                        and slot not in zajete
                        and not slot_w_pelni_zajety(salon, data_key, slot)
                        and not godzina_zablokowana(salon, data_key, slot)
                    ):
                        salon["wolne_terminy"][data_key].append(slot)
                        dodane += 1
                salon["wolne_terminy"][data_key].sort()
                if not salon["wolne_terminy"][data_key]:
                    salon["wolne_terminy"].pop(data_key, None)

            zapisz_dane(dane)
            flash(f"Wygenerowano {dodane} nowych terminów.", "success")
            return redirect(url_for("wolne_terminy", salon_slug=salon_slug, data=data_od))

        if akcja == "dodaj":
            if not waliduj_godzine(godzina):
                flash("Podaj godzinę w formacie HH:MM (np. 10:30).", "error")
            elif (
                godzina in terminy
                or godzina in zajete_godziny(salon, wybrana_data)
                or slot_w_pelni_zajety(salon, wybrana_data, godzina)
                or godzina_zablokowana(salon, wybrana_data, godzina)
            ):
                flash("Ten termin już istnieje, jest zajęty albo zablokowany.", "error")
            else:
                terminy.append(godzina)
                terminy.sort()
                flash(f"Dodano wolny termin: {godzina}.", "success")

        elif akcja == "usun":
            if godzina in terminy:
                terminy.remove(godzina)
                flash(f"Usunięto termin: {godzina}.", "success")
            if not terminy:
                salon["wolne_terminy"].pop(wybrana_data, None)

        elif akcja == "blokuj":
            data_od = request.form.get("blokada_data_od", wybrana_data)
            data_do = request.form.get("blokada_data_do", data_od)
            powod = request.form.get("powod", "Przerwa").strip() or "Przerwa"
            opis = request.form.get("opis_blokady", "").strip()
            caly_dzien = request.form.get("caly_dzien") == "on"
            od_godziny = request.form.get("blokada_od_godziny", "")
            do_godziny = request.form.get("blokada_do_godziny", "")

            if not waliduj_date_iso(data_od) or not waliduj_date_iso(data_do):
                flash("Podaj poprawny zakres dat blokady.", "error")
                return redirect(url_for("wolne_terminy", salon_slug=salon_slug, data=wybrana_data))
            if not caly_dzien:
                if not waliduj_godzine(od_godziny) or not waliduj_godzine(do_godziny) or od_godziny >= do_godziny:
                    flash("Podaj poprawny zakres godzin blokady.", "error")
                    return redirect(url_for("wolne_terminy", salon_slug=salon_slug, data=wybrana_data))

            salon.setdefault("blokady", []).append(
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
            flash("Dodano blokadę terminów.", "success")

        elif akcja == "usun_blokade":
            blokada_id = request.form.get("blokada_id", "")
            salon["blokady"] = [b for b in salon.get("blokady", []) if b.get("id") != blokada_id]
            flash("Usunięto blokadę.", "success")

        zapisz_dane(dane)
        return redirect(url_for("wolne_terminy", salon_slug=salon_slug, data=wybrana_data))

    return render_template(
        "terminy.html",
        dane=salon,
        salon_slug=salon_slug,
        wybrana_data=wybrana_data,
        terminy=dostepne_terminy(salon, wybrana_data),
        zajete=sorted(zajete_godziny(salon, wybrana_data)),
        blokady=blokady_dnia(salon, wybrana_data),
        wszystkie_terminy=salon.get("wolne_terminy", {}),
    )


def kontekst_rezerwacji(salon: dict, salon_slug: str, wybrana_data: str) -> dict:
    if not waliduj_date_iso(wybrana_data):
        wybrana_data = date.today().isoformat()
    dzien_tygodnia = klucz_dnia_tygodnia(wybrana_data)
    godziny = salon["godziny_pracy"].get(dzien_tygodnia, {})
    opinie = widoczne_opinie(salon)
    return {
        "dane": salon,
        "salon_slug": salon_slug,
        "wybrana_data": wybrana_data,
        "dzien_tygodnia": dzien_tygodnia,
        "godziny": godziny,
        "terminy": dostepne_terminy(salon, wybrana_data),
        "dni_tygodnia": dict(DNI_TYGODNIA),
        "pracownicy": aktywni_pracownicy(salon),
        "opinie": sorted(opinie, key=lambda o: o.get("utworzono", ""), reverse=True)[:6],
        "srednia_ocena": srednia_ocen(opinie),
    }


@app.route("/rezerwacja")
def rezerwacja_domyslna():
    dane = wczytaj_dane()
    return redirect(url_for("rezerwacja_publiczna", salon_slug=domyslny_slug(dane), **request.args))


@app.route("/rezerwacja/<salon_slug>")
def rezerwacja_publiczna(salon_slug: str):
    dane = wczytaj_dane()
    salon = pobierz_salon(dane, salon_slug)
    if not salon:
        return render_template("404.html", sciezka=request.path, domyslny_slug=domyslny_slug(dane)), 404
    if salon_wstrzymany(salon):
        return render_template("abonament_wstrzymany.html", dane=salon), 403
    wybrana_data = request.args.get("data", date.today().isoformat())
    return render_template("podglad.html", **kontekst_rezerwacji(salon, salon_slug, wybrana_data))


@app.route("/rezerwacja/<salon_slug>/nowa", methods=["GET", "POST"])
def rezerwacja_formularz(salon_slug: str):
    dane = wczytaj_dane()
    salon = pobierz_salon(dane, salon_slug)
    if not salon:
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
    uwagi = request.form.get("uwagi", "").strip()
    godzina = request.form.get("godzina", "").strip()
    pracownik = request.form.get("pracownik", "").strip()
    pracownicy = aktywni_pracownicy(salon)

    if przekroczono_limit_rezerwacji(salon_slug):
        flash("Zbyt wiele prób rezerwacji. Spróbuj ponownie za kilka minut.", "error")
        return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug, data=data_iso))
    if pracownicy and pracownik == "__dowolny__":
        pracownik = next((p for p in pracownicy if not pracownik_zajety(salon, data_iso, godzina, p)), "")
    if pracownicy and pracownik not in pracownicy:
        flash("Wybierz pracownika z listy.", "error")
        return redirect(url_for("rezerwacja_formularz", salon_slug=salon_slug, data=data_iso, godzina=godzina))
    if pracownik and pracownik_zajety(salon, data_iso, godzina, pracownik):
        flash("Ten pracownik jest już zajęty o tej godzinie. Wybierz inną osobę albo termin.", "error")
        return redirect(url_for("rezerwacja_formularz", salon_slug=salon_slug, data=data_iso, godzina=godzina))
    if not imie:
        flash("Podaj imię i nazwisko.", "error")
        return redirect(url_for("rezerwacja_formularz", salon_slug=salon_slug, data=data_iso, godzina=godzina))
    if not waliduj_telefon(telefon):
        flash("Podaj poprawny numer telefonu (min. 9 cyfr).", "error")
        return redirect(url_for("rezerwacja_formularz", salon_slug=salon_slug, data=data_iso, godzina=godzina))
    if godzina not in dostepne_terminy(salon, data_iso):
        flash("Ten termin został właśnie zajęty. Wybierz inną godzinę.", "error")
        return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug, data=data_iso))

    rezerwacja_id = uuid.uuid4().hex[:12]
    rezerwacja = {
        "id": rezerwacja_id,
        "token_anulowania": uuid.uuid4().hex,
        "status": "oczekuje",
        "token_opinii": uuid.uuid4().hex,
        "data": data_iso,
        "godzina": godzina,
        "imie": imie,
        "telefon": telefon,
        "pracownik": pracownik,
        "uwagi": uwagi,
        "utworzono": datetime.now().isoformat(timespec="minutes"),
    }
    salon.setdefault("rezerwacje", []).append(rezerwacja)

    terminy = salon.setdefault("wolne_terminy", {}).setdefault(data_iso, [])
    if godzina in terminy and slot_w_pelni_zajety(salon, data_iso, godzina):
        terminy.remove(godzina)
    if not terminy:
        salon["wolne_terminy"].pop(data_iso, None)

    zapisz_dane(dane)
    wyslij_email_powiadomienie(salon, rezerwacja, salon_slug)
    return redirect(url_for("rezerwacja_potwierdzenie", salon_slug=salon_slug, id=rezerwacja_id))


@app.route("/rezerwacja/<salon_slug>/potwierdzenie")
def rezerwacja_potwierdzenie(salon_slug: str):
    dane = wczytaj_dane()
    salon = pobierz_salon(dane, salon_slug)
    if not salon:
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
    )


@app.route("/rezerwacja/<salon_slug>/opinia/<token>", methods=["GET", "POST"])
def opinia_klienta(salon_slug: str, token: str):
    dane = wczytaj_dane()
    salon = pobierz_salon(dane, salon_slug)
    if not salon:
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

        opinia_id = uuid.uuid4().hex[:12]
        opinia = {
            "id": opinia_id,
            "rezerwacja_id": rezerwacja["id"],
            "ocena": int(ocena),
            "komentarz": komentarz,
            "imie": rezerwacja.get("imie", ""),
            "pracownik": rezerwacja.get("pracownik", ""),
            "data_wizyty": rezerwacja.get("data", ""),
            "widoczna": True,
            "utworzono": datetime.now().isoformat(timespec="minutes"),
        }
        salon.setdefault("opinie", []).append(opinia)
        rezerwacja["opinia_id"] = opinia_id
        zapisz_dane(dane)
        flash("Dziękujemy za opinię!", "success")
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
    dane = wczytaj_dane()
    salon = pobierz_salon(dane, salon_slug)
    if not salon:
        return render_template("404.html", sciezka=request.path, domyslny_slug=domyslny_slug(dane)), 404

    rezerwacja = znajdz_rezerwacje_po_tokenie(salon, token)
    if not rezerwacja:
        flash("Nie znaleziono rezerwacji do anulowania.", "error")
        return redirect(url_for("rezerwacja_publiczna", salon_slug=salon_slug))

    if request.method == "POST":
        if rezerwacja.get("status") not in {"anulowana", "odrzucona"}:
            rezerwacja["status"] = "anulowana"
            rezerwacja["anulowano"] = datetime.now().isoformat(timespec="minutes")
            rezerwacja["anulowal"] = "klient"
            przywroc_wolny_termin(salon, rezerwacja)
            zapisz_dane(dane)
            flash("Rezerwacja została anulowana.", "success")
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


@app.route("/panel/<salon_slug>/rezerwacje", methods=["GET", "POST"])
def panel_rezerwacje(salon_slug: str):
    dane = wczytaj_dane()
    salon = pobierz_salon(dane, salon_slug)
    if not salon:
        flash("Nie znaleziono takiego salonu.", "error")
        return redirect(url_for("panel_lista"))

    if request.method == "POST":
        akcja = request.form.get("akcja")
        rezerwacja_id = request.form.get("id", "")
        rezerwacja = znajdz_rezerwacje(salon, rezerwacja_id)

        if akcja in {"potwierdz", "odrzuc", "anuluj"} and rezerwacja:
            if akcja == "potwierdz":
                rezerwacja["status"] = "potwierdzona"
                rezerwacja["potwierdzono"] = datetime.now().isoformat(timespec="minutes")
                flash(f"Potwierdzono rezerwację: {rezerwacja['imie']}.", "success")
            elif akcja == "odrzuc":
                rezerwacja["status"] = "odrzucona"
                rezerwacja["odrzucono"] = datetime.now().isoformat(timespec="minutes")
                przywroc_wolny_termin(salon, rezerwacja)
                flash(f"Odrzucono rezerwację: {rezerwacja['imie']}. Termin wrócił do wolnych.", "success")
            elif akcja == "anuluj":
                rezerwacja["status"] = "anulowana"
                rezerwacja["anulowano"] = datetime.now().isoformat(timespec="minutes")
                rezerwacja["anulowal"] = "salon"
                przywroc_wolny_termin(salon, rezerwacja)
                flash(f"Anulowano rezerwację: {rezerwacja['imie']}. Termin wrócił do wolnych.", "success")
            zapisz_dane(dane)
        return redirect(url_for("panel_rezerwacje", salon_slug=salon_slug))

    dzisiaj = date.today().isoformat()
    rezerwacje = sorted(
        salon.get("rezerwacje", []),
        key=lambda r: (r.get("data", ""), r.get("godzina", "")),
    )
    nadchodzace = [
        r
        for r in rezerwacje
        if r.get("data", "") >= dzisiaj and r.get("status", "potwierdzona") not in {"anulowana", "odrzucona"}
    ]
    archiwum = [
        r
        for r in rezerwacje
        if r.get("data", "") < dzisiaj or r.get("status", "potwierdzona") in {"anulowana", "odrzucona"}
    ]

    return render_template(
        "rezerwacje.html",
        dane=salon,
        salon_slug=salon_slug,
        nadchodzace=nadchodzace,
        archiwum=archiwum[-20:],
        dni_tygodnia=dict(DNI_TYGODNIA),
    )


@app.route("/panel/<salon_slug>/opinie", methods=["GET", "POST"])
def panel_opinie(salon_slug: str):
    dane = wczytaj_dane()
    salon = pobierz_salon(dane, salon_slug)
    if not salon:
        flash("Nie znaleziono takiego salonu.", "error")
        return redirect(url_for("panel_lista"))

    if request.method == "POST":
        opinia_id = request.form.get("id", "")
        akcja = request.form.get("akcja", "")
        for opinia in salon.get("opinie", []):
            if opinia.get("id") == opinia_id:
                if akcja == "ukryj":
                    opinia["widoczna"] = False
                    flash("Opinia została ukryta na stronie klienta.", "success")
                elif akcja == "pokaz":
                    opinia["widoczna"] = True
                    flash("Opinia jest znowu widoczna na stronie klienta.", "success")
                break
        zapisz_dane(dane)
        return redirect(url_for("panel_opinie", salon_slug=salon_slug))

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(debug=debug, host="0.0.0.0", port=port)
