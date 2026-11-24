"""Import DJI flight logs from data/inbox into an SQLite database (data/flights.db).

Usage:  python src/parse.py           # import only new files
        python src/parse.py --force   # rebuild everything from scratch

Tables:
  frames  – one telemetry frame per row (~10 Hz), with a flight_id column
  flights – one row per flight, summary

Data flow:
  .txt file (binary, encrypted)
    → DJILog.from_bytes()   header + Details section (summary written by DJI Fly)
    → fetch_keychains()     AES keys from the DJI API (or local cache, 30 days)
    → log.frames()          decrypted records assembled into frames (~10 per second)
    → export_csv()          frames → in-memory CSV text (~120 named columns)
    → pandas DataFrame      → `frames` table in SQLite
    → summarize()           → one row in the `flights` table
"""

from __future__ import annotations

import argparse  # command-line options (--force)
import io  # StringIO – an in-memory "file", so the CSV never touches the disk
import os  # reading environment variables (API key)
import sqlite3  # single-file database, built into Python
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv  # loads variables from .env into os.environ
from pydjirecord import DJILog, FrameDetails  # DJI flight log format parser
from pydjirecord.export.csv import export_csv  # ready-made frames → CSV exporter

# Paths relative to the project root (src/.. = root directory),
# so the script works regardless of the directory it is run from.
ROOT = Path(__file__).resolve().parent.parent
INBOX = ROOT / "data" / "inbox"  # drop FlightRecord_*.txt files here
DB_PATH = ROOT / "data" / "flights.db"  # output database (ignored by git)


def get_api_key() -> str:
    """Read the DJI API key from the .env file.

    The key is only needed for v13+ logs (AES-encrypted).
    Two variable names are accepted: the standard DJI_API_KEY
    (also used by the pydjirecord CLI) and Dji_moj_API_KEY.
    """
    load_dotenv(ROOT / ".env")
    key = os.environ.get("DJI_API_KEY") or os.environ.get("Dji_moj_API_KEY")
    if not key:
        sys.exit("Brak klucza API: ustaw DJI_API_KEY w pliku .env")
    # Strip surrounding whitespace/newlines and quotes around the value
    return key.strip().strip('"')


def decode_frames(path: Path, api_key: str) -> tuple[pd.DataFrame, DJILog, FrameDetails]:
    """Decode a log file and return its frames as a DataFrame.

    Returns:
      df  – frame table (one row = drone state at a given moment)
      log – parsed log (log.details = summary written by DJI Fly)
      fd  – flight statistics computed by pydjirecord from the frames
    """
    # 1. Read the whole file into memory and parse the header + Details section.
    #    This step needs no key – Details are available right away.
    log = DJILog.from_bytes(path.read_bytes())

    # 2. v13+ logs have AES-256 encrypted records. The file only contains
    #    encrypted key fragments (KeyStorage records) – the library sends them
    #    to the DJI API and gets the real keys back. Telemetry (GPS, route)
    #    is NOT sent. The response is cached locally for 30 days in
    #    ~/.cache/pydjirecord/keychains/, so re-imports work offline.
    keychains = log.fetch_keychains(api_key) if log.version >= 13 else None

    # 3. Decrypt all records and assemble them into frames. The library keeps
    #    a "current state" of the drone (battery, RC, gimbal...) and on every
    #    OSD record (~10 times/s) stores a copy of the whole state as a frame.
    frames = log.frames(keychains)

    # 4. Convert frames to a pandas table via an in-memory CSV: the pydjirecord
    #    exporter already has ~120 well-named columns (OSD.height,
    #    BATTERY.voltage, RC.downlinkSignal...), so we don't rewrite that by
    #    hand. StringIO behaves like a file but lives in RAM – nothing hits disk.
    buf = io.StringIO()
    export_csv(frames, log.details, buf)
    buf.seek(0)  # rewind the buffer so pandas reads from the first line
    # low_memory=False – pandas reads everything at once and infers dtypes better
    df = pd.read_csv(buf, low_memory=False)

    # 5. Flight statistics computed from frames: distance (sum of GPS segments,
    #    haversine), photo count (drop in the SD card's remaining-photos
    #    counter), video time.
    fd = FrameDetails.from_details(log.details, frames)

    # DETAILS.* and RECOVER.* columns have the same value in every frame
    # (e.g. the aircraft serial number) – no point repeating them ~10,000 times.
    # That information goes to the flights table via summarize().
    df = df[[c for c in df.columns if not c.startswith(("DETAILS.", "RECOVER."))]]

    # Date text → real datetime type (UTC). errors="coerce" turns invalid
    # values into NaT instead of aborting the import.
    df["CUSTOM.dateTime"] = pd.to_datetime(df["CUSTOM.dateTime"], utc=True, errors="coerce")
    return df, log, fd


