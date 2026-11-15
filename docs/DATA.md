# DATA — what the real hackathon data looks like

## 2026-11-14 — Step 1: full inventory (exact numbers)

**Method:** `python -m pipeline.inventory scan | boundaries | report`. One streamed DuckDB pass per file, every column
read as text; row counts cross-checked against an independent newline count. 7 min for the whole set.
All generated tables are in `data/processed/inventory/report.md` (git-ignored, regenerable). This file keeps the summary.
`data/README.md` is the earlier quick look (estimates); where they differ, this file is the reference (see §9).

### 1. Summary

| Folder | Source | Proc. | Files | GB | Rows | First detection (UTC) | Last detection (UTC) |
|---|---|---|---|---|---|---|---|
| `MODIS/` | MODIS Terra + Aqua | SP | 27 | 9.03 | 114,426,661 | 2000-11-01 00:13 | 2026-08-31 23:45 |
| `MODIS/` | MODIS Terra + Aqua | NRT | 1 | 0.07 | 792,357 | 2026-09-01 00:22 | 2026-11-13 18:47 |
| `SV_C2/` | VIIRS S-NPP | SP | 15 | 23.55 | 291,559,835 | 2012-01-20 01:04 | 2026-07-31 23:59 |
| `SV_C2/` | VIIRS S-NPP | NRT | 1 | 0.56 | 6,602,259 | 2026-08-01 00:01 | **2026-11-02 15:16** |
| `J1V_C2/` | VIIRS NOAA-20 | SP | 9 | 13.44 | 166,222,011 | 2018-04-01 00:00 | 2026-07-31 23:59 |
| `J1V_C2/` | VIIRS NOAA-20 | NRT | 1 | 0.63 | 7,445,907 | 2026-08-01 00:01 | 2026-11-13 17:19 |
| `J2V_C2/` | VIIRS NOAA-21 | SP | 4 | 5.40 | 66,673,905 | 2023-03-01 00:50 | 2026-07-31 23:23 |
| `J2V_C2/` | VIIRS NOAA-21 | NRT | 1 | 0.60 | 7,151,739 | 2026-08-01 00:09 | 2026-11-13 18:06 |
| **Total** | | | **59** | **53.26** | **660,874,674** | | |

- Per satellite: Terra 41,001,060 · Aqua 74,217,958 · S-NPP 298,162,094 · NOAA-20 173,667,918 · NOAA-21 73,825,644.
- SP 638,882,412 rows (96.7 %), NRT 21,992,262 rows (3.3 %).
- 144,085 of these rows are duplicates (§3.2), so there are 660,730,589 distinct detections at most.
- Integrity: **0** rejected CSV lines, **0** empty fields, **0** unparseable values, **0** lat/lon out of range,
  row count = newline count in all 59 files. LF line endings, no BOM, every file ends with a newline.

### 2. Columns and raw values (verified on all 59 files)

Same header in the four folders. NRT files have the same 14 columns without `type`:
```
latitude,longitude,brightness,scan,track,acq_date,acq_time,satellite,instrument,confidence,version,bright_t31,frp,daynight,type
```

| Column | MODIS | VIIRS (S-NPP, NOAA-20, NOAA-21) |
|---|---|---|
| `latitude`, `longitude` | 4 decimals in SP, **5 in NRT** | 5 decimals |
| `brightness` | 300 … 510.5 K | 207.4 … 367 K (saturates at 367); a few `0` in S-NPP and NOAA-20 SP |
| `bright_t31` | 264.5 … 400.1 K, minimum 78.4 in SP | 150 … 425 K; a few `0` in S-NPP and NOAA-20 SP |
| `scan` / `track` (km) | 1 … 4.8 / 1 … 2 | 0.32 … 0.8 / 0.36 … 0.78 |
| `acq_date` | `YYYY-MM-DD`, all valid | same |
| `acq_time` | `HHMM` text, always 4 digits, `0000` … `2359`, all valid | same |
| `satellite` | `Terra`, `Aqua` | `SNPP`, `N20`, `N21` |
| `instrument` | `MODIS` | `VIIRS` for N20/N21, **`SNPP`** for S-NPP |
| `confidence` | integer 0 … 100 (all 101 values occur) | `l`, `n`, `h` only |
| `version` | `6.03` until 2022-12-31, `61.03` from 2023-01-01, `6.1NRT` | `2`, `2.0NRT` |
| `frp` (MW) | −59.2 … 16,146.4 | 0 … 4,221 in SP; NRT has negatives (down to −2.57) and up to 8,468 |
| `daynight` | `D`, `N` | `D`, `N` |
| `type` (SP only) | `0`, `1`, `2`, `3` | `0`, `1`, `2`, `3` |

Mapping for the adapter: `satellite` `SNPP` → `N`; sensor comes from the folder / file-name code, not from `instrument`.

### 3. Oddities and what they imply

1. **59 files, not 58.** MODIS has 27 SP files + 1 NRT (`CLAUDE.md` and `data/README.md` said 26).
2. **Four whole days are duplicated** between consecutive SP files, row for row:

   | Folder | Files | Day | Duplicated rows |
   |---|---|---|---|
   | MODIS | 845661 + 845665 | 2014-01-22 | 14,493 |
   | MODIS | 845665 + 845669 | 2015-01-23 | 12,143 |
   | SV_C2 | 845664 + 845668 | 2014-01-22 | 61,108 |
   | SV_C2 | 845668 + 845672 | 2015-01-23 | 56,341 |

   The adapter must deduplicate (144,085 rows). The other 51 file boundaries are clean: one file ends on a day and the
   next starts on the following day. Duplicates *inside* a file are not checked yet (pending, steps 3–4).
3. **Files are rolling ~1-year chunks, not calendar years** (see §7). Partition by `acq_date`, never by file.
4. **SP and NRT do not overlap** (0 days with both). SP ends 2026-08-31 for MODIS and 2026-07-31 for VIIRS.
5. **NRT has no `type`** (22.0 M rows, Aug/Sep–Nov 2026). Static sources and volcanoes need another filter there.
6. **S-NPP NRT stops on 2026-11-02 15:16**, 11 days before the others. The last day of every NRT file is partial
   (downloaded on 2026-11-13 around 17–19 h UTC).
