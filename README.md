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
