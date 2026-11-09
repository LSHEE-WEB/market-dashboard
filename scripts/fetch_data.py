#!/usr/bin/env python3
"""
원자재 시황 데이터 수집기
- 환율: 서울외국환중개(SMBS) 매매기준율 (실패 시 Yahoo Finance로 대체)
- 유가: WTI / 브렌트 (Yahoo Finance 선물 일봉)
- 나프타 / 에틸렌 / 펄프: 산업통상부 원자재가격정보 (KoreaPDS 제공 페이지)

결과는 data/prices.json 에 누적 저장되며, 직전 관측치 대비 ±ALERT_THRESHOLD% 이상
변동하면 알림(GitHub 이슈 / 텔레그램)을 보냅니다.
"""
from __future__ import annotations

import json
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA_FILE = ROOT / "data" / "prices.json"
DEBUG_DIR = ROOT / "data" / "debug"
KST = timezone(timedelta(hours=9))
ALERT_THRESHOLD = float(os.environ.get("ALERT_THRESHOLD", "5"))
MAX_POINTS = 800
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
}

SMBS_URL = "http://www.smbs.biz/Flash/TodayExRate_flash.jsp?tr_date={date}"
MOTIR_PAGE = "https://www.motir.go.kr/kor/contents/103"
KPDS_URL = os.environ.get("KPDS_URL", "https://www.koreapds.com/wacs/wms/kpds_price.php")

SERIES = {
    "usdkrw": dict(name="원/달러", group="fx", unit="원", source="서울외국환중개 매매기준율",
                   link="http://www.smbs.biz/ExRate/TodayExRate.jsp", yahoo="KRW=X", mult=1),
    "cnykrw": dict(name="원/위안", group="fx", unit="원", source="서울외국환중개 매매기준율",
                   link="http://www.smbs.biz/ExRate/TodayExRate.jsp", yahoo="CNYKRW=X", mult=1),
    "jpykrw": dict(name="원/100엔", group="fx", unit="원", source="서울외국환중개 매매기준율",
                   link="http://www.smbs.biz/ExRate/TodayExRate.jsp", yahoo="JPYKRW=X", mult=100),
    "wti": dict(name="WTI 원유", group="oil", unit="USD/bbl", source="NYMEX WTI 선물 (Yahoo Finance)",
                link="https://tradingeconomics.com/commodity/crude-oil", yahoo="CL=F", mult=1),
    "brent": dict(name="브렌트유", group="oil", unit="USD/bbl", source="ICE Brent 선물 (Yahoo Finance)",
                  link="https://tradingeconomics.com/commodity/brent-crude-oil", yahoo="BZ=F", mult=1),
    "naphtha": dict(name="나프타", group="petchem", unit="USD/MT", source="산업통상부 원자재가격정보",
                    link=MOTIR_PAGE, keywords=["나프타", "납사", "Naphtha", "NAPHTHA"]),
    "ethylene": dict(name="에틸렌", group="petchem", unit="USD/MT", source="산업통상부 원자재가격정보",
                     link=MOTIR_PAGE, keywords=["에틸렌", "Ethylene", "ETHYLENE"]),
    "pulp": dict(name="펄프 (SBHK)", group="petchem", unit="USD/MT", source="산업통상부 원자재가격정보",
                 link=MOTIR_PAGE, keywords=["SBHK", "활엽수", "BHKP", "펄프", "Pulp", "PULP"]),
    "palmoil": dict(name="팜오일", group="petchem", unit="MYR/MT", source="Trading Economics (말레이시아 팜유 선물)",
                    link="https://ko.tradingeconomics.com/commodity/palm-oil"),
}
# 에틸렌을 찾을 때 섞여 나오기 쉬운 다른 품목
EXCLUDE = {"ethylene": ["폴리", "PE", "글리콜", "EG", "EDC", "Poly", "POLY"],
           "naphtha": [], "pulp": ["폐지", "고지", "Paper", "종이"]}


# ---------------------------------------------------------------- utils
def now_kst() -> datetime:
    return datetime.now(KST)


def log(*a):
    print("[fetch]", *a, flush=True)


def to_float(s: str) -> float | None:
    if s is None:
        return None
    s = str(s).strip().replace(",", "").replace("$", "")
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group()) if m else None