7. **Outages: days with no data at all** (§6). Terra 57 days, Aqua 26, S-NPP 49, NOAA-20 6, NOAA-21 2.
   S-NPP alone lost 15 days in Jul–Aug 2022 and has had repeated gaps since mid-2024. A monthly count is not comparable
   across satellites in those months unless it is normalised by observed days or the month is masked.
8. **Terra's orbit drift is visible in the raw counts.** Its night share was 22–27 % until 2021 and then rises:
   33.8 % (2023), 40.0 % (2024), 40.5 % (2025), 50.8 % (2026). Its yearly total also falls (1.08 M in 2025, the lowest
   full year). Aqua's night share stays at 5–7 %. VIIRS stays at 24–28 %.
9. **The three VIIRS satellites see about the same number of detections** (2024: S-NPP 21.1 M, NOAA-20 22.6 M,
   NOAA-21 21.7 M). Adding them up would triple the series.
10. **NOAA-21 has a higher mean FRP** than S-NPP and NOAA-20 in the same years (type 0: 11.9 MW vs 10.0–10.1 MW in 2024;
    12.7 vs 10.4–10.5 in 2023). To keep in mind if FRP is used across VIIRS satellites.
11. **`frp` ≤ 0 in 160,842 rows (0.02 %)**: zeros in every source, negatives in MODIS SP (minimum −59.2) and in the three
    VIIRS NRT files. No empty `frp`.
12. **Polar detections flagged as vegetation fire (`type` 0):** 1,480 rows south of 60°S (almost all VIIRS, down to
    −89.7°) and 80 rows north of 80°N. They are false alarms. The 15,103 VIIRS `type` 1 rows south of 60°S are
    consistent with Mount Erebus (77.5°S).
13. **Invalid brightness temperatures:** `0` in VIIRS `brightness` / `bright_t31` and 78.4 K in MODIS `bright_t31`.
    Not counted; these columns are not used by the pipeline.
14. `acq_time` keeps its leading zeros only if read as text. `instrument = SNPP` in the S-NPP files.
    VIIRS files use `brightness` / `bright_t31` as column names (not `bright_ti4` / `bright_ti5`).

### 4. Rows per calendar year and satellite (all types, all confidences, SP + NRT)

2014 and 2015 include the duplicated days of §3.2 for Terra, Aqua and S-NPP. 2026 runs to 13 Nov (S-NPP to 2 Nov).

| Year | Terra | Aqua | SNPP | N20 | N21 | Total |
|---|---|---|---|---|---|---|
| 2000 | 264,718 | - | - | - | - | 264,718 |
| 2001 | 1,789,790 | - | - | - | - | 1,789,790 |
| 2002 | 1,860,775 | 1,865,228 | - | - | - | 3,726,003 |
| 2003 | 1,763,041 | 3,421,231 | - | - | - | 5,184,272 |
| 2004 | 1,742,756 | 3,384,580 | - | - | - | 5,127,336 |
| 2005 | 1,746,347 | 3,411,937 | - | - | - | 5,158,284 |
| 2006 | 1,614,420 | 3,107,462 | - | - | - | 4,721,882 |
| 2007 | 1,773,981 | 3,398,646 | - | - | - | 5,172,627 |
| 2008 | 1,640,781 | 3,214,750 | - | - | - | 4,855,531 |
| 2009 | 1,479,451 | 3,008,932 | - | - | - | 4,488,383 |
| 2010 | 1,588,936 | 3,153,171 | - | - | - | 4,742,107 |
| 2011 | 1,610,589 | 3,112,069 | - | - | - | 4,722,658 |
| 2012 | 1,768,725 | 3,214,384 | 20,968,451 | - | - | 25,951,560 |
| 2013 | 1,411,589 | 2,837,252 | 19,404,646 | - | - | 23,653,487 |
| 2014 | 1,539,276 | 2,936,636 | 20,375,654 | - | - | 24,851,566 |
| 2015 | 1,645,277 | 3,139,054 | 21,508,098 | - | - | 26,292,429 |
| 2016 | 1,510,603 | 2,932,755 | 20,358,130 | - | - | 24,801,488 |
| 2017 | 1,583,187 | 2,895,094 | 20,087,513 | - | - | 24,565,794 |
| 2018 | 1,468,657 | 2,741,561 | 19,170,193 | 14,599,990 | - | 37,980,401 |
| 2019 | 1,637,814 | 2,968,193 | 21,095,150 | 20,845,493 | - | 46,546,650 |
| 2020 | 1,603,407 | 2,861,305 | 20,719,318 | 20,482,997 | - | 45,667,027 |
| 2021 | 1,578,071 | 2,828,088 | 20,347,758 | 20,157,242 | - | 44,911,159 |
| 2022 | 1,263,088 | 2,546,030 | 17,251,846 | 18,130,921 | - | 39,191,885 |
| 2023 | 1,580,503 | 3,105,065 | 21,842,028 | 21,377,262 | 18,110,940 | 66,015,798 |
| 2024 | 1,523,348 | 3,342,856 | 21,064,859 | 22,601,576 | 21,724,518 | 70,257,157 |
| 2025 | 1,076,068 | 2,632,704 | 18,506,538 | 18,466,569 | 17,674,069 | 58,355,948 |
| 2026 | 935,862 | 2,158,975 | 15,461,912 | 17,005,868 | 16,316,117 | 51,878,734 |
| **Total** | 41,001,060 | 74,217,958 | 298,162,094 | 173,667,918 | 73,825,644 | 660,874,674 |

Night share (% of rows with `daynight = N`):

