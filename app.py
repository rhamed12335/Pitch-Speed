"""
Money Printer — backend API
===========================

HOW TO RUN
----------
  pip install -r requirements.txt
  uvicorn app:app --reload

IF DATA WON'T LOAD — test this in your terminal first:
  curl -s "https://baseballsavant.mlb.com/statcast_search/csv?all=true&type=details&player_type=pitcher&game_date_gt=2026-04-10&game_date_lt=2026-04-10&group_by=name&sort_col=pitches&min_pitches=0&min_results=0&min_abs=0&sort_order=desc" | head -1

If that prints a CSV header → fetch will work.
Then open http://127.0.0.1:8000/debug to see which strategy succeeds.

FETCH ORDER
-----------
1. httpx + HTTP/2  (pip install httpx[http2])  — most reliable
2. curl subprocess                              — pre-installed Mac/Linux/Windows10+
3. pybaseball                                   — may fail with same SSL block as requests
4. requests, SSL verify off                     — last resort
"""
from __future__ import annotations

import shutil
import subprocess
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from io import StringIO
from typing import Any

import pandas as pd
import requests
import urllib3
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    import httpx as _httpx
    _HTTPX = True
except ImportError:
    _httpx = None  # type: ignore[assignment]
    _HTTPX = False

try:
    from pybaseball import statcast as _pb_statcast
    _PB = True
    _PB_ERR = ""
except Exception as _e:
    _pb_statcast = None  # type: ignore[assignment]
    _PB = False
    _PB_ERR = str(_e)

_CURL = shutil.which("curl")

app = FastAPI(title="Money Printer API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

NEEDED = [
    "game_pk", "game_date", "player_name", "pitcher",
    "pitch_type", "pitch_name", "release_speed",
    "stand", "balls", "strikes", "inning",
    "pitch_number", "at_bat_number", "home_team", "away_team", "p_throws",
]
COLORS: dict[str, str] = {
    "FF": "#d4ff00", "FA": "#d4ff00", "SI": "#00e87a", "FT": "#00e87a",
    "FC": "#ff8c00", "SL": "#a855f7", "CU": "#60a5fa", "KC": "#60a5fa",
    "CS": "#93c5fd", "CH": "#f43f5e", "FO": "#f43f5e", "ST": "#e879f9",
    "SW": "#e879f9", "FS": "#facc15", "KN": "#94a3b8", "EP": "#64748b",
}
NAMES: dict[str, str] = {
    "FF": "4-Seam Fastball", "FA": "4-Seam Fastball",
    "SI": "Sinker", "FT": "Sinker", "FC": "Cutter",
    "SL": "Slider", "CU": "Curveball", "KC": "Knuckle Curve", "CS": "Slow Curve",
    "CH": "Changeup", "FO": "Forkball", "ST": "Sweeper", "SW": "Sweeper",
    "FS": "Splitter", "KN": "Knuckleball", "EP": "Eephus",
}
SKIP      = {"", "PO", "IN", "AB", "UN", "NULL", "undefined", "null"}
FASTBALLS = {"FF", "FA", "FT", "SI", "FC"}

_URL = "https://baseballsavant.mlb.com/statcast_search/csv"
_HDR = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://baseballsavant.mlb.com/statcast_search",
}
_TC = 10  # connect timeout seconds
_TR = 50  # read timeout seconds


def _pcolor(pt: str) -> str: return COLORS.get(pt, "#6d85a4")
def _pname(pt: str) -> str: return NAMES.get(pt, pt)
def _bucket(pt: str) -> str:
    return "fastballs" if str(pt or "").upper() in FASTBALLS else "offspeed"

def _season_start(season: int) -> str:
    return {2026: "2026-03-25", 2025: "2025-03-27", 2024: "2024-03-28",
            2023: "2023-03-30", 2022: "2022-04-07"}.get(season, f"{season}-03-25")

def _is_csv(text: str) -> bool:
    if not text or len(text) < 10:
        return False
    low = text[:400].lower()
    return (
        "player_name" in low
        and not low.startswith("<!doctype")
        and not low.startswith("<html")
        and not low.startswith("<?xml")
        and not low.startswith("{")
    )