def save_debug(name: str, text: str):
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    (DEBUG_DIR / name).write_text(text[:200_000], encoding="utf-8")


def load_data() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            log("prices.json 파싱 실패 - 새로 만듭니다")
    return {"series": {}, "alerts": [], "sent_alerts": []}


def upsert(hist: list, date: str, value: float):
    """date 기준으로 값 갱신/추가 후 날짜순 정렬"""
    for p in hist:
        if p["date"] == date:
            p["value"] = value
            break
    else:
        hist.append({"date": date, "value": value})
    hist.sort(key=lambda p: p["date"])
    del hist[:-MAX_POINTS]


# ---------------------------------------------------------------- SMBS (환율)
SMBS_KEYS = {
    "usdkrw": [r"USD"],
    "cnykrw": [r"CNY", r"CNH"],
    "jpykrw": [r"JPY(?:\(100\))?", r"JPY100"],
}


def parse_smbs(text: str) -> dict:
    """'...&USD=1,383.50&JPY=912.3&...' 형태의 응답에서 통화값 추출"""
    out = {}
    for key, pats in SMBS_KEYS.items():
        for pat in pats:
            m = re.search(r"(?<![A-Z])" + pat + r"\s*=\s*([0-9][0-9,]*\.?[0-9]*)", text)
            if m:
                v = to_float(m.group(1))
                if v:
                    if key == "jpykrw" and v < 100:  # 1엔 기준이면 100엔 기준으로
                        v = round(v * 100, 4)
                    out[key] = v
                    break
    return out


def fetch_smbs(date: str, debug: bool = True) -> dict:
    r = requests.get(SMBS_URL.format(date=date), headers=UA, timeout=20)
    r.raise_for_status()
    text = r.content.decode(r.encoding or "utf-8", errors="ignore")
    vals = parse_smbs(text)
    if not vals and debug:
        save_debug(f"smbs_{date}.txt", text)
    return vals


FX_BACKFILL_DAYS = 730   # 환율 과거 데이터 범위 (약 2년)


def update_fx(data: dict):
    """최근 6일은 매번 다시 확인, 그 이전 2년 중 비어 있는 영업일은 한 번만 채움(휴일은 기록해 두고 건너뜀)"""
    from concurrent.futures import ThreadPoolExecutor
    series = data["series"]
    today = now_kst().date()
    have = set.intersection(*[{p["date"] for p in series[k]["history"]} for k in SMBS_KEYS])
    empty = set(data.get("smbs_empty_dates", []))
    targets = []
    for i in range(FX_BACKFILL_DAYS, -1, -1):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        ds = d.isoformat()
        if i <= 6 or (ds not in have and ds not in empty):
            targets.append((ds, i <= 6))
    log(f"환율 조회 {len(targets)}일")
    ok_any = False
    errors = []

    def job(t):
        ds, recent = t
        try:
            return ds, recent, fetch_smbs(ds, debug=recent), None
        except Exception as e:
            return ds, recent, None, e

    with ThreadPoolExecutor(max_workers=6) as ex:
        for ds, recent, vals, err in ex.map(job, targets):
            if err:
                errors.append(f"{ds}: {err}")
                continue
            if not vals:
                if not recent:
                    empty.add(ds)   # 휴일 등 고시 없는 날
                continue
            for k, v in vals.items():
                upsert(series[k]["history"], ds, v)
                ok_any = True
    data["smbs_empty_dates"] = sorted(empty)[-400:]
    if not ok_any and have:
        ok_any = not errors  # 새 값이 없어도 오류가 없으면 정상
    if ok_any:
        for k in SMBS_KEYS:
            series[k]["status"] = "ok"
            series[k]["source_used"] = "서울외국환중개"
            series[k].pop("error", None)
        return
    # 대체: Yahoo Finance
    log("SMBS 실패 → Yahoo Finance 대체", errors[-3:])
    for k in SMBS_KEYS:
        try:
            update_yahoo(series, k, fallback_note="서울외국환중개 접속 실패 → Yahoo Finance 대체")
        except Exception as e:
            series[k]["status"] = "error"
            series[k]["error"] = f"SMBS/Yahoo 모두 실패: {e}"


