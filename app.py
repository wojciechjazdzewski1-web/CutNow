"""CutNow — prosty panel salonu fryzjerskiego."""

from __future__ import annotations

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

PUBLIC_ENDPOINTS = frozenset(
    {
        "strona_glowna",
        "podglad_klienta",
        "rezerwacja_publiczna",
        "rezerwacja_formularz",
        "rezerwacja_potwierdzenie",
        "health",
        "panel_login",
        "panel_wyloguj",
        "static",
    }
)

WIDOK_KLIENTA_ENDPOINTS = frozenset(
    {
        "rezerwacja_publiczna",
        "rezerwacja_formularz",
        "rezerwacja_potwierdzenie",
    }
)

DNI_TYGODNIA = [
    ("poniedzialek", "Poniedziałek"),
    ("wtorek", "Wtorek"),
    ("sroda", "Środa"),
    ("czwartek", "Czwartek"),
    ("piatek", "Piątek"),
    ("sobota", "Sobota"),
    ("niedziela", "Niedziela"),
]

DEFAULT_DATA = {
    "nazwa_salonu": "Mój Salon",
    "godziny_pracy": {
        key: {"otwarcie": "09:00", "zamkniecie": "18:00", "zamkniety": key == "niedziela"}
        for key, _ in DNI_TYGODNIA
    },
    "wolne_terminy": {},
    "rezerwacje": [],
}


def wczytaj_dane() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        zapisz_dane(DEFAULT_DATA.copy())
        return DEFAULT_DATA.copy()
    with DATA_FILE.open(encoding="utf-8") as f:
        dane = json.load(f)
    if "rezerwacje" not in dane:
        dane["rezerwacje"] = []
        zapisz_dane(dane)
    return dane