def _clean(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=NEEDED)
    present = [c for c in NEEDED if c in df.columns]
    out = df[present].copy()
    for c in NEEDED:
        if c not in out.columns:
            out[c] = None
    if "game_date" in out.columns:
        out["game_date"] = out["game_date"].astype(str).str.slice(0, 10)
    return out[NEEDED]

def _params(df: str, dt: str) -> dict[str, str]:
    return {
        "all": "true", "hfGT": "R|PO|S|=", "player_type": "pitcher",
        "hfPT": "", "hfAB": "", "hfBBT": "", "hfPR": "", "hfZ": "",
        "stadium": "", "hfBBL": "", "hfNewZones": "", "hfSea": "", "hfSit": "",
        "hfOuts": "", "opponent": "", "pitcher_throws": "", "batter_stands": "",
        "hfSA": "", "team": "", "position": "", "hfRO": "", "home_road": "",
        "hfFlag": "", "metric_1": "", "hfInn": "",
        "min_pitches": "0", "min_results": "0",
        "group_by": "name", "sort_col": "pitches",
        "player_event_sort": "h_launch_speed", "sort_order": "desc",
        "min_abs": "0", "type": "details",
        "game_date_gt": df, "game_date_lt": dt,
    }

def _windows(start: str, end: str) -> list[tuple[str, str]]:
    s = datetime.strptime(start, "%Y-%m-%d").date()
    e = datetime.strptime(end, "%Y-%m-%d").date()
    days = (e - s).days + 1
    # Bigger chunks = fewer requests = much faster
    if days <= 3:   size = 1
    elif days <= 14: size = 3
    else:            size = 7   # 7-day chunks for season ranges
    wins, cur = [], s
    while cur <= e:
        ec = min(cur + timedelta(days=size - 1), e)
        wins.append((cur.isoformat(), ec.isoformat()))
        cur = ec + timedelta(days=1)
    return wins


def _fetch_one_httpx(client: Any, d0: str, d1: str) -> pd.DataFrame | None:
    """Fetch a single window with httpx. Returns None on failure."""
    try:
        r = client.get(_URL, params=_params(d0, d1))
        r.raise_for_status()
        text = r.text.strip()
        if _is_csv(text):
            return pd.read_csv(StringIO(text), low_memory=False)
    except Exception:
        pass
    return None


def _via_httpx(start: str, end: str) -> pd.DataFrame:
    if not _HTTPX or _httpx is None:
        raise RuntimeError("httpx not installed — run: pip install httpx[http2]")
    timeout = _httpx.Timeout(connect=float(_TC), read=float(_TR), write=10.0, pool=5.0)
    windows = _windows(start, end)
    frames: list[pd.DataFrame] = []
    errors: list[str] = []

    # Parallel fetch — up to 4 concurrent requests
    with _httpx.Client(http2=True, headers=_HDR, timeout=timeout,
                       follow_redirects=True) as client:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(_fetch_one_httpx, client, d0, d1): (d0, d1)
                for d0, d1 in windows
            }
            for future in as_completed(futures):
                d0, d1 = futures[future]
                try:
                    df = future.result()
                    if df is not None:
                        frames.append(df)
                    else:
                        errors.append(f"{d0}: no data")
                except Exception as exc:
                    errors.append(f"{d0}: {exc}")

    if not frames:
        raise RuntimeError("httpx: " + "; ".join(errors or ["no data"]))
    return _clean(pd.concat(frames, ignore_index=True))