# ---------------------------------------------------------------- Yahoo (유가, 환율 대체)
def fetch_yahoo(symbol: str, rng: str = "2y") -> list[tuple[str, float]]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={rng}&interval=1d"
    last_err = None
    for host in ("query1", "query2"):
        try:
            r = requests.get(url.replace("query1", host), headers=UA, timeout=20)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            off = res["meta"].get("gmtoffset", 0)
            ts = res.get("timestamp") or []
            closes = res["indicators"]["quote"][0]["close"]
            out = []
            for t, c in zip(ts, closes):
                if c is None:
                    continue
                d = datetime.fromtimestamp(t + off, tz=timezone.utc).date().isoformat()
                out.append((d, round(float(c), 4)))
            # 장중 실시간 가격으로 마지막 값 보정
            live = res["meta"].get("regularMarketPrice")
            if live and out:
                out[-1] = (out[-1][0], round(float(live), 4))
            return out
        except Exception as e:
            last_err = e
    raise RuntimeError(f"Yahoo {symbol} 실패: {last_err}")


def update_yahoo(series: dict, key: str, fallback_note: str | None = None):
    meta = SERIES[key]
    rows = fetch_yahoo(meta["yahoo"])
    hist = series[key]["history"]
    for d, v in rows:
        upsert(hist, d, round(v * meta.get("mult", 1), 4))
    series[key]["status"] = "ok"
    series[key]["source_used"] = "Yahoo Finance"
    series[key].pop("error", None)
    if fallback_note:
        series[key]["note"] = fallback_note
    else:
        series[key].pop("note", None)


# ---------------------------------------------------------------- 산업통상부 / KoreaPDS
DATE_RE = re.compile(r"(20\d{2})[.\-/년 ]\s*(\d{1,2})[.\-/월 ]\s*(\d{1,2})")
YM_RE = re.compile(r"(20\d{2})[.\-/년 ]\s*(\d{1,2})(?!\d)")
VALUE_HDR = ["현재", "금주", "금일", "당일", "당월", "이번", "최근", "가격", "Price", "Last", "종가"]
SKIP_HDR = ["전일", "전주", "전월", "전년", "대비", "등락", "변동", "%", "Chg", "단위"]


def norm_date(s: str) -> str | None:
    m = DATE_RE.search(s or "")
    if m:
        y, mo, d = map(int, m.groups())
        try:
            return datetime(y, mo, d).date().isoformat()
        except ValueError:
            return None
    m = YM_RE.search(s or "")
    if m:
        y, mo = map(int, m.groups())
        if 1 <= mo <= 12:
            return f"{y:04d}-{mo:02d}-01"
    return None


def parse_kpds(html: str) -> dict:
    """원자재가격정보 표에서 품목별 (날짜, 값, 원본행) 추출. 표 구조가 바뀌어도 최대한 견디도록 휴리스틱 사용."""
    soup = BeautifulSoup(html, "html.parser")
    page_date = norm_date(soup.get_text(" ", strip=True)[:3000])
    found: dict = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        header: list[str] = []
        for tr in rows:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if not cells:
                continue
            if tr.find("th") and not tr.find("td"):
                header = cells
                continue
            name = next((c for c in cells if c and not re.fullmatch(r"[\d,.\-+% ]+", c)), "")
            for key in ("naphtha", "ethylene", "pulp"):
                if key in found and not (key == "pulp" and not found[key].get("preferred")):
                    continue
                kws = SERIES[key]["keywords"]
                if not any(k in name for k in kws):
                    continue
                if any(x in name for x in EXCLUDE[key]):
                    continue
                value = pick_value(cells, header, name)
                if value is None:
                    continue
                date = None
                for c in cells:
                    date = norm_date(c)
                    if date:
                        break
                if not date and header:
                    for h in header:
                        date = norm_date(h)
                        if date:
                            break
                preferred = key != "pulp" or "SBHK" in name or "활엽수" in name
                if key in found and not preferred:
                    continue  # 펄프는 SBHK(활엽수) 행을 우선
                found[key] = dict(value=value, date=date or page_date, row=cells, header=header,
                                  preferred=preferred)
    return found


