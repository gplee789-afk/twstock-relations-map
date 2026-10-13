"""MoneyDJ 公司互動 vs 其他來源（官方持股、櫃買產業鏈、新聞／法說 LLM 抽取）涵蓋率比較。

  python compare.py        → reports/coverage-日期.md，並印出「MoneyDJ 缺少什麼」摘要

只讀本機快取（data/relations/*.json、data/openapi、data/ic_chain.json、data/llm_relations.json），
不連網、不呼叫 LLM；爬蟲還在寫檔時讀到壞檔就跳過。
"""

import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from ic_chain import load_chain, memberships
from llm_rel import _key, build_name_index
from official import CACHE_DIR as OPENAPI_DIR, load_holdings, load_listed, short_name
from relations import CACHE_DIR, CATEGORIES, LLM_FILE, REPORT_DIR

CAP = 20   # 每張表最多列幾列


# ---------- 讀資料 ----------
def load_moneydj() -> dict[str, dict]:
    """代號 → 快取內容；爬蟲正在寫的檔可能半截，讀不起來就跳過。"""
    out = {}
    for f in CACHE_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(d.get("rel"), dict) and d.get("fetched"):
                out[d.get("code") or f.stem] = d
        except (OSError, ValueError):
            continue
    return out


def openapi_dates() -> str:
    ds = []
    for f in OPENAPI_DIR.glob("*.json"):
        try:
            ds.append(json.loads(f.read_text(encoding="utf-8"))["fetched"][:10])
        except (OSError, ValueError, KeyError):
            continue
    return f"{min(ds)}～{max(ds)}" if ds else "無"


# ---------- 比對工具 ----------
class Ctx:
    """把 MoneyDJ 每家每類的項目轉成「身分集合」：台股代號（含用名稱對回的）加上正規化名稱，兩邊有交集就算同一家。"""

    def __init__(self, listed, crawled):
        self.listed = listed
        self.idx = build_name_index(listed)
        self.names = {c: d.get("name") or listed.get(c, {}).get("name", c) for c, d in crawled.items()}
        self.items = {}    # code → {cat: [[name, code, ext], ...]}（只留非空類別）
        self.keys = {}     # code → {cat: 身分集合}
        for code, d in crawled.items():
            cats = {cat: its for cat, its in d["rel"].items() if its}
            self.items[code] = cats
            self.keys[code] = {cat: set().union(*(self.ident(n, tw) for n, tw, _ in its)) for cat, its in cats.items()}

    def ident(self, name: str, code: str | None = None) -> set[str]:
        ks = set()
        if code:
            ks.add(code)
        k = _key(name) if name else ""
        if k:
            ks.add("n:" + k)
            if k in self.idx:
                ks.add(self.idx[k])
        return ks

    def has(self, owner: str, cat: str, name: str, code: str | None = None) -> bool:
        return bool(self.ident(name, code) & self.keys.get(owner, {}).get(cat, set()))

    def label(self, code: str) -> str:
        return f"{code} {self.names.get(code) or self.listed.get(code, {}).get('name', '')}".strip()


def pct(a: int, b: int) -> str:
    return f"{a / b * 100:.1f}%" if b else "—"


def cell(s) -> str:
    return str(s).replace("|", "／").replace("\n", " ")


def table(headers: list[str], rows: list[list], cap: int | None = CAP) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(cell(c) for c in r) + " |" for r in rows[:cap]]
    if cap and len(rows) > cap:
        out.append("")
        out.append(f"（另有 {len(rows) - cap} 筆未列出）")
    if not rows:
        out.append("| " + " | ".join("—" for _ in headers) + " |")
    return out + [""]


# ---------- 各節 ----------
def sec_coverage(cx: Ctx, crawled: dict) -> tuple[list[str], dict]:
    listed = cx.listed
    rows, tot = [], Counter()
    empty_codes = sorted(c for c in crawled if not cx.items[c])
    for m in ("上市", "上櫃", "興櫃"):
        codes = [c for c, v in listed.items() if v["market"] == m]
        cr = [c for c in codes if c in crawled]
        ne = [c for c in cr if cx.items[c]]
        rows.append([m, len(codes), len(cr), pct(len(cr), len(codes)), len(ne), pct(len(ne), len(codes))])
        if m != "興櫃":
            tot.update(listed=len(codes), crawled=len(cr), nonempty=len(ne))
    out = ["## 1. 涵蓋率", "",
           "興櫃在 MoneyDJ 幾乎沒資料，爬蟲只抓上市、上櫃，所以興櫃已爬為 0 是預期的。", ""]
    out += table(["市場", "官方名單", "已爬", "已爬 %", "有資料", "有資料 %"], rows, None)
    out.append(f"已爬但整頁是空的（MoneyDJ 沒有任何互動資料）：{len(empty_codes)} 家。前 {min(CAP, len(empty_codes))} 家："
               + "、".join(cx.label(c) for c in empty_codes[:CAP]) if empty_codes else "已爬但整頁是空的：0 家。")
    out.append("")
    outside = [c for c in crawled if c not in listed]
    if outside:
        out += [f"另有 {len(outside)} 家已爬的代號不在官方名單裡（例如 {'、'.join(outside[:5])}），不計入上表。", ""]
    return out, {"tot": tot, "empty": len(empty_codes), "n_crawled": len(crawled)}


