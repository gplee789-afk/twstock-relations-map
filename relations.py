"""公司互動心智圖：抓 MoneyDJ「公司互動」頁（供應商、客戶、競爭者、轉投資、被投資、策略聯盟），
從指定公司往外展開幾層，輸出成可互動的心智圖 HTML（reports/relations-代號-日期.html）。

  python relations.py 1101 2330          代號或公司簡稱，可以多個
  python relations.py 台泥 --depth 2     再往外抓一層（通常上百家）
  python relations.py 1101 --refresh     忽略快取，全部重抓

  python relations.py 1101 --skip-official --skip-chain --skip-llm   只用 MoneyDJ

除了 MoneyDJ，還會補五個來源：官方持股申報（official.py：持股逾 10% 大股東、法人董事，
補進被投資／轉投資）、櫃買產業價值鏈（ic_chain.py：同一環節的公司，推論關係），
llm_rel.py 用本機 LLM 從新聞標題、法說會簡報抽出的供應／合作關係（補進客戶、供應商、策略聯盟），
capex.py 從 MOPS 重大訊息的設備／廠務／廠房採購公告讀出的交易相對人（補進供應商、客戶；--skip-capex 可關掉），
capex.py 另外讀重大訊息的合約公告（承攬、得標、供貨、銷售協議；先判斷發布公司是賣方或買方，看不出來不收；--skip-contract 可關掉），
以及 pcc.py 抓的政府電子採購網決標公告（補進客戶：每個政府機關一項；--skip-pcc 可關掉）。
關係項目的第 8 欄 deals 記錄「供應了什麼」（日期、標的、金額、來源連結），面板的往來明細就是畫這個。
抓過的頁面快取在 data/relations/代號.json，預設 30 天內不重抓；官方資料在 data/openapi/，產業鏈在 data/ic_chain.json，
LLM 抽取結果在 data/llm_relations.json，政府採購決標在 data/pcc/代號.json。
頁面版面改了 relations.html 要一起改；資料格式見 export()。
"""

import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from capex import load_capex, load_contracts
from ic_chain import load_chain, memberships
from official import load_holdings, load_listed, short_name
from pcc import load_all as load_pcc

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
REPORT_DIR = ROOT / "reports"
CACHE_DIR = DATA_DIR / "relations"
LLM_FILE = DATA_DIR / "llm_relations.json"
TEMPLATE = ROOT / "relations.html"

URL = "https://concords.moneydj.com/Z/ZC/ZC0/ZC00/ZC00_{code}.djhtm"
UA = {"User-Agent": "Mozilla/5.0 (supply-chain relations; personal research)"}
CATEGORIES = ["供應商", "客戶", "競爭者", "轉投資", "被投資", "策略聯盟", "產業鏈同業"]

# Qwen 常把「評估、可能、有意」這類語氣標成確定，用字面再補一道
_HEDGE = re.compile(r"傳|可望|有望|擬|洽談|評估|可能|有意|或將|考慮|爭取|拚")
_TITLE = re.compile(r'class="t10"[^>]*>\s*(.*?)\((\w+)\)公司互動', re.I)
_HEAD = re.compile(r'class="t2"\s*>\s*([^<]+?)\s*</td>', re.I)
_CELL = re.compile(r'<td bgcolor="white" valign="top" class="t3t1">(.*?)</td>', re.I | re.S)
_BULLET = re.compile(r'<img[^>]*point_s_yello[^>]*>', re.I)
_GENLINK = re.compile(r"GenLink2stk\('[A-Za-z]*([0-9][0-9A-Za-z]*)','([^']+)'\)")
_ANCHOR = re.compile(r'<a [^>]*href="[^"]*\?a=([^"&]+)"[^>]*>(.*?)</a>', re.I | re.S)
_CODE_PREFIX = re.compile(r"^(\d{4,6}[A-Z]?)\s*(\S.*)$")


# ---------- MoneyDJ 公司互動頁 ----------
def _text(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s).replace("&nbsp;", " ")).strip()