| Year | Terra | Aqua | SNPP | N20 | N21 |
|---|---|---|---|---|---|
| 2000 | 16.07 | - | - | - | - |
| 2001 | 21.49 | - | - | - | - |
| 2002 | 24.64 | 7.81 | - | - | - |
| 2003 | 24.19 | 6.00 | - | - | - |
| 2004 | 24.34 | 6.26 | - | - | - |
| 2005 | 24.53 | 6.25 | - | - | - |
| 2006 | 23.48 | 6.17 | - | - | - |
| 2007 | 24.56 | 6.57 | - | - | - |
| 2008 | 21.57 | 5.28 | - | - | - |
| 2009 | 22.47 | 5.34 | - | - | - |
| 2010 | 25.31 | 6.38 | - | - | - |
| 2011 | 23.00 | 5.98 | - | - | - |
| 2012 | 26.68 | 7.24 | 27.21 | - | - |
| 2013 | 22.81 | 5.28 | 23.06 | - | - |
| 2014 | 23.87 | 6.02 | 23.80 | - | - |
| 2015 | 25.53 | 6.80 | 24.66 | - | - |
| 2016 | 24.00 | 6.13 | 24.21 | - | - |
| 2017 | 24.91 | 6.92 | 25.26 | - | - |
| 2018 | 23.51 | 6.38 | 24.59 | 25.09 | - |
| 2019 | 27.05 | 7.39 | 26.68 | 27.06 | - |
| 2020 | 24.88 | 6.38 | 24.28 | 24.59 | - |
| 2021 | 28.32 | 7.38 | 26.70 | 26.98 | - |
| 2022 | 26.36 | 6.18 | 24.19 | 24.43 | - |
| 2023 | 33.76 | 7.47 | 27.66 | 27.95 | 29.23 |
| 2024 | 40.04 | 7.14 | 27.45 | 27.83 | 28.22 |
| 2025 | 40.48 | 5.62 | 25.24 | 25.75 | 25.98 |
| 2026 | 50.83 | 5.83 | 25.49 | 25.99 | 26.44 |

### 5. Value distributions (% of rows)

| Satellite | Proc. | Rows | Night | Conf. low | Conf. nominal | Conf. high | type 0 | type 1 | type 2 | type 3 | Mean FRP (MW) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Terra | SP | 40,733,424 | 26.12 | 5.56 | 65.84 | 28.60 | 92.59 | 0.26 | 6.23 | 0.92 | 39.68 |
| Terra | NRT | 267,636 | 59.44 | 6.02 | 60.22 | 33.76 | – | – | – | – | 31.50 |
| Aqua | SP | 73,693,237 | 6.38 | 4.32 | 66.58 | 29.10 | 96.77 | 0.12 | 2.72 | 0.39 | 45.71 |
| Aqua | NRT | 524,721 | 8.50 | 4.45 | 70.92 | 24.63 | – | – | – | – | 33.38 |
| S-NPP | SP | 291,559,835 | 25.36 | 11.16 | 83.00 | 5.84 | 92.76 | 0.12 | 6.37 | 0.75 | 9.71 |
| S-NPP | NRT | 6,602,259 | 27.55 | 10.80 | 83.64 | 5.56 | – | – | – | – | 9.63 |
| NOAA-20 | SP | 166,222,011 | 26.23 | 11.50 | 83.41 | 5.10 | 92.68 | 0.11 | 6.49 | 0.72 | 9.77 |
| NOAA-20 | NRT | 7,445,907 | 27.48 | 11.54 | 83.46 | 5.00 | – | – | – | – | 9.68 |
| NOAA-21 | SP | 66,673,905 | 27.48 | 11.63 | 82.20 | 6.17 | 92.53 | 0.13 | 6.56 | 0.78 | 11.60 |
| NOAA-21 | NRT | 7,151,739 | 28.08 | 11.92 | 81.93 | 6.15 | – | – | – | – | 11.42 |

- MODIS confidence is bucketed as low < 30 ≤ nominal < 80 ≤ high (FIRMS convention; display only).
- **Static sources are mostly a night-time signal:** `type` 2 is 1.5 % of day rows and 19–21 % of night rows for VIIRS and
  Aqua (Terra: 4.6 % by day, 10.9 % at night).
- Share of SP rows kept by the cleaning rule of `CLAUDE.md` §5.2 (`type` 0, MODIS ≥ 30, VIIRS `n`/`h`):
  Aqua 92.7 %, Terra 87.6 %, S-NPP 81.8 %, NOAA-20 81.4 %, NOAA-21 81.1 %.
- First look at the raw global S-NPP / Aqua ratio after that rule (not a calibration result): **day 4.9–5.05**, stable from
  2013 to 2025; **night 28–36**.

### 6. Days without any detection, per satellite

| Satellite | Span | Days with 0 rows | Ranges |
|---|---|---|---|
| Terra | 2000-11-01 → 2026-11-13 | 57 | 2001-06-16 → 2001-07-02 (17 d), 2002-03-20 → 2002-03-27 (8 d), 2002-04-15, 2003-12-17 → 2003-12-23 (7 d), 2008-12-21 → 2008-12-22 (2 d), 2016-02-19 → 2016-02-27 (9 d), 2022-10-11 → 2022-10-22 (12 d), 2026-01-12 |
| Aqua | 2002-07-04 → 2026-11-13 | 26 | 2002-07-30 → 2002-08-07 (9 d), 2002-09-13, 2022-04-01 → 2022-04-16 (16 d) |
| S-NPP | 2012-01-20 → 2026-11-02 | 49 | 2012-03-25, 2022-07-27 → 2022-08-10 (15 d), 2024-05-30 → 2024-06-03 (5 d), 2024-07-10 → 2024-07-15 (6 d), 2024-07-25 → 2024-07-28 (4 d), 2024-09-26, 2024-11-03 → 2024-11-05 (3 d), 2026-03-10 → 2026-03-12 (3 d), 2026-04-28 → 2026-04-30 (3 d), 2026-06-01 → 2026-06-02 (2 d), 2026-07-11 → 2026-07-16 (6 d) |
| NOAA-20 | 2018-04-01 → 2026-11-13 | 6 | 2023-07-19 → 2023-07-22 (4 d), 2023-09-30, 2024-02-04 |
| NOAA-21 | 2023-03-01 → 2026-11-13 | 2 | 2024-02-27 → 2024-02-28 (2 d) |

There are also days with data but far fewer rows than their neighbours (below 50 % of the centred 31-day median):
Terra 17, Aqua 39 (e.g. 2020-08-17 → 2020-09-02), S-NPP 27, NOAA-20 4, NOAA-21 2. Indicative of partial outages.
Full lists in `data/processed/inventory/gaps.json`.

### 7. File id → date range

<details><summary>All 59 files (rows, exact first and last detection, days with data)</summary>

