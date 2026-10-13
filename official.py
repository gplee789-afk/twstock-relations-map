"""證交所／櫃買中心 OpenAPI：官方上市櫃名單、持股逾 10% 大股東、董監事持股（找出法人董事）。

每份資料一次就是全市場，所以不管抓幾家公司都只要幾個請求。快取在 data/openapi/，抓不到就沿用舊快取。
"""

import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

CACHE_DIR = Path(__file__).resolve().parent / "data" / "openapi"
TWSE = "https://openapi.twse.com.tw/v1/opendata/"
TPEX = "https://www.tpex.org.tw/openapi/v1/"

LISTED = [("上市", TWSE + "t187ap03_L"), ("上櫃", TPEX + "mopsfin_t187ap03_O"), ("興櫃", TPEX + "mopsfin_t187ap03_R")]
HOLDERS = [TWSE + "t187ap02_L", TPEX + "mopsfin_t187ap02_O"]                       # 持股逾 10% 大股東名單
INSIDERS = [TWSE + "t187ap11_L", TPEX + "mopsfin_t187ap11_O", TPEX + "mopsfin_t187ap11_R"]   # 董監事持股餘額明細

# 董監事持股表裡「本人」是法人（公司、基金會、政府機關）時，代表這個法人持有並取得董事席次
_ORG = re.compile(r"公司|基金會|銀行|合作社|協會|委員會|基金|信託|部$|局$|院$|Limited|Ltd|Inc|Corp|LLC|B\.V\.|S\.A\.|L\.P\.", re.I)
_SEATS = {"董事長本人": "董事長", "副董事長本人": "副董事長", "常務董事本人": "常務董事", "董事本人": "董事",
          "監察人本人": "監察人", "大股東本人": "大股東"}


def _get(url: str, max_age_days: float) -> list[dict]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / (url.rstrip("/").rsplit("/", 1)[-1] + ".json")
    cached = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if cached and datetime.fromisoformat(cached["fetched"]) > datetime.now() - timedelta(days=max_age_days):
        return cached["rows"]
    for attempt in range(3):   # 櫃買的大檔偶爾會傳到一半斷線
        try:
            resp = requests.get(url, timeout=90)
            resp.raise_for_status()
            rows = [{k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in r.items()} for r in resp.json()]
            break
        except (requests.RequestException, ValueError) as e:
            if attempt < 2:
                time.sleep(3)
                continue
            print(f"  {url} 抓取失敗（{e}）" + ("，沿用舊資料" if cached else ""), file=sys.stderr)
            return cached["rows"] if cached else []
    path.write_text(json.dumps({"fetched": datetime.now().isoformat(timespec="seconds"), "rows": rows},
                               ensure_ascii=False), encoding="utf-8")
    return rows


def _f(r: dict, *keys):
    for k in keys:
        if r.get(k) not in (None, ""):
            return r[k]
    return None


def _int(s) -> int | None:
    try:
        return int(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def norm_name(s: str) -> str:
    """法人全名比對用：去空白、臺→台、全形括號→半形。"""
    return re.sub(r"\s+", "", s).replace("臺", "台").replace("（", "(").replace("）", ")")


def short_name(s: str) -> str:
    """沒有上市櫃代號的法人，圖上顯示短一點的名字。"""
    return re.sub(r"(股份)?有限公司$", "", s.strip()) or s


def norm_ban(s) -> str | None:
    """統一編號：只留 8 位數字，全 0 之類的占位值當作沒有（政府採購網的 00000000 會對到一堆雜訊）。"""
    s = re.sub(r"\D", "", str(s or ""))
    return s if len(s) == 8 and set(s) != {"0"} else None


def load_listed(max_age_days: float = 30) -> dict[str, dict]:
    """代號 → {name, market, full, shares, ban}。"""
    out = {}
    for market, url in LISTED:
        for r in _get(url, max_age_days):
            code = _f(r, "公司代號", "SecuritiesCompanyCode")
            name = _f(r, "公司簡稱", "CompanyAbbreviation")
            if code and name:
                out[code] = {"name": name, "market": market, "full": _f(r, "公司名稱", "CompanyName"),
                             "shares": _int(_f(r, "已發行普通股數或TDR原股發行股數", "IssueShares")),
                             "ban": norm_ban(_f(r, "營利事業統一編號", "UnifiedBusinessNo."))}
    return out


def load_holdings(listed: dict[str, dict], max_age_days: float = 7) -> list[dict]:
    """法人持股關係：每筆 {code 被投資公司, holder 法人全名, holder_code, kinds, seat, shares, pct}。"""
    by_full = {}
    for code, c in listed.items():
        if c["full"]:
            by_full[norm_name(c["full"])] = code
            by_full[norm_name(short_name(c["full"]))] = code
    rec: dict[tuple, dict] = {}

    def add(code, holder, kind, seat=None, shares=None):
        if not code or not holder or code not in listed:
            return
        key = (code, norm_name(holder))
        h = rec.setdefault(key, {"code": code, "holder": holder, "holder_code": by_full.get(key[1]),
                                 "kinds": set(), "seat": None, "shares": None})
        h["kinds"].add(kind)
        if seat and seat != "大股東":
            h["seat"] = seat
        if shares:
            h["shares"] = max(h["shares"] or 0, shares)

    for url in INSIDERS:
        for r in _get(url, max_age_days):
            seat = _SEATS.get(r.get("職稱", ""))
            name = r.get("姓名") or ""
            if seat and _ORG.search(name):
                add(_f(r, "公司代號", "SecuritiesCompanyCode"), name,
                    "holder" if seat == "大股東" else "director", seat, _int(r.get("目前持股")))
    for url in HOLDERS:
        for r in _get(url, max_age_days):
            add(_f(r, "公司代號", "SecuritiesCompanyCode"), r.get("大股東名稱"), "holder")

    out = []
    for h in rec.values():
        if h["holder_code"] == h["code"]:
            continue   # 庫藏股之類的自己持有自己
        total = listed[h["code"]]["shares"]
        h["pct"] = round(h["shares"] / total * 100, 2) if h["shares"] and total else None
        h["kinds"] = sorted(h["kinds"])
        out.append(h)
    return out
