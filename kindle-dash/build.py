#!/usr/bin/env python3
"""Build the daily Kindle dashboard pages (1072x1448 greyscale PNGs).

Pages: 01-today (weather, surf, tide) · 02-week · 03-calendar · 04-school · 05-news · lock-01 (sleep screen)
Writes the PNGs plus manifest.json into --out.

Data sources (all public, no keys):  NWS (weather), NDBC buoy 46225 (surf),
NOAA CO-OPS 9410230 (tides), Hacker News API.
Calendar: private iCal URL from the CAL_ICS_URL environment variable (optional).

Offline test mode:  --fixtures DIR  (reads saved responses instead of the network)
                    NOW_OVERRIDE=2026-11-11T17:10 pins the clock.
"""
import argparse, datetime as dt, hashlib, json, math, os, re, sys, textwrap
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont

TZ = ZoneInfo("America/Los_Angeles")
LAT, LON = 32.857, -117.257
PLACE = "LA JOLLA SHORES"
NDBC_STATION = "46225"          # Torrey Pines Outer buoy
TIDE_STATION = "9410230"        # La Jolla (Scripps Pier)
UA = {"User-Agent": "kindle-dash/1.0 (personal e-ink dashboard)"}
W, H = 1072, 1448
HERE = os.path.dirname(os.path.abspath(__file__))
FONT_DIR = os.path.join(HERE, "fonts")

FIXTURES = None


# --------------------------------------------------------------------------- utils
def now_local():
    ov = os.environ.get("NOW_OVERRIDE")
    if ov:
        return dt.datetime.fromisoformat(ov).replace(tzinfo=TZ)
    return dt.datetime.now(TZ)


def fetch(name, url, kind="json", retries=2):
    """Fetch URL (or a fixture named `name`). Returns parsed JSON or text; None on failure."""
    if FIXTURES:
        p = os.path.join(FIXTURES, name + (".json" if kind == "json" else ".txt"))
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f) if kind == "json" else f.read()
    for attempt in range(retries + 1):
        try:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            return r.json() if kind == "json" else r.text
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {name}: attempt {attempt + 1} failed: {e}", file=sys.stderr)
    return None


def compass(deg):
    names = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return names[int((deg % 360) / 22.5 + 0.5) % 16]


def m_to_ft(m):
    return m * 3.28084


def c_to_f(c):
    return c * 9 / 5 + 32


def fmt_time(t):
    """5:10 PM from a datetime."""
    s = t.strftime("%I:%M %p").lstrip("0")
    return s


def fmt_time_short(t):
    s = t.strftime("%I:%M%p").lstrip("0").lower()
    return s.replace(":00", "").replace("am", "a").replace("pm", "p")


# --------------------------------------------------------------------------- data: weather
def get_weather(now):
    pts = fetch("points", f"https://api.weather.gov/points/{LAT},{LON}")
    if not pts:
        return None
    props = pts["properties"]
    fc = fetch("forecast", props["forecast"])
    hr = fetch("hourly", props["forecastHourly"])
    if not fc:
        return None
    periods = fc["properties"]["periods"]
    out = {"now": None, "days": [], "periods": periods, "hourly": []}
    if hr:
        out["hourly"] = _parse_hourly(hr["properties"]["periods"])
        p0 = hr["properties"]["periods"][0]
        out["now"] = {
            "temp": p0["temperatureUnit"] == "F" and p0["temperature"] or round(c_to_f(p0["temperature"])),
            "cond": p0["shortForecast"],
            "wind": f'{p0["windSpeed"]} {p0["windDirection"]}'.strip(),
        }
    # Fold day/night periods into day rows.
    days = []
    for p in periods:
        start = dt.datetime.fromisoformat(p["startTime"]).astimezone(TZ)
        pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
        if p["isDaytime"]:
            days.append({"label": start.strftime("%a").upper(), "date": start.date(), "hi": p["temperature"],
                         "lo": None, "cond": p["shortForecast"], "pop": pop or 0, "detail": p["detailedForecast"],
                         "night_pop": 0, "night_cond": ""})
        else:
            prev = days[-1] if days else None
            same_night = prev is not None and prev["lo"] is None and (
                prev["date"] == start.date() or (start.hour < 6 and prev["date"] == start.date() - dt.timedelta(days=1)))
            if same_night:
                prev["lo"] = p["temperature"]
                prev["night_pop"] = pop or 0
                prev["night_cond"] = p["shortForecast"]
            else:  # leading "Tonight" with no matching day yet
                days.append({"label": "TONIGHT" if start.date() == now.date() else start.strftime("%a").upper(),
                             "date": start.date(), "hi": None, "lo": p["temperature"],
                             "cond": p["shortForecast"], "pop": pop or 0, "detail": p["detailedForecast"],
                             "night_pop": 0, "night_cond": ""})
    out["days"] = days[:7]
    return out