| Folder | Id | Proc. | MB | Rows | From (UTC) | To (UTC) | Days | Satellite share | Version(s) | `type` col. | Rows = newlines |
|---|---|---|---|---|---|---|---|---|---|---|---|
| MODIS | 845577 | SP | 141 | 1,775,367 | 2000-11-01 00:13 | 2001-11-02 23:27 | 350 | Terra 100.00% | 6.03 | yes | ok |
| MODIS | 845585 | SP | 258 | 3,259,242 | 2001-11-03 00:59 | 2002-11-04 23:51 | 358 | Terra 57.53% Aqua 42.47% | 6.03 | yes | ok |
| MODIS | 845589 | SP | 415 | 5,261,767 | 2002-11-05 00:15 | 2003-11-06 23:49 | 367 | Aqua 65.14% Terra 34.86% | 6.03 | yes | ok |
| MODIS | 845593 | SP | 401 | 5,086,543 | 2003-11-07 00:00 | 2004-11-07 23:19 | 367 | Aqua 66.59% Terra 33.41% | 6.03 | yes | ok |
| MODIS | 845597 | SP | 407 | 5,161,808 | 2004-11-08 00:23 | 2005-11-09 23:56 | 367 | Aqua 66.09% Terra 33.91% | 6.03 | yes | ok |
| MODIS | 845601 | SP | 370 | 4,702,478 | 2005-11-10 00:00 | 2006-11-11 23:48 | 367 | Aqua 66.05% Terra 33.95% | 6.03 | yes | ok |
| MODIS | 845607 | SP | 418 | 5,303,996 | 2006-11-12 00:18 | 2007-11-13 23:59 | 367 | Aqua 65.61% Terra 34.39% | 6.03 | yes | ok |
| MODIS | 845611 | SP | 382 | 4,861,798 | 2007-11-14 00:00 | 2008-11-14 23:42 | 367 | Aqua 65.75% Terra 34.25% | 6.03 | yes | ok |
| MODIS | 845616 | SP | 355 | 4,522,369 | 2008-11-15 00:49 | 2009-11-16 23:19 | 367 | Aqua 67.48% Terra 32.52% | 6.03 | yes | ok |
| MODIS | 845621 | SP | 381 | 4,837,085 | 2009-11-17 00:08 | 2010-11-18 23:54 | 367 | Aqua 66.38% Terra 33.62% | 6.03 | yes | ok |
| MODIS | 845626 | SP | 366 | 4,646,570 | 2010-11-19 01:00 | 2011-11-20 23:31 | 367 | Aqua 66.05% Terra 33.95% | 6.03 | yes | ok |
| MODIS | 845649 | SP | 61 | 789,447 | 2011-11-21 00:18 | 2012-01-19 23:39 | 60 | Aqua 65.33% Terra 34.67% | 6.03 | yes | ok |
| MODIS | 845653 | SP | 393 | 4,989,189 | 2012-01-20 00:46 | 2013-01-20 23:59 | 367 | Aqua 64.50% Terra 35.50% | 6.03 | yes | ok |
| MODIS | 845661 | SP | 336 | 4,270,946 | 2013-01-21 00:00 | 2014-01-22 23:52 | 367 | Aqua 66.77% Terra 33.23% | 6.03 | yes | ok |
| MODIS | 845665 | SP | 355 | 4,513,442 | 2014-01-22 00:16 | 2015-01-23 23:55 | 367 | Aqua 65.64% Terra 34.36% | 6.03 | yes | ok |
| MODIS | 845669 | SP | 374 | 4,755,832 | 2015-01-23 00:26 | 2016-01-24 23:59 | 367 | Aqua 65.59% Terra 34.41% | 6.03 | yes | ok |
| MODIS | 845682 | SP | 348 | 4,425,518 | 2016-01-25 00:04 | 2017-01-25 23:49 | 367 | Aqua 65.98% Terra 34.02% | 6.03 | yes | ok |
| MODIS | 845690 | SP | 358 | 4,551,600 | 2017-01-26 00:18 | 2018-01-27 23:59 | 367 | Aqua 64.74% Terra 35.26% | 6.03 | yes | ok |
| MODIS | 845694 | SP | 329 | 4,182,768 | 2018-01-28 00:00 | 2019-01-29 23:45 | 367 | Aqua 64.99% Terra 35.01% | 6.03 | yes | ok |
| MODIS | 845698 | SP | 369 | 4,689,940 | 2019-01-30 00:26 | 2020-01-31 23:19 | 367 | Aqua 64.52% Terra 35.48% | 6.03 | yes | ok |
| MODIS | 845702 | SP | 345 | 4,389,070 | 2020-02-01 00:09 | 2021-02-01 23:49 | 367 | Aqua 64.09% Terra 35.91% | 6.03 | yes | ok |
| MODIS | 845716 | SP | 350 | 4,440,775 | 2021-02-02 01:05 | 2022-02-03 23:23 | 367 | Aqua 64.15% Terra 35.85% | 6.03 | yes | ok |
| MODIS | 845720 | SP | 300 | 3,809,640 | 2022-02-04 00:16 | 2023-02-05 23:28 | 367 | Aqua 67.10% Terra 32.90% | 6.03 / 61.03 | yes | ok |
| MODIS | 845724 | SP | 380 | 4,763,337 | 2023-02-06 00:08 | 2024-02-07 23:17 | 367 | Aqua 66.49% Terra 33.51% | 61.03 | yes | ok |
| MODIS | 845734 | SP | 387 | 4,850,875 | 2024-02-08 00:07 | 2025-02-08 22:58 | 367 | Aqua 68.74% Terra 31.26% | 61.03 | yes | ok |
| MODIS | 845738 | SP | 289 | 3,627,091 | 2025-02-09 00:00 | 2026-02-10 23:49 | 367 | Aqua 71.37% Terra 28.63% | 61.03 | yes | ok |
| MODIS | 845758 | SP | 156 | 1,958,168 | 2026-02-11 00:12 | 2026-08-31 23:45 | 202 | Aqua 70.54% Terra 29.46% | 61.03 | yes | ok |
| MODIS | 845758 | NRT | 69 | 792,357 | 2026-09-01 00:22 | 2026-11-13 18:47 | 74 | Aqua 66.22% Terra 33.78% | 6.1NRT | no | ok |
| SV_C2 | 845656 | SP | 1,790 | 22,139,059 | 2012-01-20 01:04 | 2013-01-20 23:59 | 366 | SNPP 100.00% | 2 | yes | ok |
| SV_C2 | 845664 | SP | 1,573 | 19,488,727 | 2013-01-21 00:00 | 2014-01-22 22:54 | 367 | SNPP 100.00% | 2 | yes | ok |
| SV_C2 | 845668 | SP | 1,656 | 20,503,636 | 2014-01-22 00:22 | 2015-01-23 23:36 | 367 | SNPP 100.00% | 2 | yes | ok |
| SV_C2 | 845672 | SP | 1,735 | 21,479,942 | 2015-01-23 01:03 | 2016-01-24 23:59 | 367 | SNPP 100.00% | 2 | yes | ok |
| SV_C2 | 845685 | SP | 1,633 | 20,244,198 | 2016-01-25 00:00 | 2017-01-25 23:58 | 367 | SNPP 100.00% | 2 | yes | ok |
| SV_C2 | 845693 | SP | 1,646 | 20,378,683 | 2017-01-26 00:00 | 2018-01-27 23:04 | 367 | SNPP 100.00% | 2 | yes | ok |
| SV_C2 | 845697 | SP | 1,546 | 19,145,456 | 2018-01-28 00:13 | 2019-01-29 23:23 | 367 | SNPP 100.00% | 2 | yes | ok |
| SV_C2 | 845701 | SP | 1,734 | 21,454,076 | 2019-01-30 00:31 | 2020-01-31 23:41 | 367 | SNPP 100.00% | 2 | yes | ok |
| SV_C2 | 845705 | SP | 1,652 | 20,455,277 | 2020-02-01 00:49 | 2021-02-01 23:58 | 367 | SNPP 100.00% | 2 | yes | ok |
| SV_C2 | 845719 | SP | 1,653 | 20,463,241 | 2021-02-02 00:00 | 2022-02-03 23:59 | 367 | SNPP 100.00% | 2 | yes | ok |
| SV_C2 | 845723 | SP | 1,395 | 17,276,924 | 2022-02-04 00:00 | 2023-02-05 23:45 | 352 | SNPP 100.00% | 2 | yes | ok |
| SV_C2 | 845727 | SP | 1,797 | 22,215,409 | 2023-02-06 00:08 | 2024-02-07 23:17 | 367 | SNPP 100.00% | 2 | yes | ok |
| SV_C2 | 845737 | SP | 1,705 | 21,081,785 | 2024-02-08 00:25 | 2025-02-08 23:36 | 348 | SNPP 100.00% | 2 | yes | ok |
| SV_C2 | 845741 | SP | 1,477 | 18,279,387 | 2025-02-09 00:45 | 2026-02-10 23:59 | 367 | SNPP 100.00% | 2 | yes | ok |
| SV_C2 | 845761 | SP | 562 | 6,954,035 | 2026-02-11 00:00 | 2026-07-31 23:59 | 157 | SNPP 100.00% | 2 | yes | ok |
| SV_C2 | 845761 | NRT | 555 | 6,602,259 | 2026-08-01 00:01 | 2026-11-02 15:16 | 94 | SNPP 100.00% | 2.0NRT | no | ok |
| J1V_C2 | 845695 | SP | 1,301 | 16,091,029 | 2018-04-01 00:00 | 2019-01-29 23:59 | 304 | N20 100.00% | 2 | yes | ok |
| J1V_C2 | 845699 | SP | 1,713 | 21,187,309 | 2019-01-30 00:00 | 2020-01-31 22:52 | 367 | N20 100.00% | 2 | yes | ok |
| J1V_C2 | 845703 | SP | 1,634 | 20,223,960 | 2020-02-01 00:00 | 2021-02-01 23:56 | 367 | N20 100.00% | 2 | yes | ok |
| J1V_C2 | 845717 | SP | 1,641 | 20,308,412 | 2021-02-02 00:19 | 2022-02-03 23:29 | 367 | N20 100.00% | 2 | yes | ok |
| J1V_C2 | 845721 | SP | 1,465 | 18,131,350 | 2022-02-04 00:37 | 2023-02-05 23:48 | 367 | N20 100.00% | 2 | yes | ok |
| J1V_C2 | 845725 | SP | 1,755 | 21,683,388 | 2023-02-06 00:56 | 2024-02-07 23:59 | 361 | N20 100.00% | 2 | yes | ok |
| J1V_C2 | 845735 | SP | 1,835 | 22,684,161 | 2024-02-08 00:01 | 2025-02-08 23:59 | 367 | N20 100.00% | 2 | yes | ok |
| J1V_C2 | 845739 | SP | 1,476 | 18,264,796 | 2025-02-09 01:13 | 2026-02-10 23:59 | 367 | N20 100.00% | 2 | yes | ok |
| J1V_C2 | 845759 | SP | 618 | 7,647,606 | 2026-02-11 00:01 | 2026-07-31 23:59 | 171 | N20 100.00% | 2 | yes | ok |
| J1V_C2 | 845759 | NRT | 626 | 7,445,907 | 2026-08-01 00:01 | 2026-11-13 17:19 | 105 | N20 100.00% | 2.0NRT | no | ok |
| J2V_C2 | 845726 | SP | 1,635 | 20,176,230 | 2023-03-01 00:50 | 2024-02-07 23:59 | 344 | N21 100.00% | 2 | yes | ok |
| J2V_C2 | 845736 | SP | 1,756 | 21,694,229 | 2024-02-08 00:00 | 2025-02-08 23:57 | 365 | N21 100.00% | 2 | yes | ok |
| J2V_C2 | 845740 | SP | 1,409 | 17,435,000 | 2025-02-09 00:20 | 2026-02-10 23:31 | 367 | N21 100.00% | 2 | yes | ok |
| J2V_C2 | 845760 | SP | 596 | 7,368,446 | 2026-02-11 00:39 | 2026-07-31 23:23 | 171 | N21 100.00% | 2 | yes | ok |
| J2V_C2 | 845760 | NRT | 601 | 7,151,739 | 2026-08-01 00:09 | 2026-11-13 18:06 | 105 | N21 100.00% | 2.0NRT | no | ok |

