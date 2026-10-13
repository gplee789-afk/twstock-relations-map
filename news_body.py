"""把 llm_rel.py 只有標題的新聞抓成全文，存到 data/news_bodies/<doc_id>.json，供 llm_rel.py --only bodies 使用。

  python news_body.py --limit 15      先小量試跑：最多處理 15 篇
  python news_body.py                 全部（可中斷，下次接著跑；已 ok／no_text／paywall 的不會重抓）
  python news_body.py --retry         連同 decode_failed／fetch_failed 一起重試

流程：Google News 的 rss/articles 連結 → googlenewsdecoder 解出原始出版社網址 → requests 下載 → trafilatura 抽正文。
下載到的 HTML 一律當不可信資料：只丟給 trafilatura 解析成文字，不執行、不存檔。
radar.db 以唯讀開啟，不會寫入。
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import requests
import trafilatura
from googlenewsdecoder import gnewsdecoder

from llm_rel import DEFAULT_DB, KW_RE

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "data" / "news_bodies"
GNEWS = "https://news.google.com/rss/articles/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5",
           "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5"}
MIN_INTERVAL, TIMEOUT, RETRIES = 1.5, 20, 2
MAX_HTML, MAX_TEXT, MIN_TEXT = 3_000_000, 20_000, 100
PAYWALL_RE = re.compile(r"付費會員|訂閱(?:後|即可)?(?:閱讀|觀看|解鎖)|登入後(?:閱讀|觀看)|會員專屬|subscribe to (?:read|continue)|paywall", re.I)
FINAL = {"ok", "no_text", "paywall"}   # 這些狀態不會自動重抓

_last: dict[str, float] = {}


def _throttle(host: str):
    """同一個網站兩次請求之間至少隔 MIN_INTERVAL 秒。"""
    wait = _last.get(host, 0) + MIN_INTERVAL - time.time()
    if wait > 0:
        time.sleep(wait)
    _last[host] = time.time()


def load_news_docs(con) -> list[dict]:
    """llm_rel.load_titles 選的新聞，再排除重大訊息（MOPS 公告沒有全文可抓）與法說會簡報。"""
    rows = con.execute(
        "SELECT id, title, url, published FROM documents WHERE (body IS NULL OR length(body) <= 200) "
        "AND status != 'duplicate' AND source NOT LIKE '重大訊息%' "
        "AND id NOT IN (SELECT doc_id FROM presentations) ORDER BY published DESC").fetchall()
    return [dict(id=r[0], title=r[1], url=r[2]) for r in rows if KW_RE.search(r[1] or "") and r[2]]


def decode_url(gnews_url: str) -> str | None:
    for attempt in range(RETRIES + 1):
        _throttle("news.google.com")
        try:
            res = gnewsdecoder(gnews_url, interval=None)
            if res.get("success") and str(res.get("decoded_url", "")).startswith(("http://", "https://")):
                return res["decoded_url"]
        except Exception:   # 套件內部各種網路／解析錯誤都當解不出來
            pass
        time.sleep(2 * (attempt + 1))
    return None


def download(url: str) -> tuple[bytes | None, int | None]:
    """回傳 (HTML bytes, HTTP 狀態)。連線失敗回 (None, None)。"""
    status = None
    for attempt in range(RETRIES + 1):
        _throttle(urlparse(url).netloc)
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT, stream=True)
            status = r.status_code
            if status == 429 or status >= 500:
                r.close()
                time.sleep(2 * (attempt + 1))
                continue
            if status >= 400:
                r.close()
                return None, status   # 4xx 重試也一樣，不浪費請求
            body = r.raw.read(MAX_HTML, decode_content=True)
            r.close()
            return body, status
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
    return None, status


def process(doc: dict) -> dict:
    rec = {"doc_id": doc["id"], "gnews_url": doc["url"] if doc["url"].startswith(GNEWS) else "", "url": "",
           "status": "", "text": "", "fetched": datetime.now().isoformat(timespec="seconds")}
    if rec["gnews_url"]:
        real = decode_url(doc["url"])
        if not real:
            rec["status"] = "decode_failed"
            return rec
    else:
        real = doc["url"]
    rec["url"] = real
    html, http = download(real)
    if html is None:
        rec["status"] = "paywall" if http in (401, 402) else "fetch_failed"
        return rec
    try:
        text = trafilatura.extract(html, include_comments=False, include_tables=False, favor_precision=True) or ""
    except Exception:
        text = ""
    text = text.strip()
    if len(text) >= MIN_TEXT:
        rec["status"], rec["text"] = "ok", text[:MAX_TEXT]
    elif PAYWALL_RE.search(html.decode("utf-8", errors="ignore")):
        rec["status"] = "paywall"
    else:
        rec["status"] = "no_text"
    return rec


def main():
    p = argparse.ArgumentParser(description="抓新聞全文（Google News 連結 → 出版社網址 → 正文）")
    p.add_argument("--radar-db", default=str(DEFAULT_DB))
    p.add_argument("--limit", type=int, default=None, help="這次最多處理幾篇")
    p.add_argument("--retry", action="store_true", help="重試 decode_failed／fetch_failed")
    args = p.parse_args()

    con = sqlite3.connect(f"file:{Path(args.radar_db).as_posix()}?mode=ro", uri=True)
    docs = load_news_docs(con)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    todo = []
    for d in docs:
        f = OUT_DIR / f"{d['id']}.json"
        if f.exists():
            try:
                old = json.loads(f.read_text(encoding="utf-8"))["status"]
            except (ValueError, KeyError):
                old = ""
            if old in FINAL or (old and not args.retry):
                continue
        todo.append(d)
    print(f"新聞 {len(docs)} 篇，已有結果 {len(docs) - len(todo)}，待處理 {len(todo)}"
          + (f"，上限 {args.limit}" if args.limit else ""))
    if args.limit is not None:
        todo = todo[:args.limit]

    stat, domains = Counter(), Counter()
    try:
        for n, d in enumerate(todo, 1):
            rec = process(d)
            tmp = OUT_DIR / f"{d['id']}.tmp"
            tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(OUT_DIR / f"{d['id']}.json")
            stat[rec["status"]] += 1
            if rec["status"] == "ok":
                domains[urlparse(rec["url"]).netloc] += 1
            if n % 20 == 0:
                print(f"  {n}/{len(todo)}  {dict(stat)}")
    except KeyboardInterrupt:
        print("\n中斷，已存檔；下次執行會接著跑")
    print(f"\n本次處理 {sum(stat.values())} 篇：{dict(stat)}")
    print("ok 的網域：" + "、".join(f"{k} {v}" for k, v in domains.most_common()) if domains else "沒有 ok")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
