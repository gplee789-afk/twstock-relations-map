"""政府電子採購網的決標公告（g0v 鏡像 https://pcc-api.openfun.app）→ 「上市櫃公司賣給哪些政府機關什麼」。

  python pcc.py --codes 2412 3231        只抓指定公司
  python pcc.py --limit 20               全市場，最多抓 20 家（已有新快取的不算）
  python pcc.py                          全部上市櫃（用統一編號查；每個請求間隔 1 秒，很久，可以 Ctrl-C 下次接著跑）

每家公司：用統編查出所有公告，只留該公司「得標」的決標／更正決標公告（同一標案取最新一筆），
近 3 年的最多抓 50 筆明細讀金額與品項；更早的只留標題、日期、機關。
快取在 data/pcc/代號.json，30 天內不重抓；明細結果也存在檔案裡，重跑不會重抓。
鏡像資料可能落後官方網站，執行結束會印出目前最新的決標日期。
"""

import argparse
import json
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

from official import load_listed

ROOT = Path(__file__).resolve().parent
CACHE_DIR = ROOT / "data" / "pcc"
API = "https://pcc-api.openfun.app/api/"
UA = {"User-Agent": "Mozilla/5.0 (supply-chain relations; personal research)"}
KINDS = ("決標公告", "更正決標公告")
GAP = 1.0   # 鏡像沒有公布速率限制，保守一點，請求之間至少隔 1 秒

_ITEM = re.compile(r"^決標品項:第(\d+)品項:得標廠商(\d+):得標廠商$")
_NUM = re.compile(r"[\d,]+(?:\.\d+)?")


class Client:
    """所有請求共用的節流與計數。"""

    def __init__(self, gap: float = GAP):
        self.s = requests.Session()
        self.s.headers.update(UA)
        self.gap, self.last, self.requests = gap, 0.0, 0

    def get(self, path: str, **params) -> dict:
        err = None
        for attempt in range(4):
            wait = self.gap - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            self.requests += 1
            try:
                resp = self.s.get(API + path, params=params, timeout=60)
                resp.raise_for_status()
                return resp.json()
            except (requests.RequestException, ValueError) as e:
                err = e
                self.s.close()   # 鏡像偶爾會直接重置連線（WinError 10054），換一條新連線並拉長等待
                self.s = requests.Session()
                self.s.headers.update(UA)
                time.sleep(5 * 2 ** attempt)
        raise err


def iso(d: int | str) -> str:
    s = str(d)
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


def won_name(brief: dict, ban: str) -> str | None:
    """這家公司在這筆公告裡得標的話回傳它的名稱，否則 None。「未得標廠商」字串裡也含「得標廠商」，所以先排除。"""
    cp = brief.get("companies") or {}
    for i, cid in enumerate(cp.get("ids") or []):
        if cid != ban or i >= len(cp.get("names") or []):
            continue
        name = cp["names"][i]
        for path in (cp.get("name_key") or {}).get(name, []):
            if "得標廠商" in path and "未得標" not in path:
                return name
    return None


def search_awards(cl: Client, ban: str) -> tuple[list[dict], str | None]:
    """所有頁面 → (這家公司得標的決標公告，同一標案留最新一筆；所有公告裡最新的日期)。"""
    best: dict[tuple, dict] = {}
    newest_any, page, pages = None, 1, 1
    while page <= pages:
        r = cl.get("searchbycompanyid", query=ban, page=page)
        pages = r.get("total_pages") or 1
        for rec in r.get("records", []):
            if page == 1 and newest_any is None:
                newest_any = iso(rec["date"])   # 新的在前，第一筆就是最新
            brief = rec.get("brief") or {}
            if brief.get("type") not in KINDS:
                continue
            name = won_name(brief, ban)
            if not name:
                continue
            key = (rec["unit_id"], rec["job_number"])
            cur = best.get(key)
            if cur is None or (rec["date"], rec["filename"]) > (cur["_d"], cur["filename"]):
                best[key] = {
                    "date": iso(rec["date"]), "unit_id": rec["unit_id"], "unit_name": rec.get("unit_name") or "",
                    "job_number": rec["job_number"], "title": brief.get("title") or "", "item": brief.get("title") or "",
                    "amount": None,
                    # 下面三個是為了之後只補明細時不必重查：公告檔名、得標時的寫法、明細是否已抓
                    "url": f"https://web.pcc.gov.tw/prkms/tender/common/noticeDate/redirectPublic?ds={rec['date']}&fn={rec['filename']}.xml",
                    "filename": rec["filename"], "winner": name, "detail": False, "_d": rec["date"]}
        page += 1
    out = sorted(best.values(), key=lambda a: (a["date"], a["filename"]), reverse=True)
    for a in out:
        del a["_d"]
    return out, newest_any