def zapisz_dane(dane: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(dane, f, ensure_ascii=False, indent=2)


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


def zajete_godziny(dane: dict, data_iso: str) -> set[str]:
    return {
        r["godzina"]
        for r in dane.get("rezerwacje", [])
        if r.get("data") == data_iso
    }


def dostepne_terminy(dane: dict, data_iso: str) -> list[str]:
    wolne = dane.get("wolne_terminy", {}).get(data_iso, [])
    zajete = zajete_godziny(dane, data_iso)
    return sorted(g for g in wolne if g not in zajete)


def normalizuj_telefon(telefon: str) -> str:
    return re.sub(r"\D", "", telefon)


def waliduj_telefon(telefon: str) -> bool:
    cyfry = normalizuj_telefon(telefon)
    return 9 <= len(cyfry) <= 15


def znajdz_rezerwacje(dane: dict, rezerwacja_id: str) -> dict | None:
    for rezerwacja in dane.get("rezerwacje", []):
        if rezerwacja.get("id") == rezerwacja_id:
            return rezerwacja
    return None


def bezpieczny_next_url(url: str | None) -> str:
    if not url:
        return url_for("panel")
    parsed = urlparse(url)
    if parsed.netloc or not url.startswith("/") or url.startswith("//"):
        return url_for("panel")
    return url


@app.before_request
def wymagaj_hasla_panelu():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return
    if not (request.endpoint and request.endpoint.startswith("panel")):
        return

    # Na Renderze panel bez hasła jest zablokowany (ochrona przed klientami).
    if not PANEL_PASSWORD:
        if os.environ.get("RENDER"):
            flash("Ta strona jest tylko dla właściciela salonu.", "error")
            return redirect(url_for("rezerwacja_publiczna"))
        return

    if not session.get("panel_auth"):
        return redirect(url_for("panel_login", next=request.path))


@app.context_processor
def inject_globals():
    return {
        "dni_tygodnia": DNI_TYGODNIA,
        "panel_chroniony_haslem": bool(PANEL_PASSWORD),
        "zalogowany_do_panelu": session.get("panel_auth", False),
        "widok_klienta": request.endpoint in WIDOK_KLIENTA_ENDPOINTS,
    }


@app.route("/panel/login", methods=["GET", "POST"])
def panel_login():
    if not PANEL_PASSWORD:
        return redirect(url_for("panel"))
    if session.get("panel_auth"):
        return redirect(bezpieczny_next_url(request.args.get("next")))
    if request.method == "POST":
        haslo = request.form.get("haslo", "")
        if haslo == PANEL_PASSWORD:
            session["panel_auth"] = True
            flash("Zalogowano do panelu.", "success")
            return redirect(bezpieczny_next_url(request.form.get("next") or request.args.get("next")))
        flash("Nieprawidłowe hasło.", "error")
    return render_template("login.html")


@app.route("/panel/wyloguj")
def panel_wyloguj():
    session.pop("panel_auth", None)
    flash("Wylogowano z panelu.", "success")
    return redirect(url_for("strona_glowna"))


@app.route("/health")
def health():
    """Render sprawdza, czy aplikacja żyje — wejdź na /health w przeglądarce."""
    return jsonify({"status": "ok", "app": "CutNow"}), 200


@app.route("/")
def strona_glowna():
    return render_template("index.html")


@app.errorhandler(404)
def nie_znaleziono(_error):
    return (
        render_template(
            "404.html",
            sciezka=request.path,
        ),
        404,
    )


@app.route("/panel")
def panel():
    dane = wczytaj_dane()
    dzisiaj = date.today().isoformat()
    terminy_dzis = dostepne_terminy(dane, dzisiaj)
    rezerwacje = sorted(
        dane.get("rezerwacje", []),
        key=lambda r: (r.get("data", ""), r.get("godzina", "")),
    )
    nadchodzace = [r for r in rezerwacje if r.get("data", "") >= dzisiaj]
    return render_template(
        "panel.html",
        dane=dane,
        dzisiaj=dzisiaj,
        terminy_dzis=terminy_dzis,
        liczba_dni_z_terminami=len(dane.get("wolne_terminy", {})),
        liczba_rezerwacji=len(nadchodzace),
        ostatnie_rezerwacje=nadchodzace[:5],
    )


@app.route("/panel/salon", methods=["GET", "POST"])
def ustawienia_salonu():
    dane = wczytaj_dane()
    if request.method == "POST":
        nazwa = request.form.get("nazwa_salonu", "").strip()
        if nazwa:
            dane["nazwa_salonu"] = nazwa
            zapisz_dane(dane)
            flash("Nazwa salonu została zapisana.", "success")
        else:
            flash("Podaj nazwę salonu.", "error")
        return redirect(url_for("ustawienia_salonu"))
    return render_template("salon.html", dane=dane)


@app.route("/panel/godziny", methods=["GET", "POST"])
def godziny_pracy():
    dane = wczytaj_dane()
    if request.method == "POST":
        for klucz, _ in DNI_TYGODNIA:
            zamkniety = request.form.get(f"zamkniety_{klucz}") == "on"
            otwarcie = request.form.get(f"otwarcie_{klucz}", "09:00")
            zamkniecie = request.form.get(f"zamkniecie_{klucz}", "18:00")

            if not zamkniety and (
                not waliduj_godzine(otwarcie) or not waliduj_godzine(zamkniecie)
            ):
                flash(f"Nieprawidłowy format godzin dla {klucz}. Użyj HH:MM.", "error")
                return redirect(url_for("godziny_pracy"))

            if not zamkniety and otwarcie >= zamkniecie:
                flash("Godzina otwarcia musi być wcześniejsza niż zamknięcia.", "error")
                return redirect(url_for("godziny_pracy"))

            dane["godziny_pracy"][klucz] = {
                "otwarcie": otwarcie,
                "zamkniecie": zamkniecie,
                "zamkniety": zamkniety,
            }
        zapisz_dane(dane)
        flash("Godziny pracy zostały zapisane.", "success")
        return redirect(url_for("godziny_pracy"))

    return render_template("godziny.html", dane=dane)


@app.route("/panel/terminy", methods=["GET", "POST"])
def wolne_terminy():
    dane = wczytaj_dane()
    wybrana_data = request.args.get("data") or request.form.get("data") or date.today().isoformat()

    try:
        datetime.strptime(wybrana_data, "%Y-%m-%d")
    except ValueError:
        wybrana_data = date.today().isoformat()
        flash("Nieprawidłowa data — pokazuję dzisiejszy dzień.", "error")

    if request.method == "POST":
        akcja = request.form.get("akcja")
        godzina = request.form.get("godzina", "").strip()

        if wybrana_data not in dane["wolne_terminy"]:
            dane["wolne_terminy"][wybrana_data] = []

        terminy = dane["wolne_terminy"][wybrana_data]

        if akcja == "dodaj":
            if not waliduj_godzine(godzina):
                flash("Podaj godzinę w formacie HH:MM (np. 10:30).", "error")
            elif godzina in terminy:
                flash("Ten termin już istnieje.", "error")
            else:
                terminy.append(godzina)
                terminy.sort()
                flash(f"Dodano wolny termin: {godzina}.", "success")

        elif akcja == "usun":
            if godzina in terminy:
                terminy.remove(godzina)
                flash(f"Usunięto termin: {godzina}.", "success")
            if not terminy:
                del dane["wolne_terminy"][wybrana_data]

        zapisz_dane(dane)
        return redirect(url_for("wolne_terminy", data=wybrana_data))

    terminy = dostepne_terminy(dane, wybrana_data)
    zajete = sorted(zajete_godziny(dane, wybrana_data))
    wszystkie_terminy = dane.get("wolne_terminy", {})

    return render_template(
        "terminy.html",
        dane=dane,
        wybrana_data=wybrana_data,
        terminy=terminy,
        zajete=zajete,
        wszystkie_terminy=wszystkie_terminy,
    )


def kontekst_rezerwacji(dane: dict, wybrana_data: str) -> dict:
    if not waliduj_date_iso(wybrana_data):
        wybrana_data = date.today().isoformat()
    dzien_tygodnia = klucz_dnia_tygodnia(wybrana_data)
    godziny = dane["godziny_pracy"].get(dzien_tygodnia, {})
    return {
        "dane": dane,
        "wybrana_data": wybrana_data,
        "dzien_tygodnia": dzien_tygodnia,
        "godziny": godziny,
        "terminy": dostepne_terminy(dane, wybrana_data),
        "dni_tygodnia": dict(DNI_TYGODNIA),
    }


@app.route("/rezerwacja")
def rezerwacja_publiczna():
    """Strona rezerwacji dla klientów."""
    dane = wczytaj_dane()
    wybrana_data = request.args.get("data", date.today().isoformat())
    return render_template("podglad.html", **kontekst_rezerwacji(dane, wybrana_data))


@app.route("/rezerwacja/nowa", methods=["GET", "POST"])
def rezerwacja_formularz():
    dane = wczytaj_dane()
    data_iso = request.values.get("data", date.today().isoformat())
    godzina = request.values.get("godzina", "").strip()

    if not waliduj_date_iso(data_iso):
        flash("Nieprawidłowa data.", "error")
        return redirect(url_for("rezerwacja_publiczna"))

    if request.method == "GET":
        if not godzina or godzina not in dostepne_terminy(dane, data_iso):
            flash("Wybierz dostępny termin z listy.", "error")
            return redirect(url_for("rezerwacja_publiczna", data=data_iso))
        ctx = kontekst_rezerwacji(dane, data_iso)
        ctx["godzina"] = godzina
        return render_template("rezerwacja_form.html", **ctx)

    imie = request.form.get("imie", "").strip()
    telefon = request.form.get("telefon", "").strip()
    uwagi = request.form.get("uwagi", "").strip()
    godzina = request.form.get("godzina", "").strip()

    if not imie:
        flash("Podaj imię i nazwisko.", "error")
        return redirect(url_for("rezerwacja_formularz", data=data_iso, godzina=godzina))
    if not waliduj_telefon(telefon):
        flash("Podaj poprawny numer telefonu (min. 9 cyfr).", "error")
        return redirect(url_for("rezerwacja_formularz", data=data_iso, godzina=godzina))
    if godzina not in dostepne_terminy(dane, data_iso):
        flash("Ten termin został właśnie zajęty. Wybierz inną godzinę.", "error")
        return redirect(url_for("rezerwacja_publiczna", data=data_iso))

    rezerwacja_id = uuid.uuid4().hex[:12]
    rezerwacja = {
        "id": rezerwacja_id,
        "data": data_iso,
        "godzina": godzina,
        "imie": imie,
        "telefon": telefon,
        "uwagi": uwagi,
        "utworzono": datetime.now().isoformat(timespec="minutes"),
    }
    dane.setdefault("rezerwacje", []).append(rezerwacja)

    terminy = dane.setdefault("wolne_terminy", {}).setdefault(data_iso, [])
    if godzina in terminy:
        terminy.remove(godzina)
    if not terminy:
        dane["wolne_terminy"].pop(data_iso, None)

    zapisz_dane(dane)
    return redirect(url_for("rezerwacja_potwierdzenie", id=rezerwacja_id))


@app.route("/rezerwacja/potwierdzenie")
def rezerwacja_potwierdzenie():
    dane = wczytaj_dane()
    rezerwacja_id = request.args.get("id", "")
    rezerwacja = znajdz_rezerwacje(dane, rezerwacja_id)
    if not rezerwacja:
        flash("Nie znaleziono rezerwacji.", "error")
        return redirect(url_for("rezerwacja_publiczna"))
    dzien = klucz_dnia_tygodnia(rezerwacja["data"])
    return render_template(
        "rezerwacja_potwierdzenie.html",
        dane=dane,
        rezerwacja=rezerwacja,
        dzien_nazwa=dict(DNI_TYGODNIA)[dzien],
    )


@app.route("/panel/podglad")
def podglad_klienta():
    return redirect(url_for("rezerwacja_publiczna", **request.args))


@app.route("/panel/rezerwacje", methods=["GET", "POST"])
def panel_rezerwacje():
    dane = wczytaj_dane()

    if request.method == "POST":
        akcja = request.form.get("akcja")
        rezerwacja_id = request.form.get("id", "")
        rezerwacja = znajdz_rezerwacje(dane, rezerwacja_id)

        if akcja == "anuluj" and rezerwacja:
            dane["rezerwacje"] = [
                r for r in dane.get("rezerwacje", []) if r.get("id") != rezerwacja_id
            ]
            data_iso = rezerwacja["data"]
            godzina = rezerwacja["godzina"]
            if data_iso not in dane.setdefault("wolne_terminy", {}):
                dane["wolne_terminy"][data_iso] = []
            if godzina not in dane["wolne_terminy"][data_iso]:
                dane["wolne_terminy"][data_iso].append(godzina)
                dane["wolne_terminy"][data_iso].sort()
            zapisz_dane(dane)
            flash(
                f"Anulowano rezerwację: {rezerwacja['imie']}, {data_iso} o {godzina}.",
                "success",
            )
        return redirect(url_for("panel_rezerwacje"))

    dzisiaj = date.today().isoformat()
    rezerwacje = sorted(
        dane.get("rezerwacje", []),
        key=lambda r: (r.get("data", ""), r.get("godzina", "")),
    )
    nadchodzace = [r for r in rezerwacje if r.get("data", "") >= dzisiaj]
    archiwum = [r for r in rezerwacje if r.get("data", "") < dzisiaj]

    return render_template(
        "rezerwacje.html",
        dane=dane,
        nadchodzace=nadchodzace,
        archiwum=archiwum[-20:],
        dni_tygodnia=dict(DNI_TYGODNIA),
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    # 0.0.0.0 = dostęp też z telefonu w tej samej sieci Wi‑Fi
    app.run(debug=debug, host="0.0.0.0", port=port)