def pick_value(cells: list[str], header: list[str], name: str) -> float | None:
    # 1) 헤더가 있으면 '현재/금주/가격' 등 열을 사용 (대비/등락 열 제외)
    if header and len(header) == len(cells):
        for i, h in enumerate(header):
            if any(k in h for k in VALUE_HDR) and not any(k in h for k in SKIP_HDR):
                v = to_float(cells[i])
                if v and v > 1:
                    return v
    # 2) 아니면 품목명 이후 첫 번째 '가격 같은' 숫자
    start = cells.index(name) + 1 if name in cells else 0
    for c in cells[start:]:
        if "%" in c or norm_date(c):
            continue
        v = to_float(c)
        if v and v > 10:
            return v
    return None


def update_petchem(data: dict):
    """1순위: KoreaPDS 차트 데이터(2년 이력) / 2순위: 원자재가격 표에서 최신값"""
    series = data["series"]
    table = latest_table_dates()
    pending = []
    for key in ("naphtha", "ethylene", "pulp"):
        s = series[key]
        end_date = end_value = None
        if key in table:
            end_date = datetime.fromisoformat(table[key]["date"]).date()
            end_value = table[key]["value"]
        try:
            rows, approx = fetch_kpds_history(key, 730, end_date, end_value)
            if approx:
                # 날짜를 추정한 경우: 매번 새로 계산하므로 2년 구간은 통째로 교체 (중복 방지)
                first = rows[0][0]
                s["history"] = [p for p in s["history"] if p["date"] < first] + \
                               [{"date": d, "value": v} for d, v in rows]
                s["note"] = "일간 차트 날짜는 월 표시 기준 추정값입니다 (수일 오차 가능)"
            else:
                for d, v in rows:
                    upsert(s["history"], d, v)
                s.pop("note", None)
            # 표의 최신값이 더 정확하면 마지막 점을 덮어씀
            if key in table:
                upsert(s["history"], table[key]["date"], table[key]["value"])
            s["status"] = "ok"
            s["source_used"] = "KoreaPDS 차트"
            s.pop("error", None)
            log(key, f"차트 이력 {len(rows)}건" + (" (날짜 추정)" if approx else ""))
        except Exception as e:
            log(key, "차트 이력 실패 → 표에서 최신값 시도:", e)
            pending.append(key)
    if pending:
        update_petchem_table(data, pending)


def update_petchem_table(data: dict, keys: list):
    series = data["series"]
    html, err, found = None, None, {}
    for url in (KPDS_URL, MOTIR_PAGE):
        try:
            r = requests.get(url, headers={**UA, "Referer": MOTIR_PAGE}, timeout=30)
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding
            html = r.text
            found = parse_kpds(html)
            if found:
                break
            save_debug(f"petchem_{url.split('/')[-1] or 'page'}.html", html)
            err = f"{url}: 표에서 품목을 찾지 못함"
        except Exception as e:
            err = f"{url}: {e}"
            found = {}
    today = now_kst().date().isoformat()
    for key in keys:
        s = series[key]
        if key in found:
            f = found[key]
            upsert(s["history"], f["date"] or today, f["value"])
            s["status"] = "ok"
            s["raw_row"] = f["row"][:12]
            s.pop("error", None)
        else:
            s["status"] = "error"
            s["error"] = err or "품목을 찾지 못함"


# ---------------------------------------------------------------- KoreaPDS 차트 이력 (FusionCharts XML)
KPDS_CHART_URL = "https://www.koreapds.com/wacs/wms/price_chart_backendprc.php"
# 개발자도구 Payload에서 확인한 품목 코드
KPDS_ITEMS = {
    "naphtha":  dict(large_cd="22", middle_cd="01", small_cd="00", distr_cd="00", item_cd="01", itemd_cd="00", deal_cd="2432", gap_cd="00"),
    "ethylene": dict(large_cd="22", middle_cd="02", small_cd="00", distr_cd="00", item_cd="01", itemd_cd="03", deal_cd="2441", gap_cd="00"),
    "pulp":     dict(large_cd="29", middle_cd="01", small_cd="00", distr_cd="00", item_cd="01", itemd_cd="16", deal_cd="3010", gap_cd="00"),
}
MONTHS_EN = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def _attr(tag: str, name: str) -> str | None:
    m = re.search(r"\b" + name + r"\s*=\s*['\"]([^'\"]*)['\"]", tag, flags=re.I)
    return m.group(1) if m else None


