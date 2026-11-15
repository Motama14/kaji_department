# SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
"""Central, parameterized configuration for the harmonization pipeline (global data).

Ported from the preparation project and adapted: the raw data is now global and
organised by sensor folder (FIRMS product code) instead of by country. Everything
that may change (paths, thresholds, periods, grid) lives here so the rest of the
code does not need edits. Values can be overridden with environment variables
where noted.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("FIRE_DATA_DIR", ROOT / "data"))
PROCESSED_DIR = Path(os.getenv("FIRE_PROCESSED_DIR", DATA_DIR / "processed"))
# The 10.8 GB detections table can live apart from the small serving tables: on Cloud Run the small tables are in
# the image (FIRE_PROCESSED_DIR) and the detections stay on the bucket mount (FIRE_HOTSPOTS_DIR=/data/hotspots).
HOTSPOTS_DIR = Path(os.getenv("FIRE_HOTSPOTS_DIR", PROCESSED_DIR / "hotspots"))
FIGURES_DIR = ROOT / "docs" / "figures"
# DuckDB spill folder; FIRE_TMP_DIR moves it off the data folder (read-only bucket on Cloud Run).
TMP_DIR = Path(os.getenv("FIRE_TMP_DIR", PROCESSED_DIR / "_duckdb_tmp"))

DUCKDB_MEMORY_LIMIT = os.getenv("DUCKDB_MEMORY_LIMIT", "2GB")
DUCKDB_THREADS = int(os.getenv("DUCKDB_THREADS", "4"))


@dataclass(frozen=True)
class Source:
    """One raw folder = one FIRMS product (the sensor is taken from here, never from the CSV)."""
    folder: str
    code: str
    sensor: str
    satellites: dict[str, str]
    label: str


SOURCES: dict[str, Source] = {
    "MODIS": Source("MODIS", "M-C61", "MODIS", {"Terra": "Terra", "Aqua": "Aqua"}, "MODIS Terra + Aqua"),
    "SV_C2": Source("SV_C2", "SV-C2", "VIIRS", {"SNPP": "N"}, "VIIRS S-NPP"),
    "J1V_C2": Source("J1V_C2", "J1V-C2", "VIIRS", {"N20": "N20"}, "VIIRS NOAA-20"),
    "J2V_C2": Source("J2V_C2", "J2V-C2", "VIIRS", {"N21": "N21"}, "VIIRS NOAA-21"),
}

SATELLITES: dict[str, str] = {"Terra": "MODIS", "Aqua": "MODIS", "N": "VIIRS", "N20": "VIIRS", "N21": "VIIRS"}

@dataclass(frozen=True)
class Exclusion:
    """Non-vegetation event mislabelled as type=0 by FIRMS (e.g. lava outside the static volcano mask)."""
    name: str
    bbox: tuple[float, float, float, float]
    start: str | None
    end: str | None


# Lava flows and lava lakes that FIRMS labels type=0 (vegetation fire) because they lie outside its static
# volcano mask. Mostly a VIIRS-era effect (375 m pixels see the flow fields). Boxes and windows were set from
# the monthly type-0 counts in each box (step 5, see docs/DATA.md); counts are clean type-0 detections removed.
EXCLUSIONS: list[Exclusion] = [
    # Cumbre Vieja, La Palma (28.3k).
    Exclusion("la_palma_2021", (-18.05, 28.45, -17.75, 28.75), "2021-09-19", "2021-12-31"),
    # Tolbachik, Kamchatka, fissure eruption (1.3k).
    Exclusion("tolbachik_2012", (160.2, 55.6, 160.6, 55.9), "2012-11-27", "2013-09-30"),
    # Holuhraun, Iceland, including the cooling flow field until April 2015 (55.5k).
    Exclusion("holuhraun_2014", (-17.0, 64.8, -16.5, 65.0), "2014-08-29", "2015-04-30"),
    # Kilauea summit, East Rift and lower Puna: lava almost every year since 2012 (VIIRS 105.9k vs MODIS 0.3k).
    Exclusion("kilauea", (-155.35, 19.25, -154.80, 19.55), None, None),
    # Nyiragongo lava lake (whole record) and the May 2021 flank flows towards Goma.
    Exclusion("nyiragongo_crater", (29.22, -1.55, 29.28, -1.49), None, None),
    Exclusion("nyiragongo_2021", (29.15, -1.70, 29.30, -1.50), "2021-05-22", "2021-05-31"),
    # Reykjanes peninsula eruptions (Fagradalsfjall, Sundhnukur), from the first one on (57.8k).
    Exclusion("reykjanes", (-22.60, 63.80, -22.20, 63.95), "2021-03-19", None),
    # Mauna Loa 2022 and its cooling flow until Nov 2023; north edge at 19.70 keeps the Pohakuloa range fires.
    Exclusion("mauna_loa_2022", (-155.75, 19.40, -155.40, 19.70), "2022-11-27", "2023-11-30"),
]


def exclusion_sql_one(e: Exclusion, lat: str = "lat", lon: str = "lon", date: str = "date") -> str:
    """SQL boolean expression that is TRUE for detections inside one exclusion."""
    c = f"{lon} BETWEEN {e.bbox[0]} AND {e.bbox[2]} AND {lat} BETWEEN {e.bbox[1]} AND {e.bbox[3]}"
    if e.start:
        c += f" AND {date} >= DATE '{e.start}'"
    if e.end:
        c += f" AND {date} <= DATE '{e.end}'"
    return f"({c})"


def exclusion_sql(lat: str = "lat", lon: str = "lon", date: str = "date") -> str:
    """SQL boolean expression that is TRUE for detections inside any exclusion."""
    if not EXCLUSIONS:
        return "FALSE"
    return "(" + " OR ".join(exclusion_sql_one(e, lat, lon, date) for e in EXCLUSIONS) + ")"


@dataclass(frozen=True)
class Settings:
    grid_deg: float = float(os.getenv("GRID_DEG", "0.25"))
    # Fire types kept in the burn calendar (0 = presumed vegetation fire).
    calendar_types: tuple[int, ...] = (0,)
    # MODIS numeric confidence threshold; VIIRS uses the normalized category.
    modis_min_confidence: int = int(os.getenv("MODIS_MIN_CONF", "30"))
    viirs_min_confidence: tuple[str, ...] = ("nominal", "high")
    # Periods (inclusive years), decided in step 6: validation = the last two years before the orbit
    # drift shows in the data (Terra from 2022, Aqua night counts from 2025); 2022+ analysed separately.
    calib_years: tuple[int, int] = (2012, 2019)
    valid_years: tuple[int, int] = (2020, 2021)
    drift_years: tuple[int, int] = (2022, 2026)
    # Terra -> Aqua relation: fitted from Aqua's start (2002-07) to 2019, validated on 2020-2021.
    terra_aqua_years: tuple[int, int] = (2002, 2019)
    terra_aqua_valid_years: tuple[int, int] = (2020, 2021)
    # Aqua climatology feature (mean counts per cell and calendar month), pre-drift years only.
    climatology_years: tuple[int, int] = (2002, 2021)
    # NOAA-20 / NOAA-21 -> S-NPP day/night ratios, fitted on months where both have no missing days.
    n20_ratio_years: tuple[int, int] = (2018, 2025)
    n21_ratio_years: tuple[int, int] = (2023, 2025)
    # Harmonized output keeps cell-months above this value or with any raw detection.
    harmonized_min_value: float = 0.01
    # First month with full VIIRS S-NPP coverage (data start 2012-01-20).
    viirs_start: str = "2012-02-01"
    # Empirical-Bayes shrinkage strength (pseudo-counts of Aqua detections).
    shrink_k: float = 20.0
    # Parquet output of the adapter: small row groups, so that day queries skip most of a file.
    parquet_row_group_size: int = 100_000
    parquet_compression: str = "zstd"
    # NRT rows have no `type`: it is inferred from the SP history. A 0.01 deg cell (rounded lat/lon) with at
    # least `static_mask_min_detections` SP detections of type 1/2/3 makes NRT rows there that type (step 5).
    static_mask_deg: float = 0.01
    static_mask_min_detections: int = 1
    # Calendar latitude range: outside it, type-0 detections are false alarms (Antarctica, high Arctic).
    calendar_lat_range: tuple[float, float] = (-60.0, 80.0)
    # FRP below this is clipped in the FRP sums (FIRMS has a few zero and negative values).
    frp_clip_min: float = 0.0
    extra: dict = field(default_factory=dict)


SETTINGS = Settings()


def sql_path(p: Path | str) -> str:
    """Path as a SQL string literal body (forward slashes, quotes escaped)."""
    return Path(p).as_posix().replace("'", "''")


def hotspots_dir() -> Path:
    """Root of the single detections table (hive-partitioned Parquet); FIRE_HOTSPOTS_DIR overrides it."""
    return HOTSPOTS_DIR


def hotspots_sql() -> str:
    """FROM-clause expression that reads the whole detections table."""
    return (
        f"read_parquet('{sql_path(hotspots_dir())}/**/*.parquet', hive_partitioning=true, "
        "hive_types={'sensor': VARCHAR, 'satellite': VARCHAR, 'year': SMALLINT})"
    )


def agg_path(grid_deg: float | None = None) -> Path:
    g = grid_deg or SETTINGS.grid_deg
    return PROCESSED_DIR / f"cell_month_g{g:g}.parquet"


def static_mask_path() -> Path:
    """0.01 deg cells with SP static-source history, used to infer `type` for NRT rows."""
    return PROCESSED_DIR / f"static_mask_d{SETTINGS.static_mask_deg:g}.parquet"


def coverage_path() -> Path:
    """Observed, missing and low days per satellite x month."""
    return PROCESSED_DIR / "coverage_sat_month.parquet"


def harmonized_path(grid_deg: float | None = None) -> Path:
    g = grid_deg or SETTINGS.grid_deg
    return PROCESSED_DIR / f"harmonized_g{g:g}.parquet"


def connect(tmp_dir: Path | None = None):
    """In-memory DuckDB connection with conservative resource limits (spills to ``tmp_dir``)."""
    import duckdb

    tmp = tmp_dir or TMP_DIR
    tmp.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(":memory:")
    con.execute(f"SET memory_limit='{DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET threads={DUCKDB_THREADS}")
    con.execute(f"SET temp_directory='{sql_path(tmp)}'")
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET enable_progress_bar=false")
    return con
