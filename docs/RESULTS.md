# RESULTS

Calibration and validation metrics, figures and conclusions. Machine-readable numbers:
`data/processed/calibration/results.json`; run log: `data/processed/calibrate_run.log`.

## 2026-11-15 — Step 7: calibration MODIS → VIIRS-equivalent (global, 0.25° × month)

**Runs:** `python -m pipeline.calibrate run`, 60 min on the laptop (fit 8 min, evaluation 15 min, production with
blend_dn 32 min). The first run (log `calibrate_run_v1_diverged.log`) had a diverging night LightGBM model; fixed with
a leaf-output cap (`max_delta_step = 2`, see `docs/DECISIONS.md`) and run again. Fit and validation numbers below are
from that second run (`results_run_blend.json`). The user then chose LightGBM alone for the series:
`python -m pipeline.calibrate produce --model lgbm_dn` (30 min, log `calibrate_produce.log`) rebuilt it from the
saved boosters after reproducing the saved validation WAPE exactly (0.2248). Drift and series numbers are from it.

### Setup
- **Target:** S-NPP detections per 0.25° cell × month, day and night modelled separately, as full-month equivalents
  (exposure = observed days / expected days, from the coverage table).
- **Periods:** calibration 2012-02 → 2019-12 (16.2 M dense cell-months, zeros included, over the 170,489 cells with
  any Aqua or S-NPP detection in 2012–2021); validation 2020–2021; orbit-drift years 2022+ reported separately.
- **Metrics:** *1° block WAPE* = Σ|pred − obs| / Σ obs over 1° blocks × month (main metric, selects the model);
  Spearman on the same blocks; p10–p90 of pred/obs on blocks with ≥ 20 detections; global monthly WAPE and R²;
  peak-month bias (top 10 % months); total bias.

### Raw ratios (2012–2019, exposure-matched)
- S-NPP / Aqua: **total 6.14, day 4.95, night 30.1**. Per-cell day ratio (shrunk, k = 20) p10–p90: 4.03–6.90.

### Aqua → S-NPP: validation 2020–2021 (out of sample)
| Model | 1° block WAPE | Spearman | block ratio p10–p90 | global month WAPE | global month R² | peak-month bias | total bias |
|---|---|---|---|---|---|---|---|
| raw (no calibration) | 0.844 | 0.809 | 0.03–0.26 | 0.844 | −5.216 | −85.4 % | −84.4 % |
| ratio (one global) | 0.354 | 0.809 | 0.17–1.62 | 0.081 | 0.874 | −10.6 % | −4.1 % |
| ratio_dn (global day/night) | 0.331 | 0.808 | 0.15–1.63 | 0.089 | 0.862 | +0.1 % | −2.0 % |
| cell_ratio_dn | 0.262 | 0.812 | 0.17–1.56 | 0.073 | 0.869 | −4.4 % | −4.1 % |
| **lgbm_dn (used for the series)** | 0.225 | **0.847** | 0.42–1.58 | **0.047** | **0.909** | −6.7 % | −2.8 % |
| blend_dn | **0.224** | 0.835 | 0.33–1.52 | 0.058 | 0.893 | −5.5 % | −3.4 % |

Calibration years (in sample), for reference:

| Model | 1° block WAPE | Spearman | block ratio p10–p90 | global month WAPE | global month R² | peak-month bias | total bias |
|---|---|---|---|---|---|---|---|
| ratio_dn | 0.316 | 0.823 | 0.17–1.62 | 0.060 | 0.947 | +5.3 % | −0.0 % |
| cell_ratio_dn | 0.212 | 0.828 | 0.19–1.47 | 0.050 | 0.962 | −1.3 % | −4.3 % |
| lgbm_dn | 0.200 | 0.857 | 0.44–1.58 | 0.017 | 0.995 | +0.2 % | −0.0 % |
| blend_dn | 0.188 | 0.848 | 0.35–1.49 | 0.030 | 0.986 | −0.6 % | −2.2 % |

- The automatic rule from the preparation v2 method (lowest validation 1° block WAPE) picks **blend_dn**
  (0.5 × LightGBM + 0.5 × per-cell ratios), but only by 0.001. **The user chose lgbm_dn** (`docs/DECISIONS.md`): it is
  better on every other metric except the peak-month bias (−6.7 % vs −5.5 %), and unbiased in sample.
