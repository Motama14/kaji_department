# SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
"""Step 7 - calibration MODIS -> VIIRS-equivalent and the harmonized series (global).

Ported from the preparation project (v2 method: day/night models, LightGBM Poisson blended with per-cell
ratios, selection by 1-degree block WAPE) and adapted to the global data and the step-6 decisions
(docs/DECISIONS.md):
  * One global model. Metrics globally and per latitude band.
  * Target series: S-NPP-equivalent counts per 0.25 deg cell and month, day and night modelled separately.
  * Aqua -> S-NPP fitted on calib_years (2012-2019) and validated on valid_years (2020-2021, the last years
    before the orbit drift shows in the data). The production model is the calibration model, so the
    validation metrics are those of the model that builds the series.
  * Terra -> Aqua per-cell ratios fitted on 2002-07..2019, validated on 2020-2021, used for 2000-11..2002-06.
  * NOAA-20 / NOAA-21 only fill months where S-NPP has missing days, scaled to S-NPP (day/night ratios).
  * Outages: counts become full-month equivalents with the coverage table (exposure e = observed days /
    expected days). The Poisson fit uses target n/e with weight e, the same likelihood as an offset log(e).
  * The dense training panel (every cell x month, zeros included) is held in memory as float32 arrays.

Harmonized series per cell-month (column `source`):
    VIIRS_SNPP           month >= viirs_start and S-NPP has no missing day: observed S-NPP
    VIIRS_N20_SCALED     S-NPP has missing days, NOAA-20 complete: NOAA-20 x day/night ratio
    VIIRS_N21_SCALED     same with NOAA-21
    VIIRS_EXPOSURE       no complete VIIRS satellite: the one with most days, scaled by its exposure
    MODIS_AQUA_MODEL     2002-07 .. viirs_start: selected model applied to Aqua
    MODIS_TERRA_MODEL    before 2002-07: selected model applied to the Terra -> Aqua estimate
`type_inferred` marks cell-months that contain NRT detections whose type was calculated (step 5).

The series is built with PRODUCTION_MODEL (lgbm_dn, user decision in step 7); every candidate is still validated.

Run from the repo root:
    .venv/Scripts/python -m pipeline.calibrate run            # fit, validate, build the series, figures
    .venv/Scripts/python -m pipeline.calibrate produce        # series again with the saved boosters (no refit)
    .venv/Scripts/python -m pipeline.calibrate figures        # figures again from the saved outputs
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import time

import numpy as np
import pandas as pd

from . import config

S = config.SETTINGS
DN = ("d", "n")
SAT_KEYS = ("terra", "aqua", "snpp", "n20", "n21")
COV_SAT = {"terra": "Terra", "aqua": "Aqua", "snpp": "N", "n20": "N20", "n21": "N21"}
MODELS = ["raw", "ratio", "ratio_dn", "cell_ratio_dn", "lgbm_dn", "blend_dn"]
SOURCES = ["VIIRS_SNPP", "VIIRS_N20_SCALED", "VIIRS_N21_SCALED", "VIIRS_EXPOSURE", "MODIS_AQUA_MODEL",
           "MODIS_TERRA_MODEL"]
AQUA_START = dt.date(2002, 7, 1)
VIIRS_START = dt.date.fromisoformat(S.viirs_start)
FEATURES = ["log_a_d", "log_a_n", "log_frp_d", "log_frp_n", "log_nb_d", "log_nb_n", "log_clim_d", "log_clim_n",
            "month_f", "lat", "lon"]
LAT_BANDS = [(-60, -30, "60S-30S"), (-30, -10, "30S-10S"), (-10, 10, "10S-10N"), (10, 30, "10N-30N"),
             (30, 50, "30N-50N"), (50, 81, "50N-80N")]
# max_delta_step caps each leaf output (log space). Without it the night model diverged on the global panel: the
# Poisson Newton step grows linearly with y / mean (night mean 1.9, top rows about 4,000 detections), so the first
# trees pushed some predictions to exp(20+) and 600 rounds were not enough to recover (docs/DECISIONS.md, step 7).
LGB_PARAMS = dict(objective="poisson", learning_rate=0.03, num_leaves=63, min_data_in_leaf=50,
                  bagging_fraction=0.8, bagging_freq=1, feature_fraction=0.9, lambda_l2=1.0, max_delta_step=2.0,
                  num_threads=config.DUCKDB_THREADS, verbose=-1, seed=0)
MAX_INSAMPLE_BIAS_PCT = 5.0   # an in-sample total bias above this means the LightGBM fit did not converge
# Model that builds the series (user decision, step 7, docs/DECISIONS.md): LightGBM alone. It ties with blend_dn on
# validation block WAPE (0.225 vs 0.224) and has the lower total bias (-2.8 % vs -3.4 %).
PRODUCTION_MODEL = "lgbm_dn"
OUT_DIR = config.PROCESSED_DIR / "calibration"
RESULTS = OUT_DIR / "results.json"
SERIES = OUT_DIR / "series_month.parquet"
VALID_BLOCKS = OUT_DIR / "valid_blocks.parquet"
OFFSETS = ", ".join(f"({dy}, {dx})" for dy in (-1, 0, 1) for dx in (-1, 0, 1) if (dy, dx) != (0, 0))
# Columns fetched for the calibration and validation panels (the production panel takes all of them).
TRAIN_COLS = ["cell_id", "lat", "lon", "year", "month", "e_snpp", "n_snpp_d", "n_snpp_n", "src_d", "src_n"] + \
             [f for f in FEATURES if f not in ("lat", "lon")]


def log(msg: str) -> None:
    print(f"{dt.datetime.now():%H:%M:%S} {msg}", flush=True)


def ym(year, month):
    return np.asarray(year, dtype=np.int32) * 12 + np.asarray(month, dtype=np.int32) - 1


def lit(x: float) -> str:
    """Float literal for SQL (DuckDB would read a long decimal literal as DECIMAL and overflow)."""
    return f"CAST({float(x)!r} AS DOUBLE)"


def first_of_month(y: int, m: int = 1) -> str:
    return f"{y:04d}-{m:02d}-01"


def fetch(con, sql: str) -> dict[str, np.ndarray]:
    """Query result as numpy arrays (NULL -> NaN for floats).

    Goes through Arrow: DuckDB 2.0.0 `fetchnumpy()` raised an internal error ("dict-surviving cache") on
    these joined panels.
    """
    tbl = con.sql(sql).to_arrow_table()
    out = {}
    for name in tbl.column_names:
        col = tbl.column(name)
        if col.null_count and not (col.type.equals("float") or col.type.equals("double")):
            raise SystemExit(f"Unexpected NULLs in column {name}")
        out[name] = col.to_numpy()
    return out


# ----------------------------------------------------------------------------- tables
def prepare(con, grid: float) -> dict:
    """Temp tables: cov (exposure per month), cells, cmx (cell table with ids), clim (Aqua climatology)."""
    agg = config.sql_path(config.agg_path(grid))
    cov_path = config.sql_path(config.coverage_path())
    hot = config.hotspots_sql()
    last = con.execute(f"SELECT max(date) FROM {hot} WHERE year = (SELECT max(year) FROM '{cov_path}')").fetchone()[0]
    obs = ", ".join(f"coalesce(sum(obs_days) FILTER (WHERE satellite = '{s}'), 0) AS obs_{k}" for k, s in COV_SAT.items())
    exp = ", ".join(f"obs_{k} / expected_days AS e_{k}" for k in SAT_KEYS)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE cov AS
        SELECT *, {exp} FROM (
            SELECT year, month,
                   CASE WHEN year = {last.year} AND month = {last.month} THEN {last.day} ELSE max(days_in_month) END AS expected_days,
                   {obs}
            FROM '{cov_path}' GROUP BY year, month)""")
    c0, v1 = S.calib_years[0], S.valid_years[1]
    con.execute(f"""CREATE OR REPLACE TEMP TABLE cells AS
        SELECT CAST(row_number() OVER (ORDER BY cell_lat, cell_lon) AS INTEGER) AS cell_id, cell_lat, cell_lon,
               CAST(round(cell_lat / {grid}) AS INTEGER) AS iy, CAST(round(cell_lon / {grid}) AS INTEGER) AS ix,
               bool_or(year BETWEEN {c0} AND {v1} AND n_aqua + n_snpp > 0) AS overlap_cell
        FROM '{agg}' GROUP BY cell_lat, cell_lon""")
    counts = ", ".join(f"a.n_{k}_{d}" for k in SAT_KEYS for d in DN)
    frps = ", ".join(f"a.frp_sum_{k}_{d}" for k in ("terra", "aqua") for d in DN)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE cmx AS
        SELECT c.cell_id, a.year, a.month, {counts}, {frps}, a.n_type_inferred
        FROM '{agg}' a JOIN cells c USING (cell_lat, cell_lon)""")
    y0, y1 = S.climatology_years
    con.execute(f"""CREATE OR REPLACE TEMP TABLE clim AS
        WITH m AS (SELECT year, month, e_aqua FROM cov
                   WHERE make_date(year, month, 1) BETWEEN DATE '{AQUA_START}' AND DATE '{first_of_month(y1, 12)}'
                     AND e_aqua > 0),
             nm AS (SELECT month, count(*) AS n FROM m GROUP BY month)
        SELECT a.cell_id, a.month,
               CAST(sum(a.n_aqua_d / m.e_aqua) / any_value(nm.n) AS FLOAT) AS clim_d,
               CAST(sum(a.n_aqua_n / m.e_aqua) / any_value(nm.n) AS FLOAT) AS clim_n
        FROM cmx a JOIN m USING (year, month) JOIN nm USING (month)
        GROUP BY a.cell_id, a.month""")
    n_cells, n_overlap, max_id = con.execute(
        "SELECT count(*), count(*) FILTER (WHERE overlap_cell), max(cell_id) FROM cells").fetchone()
    info = {"last_date": last.isoformat(), "cells": n_cells, "overlap_cells": n_overlap, "max_cell_id": max_id}
    log(f"prepare: {n_cells:,} cells ({n_overlap:,} with Aqua or S-NPP in {c0}-{v1}), data up to {last}")
    return info


def nb_sql(t0: str, t1: str) -> str:
    """3x3 neighbourhood sums (cell excluded) of Aqua and Terra counts."""
    # Aggregate on the shifted grid key first, then one equi-join to the cell ids (a join on
    # `c2.iy = c1.iy + o.dy` after a cross join made DuckDB fall back to a nested-loop join).
    return f"""SELECT c2.cell_id, s.year, s.month, s.nb_aqua_d, s.nb_aqua_n, s.nb_terra_d, s.nb_terra_n
        FROM (SELECT c1.iy + o.dy AS iy, c1.ix + o.dx AS ix, a.year, a.month,
                     sum(a.n_aqua_d) AS nb_aqua_d, sum(a.n_aqua_n) AS nb_aqua_n,
                     sum(a.n_terra_d) AS nb_terra_d, sum(a.n_terra_n) AS nb_terra_n
              FROM cmx a JOIN cells c1 USING (cell_id) CROSS JOIN (VALUES {OFFSETS}) o(dy, dx)
              WHERE make_date(a.year, a.month, 1) BETWEEN DATE '{t0}' AND DATE '{t1}'
              GROUP BY ALL) s
        JOIN cells c2 USING (iy, ix)"""


def panel_sql(t0: str, t1: str, ta: dict, cell_filter: str = "TRUE", month_filter: str = "TRUE") -> str:
    """Dense panel (cells x months) with model features computed in SQL, all as float32."""
    raw = ", ".join(f"coalesce(a.n_{k}_{d}, 0) AS n_{k}_{d}" for k in SAT_KEYS for d in DN)
    frp = ", ".join(f"coalesce(a.frp_sum_{k}_{d}, 0) AS frp_{k}_{d}" for k in ("terra", "aqua") for d in DN)
    nbs = ", ".join(f"coalesce(nb.nb_{k}_{d}, 0) AS nb_{k}_{d}" for k in ("terra", "aqua") for d in DN)
    src = []
    for d in DN:
        src += [
            f"CASE WHEN has_aqua THEN n_aqua_{d} / nullif(e_aqua, 0) "
            f"ELSE ta_r_{d} * n_terra_{d} / nullif(e_terra, 0) END AS src_{d}",
            f"CASE WHEN has_aqua THEN frp_aqua_{d} / nullif(n_aqua_{d}, 0) "
            f"ELSE frp_terra_{d} / nullif(n_terra_{d}, 0) END AS frpm_{d}",
            f"CASE WHEN has_aqua THEN nb_aqua_{d} / nullif(e_aqua, 0) "
            f"ELSE {lit(ta['ratio_' + d])} * nb_terra_{d} / nullif(e_terra, 0) END AS srcnb_{d}",
        ]
    feats = []
    for d in DN:
        feats += [f"CAST(src_{d} AS FLOAT) AS src_{d}",
                  f"CAST(ln(1 + src_{d}) AS FLOAT) AS log_a_{d}",
                  f"CAST(ln(1 + coalesce(frpm_{d}, 0)) AS FLOAT) AS log_frp_{d}",
                  f"CAST(ln(1 + srcnb_{d}) AS FLOAT) AS log_nb_{d}",
                  f"CAST(ln(1 + clim_{d}) AS FLOAT) AS log_clim_{d}"]
    keep_raw = ", ".join(f"CAST(n_{k}_{d} AS FLOAT) AS n_{k}_{d}" for k in SAT_KEYS for d in DN)
    return f"""
        WITH m AS (SELECT * FROM cov WHERE make_date(year, month, 1) BETWEEN DATE '{t0}' AND DATE '{t1}' AND ({month_filter})),
             nb AS ({nb_sql(t0, t1)}),
             p AS (
                SELECT c.cell_id, c.cell_lat, c.cell_lon, m.year, m.month,
                       make_date(m.year, m.month, 1) >= DATE '{AQUA_START}' AS has_aqua,
                       m.e_terra, m.e_aqua, m.e_snpp, m.e_n20, m.e_n21, {raw}, {frp}, {nbs},
                       coalesce(cl.clim_d, 0) AS clim_d, coalesce(cl.clim_n, 0) AS clim_n,
                       coalesce(ta.r_d, {lit(ta['ratio_d'])}) AS ta_r_d, coalesce(ta.r_n, {lit(ta['ratio_n'])}) AS ta_r_n,
                       coalesce(a.n_type_inferred, 0) AS n_type_inferred
                FROM cells c CROSS JOIN m
                LEFT JOIN cmx a ON a.cell_id = c.cell_id AND a.year = m.year AND a.month = m.month
                LEFT JOIN nb ON nb.cell_id = c.cell_id AND nb.year = m.year AND nb.month = m.month
                LEFT JOIN clim cl ON cl.cell_id = c.cell_id AND cl.month = m.month
                LEFT JOIN ta_ratio ta ON ta.cell_id = c.cell_id
                WHERE {cell_filter}),
             s AS (SELECT *, {', '.join(src)} FROM p)
        SELECT cell_id, CAST(cell_lat AS FLOAT) AS lat, CAST(cell_lon AS FLOAT) AS lon,
               CAST(year AS SMALLINT) AS year, CAST(month AS TINYINT) AS month, has_aqua,
               CAST(e_snpp AS FLOAT) AS e_snpp, CAST(e_n20 AS FLOAT) AS e_n20, CAST(e_n21 AS FLOAT) AS e_n21,
               {keep_raw}, CAST(n_type_inferred AS INTEGER) AS n_type_inferred,
               {', '.join(feats)}, CAST(month AS FLOAT) AS month_f
        FROM s"""


# ----------------------------------------------------------------------------- ratio fits (SQL)
def months_sql(t0: str, t1: str, cond: str) -> str:
    return f"SELECT * FROM cov WHERE make_date(year, month, 1) BETWEEN DATE '{t0}' AND DATE '{t1}' AND {cond}"


def fit_terra_to_aqua(con) -> dict:
    """Per-cell day/night Aqua/Terra ratios (shrunk to the global ones), Terra scaled to Aqua's exposure."""
    t1 = first_of_month(S.terra_aqua_years[1], 12)
    sums = ", ".join(f"sum(a.n_aqua_{d}) AS a_{d}, sum(a.n_terra_{d} / m.e_terra * m.e_aqua) AS t_{d}" for d in DN)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE ta_sums AS
        SELECT a.cell_id, {sums} FROM cmx a
        JOIN ({months_sql(str(AQUA_START), t1, 'e_terra > 0 AND e_aqua > 0')}) m USING (year, month)
        GROUP BY a.cell_id""")
    r = [float(v) for v in con.execute("SELECT sum(a_d) / sum(t_d), sum(a_n) / sum(t_n) FROM ta_sums").fetchone()]
    k = lit(S.shrink_k)
    rd, rn = lit(r[0]), lit(r[1])
    con.execute(f"""CREATE OR REPLACE TEMP TABLE ta_ratio AS
        SELECT cell_id, (a_d + {k} * {rd}) / (t_d + {k}) AS r_d, (a_n + {k} * {rn}) / (t_n + {k}) AS r_n
        FROM ta_sums""")
    ta = {"ratio_d": float(r[0]), "ratio_n": float(r[1]), "fit_period": [str(AQUA_START), t1]}
    # Validation (2020-2021): predicted Aqua = cell ratio x Terra (exposure-matched) vs observed Aqua.
    v0, v1 = S.terra_aqua_valid_years
    fr = con.execute(f"""
        SELECT c.cell_lat AS lat, c.cell_lon AS lon, a.year, a.month,
               CAST(a.n_aqua_d + a.n_aqua_n AS DOUBLE) AS obs,
               coalesce(t.r_d, {rd}) * a.n_terra_d / m.e_terra * m.e_aqua
                 + coalesce(t.r_n, {rn}) * a.n_terra_n / m.e_terra * m.e_aqua AS pred,
               {rd} * a.n_terra_d / m.e_terra * m.e_aqua + {rn} * a.n_terra_n / m.e_terra * m.e_aqua AS pred_global
        FROM cmx a JOIN cells c USING (cell_id) LEFT JOIN ta_ratio t USING (cell_id)
        JOIN ({months_sql(first_of_month(v0), first_of_month(v1, 12), 'e_terra > 0 AND e_aqua > 0')}) m USING (year, month)
        WHERE a.n_aqua_d + a.n_aqua_n + a.n_terra_d + a.n_terra_n > 0""").df()
    ta["valid_cell_ratio"] = metrics(fr.lat, fr.lon, fr.year, fr.month, fr.obs, fr.pred)
    ta["valid_global_ratio"] = metrics(fr.lat, fr.lon, fr.year, fr.month, fr.obs, fr.pred_global)
    log(f"terra->aqua: ratio day {r[0]:.3f}, night {r[1]:.3f}; validation {v0}-{v1} block WAPE "
        f"{ta['valid_cell_ratio']['block1deg_month_wape']:.3f} (global ratio "
        f"{ta['valid_global_ratio']['block1deg_month_wape']:.3f}), total bias {ta['valid_cell_ratio']['total_bias_pct']:+.1f} %")
    return ta


def fit_viirs_fill(con) -> dict:
    """Global day/night ratios S-NPP / NOAA-20 and S-NPP / NOAA-21 on months where both are complete."""
    out = {}
    for key, (y0, y1) in (("n20", S.n20_ratio_years), ("n21", S.n21_ratio_years)):
        rows = con.execute(f"""
            SELECT a.year, sum(a.n_snpp_d), sum(a.n_{key}_d), sum(a.n_snpp_n), sum(a.n_{key}_n)
            FROM cmx a JOIN ({months_sql(first_of_month(y0), first_of_month(y1, 12), f'e_snpp = 1 AND e_{key} = 1')}) m
              USING (year, month)
            GROUP BY ALL ORDER BY 1""").fetchall()
        sd, kd, sn, kn = (sum(r[i] for r in rows) for i in (1, 2, 3, 4))
        out[key] = {"ratio_d": sd / kd, "ratio_n": sn / kn, "years": [y0, y1],
                    "per_year": {r[0]: [r[1] / r[2], r[3] / r[4]] for r in rows}}
        log(f"viirs fill: S-NPP / {key.upper()} day {sd / kd:.3f}, night {sn / kn:.3f} "
            f"(per year day {min(v[0] for v in out[key]['per_year'].values()):.3f}-"
            f"{max(v[0] for v in out[key]['per_year'].values()):.3f})")
    return out


class AquaToViirs:
    """Candidate models Aqua -> S-NPP (full-month equivalents, day and night)."""

    def __init__(self, con, max_cell_id: int):
        self.con = con
        self.max_id = max_cell_id

    def fit_ratios(self):
        t1 = first_of_month(S.calib_years[1], 12)
        sums = ", ".join(f"CAST(sum(a.n_snpp_{d}) AS DOUBLE) AS v_{d}, sum(a.n_aqua_{d} / m.e_aqua * m.e_snpp) AS a_{d}"
                         for d in DN)
        rows = self.con.execute(f"""
            SELECT a.cell_id, {sums} FROM cmx a
            JOIN ({months_sql(str(VIIRS_START), t1, 'e_aqua > 0 AND e_snpp > 0')}) m USING (year, month)
            GROUP BY a.cell_id""").fetchnumpy()
        cid = np.asarray(rows["cell_id"])
        self.r = float((rows["v_d"].sum() + rows["v_n"].sum()) / (rows["a_d"].sum() + rows["a_n"].sum()))
        self.r_dn, self.r_cell = {}, {}
        for d in DN:
            v, a = np.asarray(rows[f"v_{d}"], float), np.asarray(rows[f"a_{d}"], float)
            self.r_dn[d] = float(v.sum() / a.sum())
            rc = np.full(self.max_id + 1, self.r_dn[d])
            rc[cid] = (v + S.shrink_k * self.r_dn[d]) / (a + S.shrink_k)
            self.r_cell[d] = rc
        log(f"aqua->s-npp ratios: total {self.r:.3f}, day {self.r_dn['d']:.3f}, night {self.r_dn['n']:.3f}; "
            f"cell ratio day p10-p90 {np.quantile(self.r_cell['d'][cid], 0.1):.2f}-{np.quantile(self.r_cell['d'][cid], 0.9):.2f}")

    def fit_lgbm(self, fr: dict, rounds: int):
        import lightgbm as lgb

        X = np.column_stack([fr[f] for f in FEATURES]).astype(np.float32)
        w = fr["e_snpp"].astype(np.float32)
        self.lgbm = {}
        for d in DN:
            t0 = time.time()
            y = fr[f"n_snpp_{d}"] / np.maximum(w, 1e-6)
            ds = lgb.Dataset(X, label=y, weight=w, feature_name=FEATURES, free_raw_data=False)
            timer = {"t": time.time()}

            def progress(env, d=d, timer=timer):
                if (env.iteration + 1) % 100 == 0:
                    log(f"   lgbm {d}: {env.iteration + 1}/{rounds} rounds ({time.time() - timer['t']:.0f}s)")

            self.lgbm[d] = lgb.train(LGB_PARAMS, ds, num_boost_round=rounds, callbacks=[progress])
            self.lgbm[d].save_model(str(OUT_DIR / f"lgbm_{d}.txt"))
            log(f"lgbm {d}: {len(y):,} rows, {rounds} rounds in {time.time() - t0:.0f}s")
            del ds
        del X

    def predict_dn(self, fr: dict, model: str) -> tuple[np.ndarray, np.ndarray]:
        ad, an = fr["src_d"].astype(np.float64), fr["src_n"].astype(np.float64)
        if model == "raw":
            return ad, an
        if model == "ratio":
            return self.r * ad, self.r * an
        if model == "ratio_dn":
            return self.r_dn["d"] * ad, self.r_dn["n"] * an
        if model == "cell_ratio_dn":
            cid = fr["cell_id"]
            return self.r_cell["d"][cid] * ad, self.r_cell["n"][cid] * an
        if model == "lgbm_dn":
            X = np.column_stack([fr[f] for f in FEATURES]).astype(np.float32)
            return (self.lgbm["d"].predict(X, num_threads=config.DUCKDB_THREADS),
                    self.lgbm["n"].predict(X, num_threads=config.DUCKDB_THREADS))
        if model == "blend_dn":
            ld, ln_ = self.predict_dn(fr, "lgbm_dn")
            rd, rn = self.predict_dn(fr, "cell_ratio_dn")
            return 0.5 * (ld + rd), 0.5 * (ln_ + rn)
        raise ValueError(model)


# ----------------------------------------------------------------------------- metrics
def metrics(lat, lon, year, month, obs, pred, bands: bool = True) -> dict:
    df = pd.DataFrame({"bl": np.floor(np.asarray(lat, float)).astype(np.int16),
                       "bo": np.floor(np.asarray(lon, float)).astype(np.int16),
                       "t": ym(year, month), "obs": np.asarray(obs, np.float64),
                       "pred": np.nan_to_num(np.asarray(pred, np.float64))})
    out = {"cell_month_mae": float((df.pred - df.obs).abs().mean())}
    blk = df.groupby(["bl", "bo", "t"], sort=False)[["obs", "pred"]].sum()
    blk = blk[(blk.obs > 0) | (blk.pred > 0.5)]
    out["block1deg_month_wape"] = float((blk.pred - blk.obs).abs().sum() / blk.obs.sum())
    out["block1deg_month_spearman"] = float(blk.obs.corr(blk.pred, method="spearman"))
    top = blk[blk.obs >= blk.obs.quantile(0.95)]
    out["block1deg_peak_bias_pct"] = float(100 * (top.pred.sum() / top.obs.sum() - 1))
    big = blk[blk.obs >= 20]
    if len(big):
        lr = np.log(np.maximum(big.pred, 0.5) / big.obs)
        out["block1deg_ratio_p10_p90"] = [float(np.exp(np.quantile(lr, 0.1))), float(np.exp(np.quantile(lr, 0.9)))]
    gm = df.groupby("t")[["obs", "pred"]].sum()
    out["global_month_wape"] = float((gm.pred - gm.obs).abs().sum() / gm.obs.sum())
    out["global_month_r2"] = float(1 - ((gm.pred - gm.obs) ** 2).sum() / ((gm.obs - gm.obs.mean()) ** 2).sum())
    hi = gm.obs >= gm.obs.quantile(0.9)
    out["global_month_peak_bias_pct"] = float(100 * (gm.pred[hi].sum() / gm.obs[hi].sum() - 1))
    out["total_bias_pct"] = float(100 * (gm.pred.sum() / gm.obs.sum() - 1))
    if bands:
        out["by_lat_band"] = {}
        for lo, hi_, name in LAT_BANDS:
            sub = blk[(blk.index.get_level_values("bl") >= lo) & (blk.index.get_level_values("bl") < hi_)]
            if sub.obs.sum() == 0:
                continue
            bm = sub.groupby(level="t")[["obs", "pred"]].sum()
            out["by_lat_band"][name] = {
                "share_of_obs_pct": float(100 * sub.obs.sum() / blk.obs.sum()),
                "block1deg_month_wape": float((sub.pred - sub.obs).abs().sum() / sub.obs.sum()),
                "band_month_wape": float((bm.pred - bm.obs).abs().sum() / bm.obs.sum()),
                "band_month_r2": float(1 - ((bm.pred - bm.obs) ** 2).sum() / ((bm.obs - bm.obs.mean()) ** 2).sum()),
                "total_bias_pct": float(100 * (sub.pred.sum() / sub.obs.sum() - 1)),
            }
    return out


# ----------------------------------------------------------------------------- run
def evaluate(model: AquaToViirs, fr: dict, label: str) -> dict:
    res = {}
    obs = fr["n_snpp_d"] + fr["n_snpp_n"]
    w = fr["e_snpp"].astype(np.float64)
    for name in MODELS:
        pdn = model.predict_dn(fr, name)
        res[name] = metrics(fr["lat"], fr["lon"], fr["year"], fr["month"], obs, (pdn[0] + pdn[1]) * w)
        v = res[name]
        log(f"   {label:<6} {name:<14} block WAPE {v['block1deg_month_wape']:.3f} | spearman {v['block1deg_month_spearman']:.3f}"
            f" | global month WAPE {v['global_month_wape']:.3f} | peak bias {v['global_month_peak_bias_pct']:+.1f} %"
            f" | total bias {v['total_bias_pct']:+.1f} %")
    return res


def harmonize(fr: dict, pred_d: np.ndarray, pred_n: np.ndarray, fill: dict) -> dict:
    """Harmonized day/night values and source code per row (rules in the module docstring)."""
    t = ym(fr["year"], fr["month"])
    viirs = t >= ym(VIIRS_START.year, VIIRS_START.month)
    has_aqua = fr["has_aqua"].astype(bool)
    e = np.column_stack([fr["e_snpp"], fr["e_n20"], fr["e_n21"]]).astype(np.float64)
    full = np.isclose(e, 1.0)
    vals = {}
    for d in DN:
        vals[d] = np.column_stack([fr[f"n_snpp_{d}"], fill["n20"][f"ratio_{d}"] * fr[f"n_n20_{d}"],
                                   fill["n21"][f"ratio_{d}"] * fr[f"n_n21_{d}"]]).astype(np.float64)
    best = np.argmax(e, axis=1)
    e_best = e[np.arange(len(best)), best]
    code = np.select([viirs & full[:, 0], viirs & full[:, 1], viirs & full[:, 2], viirs, has_aqua],
                     [0, 1, 2, 3, 4], 5).astype(np.int8)
    out = {"source": code}
    for d, pred in zip(DN, (pred_d, pred_n)):
        v = vals[d]
        exposure = v[np.arange(len(best)), best] / np.where(e_best > 0, e_best, np.nan)
        out[d] = np.select([code == 0, code == 1, code == 2, code == 3], [v[:, 0], v[:, 1], v[:, 2], exposure], pred)
    return out


def produce(con, model: AquaToViirs, selected: str, ta: dict, fill: dict, info: dict, grid: float) -> dict:
    """Selected model on every cell x month, harmonized series written year by year, then merged."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    out = config.harmonized_path(grid)
    parts = config.TMP_DIR / f"{out.stem}_parts"
    shutil.rmtree(parts, ignore_errors=True)
    parts.mkdir(parents=True)
    cells = con.execute("SELECT cell_id, cell_lat, cell_lon FROM cells ORDER BY cell_id").fetchnumpy()
    lat_by_id = np.zeros(info["max_cell_id"] + 1)
    lon_by_id = np.zeros(info["max_cell_id"] + 1)
    lat_by_id[cells["cell_id"]] = cells["cell_lat"]
    lon_by_id[cells["cell_id"]] = cells["cell_lon"]
    years = [r[0] for r in con.execute("SELECT DISTINCT year FROM cov ORDER BY 1").fetchall()]
    series, drift, kept_rows, small = [], [], 0, 0.0
    band_edges = np.array([b[0] for b in LAT_BANDS] + [LAT_BANDS[-1][1]])
    for y in years:
        t0 = time.time()
        fr = fetch(con, panel_sql(first_of_month(y), first_of_month(y, 12), ta))
        pd_, pn_ = model.predict_dn(fr, selected)
        h = harmonize(fr, pd_, pn_, fill)
        n_h = h["d"] + h["n"]
        pred = pd_ + pn_
        raw = {k: fr[f"n_{k}_d"] + fr[f"n_{k}_n"] for k in SAT_KEYS}
        raw_any = sum(raw.values()) > 0
        keep = (np.nan_to_num(n_h) > S.harmonized_min_value) | raw_any
        small += float(np.nan_to_num(n_h)[~keep].sum())
        cid = fr["cell_id"][keep]
        table = pa.table({
            "cell_lat": lat_by_id[cid], "cell_lon": lon_by_id[cid],
            "year": fr["year"][keep].astype(np.int16), "month": fr["month"][keep].astype(np.int8),
            "n_harmonized": n_h[keep].astype(np.float32),
            "n_harmonized_d": h["d"][keep].astype(np.float32), "n_harmonized_n": h["n"][keep].astype(np.float32),
            "pred_from_modis": pred[keep].astype(np.float32),
            "source": pa.DictionaryArray.from_arrays(pa.array(h["source"][keep], pa.int8()), pa.array(SOURCES)),
            **{f"n_{k}": raw[k][keep].astype(np.int32) for k in SAT_KEYS},
            "type_inferred": fr["n_type_inferred"][keep] > 0,
        })
        pq.write_table(table, parts / f"{y}.parquet", compression="zstd")
        kept_rows += table.num_rows
        band = np.clip(np.searchsorted(band_edges, fr["lat"], side="right") - 1, 0, len(LAT_BANDS) - 1)
        df = pd.DataFrame({"t": ym(fr["year"], fr["month"]), "band": band, "pred_from_modis": np.nan_to_num(pred),
                           "n_harmonized": np.nan_to_num(n_h), "source": h["source"],
                           **{f"n_{k}": raw[k] for k in SAT_KEYS}})
        g = df.drop(columns="source").groupby(["t", "band"]).sum().reset_index()
        series.append(g)
        if y >= S.drift_years[0]:
            ok = np.isclose(fr["e_snpp"], 1.0) & fr["has_aqua"].astype(bool)
            drift.append(pd.DataFrame({"lat": fr["lat"][ok], "lon": fr["lon"][ok], "year": fr["year"][ok],
                                       "month": fr["month"][ok], "obs": raw["snpp"][ok], "pred": pred[ok],
                                       "ratio_dn": (model.r_dn["d"] * fr["src_d"] + model.r_dn["n"] * fr["src_n"])[ok]}))
        log(f"   {y}: {len(n_h):,} cell-months, {table.num_rows:,} kept, harmonized {np.nansum(n_h) / 1e6:.2f} M "
            f"({time.time() - t0:.0f}s)")
        del fr
    tmp = out.with_name(out.name + ".tmp")
    con.execute(f"""COPY (SELECT * FROM read_parquet('{config.sql_path(parts)}/*.parquet')
                          ORDER BY cell_lat, cell_lon, year, month)
                    TO '{config.sql_path(tmp)}'
                    (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {S.parquet_row_group_size})""")
    tmp.replace(out)
    shutil.rmtree(parts, ignore_errors=True)
    ser = pd.concat(series, ignore_index=True)
    ser["year"], ser["month"] = ser.t // 12, ser.t % 12 + 1
    ser["band"] = ser.band.map({i: b[2] for i, b in enumerate(LAT_BANDS)})
    ser.drop(columns="t").to_parquet(SERIES, index=False)
    res = {"rows": kept_rows, "bytes": out.stat().st_size, "dropped_small_sum": small}
    if drift:
        dd = pd.concat(drift, ignore_index=True)
        res["drift"] = {"selected": metrics(dd.lat, dd.lon, dd.year, dd.month, dd.obs, dd.pred),
                        "ratio_dn": metrics(dd.lat, dd.lon, dd.year, dd.month, dd.obs, dd.ratio_dn)}
        res["drift_by_year"] = {
            int(yy): {"total_bias_pct": float(100 * (g.pred.sum() / g.obs.sum() - 1))}
            for yy, g in dd.groupby("year")}
    log(f"harmonized: {kept_rows:,} rows, {out.stat().st_size / 1e6:.1f} MB -> {out.name}; "
        f"values below {S.harmonized_min_value} dropped: {small:,.0f} detections in total")
    return res