def _via_curl(start: str, end: str) -> pd.DataFrame:
    if not _CURL:
        raise RuntimeError("curl not found on PATH")

    def fetch_chunk(d0: str, d1: str) -> pd.DataFrame | None:
        p = _params(d0, d1)
        qs = "&".join(
            f"{k}={v.replace('|', '%7C')}" if "|" in v else f"{k}={v}"
            for k, v in p.items()
        )
        cmd = [
            _CURL, "--silent", "--show-error", "--compressed",
            "--max-time", str(_TR + _TC),
            "--connect-timeout", str(_TC),
            "--location",
            "-H", f"User-Agent: {_HDR['User-Agent']}",
            "-H", f"Accept: {_HDR['Accept']}",
            "-H", f"Accept-Language: {_HDR['Accept-Language']}",
            "-H", "Accept-Encoding: gzip, deflate, br",
            "-H", f"Referer: {_HDR['Referer']}",
            f"{_URL}?{qs}",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=_TR + _TC + 5)
            if result.returncode == 0 and _is_csv(result.stdout.strip()):
                return pd.read_csv(StringIO(result.stdout.strip()), low_memory=False)
        except Exception:
            pass
        return None

    windows = _windows(start, end)
    frames: list[pd.DataFrame] = []

    # Parallel curl — up to 4 concurrent processes
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_chunk, d0, d1): (d0, d1) for d0, d1 in windows}
        for future in as_completed(futures):
            try:
                df = future.result()
                if df is not None:
                    frames.append(df)
            except Exception:
                pass

    if not frames:
        raise RuntimeError("curl: no data returned for any window")
    return _clean(pd.concat(frames, ignore_index=True))


def _via_pybaseball(start: str, end: str) -> pd.DataFrame:
    if not _PB or _pb_statcast is None:
        raise RuntimeError(f"pybaseball not available: {_PB_ERR}")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df = _pb_statcast(start_dt=start, end_dt=end, verbose=False)
    return _clean(df)


def _via_requests(start: str, end: str) -> pd.DataFrame:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=Retry(total=0)))
    session.mount("http://",  HTTPAdapter(max_retries=Retry(total=0)))
    session.headers.update(_HDR)
    frames: list[pd.DataFrame] = []
    errors: list[str] = []
    for d0, d1 in _windows(start, end):
        try:
            r = session.get(_URL, params=_params(d0, d1),
                            timeout=(_TC, _TR), verify=False)
            r.raise_for_status()
            text = r.text.strip()
            if _is_csv(text):
                frames.append(pd.read_csv(StringIO(text), low_memory=False))
            else:
                errors.append(f"{d0}: non-CSV")
        except Exception as exc:
            errors.append(f"{d0}: {exc}")
        time.sleep(0.25)
    if not frames:
        raise RuntimeError("requests: " + "; ".join(errors or ["no data"]))
    return _clean(pd.concat(frames, ignore_index=True))


def _load(start: str, end: str) -> pd.DataFrame:
    errors: list[str] = []
    for name, fn in [
        ("pybaseball",     lambda: _via_pybaseball(start, end)),  # 1 call, whole range
        ("httpx+http2",    lambda: _via_httpx(start, end)),        # parallel chunks
        ("curl",           lambda: _via_curl(start, end)),          # parallel chunks
        ("requests-nossl", lambda: _via_requests(start, end)),
    ]:
        try:
            df = fn()
            if df is not None and not df.empty:
                return df
            errors.append(f"{name}: returned empty")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise RuntimeError(
        "All strategies failed:\n"
        + "\n".join(f"  • {e}" for e in errors)
        + "\n\nTo fix: run  pip install httpx[http2]  then restart uvicorn."
        + "\nOr check: http://127.0.0.1:8000/debug"
    )