</details>

### 8. Differences from the practice data (Spain / Brazil)

| | Practice (`nasa_space_apps_prep`) | Hackathon (global) |
|---|---|---|
| File layout | one CSV per country and calendar year | rolling ~1-year chunks per sensor, plus one NRT file per sensor |
| VIIRS column names | `bright_ti4`, `bright_ti5` | `brightness`, `bright_t31` (same header as MODIS) |
| S-NPP `satellite` / `instrument` | `N` / `VIIRS` | `SNPP` / `SNPP` |
| VIIRS satellites | S-NPP only | S-NPP, NOAA-20 (`N20`), NOAA-21 (`N21`) |
| MODIS `version` | `6.2` (≤ 2017), `6.03`, `61.03` | `6.03` (≤ 2022), `61.03` (2023+), `6.1NRT` |
| `type` | in every file | missing in the 4 NRT files |
| 2026 data | none | SP to Jul/Aug 2026, NRT to 13 Nov 2026 |
| Duplicates | none seen | 4 duplicated boundary days |
| Volume | 29.6 M rows | 660.9 M rows |
| S-NPP / Aqua, night | 14.5 (Spain), 19.4 (Brazil) | 28–36 (raw global, first look) |

### 9. Corrections to the quick look in `data/README.md`

- MODIS has 27 SP files (the README header says 26; its own id table lists 27). Total 59 files.
- Rows: 660.9 M, not ~670 M. Exact per-folder counts in §1.
- `type` 0 is 92.5–92.8 % for VIIRS and Terra and 96.8 % for Aqua, not ~95 % everywhere. `type` 2 is 6.2–6.6 % (Aqua 2.7 %).
- The MODIS `version` switch is exactly on 2023-01-01.
- The shared boundary days (2014-01-22, 2015-01-23) are full duplicates, not a split at a time of day.
  They also affect S-NPP, not only MODIS.