def sec_categories(cx: Ctx, crawled: dict) -> tuple[list[str], dict]:
    cats = [c for c in CATEGORIES if c != "產業鏈同業"]
    cats += sorted({c for d in crawled.values() for c in d["rel"]} - set(cats))
    n = len(crawled)
    rows, st = [], {}
    for cat in cats:
        sizes = [len(cx.items[c][cat]) for c in crawled if cat in cx.items[c]]
        st[cat] = (len(sizes), n)
        rows.append([cat, len(sizes), pct(len(sizes), n), f"{sum(sizes) / len(sizes):.1f}" if sizes else "—"])
    no_sc = [c for c in crawled if "供應商" not in cx.items[c] and "客戶" not in cx.items[c]]
    nonempty = [c for c in crawled if cx.items[c]]
    no_sc_ne = [c for c in nonempty if c in set(no_sc)]
    out = ["## 2. 各類別", "", f"分母是已爬的 {n} 家（含整頁空白的）。平均項目數只算該類別至少有 1 筆的公司。", ""]
    out += table(["類別", "有資料的公司數", "佔已爬 %", "平均項目數"], rows, None)
    out += [f"供應商與客戶都是空的：{len(no_sc)} 家（佔已爬 {pct(len(no_sc), n)}）；"
            f"扣掉整頁空白的，有資料但供應商、客戶都沒有：{len(no_sc_ne)} 家（佔有資料 {pct(len(no_sc_ne), len(nonempty))}）。", ""]
    return out, {"cats": st, "no_sc": len(no_sc), "no_sc_ne": len(no_sc_ne), "nonempty": len(nonempty), "n": n}


def sec_holdings(cx: Ctx, crawled: dict, holdings: list[dict]) -> tuple[list[str], dict]:
    def holder_ident(h):
        return cx.ident(h["holder"], h["holder_code"]) | cx.ident(short_name(h["holder"]), h["holder_code"])

    def kind(h):
        return "大股東 >10%" if "holder" in h["kinds"] else "法人董事"

    stat = defaultdict(lambda: Counter())   # kind → 計數
    missing = []
    for h in holdings:
        k = kind(h)
        stat[k]["all"] += 1
        a = b = None
        if h["code"] in crawled and cx.items[h["code"]]:   # 被投資公司有 MoneyDJ 資料 → 看它的「被投資」有沒有這個股東
            a = bool(holder_ident(h) & cx.keys[h["code"]].get("被投資", set()))
            stat[k]["a_n"] += 1
            stat[k]["a_hit"] += a
        hc = h["holder_code"]
        if hc and hc in crawled and cx.items[hc]:   # 股東有 MoneyDJ 資料 → 看它的「轉投資」有沒有這家被投資公司
            b = cx.has(hc, "轉投資", cx.listed[h["code"]]["name"], h["code"])
            stat[k]["b_n"] += 1
            stat[k]["b_hit"] += b
        if a is not None or b is not None:
            stat[k]["any_n"] += 1
            stat[k]["any_hit"] += bool(a or b)
            if not (a or b) or a is False or b is False:
                where = [t for t, v in (("被投資頁缺股東", a), ("轉投資頁缺被投資", b)) if v is False]
                missing.append((h, k, "；".join(where), a is False and b is False))
    out = ["## 3. 官方持股 vs MoneyDJ", "",
           "官方持股＝持股逾 10% 大股東＋法人董事（來源：證交所／櫃買 OpenAPI）。"
           "「被投資」檢查：被投資公司有 MoneyDJ 資料時，股東有沒有出現在它的「被投資」；"
           "「轉投資」檢查：股東（對得到上市櫃代號者）有 MoneyDJ 資料時，被投資公司有沒有出現在它的「轉投資」。", ""]
    rows = []
    for k in ("大股東 >10%", "法人董事"):
        s = stat[k]
        rows.append([k, s["all"], f"{s['a_hit']}/{s['a_n']}（{pct(s['a_hit'], s['a_n'])}）",
                     f"{s['b_hit']}/{s['b_n']}（{pct(s['b_hit'], s['b_n'])}）",
                     f"{s['any_hit']}/{s['any_n']}（{pct(s['any_hit'], s['any_n'])}）"])
    out += table(["類型", "官方持股筆數", "被投資頁有股東", "轉投資頁有被投資", "任一方向有（可檢查者）"], rows, None)
    missing.sort(key=lambda t: (t[0]["pct"] is None, -(t[0]["pct"] or 0), t[0]["code"]))
    out += [f"MoneyDJ 缺漏的持股共 {len(missing)} 筆（可檢查的方向中至少一個缺）；持股比例最大的前 {min(CAP, len(missing))} 筆：", ""]
    out += table(["被投資公司", "法人股東", "持股 %", "類型", "MoneyDJ 缺在哪"],
                 [[cx.label(h["code"]), h["holder"] + (f"（{h['holder_code']}）" if h["holder_code"] else ""),
                   h["pct"] if h["pct"] is not None else "—", k, w] for h, k, w, _ in missing])
    return out, {"stat": stat, "n_missing": len(missing)}