def cmd_run(args) -> None:
    t_start = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    con = config.connect()
    info = prepare(con, args.grid)
    ta = fit_terra_to_aqua(con)
    fill = fit_viirs_fill(con)
    model = AquaToViirs(con, info["max_cell_id"])
    model.fit_ratios()

    c1, (v0, v1) = S.calib_years[1], S.valid_years
    cond = "e_aqua > 0 AND e_snpp > 0"
    t0 = time.time()
    cols = ", ".join(TRAIN_COLS)
    calib = fetch(con, f"SELECT {cols} FROM ({panel_sql(str(VIIRS_START), first_of_month(c1, 12), ta, 'c.overlap_cell', cond)})")
    log(f"calibration panel: {len(calib['cell_id']):,} rows, "
        f"{sum(v.nbytes for v in calib.values()) / 1e9:.2f} GB in memory ({time.time() - t0:.0f}s)")
    model.fit_lgbm(calib, args.rounds)
    results = {"settings": {"grid_deg": args.grid, "calib_years": S.calib_years, "valid_years": S.valid_years,
                            "drift_years": S.drift_years, "viirs_start": S.viirs_start, "shrink_k": S.shrink_k,
                            "lgbm_params": LGB_PARAMS, "lgbm_rounds": args.rounds, "blend_weight": 0.5},
               "data": info, "terra_to_aqua": ta, "viirs_fill": fill,
               "ratios": {"total": model.r, "day": model.r_dn["d"], "night": model.r_dn["n"]},
               "models": {}}
    log("evaluation (calibration years are in-sample)")
    ev_calib = evaluate(model, calib, "calib")
    del calib
    bias = ev_calib["lgbm_dn"]["total_bias_pct"]
    if abs(bias) > MAX_INSAMPLE_BIAS_PCT:
        raise SystemExit(f"LightGBM did not converge: in-sample total bias {bias:+.1f} % "
                         f"(limit {MAX_INSAMPLE_BIAS_PCT} %). Stopping before validation and production.")
    valid = fetch(con, f"SELECT {cols} FROM ({panel_sql(first_of_month(v0), first_of_month(v1, 12), ta, 'c.overlap_cell', cond)})")
    ev_valid = evaluate(model, valid, "valid")
    for name in MODELS:
        results["models"][name] = {"calib": ev_calib[name], "valid": ev_valid[name]}
    selected = args.model
    if selected == "auto":
        selected = min((m for m in MODELS if m != "raw"), key=lambda m: ev_valid[m]["block1deg_month_wape"])
    results["selected_model"] = selected
    log(f"selected model: {selected}")
    write_valid_blocks(model, valid, selected)
    del valid

    log("production: harmonized series for every cell and month")
    results["output"] = produce(con, model, selected, ta, fill, info, args.grid)
    results["seconds"] = round(time.time() - t_start)
    results["figures"] = make_figures(results)
    RESULTS.write_text(json.dumps(results, indent=1, default=float), encoding="utf-8")
    log(f"Done in {results['seconds']}s. Results: {RESULTS}")