def summarize(flight_id: str, df: pd.DataFrame, log: DJILog, fd: FrameDetails) -> dict:
    """Compute the summary of one flight (one row of the flights table)."""
    d = log.details  # summary written by the DJI Fly app

    # Only frames where the drone was airborne – signal or GPS on the ground
    # (before takeoff / after landing) say nothing about flight quality.
    airborne = df[df["OSD.isOnGround"] == False]  # noqa: E712

    # Battery level in %; zeros are skipped – at the start of the log, before
    # the battery reports its first reading, the column is often empty or 0.
    batt = df["BATTERY.chargeLevel"].dropna()
    batt = batt[batt > 0]

    # Helpers: min / max / percentile of a column, ignoring missing values.
    # They return None when the column is absent or has no data (e.g. the
    # drone doesn't report that field) – so the import never crashes on it.
    def col_min(name: str, frame: pd.DataFrame = df):
        s = frame[name].dropna() if name in frame else pd.Series(dtype=float)
        return float(s.min()) if len(s) else None

    def col_max(name: str, frame: pd.DataFrame = df):
        s = frame[name].dropna() if name in frame else pd.Series(dtype=float)
        return float(s.max()) if len(s) else None

    def col_q(name: str, q: float, frame: pd.DataFrame = df):
        # q=0.05 → 5th percentile: the value below which only 5% of samples fall
        s = frame[name].dropna() if name in frame else pd.Series(dtype=float)
        return float(s.quantile(q)) if len(s) else None

    return {
        # --- flight and hardware identification (from Details) ---
        "flight_id": flight_id,
        "start_time": str(d.start_time) if getattr(d, "start_time", None) else None,  # UTC
        # Lito X1 is unknown to the library (UNKNOWN_165), hence the name from the log
        "aircraft": d.aircraft_name or d.product_type.name,
        "aircraft_sn": d.aircraft_sn,
        "battery_sn": getattr(d, "battery_sn", None),  # tells batteries apart
        "log_version": log.version,
        # --- flight parameters ---
        # duration and maxima: values written by DJI Fly in the Details section
        "duration_s": float(fd.total_time),
        # distance: sum of distances between consecutive GPS points (haversine)
        "distance_m": float(fd.total_distance),
        "max_height_m": float(fd.max_height),  # height relative to the takeoff point
        "max_hspeed_ms": float(fd.max_horizontal_speed),
        "max_vspeed_ms": float(fd.max_vertical_speed),
        # photos: difference of the SD card "photos remaining" counter (start − end)
        "photos": int(fd.photo_num),
        # video: total length of all recordings during the flight
        "video_s": float(fd.video_time),
        # takeoff point; falls back to Details coordinates if missing in frames.
        # NOTE: max() is only correct if the Home point doesn't change mid-flight
        "home_lat": col_max("HOME.latitude") or fd.latitude,
        "home_lon": col_max("HOME.longitude") or fd.longitude,
        # --- battery ---
        "battery_start_pct": float(batt.iloc[0]) if len(batt) else None,  # first reading
        "battery_end_pct": float(batt.iloc[-1]) if len(batt) else None,  # last reading
        "battery_min_v": col_min("BATTERY.voltage"),  # lowest voltage (under load)
        "battery_max_temp_c": col_max("BATTERY.temperature"),
        # largest voltage difference between cells – a growing value can indicate wear
        "max_cell_dev_v": col_max("BATTERY.maxCellVoltageDeviation"),
        # --- link and GPS (airborne only) ---
        # Single zero samples are short dropouts – min() would understate quality,
        # so we use the 5th percentile + total weak-signal time (frames every 0.1 s)
        # WARNING: for Lito X1 (unknown to pydjirecord as UNKNOWN_165) these values
        # look misdecoded – they come from the OFDM record (7 bits value + 1 bit
        # direction), reach up to 127 instead of 0–100 and jump around a constant 90
        # even when the drone hovers next to the pilot. Treat rc_* columns as
        # unreliable until the record layout for this model is confirmed.
        "rc_down_p05_air": col_q("RC.downlinkSignal", 0.05, airborne),  # drone → remote
        "rc_up_p05_air": col_q("RC.uplinkSignal", 0.05, airborne),  # remote → drone
        # number of frames with signal < 30, times 0.1 s (assumes ~10 frames per second)
        "rc_down_weak_s": round(float((airborne["RC.downlinkSignal"] < 30).sum()) * 0.1, 1),
        "gps_p05_sats_air": col_q("OSD.gpsNum", 0.05, airborne),  # satellite count
        # --- sanity checks ---
        "frames": len(df),  # all frames (including on the ground)
        "airborne_frames": len(airborne),  # frames in the air
        # number of frames with an app warning (a message persists over several frames)
        "app_warnings": int(df["APP.warn"].notna().sum()) if "APP.warn" in df else 0,
    }