def _same(a: str, b: str) -> bool:
    """得標廠商在明細裡的寫法可能少了英文名，只比對括號前的部分。"""
    a, b = a.strip(), b.strip()
    return a == b or re.split(r"\s*[(（]", a)[0] == re.split(r"\s*[(（]", b)[0]


def _amount(s) -> int | None:
    m = _NUM.search(str(s or ""))
    if not m:
        return None
    try:
        return round(float(m.group().replace(",", "")))
    except ValueError:
        return None


def parse_detail(detail: dict, winner: str) -> tuple[int | None, str | None]:
    """明細 → (這家公司的決標金額, 品項名稱)。找不到這家公司的品項金額就用總決標金額。"""
    amounts, names = [], []
    for k, v in detail.items():
        m = _ITEM.match(k)
        if not m or not _same(str(v), winner):
            continue
        amt = _amount(detail.get(f"決標品項:第{m[1]}品項:得標廠商{m[2]}:決標金額"))
        if amt is not None:
            amounts.append(amt)
        nm = str(detail.get(f"決標品項:第{m[1]}品項:品項名稱") or "").strip()
        if nm:
            names.append(nm)
    if not names:   # 對不到這家的品項：品項名稱改用全部品項
        names = [str(v).strip() for k, v in detail.items() if re.match(r"^決標品項:第\d+品項:品項名稱$", k) and str(v).strip()]
    amount = sum(amounts) if amounts else _amount(detail.get("決標資料:總決標金額"))
    item = "、".join(dict.fromkeys(names))
    if len(item) > 40:
        item = item[:39] + "…"
    return amount, item or None


def fetch_detail(cl: Client, a: dict):
    """讀一筆決標明細，直接更新 a。優先用同一份公告（檔名相同），沒有就用該標案最新的決標／更正決標。"""
    r = cl.get("tender", unit_id=a["unit_id"], job_number=a["job_number"])
    recs = [x for x in r.get("records", []) if (x.get("brief") or {}).get("type") in KINDS]
    rec = next((x for x in recs if x.get("filename") == a["filename"]), None) or \
        max(recs, key=lambda x: (x["date"], x["filename"]), default=None)
    if rec is None:
        raise ValueError("明細裡沒有決標公告")
    amount, item = parse_detail(rec.get("detail") or {}, a["winner"])
    a["amount"] = amount
    if item:
        a["item"] = item
    if rec.get("url", "").startswith("https://web.pcc.gov.tw"):
        a["url"] = rec["url"]
    a["detail"] = True


def load_cache(code: str) -> dict | None:
    path = CACHE_DIR / f"{code}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def save(code: str, data: dict):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{code}.json").write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def fetch_company(cl: Client, code: str, ban: str, cached: dict | None, refresh: bool, since: str,
                  max_details: int, st: dict) -> dict:
    """抓一家：快取還新就只補沒抓到的明細；否則重查全部頁面（舊的明細結果照用）。"""
    fresh = cached and not refresh and datetime.fromisoformat(cached["fetched"]) > datetime.now() - timedelta(days=30) \
        and cached.get("ban") == ban
    if fresh:
        data = cached
    else:
        awards, newest_any = search_awards(cl, ban)
        old = {(a["unit_id"], a["job_number"]): a for a in (cached or {}).get("awards", [])}
        for a in awards:   # 同一份公告已經讀過明細就不重抓
            o = old.get((a["unit_id"], a["job_number"]))
            if o and o.get("detail") and o.get("filename") == a["filename"]:
                a.update({k: o[k] for k in ("item", "amount", "url", "detail")})
        data = {"code": code, "ban": ban, "fetched": datetime.now().isoformat(timespec="seconds"),
                "latest_date": awards[0]["date"] if awards else None, "newest_any": newest_any, "complete": False,
                "awards": awards}
        save(code, data)   # 搜尋是最花時間的一段，先存起來，明細中斷了也不用重查
    # 近期的最多 max_details 筆（新的在前，已經抓過的也算在名額裡，才不會越補越多）
    todo = [a for a in data["awards"] if a["date"] >= since][:max_details]
    todo = [a for a in todo if not a.get("detail")]
    failed = 0
    for a in todo:
        try:
            fetch_detail(cl, a)
            st["details"] += 1
        except (requests.RequestException, ValueError, KeyError) as e:
            failed += 1
            st["failures"] += 1
            print(f"  {code} 明細失敗 {a['unit_id']}/{a['job_number']}：{e}", file=sys.stderr)
    data["complete"] = failed == 0
    if todo or not fresh:
        save(code, data)
    return data


