"""CutNow — panel rezerwacji dla wielu salonów/fryzjerów."""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import uuid
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent / "data"))
DATA_FILE = DATA_DIR / "salon.json"
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "").strip()

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
    "zdjecia_prac": [],
    "godziny_pracy": {
        key: {"otwarcie": "09:00", "zamkniecie": "18:00", "zamkniety": key == "niedziela"}
        for key, _ in DNI_TYGODNIA
    },
    "wolne_terminy": {},
    "rezerwacje": [],
}

PUBLIC_ENDPOINTS = {
    "strona_glowna",
    "health",
    "rezerwacja_domyslna",
    "rezerwacja_publiczna",
    "rezerwacja_formularz",
    "rezerwacja_potwierdzenie",
    "panel_login",
    "static",
}

WIDOK_KLIENTA_ENDPOINTS = {
    "rezerwacja_domyslna",
    "rezerwacja_publiczna",
    "rezerwacja_formularz",
    "rezerwacja_potwierdzenie",
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
            salon.setdefault("zdjecia_prac", [])
            salon.setdefault("godziny_pracy", copy.deepcopy(DEFAULT_SALON["godziny_pracy"]))
            salon.setdefault("wolne_terminy", {})
            salon.setdefault("rezerwacje", [])
        return dane

    # Stary format jednej strony zamieniamy na salon "demo", żeby nie stracić danych.
    salon = nowy_salon(dane.get("nazwa_salonu", "Mój Salon"), PANEL_PASSWORD)
    salon["godziny_pracy"] = dane.get("godziny_pracy", salon["godziny_pracy"])
    salon["wolne_terminy"] = dane.get("wolne_terminy", {})
    salon["rezerwacje"] = dane.get("rezerwacje", [])
    salon["slug"] = "demo"
    return {"salony": {"demo": salon}}


def wczytaj_dane() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        dane = {"salony": {"demo": {**nowy_salon("Mój Salon", PANEL_PASSWORD), "slug": "demo"}}}
        zapisz_dane(dane)
        return dane

    with DATA_FILE.open(encoding="utf-8") as f:
        dane = json.load(f)

    zmigrowane = migracja_danych(dane)
    if zmigrowane != dane:
        zapisz_dane(zmigrowane)
    return zmigrowane


def zapisz_dane(dane: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(dane, f, ensure_ascii=False, indent=2)


def pobierz_salon(dane: dict, salon_slug: str) -> dict | None:
    salon = dane.get("salony", {}).get(salon_slug)
    if salon:
        salon.setdefault("slug", salon_slug)
    return salon


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


def zajete_godziny(salon: dict, data_iso: str) -> set[str]:
    return {
        r["godzina"]
        for r in salon.get("rezerwacje", [])
        if r.get("data") == data_iso
    }


def dostepne_terminy(salon: dict, data_iso: str) -> list[str]:
    wolne = salon.get("wolne_terminy", {}).get(data_iso, [])
    zajete = zajete_godziny(salon, data_iso)
    return sorted(g for g in wolne if g not in zajete)


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


def znajdz_rezerwacje(salon: dict, rezerwacja_id: str) -> dict | None:
    for rezerwacja in salon.get("rezerwacje", []):
        if rezerwacja.get("id") == rezerwacja_id:
            return rezerwacja
    return None


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


@app.context_processor
def inject_globals():
    salon_slug = request.view_args.get("salon_slug") if request.view_args else None
    return {
        "dni_tygodnia": DNI_TYGODNIA,
        "panel_chroniony_haslem": bool(PANEL_PASSWORD),
        "zalogowany_do_panelu": bool(salon_slug and zalogowany_do_salonu(salon_slug)),
        "widok_klienta": request.endpoint in WIDOK_KLIENTA_ENDPOINTS,
        "aktywny_salon_slug": salon_slug,
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
    return jsonify({"status": "ok", "app": "CutNow"}), 200


@app.route("/")
def strona_glowna():
    dane = wczytaj_dane()
    return render_template("index.html", salony=dane.get("salony", {}))


@app.errorhandler(404)
def nie_znaleziono(_error):
    dane = wczytaj_dane()
    domyslny = domyslny_slug(dane)
    return render_template("404.html", sciezka=request.path, domyslny_slug=domyslny), 404


@app.route("/panel")
def panel_lista():
    dane = wczytaj_dane()
    return render_template("salony.html", salony=dane.get("salony", {}))


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
    return render_template(
        "panel.html",
        dane=salon,
        salon_slug=salon_slug,
        dzisiaj=dzisiaj,
        terminy_dzis=terminy_dzis,
        liczba_dni_z_terminami=len(salon.get("wolne_terminy", {})),
        liczba_rezerwacji=len(nadchodzace),
        ostatnie_rezerwacje=nadchodzace[:5],
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
        zdjecia = parsuj_linki_zdjec(request.form.get("zdjecia_prac", ""))
        if nazwa:
            salon["nazwa_salonu"] = nazwa
            salon["opis"] = opis
            salon["telefon_kontaktowy"] = telefon
            salon["instagram"] = instagram
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

        if akcja == "dodaj":
            if not waliduj_godzine(godzina):
                flash("Podaj godzinę w formacie HH:MM (np. 10:30).", "error")
            elif godzina in terminy or godzina in zajete_godziny(salon, wybrana_data):
                flash("Ten termin już istnieje albo jest zajęty.", "error")
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

        zapisz_dane(dane)
        return redirect(url_for("wolne_terminy", salon_slug=salon_slug, data=wybrana_data))

    return render_template(
        "terminy.html",
        dane=salon,
        salon_slug=salon_slug,
        wybrana_data=wybrana_data,
        terminy=dostepne_terminy(salon, wybrana_data),
        zajete=sorted(zajete_godziny(salon, wybrana_data)),
        wszystkie_terminy=salon.get("wolne_terminy", {}),
    )


def kontekst_rezerwacji(salon: dict, salon_slug: str, wybrana_data: str) -> dict:
    if not waliduj_date_iso(wybrana_data):
        wybrana_data = date.today().isoformat()
    dzien_tygodnia = klucz_dnia_tygodnia(wybrana_data)
    godziny = salon["godziny_pracy"].get(dzien_tygodnia, {})
    return {
        "dane": salon,
        "salon_slug": salon_slug,
        "wybrana_data": wybrana_data,
        "dzien_tygodnia": dzien_tygodnia,
        "godziny": godziny,
        "terminy": dostepne_terminy(salon, wybrana_data),
        "dni_tygodnia": dict(DNI_TYGODNIA),
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
    wybrana_data = request.args.get("data", date.today().isoformat())
    return render_template("podglad.html", **kontekst_rezerwacji(salon, salon_slug, wybrana_data))


@app.route("/rezerwacja/<salon_slug>/nowa", methods=["GET", "POST"])
def rezerwacja_formularz(salon_slug: str):
    dane = wczytaj_dane()
    salon = pobierz_salon(dane, salon_slug)
    if not salon:
        return render_template("404.html", sciezka=request.path, domyslny_slug=domyslny_slug(dane)), 404

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
    salon.setdefault("rezerwacje", []).append(
        {
            "id": rezerwacja_id,
            "data": data_iso,
            "godzina": godzina,
            "imie": imie,
            "telefon": telefon,
            "uwagi": uwagi,
            "utworzono": datetime.now().isoformat(timespec="minutes"),
        }
    )

    terminy = salon.setdefault("wolne_terminy", {}).setdefault(data_iso, [])
    if godzina in terminy:
        terminy.remove(godzina)
    if not terminy:
        salon["wolne_terminy"].pop(data_iso, None)

    zapisz_dane(dane)
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

        if akcja == "anuluj" and rezerwacja:
            salon["rezerwacje"] = [
                r for r in salon.get("rezerwacje", []) if r.get("id") != rezerwacja_id
            ]
            data_iso = rezerwacja["data"]
            godzina = rezerwacja["godzina"]
            salon.setdefault("wolne_terminy", {}).setdefault(data_iso, [])
            if godzina not in salon["wolne_terminy"][data_iso]:
                salon["wolne_terminy"][data_iso].append(godzina)
                salon["wolne_terminy"][data_iso].sort()
            zapisz_dane(dane)
            flash(f"Anulowano rezerwację: {rezerwacja['imie']}, {data_iso} o {godzina}.", "success")
        return redirect(url_for("panel_rezerwacje", salon_slug=salon_slug))

    dzisiaj = date.today().isoformat()
    rezerwacje = sorted(
        salon.get("rezerwacje", []),
        key=lambda r: (r.get("data", ""), r.get("godzina", "")),
    )
    nadchodzace = [r for r in rezerwacje if r.get("data", "") >= dzisiaj]
    archiwum = [r for r in rezerwacje if r.get("data", "") < dzisiaj]

    return render_template(
        "rezerwacje.html",
        dane=salon,
        salon_slug=salon_slug,
        nadchodzace=nadchodzace,
        archiwum=archiwum[-20:],
        dni_tygodnia=dict(DNI_TYGODNIA),
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    app.run(debug=debug, host="0.0.0.0", port=port)