- lgbm_dn's total bias moves from 0.0 % (2012–2019) to −2.8 % (2020–2021), mostly from 2020 (yearly totals: 2020
  about −5 %, 2021 about 0 %). VIIRS seems to see slightly more fires per Aqua detection in 2020 (consistent with Aqua
  ageing); no model fitted on 2012–2019 can correct this.
- Validation block WAPE is 1.1–1.25× the in-sample one for every model: little overfitting.
- cell_ratio_dn predicts 0 wherever Aqua saw nothing, but 7.0 % of S-NPP detections (2012–2019) fall in cell-months
  with zero Aqua detections. That is the main source of its −4 % bias; LightGBM recovers it from neighbours and
  climatology.

### Per latitude band, validation 2020–2021
| Band | share of obs | blend_dn block WAPE | blend_dn total bias | lgbm_dn block WAPE | lgbm_dn total bias | cell_ratio_dn block WAPE | cell_ratio_dn total bias |
|---|---|---|---|---|---|---|---|
| 60S-30S | 1.8 % | 0.369 | +5.9 % | 0.327 | −2.6 % | 0.480 | +14.5 % |
| 30S-10S | 28.7 % | 0.209 | −5.2 % | 0.202 | −5.9 % | 0.247 | −4.4 % |
| 10S-10N | 35.6 % | 0.168 | −3.1 % | 0.179 | −2.1 % | 0.184 | −4.1 % |
| 10N-30N | 16.5 % | 0.219 | −4.2 % | 0.231 | −1.1 % | 0.256 | −7.2 % |
| 30N-50N | 7.5 % | 0.392 | +2.4 % | 0.381 | −0.8 % | 0.496 | +5.8 % |
| 50N-80N | 9.9 % | 0.321 | −5.1 % | 0.309 | −1.5 % | 0.378 | −8.3 % |

- Band monthly series (all fires in the band per month): lgbm_dn WAPE 0.03–0.11, R² 0.96–1.00
  (blend_dn 0.05–0.15, R² 0.89–0.99).
- 30S–30N (81 % of detections) is the most accurate; 30N–50N (many small agricultural fires) the weakest.

### Orbit-drift years 2022–2026 (never used for fitting)
| Model | 1° block WAPE | Spearman | global month WAPE | global month R² | peak-month bias | total bias |
|---|---|---|---|---|---|---|
| **lgbm_dn** | 0.247 | 0.838 | **0.033** | **0.981** | −1.0 % | **−0.9 %** |
| blend_dn | 0.242 | 0.832 | 0.046 | 0.971 | −1.9 % | −2.3 % |
| ratio_dn | 0.344 | 0.801 | 0.079 | 0.932 | +1.6 % | −2.3 % |

