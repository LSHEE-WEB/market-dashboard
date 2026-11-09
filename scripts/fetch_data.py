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


def fetch_smbs(date: str) -> dict:
    r = requests.get(SMBS_URL.format(date=date), headers=UA, timeout=20)
    r.raise_for_status()
    text = r.content.decode(r.encoding or "utf-8", errors="ignore")
    vals = parse_smbs(text)
    if not vals:
        save_debug(f"smbs_{date}.txt", text)
    return vals


def update_fx(data: dict):
    series = data["series"]
    have = min(len(series.get(k, {}).get("history", [])) for k in SMBS_KEYS)
    days_back = 120 if have < 20 else 6  # 처음엔 과거 데이터 백필
    today = now_kst().date()
    ok_any = False
    errors = []
    for i in range(days_back, -1, -1):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        ds = d.isoformat()
        try:
            vals = fetch_smbs(ds)
        except Exception as e:
            errors.append(f"{ds}: {e}")
            continue
        for k, v in vals.items():
            upsert(series[k]["history"], ds, v)
            ok_any = True
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
    series = data["series"]
    html, err = None, None
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
    for key in ("naphtha", "ethylene", "pulp"):
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