### 10. Pending checks

- Duplicates inside a file (same latitude, longitude, date, time, satellite): checked on the step-3 test files
  (see below); `pipeline.adapter check` repeats it for every partition after the full conversion.
- How many rows have invalid brightness temperatures (§3.13). Low priority.

## 2026-11-15 — Step 3: adapter test (Parquet detections table)

Test set: `fire_archive_SV-C2_845668.csv` (the single big file), plus three small MODIS files to exercise the
other code paths (`845661` + `845665` for the duplicated boundary day, `fire_nrt_M-C61_845758.csv` for NRT).

### Output schema (`data/processed/hotspots/`)
Path: `sensor=<MODIS|VIIRS>/satellite=<Terra|Aqua|N|N20|N21>/year=<YYYY>/<source file stem>.parquet`
(zstd, 100,000-row groups, sorted by `datetime_utc, lat, lon`).

| Column | Type | Notes |
|---|---|---|
| `sensor`, `satellite`, `year` | VARCHAR, VARCHAR, SMALLINT | from the path (hive partitions); sensor from the folder, `SNPP` → `N` |
| `lat`, `lon` | DOUBLE | as in the CSV (4 decimals MODIS SP, 5 elsewhere) |
| `datetime_utc`, `date`, `month` | TIMESTAMP, DATE, TINYINT | `acq_date` + `acq_time` (UTC) |
| `daynight` | VARCHAR | `D` / `N` |
| `confidence_raw`, `confidence_norm` | VARCHAR | raw (`0`–`100` or `l`/`n`/`h`) and `low`/`nominal`/`high` (MODIS: < 30, 30–79, ≥ 80) |
| `frp`, `brightness`, `bright_t31`, `scan`, `track` | DOUBLE | unchanged |
| `type` | TINYINT | NULL in NRT rows |
| `version`, `processing`, `source_file` | VARCHAR | `processing` = `SP` / `NRT` |

### Figures
| Source file | Rows in | Written | Dropped | Parquet | Bytes/row | Time |
|---|---:|---:|---:|---:|---:|---:|
| `fire_archive_SV-C2_845668.csv` (1.66 GB) | 20,503,636 | 20,503,636 | 0 | 344.8 MB | 16.8 | 22 s |
| `fire_archive_M-C61_845661.csv` (336 MB) | 4,270,946 | 4,270,946 | 0 | 60.8 MB | 14.2 | 4 s |
| `fire_archive_M-C61_845665.csv` (355 MB) | 4,513,442 | 4,498,949 | 14,493 | 64.1 MB | 14.2 | 5 s |
| `fire_nrt_M-C61_845758.csv` (69 MB) | 792,357 | 792,357 | 0 | 15.2 MB | 19.2 | 1 s |

- Parquet is 20 % of the CSV size. `lat` + `lon` are 57 % of it (4.6 bytes/row each), `frp`, `brightness` and
  `bright_t31` about 10 % each, everything else under 4 %.
- Type variants measured on S-NPP 2014 (19.1 M rows, baseline 16.82 bytes/row): all FLOAT 14.33 (85 %),
  DECIMAL(8,5) coordinates + FLOAT 13.96 (83 %), without `bright_t31` 14.91 (89 %), minimal `CLAUDE.md` §5.1
  schema 11.76 (70 %). FLOAT coordinates do not round back to the 5-decimal value in 289 of 19.1 M rows.
- Query times on the test table (2 GB cap, 4 threads): one day of S-NPP 0.02 s, bbox × one month 0.05 s,
  bbox × one year 0.23 s.
- Projection for all 59 files: about 10.8 GB of Parquet and about 12–15 minutes.

### Checks (`pipeline.adapter check`, all passed)
- Row accounting matches the inventory for the 4 files; 0 rejected lines, 0 values that fail to parse.
- 37,631 groups (day × satellite × day/night × confidence class × type) compared with the inventory:
  37,588 identical in count and FRP sum; the other 43 are the duplicated day 2014-01-22 of `845665`
  (14,493 rows, all exact copies of rows in `845661`, dropped).
- Every file is in time order (307 row groups).
- **Repeated keys inside a file** (same satellite, latitude, longitude and minute, different attributes — never
  exact copies): S-NPP 2014: 14 pairs, S-NPP 2015: 1, Terra 2013: 1, Aqua: 0. NOAA-20 file `845695` has 8
  (checked before the test). About 1 in a million rows; kept as they are.

## 2026-11-15 — Step 4: full conversion (the detections table)

`data/processed/hotspots/`: **660,730,589 rows, 10.82 GB, 161 Parquet files** (20.3 % of the 53.26 GB of CSV,
16.4 bytes/row). Schema and layout as in the step-3 section above. Conversion 30 min, audit 15 min
(2 GB cap, 4 threads). Throughput fell from 75 MB/s (MODIS) to about 20 MB/s (last VIIRS files).