def write_valid_blocks(model: AquaToViirs, valid: dict, selected: str) -> None:
    """Validation 1-degree blocks x month for the scatter figure (selected model vs global day/night ratio)."""
    w = valid["e_snpp"].astype(np.float64)
    ps, pr = (sum(model.predict_dn(valid, m)) * w for m in (selected, "ratio_dn"))
    pd.DataFrame({"bl": np.floor(valid["lat"]), "bo": np.floor(valid["lon"]), "t": ym(valid["year"], valid["month"]),
                  "obs": valid["n_snpp_d"] + valid["n_snpp_n"], "selected": ps, "ratio_dn": pr}
                 ).groupby(["bl", "bo", "t"]).sum().reset_index().to_parquet(VALID_BLOCKS, index=False)


def cmd_produce(args) -> None:
    """Rebuild the harmonized series with another model, reusing the saved LightGBM boosters (no refit).

    The ratios are refitted (seconds, deterministic); the validation metrics of every model are kept from the last
    `run`. Refuses to run if the saved boosters were trained with other parameters or another grid.
    """
    import lightgbm as lgb

    t_start = time.time()
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    saved = results["settings"]
    current = json.loads(json.dumps(LGB_PARAMS))
    if saved["lgbm_params"] != current or saved["grid_deg"] != args.grid:
        raise SystemExit("The saved boosters do not match the current LightGBM parameters or grid: use `run`.")
    con = config.connect()
    info = prepare(con, args.grid)
    ta = fit_terra_to_aqua(con)
    fill = fit_viirs_fill(con)
    model = AquaToViirs(con, info["max_cell_id"])
    model.fit_ratios()
    model.lgbm = {d: lgb.Booster(model_file=str(OUT_DIR / f"lgbm_{d}.txt")) for d in DN}
    log(f"loaded boosters lgbm_d.txt, lgbm_n.txt ({model.lgbm['d'].num_trees()} + {model.lgbm['n'].num_trees()} trees)")
    (v0, v1), cols = S.valid_years, ", ".join(TRAIN_COLS)
    valid = fetch(con, f"SELECT {cols} FROM ({panel_sql(first_of_month(v0), first_of_month(v1, 12), ta, 'c.overlap_cell', 'e_aqua > 0 AND e_snpp > 0')})")
    # Same boosters and ratios as the last run, so the saved validation metrics must be reproduced exactly.
    pd_, pn_ = model.predict_dn(valid, args.model)
    v = metrics(valid["lat"], valid["lon"], valid["year"], valid["month"], valid["n_snpp_d"] + valid["n_snpp_n"],
                (pd_ + pn_) * valid["e_snpp"].astype(np.float64), bands=False)
    ref = results["models"][args.model]["valid"]["block1deg_month_wape"]
    log(f"validation check {args.model}: block WAPE {v['block1deg_month_wape']:.4f} (saved {ref:.4f})")
    if abs(v["block1deg_month_wape"] - ref) > 1e-3:
        raise SystemExit("Validation metrics differ from the saved run: the inputs changed, use `run`.")
    write_valid_blocks(model, valid, args.model)
    del valid
    results["selected_model"] = args.model
    log(f"production with {args.model}: harmonized series for every cell and month")
    results["output"] = produce(con, model, args.model, ta, fill, info, args.grid)
    results["produce_seconds"] = round(time.time() - t_start)
    results["figures"] = make_figures(results)
    RESULTS.write_text(json.dumps(results, indent=1, default=float), encoding="utf-8")
    log(f"Done in {results['produce_seconds']}s. Results: {RESULTS}")


