"""REST API v1 — integracja z botem Instagram AI."""

from __future__ import annotations

import os

from flask import Blueprint, jsonify, request, url_for

api_v1_bp = Blueprint("api_v1", __name__, url_prefix="/api/v1")


def _instagram_bot_api_key() -> str:
    return os.environ.get("INSTAGRAM_BOT_API_KEY", "").strip()


def _api_autoryzacja_ok() -> bool:
    klucz = _instagram_bot_api_key()
    if not klucz:
        return False
    naglowek = request.headers.get("Authorization", "")
    if naglowek.startswith("Bearer ") and naglowek[7:].strip() == klucz:
        return True
    return request.headers.get("X-API-Key", "").strip() == klucz


def _wymaga_klucza_api():
    if not _api_autoryzacja_ok():
        return jsonify({"error": "unauthorized", "message": "Brak lub nieprawidłowy klucz API."}), 401
    return None


def _salon_slug_z_parametru(salon_id: str) -> str:
    """W Glovaro identyfikatorem salonu jest slug z URL (np. beautiful-body)."""
    return (salon_id or "").strip().lower()


@api_v1_bp.route("/slots", methods=["GET"])
def api_slots():
    blad_auth = _wymaga_klucza_api()
    if blad_auth:
        return blad_auth

    from app import (
        dostepne_terminy,
        salon_wstrzymany,
        waliduj_date_iso,
        wczytaj_salon_bezposrednio,
        zakres_publicznej_rezerwacji,
    )

    salon_id = _salon_slug_z_parametru(request.args.get("salon_id", ""))
    data_iso = (request.args.get("date") or "").strip()

    if not salon_id:
        return jsonify({"error": "bad_request", "message": "Parametr salon_id jest wymagany."}), 400
    if not waliduj_date_iso(data_iso):
        return jsonify({"error": "bad_request", "message": "Parametr date musi być w formacie YYYY-MM-DD."}), 400

    data_od, data_do = zakres_publicznej_rezerwacji(data_iso)
    salon = wczytaj_salon_bezposrednio(
        salon_id,
        data_od=data_od,
        data_do=data_do,
        include_clients=False,
    )
    if not salon:
        return jsonify({"error": "not_found", "message": "Nie znaleziono salonu o podanym salon_id."}), 404
    if salon_wstrzymany(salon):
        return jsonify({"error": "unavailable", "message": "Rezerwacje dla tego salonu są chwilowo niedostępne."}), 403

    sloty = dostepne_terminy(salon, data_iso)
    return jsonify(
        {
            "salon_id": salon_id,
            "salon_name": salon.get("nazwa_salonu", ""),
            "date": data_iso,
            "slots": sloty,
            "count": len(sloty),
        }
    )