def _month_day(s: str) -> tuple[int, int] | None:
    """연도 없는 라벨: '08/03', '8.3', '08-03', '8월 3일', 'Aug 3', '3 Aug'"""
    s = s.strip()
    m = re.search(r"(?<!\d)(\d{1,2})\s*[./\-월]\s*(\d{1,2})(?!\d)", s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return mo, d
    m = re.search(r"([A-Za-z]{3})[a-z]*\.?\s*(\d{1,2})|(\d{1,2})\s*([A-Za-z]{3})", s)
    if m:
        mon = (m.group(1) or m.group(4) or "").lower()
        day = int(m.group(2) or m.group(3))
        if mon in MONTHS_EN:
            return MONTHS_EN[mon], day
    return None


def _month_marker(label: str) -> tuple[int, int | None] | None:
    """'Nov{br}`24' → (11, 2024), 'Mar' → (3, None)"""
    s = re.sub(r"\{br\}|<br\s*/?>|&#10;", " ", label or "").strip()
    m = re.fullmatch(r"([A-Za-z]{3})[a-z]*\.?\s*(?:[`'’]?\s*(\d{2}|\d{4}))?", s)
    if not m or m.group(1).lower() not in MONTHS_EN:
        return None
    y = m.group(2)
    return MONTHS_EN[m.group(1).lower()], (None if not y else int(y) + (2000 if len(y) == 2 else 0))


def _weekdays(y: int, mo: int) -> list:
    from calendar import monthrange
    return [datetime(y, mo, d).date() for d in range(1, monthrange(y, mo)[1] + 1)
            if datetime(y, mo, d).weekday() < 5]


def _prev_weekday(d):
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _dates_from_month_markers(labels, values, today, end_date=None) -> list[tuple[str, float]]:
    """일간 차트인데 점마다 날짜가 없고 일정 간격으로 월 표시('Nov{br}`24', 'Mar' …)만 있는 경우.
    마지막 점 = 최신 발표일(end_date)로 고정하고, 각 월 표시 점이 해당 월 안에 들어가도록
    '점 1개당 평균 달력일수'를 맞춘 뒤 평일로 맞춰 날짜를 추정한다 (오차: 보통 수일 이내)."""
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if not pts:
        return []
    n = len(values)
    end = _prev_weekday(end_date or (today - timedelta(days=1)))
    idx = [(i, _month_marker(l)) for i, l in enumerate(labels) if _month_marker(l)]
    # 연도 채우기 (마지막 표시부터 거꾸로, 연도 표기가 있으면 그것 우선)
    ys = [0] * len(idx)
    for k in range(len(idx) - 1, -1, -1):
        mo, y = idx[k][1]
        if y:
            ys[k] = y
        elif k == len(idx) - 1:
            ys[k] = end.year if mo <= end.month else end.year - 1
        else:
            ys[k] = ys[k + 1] - (1 if mo >= idx[k + 1][1][0] else 0)
    # 구간별 기준점: 마지막 점 = end. 거꾸로 가며 각 월 표시 점을
    # '평일 기준 간격(1.4일/점)'으로 추정하되, 그 달 범위를 벗어나면 달 안으로 맞춘다.
    anchors = {n - 1: 0.0}          # end 기준 '며칠 전'(실수)
    cur_i, cur_back = n - 1, 0.0
    for (i, (mo, _)), y in sorted(zip(idx, ys), key=lambda t: -t[0][0]):
        if i >= cur_i:
            continue
        first = datetime(y, mo, 1).date()
        while first.weekday() >= 5:      # 그 달 첫 평일
            first += timedelta(days=1)
        last = _prev_weekday(datetime(y + (mo == 12), mo % 12 + 1, 1).date() - timedelta(days=1))
        back = cur_back + (cur_i - i) * 1.4
        back = min(max(back, (end - last).days), (end - first).days)
        back = max(back, cur_back + (cur_i - i))   # 점마다 최소 하루
        anchors[i] = back
        cur_i, cur_back = i, back
    keys = sorted(anchors)
    def est(i):
        if i <= keys[0]:
            back = anchors[keys[0]] + (keys[0] - i) * 1.4
        else:
            back = 0.0
            for a_i, b_i in zip(keys, keys[1:]):
                if a_i <= i <= b_i:
                    back = anchors[a_i] + (anchors[b_i] - anchors[a_i]) * (i - a_i) / (b_i - a_i)
                    break
        return end - timedelta(days=round(back))
    out = []
    nxt = None
    for i, v in reversed(pts):
        d = _prev_weekday(est(i))
        if nxt is not None and d >= nxt:
            d = _prev_weekday(nxt - timedelta(days=1))
        nxt = d
        out.append((d.isoformat(), v))
    return list(reversed(out))


def parse_fusion_xml(xml: str, today=None, end_date=None) -> list[tuple[str, float]]:
    """FusionCharts XML에서 (날짜, 값) 목록 추출.
    날짜는 set의 label/toolText 또는 categories의 label에서 읽고, 연도가 없으면 최신=올해로 거꾸로 추정."""
    today = today or now_kst().date()
    sets = re.findall(r"<set\b[^>]*>", xml, flags=re.I)
    cats = [c for c in re.findall(r"<category\b[^>]*>", xml, flags=re.I)]
    labels, values = [], []
    for i, s in enumerate(sets):
        v = to_float(_attr(s, "value"))
        lab = _attr(s, "label") or _attr(s, "toolText") or _attr(s, "tooltext") or ""
        if not lab and i < len(cats):
            lab = _attr(cats[i], "label") or _attr(cats[i], "toolText") or ""
        labels.append(lab)
        values.append(v)
    # toolText에 날짜가 있으면 그걸 우선 (label은 축 표시용으로 축약된 경우가 많음)
    full = []
    for s, lab in zip(sets, labels):
        tt = _attr(s, "toolText") or _attr(s, "tooltext") or ""
        full.append(norm_date(tt) if DATE_RE.search(tt) else norm_date(lab) if DATE_RE.search(lab) else None)
    out: list[tuple[str, float]] = []
    if any(full):
        for d, v in zip(full, values):
            if d and v is not None:
                out.append((d, v))
        return out
    # 월 표시만 있는 일간 차트 (예: 'Nov{br}`24', 'Mar', …, 나머지 라벨은 빈칸)
    if any(_month_marker(l) for l in labels) and not any(_month_day(l) for l in labels):
        return _dates_from_month_markers(labels, values, today, end_date)
    # 연도 없는 라벨 → 마지막(최신)부터 거꾸로 연도 추정
    md = [_month_day(l) for l in labels]
    if not any(md):
        return []
    year = today.year
    nxt = None
    rev = []
    for x, v in zip(reversed(md), reversed(values)):
        if x is None or v is None:
            continue
        mo, d = x
        if nxt is None:
            if (mo, d) > (today.month, today.day):
                year -= 1
        elif (mo, d) > nxt:
            year -= 1
        nxt = (mo, d)
        try:
            rev.append((datetime(year, mo, d).date().isoformat(), v))
        except ValueError:
            continue
    return list(reversed(rev))


def fetch_kpds_history(key: str, days: int, end_date=None, end_value=None) -> tuple[list[tuple[str, float]], bool]:
    """(날짜, 값) 목록과 '날짜 추정 여부'를 돌려줌"""
    import random
    form = {**KPDS_ITEMS[key], "per": str(days), "rand": str(random.random())}
    headers = {**UA, "Referer": KPDS_URL, "X-Requested-With": "XMLHttpRequest",
               "Origin": "https://www.koreapds.com"}
    r = requests.post(KPDS_CHART_URL, data=form, headers=headers, timeout=30)
    r.raise_for_status()
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding
    rows = parse_fusion_xml(r.text, end_date=end_date)
    # 표의 최신값과 차트 마지막 값이 다르면 차트가 하루 늦은 것 → 기준일을 직전 평일로
    if rows and end_date and end_value is not None and abs(rows[-1][1] - end_value) > 1e-6:
        rows = parse_fusion_xml(r.text, end_date=_prev_weekday(end_date - timedelta(days=1)))
    if not rows:
        save_debug(f"kpds_chart_{key}.xml", r.text)
        raise RuntimeError("차트 응답에서 날짜/값을 읽지 못함 (data/debug 확인)")
    labels = re.findall(r"<category\b[^>]*label\s*=\s*['\"]([^'\"]*)", r.text)
    approx = any(_month_marker(l) for l in labels) and not any(_month_day(l) for l in labels)
    return rows, approx


def latest_table_dates() -> dict:
    """원자재가격 표에서 품목별 최신 발표일 (날짜 추정의 기준점)"""
    try:
        r = requests.get(KPDS_URL, headers={**UA, "Referer": MOTIR_PAGE}, timeout=30)
        r.raise_for_status()
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding
        return {k: v for k, v in parse_kpds(r.text).items() if v.get("date")}
    except Exception as e:
        log("표 최신일 조회 실패:", e)
        return {}


# ---------------------------------------------------------------- 과거 데이터 CSV 병합
BACKFILL_FILE = ROOT / "data" / "backfill.csv"
NAME_ALIASES = {
    "naphtha": ["naphtha", "나프타", "납사"], "ethylene": ["ethylene", "에틸렌"],
    "pulp": ["pulp", "펄프", "sbhk"], "usdkrw": ["usdkrw", "달러", "usd"], "cnykrw": ["cnykrw", "위안", "cny"],
    "jpykrw": ["jpykrw", "엔", "jpy"], "wti": ["wti"], "brent": ["brent", "브렌트"],
    "palmoil": ["palmoil", "팜", "palm"],
}


def col_key(h: str) -> str | None:
    h = h.strip().lower()
    for key, names in NAME_ALIASES.items():
        if any(n == h or (len(n) > 1 and n in h) for n in names):
            return key
    return None


def merge_backfill(data: dict):
    """data/backfill.csv (첫 열=날짜, 다음 열=품목) 내용을 이력에 합침. 여러 번 실행돼도 안전."""
    if not BACKFILL_FILE.exists():
        return
    import csv
    raw = BACKFILL_FILE.read_bytes()
    for enc in ("utf-8-sig", "cp949"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    rows = list(csv.reader(text.splitlines()))
    if not rows:
        return
    keys = [col_key(h) for h in rows[0][1:]]
    n = 0
    for r in rows[1:]:
        if not r:
            continue
        d = norm_date(r[0])
        if not d:
            continue
        for k, cell in zip(keys, r[1:]):
            v = to_float(cell)
            if k and v:
                upsert(data["series"][k]["history"], d, v)
                n += 1
    log(f"backfill.csv 병합: {n}개 값")


# ---------------------------------------------------------------- Trading Economics (팜오일)
TE_PALM_URL = os.environ.get("TE_PALM_URL", "https://ko.tradingeconomics.com/commodity/palm-oil")
ISO_DATE = re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})")