_DIR_DEG = {n: i * 22.5 for i, n in enumerate(
    ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE", "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"])}


def _parse_hourly(periods):
    """NWS hourly periods -> [{t, temp, mph, deg}] (skips entries it cannot parse)."""
    out = []
    for p in periods:
        try:
            t = dt.datetime.fromisoformat(p["startTime"]).astimezone(TZ)
            nums = [int(x) for x in re.findall(r"\d+", p.get("windSpeed", ""))]
            temp = p["temperature"] if p.get("temperatureUnit", "F") == "F" else round(c_to_f(p["temperature"]))
            out.append({"t": t, "temp": temp, "mph": max(nums) if nums else None,
                        "deg": _DIR_DEG.get(p.get("windDirection", ""))})
        except (KeyError, ValueError, TypeError):
            continue
    return out


def best_window(weather, now, first_hr=6, last_hr=18):
    """Best 2-hour daylight slot by wind (offshore counts as half as bad).
    Looks at the rest of today; if less than 2 hours remain, tomorrow. Returns (start, end, avg_mph, offshore) or None."""
    hours = (weather or {}).get("hourly") or []
    def day_hours(day):
        return [h for h in hours if h["t"].date() == day and first_hr <= h["t"].hour < last_hr
                and h["mph"] is not None and h["t"] >= now.replace(minute=0, second=0, microsecond=0)]
    for day in (now.date(), now.date() + dt.timedelta(days=1)):
        hs = day_hours(day)
        best = None
        for a, b in zip(hs, hs[1:]):
            if (b["t"] - a["t"]) != dt.timedelta(hours=1):
                continue
            offs = [h["deg"] is not None and 45 <= h["deg"] <= 135 for h in (a, b)]
            cost = sum(h["mph"] * (0.5 if o else 1.0) for h, o in zip((a, b), offs)) / 2
            if best is None or cost < best[0]:
                best = (cost, a["t"], b["t"] + dt.timedelta(hours=1), (a["mph"] + b["mph"]) / 2, all(offs))
        if best:
            return best[1], best[2], best[3], best[4]
    return None


def surf_call(surf, window):
    """One word: Flat / Poor / Fair / Good. Rough guide from buoy wave height and wind in the best window."""
    if not surf or surf.get("ft") is None:
        return None
    h = surf["ft"]
    if h < 1.5:
        return "FLAT"
    if window is None:
        return "FAIR" if h >= 2.5 else "POOR"
    _, _, mph, offshore = window
    if offshore or mph <= 6:
        return "GOOD" if h >= 2.5 else "FAIR"
    if mph <= 12:
        return "FAIR"
    return "POOR"


def rain_alert(weather, now):
    """Return (headline, detail) if meaningful rain is in the next ~3 days, else None."""
    if not weather:
        return None
    wet = []
    horizon = now + dt.timedelta(days=3)
    for p in weather["periods"]:
        start = dt.datetime.fromisoformat(p["startTime"]).astimezone(TZ)
        if start > horizon:
            break
        pop = (p.get("probabilityOfPrecipitation") or {}).get("value") or 0
        if pop >= 50 and re.search(r"rain|shower|storm|thunder", p["shortForecast"], re.I):
            wet.append((p["name"], pop, p["detailedForecast"]))
    if not wet:
        return None
    heavy = any(re.search(r"heavy rain|storm|thunder", w[2], re.I) for w in wet)
    def short(n):
        n = re.sub(r"\b(Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\b", lambda m: m.group(1)[:3], n)
        return n.replace(" Night", " night").replace("This Afternoon", "this PM")
    first, last = short(wet[0][0]), short(wet[-1][0])
    span = first if first == last else f"{first} – {last}"
    amt = re.search(r"between ([\d.]+) and ([\d.]+) of an inch", " ".join(w[2] for w in wet))
    detail = f"{span} · {'heavy rain possible · ' if heavy else ''}{max(w[1] for w in wet)}%"
    if amt:
        try:
            detail += f" · {float(amt.group(1)):g}–{float(amt.group(2)):g} in"
        except ValueError:
            pass
    return ("RAIN INCOMING" if heavy else "RAIN LIKELY", detail)


# --------------------------------------------------------------------------- data: surf
def _ndbc_rows(text):
    rows, header = [], None
    for line in text.splitlines():
        if line.startswith("#YY"):
            header = line.lstrip("#").split()
        elif line.startswith("#") or not line.strip():
            continue
        elif header:
            parts = line.split()
            if len(parts) == len(header):
                rows.append(dict(zip(header, parts)))
    return rows


def _num(v):
    try:
        return None if v == "MM" else float(v)
    except (TypeError, ValueError):
        return None


def _row_time(r):
    t = dt.datetime(int(r["YY"]), int(r["MM"]), int(r["DD"]), int(r["hh"]), int(r["mm"]), tzinfo=dt.timezone.utc)
    return t.astimezone(TZ)


def get_surf():
    txt = fetch("ndbc", f"https://www.ndbc.noaa.gov/data/realtime2/{NDBC_STATION}.txt", "text")
    spec = fetch("ndbc_spec", f"https://www.ndbc.noaa.gov/data/realtime2/{NDBC_STATION}.spec", "text")
    out = {}
    if txt:
        for r in _ndbc_rows(txt)[:12]:
            h = _num(r.get("WVHT"))
            if h is not None:
                out.update({
                    "time": _row_time(r),
                    "ft": m_to_ft(h),
                    "period": _num(r.get("DPD")),
                    "dir": _num(r.get("MWD")),
                    "water_f": c_to_f(_num(r["WTMP"])) if _num(r.get("WTMP")) is not None else None,
                })
                break
    if spec:
        for r in _ndbc_rows(spec)[:12]:
            swh = _num(r.get("SwH"))
            if swh is not None:
                out["swell"] = {
                    "time": _row_time(r), "ft": m_to_ft(swh), "per": _num(r.get("SwP")), "dir": r.get("SwD"),
                    "wft": m_to_ft(_num(r["WWH"])) if _num(r.get("WWH")) is not None else None,
                    "wper": _num(r.get("WWP")), "wdir": r.get("WWD"),
                }
                break
    return out or None


# --------------------------------------------------------------------------- data: tides
def get_tides(now):
    b = (now - dt.timedelta(days=1)).strftime("%Y%m%d")
    e = (now + dt.timedelta(days=3)).strftime("%Y%m%d")
    url = ("https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?product=predictions&application=kindle_dash"
           f"&begin_date={b}&end_date={e}&datum=MLLW&station={TIDE_STATION}&time_zone=lst_ldt&units=english"
           "&interval=hilo&format=json")
    data = fetch("tides", url)
    if not data or "predictions" not in data:
        return None
    pts = []
    for p in data["predictions"]:
        t = dt.datetime.strptime(p["t"], "%Y-%m-%d %H:%M")  # naive local
        pts.append((t, float(p["v"]), p["type"]))
    return pts


def tide_at(pts, t):
    for (t0, v0, _), (t1, v1, _) in zip(pts, pts[1:]):
        if t0 <= t <= t1:
            p = (t - t0) / (t1 - t0)
            return v0 + (v1 - v0) * (1 - math.cos(math.pi * p)) / 2
    return pts[-1][1] if t > pts[-1][0] else pts[0][1]


# --------------------------------------------------------------------------- data: news
def get_news(n=14):
    ids = fetch("hn_top", "https://hacker-news.firebaseio.com/v0/topstories.json")
    if not ids:
        return None
    items = []
    for i in ids[: n + 6]:
        it = fetch(f"hn_item_{i}", f"https://hacker-news.firebaseio.com/v0/item/{i}.json")
        if not it or it.get("type") != "story" or it.get("dead") or it.get("deleted"):
            continue
        dom = ""
        if it.get("url"):
            m = re.match(r"https?://(?:www\.)?([^/]+)", it["url"])
            dom = m.group(1) if m else ""
        items.append({"title": it.get("title", ""), "score": it.get("score", 0),
                      "comments": it.get("descendants", 0), "domain": dom})
        if len(items) >= n:
            break
    return items or None


# --------------------------------------------------------------------------- data: calendar
def _ics_events(text, start, end):
    import icalendar
    import recurring_ical_events
    cal = icalendar.Calendar.from_ical(text)
    evs = []
    for ev in recurring_ical_events.of(cal).between(start, end):
        s = ev.get("DTSTART").dt
        e = ev.get("DTEND").dt if ev.get("DTEND") else None
        title = str(ev.get("SUMMARY", "(no title)"))
        if isinstance(s, dt.datetime):
            s = s.astimezone(TZ) if s.tzinfo else s.replace(tzinfo=TZ)
            e = (e.astimezone(TZ) if e.tzinfo else e.replace(tzinfo=TZ)) if isinstance(e, dt.datetime) else None
            evs.append({"day": s.date(), "all_day": False, "start": s, "end": e, "title": title})
        else:  # all-day (date objects); DTEND is exclusive
            d0 = s
            d1 = (e - dt.timedelta(days=1)) if isinstance(e, dt.date) and e else d0
            d = max(d0, start.date())
            while d <= min(d1, end.date() - dt.timedelta(days=1)):
                evs.append({"day": d, "all_day": True, "start": None, "end": None, "title": title})
                d += dt.timedelta(days=1)
    evs.sort(key=lambda x: (x["day"], not x["all_day"], x["start"] or dt.datetime.min.replace(tzinfo=TZ)))
    return evs


def _fetch_ics(name, url):
    if FIXTURES:
        return fetch(name, "", "text")
    if not url:
        return None
    try:
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        return r.text
    except Exception as e:  # noqa: BLE001
        print(f"[warn] {name} fetch failed: {type(e).__name__}", file=sys.stderr)  # never print the URL
        return None


def get_calendar(now, days=4):
    text = _fetch_ics("calendar", os.environ.get("CAL_ICS_URL", "").strip())
    if not text:
        return None
    start = dt.datetime.combine(now.date(), dt.time.min, tzinfo=TZ)
    return _ics_events(text, start, start + dt.timedelta(days=days))


# Hardy Elementary PTA calendar: a public Google Calendar feed published on the school's website
# (https://hardy.sandiegounified.org/about/Calendar). Override with SCHOOL_ICS_URL if it ever moves.
HARDY_ICS = ("https://calendar.google.com/calendar/ical/"
             "c_13db74eb3070947ae2f7582121d55da21458e02993fa8cfcb97b5f45a0118d1f%40group.calendar.google.com/public/basic.ics")


def get_school(now, days=60):
    text = _fetch_ics("school", os.environ.get("SCHOOL_ICS_URL", "").strip() or HARDY_ICS)
    if not text:
        return None
    start = dt.datetime.combine(now.date(), dt.time.min, tzinfo=TZ)
    return _ics_events(text, start, start + dt.timedelta(days=days))


# --------------------------------------------------------------------------- drawing helpers
_font_cache = {}


def F(weight, size):
    key = (weight, size)
    if key not in _font_cache:
        _font_cache[key] = ImageFont.truetype(os.path.join(FONT_DIR, f"Inter-{weight}.otf"), size)
    return _font_cache[key]


class Page:
    def __init__(self, title, now, n, total):
        self.im = Image.new("L", (W, H), 255)
        self.d = ImageDraw.Draw(self.im)
        self.n, self.total = n, total
        self.text((48, 40), title, F("ExtraBold", 44))
        stamp = f'{now.strftime("%a %b")} {now.day} · {fmt_time(now)} {now.strftime("%Z")}'
        self.text((W - 48, 52), stamp, F("Medium", 28), anchor="ra")
        self.rule(108, 5)

    def text(self, xy, s, font, fill=0, anchor="la"):
        self.d.text(xy, s, font=font, fill=fill, anchor=anchor)

    def rule(self, y, w=3):
        self.d.rectangle([48, y, W - 48, y + w - 1], fill=0)

    def wrap(self, s, font, width, max_lines=2):
        words, lines, cur = s.split(), [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if font.getlength(t) <= width:
                cur = t
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        if len(lines) > max_lines:
            lines = lines[:max_lines]
            while font.getlength(lines[-1] + "…") > width and len(lines[-1]) > 1:
                lines[-1] = lines[-1][:-1]
            lines[-1] = lines[-1].rstrip() + "…"
        return lines

    def footer(self, credits):
        self.rule(1345)
        self.text((48, 1362), credits, F("Regular", 22), fill=70)
        self.text((W - 48, 1362), f"{self.n}/{self.total}", F("Bold", 24), anchor="ra")
        self.text((48, 1394), "Swipe or tap the right edge for the next page", F("Regular", 22), fill=70)

    def unavailable(self, y, what):
        self.d.rectangle([48, y, W - 48, y + 90], outline=0, width=3)
        self.text((72, y + 26), f"{what} unavailable right now", F("Medium", 30))

    def save(self, path):
        self.im.save(path, optimize=True)


# --------------------------------------------------------------------------- pages
def page_today(now, weather, surf, tides, n, total):
    p = Page(PLACE, now, n, total)
    d = p.d
    # --- NOW
    p.text((48, 140), "NOW", F("Bold", 26))
    if weather and weather["now"]:
        nw = weather["now"]
        p.text((48, 170), f'{nw["temp"]}°', F("ExtraBold", 230))
        cond_lines = p.wrap(nw["cond"], F("Bold", 56), 470, 2)
        y = 205
        for ln in cond_lines:
            p.text((520, y), ln, F("Bold", 56)); y += 66
        p.text((520, max(y + 6, 300)), f'Wind {nw["wind"]}', F("Medium", 34))
        days = weather["days"]
        yy = max(y + 6, 300) + 50
        for dd in days[:2]:
            if dd["hi"] is None and dd["lo"] is not None:
                line = f'Tonight low {dd["lo"]}°'
            else:
                line = f'{dd["label"].title()}: {dd["cond"].lower()}, high {dd["hi"]}°'
            for ln in p.wrap(line, F("Medium", 32), 500, 1):
                p.text((520, yy), ln, F("Medium", 32)); yy += 44
    else:
        p.unavailable(190, "Weather")
    # --- rain alert
    al = rain_alert(weather, now)
    if al:
        d.rectangle([48, 455, W - 48, 562], outline=0, width=5)
        p.text((72, 470), al[0], F("ExtraBold", 32))
        p.text((72, 516), p.wrap(al[1], F("Medium", 27), W - 190, 1)[0], F("Medium", 27))
    p.rule(583)
    # --- SURF
    p.text((48, 598), "SURF", F("Bold", 26))
    if surf and "ft" in surf:
        p.text((48, 630), f'~{surf["ft"]:.1f} ft', F("ExtraBold", 130))
        p.text((48, 770), f'waves ({surf["ft"] / 3.28084:.1f} m) at the offshore buoy', F("Medium", 28))
        per = f'{surf["period"]:.0f} s' if surf.get("period") else "–"
        p.text((560, 640), per, F("ExtraBold", 64)); p.text((560, 716), "dominant period", F("Medium", 26))
        if surf.get("dir") is not None:
            p.text((800, 640), compass(surf["dir"]), F("ExtraBold", 64)); p.text((800, 716), f'from {surf["dir"]:.0f}°', F("Medium", 26))
        if surf.get("water_f") is not None:
            p.text((560, 770), f'Water {surf["water_f"]:.0f}°F', F("Bold", 34))
        sw = surf.get("swell")
        if sw:
            line = f'Swell: {sw["ft"]:.1f} ft at {sw["per"]:.0f} s from {sw["dir"]}'
            if sw.get("wft") is not None:
                line += f'  +  wind wave {sw["wft"]:.1f} ft at {sw["wper"]:.0f} s from {sw["wdir"]}'
            p.text((48, 830), p.wrap(line, F("Medium", 26), W - 96, 1)[0], F("Medium", 26))
        t = surf["time"]
        p.text((48, 868), f'Torrey Pines Outer buoy ({NDBC_STATION}) as of {fmt_time(t)}. The Shores is usually smaller.',
               F("Regular", 24), fill=70)
    else:
        p.unavailable(640, "Surf data")
    p.rule(915)
    # --- TIDE
    p.text((48, 938), "TIDE", F("Bold", 26))
    if tides:
        naive_now = now.replace(tzinfo=None)
        x0 = naive_now - dt.timedelta(hours=6)
        x1 = naive_now + dt.timedelta(hours=18)
        cx0, cx1, cy0, cy1 = 70, W - 60, 1010, 1210
        vals = [v for t, v, _ in tides if x0 - dt.timedelta(hours=7) <= t <= x1 + dt.timedelta(hours=7)] or [0, 6]
        vmin, vmax = min(min(vals), 0) - 0.4, max(vals) + 0.9

        def X(t): return cx0 + (t - x0) / (x1 - x0) * (cx1 - cx0)
        def Y(v): return cy1 - (v - vmin) / (vmax - vmin) * (cy1 - cy0)
        d.line([cx0, Y(0), cx1, Y(0)], fill=150, width=2)
        p.text((cx0 - 8, Y(0)), "0", F("Medium", 22), anchor="rm")
        # hour ticks every 6h aligned to clock
        t = x0.replace(minute=0, second=0, microsecond=0)
        while t <= x1:
            if t >= x0 and t.hour % 6 == 0:
                d.line([X(t), cy1, X(t), cy1 + 10], fill=0, width=2)
                p.text((X(t), cy1 + 14), fmt_time_short(t).replace(":00", ""), F("Medium", 22), anchor="ma")
            t += dt.timedelta(hours=1)
        steps = 240
        curve = []
        for i in range(steps + 1):
            tt = x0 + (x1 - x0) * i / steps
            curve.append((X(tt), Y(tide_at(tides, tt))))
        d.polygon(curve + [(X(x1), Y(vmin)), (X(x0), Y(vmin))], fill=225)
        d.line(curve, fill=0, width=5)
        nx = X(naive_now)
        d.line([nx, cy0 - 10, nx, cy1], fill=0, width=3)
        p.text((nx, cy0 - 14), "now", F("Bold", 24), anchor="ms")
        ny = Y(tide_at(tides, naive_now))
        d.ellipse([nx - 9, ny - 9, nx + 9, ny + 9], fill=0)
        for tt, v, typ in tides:
            if x0 <= tt <= x1:
                lab = f'{"High" if typ == "H" else "Low"} {fmt_time(tt)}  {v:+.1f} ft'
                xx = min(max(X(tt), 210), W - 210)
                p.text((xx, Y(v) - 34) if typ == "H" else (xx, cy1 + 44), lab, F("Bold", 25), anchor="ma")
        nxt = [(tt, v, typ) for tt, v, typ in tides if tt > naive_now][:3]
        line = "Next: " + " · ".join(f'{"High" if typ == "H" else "Low"} {fmt_time(tt)} ({v:.1f} ft)' for tt, v, typ in nxt)
        p.text((48, 1304), p.wrap(line, F("Medium", 27), W - 96, 1)[0], F("Medium", 27))
    else:
        p.unavailable(1000, "Tide data")
    p.footer(f"Weather: NWS · Surf: NDBC {NDBC_STATION} · Tides: NOAA {TIDE_STATION}")
    return p


def page_week(now, weather, tides, n, total):
    p = Page("THE WEEK AHEAD", now, n, total)
    d = p.d
    if not weather or not weather["days"]:
        p.unavailable(160, "Forecast")
        p.footer("Weather: NWS")
        return p
    p.text((W - 48 - 140, 140), "HIGH", F("Bold", 24), anchor="ra"); p.text((W - 48, 140), "LOW", F("Bold", 24), anchor="ra")
    y = 175
    for dd in weather["days"][:7]:
        p.text((48, y + 8), dd["label"][:7], F("ExtraBold", 50 if len(dd["label"]) <= 3 else 36))
        cond = p.wrap(dd["cond"], F("Medium", 36), 500, 1)[0]
        p.text((270, y + 8), cond, F("Medium", 36))
        hi = f'{dd["hi"]}°' if dd["hi"] is not None else "–"
        lo = f'{dd["lo"]}°' if dd["lo"] is not None else "–"
        p.text((W - 48 - 140, y + 4), hi, F("ExtraBold", 58), anchor="ra", fill=0 if dd["hi"] is not None else 150)
        p.text((W - 48, y + 4), lo, F("Medium", 58), anchor="ra", fill=0 if dd["lo"] is not None else 150)
        if dd["pop"] >= 30:
            p.text((270, y + 56), f'{dd["pop"]}% chance of rain', F("Regular", 22), fill=60)
        elif dd["night_pop"] >= 40 and re.search(r"rain|shower|storm|thunder", dd["night_cond"], re.I):
            p.text((270, y + 56), f'Rain overnight ({dd["night_pop"]}%)', F("Regular", 22), fill=60)
        p.rule(y + 88, 2)
        y += 100
    p.text((48, y + 16), "RAIN OUTLOOK", F("Bold", 26))
    wet = [pp for pp in weather["periods"]
           if ((pp.get("probabilityOfPrecipitation") or {}).get("value") or 0) >= 40
           and re.search(r"rain|shower|storm|thunder", pp["shortForecast"], re.I)]
    lines = [f'{pp["name"]}: {pp["shortForecast"].lower()} ({pp["probabilityOfPrecipitation"]["value"]}%)' for pp in wet[:5]] \
        or ["No significant rain in the forecast"]
    for j, l in enumerate(lines):
        p.text((48, y + 56 + j * 40), "•  " + l, F("Medium", 28))
    y2 = y + 56 + max(len(lines), 1) * 40 + 26
    if tides and y2 < 1180:
        p.rule(y2)
        p.text((48, y2 + 22), "TIDES · LA JOLLA (ft)", F("Bold", 26))
        yy = y2 + 66
        for k in range(1, 3):
            day = (now + dt.timedelta(days=k)).date()
            items = [(t, v, ty) for t, v, ty in tides if t.date() == day]
            if not items:
                continue
            p.text((48, yy), (now + dt.timedelta(days=k)).strftime("%a %-m/%-d"), F("Bold", 28))
            p.text((270, yy + 4), "   ".join(f'{"▲" if ty == "H" else "▼"}{fmt_time_short(t)} {v:.1f}' for t, v, ty in items),
                   F("Medium", 24))
            yy += 44
    p.footer("Weather: NWS San Diego · Tides: NOAA " + TIDE_STATION)
    return p


def page_calendar(now, events, n, total):
    p = Page("CALENDAR", now, n, total)
    if events is None:
        p.unavailable(160, "Calendar")
        p.footer("Google Calendar")
        return p
    y = 140
    days = [now.date() + dt.timedelta(days=i) for i in range(4)]
    shown_any = False
    for day in days:
        evs = [e for e in events if e["day"] == day]
        label = "TODAY" if day == now.date() else ("TOMORROW" if day == now.date() + dt.timedelta(days=1) else day.strftime("%A").upper())
        if y > 1270:
            break
        p.text((48, y), f'{label} · {day.strftime("%b")} {day.day}', F("ExtraBold", 34))
        y += 56
        p.rule(y - 6, 2)
        if not evs:
            p.text((48, y + 4), "Nothing scheduled", F("Regular", 30), fill=90)
            y += 64
            continue
        shown_any = True
        for e in evs[:9]:
            if y > 1280:
                break
            if e["all_day"]:
                when = "All day"
            else:
                when = fmt_time_short(e["start"])
                if e["end"]:
                    when += "–" + fmt_time_short(e["end"])
            past = (not e["all_day"]) and e["end"] and e["end"] < now and day == now.date()
            fill = 120 if past else 0
            p.text((48, y + 4), when, F("Bold", 28), fill=fill)
            title = p.wrap(e["title"], F("Medium", 32), W - 96 - 250, 2)
            for i, ln in enumerate(title):
                p.text((300, y + i * 38), ln, F("Medium", 32), fill=fill)
            y += 38 * len(title) + 22
        if len(evs) > 9:
            p.text((48, y), f"+ {len(evs) - 9} more", F("Regular", 26), fill=90)
            y += 40
        y += 26
    p.footer("Google Calendar (private link)")
    return p


def page_school(now, events, n, total):
    p = Page("HARDY ELEMENTARY", now, n, total)
    if events is None:
        p.unavailable(160, "School calendar")
        p.footer("Hardy PTA calendar")
        return p
    p.text((48, 128), "UPCOMING", F("Bold", 26), fill=70)
    y, last_day, rows = 170, None, 0
    for e in events:
        if rows >= 14 or y > 1250:
            break
        if e["day"] != last_day:
            if last_day is not None:
                y += 12
            lab = "TODAY" if e["day"] == now.date() else ("TOMORROW" if e["day"] == now.date() + dt.timedelta(days=1)
                                                          else e["day"].strftime("%a %b ") + str(e["day"].day))
            p.text((48, y), lab.upper(), F("ExtraBold", 32))
            y += 50
            p.rule(y - 8, 2)
            last_day = e["day"]
        when = "All day" if e["all_day"] else fmt_time_short(e["start"]) + (("–" + fmt_time_short(e["end"])) if e["end"] else "")
        p.text((48, y + 2), when, F("Bold", 26))
        for i, ln in enumerate(p.wrap(e["title"], F("Medium", 30), W - 96 - 250, 2)):
            p.text((300, y + i * 36), ln, F("Medium", 30))
            y += 36 if i else 0
        y += 46
        rows += 1
    if rows == 0:
        p.text((48, 190), "No events in the next 60 days", F("Regular", 30), fill=90)
    p.footer("Hardy PTA calendar (public) · check the school site for changes")
    return p


def page_news(now, items, n, total):
    p = Page("HACKER NEWS", now, n, total)
    if not items:
        p.unavailable(160, "Headlines")
        p.footer("news.ycombinator.com")
        return p
    y = 140
    for i, it in enumerate(items, 1):
        if y > 1250:
            break
        p.text((48, y), f"{i}", F("ExtraBold", 40))
        lines = p.wrap(it["title"], F("Bold", 31), W - 96 - 90, 2)
        for j, ln in enumerate(lines):
            p.text((120, y + j * 38 - 2), ln, F("Bold", 31))
        meta = f'{it["score"]} pts · {it["comments"]} comments' + (f' · {it["domain"]}' if it["domain"] else "")
        p.text((120, y + len(lines) * 38 + 2), meta, F("Regular", 23), fill=80)
        y += len(lines) * 38 + 2 + 44
        p.rule(y - 14, 1)
    p.footer("Hacker News front page")
    return p


# --------------------------------------------------------------------------- main
def page_lock(now, weather, surf, tides):
    im = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(im)
    def T(xy, s, font, fill=0, anchor="la"):
        d.text(xy, s, font=font, fill=fill, anchor=anchor)
    window = best_window(weather, now)
    call = surf_call(surf, window)
    T((W // 2, 70), PLACE, F("ExtraBold", 44), anchor="ma")
    d.rectangle([48, 135, W - 48, 139], fill=0)
    # --- surf call
    T((W // 2, 175), "SURF", F("Bold", 36), fill=60, anchor="ma")
    T((W // 2, 215), call or "—", F("ExtraBold", 270), anchor="ma")
    if window:
        a, b = window[0], window[1]
        sa = fmt_time_short(a).replace("a", "").replace("p", "") if a.strftime("%p") == b.strftime("%p") else fmt_time_short(a)
        T((W // 2, 530), f"Best window  {sa}–{fmt_time_short(b)}", F("Bold", 54), anchor="ma")
        wd = "offshore" if window[3] else "wind"
        T((W // 2, 602), f"{round(window[2])} mph {wd}", F("Medium", 36), fill=60, anchor="ma")
    elif call:
        T((W // 2, 530), "No wind forecast available", F("Medium", 40), fill=60, anchor="ma")
    if surf and surf.get("ft") is not None:
        per = f' @ {surf["period"]:.0f}s' if surf.get("period") else ""
        T((W // 2, 665), f'Swell {surf["ft"]:.1f} ft{per}', F("Medium", 40), anchor="ma")
    d.rectangle([48, 740, W - 48, 743], fill=0)
    # --- temps
    air = weather["now"]["temp"] if weather and weather.get("now") else None
    water = surf.get("water_f") if surf else None
    col = [(W // 4, "AIR", f"{air}°" if air is not None else "—"),
           (3 * W // 4, "WATER", f"{round(water)}°" if water is not None else "—")]
    for x, label, val in col:
        T((x, 775), label, F("Bold", 36), fill=60, anchor="ma")
        T((x, 815), val, F("ExtraBold", 210), anchor="ma")
    d.rectangle([W // 2 - 1, 790, W // 2 + 1, 1050], fill=0)
    days = (weather or {}).get("days") or []
    if days and days[0].get("hi") is not None and days[0].get("lo") is not None:
        T((W // 4, 1060), f'High {days[0]["hi"]}°  ·  Low {days[0]["lo"]}°', F("Medium", 36), anchor="ma")
    elif days and days[0].get("lo") is not None:
        T((W // 4, 1060), f'Tonight low {days[0]["lo"]}°', F("Medium", 36), anchor="ma")
    if weather and weather.get("now"):
        T((W // 4, 1100), weather["now"]["cond"], F("Medium", 32), fill=60, anchor="ma")
    if surf and surf.get("time"):
        T((3 * W // 4, 1060), f'Buoy {fmt_time_short(surf["time"])}', F("Medium", 32), fill=60, anchor="ma")
    # --- rain / tide
    y = 1160
    ra = rain_alert(weather, now)
    if ra:
        d.rectangle([48, y, W - 48, y + 100], outline=0, width=4)
        T((72, y + 14), ra[0], F("ExtraBold", 34))
        T((72, y + 56), ra[1], F("Medium", 28))
        y += 125
    if tides:
        nxt = [p for p in tides if p[0] > now.replace(tzinfo=None)][:2]
        if nxt:
            parts = [f'{"High" if p[2] == "H" else "Low"} {fmt_time_short(p[0])} {p[1]:.1f} ft' for p in nxt]
            T((W // 2, y + 10), "TIDE   " + "   ·   ".join(parts), F("Medium", 32), anchor="ma")
    T((W // 2, 1400), f'Updated {now.strftime("%a %b")} {now.day} · {fmt_time(now)}', F("Regular", 24), fill=90, anchor="ma")
    return im


def main():
    global FIXTURES
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--fixtures")
    a = ap.parse_args()
    FIXTURES = a.fixtures
    os.makedirs(a.out, exist_ok=True)
    now = now_local()

    weather = surf = tides = news = events = school = None
    for label, fn in (("weather", lambda: get_weather(now)), ("surf", get_surf), ("tides", lambda: get_tides(now)),
                      ("news", get_news), ("calendar", lambda: get_calendar(now)),
                      ("school", lambda: get_school(now))):
        try:
            val = fn()
        except Exception as e:  # noqa: BLE001
            print(f"[warn] {label} failed: {type(e).__name__}: {e}", file=sys.stderr)
            val = None
        if label == "weather": weather = val
        elif label == "surf": surf = val
        elif label == "tides": tides = val
        elif label == "news": news = val
        elif label == "school": school = val
        else: events = val
        print(f"[{label}] {'ok' if val is not None else 'UNAVAILABLE'}")

    pages = [("01-today.png", lambda n, t: page_today(now, weather, surf, tides, n, t)),
             ("02-week.png", lambda n, t: page_week(now, weather, tides, n, t))]
    if os.environ.get("CAL_ICS_URL") or FIXTURES:
        pages.append(("03-calendar.png", lambda n, t: page_calendar(now, events, n, t)))
    if school is not None:  # skip the page entirely if the feed is down, rather than publish an error page
        pages.append(("04-school.png", lambda n, t: page_school(now, school, n, t)))
    pages.append(("05-news.png", lambda n, t: page_news(now, news, n, t)))

    pdf_pages = []
    manifest = {"generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "generated_local": now.isoformat(timespec="minutes"), "pages": [], "sha256": {}}
    total = len(pages)
    for i, (name, fn) in enumerate(pages, 1):
        page = fn(i, total)
        # Each page is also its own one-page PDF: KOReader's library treats it as a document (a tile on the
        # home screen), whereas plain PNGs are not listed there.
        name = name.replace(".png", ".pdf")
        path = os.path.join(a.out, name)
        page.im.save(path, "PDF", resolution=300.0, quality=95)
        pdf_pages.append(page.im)
        with open(path, "rb") as f:
            manifest["sha256"][name] = hashlib.sha256(f.read()).hexdigest()
        manifest["pages"].append(name)
    # One multi-page PDF so KOReader can swipe between pages (separate images do not page).
    pdf_path = os.path.join(a.out, "00-dashboard.pdf")
    pdf_pages[0].save(pdf_path, "PDF", save_all=True, append_images=pdf_pages[1:], resolution=300.0, quality=95)
    with open(pdf_path, "rb") as f:
        manifest["sha256"]["00-dashboard.pdf"] = hashlib.sha256(f.read()).hexdigest()
    manifest["pages"].insert(0, "00-dashboard.pdf")
    # Lock-screen image (served separately; the Kindle puts it in its own sleep-screen folder).
    try:
        lock = page_lock(now, weather, surf, tides)
        lock_name = "lock-01.png"
        lock_path = os.path.join(a.out, lock_name)
        lock.save(lock_path, optimize=True)
        with open(lock_path, "rb") as f:
            manifest["sha256"][lock_name] = hashlib.sha256(f.read()).hexdigest()
        manifest["pages"].append(lock_name)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] lock screen failed: {type(e).__name__}: {e}", file=sys.stderr)
    with open(os.path.join(a.out, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=1)
    # Fail the build only if nothing useful came back at all (so a stale page isn't published over a good one).
    if weather is None and surf is None and tides is None and news is None:
        print("All data sources failed; refusing to publish.", file=sys.stderr)
        sys.exit(2)
    print("built", ", ".join(manifest["pages"]))


if __name__ == "__main__":
    main()