# ----------------------------------------------------------------------------- figures
INK, INK2, MUTED, GRID, AXIS, SURFACE = "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7", "#fcfcfb"
C_HARM, C_RAW, C_VIIRS, C_OTHER = "#2a78d6", "#eb6834", "#1baf7a", "#c3c2b7"


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.family": ["Segoe UI", "DejaVu Sans"], "axes.facecolor": SURFACE,
                         "figure.facecolor": SURFACE, "axes.edgecolor": AXIS, "axes.labelcolor": INK2,
                         "xtick.color": MUTED, "ytick.color": MUTED, "font.size": 9})
    config.FIGURES_DIR.mkdir(parents=True, exist_ok=True)
    return plt


def _style(ax, grid_axis="y"):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(axis=grid_axis, color=GRID, linewidth=0.8, linestyle="-")
    ax.set_axisbelow(True)


def _millions(ax, axis="y"):
    from matplotlib.ticker import FuncFormatter
    f = FuncFormatter(lambda v, _: "0" if v == 0 else f"{v / 1e6:g} M" if v >= 1e6 else f"{v / 1e3:g} k")
    (ax.yaxis if axis == "y" else ax.xaxis).set_major_formatter(f)


def make_figures(results: dict) -> list[str]:
    plt = _plt()
    ser = pd.read_parquet(SERIES)
    g = ser.groupby(["year", "month"]).sum(numeric_only=True).reset_index()
    g["t"] = pd.to_datetime(dict(year=g.year, month=g.month, day=1))
    paths = []
    sel = results["selected_model"]

    # 1. Yearly global totals: raw sensor switch vs harmonized (complete years only).
    yr = g[(g.year >= 2001) & (g.year <= 2025)].groupby("year").sum(numeric_only=True)
    vstart = VIIRS_START.year
    naive = np.where(yr.index < vstart, np.where(yr.n_aqua > 0, yr.n_aqua, yr.n_terra), yr.n_snpp)
    fig, ax = plt.subplots(figsize=(10, 4.4), dpi=150)
    ax.plot(yr.index, naive, color=C_RAW, lw=2, marker="o", ms=4, label="Raw: MODIS until 2011, VIIRS S-NPP from 2012")
    ax.plot(yr.index, yr.n_harmonized, color=C_HARM, lw=2, marker="o", ms=4, label="Harmonized (VIIRS-equivalent)")
    ax.axvline(vstart - 0.5, color=AXIS, lw=1)
    ax.text(vstart - 0.35, 0.03, "VIIRS S-NPP starts", color=MUTED, fontsize=8, transform=ax.get_xaxis_transform())
    _style(ax)
    _millions(ax)
    ax.set_ylim(0)
    ax.set_ylabel("Vegetation-fire detections per year")
    ax.set_title("Global: raw series (sensor step in 2012) vs harmonized series", loc="left", color=INK, fontsize=12)
    ax.legend(frameon=False, loc="center left", labelcolor=INK2)
    fig.tight_layout()
    p = config.FIGURES_DIR / "global_raw_vs_harmonized.png"
    fig.savefig(p)
    plt.close(fig)
    paths.append(str(p))

    # 2. Monthly overlap: observed VIIRS vs predicted from Aqua vs raw Aqua.
    ov = g[(g.t >= pd.Timestamp(VIIRS_START)) & (g.t < pd.Timestamp(results["data"]["last_date"][:8] + "01"))]
    fig, ax = plt.subplots(figsize=(10, 4.4), dpi=150)
    for (y0, y1), lab in ((S.valid_years, "validation"), (S.drift_years, "orbit drift")):
        ax.axvspan(pd.Timestamp(f"{y0}-01-01"), min(pd.Timestamp(f"{y1}-12-31"), ov.t.max()), color=GRID, alpha=0.6, lw=0)
        ax.text(pd.Timestamp(f"{y0}-02-01"), 0.97, lab, transform=ax.get_xaxis_transform(), color=MUTED, fontsize=8, va="top")
    ax.plot(ov.t, ov.n_harmonized, color=C_VIIRS, lw=1.6, label="Observed VIIRS (S-NPP; NOAA-20/21 fill outages)")
    ax.plot(ov.t, ov.pred_from_modis, color=C_HARM, lw=1.6, label=f"Predicted from MODIS Aqua ({sel})")
    ax.plot(ov.t, ov.n_aqua, color=C_RAW, lw=1.2, label="Raw MODIS Aqua")
    _style(ax)
    _millions(ax)
    ax.set_ylim(0)
    ax.set_ylabel("Detections per month")
    ax.set_title(f"Global: Aqua -> VIIRS over the overlap (model fitted on {S.calib_years[0]}-{S.calib_years[1]})",
                 loc="left", color=INK, fontsize=12)
    ax.legend(frameon=False, loc="upper left", labelcolor=INK2)
    fig.tight_layout()
    p = config.FIGURES_DIR / "global_overlap_monthly.png"
    fig.savefig(p)
    plt.close(fig)
    paths.append(str(p))

    # 3. Validation scatter on 1-degree blocks x month.
    b = pd.read_parquet(VALID_BLOCKS)
    b = b[b.obs > 0]
    lim = [0.5, max(b.obs.max(), b.selected.max(), b.ratio_dn.max()) * 1.5]
    fig, axes = plt.subplots(1, 2, figsize=(10, 5.2), dpi=150, sharex=True, sharey=True)
    panels = (("ratio_dn", "ratio_dn", "Global day/night ratio", C_RAW), ("selected", sel, sel, C_HARM))
    for ax, (col, key, label, color) in zip(axes, panels):
        ax.scatter(b.obs, b[col].clip(lower=0.5), s=2, color=color, alpha=0.25, lw=0, rasterized=True)
        ax.plot(lim, lim, color=AXIS, lw=1)
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlim(lim)
        ax.set_ylim(lim)
        _style(ax, "both")
        ax.set_xlabel("Observed VIIRS S-NPP (1° block-month)")
        w = results["models"][key]["valid"]["block1deg_month_wape"]
        ax.set_title(f"{label}: block WAPE {w:.2f}", loc="left", color=INK, fontsize=10)
    axes[0].set_ylabel("Predicted from Aqua")
    fig.suptitle(f"Validation {S.valid_years[0]}-{S.valid_years[1]}: predicted vs observed, 1° blocks x month",
                 x=0.01, ha="left", color=INK, fontsize=12)
    fig.tight_layout()
    p = config.FIGURES_DIR / "validation_scatter.png"
    fig.savefig(p)
    plt.close(fig)
    paths.append(str(p))

    # 4. Model comparison (validation block WAPE, lower is better).
    names = [m for m in MODELS if m != "raw"]
    vals = [results["models"][m]["valid"]["block1deg_month_wape"] for m in names]
    fig, ax = plt.subplots(figsize=(6.4, 3.0), dpi=150)
    ax.barh(names, vals, color=[C_HARM if m == sel else C_OTHER for m in names], height=0.55)
    for i, val in enumerate(vals):
        ax.text(val, i, f"  {val:.3f}", va="center", fontsize=8, color=INK)
    ax.invert_yaxis()
    _style(ax, "x")
    ax.set_xlim(0, max(vals) * 1.2)
    ax.set_title(f"1° block WAPE, validation {S.valid_years[0]}-{S.valid_years[1]} (lower is better; blue = model used)",
                 loc="left", color=INK, fontsize=10)
    fig.tight_layout()
    p = config.FIGURES_DIR / "model_comparison.png"
    fig.savefig(p)
    plt.close(fig)
    paths.append(str(p))
    return paths


def cmd_figures(args) -> None:
    results = json.loads(RESULTS.read_text(encoding="utf-8"))
    results["figures"] = make_figures(results)
    RESULTS.write_text(json.dumps(results, indent=1, default=float), encoding="utf-8")
    log("figures: " + ", ".join(results["figures"]))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="fit, validate, build the harmonized series and the figures")
    r.add_argument("--grid", type=float, default=S.grid_deg)
    r.add_argument("--rounds", type=int, default=600, help="LightGBM boosting rounds per model")
    r.add_argument("--model", default=PRODUCTION_MODEL, choices=["auto"] + MODELS[1:],
                   help="model that builds the series (auto: best validation 1-degree block WAPE)")
    r.set_defaults(func=cmd_run)
    pr = sub.add_parser("produce", help="harmonized series again with the saved boosters (no refit)")
    pr.add_argument("--grid", type=float, default=S.grid_deg)
    pr.add_argument("--model", default=PRODUCTION_MODEL, choices=MODELS[1:])
    pr.set_defaults(func=cmd_produce)
    f = sub.add_parser("figures", help="figures again from the saved outputs")
    f.set_defaults(func=cmd_figures)
    args = p.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