def parse_te_row(html: str, slug: str = "commodity/palm-oil") -> tuple[str | None, float] | None:
    """원자재 표에서 해당 품목 링크가 있는 행의 (날짜, 가격)"""
    soup = BeautifulSoup(html, "html.parser")
    for tr in soup.find_all("tr"):
        a = tr.find("a", href=re.compile(re.escape(slug) + r"/?$"))
        if not a:
            continue
        cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
        price = None
        for c in cells:
            t = c.replace(" ", "")
            if re.fullmatch(r"[\d,]+\.\d+|[\d,]{3,}", t):
                price = to_float(t)
                break
        m = ISO_DATE.search(" ".join(cells))
        if price:
            return (norm_date(m.group(0)) if m else None), price
    return None


def update_palmoil(series: dict):
    s = series["palmoil"]
    r = requests.get(TE_PALM_URL, headers=UA, timeout=30)
    r.raise_for_status()
    r.encoding = "utf-8"
    got = parse_te_row(r.text)
    if not got:
        save_debug("te_palmoil.html", r.text)
        raise RuntimeError("Trading Economics 페이지에서 팜유 가격을 찾지 못함 (data/debug 확인)")
    d, v = got
    upsert(s["history"], d or now_kst().date().isoformat(), v)
    s["status"] = "ok"
    s["source_used"] = "Trading Economics"
    s.pop("error", None)
    log("palmoil", d, v)