| Folder | Files | Rows in | Rows written | Dropped (duplicates) | Parquet | Bytes/row | Time |
|---|---:|---:|---:|---:|---:|---:|---:|
| `MODIS` | 28 | 115,219,018 | 115,192,382 | 26,636 | 1.64 GB | 14.3 | 2.5 min |
| `SV_C2` | 16 | 298,162,094 | 298,044,645 | 117,449 | 5.01 GB | 16.8 | 12.5 min |
| `J1V_C2` | 10 | 173,667,918 | 173,667,918 | 0 | 2.92 GB | 16.8 | 10 min |
| `J2V_C2` | 5 | 73,825,644 | 73,825,644 | 0 | 1.24 GB | 16.9 | 5 min |
| **Total** | **59** | **660,874,674** | **660,730,589** | **144,085** | **10.82 GB** | **16.4** | **30 min** |

| Satellite | Rows | Parquet | Year partitions |
|---|---:|---:|---|
| Terra | 40,991,355 | 0.59 GB | 2000–2026 (27) |
| Aqua | 74,201,027 | 1.05 GB | 2002–2026 (25) |
| N (S-NPP) | 298,044,645 | 5.01 GB | 2012–2026 (15) |
| N20 | 173,667,918 | 2.92 GB | 2018–2026 (9) |
| N21 | 73,825,644 | 1.24 GB | 2023–2026 (4) |

- SP rows 638,738,327 (2000-11-01 → 2026-08-31), NRT rows 21,992,262 (2026-08-01 → 2026-11-13). 9,483 distinct dates.
- Rows per year and satellite are the table in §4 minus the duplicated days (2014 and 2015, MODIS and S-NPP).

### Audit (`pipeline.adapter check`, passed)
- Row accounting equals the inventory for all 59 files; 0 rejected lines, 0 values that fail to parse.
- 541,250 groups (day × satellite × day/night × confidence class × type) compared with the inventory:
  541,128 identical in count and FRP sum. The other 122 are the four duplicated days.
- **Dropped duplicates: 144,085 rows**, the four whole days found in step 1, every one an exact copy:
  MODIS 2014-01-22 (14,493) and 2015-01-23 (12,143); S-NPP 2014-01-22 (61,108) and 2015-01-23 (56,341).
  No overlapping row was "almost" a copy.
- All 161 files are in time order (6,671 row groups).
- Repeated keys left in the table (same satellite, latitude, longitude and minute, different attributes):
  279 extra rows in total — S-NPP 162, NOAA-20 79, NOAA-21 35, Aqua 2, Terra 1 (about 0.5 per million VIIRS rows).
  None is an exact copy and none spans two source files.

### Query times on the full table (2 GB cap, 4 threads)
| Query | Time |
|---|---:|
| Rows per year × satellite (whole table) | 0.3 s |
| One day, all five satellites (351,438 rows on 2024-08-15) | 0.04 s |
| Bounding box (Iberia) × one month, all satellites | 0.17 s |
| Bounding box (Iberia) × 2000–2026, all satellites (1.02 M rows) | 7.4 s |

The last one scans the coordinates of all 660 M rows: relevant for the CSV download over long periods (step 8).

## 2026-11-15 — Step 5: cleaning and aggregation

### Measurements that informed the choices (read-only, before building)
- **NRT `type` inferred from SP history**, validated on the SP rows of 2026 (29.9 M rows, which do have `type`)
  with a mask built from SP 2000–2025. Net error of the calendar count = (static missed − vegetation wrongly masked) / vegetation:

  | Mask variant | MODIS: static recall / veg. masked / net error | VIIRS: static recall / veg. masked / net error |
  |---|---|---|
  | **0.01°, own cell, ≥ 1 SP static detection (chosen)** | **90.1 % / 1.20 % / −0.54 %** | **97.3 % / 1.86 % / −1.64 %** |
  | 0.01°, 3×3 neighbourhood | 94.4 % / 3.29 % / −2.74 % | 97.8 % / 4.39 % / −4.27 % |
  | 0.005°, 3×3 | 92.6 % / 1.63 % / −1.09 % | 97.5 % / 2.45 % / −2.26 % |
  | 0.01°, 3×3, ≥ 3 detections | 90.5 % / 1.89 % / −1.22 % | 97.5 % / 2.49 % / −2.30 % |
  | 0.01°, 3×3, SP 2023–2025 only | 91.1 % / 1.84 % / −1.17 % | 97.4 % / 2.53 % / −2.34 % |

  Keeping every NRT row instead would add +6.3 % (MODIS) / +10.4 % (VIIRS) and more than double the count in
  4,504 of 116,135 active 0.25° cells (gas flares).
- **Grid sizes** (calendar-clean rows): 0.25° 192 k active cells, 12.7 M cell-month rows, Aqua/S-NPP dense panel
  2012–2021 20.5 M cell-months; 0.5° 57 k / 5.5 M / 6.2 M; 1° 17 k / 2.2 M / 1.9 M. Half of all detections fall
  in 10,944 cells of 0.25°, 90 % in 50,105.
- **Lava labelled as vegetation fire (type 0):** a VIIRS-era effect. Kīlauea box: 105,896 VIIRS vs 293 MODIS type-0
  detections; Reykjanes, Holuhraun and Mauna Loa have none outside their eruptions. Mauna Loa type-0 rows of
  May–Aug 2022 are at 19.72–19.74°N (Pōhakuloa range fires, real) and are kept. Windows in `pipeline/config.py`.

### Outputs (`data/processed/`)
| File | Content | Size |
|---|---|---:|
| `cell_month_g0.25.parquet` | 12,703,126 cell-months, 190,772 cells of 0.25°, 35 columns, sorted by cell | 315.7 MB |
| `cell_month_g0.25.funnel.json` | rows removed by each rule, per satellite and SP/NRT | 5 KB |
| `static_mask_d0.01.parquet` | 734,513 cells of 0.01° with SP static history (type 1: 6,928; 2: 385,551; 3: 342,034) | 5.2 MB |
| `coverage_sat_month.parquet` | 934 satellite-months: days in record, observed, missing, low days, coverage | 4 KB |

Columns of the cell table: `cell_lat, cell_lon` (SW corner), `year, month`, then per satellite
(`terra, aqua, snpp, n20, n21`) `n, n_d, n_n, frp_sum, frp_sum_d, frp_sum_n`, and `n_type_inferred`
(kept rows whose `type` was inferred: 17.9 M rows in 221,481 cell-months, Aug–Nov 2026 only).

