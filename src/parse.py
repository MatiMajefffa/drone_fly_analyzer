"""Import logów DJI z data/inbox do bazy SQLite (data/flights.db).

Uruchomienie:  python src/parse.py           # importuje tylko nowe pliki
               python src/parse.py --force   # importuje wszystko od nowa

Tabele:
  frames  – jedna klatka telemetrii na wiersz (~10 Hz), kolumna flight_id
  flights – jeden lot na wiersz, podsumowanie
"""

from __future__ import annotations

import argparse
import io
import os
import sqlite3
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from pydjirecord import DJILog, FrameDetails
from pydjirecord.export.csv import export_csv

ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "data" / "inbox"
DB_PATH = ROOT / "data" / "flights.db"


def get_api_key() -> str:
    load_dotenv(ROOT / ".env")
    key = os.environ.get("DJI_API_KEY") or os.environ.get("Dji_moj_API_KEY")
    if not key:
        sys.exit("Brak klucza API: ustaw DJI_API_KEY w pliku .env")
    return key.strip().strip('"')


def decode_frames(path: Path, api_key: str) -> tuple[pd.DataFrame, DJILog, FrameDetails]:
    """Zdekoduj plik logu i zwróć klatki jako DataFrame."""
    log = DJILog.from_bytes(path.read_bytes())
    keychains = log.fetch_keychains(api_key) if log.version >= 13 else None
    frames = log.frames(keychains)

    buf = io.StringIO()
    export_csv(frames, log.details, buf)
    buf.seek(0)
    df = pd.read_csv(buf, low_memory=False)
    fd = FrameDetails.from_details(log.details, frames)

    # Kolumny DETAILS.* i RECOVER.* są stałe dla całego lotu – trafiają do flights
    df = df[[c for c in df.columns if not c.startswith(("DETAILS.", "RECOVER."))]]
    df["CUSTOM.dateTime"] = pd.to_datetime(df["CUSTOM.dateTime"], utc=True, errors="coerce")
    return df, log, fd


def summarize(flight_id: str, df: pd.DataFrame, log: DJILog, fd: FrameDetails) -> dict:
    """Policz podsumowanie jednego lotu."""
    d = log.details
    airborne = df[df["OSD.isOnGround"] == False]  # noqa: E712
    batt = df["BATTERY.chargeLevel"].dropna()
    batt = batt[batt > 0]

    def col_min(name: str, frame: pd.DataFrame = df):
        s = frame[name].dropna() if name in frame else pd.Series(dtype=float)
        return float(s.min()) if len(s) else None

    def col_max(name: str, frame: pd.DataFrame = df):
        s = frame[name].dropna() if name in frame else pd.Series(dtype=float)
        return float(s.max()) if len(s) else None

    def col_q(name: str, q: float, frame: pd.DataFrame = df):
        s = frame[name].dropna() if name in frame else pd.Series(dtype=float)
        return float(s.quantile(q)) if len(s) else None

    return {
        "flight_id": flight_id,
        "start_time": str(d.start_time) if getattr(d, "start_time", None) else None,
        "aircraft": d.aircraft_name or d.product_type.name,
        "aircraft_sn": d.aircraft_sn,
        "battery_sn": getattr(d, "battery_sn", None),
        "log_version": log.version,
        "duration_s": float(fd.total_time),
        "distance_m": float(fd.total_distance),
        "max_height_m": float(fd.max_height),
        "max_hspeed_ms": float(fd.max_horizontal_speed),
        "max_vspeed_ms": float(fd.max_vertical_speed),
        "photos": int(fd.photo_num),
        "video_s": float(fd.video_time),
        "home_lat": col_max("HOME.latitude") or fd.latitude,
        "home_lon": col_max("HOME.longitude") or fd.longitude,
        "battery_start_pct": float(batt.iloc[0]) if len(batt) else None,
        "battery_end_pct": float(batt.iloc[-1]) if len(batt) else None,
        "battery_min_v": col_min("BATTERY.voltage"),
        "battery_max_temp_c": col_max("BATTERY.temperature"),
        "max_cell_dev_v": col_max("BATTERY.maxCellVoltageDeviation"),
        # Pojedyncze próbki z zerem to krótkie zaniki – min() zaniża ocenę,
        # dlatego 5. percentyl + łączny czas słabego sygnału (klatki co 0.1 s)
        "rc_down_p05_air": col_q("RC.downlinkSignal", 0.05, airborne),
        "rc_up_p05_air": col_q("RC.uplinkSignal", 0.05, airborne),
        "rc_down_weak_s": round(float((airborne["RC.downlinkSignal"] < 30).sum()) * 0.1, 1),
        "gps_p05_sats_air": col_q("OSD.gpsNum", 0.05, airborne),
        "frames": len(df),
        "airborne_frames": len(airborne),
        "app_warnings": int(df["APP.warn"].notna().sum()) if "APP.warn" in df else 0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="importuj wszystko od nowa")
    args = parser.parse_args()

    api_key = get_api_key()
    files = sorted(INBOX.glob("*.txt"))
    if not files:
        sys.exit(f"Brak plików w {INBOX}")

    con = sqlite3.connect(DB_PATH)
    if args.force:
        con.execute("DROP TABLE IF EXISTS frames")
        con.execute("DROP TABLE IF EXISTS flights")

    done: set[str] = set()
    if con.execute("SELECT name FROM sqlite_master WHERE name='flights'").fetchone():
        done = {r[0] for r in con.execute("SELECT flight_id FROM flights")}

    for path in files:
        flight_id = path.stem.removeprefix("FlightRecord_")
        if flight_id in done:
            print(f"pomijam  {flight_id} (już w bazie)")
            continue
        try:
            df, log, fd = decode_frames(path, api_key)
        except Exception as e:  # noqa: BLE001
            print(f"BŁĄD     {flight_id}: {e}")
            continue

        df.insert(0, "flight_id", flight_id)
        summary = summarize(flight_id, df, log, fd)
        df.to_sql("frames", con, if_exists="append", index=False)
        pd.DataFrame([summary]).to_sql("flights", con, if_exists="append", index=False)
        con.commit()
        print(f"OK       {flight_id}: {len(df):>6} klatek, {summary['duration_s'] / 60:5.1f} min")

    con.execute("CREATE INDEX IF NOT EXISTS ix_frames_flight ON frames(flight_id)")
    con.commit()
    con.close()
    print(f"\nBaza: {DB_PATH}")


if __name__ == "__main__":
    main()