# ---------------------------------------------------------------- 알림
def compute_changes(data: dict):
    alerts = []
    for key, s in data["series"].items():
        h = s["history"]
        if len(h) >= 2:
            a, b = h[-2], h[-1]
            pct = (b["value"] - a["value"]) / a["value"] * 100 if a["value"] else 0
            s["change"] = dict(prev_date=a["date"], prev=a["value"], date=b["date"],
                               value=b["value"], pct=round(pct, 2))
            if abs(pct) >= ALERT_THRESHOLD:
                alerts.append(dict(id=f"{key}:{b['date']}", key=key, name=s["name"], unit=s["unit"],
                                   **s["change"]))
        elif h:
            s["change"] = dict(date=h[-1]["date"], value=h[-1]["value"], pct=None)
    data["alerts"] = alerts
    return alerts


def notify(data: dict, alerts: list):
    sent = set(data.get("sent_alerts", []))
    new = [a for a in alerts if a["id"] not in sent]
    if not new:
        return
    lines = []
    for a in new:
        arrow = "▲" if a["pct"] > 0 else "▼"
        lines.append(f"{arrow} {a['name']} {a['pct']:+.2f}%  "
                     f"{a['prev']:,.2f} → {a['value']:,.2f} {a['unit']} ({a['prev_date']} → {a['date']})")
    body = "\n".join(lines)
    title = f"[시황 알림] {', '.join(a['name'] for a in new)} {ALERT_THRESHOLD:g}% 이상 변동"
    site = os.environ.get("SITE_URL", "")
    log("ALERT\n" + body)

    # 1) GitHub 이슈 → 저장소 주인에게 GitHub가 이메일/앱 알림을 보냄
    gh_token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if gh_token and repo and os.environ.get("ALERT_GITHUB_ISSUE", "1") == "1":
        try:
            requests.post(f"https://api.github.com/repos/{repo}/issues",
                          headers={"Authorization": f"Bearer {gh_token}",
                                   "Accept": "application/vnd.github+json"},
                          json={"title": title, "body": body + (f"\n\n대시보드: {site}" if site else ""),
                                "labels": ["price-alert"]}, timeout=20).raise_for_status()
        except Exception as e:
            log("GitHub 이슈 생성 실패", e)

    # 2) 텔레그램 (선택)
    tg_token, tg_chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        try:
            requests.post(f"https://api.telegram.org/bot{tg_token}/sendMessage",
                          json={"chat_id": tg_chat, "text": f"{title}\n\n{body}\n{site}"},
                          timeout=20).raise_for_status()
        except Exception as e:
            log("텔레그램 전송 실패", e)

    data["sent_alerts"] = (list(sent) + [a["id"] for a in new])[-300:]


