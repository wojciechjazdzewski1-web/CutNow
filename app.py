"""CutNow — prosty panel salonu fryzjerskiego."""

from __future__ import annotations

import json
import os
import secrets
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
        "health",
        "panel_login",
        "panel_wyloguj",
        "static",
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
}


def wczytaj_dane() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        zapisz_dane(DEFAULT_DATA.copy())
        return DEFAULT_DATA.copy()
    with DATA_FILE.open(encoding="utf-8") as f:
        return json.load(f)


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


def bezpieczny_next_url(url: str | None) -> str:
    if not url:
        return url_for("panel")
    parsed = urlparse(url)
    if parsed.netloc or not url.startswith("/") or url.startswith("//"):
        return url_for("panel")
    return url


@app.before_request
def wymagaj_hasla_panelu():
    if not PANEL_PASSWORD:
        return
    if request.endpoint in PUBLIC_ENDPOINTS:
        return
    if request.endpoint and request.endpoint.startswith("panel") and not session.get("panel_auth"):
        return redirect(url_for("panel_login", next=request.path))


@app.context_processor
def inject_globals():
    return {
        "dni_tygodnia": DNI_TYGODNIA,
        "panel_chroniony_haslem": bool(PANEL_PASSWORD),
        "zalogowany_do_panelu": session.get("panel_auth", False),
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
    terminy_dzis = dane.get("wolne_terminy", {}).get(dzisiaj, [])
    return render_template(
        "panel.html",
        dane=dane,
        dzisiaj=dzisiaj,
        terminy_dzis=terminy_dzis,
        liczba_dni_z_terminami=len(dane.get("wolne_terminy", {})),
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

    terminy = sorted(dane.get("wolne_terminy", {}).get(wybrana_data, []))
    wszystkie_terminy = dane.get("wolne_terminy", {})

    return render_template(
        "terminy.html",
        dane=dane,
        wybrana_data=wybrana_data,
        terminy=terminy,
        wszystkie_terminy=wszystkie_terminy,
    )


@app.route("/rezerwacja")
def rezerwacja_publiczna():
    """Krótki link dla klientów (Instagram, SMS)."""
    return redirect(url_for("podglad_klienta", **request.args))


@app.route("/panel/podglad")
def podglad_klienta():
    """Podgląd tego, co zobaczy klient rezerwujący wizytę."""
    dane = wczytaj_dane()
    wybrana_data = request.args.get("data", date.today().isoformat())
    try:
        datetime.strptime(wybrana_data, "%Y-%m-%d")
    except ValueError:
        wybrana_data = date.today().isoformat()

    dzien_tygodnia = [
        "poniedzialek",
        "wtorek",
        "sroda",
        "czwartek",
        "piatek",
        "sobota",
        "niedziela",
    ][datetime.strptime(wybrana_data, "%Y-%m-%d").weekday()]

    godziny = dane["godziny_pracy"].get(dzien_tygodnia, {})
    terminy = sorted(dane.get("wolne_terminy", {}).get(wybrana_data, []))

    return render_template(
        "podglad.html",
        dane=dane,
        wybrana_data=wybrana_data,
        dzien_tygodnia=dzien_tygodnia,
        godziny=godziny,
        terminy=terminy,
        dni_tygodnia=dict(DNI_TYGODNIA),
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    # 0.0.0.0 = dostęp też z telefonu w tej samej sieci Wi‑Fi
    app.run(debug=debug, host="0.0.0.0", port=port)
