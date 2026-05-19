# Dlaczego znikały salony po deploy i jak to naprawić

## Przyczyna

Glovaro trzyma salony w pliku `data/salon.json`. Ten plik:

- **nie jest w GitHubie** (jest w `.gitignore`),
- na Renderze (plan free) przy każdym **Deploy** powstaje **nowy, pusty serwer**,
- aplikacja nie znajduje pliku i tworzy od zera tylko salon **„demo”**.

To nie jest reset kont użytkowników — to **utrata pliku z danymi** przy wdrożeniu.

## Rozwiązanie: PostgreSQL na Renderze

1. W [Render Dashboard](https://dashboard.render.com) → **New** → **PostgreSQL** (plan Free).
2. Po utworzeniu bazy: skopiuj **Internal Database URL**.
3. Otwórz serwis **glovaro** (web) → **Environment** → dodaj zmienną:
   - `DATABASE_URL` = wklejony URL (zaczyna się od `postgresql://...`)
4. **Save** → **Manual Deploy**.

Po starcie sprawdź: `https://glovaro.pl/health` — powinno być `"storage": "postgres"`.

Od tego momentu salony, rezerwacje i ustawienia **zostają po deploy**.

## Uwagi

- Salony utworzone **przed** podłączeniem bazy trzeba dodać ponownie (chyba że masz kopię `data/salon.json` z komputera — wtedy można ją zaimportować lokalnie z `DATABASE_URL` i jednym uruchomieniem aplikacji).
- Lokalnie nadal działa plik `data/salon.json` (bez `DATABASE_URL`).
- `SECRET_KEY` na Renderze **nie zmieniaj** bez potrzeby — wtedy trzeba się tylko ponownie zalogować do panelu.