- Total bias by year (lgbm_dn): 2022 −2.6 %, 2023 +1.0 %, 2024 −0.4 %, 2025 +0.6 %, 2026 −6.5 % (to October;
  Aqua's night counts fall with its drift). blend_dn: −4.1, +0.5, −0.8, −1.4, −9.5 %.
- Per band (lgbm_dn): 60S–30S −4.7 %, 30S–10S −1.5 %, 10S–10N −1.6 %, 10N–30N +0.8 %, 30N–50N +0.9 %, 50N–80N −1.2 %
  (blend_dn had −10.2 % in 50N–80N).
- The model trained on 2012–2019 still holds in 2022–2025, so the drift barely affects the Aqua → VIIRS mapping
  before 2026. The harmonized series uses observed VIIRS from 2012 anyway; this only checks the method.

### Terra → Aqua (fills 2000-11 → 2002-06 only)
- Per-cell day/night ratios (k = 20) fitted on 2002-07 → 2019; global ratios Aqua/Terra: day 2.45, night 0.44.
- Validation 2020–2021:

| Model | 1° block WAPE | Spearman | global month WAPE | global month R² | total bias |
|---|---|---|---|---|---|
| per-cell ratios (used) | 0.441 | 0.686 | 0.105 | 0.788 | +8.8 % |
| global day/night ratio | 0.601 | 0.667 | 0.092 | 0.886 | +3.7 % |

- Per band (per-cell ratios): tropics −1.4 %, 30S–10S +9.2 %, 10N–30N +14.9 %, 30N–50N +12.8 %, 50N–80N +55.0 %
  (6 % of detections). Terra and Aqua see different fires (diurnal cycle), so this step is the weakest link; it
  only affects the first 20 months of the series.

### NOAA-20 / NOAA-21 → S-NPP (fills S-NPP outages)
- Ratios S-NPP / NOAA-20 over complete overlap months 2018–2025: **day 1.021, night 1.000** (per year, day
  1.015–1.030). S-NPP / NOAA-21 (2023–2025): **day 1.066, night 1.018** (per year, day 1.060–1.071).
- The three VIIRS sensors agree within 2–7 %, so one scaling factor per sensor and day/night is enough.

### Harmonized series: `data/processed/harmonized_g0.25.parquet`
Built with **lgbm_dn**.
- **34,985,299 rows, 274 MB**, sorted by cell; one row per cell-month with a harmonized value > 0.01 or any raw
  detection (every MODIS-era cell-month passes: LightGBM never predicts below 0.01).
- Columns: `cell_lat, cell_lon, year, month, n_harmonized, n_harmonized_d, n_harmonized_n, pred_from_modis, source,
  n_terra, n_aqua, n_snpp, n_n20, n_n21, type_inferred`.

| source | months | rows | harmonized detections |
|---|---|---|---|
| MODIS_TERRA_MODEL | 2000-11 → 2002-06 | 3.82 M | 31.4 M |
| MODIS_AQUA_MODEL | 2002-07 → 2012-01 | 21.94 M | 175.2 M |
| VIIRS_SNPP | 2012-02 → 2026-10 | 8.51 M | 228.1 M |
| VIIRS_N20_SCALED | 2022-07 → 2026-11 | 0.67 M | 17.5 M |
| VIIRS_EXPOSURE | 2012-03 | 0.05 M | 1.3 M |

- VIIRS_N21_SCALED is never needed (NOAA-20 is always complete when S-NPP is not).
- 22.3 M rows (all in the MODIS era) have no raw detection: LightGBM's small expected counts where MODIS saw
  nothing. Together they hold 12.4 M detections (6 % of the MODIS era, in line with the 7 % of S-NPP detections
  that fall in cell-months without Aqua). This is why the file is 274 MB instead of 115 MB with cell ratios.
- 221,481 cell-months carry `type_inferred` (NRT, Aug–Nov 2026).
- Global yearly totals (M detections, VIIRS-equivalent): 2001 20.56, 2002 19.92, 2003 19.07, 2004 18.69, 2005 18.67,
  2006 17.30, 2007 18.78, 2008 17.65, 2009 16.63, 2010 17.68, 2011 17.46, 2012 18.34, 2013 15.72, 2014 16.62,
  2015 17.67, 2016 16.62, 2017 16.31, 2018 15.53, 2019 17.33, 2020 17.06, 2021 16.67, 2022 14.84, 2023 18.03,
  2024 18.97, 2025 14.91, 2026 13.68 (to 2026-11-13). With blend_dn, 2001–2011 were within ±1.6 % of these.

### Figures (`docs/figures/`)
- `global_raw_vs_harmonized.png`: yearly totals. Raw series (MODIS to 2011, S-NPP from 2012) jumps 6× in 2012; the
  harmonized series has no step.
- `global_overlap_monthly.png`: monthly totals 2012–2026: observed VIIRS vs predicted from Aqua vs raw Aqua.
- `validation_scatter.png`: 1° block-months in 2020–2021, global day/night ratio vs lgbm_dn.
- `model_comparison.png`: validation 1° block WAPE per model (blue = model used).

### Conclusions and caveats
- The harmonization removes the 2012 sensor step: raw Aqua is −84 % below VIIRS; lgbm_dn is −2.8 % in validation
  and −0.9 % in the drift years, with 1° block-month errors of 22–25 %.
- **Bias:** lgbm_dn is unbiased in sample, −2.8 % in validation (mostly 2020) and −0.9 % in 2022+, so the MODIS-era
  part of the series is at most a few percent low relative to the VIIRS era. Peak months are slightly
  under-predicted (−6.7 % in validation, −1.0 % in 2022+).
- The decline of the harmonized series from about 20 M (2001–2005) to about 17 M (2010s) is consistent with the
  documented decline of global burned area, but 2001–2002 rests on Terra → Aqua (+8.8 % in validation).
- Mid and high latitudes are less accurate (block WAPE 0.32–0.39 vs 0.17–0.22 between 30S and 30N).
- Aqua/Terra raw-ratio note: Aqua sees 2.45× Terra's day detections but only 0.44× its night detections
  (afternoon fire peak; 01:30 vs 22:30 night overpass), which is why Terra cannot stand in for Aqua without a model.