### Cleaning funnel (type → confidence → exclusions → latitude)
| Satellite | Rows | Kept | % kept | Rows with inferred type kept |
|---|---:|---:|---:|---:|
| Terra | 40,991,355 | 35,899,843 | 87.6 | 225,769 |
| Aqua | 74,201,027 | 68,756,104 | 92.7 | 480,432 |
| S-NPP | 298,044,645 | 243,742,789 | 81.8 | 5,398,840 |
| NOAA-20 | 173,667,918 | 141,286,281 | 81.4 | 6,044,922 |
| NOAA-21 | 73,825,644 | 59,863,966 | 81.1 | 5,780,412 |
| **Total** | **660,730,589** | **549,548,983** | **83.2** | **17,930,375** |

- NRT rows inferred as static: Terra 10.4 %, Aqua 4.3 %, S-NPP 7.8 %, NOAA-20 7.6 %, NOAA-21 7.6 %
  (true static share in the SP rows of 2026: 11.8 %, 3.7 %, 8.5 %, 8.6 %, 8.7 %).
- Exclusions removed 261,498 clean type-0 detections: Kīlauea 100,870, Reykjanes 57,785, Holuhraun 55,481,
  La Palma 28,251, Mauna Loa 15,155, Nyiragongo crater 2,522, Tolbachik 1,299, Nyiragongo 2021 135.
- Outside 60°S–80°N: 1,556. FRP clipped to 0 in the sums: 4 kept rows (zeros are far more common and need no clip).

### Coverage (outages)
Missing and low days per satellite equal the step-1 inventory (Terra 57 / 17, Aqua 26 / 39, S-NPP 49 / 27,
NOAA-20 6 / 4, NOAA-21 2 / 2). Months with ≥ 5 missing days: Aqua 2022-04 (16), Terra 2001-06 (15), Terra 2022-10 (12),
S-NPP 2022-08 and 2024-07 (10), Terra 2016-02 (9), Terra 2002-03 (8), Aqua 2002-08 (7), Terra 2003-12 (7),
S-NPP 2026-07 (6), S-NPP 2022-07 (5).

### Raw global S-NPP / Aqua ratio of calendar-clean counts
Day 4.57–5.23 (2012–2023), 4.35 in 2024 (S-NPP outages); night 27–33 until 2024, then 36–37 in 2025–2026 (Aqua drift).
Matches `docs/ANALYSIS.md`. Yearly totals per satellite: `python -m pipeline.aggregate info`.

## 2026-11-15 — Step 7: what the calibration learned about the data
- 7.0 % of S-NPP calendar detections (2012–2019) fall in 0.25° cell-months where Aqua has none; only 0.31 % fall in
  cells where Aqua saw nothing over the whole period. VIIRS sees small fires next to, not instead of, MODIS fires.
- Night counts are extremely skewed: mean 1.9 S-NPP night detections per dense cell-month, top 50 cell-months about
  4,000, maximum 12,895. This broke the default LightGBM Poisson fit (`docs/DECISIONS.md`).
- S-NPP exposure in the calibration months is at least 30/31 (S-NPP's long outages fall in 2022+).
- Sensor ratios on complete overlap months: S-NPP/NOAA-20 day 1.021, night 1.000; S-NPP/NOAA-21 day 1.066, night
  1.018; Aqua/Terra day 2.45, night 0.44 (2002-07 → 2019). Details and metrics in `docs/RESULTS.md`.
- Harmonized table `harmonized_g0.25.parquet` (built with LightGBM alone): 35.0 M rows, 274 MB; schema and sources
  in `docs/RESULTS.md`.

## 2026-11-15 — Step 8: data as the API serves it
- Last data date 2026-11-13 (S-NPP 2026-11-02): November 2026 is a partial month (13 of 30 days). Its harmonized
  value covers those 13 days; the anomaly uses a pro-rated baseline (`docs/DECISIONS.md`).
- Near-real-time rows with a calculated type: VIIRS from 2026-08-01, MODIS from 2026-09-01; 22.0 M rows
  (Terra 0.27 M, Aqua 0.52 M, S-NPP 6.60 M, NOAA-20 7.45 M, NOAA-21 7.15 M). Calendar months flagged: 2026-08 → 2026-11.
- Series satellite per month: Terra 2000-11 → 2002-06, Aqua 2002-07 → 2012-01, S-NPP from 2012-02 except NOAA-20 in
  2022-07/08, 2024-05/06/07/09/11, 2026-03/04/06/07 and 2026-11 (S-NPP missing days).
- Point weights carry 439.7 M of the 453.4 M harmonized detections (97.0 %); the other 3 % are model values in
  cell-months where the series satellite has no detection, so the map's "VIIRS-eq." sum is slightly below the calendar.
- `daily_counts.parquet`: 558,210 rows, 2.6 MB; matches the detections table (660,730,589) and the cell table's
  calendar count (549,548,983).

## 2026-11-15 — Step 9b: country borders (Natural Earth)
- Source: Natural Earth 1:50m Admin 0 countries, release v5.1.2 (public domain), pinned URL in `pipeline/countries.py`.
  242 units → `web/public/countries.geojson` (1.70 MB, 0.57 MB gzipped), coordinates rounded to 0.001°.
- Id = `ADM0_A3`: `ISO_A3` is `-99` for France, Norway and a few others. Names from `NAME_EN`.
- Antimeridian: Natural Earth splits rings at ±180°. Five units get a shifted box (east > 180): Fiji, Kiribati,
  New Zealand, Russia, USA. Antarctica spans every longitude and keeps the plain box. The zoom box is the largest
  part (France's would otherwise cover the overseas departments).
- Antarctica has no calendar cells (calendar latitude limit 60°S): its polygon calendar is all zeros.
- 1:50m coastlines are generalised: a few coastal or offshore detections fall inside or outside the border by a few
  hundred metres (Brazil, 2024-09-10: 61 `type = 3` offshore detections inside the border).
- Country calendars (harmonized, whole record): Brazil 37.8 M, Russia 30.0 M, USA 10.4 M, Canada 8.4 M VIIRS-eq.
  detections; Canada 2023 = 1.64 M (record season).
