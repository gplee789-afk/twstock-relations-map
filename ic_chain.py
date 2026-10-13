"""櫃買中心「產業價值鏈資訊平台」（ic.tpex.org.tw）：每個產業分成上中下游環節，每個環節列出所屬公司。

這裡的關係是「同一環節」，不是公司之間實際的買賣，所以圖上標成推論（虛線）。
快取在 data/ic_chain.json，預設 30 天內不重抓。
"""

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

BASE = "https://ic.tpex.org.tw/"
UA = {"User-Agent": "Mozilla/5.0 (supply-chain relations; personal research)"}
CACHE = Path(__file__).resolve().parent / "data" / "ic_chain.json"

_IC_LINK = re.compile(r'href="introduce\.php\?ic=(\w+)"')
_TITLE = re.compile(r"<title>[^<]*?>\s*(.*?)產業鏈簡介\s*</title>", re.S)
_STREAM = re.compile(r'chain-title-panel">\s*(上游|中游|下游)\s*<')
_SEG = re.compile(r'id="ic_link_(\w+)"')
_LIST = re.compile(r'<div id="companyList_(\w+)" title="([^"]*)"')
_CO = re.compile(r'company_basic\.php\?stk_code=(\w+)"[^>]*title="([^"]*)"')


def parse_industry(html: str) -> dict:
    title = _TITLE.search(html)
    # 環節屬於哪一段：看它前面最近的「上游／中游／下游」標題
    marks = sorted([(m.start(), "stream", m.group(1)) for m in _STREAM.finditer(html)]
                   + [(m.start(), "seg", m.group(1)) for m in _SEG.finditer(html)])
    stream_of, cur = {}, None
    for _, kind, val in marks:
        if kind == "stream":
            cur = val
        else:
            stream_of.setdefault(val, cur)
    segments = {}
    blocks = list(_LIST.finditer(html))
    for i, m in enumerate(blocks):
        seg = m.group(1)
        if seg in segments:
            continue   # 頁面上同一份清單會出現兩次
        body = html[m.end(): blocks[i + 1].start() if i + 1 < len(blocks) else len(html)]
        codes = list(dict.fromkeys(c for c, _ in _CO.findall(body)))
        segments[seg] = {"title": m.group(2), "stream": stream_of.get(seg), "codes": codes}
    return {"name": title.group(1).strip() if title else None, "segments": segments}


def load_chain(max_age_days: float = 30, delay: float = 1.0, log=print) -> dict:
    if CACHE.exists():
        cached = json.loads(CACHE.read_text(encoding="utf-8"))
        if datetime.fromisoformat(cached["fetched"]) > datetime.now() - timedelta(days=max_age_days):
            return cached
    else:
        cached = None
    session = requests.Session()
    try:
        index = session.get(BASE, headers=UA, timeout=30)
        index.raise_for_status()
        ics = list(dict.fromkeys(_IC_LINK.findall(index.content.decode("utf-8", "replace"))))
        industries = {}
        log(f"  櫃買產業鏈：{len(ics)} 個產業")
        for ic in ics:
            resp = session.get(f"{BASE}introduce.php?ic={ic}", headers=UA, timeout=30)
            resp.raise_for_status()
            ind = parse_industry(resp.content.decode("utf-8", "replace"))   # 伺服器沒標 charset，requests 會猜成 latin-1
            if ind["segments"]:
                industries[ic] = ind
            time.sleep(delay)
    except requests.RequestException as e:
        log(f"  櫃買產業鏈抓取失敗（{e}）" + ("，沿用舊資料" if cached else ""))
        return cached or {"fetched": None, "industries": {}}
    data = {"fetched": datetime.now().isoformat(timespec="seconds"), "industries": industries}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def memberships(chain: dict) -> dict[str, list[tuple[str, str]]]:
    """代號 → [(產業代碼, 環節代碼)]。"""
    out: dict[str, list] = {}
    for ic, ind in chain["industries"].items():
        for seg, s in ind["segments"].items():
            for code in s["codes"]:
                out.setdefault(code, []).append((ic, seg))
    return out