# ---------------------------------------------------------------- main
def main():
    data = load_data()
    for key, meta in SERIES.items():
        s = data["series"].setdefault(key, {"history": []})
        for f in ("name", "group", "unit", "source", "link"):
            s[f] = meta[f]

    steps = [("환율", lambda: update_fx(data))]
    for k in ("wti", "brent"):
        steps.append((k, lambda k=k: update_yahoo(data["series"], k)))
    steps.append(("석유화학/펄프", lambda: update_petchem(data)))
    steps.append(("palmoil", lambda: update_palmoil(data["series"])))

    for label, fn in steps:
        try:
            fn()
            log(label, "완료")
        except Exception as e:
            log(label, "실패:", e)
            traceback.print_exc()
            keys = SMBS_KEYS if label == "환율" else (["naphtha", "ethylene", "pulp"] if label.startswith("석유") else [label])
            for k in keys:
                data["series"][k]["status"] = "error"
                data["series"][k]["error"] = str(e)

    try:
        merge_backfill(data)
    except Exception as e:
        log("backfill.csv 병합 실패:", e)

    alerts = compute_changes(data)
    notify(data, alerts)
    data["updated_at"] = now_kst().isoformat(timespec="seconds")
    data["threshold"] = ALERT_THRESHOLD
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log("저장 완료:", DATA_FILE)
    # 하나도 성공 못하면 실패로 표시 (Actions에서 빨간불)
    if all(s.get("status") != "ok" for s in data["series"].values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