def sec_llm(cx: Ctx, crawled: dict) -> tuple[list[str], dict]:
    out = ["## 4. 新聞／法說（Qwen）vs MoneyDJ", ""]
    if not LLM_FILE.exists():
        return out + ["data/llm_relations.json 還不存在（llm_rel.py 還沒產出），這節略過。", ""], {"exists": False}
    try:
        raw = json.loads(LLM_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out + ["data/llm_relations.json 讀不起來（可能正在寫入），這節略過。", ""], {"exists": False}
    rels = raw.get("relations", [])
    out += ["判斷方式：供應關係＝客戶出現在供應商的「客戶」，或供應商出現在客戶的「供應商」；合作＝任一方的「策略聯盟」有對方。"
            "有至少一方被爬過才算可比對；雙方都沒有 MoneyDJ 資料的標「無法比對」。同一對公司的多則證據併成一列。", ""]

    def ident(name, code):
        return code or "n:" + (_key(name) or name)

    groups: dict[tuple, list[dict]] = {}
    for r in rels:
        a, b = ident(r["supplier"], r["supplier_code"]), ident(r["customer"], r["customer_code"])
        if r["kind"] == "合作":
            a, b = sorted((a, b))
        groups.setdefault((r["kind"], a, b), []).append(r)

    status, rows = Counter(), []
    for (kind, _, _), rs in groups.items():
        r = rs[0]
        sc, cc = r["supplier_code"], r["customer_code"]
        if kind == "供應":
            hit = ((sc in crawled and cx.has(sc, "客戶", r["customer"], cc)) or
                   (cc in crawled and cx.has(cc, "供應商", r["supplier"], sc)))
        else:
            hit = ((sc in crawled and cx.has(sc, "策略聯盟", r["customer"], cc)) or
                   (cc in crawled and cx.has(cc, "策略聯盟", r["supplier"], sc)))
        checkable = (sc in crawled) or (cc in crawled)
        st = "已有" if hit else ("新發現" if checkable else "無法比對")
        status[st] += 1
        if not hit:
            rs.sort(key=lambda x: x["published"], reverse=True)
            top = rs[0]
            ev = top["evidence"] if len(top["evidence"]) <= 60 else top["evidence"][:60] + "…"
            rows.append([st, top["supplier"], top["customer"], kind, "傳" if all(x["hedged"] for x in rs) else "",
                         f"「{ev}」" + (f"（共 {len(rs)} 則）" if len(rs) > 1 else ""),
                         f"{top['title']}（{top['published']}）"])
    order = {"新發現": 0, "無法比對": 1}
    rows.sort(key=lambda x: order[x[0]])
    npair = len(groups)
    out += [f"LLM 關係 {len(rels)} 則證據，併成 {npair} 對公司：MoneyDJ 已有 {status['已有']} 對（{pct(status['已有'], npair)}）、"
            f"新發現 {status['新發現']} 對、無法比對 {status['無法比對']} 對。", ""]
    out += ["未在 MoneyDJ 的關係（全部列出，新發現在前）：", ""]
    out += table(["狀態", "供應商", "客戶", "類別", "傳?", "證據", "來源（標題、日期）"], rows, None)
    return out, {"exists": True, "n_rel": len(rels), "n_pair": npair, "status": status, "raw": raw}


def sec_chain(cx: Ctx, crawled: dict, chain: dict) -> tuple[list[str], dict]:
    mem = memberships(chain)
    inds = chain["industries"]
    peers_of = {}
    for code in crawled:
        ps = {}
        for ic, seg in mem.get(code, []):
            for p in inds[ic]["segments"][seg]["codes"]:
                if p != code:
                    ps.setdefault(p, set()).add(inds[ic]["name"])
        if ps:
            peers_of[code] = ps
    pairs = hits = pairs_c = hits_c = 0
    rev_n = rev_hit = 0
    empty_comp = []
    for code, ps in peers_of.items():
        comp = cx.keys[code].get("競爭者", set())
        h = sum(p in comp for p in ps)
        pairs += len(ps)
        hits += h
        if not comp:
            empty_comp.append((code, len(ps)))
        else:
            pairs_c += len(ps)
            hits_c += h
        for nm, tw, _ext in cx.items[code].get("競爭者", []):   # 反過來：MoneyDJ 的台股競爭者有多少在同一環節
            c = tw or cx.idx.get(_key(nm))
            if c:
                rev_n += 1
                rev_hit += c in ps
    empty_comp.sort(key=lambda t: -t[1])
    out = ["## 5. 產業鏈同業 vs MoneyDJ 競爭者", "",
           "同業＝櫃買產業價值鏈裡跟該公司同一環節的其他公司（只算已爬的公司）。", "",
           f"- 有同業的已爬公司：{len(peers_of)} 家，公司對同業共 {pairs} 組。",
           f"- 同業出現在該公司 MoneyDJ 競爭者的比例：{hits}/{pairs}（{pct(hits, pairs)}）；"
           f"只看競爭者非空的公司：{hits_c}/{pairs_c}（{pct(hits_c, pairs_c)}）。",
           f"- 反方向：MoneyDJ 列的台股競爭者中，有 {rev_hit}/{rev_n}（{pct(rev_hit, rev_n)}）和該公司在同一產業鏈環節。",
           f"- MoneyDJ 競爭者是空的、但有產業鏈同業的公司：{len(empty_comp)} 家。同業最多的前 {min(CAP, len(empty_comp))} 家：", ""]
    out += table(["公司", "產業鏈同業數"], [[cx.label(c), n] for c, n in empty_comp])
    return out, {"pairs": pairs, "hits": hits, "pairs_c": pairs_c, "hits_c": hits_c,
                 "empty_comp": len(empty_comp), "n_peer_cos": len(peers_of)}


def sec_asym(cx: Ctx, crawled: dict) -> tuple[list[str], dict]:
    out = ["## 6. MoneyDJ 內部不對稱", "",
           "A 的某類別列了 B（對得到台股代號），而且 B 也有 MoneyDJ 資料，但 B 的對應類別沒有 A。", ""]
    st = {}
    for cat, inv in (("客戶", "供應商"), ("供應商", "客戶"), ("轉投資", "被投資"), ("被投資", "轉投資")):
        n = miss = blank = 0
        ex = []
        for a in crawled:
            for name, tw, _ext in cx.items[a].get(cat, []):
                b = tw or cx.idx.get(_key(name))
                if not b or b == a or b not in crawled:
                    continue
                if not cx.items[b]:
                    blank += 1   # 對方頁面整頁是空的：A 有列、B 什麼都沒有，另外計
                    continue
                n += 1
                if not cx.has(b, inv, cx.names.get(a) or a, a):
                    miss += 1
                    ex.append(f"{cx.label(a)} 的{cat}有 {cx.label(b)}，但後者的{inv}沒有前者")
        st[cat] = (miss, n)
        out += [f"### {cat} → 對方的{inv}", "",
                f"可檢查 {n} 組，對方缺漏 {miss} 組（{pct(miss, n)}）。另有 {blank} 組的對方已爬但整頁空白（未計入）。"
                + ("例子：" if ex else ""), ""]
        out += [f"- {e}" for e in ex[:10]] + [""]
    return out, st


# ---------- 組合 ----------
def main():
    t0 = datetime.now()
    # 比較工具只讀快取：max_age 拉長，快取過期也不連網重抓
    listed = load_listed(36500)
    holdings = load_holdings(listed, 36500)
    chain = load_chain(36500)
    crawled = load_moneydj()
    cx = Ctx(listed, crawled)

    s1, c1 = sec_coverage(cx, crawled)
    s2, c2 = sec_categories(cx, crawled)
    s3, c3 = sec_holdings(cx, crawled, holdings)
    s4, c4 = sec_llm(cx, crawled)
    s5, c5 = sec_chain(cx, crawled, chain)
    s6, c6 = sec_asym(cx, crawled)

    # 摘要
    fetched = sorted(d["fetched"] for d in crawled.values())
    t = c1["tot"]
    cats = c2["cats"]
    bullets = [
        f"涵蓋率：上市櫃 {t['listed']} 家中只爬了 {t['crawled']} 家（{pct(t['crawled'], t['listed'])}），其中 {c1['empty']} 家 MoneyDJ 頁面是空的；"
        f"真正有資料的 {t['nonempty']} 家（{pct(t['nonempty'], t['listed'])}）。",
        f"類別缺口：已爬公司裡競爭者有資料 {pct(*cats['競爭者'])}、客戶 {pct(*cats['客戶'])}、供應商 {pct(*cats['供應商'])}、"
        f"轉投資 {pct(*cats['轉投資'])}、被投資 {pct(*cats['被投資'])}、策略聯盟 {pct(*cats['策略聯盟'])}；"
        f"供應商與客戶都沒有的有 {c2['no_sc']} 家（{pct(c2['no_sc'], c2['n'])}）。",
    ]
    for k in ("大股東 >10%", "法人董事"):
        s = c3["stat"][k]
        bullets.append(f"{k} 持股（官方 {s['all']} 筆）：被投資公司的「被投資」有收到的 {pct(s['a_hit'], s['a_n'])}（{s['a_hit']}/{s['a_n']}），"
                       f"股東的「轉投資」有收到的 {pct(s['b_hit'], s['b_n'])}（{s['b_hit']}/{s['b_n']}）。")
    if c4["exists"]:
        s = c4["status"]
        bullets.append(f"新聞／法說關係 {c4['n_pair']} 對公司：MoneyDJ 已有 {s['已有']} 對，新發現 {s['新發現']} 對，無法比對（雙方都沒爬到）{s['無法比對']} 對。")
    else:
        bullets.append("新聞／法說（Qwen）關係檔 data/llm_relations.json 尚未產出，無法比對。")
    bullets.append(f"產業鏈同業只有 {pct(c5['hits'], c5['pairs'])}（{c5['hits']}/{c5['pairs']}）出現在 MoneyDJ 競爭者；"
                   f"{c5['empty_comp']} 家有同業但 MoneyDJ 競爭者是空的。")
    a, b, c, d = (c6[k] for k in ("客戶", "供應商", "轉投資", "被投資"))
    bullets.append(f"MoneyDJ 自己不對稱：客戶→對方供應商缺 {pct(*a)}（{a[0]}/{a[1]}）、供應商→對方客戶缺 {pct(*b)}（{b[0]}/{b[1]}）、"
                   f"轉投資→對方被投資缺 {pct(*c)}（{c[0]}/{c[1]}）、被投資→對方轉投資缺 {pct(*d)}（{d[0]}/{d[1]}）。")

    llm_note = "無（檔案不存在）"
    if c4["exists"]:
        raw = c4["raw"]
        llm_note = f"{c4['n_rel']} 則關係、{len(raw.get('done', {}))} 份文件已處理，檔案更新於 {datetime.fromtimestamp(LLM_FILE.stat().st_mtime):%Y-%m-%d %H:%M}"
    note = (f"資料日期：MoneyDJ 快取 {len(crawled)} 家，抓取時間 {fetched[0][:10] if fetched else '—'}～{fetched[-1][:10] if fetched else '—'}"
            f"（爬蟲可能仍在進行，數字會隨之變動）；官方 OpenAPI 快取 {openapi_dates()}；"
            f"櫃買產業鏈 {(chain.get('fetched') or '—')[:10]}；LLM 抽取：{llm_note}。")

    today = date.today().isoformat()
    doc = [f"# MoneyDJ 公司互動涵蓋率比較（{today}）", "", f"> {note}", "", "## 0. 摘要：MoneyDJ 缺少什麼", ""]
    doc += [f"- {x}" for x in bullets] + [""]
    for s in (s1, s2, s3, s4, s5, s6):
        doc += s
    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / f"coverage-{today}.md"
    path.write_text("\n".join(doc), encoding="utf-8")
    print(f"報告：{path}（{(datetime.now() - t0).total_seconds():.1f} 秒）")
    print("摘要：MoneyDJ 缺少什麼")
    for x in bullets:
        print(f"- {x}")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