def main() -> None:
    # Command-line options; the --help description is the docstring's first line
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="importuj wszystko od nowa")
    args = parser.parse_args()

    api_key = get_api_key()
    files = sorted(INBOX.glob("*.txt"))  # sorting by name = chronological order
    if not files:
        sys.exit(f"Brak plików w {INBOX}")

    # Connect to the database – the file is created if it doesn't exist
    con = sqlite3.connect(DB_PATH)
    if args.force:
        # Rebuild from scratch: drop old tables (pandas recreates them)
        con.execute("DROP TABLE IF EXISTS frames")
        con.execute("DROP TABLE IF EXISTS flights")

    # Set of flights already in the database – so they aren't imported twice.
    # On the very first run the flights table doesn't exist yet.
    done: set[str] = set()
    if con.execute("SELECT name FROM sqlite_master WHERE name='flights'").fetchone():
        done = {r[0] for r in con.execute("SELECT flight_id FROM flights")}

    for path in files:
        # Flight ID = file name without "FlightRecord_", e.g. "2026-08-25_[19-12-02]"
        # (the time in the name is the phone's local time; start_time in the DB is UTC)
        flight_id = path.stem.removeprefix("FlightRecord_")
        if flight_id in done:
            print(f"pomijam  {flight_id} (już w bazie)")
            continue
        try:
            df, log, fd = decode_frames(path, api_key)
        except Exception as e:  # noqa: BLE001
            # A corrupted file / API error doesn't stop importing the other flights
            print(f"BŁĄD     {flight_id}: {e}")
            continue

        # flight_id column first – lets you join frames with flights
        df.insert(0, "flight_id", flight_id)
        summary = summarize(flight_id, df, log, fd)

        # Append frames and the summary to the database. if_exists="append"
        # adds rows (for the first flight pandas creates the table itself).
        df.to_sql("frames", con, if_exists="append", index=False)
        pd.DataFrame([summary]).to_sql("flights", con, if_exists="append", index=False)
        # Commit after each flight: an interrupted import keeps the flights saved so far
        con.commit()
        print(f"OK       {flight_id}: {len(df):>6} klatek, {summary['duration_s'] / 60:5.1f} min")

    # Index on flight_id – fast selection of a single flight's frames
    # (e.g. SELECT ... FROM frames WHERE flight_id = '...')
    con.execute("CREATE INDEX IF NOT EXISTS ix_frames_flight ON frames(flight_id)")
    con.commit()
    con.close()
    print(f"\nBaza: {DB_PATH}")


# Run main() only when started as a script (python src/parse.py),
# not when imported from another file / notebook (from parse import decode_frames)
if __name__ == "__main__":
    main()