@api_v1_bp.route("/book", methods=["POST"])
def api_book():
    blad_auth = _wymaga_klucza_api()
    if blad_auth:
        return blad_auth

    from app import (
        aktywni_pracownicy,
        aktualizuj_salon_atomowo,
        czas_trwania_rezerwacji_min,
        dostepne_terminy,
        normalizuj_godzine,
        pracownik_zajety,
        przekroczono_limit_rezerwacji,
        salon_wstrzymany,
        uslugi_salonu,
        utworz_rezerwacje,
        waliduj_date_iso,
        waliduj_email,
        waliduj_telefon,
        wczytaj_salon_bezposrednio,
        wyslij_email_powiadomienie,
        wywiad_przy_rezerwacji_wlaczony,
        zakres_publicznej_rezerwacji,
    )

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "bad_request", "message": "Oczekiwano JSON w body żądania."}), 400

    salon_id = _salon_slug_z_parametru(str(body.get("salon_id", "")))
    data_iso = str(body.get("date", "")).strip()
    godzina = normalizuj_godzine(str(body.get("time", "")))
    imie = str(body.get("customer_name", "")).strip()
    telefon = str(body.get("customer_phone", "")).strip()
    email = str(body.get("customer_email", "")).strip().lower()
    uwagi = str(body.get("notes", "")).strip()
    usluga_nazwa = str(body.get("service_name", "")).strip()

    if not salon_id:
        return jsonify({"error": "bad_request", "message": "Pole salon_id jest wymagane."}), 400
    if not waliduj_date_iso(data_iso):
        return jsonify({"error": "bad_request", "message": "Pole date musi być w formacie YYYY-MM-DD."}), 400
    if not godzina:
        return jsonify({"error": "bad_request", "message": "Pole time musi być w formacie HH:MM."}), 400
    if not imie:
        return jsonify({"error": "bad_request", "message": "Pole customer_name jest wymagane."}), 400
    if not waliduj_telefon(telefon):
        return jsonify({"error": "bad_request", "message": "Pole customer_phone jest nieprawidłowe (min. 9 cyfr)."}), 400
    if email and not waliduj_email(email):
        return jsonify({"error": "bad_request", "message": "Pole customer_email jest nieprawidłowe."}), 400

    if przekroczono_limit_rezerwacji(salon_id):
        return jsonify({"error": "rate_limited", "message": "Zbyt wiele prób rezerwacji. Spróbuj za kilka minut."}), 429

    data_od, data_do = zakres_publicznej_rezerwacji(data_iso)
    salon = wczytaj_salon_bezposrednio(
        salon_id,
        data_od=data_od,
        data_do=data_do,
        include_clients=False,
    )
    if not salon:
        return jsonify({"error": "not_found", "message": "Nie znaleziono salonu o podanym salon_id."}), 404
    if salon_wstrzymany(salon):
        return jsonify({"error": "unavailable", "message": "Rezerwacje dla tego salonu są chwilowo niedostępne."}), 403
    if wywiad_przy_rezerwacji_wlaczony(salon):
        return jsonify(
            {
                "error": "health_survey_required",
                "message": "Ten salon wymaga wywiadu zdrowotnego — rezerwacja przez API nie jest dostępna.",
            }
        ), 409

    uslugi = uslugi_salonu(salon)
    mapa_uslug = {u["nazwa"]: u for u in uslugi}
    if uslugi and not usluga_nazwa and len(uslugi) == 1:
        usluga_nazwa = uslugi[0]["nazwa"]
    if uslugi and usluga_nazwa not in mapa_uslug:
        return jsonify(
            {
                "error": "bad_request",
                "message": "Wybierz usługę z listy salonu.",
                "available_services": [u["nazwa"] for u in uslugi],
            }
        ), 400

    if godzina not in dostepne_terminy(salon, data_iso):
        return jsonify({"error": "slot_unavailable", "message": "Ten termin nie jest już dostępny."}), 409

    wybrana_usluga = mapa_uslug.get(usluga_nazwa, {})
    czas_uslugi = czas_trwania_rezerwacji_min(
        salon,
        {"usluga_czas_min": wybrana_usluga.get("czas_min", 0)},
    )
    pracownicy = aktywni_pracownicy(salon)
    pracownik = ""
    if pracownicy:
        pracownik = next(
            (p for p in pracownicy if not pracownik_zajety(salon, data_iso, godzina, p, czas_uslugi)),
            "",
        )
        if not pracownik:
            return jsonify({"error": "slot_unavailable", "message": "Brak wolnego pracownika w tym terminie."}), 409

    pracownik_formularz = pracownik
    usluga_formularz = usluga_nazwa

    def utworz_atomowo(salon_atomowy: dict | None):
        if not salon_atomowy:
            return None, "Nie znaleziono takiej firmy.", None
        if salon_wstrzymany(salon_atomowy):
            return None, "Rezerwacje dla tej firmy są chwilowo niedostępne.", None

        final_pracownik = pracownik_formularz
        final_pracownicy = aktywni_pracownicy(salon_atomowy)
        final_uslugi = uslugi_salonu(salon_atomowy)
        final_mapa_uslug = {u["nazwa"]: u for u in final_uslugi}
        if final_uslugi and usluga_formularz not in final_mapa_uslug:
            return None, "Wybierz usługę z listy.", None
        final_usluga = final_mapa_uslug.get(usluga_formularz, {})
        final_czas = czas_trwania_rezerwacji_min(
            salon_atomowy,
            {"usluga_czas_min": final_usluga.get("czas_min", 0)},
        )
        if final_pracownicy and not final_pracownik:
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
            usluga_nazwa=usluga_formularz,
            status="oczekuje",
            zrodlo="instagram",
        )
        return rezerwacja_atomowa, blad_atomowy, None

    rezerwacja, blad, salon_po_zapisie = aktualizuj_salon_atomowo(salon_id, utworz_atomowo)
    if blad:
        kod = 409 if "termin" in blad.lower() or "zajęt" in blad.lower() else 400
        return jsonify({"error": "booking_failed", "message": blad}), kod
    if not rezerwacja:
        return jsonify({"error": "booking_failed", "message": "Nie udało się utworzyć rezerwacji."}), 500

    wyslij_email_powiadomienie(salon_po_zapisie or salon, rezerwacja, salon_id)

    return jsonify(
        {
            "success": True,
            "reservation": {
                "id": rezerwacja["id"],
                "salon_id": salon_id,
                "date": rezerwacja["data"],
                "time": rezerwacja["godzina"],
                "status": rezerwacja.get("status", "oczekuje"),
                "customer_name": rezerwacja["imie"],
                "customer_phone": rezerwacja["telefon"],
                "service_name": rezerwacja.get("usluga_nazwa", ""),
            },
            "confirmation_url": url_for(
                "rezerwacja_potwierdzenie",
                salon_slug=salon_id,
                id=rezerwacja["id"],
                _external=True,
            ),
        }
    ), 201