def load_all() -> dict[str, dict]:
    """給 relations.py 用：代號 → 快取內容，只讀 data/pcc/，不連網。"""
    return {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(CACHE_DIR.glob("*.json"))}


def main():
    p = argparse.ArgumentParser(description="政府電子採購網決標公告（g0v 鏡像），每家上市櫃公司得標的標案")
    p.add_argument("--codes", nargs="*", help="只抓這些代號")
    p.add_argument("--limit", type=int, help="最多抓幾家（已有新快取、不必連網的不算）")
    p.add_argument("--refresh", action="store_true", help="忽略 30 天快取，全部重抓")
    p.add_argument("--since-years", type=float, default=3, help="幾年內的決標才抓明細（預設 3）")
    p.add_argument("--max-details", type=int, default=50, help="每家最多抓幾筆明細（預設 50）")
    args = p.parse_args()

    listed = load_listed()
    codes = args.codes or sorted(c for c, v in listed.items() if v["market"] in ("上市", "上櫃"))
    targets = []
    for c in codes:
        if c not in listed or not listed[c]["ban"]:
            print(f"  {c} 沒有有效統一編號，略過")
            continue
        targets.append(c)
    since = (date.today() - timedelta(days=round(365.25 * args.since_years))).isoformat()
    cl, st = Client(), {"details": 0, "failures": 0, "done": 0, "cached": 0, "with_award": 0, "awards": 0, "failed_cos": 0}
    newest_award = newest_any = ""
    t0 = time.time()
    fetched = 0
    try:
        for i, code in enumerate(targets, 1):
            if args.limit and fetched >= args.limit:
                break
            cached = load_cache(code)
            ban = listed[code]["ban"]
            ok = cached and not args.refresh and cached.get("complete") and cached.get("ban") == ban \
                and datetime.fromisoformat(cached["fetched"]) > datetime.now() - timedelta(days=30)
            before = cl.requests
            try:
                data = fetch_company(cl, code, ban, cached, args.refresh, since, args.max_details, st)
            except (requests.RequestException, ValueError, KeyError) as e:
                st["failed_cos"] += 1
                print(f"  {code} 失敗：{e}", file=sys.stderr)
                continue
            if cl.requests > before:
                fetched += 1
            elif ok:
                st["cached"] += 1
            st["done"] += 1
            st["awards"] += len(data["awards"])
            st["with_award"] += bool(data["awards"])
            newest_award = max(newest_award, data.get("latest_date") or "")
            newest_any = max(newest_any, data.get("newest_any") or "")
            if st["done"] % 20 == 0:
                print(f"  [{st['done']}/{len(targets)}] 已有決標 {st['with_award']} 家、{st['awards']} 筆；"
                      f"明細 {st['details']}；請求 {cl.requests}；{time.time() - t0:.0f}s")
    except KeyboardInterrupt:
        print("中斷，已抓的都存在 data/pcc/，下次接著跑")
    print(f"完成：{st['done']} 家（快取 {st['cached']}、失敗 {st['failed_cos']}）；至少一筆得標 {st['with_award']} 家，"
          f"共 {st['awards']} 筆；本次抓明細 {st['details']} 筆、明細失敗 {st['failures']}；請求 {cl.requests} 次；耗時 {time.time() - t0:.0f}s")
    print(f"最新得標日期 {newest_award or '—'}；鏡像裡最新公告日期 {newest_any or '—'}（今天 {date.today()}，差太多代表鏡像落後）")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