def _item(piece: str) -> list | None:
    """一個項目 → [名稱, 台股代號或 None, 外國代號或 None]。"""
    piece = re.sub(r"<script[^>]*>|</script>|<!--|//-->", "", piece, flags=re.I)
    if m := _GENLINK.search(piece):
        return [m.group(2).strip(), m.group(1), None]
    if m := _ANCHOR.search(piece):
        return [_text(m.group(2)), None, m.group(1)]
    name = _text(piece)
    if not name:
        return None
    if m := _CODE_PREFIX.match(name):   # 例如「8329台視」：沒有連結的台股（多半是興櫃）
        return [m.group(2), m.group(1), None]
    return [name, None, None]


def parse(html: str) -> dict:
    title = _TITLE.search(html)
    rel = {}
    for head, cell in zip(_HEAD.findall(html), _CELL.findall(html)):
        rel[head] = [it for p in _BULLET.split(cell)[1:] if (it := _item(p))]
    return {"name": title.group(1).strip() if title else None, "rel": rel}


def fetch(code: str, max_age_days: int = 30, session: requests.Session | None = None) -> tuple[dict, str]:
    """回傳 (資料, 狀態)。狀態：fetched 連網抓到、cache 快取還新、stale 連網失敗或結果可疑而沿用舊快取。
    查不到的公司 rel 是空的。"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{code}.json"
    cached = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    if cached and datetime.fromisoformat(cached["fetched"]) > datetime.now() - timedelta(days=max_age_days):
        return cached, "cache"
    try:
        resp = (session or requests).get(URL.format(code=code), headers=UA, timeout=30)
        resp.raise_for_status()
    except requests.RequestException:
        if cached:
            return cached, "stale"   # 抓不到就沿用舊資料，不要讓公司從圖上消失
        raise
    data = {"code": code, "fetched": datetime.now().isoformat(timespec="seconds"),
            **parse(resp.content.decode("cp950", errors="replace"))}
    if cached and any(cached["rel"].values()) and not any(data["rel"].values()):
        # 舊的有資料、新的整頁空白：多半是被擋或改版，不要用空白蓋掉好資料
        return cached, "stale"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return data, "fetched"


def crawl(roots: list[str], depth: int = 1, max_age_days: int = 30, limit: int = 300,
          delay: float = 1.0, log=print) -> dict[str, dict]:
    """從 roots 往外抓 depth 層台股（depth=1：根公司加上它直接相關的台股）。"""
    session = requests.Session()
    seen: dict[str, dict] = {}
    frontier = list(dict.fromkeys(roots))
    for level in range(depth + 1):
        nxt = []
        for code in frontier:
            if code in seen or len(seen) >= limit:
                continue
            try:
                data, status = fetch(code, max_age_days, session)
            except requests.RequestException as e:
                log(f"  {code} 抓取失敗：{e}")
                continue
            seen[code] = data
            n = sum(len(v) for v in data["rel"].values())
            note = {"fetched": "", "cache": "（快取）", "stale": f"（抓取失敗，沿用 {data['fetched'][:10]} 的舊資料）"}[status]
            log(f"  [{level}] {code} {data.get('name') or '?'}：{n} 筆{note}")
            if status != "cache":   # 有連網就要間隔，失敗也一樣
                time.sleep(delay)
            nxt += [it[1] for items in data["rel"].values() for it in items if it[1]]
        frontier = nxt
    if len(seen) >= limit:
        log(f"  已達上限 {limit} 家，停止往外抓")
    return seen


# ---------- 輸出 ----------
SRC_ORDER = ["dj", "holder", "director", "llm", "capex", "contract", "pcc", "chain"]


def _holding_fields(h: dict) -> tuple[str, str, list[str]]:
    """一筆持股申報 → (標籤, 說明, 來源)。標籤直接畫在圖上的名字旁邊。"""
    pct, seat, shares = h["pct"], h["seat"], h["shares"]
    holder = "holder" in h["kinds"]
    tag = "大股東" if holder else (seat or "法人董事")
    if pct is not None and pct >= 0.01:
        tag += f" {pct:g}%"
    parts = [h["holder"]]
    if holder:
        parts.append("持股逾 10% 大股東")
    if seat and seat != "大股東":
        parts.append(f"{seat}席次")
    if shares:
        parts.append(f"持股 {shares / 1000:,.0f} 張" + (f"（約 {pct:g}%）" if pct else ""))
    return tag, " · ".join(parts), [k for k in SRC_ORDER if k in h["kinds"]]


def _add_holding(items: list, name: str, tw: str | None, h: dict, alt_names: set[str]):
    """MoneyDJ 已經列了同一家就併進去（補上來源、標籤、說明），不重複畫。"""
    tag, desc, src = _holding_fields(h)
    for it in items:
        if (tw and it[1] == tw) or (not tw and not it[1] and it[0] in alt_names):
            it[3] = [k for k in SRC_ORDER if k in {*it[3], *src}]
            it[4], it[5] = tag, desc
            return None
    return [name, tw, None, src, tag, desc]


def _llm_items(llm: list[dict]) -> dict[str, dict[str, list]]:
    """llm_rel.py 抽出的關係 → {公司代號: {類別: [項目]}}。同一對公司的多筆證據合成一個項目。"""
    groups: dict[tuple, list[dict]] = {}
    for r in llm:
        sc, cc = r["supplier_code"], r["customer_code"]
        if r["kind"] == "供應":
            ends = [(sc, "客戶", r["customer"], cc), (cc, "供應商", r["supplier"], sc)]
        else:   # 合作沒有方向，兩邊都放進策略聯盟
            ends = [(sc, "策略聯盟", r["customer"], cc), (cc, "策略聯盟", r["supplier"], sc)]
        for owner, cat, other, other_code in ends:
            if owner:   # 對不到台股代號的那一邊不會有自己的頁面，只當別人的項目
                groups.setdefault((owner, cat, other_code or "n:" + other), []).append({**r, "_other": other, "_oc": other_code})
    out: dict[str, dict[str, list]] = {}
    for (owner, cat, _), rs in groups.items():
        rs.sort(key=lambda r: r["published"], reverse=True)   # 新的在前；sort 穩定，同日維持原順序
        # 證據全是「傳」才標傳（優先）；否則有寫出產品就標最新的產品（圖上標籤短，最多 10 字），再不然標最新那筆的來源類型
        product = next((r["product"] for r in rs if r.get("product")), "")
        if all(r["hedged"] or _HEDGE.search(r["evidence"]) for r in rs):
            tag = "傳"
        else:
            tag = (product if len(product) <= 10 else product[:9] + "…") if product else rs[0]["doc_type"]
        shown = rs[:3]
        desc = "\n".join(f"「{r['evidence']}」— {r['title']}（{r['published']}）" for r in shown)
        links = list(dict.fromkeys((f"{r['doc_type']} {r['published']}", r["url"]) for r in shown if r["url"]))
        # 有 product 就當標的、證據退到 note（面板畫成小字第二行）；沒有就維持原樣用證據當標的
        deals = [{"date": r["published"], "item": r.get("product") or r["evidence"], "note": r["evidence"] if r.get("product") else "",
                  "amount": None, "kind": "llm", "url": r["url"], "title": r["title"]} for r in rs]   # rs 已是新的在前
        out.setdefault(owner, {}).setdefault(cat, []).append(
            [rs[0]["_other"], rs[0]["_oc"], None, ["llm"], tag, desc, [list(x) for x in links], deals])
    return out


def _merge_rel(items: list, new: list):
    """MoneyDJ／官方已經列了同一家就併進去（聯集來源、附加說明與連結、往來明細），不重複畫。"""
    name, tw = new[0], new[1]
    for it in items:
        if (tw and it[1] == tw) or (not tw and not it[1] and it[0] == name):
            it[3] = [k for k in SRC_ORDER if k in {*it[3], *new[3]}]
            it[4] = it[4] or new[4]   # 官方持股的標籤（大股東 x%）優先
            it[5] = f"{it[5]}\n{new[5]}" if it[5] else new[5]
            deals = (it[7] if len(it) > 7 else []) + (new[7] if len(new) > 7 else [])
            it[6:] = [(it[6] if len(it) > 6 else []) + (new[6] if len(new) > 6 else [])]
            if deals:
                it[6:] = [it[6], sorted(deals, key=lambda d: d["date"] or "", reverse=True)]   # sort 穩定
            return
    items.append(new)


def money(v: float | None) -> str:
    """新台幣金額 → 3.2億 / 4,500萬；未知回傳空字串。"""
    if not v:
        return ""
    if v >= 1e8:
        return f"{v / 1e8:,.0f}億" if v >= 1e10 else f"{v / 1e8:.1f}".rstrip("0").rstrip(".") + "億"
    if v >= 1e4:
        return f"{round(v / 1e4):,}萬"
    return f"{round(v):,}元"


def _deal_tag(rs: list[dict]) -> str:
    """同一對公司的多筆公告 → 圖上標籤：標的（一筆）或「n 筆」，加合計金額。"""
    total = money(sum(r["amount_twd"] or 0 for r in rs))
    # 「供營業用之機器設備」前綴對圖上標籤沒有資訊量，拿掉才放得下標的
    label = re.sub(r"^供?營業用之?", "", rs[0]["item"]) or rs[0]["item"] if len(rs) == 1 else f"{len(rs)} 筆"
    room = 12 - (len(total) + 1 if total else 0)   # 標籤會畫在圖上，總長不超過 12 字
    if len(label) > room:
        label = label[:room - 1] + "…"
    return f"{label} {total}" if total else label


def _capex_items(capex: list[dict], listed: dict[str, dict]) -> dict[str, dict[str, list]]:
    """capex.py 的採購公告 → {公司代號: {類別: [項目]}}：發布公司的供應商、交易相對人（有代號）的客戶（role=seller 時相反）。
    同一對公司的多筆公告合成一個項目，標籤是標的（一筆）或「n 筆」加合計金額。"""
    groups: dict[tuple, list[dict]] = {}
    for r in capex:
        groups.setdefault((r["issuer"], r["key"], r.get("role", "buyer")), []).append(r)
    out: dict[str, dict[str, list]] = {}
    for (issuer, _, role), rs in groups.items():
        rs.sort(key=lambda r: r["date"], reverse=True)
        deals = [{"date": r["date"], "item": r["item"], "amount": r["amount_twd"], "kind": "capex",
                  "url": r["url"], "title": r["title"]} for r in rs]
        tag = _deal_tag(rs)
        sup = rs[0]["name"]
        # seller：發布公司是得標／承攬的一方，相對人才是買方，類別整個反過來
        cp_cat, issuer_cat = ("客戶", "供應商") if role == "seller" else ("供應商", "客戶")
        out.setdefault(issuer, {}).setdefault(cp_cat, []).append([sup, rs[0]["code"], None, ["capex"], tag, "", [], deals])
        if rs[0]["code"] and issuer in listed:
            out.setdefault(rs[0]["code"], {}).setdefault(issuer_cat, []).append(
                [listed[issuer]["name"], issuer, None, ["capex"], tag, "", [], deals])
    return out


def _contract_items(contracts: list[dict], listed: dict[str, dict]) -> dict[str, dict[str, list]]:
    """capex.py 的合約公告 → {公司代號: {類別: [項目]}}。role=buyer（發布公司向相對人買）同 capex：發布公司的供應商、
    相對人（有代號）的客戶；role=supplier（發布公司賣給相對人）反過來：發布公司的客戶、相對人（有代號）的供應商。
    政府機關、沒有代號的相對人只當發布公司的項目。"""
    groups: dict[tuple, list[dict]] = {}
    for r in contracts:
        groups.setdefault((r["issuer"], r["role"], r["key"]), []).append(r)
    out: dict[str, dict[str, list]] = {}
    for (issuer, role, _), rs in groups.items():
        rs.sort(key=lambda r: r["date"], reverse=True)
        deals = [{"date": r["date"], "item": r["item"], "amount": r["amount_twd"], "kind": "contract",
                  "url": r["url"], "title": r["title"]} for r in rs]
        tag = _deal_tag(rs)
        cp_cat, issuer_cat = ("供應商", "客戶") if role == "buyer" else ("客戶", "供應商")
        out.setdefault(issuer, {}).setdefault(cp_cat, []).append([rs[0]["name"], rs[0]["code"], None, ["contract"], tag, "", [], deals])
        if rs[0]["code"] and issuer in listed:
            out.setdefault(rs[0]["code"], {}).setdefault(issuer_cat, []).append(
                [listed[issuer]["name"], issuer, None, ["contract"], tag, "", [], deals])
    return out


PCC_YEARS, PCC_TOP = 5, 20


def _pcc_items(pcc: dict[str, dict], today: date) -> dict[str, dict[str, list]]:
    """pcc.py 的決標紀錄 → {公司代號: {"客戶": [項目]}}：每個政府機關一項，近 5 年有得標的才算，
    只留 5 年合計金額前 20 名的機關（金額相同時近的在前），圖上才不會被一堆小機關淹沒。"""
    since = (today - timedelta(days=round(365.25 * PCC_YEARS))).isoformat()
    out: dict[str, dict[str, list]] = {}
    for code, d in pcc.items():
        by_unit: dict[str, list[dict]] = {}
        for a in d.get("awards", []):
            if a["date"] >= since and a.get("unit_name"):
                by_unit.setdefault(a["unit_name"], []).append(a)
        ranked = []
        for unit, rs in by_unit.items():
            rs.sort(key=lambda a: a["date"], reverse=True)
            ranked.append((sum(a["amount"] or 0 for a in rs), rs[0]["date"], unit, rs))
        ranked.sort(key=lambda t: (t[0], t[1]), reverse=True)
        for total, _, unit, rs in ranked[:PCC_TOP]:
            deals = [{"date": a["date"], "item": a["item"], "amount": a["amount"], "kind": "pcc",
                      "url": a["url"], "title": a["title"]} for a in rs]
            tag = f"標案 {len(rs)} 筆" + (f" {money(total)}" if money(total) else "")
            out.setdefault(code, {}).setdefault("客戶", []).append([unit, None, None, ["pcc"], tag, "", [], deals])
    return out


def _by_pct(it_h):
    h = it_h[1]
    return (h["pct"] is None, -(h["pct"] or 0))


def export(roots: list[str], crawled: dict[str, dict], listed: dict[str, dict], holdings: list[dict],
           chain: dict, today: date | None = None, llm: list[dict] | None = None,
           capex: list[dict] | None = None, pcc: dict[str, dict] | None = None,
           contracts: list[dict] | None = None) -> dict:
    names_to_code = {c["name"]: code for code, c in listed.items()}
    mem = memberships(chain)
    inds = chain["industries"]
    invested = {}    # 被投資公司代號 → 它的法人股東們
    investing = {}   # 法人代號 → 它投資的公司們
    for h in holdings:
        invested.setdefault(h["code"], []).append(h)
        if h["holder_code"]:
            investing.setdefault(h["holder_code"], []).append(h)

    llm_by_co = _llm_items(llm or [])
    capex_by_co = _capex_items(capex or [], listed)
    contract_by_co = _contract_items(contracts or [], listed)
    pcc_by_co = _pcc_items(pcc or {}, today or date.today())
    companies, empty = {}, []
    for code, d in crawled.items():
        rel = {}
        for cat in CATEGORIES + [c for c in d["rel"] if c not in CATEGORIES]:
            items = [[name, tw or names_to_code.get(name), ext, ["dj"], "", ""]   # 沒附代號但名字對得上官方名單
                     for name, tw, ext in d["rel"].get(cat, [])]
            if items:
                rel[cat] = items

        # 官方持股申報：誰持有它（被投資）、它持有誰（轉投資）
        for cat, hs, other in (("被投資", invested.get(code, []), "holder_code"), ("轉投資", investing.get(code, []), "code")):
            items, new = rel.get(cat, []), []
            for h in hs:
                tw = h[other]
                if cat == "被投資":
                    name = listed[tw]["name"] if tw else short_name(h["holder"])
                    alt = {name, short_name(h["holder"])}
                else:
                    name = listed[tw]["name"]
                    alt = {name}
                if added := _add_holding(items, name, tw, h, alt):
                    new.append((added, h))
            if new:
                rel[cat] = items + [it for it, _ in sorted(new, key=_by_pct)]

        # 新聞／法說會（本機 LLM 抽取）：客戶、供應商、策略聯盟
        for cat, new_items in llm_by_co.get(code, {}).items():
            items = rel.setdefault(cat, [])
            for it in new_items:
                _merge_rel(items, it)

        # MOPS 重大訊息的設備／工程採購公告（在 LLM 之後併入，LLM 的標籤不會被蓋掉）
        for cat, new_items in capex_by_co.get(code, {}).items():
            items = rel.setdefault(cat, [])
            for it in new_items:
                _merge_rel(items, it)

        # MOPS 重大訊息的合約公告（承攬、得標、供貨、銷售協議）：方向依公告內容，賣方或買方都有
        for cat, new_items in contract_by_co.get(code, {}).items():
            items = rel.setdefault(cat, [])
            for it in new_items:
                _merge_rel(items, it)

        # 政府電子採購網決標：賣給各政府機關的標案（客戶）
        for cat, new_items in pcc_by_co.get(code, {}).items():
            items = rel.setdefault(cat, [])
            for it in new_items:
                _merge_rel(items, it)

        # 櫃買產業價值鏈：同一環節的其他公司（推論）
        peers: dict[str, list[str]] = {}
        for ic, seg in mem.get(code, []):
            s = inds[ic]["segments"][seg]
            for peer in s["codes"]:
                if peer != code:
                    peers.setdefault(peer, []).append(f"同在 {inds[ic]['name']} › {s['title']}")
        if peers:
            rel["產業鏈同業"] = [[listed[c]["name"] if c in listed else c, c, None, ["chain"], "", "；".join(parts)]
                                 for c, parts in sorted(peers.items(), key=lambda kv: (kv[0] not in crawled, kv[0]))]

        if not rel:   # 三個來源都沒有這家的資料（多半是興櫃或剛上市）
            empty.append(code)
            continue
        companies[code] = {"name": d.get("name") or listed.get(code, {}).get("name", code),
                           "fetched": d["fetched"][:10], "rel": rel}
    chain_of = {code: [list(m) for m in mem[code]] for code in companies if code in mem}
    used_ics = {ic for ms in chain_of.values() for ic, _ in ms}
    return {
        "generated": (today or date.today()).isoformat(),
        "roots": [r for r in roots if r in companies],
        "categories": CATEGORIES,
        "listed": {code: [c["name"], c["market"]] for code, c in listed.items()},
        "companies": companies,
        "chain": chain_of,
        "industries": {ic: {"name": inds[ic]["name"],
                            "segments": [[seg, s["title"], s["stream"]] for seg, s in inds[ic]["segments"].items()]}
                       for ic in inds if ic in used_ics},
        "empty": empty,
        "source": URL,
    }


def write(data: dict) -> Path:
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", payload)
    REPORT_DIR.mkdir(exist_ok=True)
    name = "all" if len(data["roots"]) > 10 else "-".join(data["roots"][:3])
    path = REPORT_DIR / f"relations-{name}-{data['generated']}.html"
    path.write_text(html, encoding="utf-8")
    return path


def main():
    p = argparse.ArgumentParser(description="公司互動心智圖（MoneyDJ 供應商／客戶／競爭者／轉投資…，可點開往外展）")
    p.add_argument("items", nargs="*", help="代號或公司簡稱，可以多個")
    p.add_argument("--all", action="store_true", help="全市場：所有上市、上櫃公司都當根公司抓（約 2000 家，第一次約 40 分鐘）")
    p.add_argument("--depth", type=int, default=1, help="往外抓幾層台股（預設 1；2 層通常要抓上百家）")
    p.add_argument("--limit", type=int, default=300, help="最多抓幾家")
    p.add_argument("--refresh", action="store_true", help="忽略 30 天快取，全部重抓")
    p.add_argument("--skip-official", action="store_true", help="不補官方持股申報（大股東、法人董事）")
    p.add_argument("--skip-chain", action="store_true", help="不補櫃買產業鏈同業")
    p.add_argument("--skip-capex", action="store_true", help="不補重大訊息的設備／工程採購公告（capex.py）")
    p.add_argument("--skip-contract", action="store_true", help="不補重大訊息的合約公告（承攬、得標、供貨、銷售協議；capex.py）")
    p.add_argument("--skip-pcc", action="store_true", help="不補政府採購決標（data/pcc/，由 pcc.py 抓）")
    p.add_argument("--skip-llm", action="store_true", help="不補新聞／法說會抽取的關係（data/llm_relations.json）")
    args = p.parse_args()
    if not args.items and not args.all:
        p.error("要給代號，或加 --all")

    listed = load_listed()
    by_name = {c["name"]: code for code, c in listed.items()}
    roots = [item if item in listed else by_name.get(item, item) for item in args.items]
    if args.all:
        # 興櫃在 MoneyDJ 幾乎都沒有資料，不抓；全市場本來就涵蓋所有鄰居，不必再往外
        roots += sorted(code for code, c in listed.items() if c["market"] in ("上市", "上櫃") and code not in roots)
        args.depth, args.limit = 0, max(args.limit, len(roots))
    print(f"抓取公司互動（{len(roots)} 家根公司，往外 {args.depth} 層）")
    crawled = crawl(roots, depth=args.depth, max_age_days=0 if args.refresh else 30, limit=args.limit)
    holdings = [] if args.skip_official else load_holdings(listed)
    chain = {"industries": {}} if args.skip_chain else load_chain()
    llm = []
    if not args.skip_llm and LLM_FILE.exists():
        llm = json.loads(LLM_FILE.read_text(encoding="utf-8")).get("relations", [])
    capex = [] if args.skip_capex else load_capex(listed=listed)
    contracts = [] if args.skip_contract else load_contracts(listed=listed)
    pcc = {} if args.skip_pcc else load_pcc()
    data = export(roots, crawled, listed, holdings, chain, llm=llm, capex=capex, pcc=pcc, contracts=contracts)
    if not data["roots"]:
        raise SystemExit("沒有抓到任何公司")
    print(f"心智圖：{write(data)}（{len(data['companies'])} 家公司可展開）")
    n_capex = sum("capex" in it[3] for c in data["companies"].values() for items in c["rel"].values() for it in items)
    print(f"重大訊息採購：{len(capex)} 筆公告明細（{sum(1 for r in capex if r['code'])} 筆對到台股代號），"
          f"圖上 {n_capex} 個關係項目帶 capex 來源")
    n_contract = sum("contract" in it[3] for c in data["companies"].values() for items in c["rel"].values() for it in items)
    print(f"重大訊息合約：{len(contracts)} 筆公告明細（{sum(1 for r in contracts if r['code'])} 筆對到台股代號；"
          f"賣方 {sum(r['role'] == 'supplier' for r in contracts)}、買方 {sum(r['role'] == 'buyer' for r in contracts)}），"
          f"圖上 {n_contract} 個關係項目帶 contract 來源")
    n_pcc = sum("pcc" in it[3] for c in data["companies"].values() for items in c["rel"].values() for it in items)
    print(f"政府採購決標：{len(pcc)} 家公司有快取，圖上 {n_pcc} 個關係項目帶 pcc 來源")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")   # Windows 主控台預設 cp950，印中文會亂碼
    main()