# ── aggregation ────────────────────────────────────────────────────────────────
def _agg(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    by: dict[str, Any] = {}
    for r in rows:
        pt = str(r.get("pitch_type") or "").upper()
        if pt in SKIP:
            continue
        v = r.get("release_speed")
        if pt not in by:
            by[pt] = {"c": 0, "vs": 0.0, "vn": 0,
                      "vlo": float("inf"), "vhi": float("-inf"),
                      "nm": r.get("pitch_name") or _pname(pt)}
        b = by[pt]
        b["c"] += 1
        if v is not None and pd.notna(v):
            fv = float(v); b["vs"] += fv; b["vn"] += 1
            b["vlo"] = min(b["vlo"], fv); b["vhi"] = max(b["vhi"], fv)
    out = []
    for pt, b in by.items():
        n = b["vn"]
        out.append({
            "pt": pt, "name": b["nm"], "count": b["c"],
            "pct": round(b["c"] / total * 100, 2) if total else 0,
            "mphAvg": round(b["vs"] / n, 1) if n else None,
            "mphMin": round(b["vlo"], 1) if n else None,
            "mphMax": round(b["vhi"], 1) if n else None,
            "color": _pcolor(pt),
        })
    out.sort(key=lambda x: x["count"], reverse=True)
    return {"total": total, "rows": out}

def _wavg(rows: list[dict]) -> float | None:
    tw, ws = 0, 0.0
    for r in rows:
        if r.get("mphAvg") is not None:
            c = int(r.get("count") or 0); ws += float(r["mphAvg"]) * c; tw += c
    return round(ws / tw, 1) if tw else None

def _wmin(rows: list[dict]) -> float | None:
    v = [float(r["mphMin"]) for r in rows if r.get("mphMin") is not None]
    return min(v) if v else None

def _wmax(rows: list[dict]) -> float | None:
    v = [float(r["mphMax"]) for r in rows if r.get("mphMax") is not None]
    return max(v) if v else None

def _count_grid(rows: list[dict]) -> dict:
    return {
        f"{b}-{s}": _agg([r for r in rows
                          if int(r.get("balls") or 0) == b
                          and int(r.get("strikes") or 0) == s])
        for b in range(4) for s in range(3)
    }

def _inning_first(rows: list[dict]) -> dict:
    grps: dict[str, list] = {}
    for r in rows:
        ing = r.get("inning")
        if ing is None or pd.isna(ing):
            continue
        gk = (str(r["game_pk"]) if pd.notna(r.get("game_pk")) and r.get("game_pk")
              else f'{r.get("game_date","")}'
                   f'|{r.get("pitcher","")}'
                   f'|{r.get("home_team","")}'
                   f'|{r.get("away_team","")}')
        grps.setdefault(f"{gk}|{int(ing)}", []).append(r)
    chosen = []
    for g in grps.values():
        g.sort(key=lambda r: (int(r.get("at_bat_number") or 999999),
                              int(r.get("pitch_number") or 999999)))
        if g:
            chosen.append(g[0])
    chosen.sort(key=lambda r: (str(r.get("game_date") or ""), int(r.get("inning") or 0),
                               int(r.get("at_bat_number") or 0), int(r.get("pitch_number") or 0)))
    by_ing = {str(n): _agg([r for r in chosen if int(r.get("inning") or 0) == n])
              for n in range(1, 10)}
    return {"all": _agg(chosen), "byInning": by_ing, "sample": len(chosen)}

def _speed_groups(rows: list[dict]) -> list[dict]:
    fb = [r for r in rows if str(r.get("pitch_type") or "").upper() in FASTBALLS]
    os = [r for r in rows if str(r.get("pitch_type") or "").upper() not in FASTBALLS]
    total = len(rows)
    result = []
    for key, nm, col, desc, gr in [
        ("fastballs", "Fastballs", "#42f39d", "FF, FT, SI, FC", fb),
        ("offspeed", "Off-Speed", "#65afff", "SL, ST, CH, FS, FO, CU, KC, KN, EP", os),
    ]:
        ag = _agg(gr)
        inc = sorted({str(r.get("pitch_type") or "").upper()
                      for r in gr if str(r.get("pitch_type") or "").upper()})
        result.append({
            "key": key, "name": nm, "color": col, "description": desc,
            "total": ag["total"],
            "pct": round(ag["total"] / total * 100, 2) if total else 0,
            "mphAvg": _wavg(ag["rows"]), "mphMin": _wmin(ag["rows"]), "mphMax": _wmax(ag["rows"]),
            "included": ", ".join(inc) if inc else "None",
            "rows": ag["rows"],
        })
    return result

def _patterns(rows: list[dict]) -> dict:
    ordered = sorted(rows, key=lambda r: (
        str(r.get("game_date") or ""), str(r.get("game_pk") or ""),
        int(r.get("inning") or 0), int(r.get("at_bat_number") or 0),
        int(r.get("pitch_number") or 0)))
    simple = {"fastballs": {"fastballs": 0, "offspeed": 0},
              "offspeed":  {"fastballs": 0, "offspeed": 0}}
    se = {s: {n: {"fastballs": 0, "offspeed": 0} for n in (1, 2, 3)}
          for s in ("fastballs", "offspeed")}
    if not ordered:
        return {"transitions": [], "streaks": [], "highlights": [], "tendencies": []}
    prev = _bucket(ordered[0].get("pitch_type")); streak = 1
    for i in range(1, len(ordered)):
        cur = _bucket(ordered[i].get("pitch_type"))
        simple[prev][cur] += 1
        bl = streak if streak in (1, 2) else 3
        se[prev][bl][cur] += 1
        if cur == prev: streak += 1
        else: streak = 1; prev = cur

    def pt(src: str) -> dict:
        fb, os_ = simple[src]["fastballs"], simple[src]["offspeed"]; t = fb + os_
        return {"source": src, "sample": t,
                "toFastballsPct": round(fb / t * 100, 1) if t else 0,
                "toOffspeedPct":  round(os_ / t * 100, 1) if t else 0,
                "toFastballsCount": fb, "toOffspeedCount": os_}

    def ps(src: str, lb: int) -> dict:
        fb, os_ = se[src][lb]["fastballs"], se[src][lb]["offspeed"]; t = fb + os_
        lbl = {1: "After 1 straight", 2: "After 2 straight", 3: "After 3+ straight"}[lb]
        return {"source": src, "label": lbl, "sample": t,
                "toFastballsPct": round(fb / t * 100, 1) if t else 0,
                "toOffspeedPct":  round(os_ / t * 100, 1) if t else 0,
                "toFastballsCount": fb, "toOffspeedCount": os_}

    trans = [pt("fastballs"), pt("offspeed")]
    streaks = [ps("fastballs", 1), ps("fastballs", 2), ps("fastballs", 3),
               ps("offspeed", 1),  ps("offspeed", 2),  ps("offspeed", 3)]
    sn = lambda s: "fastballs" if s == "fastballs" else "off-speed"
    hi, tend = [], []
    for it in streaks:
        np_ = max(it["toFastballsPct"], it["toOffspeedPct"])
        nn  = "fastballs" if it["toFastballsPct"] >= it["toOffspeedPct"] else "off-speed"
        if it["sample"] >= 10:
            hi.append({"title": f"{it['label']} {sn(it['source'])}",
                       "value": f"{np_:.1f}%", "sub": f"next is {nn}"})
        tier = None
        if   it["sample"] >= 8  and np_ == 100: tier = "Perfect so far"
        elif it["sample"] >= 8  and np_ >= 90:  tier = "Elite"
        elif it["sample"] >= 10 and np_ >= 85:  tier = "Strong"
        elif it["sample"] >= 12 and np_ >= 75:  tier = "Watch"
        if tier:
            tend.append({"tier": tier,
                         "title": f"{it['label']} {sn(it['source'])}",
                         "pct": round(np_, 1), "nextPitch": nn, "sample": it["sample"],
                         "sentence": f"{tier}: {it['label']} {sn(it['source'])}, "
                                     f"next is {nn} {np_:.1f}% of the time."})
    hi.sort(key=lambda x: float(x["value"].replace("%", "")), reverse=True)
    tend.sort(key=lambda x: (
        {"Perfect so far": 4, "Elite": 3, "Strong": 2, "Watch": 1}.get(x["tier"], 0),
        x["pct"], x["sample"]), reverse=True)
    return {"transitions": trans, "streaks": streaks,
            "highlights": hi[:4], "tendencies": tend[:8]}

def _process(rows: list[dict]) -> dict:
    vl = [r for r in rows if str(r.get("stand") or "").upper() == "L"]
    vr = [r for r in rows if str(r.get("stand") or "").upper() == "R"]
    gks = {str(r.get("game_pk")) if pd.notna(r.get("game_pk")) and str(r.get("game_pk"))
           else f'{r.get("game_date","")}'
                f'|{r.get("pitcher","")}'
                f'|{r.get("home_team","")}'
                f'|{r.get("away_team","")}'
           for r in rows}
    return {"games": len(gks), "total": len(rows),
            "overall": _agg(rows), "vsL": _agg(vl), "vsR": _agg(vr),
            "countL": _count_grid(vl), "countR": _count_grid(vr),
            "firstPitchEachInning": _inning_first(rows),
            "speedGroups": _speed_groups(rows), "patterns": _patterns(rows)}


# ── endpoints ──────────────────────────────────────────────────────────────────
@app.get("/health")
def health() -> dict:
    return {"status": "ok", "httpx": _HTTPX, "curl": bool(_CURL),
            "curl_path": _CURL or "not found", "pybaseball": _PB,
            "hint": "Run: pip install httpx[http2]" if not _HTTPX else "httpx+http2 ready"}


@app.get("/debug")
def debug() -> dict:
    """Test each strategy. Open http://127.0.0.1:8000/debug to diagnose."""
    test = "2026-04-10"
    results: dict[str, Any] = {}
    for label, fn in [
        ("httpx+http2",    lambda: _via_httpx(test, test)),
        ("curl",           lambda: _via_curl(test, test)),
        ("pybaseball",     lambda: _via_pybaseball(test, test)),
        ("requests-nossl", lambda: _via_requests(test, test)),
    ]:
        try:
            df = fn()
            results[label] = {"status": "ok", "rows": len(df),
                              "pitchers": int(df["player_name"].nunique())
                              if "player_name" in df.columns else 0}
        except Exception as exc:
            results[label] = {"status": "error", "error": str(exc)[:400]}
    return {"test_date": test, "strategies": results,
            "available": {"httpx": _HTTPX, "curl": bool(_CURL),
                          "curl_path": _CURL or "not found", "pybaseball": _PB}}


@app.get("/api/savant")
def savant(
    season:     int        = Query(..., ge=2015, le=2100),
    start_date: str | None = Query(default=None),
    end_date:   str | None = Query(default=None),
    pitcher_id: int | None = Query(default=None),
) -> dict:
    try:
        if not start_date: start_date = _season_start(season)
        if not end_date:   end_date   = datetime.now().strftime("%Y-%m-%d")

        df = _load(start_date, end_date)
        if df.empty:
            return {"meta": {"season": season, "start_date": start_date,
                             "end_date": end_date, "rows": 0, "pitchers": 0},
                    "pitchers": []}

        if "game_date" in df.columns:
            df = df[(df["game_date"] >= start_date) & (df["game_date"] <= end_date)]
        if pitcher_id and "pitcher" in df.columns:
            df = df[df["pitcher"].astype(str) == str(pitcher_id)]
        df = df.drop_duplicates()
        sc = [c for c in ["game_date","pitcher","inning","at_bat_number","pitch_number"]
              if c in df.columns]
        if sc: df = df.sort_values(sc)

        grouped: dict[str, dict] = {}
        for r in df.to_dict(orient="records"):
            pt = str(r.get("pitch_type") or "").upper()
            if pt in SKIP: continue
            nm  = str(r.get("player_name") or "Unknown")
            pid = str(r.get("pitcher") or nm)
            k   = f"{pid}|{nm}"
            if k not in grouped:
                grouped[k] = {"id": pid, "name": nm,
                              "team": r.get("home_team") or r.get("away_team") or "—",
                              "hand": str(r.get("p_throws") or "?").upper(), "rows": []}
            grouped[k]["rows"].append(r)

        out = [{"id": g["id"], "name": g["name"], "team": g["team"],
                "hand": g["hand"], "stats": _process(g["rows"])}
               for g in grouped.values()]
        out.sort(key=lambda x: x["name"])
        return {"meta": {"season": season, "start_date": start_date,
                         "end_date": end_date, "rows": int(len(df)),
                         "pitchers": len(out)},
                "pitchers": out}

    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
