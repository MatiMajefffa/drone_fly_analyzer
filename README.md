# drone_fly_analyzer

Analiza logów lotów z drona DJI (pliki `FlightRecord_*.txt` z aplikacji DJI Fly).

## Struktura

- `src/` – kod (parser logów, analizy)
- `notebooks/` – notebooki Jupyter
- `data/inbox/` – surowe logi lotów (nie są w repo)

## Uruchomienie

```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Logi w wersji 13+ są szyfrowane – potrzebny jest klucz API DJI w pliku `.env`:

```
DJI_API_KEY=twoj_klucz
```

Szybki podgląd lotu:

```bash
python -m pydjirecord "data/inbox/FlightRecord_....txt"
```

## Import lotów do bazy

Wrzuć pliki `FlightRecord_*.txt` do `data/inbox/` i uruchom:

```bash
python src/parse.py          # importuje tylko nowe loty
python src/parse.py --force  # przebudowuje bazę od zera
```

Wynik trafia do `data/flights.db` (SQLite):

- `frames` – telemetria klatka po klatce (~10 Hz, ~110 kolumn: pozycja, prędkości, bateria, sygnał RC, gimbal, ostrzeżenia), kolumna `flight_id`
- `flights` – jeden wiersz na lot: czas, dystans, maks. wysokość/prędkość, zużycie baterii, temperatura, sygnał RC (5. percentyl i czas słabego sygnału), GPS
