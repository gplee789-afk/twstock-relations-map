"""用本機 LLM（LM Studio 的 Qwen3）從新聞標題、新聞全文、法說會簡報抽出「A 供應 B／A 與 B 合作」，
並盡量記下「供應什麼」（product）。存到 data/llm_relations.json，relations.py 會把它畫進心智圖（來源 llm，可信度中等）。

  python llm_rel.py --limit 6 --only titles          先小量試跑：最多呼叫 6 次，只跑新聞標題
  python llm_rel.py --only bodies                    新聞全文（要先跑 news_body.py 抓到 data/news_bodies/）
  python llm_rel.py --only presentations             法說會簡報
  python llm_rel.py                                  三種都跑（可中斷，下次接著跑）
  python llm_rel.py --revalidate                     不呼叫 LLM，用現行規則重驗已存的關係（先備份成 llm_relations.prev.json）

單位編號（done 的鍵）帶版本前綴 v2:（t 標題、b 全文片段、p 簡報片段）。第一次跑 v2 會把舊的 v1 結果
備份到 data/llm_relations.v1.json（已存在就不覆蓋），並清掉主檔裡的 v1 關係重跑，舊統計留在 stats["v1"]。

資料來源是 supply-chain-radar 的 radar.db（唯讀開啟，不會寫入）。
抽出的每一筆都要通過四道檢查才保留，否則算進 stats 的 drops：
  evidence_not_found  引用原文不在來源文字裡（模型改寫或編造）
  name_not_in_text    供應方或客戶名稱沒出現在來源文字裡
  no_tw_company       兩邊都對不到台股公司（這張圖只關心台股）
  self                兩邊是同一家
product 不通過檢查（不在來源文字裡）時只清空 product、關係照留，計入 stats["product_not_in_text"]。
"""

import argparse
import json
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import requests

from official import load_listed, norm_name, short_name

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "data" / "llm_relations.json"
DEFAULT_DB = ROOT.parent / "supply-chain-radar" / "data" / "radar.db"

KW = "供應鏈|打入|拿下|訂單|客戶|供應商|認證|出貨|獨家|攜手|合作|採用|代工|導入|搶進|獨供|大單|聯手|委外"
KW_RE = re.compile(KW)
LINE_RE = re.compile(KW + "|客戶|供應")   # 簡報逐行比對；spec 要求 KW 外多加「供應」
CHUNK_CHARS, MAX_CHUNKS, BODY_MAX_CHUNKS = 2500, 3, 2
BODY_DIR = ROOT / "data" / "news_bodies"
VER = "v2:"   # 單位編號前綴；抽取格式變了（例如加 product）就升版，整批重跑
SELF_WORDS = {"本公司", "我們", "公司"}
# 引用句要含其中一個關係詞，才算「句子本身在講供應／合作」，避免只是兩家公司剛好同時出現在標題裡
RELATION_KW = re.compile("供應|供貨|出貨|攜手|合作|聯手|採用|導入|訂單|大單|代工|認證|打入|拿下|切入|獨家|獨供|委外"
                         "|客戶|夥伴|策略聯盟|代理|經銷|簽署|協議")
# 對不到台股的那一邊若是泛稱（客戶、韓系大廠、某業者）或不是公司（學校、研究單位、政府機關、醫院），就不算一筆關係
GENERIC = re.compile(r"客戶|大廠|廠商|業者|供應商|廠$|^(韓系|美系|日系|陸系|中系|歐系|台系|國際|海外|國內|北美|一線|大型|特定|主要|某)"
                     r"|大學|學院|研究院|研究所|實驗室|政府|部$|局$|醫院|學校"
                     # 認證機構、公會，和被當成公司的技術名詞（試跑時出現過 NIST、公會、CoWoS）
                     r"|公會|協會|聯盟|^(NIST|UL|TÜV|ISO|IEC|CNS|JEDEC)$"
                     r"|^(CoWoS|SoIC|InFO|HBM\d*|CPO|AI|GPU|ASIC|PCB|CCL|ABF|SiC|GaN)$", re.I)

SYSTEM = """你是供應鏈關係抽取器。只抽取文字中「明確寫出」的兩家公司之間的關係，不要推測。
每筆關係欄位：
- supplier：提供產品、服務、代工的一方。customer：買方、委託方。名稱照原文寫，不要翻譯、不要補全。
- kind：「供應」（包含代工、訂單、認證、採用、出貨給、打入供應鏈）或「合作」（攜手、聯手、策略聯盟，看不出誰賣誰買時；supplier/customer 任填兩家）。
- hedged：原文語氣不確定（傳、可望、有望、擬、洽談、傳出）填 true，已確定填 false。
- evidence：從原文逐字複製的連續片段，不超過 60 字，不可改寫、省略或翻譯。
- product：supplier 供應給 customer 的產品或服務，原文有寫才填，照原文逐字複製，不超過 20 字（例如「GB300 水冷板」「CoWoS 矽中介層」「探針卡」）。原文沒寫就填空字串 ""，不可猜測、不可自己歸類或改寫；營收、金額、數量不是產品。
規則：
- 兩邊都必須是具體的公司名稱；「客戶」「韓系客戶」「大廠」「國際客戶」這類泛稱不算，產品名（Vera Rubin）、專案名、設施名、學校、人名也不是公司。公司可以是外國公司（Apple、輝達、Google）。
- 兩家公司的名稱都要出現在原文，且 evidence 要同時寫到兩家（簡報的「本公司」除外）。只有一家公司的營收、訂單、獲利報導，不要抽。
- 忽略股價、目標價、法人買賣與建議、處置股名單。
- 併購、投資、持股、設廠、參展、產學合作、營收獲利報導都不是供應或合作關係，不要抽。
- 沒有符合的關係就回傳空陣列。只輸出 JSON，格式 {"relations": [...]}。
範例（標題 → 輸出）：
「家登打入美光供應鏈，先進封裝載具獲大單」→ supplier 家登，customer 美光，kind 供應，hedged false，evidence「家登打入美光供應鏈」，product「先進封裝載具」
「傳台積電拿下輝達新晶片訂單」→ supplier 台積電，customer 輝達，kind 供應，hedged true，evidence「傳台積電拿下輝達新晶片訂單」，product ""（只寫「新晶片」，不具體）
「台積電攜手格羅方德強化先進封裝」→ kind 合作，evidence「台積電攜手格羅方德強化先進封裝」，product ""
「家登接獲韓系客戶大單」→ []（客戶是泛稱）；「廣達EPS上看26.5元」→ []；「智崴併 Flyover」→ []（併購）；「康舒攜手 OmniOn 參展」→ []（只是參展）。 /no_think"""


def _schema(with_item: bool) -> dict:
    props = {
        "supplier": {"type": "string"}, "customer": {"type": "string"},
        "kind": {"type": "string", "enum": ["供應", "合作"]},
        "hedged": {"type": "boolean"}, "evidence": {"type": "string"}, "product": {"type": "string"},
    }
    if with_item:
        props["item"] = {"type": "integer"}
    return {"type": "object", "required": ["relations"], "properties": {"relations": {
        "type": "array", "items": {"type": "object", "properties": props, "required": list(props)}}}}


def call_llm(base_url: str, model: str, user: str, with_item: bool) -> list[dict]:
    """對應 radar/llm.py 的 _create + structured：json_schema 輸出、/no_think 關掉 Qwen3 思考，壞掉重試一次。"""
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    err = None
    for _ in range(2):
        r = requests.post(f"{base_url.rstrip('/')}/chat/completions", timeout=300, json={
            "model": model, "messages": messages, "temperature": 0.1, "max_tokens": 2000,
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "Relations", "schema": _schema(with_item)}}})
        if not r.ok:   # 400 時 LM Studio 會在內文說原因（例如模型被卸載），一併帶出來
            raise requests.HTTPError(f"{r.status_code} {r.text[:200]}")
        text = r.json()["choices"][0]["message"]["content"] or ""
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        try:
            rels = json.loads(text)["relations"]
            if isinstance(rels, list):
                return [x for x in rels if isinstance(x, dict)]
            err = "relations 不是陣列"
        except (ValueError, KeyError, TypeError) as e:
            err = e
        messages += [{"role": "assistant", "content": text},
                     {"role": "user", "content": f"輸出格式錯誤（{str(err)[:100]}）。請只輸出 {{\"relations\": [...]}}。 /no_think"}]
    raise ValueError(f"LLM 輸出無法解析：{err}")


# ---------- 名稱 → 台股代號 ----------
def _key(s: str) -> str:
    s = norm_name(s).lower()
    s = re.sub(r"股份有限公司$|有限公司$", "", s)
    return re.sub(r"[-－]?ky\*?$|\*$", "", s)   # 簡稱常帶 -KY，新聞多半不寫


def build_name_index(listed: dict) -> dict[str, str]:
    idx, bad = {}, set()
    for code, c in listed.items():
        for n in {c["name"], c["full"] or "", short_name(c["full"] or "")}:
            k = _key(n) if n else ""
            if not k:
                continue
            if idx.setdefault(k, code) != code:
                bad.add(k)   # 兩家撞名就不猜
    return {k: v for k, v in idx.items() if k not in bad}


# ---------- 來源文件 ----------
def _ws(s: str) -> str:
    return re.sub(r"\s+", "", s)   # 簡報逐行斷開，模型引用時常把換行吃掉，所以全部空白拿掉再比


def load_titles(con) -> list[dict]:
    rows = con.execute("SELECT id, title, url, published FROM documents WHERE (body IS NULL OR length(body) <= 200) "
                       "AND status != 'duplicate' ORDER BY published DESC").fetchall()
    return [dict(id=r[0], title=r[1], url=r[2], published=(r[3] or "")[:10]) for r in rows if KW_RE.search(r[1] or "")]


def chunks_of(body: str, max_chunks: int = MAX_CHUNKS, head: str = "") -> list[str]:
    """命中關鍵字的行（±1 行）合併成視窗，再裝進 ≤2500 字的片段，預設最多 3 段。head（新聞標題）放在每段最上面。"""
    lines = body.split("\n")
    wins: list[list[int]] = []
    for i, ln in enumerate(lines):
        if LINE_RE.search(ln):
            lo, hi = max(0, i - 1), min(len(lines) - 1, i + 1)
            if wins and lo <= wins[-1][1] + 1:
                wins[-1][1] = max(wins[-1][1], hi)
            else:
                wins.append([lo, hi])
    out, cur = [], ""
    for lo, hi in wins:
        w = "\n".join(lines[lo:hi + 1])[:CHUNK_CHARS]
        if cur and len(cur) + len(w) + 1 > CHUNK_CHARS:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{w}" if cur else w
    if cur:
        out.append(cur)
    return [f"{head}\n{c}" if head else c for c in out[:max_chunks]]


def load_presentations(con) -> list[dict]:
    rows = con.execute("SELECT d.id, d.title, d.url, d.published, d.body, p.code FROM documents d "
                       "JOIN presentations p ON p.doc_id = d.id WHERE d.status != 'duplicate' "
                       "ORDER BY d.published DESC").fetchall()
    return [dict(id=r[0], title=r[1], url=r[2], published=(r[3] or "")[:10], code=r[5], chunks=chunks_of(r[4] or ""))
            for r in rows]


def load_bodies(con) -> list[dict]:
    """news_body.py 抓到全文（status ok）的新聞，切成片段；標題放在每段最上面。url 用出版社原網址。"""
    recs = {}
    for f in BODY_DIR.glob("*.json"):
        try:
            r = json.loads(f.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if r.get("status") == "ok" and r.get("text"):
            recs[r["doc_id"]] = r
    ids, out = list(recs), []
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        rows = con.execute("SELECT id, title, published FROM documents WHERE status != 'duplicate' AND id IN "
                           f"({','.join('?' * len(part))}) ORDER BY published DESC", part).fetchall()
        for id_, title, pub in rows:
            out.append(dict(id=id_, title=title, url=recs[id_]["url"], published=(pub or "")[:10], text=recs[id_]["text"],
                            chunks=chunks_of(recs[id_]["text"], BODY_MAX_CHUNKS, title)))
    out.sort(key=lambda d: d["published"], reverse=True)
    return out


# ---------- 驗證 ----------
class Validator:
    def __init__(self, listed: dict, stats: dict):
        self.listed, self.idx, self.stats = listed, build_name_index(listed), stats
        self.fulls = [(_key(c["full"]), code) for code, c in listed.items() if c["full"]]

    def map_code(self, name: str) -> str | None:
        """先精確比對簡稱／全名；對不到再試前綴：新聞常寫「世界先進」，官方簡稱是「世界」，全名是「世界先進積體電路」。"""
        k = _key(name)
        if k in self.idx:
            return self.idx[k]
        if len(k) >= 3:
            hit = {code for fk, code in self.fulls if fk.startswith(k)}
            if len(hit) == 1:   # 前綴要唯一才採用，撞到兩家以上就不猜
                return hit.pop()
        return None

    def drop(self, reason: str, rel: dict, doc: dict):
        self.stats["drops"][reason] = self.stats["drops"].get(reason, 0) + 1
        s = self.stats["samples"].setdefault(reason, [])
        if len(s) < 5:
            s.append({"supplier": rel.get("supplier"), "customer": rel.get("customer"),
                      "evidence": rel.get("evidence"), "title": doc["title"]})

    def check(self, rel: dict, source: str, doc: dict, doc_type: str, issuer: str | None = None,
              unit: str = "title") -> dict | None:
        """通過四道檢查回傳要存的關係，否則記一筆 drop 回傳 None。issuer：簡報的發行公司代號。
        unit：來源單位種類（title／body／presentation），重新驗證時要靠它找回原始文字。"""
        sup, cus = (str(rel.get("supplier") or "").strip(), str(rel.get("customer") or "").strip())
        ev = str(rel.get("evidence") or "").strip()
        text = _ws(source)
        ic = self.listed[issuer] if issuer else None
        if ic:   # 簡報裡寫的是「本公司」，名稱不會出現在片段裡；發行公司本人視為在文中
            sup, cus = ((ic["name"] if x in SELF_WORDS else x) for x in (sup, cus))
            text += _ws(ic["name"]) + _ws(ic["full"] or "") + issuer
        if not ev or _ws(ev) not in text:
            self.drop("evidence_not_found", rel, doc)
            return None
        for n in (sup, cus):
            if not n or (_ws(n).lower() not in text.lower() and _key(n) not in text.lower()):
                self.drop("name_not_in_text", rel, doc)
                return None
        sc, cc = self.map_code(sup), self.map_code(cus)
        # 引用的那句話要真的在講關係：含關係動詞，而且提到對方（標題至少提到一家；簡報主詞常省略，發行公司免提）
        evt = _ws(ev).lower()
        named = [(_ws(n).lower() in evt or _key(n) in evt) for n in (sup, cus)]
        if issuer and issuer in (sc, cc):
            named = [h for h, code in zip(named, (sc, cc)) if code != issuer]   # 只檢查非發行公司那一邊
            ok = all(named)
        else:
            ok = any(named)
        if not (ok and RELATION_KW.search(ev)):
            self.drop("evidence_missing_name", rel, doc)
            return None
        for n, code in ((sup, sc), (cus, cc)):
            if not code and GENERIC.search(n):
                self.drop("generic_name", rel, doc)
                return None
        if not sc and not cc:
            self.drop("no_tw_company", rel, doc)
            return None
        if (sc and sc == cc) or _key(sup) == _key(cus):
            self.drop("self", rel, doc)
            return None
        product_raw = str(rel.get("product") or "").strip()
        product = product_raw
        if product and _ws(product) not in text:   # 模型改寫或編造的產品名：只清掉 product，關係照留
            self.stats["product_not_in_text"] = self.stats.get("product_not_in_text", 0) + 1
            product = ""
        return {"supplier": self.listed[sc]["name"] if sc else sup, "supplier_code": sc,
                "customer": self.listed[cc]["name"] if cc else cus, "customer_code": cc,
                "kind": rel["kind"] if rel.get("kind") in ("供應", "合作") else "供應",
                "supplier_raw": sup, "customer_raw": cus,   # 重新驗證時用原文名稱（對到台股的名稱已換成簡稱）
                "product": product, "product_raw": product_raw,   # 重新驗證時用模型原始輸出
                "hedged": bool(rel.get("hedged")), "evidence": ev, "doc_type": doc_type, "unit": unit,
                "title": doc["title"], "url": doc["url"], "published": doc["published"], "doc_id": doc["id"]}


def dedupe_key(r: dict) -> tuple:
    a, b = (r["supplier_code"] or r["supplier"]), (r["customer_code"] or r["customer"])
    if r["kind"] == "合作":
        a, b = sorted((a, b))   # 合作沒有方向
    return a, b, r["kind"], _ws(r["evidence"])


# ---------- 主流程 ----------
def migrate_v2(c: dict) -> dict:
    """v1（沒有 product、單位編號沒前綴）的快取：備份原檔，主檔改成空的 v2，舊統計留在 stats["v1"]。"""
    v1 = OUT.with_suffix(".v1.json")
    if not v1.exists():   # 已經有備份就不覆蓋，避免把第一次的 v1 原貌蓋掉
        shutil.copy2(OUT, v1)
    print(f"v1 快取已備份到 {v1.name}（{len(c.get('relations', []))} 筆關係），主檔改為 v2 重新抽取")
    return {"version": 2, "done": {}, "relations": [], "stats": {"v1": c.get("stats", {})}}


def load_cache(migrate: bool = False) -> dict:
    """migrate=True（真的要呼叫 LLM 的那次）才升版；--revalidate 只讀不動。"""
    if OUT.exists():
        c = json.loads(OUT.read_text(encoding="utf-8"))
        if migrate and c.get("version") != 2:
            c = migrate_v2(c)
        c.setdefault("version", 1)   # 沒有版本欄位的舊檔就是 v1，--revalidate 存回去時不能被標成 v2
    else:
        c = {"version": 2}
    c.setdefault("done", {})
    c.setdefault("relations", [])
    c.setdefault("stats", {})
    st = c["stats"]
    st.setdefault("calls", 0)
    st.setdefault("seconds", 0.0)
    st.setdefault("errors", 0)
    st.setdefault("raw", 0)
    st.setdefault("product_not_in_text", 0)
    st.setdefault("drops", {})
    st.setdefault("samples", {})
    return c


def save_cache(c: dict):
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(OUT)   # 先寫暫存檔再換，Ctrl-C 不會把快取寫成半截


def revalidate(con, cache: dict, listed: dict):
    """用目前的全部規則重新驗證 cache 裡已存的關係（不呼叫 LLM）；通過的更新對應，沒通過的記進 stats。"""
    val = Validator(listed, cache["stats"])
    bodies = {r[0]: (r[1], r[2]) for r in con.execute(
        "SELECT d.id, d.body, p.code FROM documents d JOIN presentations p ON p.doc_id = d.id")}
    news = {}   # 新聞全文：v2 的 body 單位要拿全文重驗，標題比對會誤殺
    kept, seen, report = [], set(), []
    for r in cache["relations"]:
        rel = {"supplier": r.get("supplier_raw") or r["supplier"], "customer": r.get("customer_raw") or r["customer"],
               "kind": r["kind"], "hedged": r["hedged"], "evidence": r["evidence"],
               "product": r.get("product_raw", r.get("product", ""))}
        doc = {k: r[k] for k in ("title", "url", "published")} | {"id": r["doc_id"]}
        if r["doc_type"] == "法說" and r["doc_id"] in bodies:
            body, code = bodies[r["doc_id"]]
            out = val.check(rel, body or "", doc, "法說", issuer=code if code in listed else None, unit="presentation")
        elif r.get("unit") == "body":
            if r["doc_id"] not in news:
                f = BODY_DIR / f"{r['doc_id']}.json"
                news[r["doc_id"]] = json.loads(f.read_text(encoding="utf-8")).get("text", "") if f.exists() else ""
            out = val.check(rel, f"{r['title']}\n{news[r['doc_id']]}", doc, r["doc_type"], unit="body")
        else:
            out = val.check(rel, r["title"], doc, r["doc_type"])
        ok = bool(out and (k := dedupe_key(out)) not in seen)
        if ok:
            seen.add(k)
            kept.append(out)
        report.append((ok, out or r))
    cache["relations"] = kept
    return report


def main():
    global OUT   # --out 試跑時換掉輸出檔
    p = argparse.ArgumentParser(description="用本機 LLM 從新聞標題／法說會簡報抽取供應鏈關係")
    p.add_argument("--radar-db", default=str(DEFAULT_DB))
    p.add_argument("--base-url", default="http://localhost:1234/v1")
    p.add_argument("--model", default="qwen/qwen3-14b")
    p.add_argument("--limit", type=int, default=None, help="這次最多呼叫幾次 LLM")
    p.add_argument("--only", choices=["titles", "bodies", "presentations"])
    p.add_argument("--batch", type=int, default=12, help="每次呼叫放幾則標題")
    p.add_argument("--out", default=str(OUT), help="輸出檔（預設 data/llm_relations.json；試跑可指到別處，備份檔名跟著換）")
    p.add_argument("--revalidate", action="store_true", help="只用現行規則重新驗證已存的關係，不呼叫 LLM")
    args = p.parse_args()

    OUT = Path(args.out)
    con = sqlite3.connect(f"file:{Path(args.radar_db).as_posix()}?mode=ro", uri=True)
    cache = load_cache(migrate=not args.revalidate)
    done, st = cache["done"], cache["stats"]
    listed = load_listed()
    val = Validator(listed, st)
    if args.revalidate:
        for ok, r in revalidate(con, cache, listed):
            print(("保留 " if ok else "捨棄 ") + f"{r['supplier']}({r.get('supplier_code')}) → {r['customer']}({r.get('customer_code')}) "
                  f"({r['kind']}) 「{r['evidence'][:40]}」")
        if OUT.exists():
            shutil.copy2(OUT, OUT.with_suffix(".prev.json"))   # 被捨棄的關係只存在舊檔裡，先留一份再覆寫
        save_cache(cache)
        print(f"剩 {len(cache['relations'])} 筆；累計捨棄：{cache['stats']['drops']}")
        return
    seen = {dedupe_key(r) for r in cache["relations"]}

    # 工作單位：(unit_id 清單, 呼叫內容)
    jobs = []
    all_units = []
    bodies = load_bodies(con)   # 有全文的新聞改用全文片段，標題單位就不需要了（全文沒命中關鍵字、切不出片段的除外）
    if args.only in (None, "titles"):
        has_body = {b["id"] for b in bodies if b["chunks"]}
        titles = [t for t in load_titles(con) if t["id"] not in has_body]
        all_units += [VER + "t:" + t["id"] for t in titles]
        todo = [t for t in titles if VER + "t:" + t["id"] not in done]
        for i in range(0, len(todo), args.batch):
            jobs.append(("titles", todo[i:i + args.batch]))
    if args.only in (None, "bodies"):
        for d in bodies:
            all_units += [f"{VER}b:{d['id']}:{i}" for i in range(len(d["chunks"]))]
            for i, ch in enumerate(d["chunks"]):
                if f"{VER}b:{d['id']}:{i}" not in done:
                    jobs.append(("body", (d, i, ch)))
    if args.only in (None, "presentations"):
        pres = load_presentations(con)
        for d in pres:
            all_units += [f"{VER}p:{d['id']}:{i}" for i in range(len(d["chunks"]))]
            for i, ch in enumerate(d["chunks"]):
                if f"{VER}p:{d['id']}:{i}" not in done:
                    jobs.append(("pres", (d, i, ch)))
    total = len(all_units)
    print(f"工作單位 {total}（已完成 {sum(u in done for u in all_units)}），本次排隊 {len(jobs)} 次呼叫"
          + (f"，上限 {args.limit}" if args.limit else ""))

    run_calls, run_secs, fails = 0, 0.0, 0
    kept_before = len(cache["relations"])
    drops_before, pnt_before = dict(st["drops"]), st["product_not_in_text"]
    try:
        for kind, payload in jobs:
            if args.limit is not None and run_calls >= args.limit:
                break
            if kind == "titles":
                batch = payload
                user = f"以下是 {len(batch)} 則新聞標題，每則有編號。item 填標題編號。\n" + \
                       "\n".join(f"{i}. {t['title']}" for i, t in enumerate(batch, 1))
                unit_ids = [VER + "t:" + t["id"] for t in batch]
            elif kind == "body":
                d, i, ch = payload
                user = f"以下是一則新聞全文的節錄（第一行是標題）。\n---\n{ch}\n---"
                unit_ids = [f"{VER}b:{d['id']}:{i}"]
            else:
                d, i, ch = payload
                c = listed.get(d["code"], {})
                nm = c.get("name", d["code"])
                user = (f"以下是 {nm}（{d['code']}）法說會簡報的節錄文字。文中「本公司」「我們」「公司」都指 {nm}。\n"
                        f"---\n{ch}\n---")
                unit_ids = [f"{VER}p:{d['id']}:{i}"]
            t0 = time.time()
            try:
                rels = call_llm(args.base_url, args.model, user, kind == "titles")
            except (requests.RequestException, ValueError) as e:
                st["errors"] += 1
                fails += 1
                print(f"  呼叫失敗（{unit_ids[0]}）：{str(e)[:120]}")
                if fails >= 3:
                    print("連續失敗 3 次，停止（LM Studio 有開嗎？）")
                    break
                continue
            fails = 0
            dt = time.time() - t0
            run_calls += 1
            run_secs += dt
            st["calls"] += 1
            st["seconds"] = round(st["seconds"] + dt, 1)
            for rel in rels:
                st["raw"] += 1
                if kind == "titles":
                    n = rel.get("item")
                    if not isinstance(n, int) or not 1 <= n <= len(batch):
                        val.drop("evidence_not_found", rel, {"title": "（編號無效）"})
                        continue
                    doc = batch[n - 1]
                    out = val.check(rel, doc["title"], doc, "新聞")
                elif kind == "body":
                    out = val.check(rel, ch, d, "新聞", unit="body")
                else:
                    out = val.check(rel, ch, d, "法說", issuer=d["code"] if d["code"] in listed else None,
                                    unit="presentation")
                if out and (k := dedupe_key(out)) not in seen:
                    seen.add(k)
                    cache["relations"].append(out)
            for u in unit_ids:
                done[u] = True
            save_cache(cache)
            if run_calls % 10 == 0:
                print(f"  {run_calls} 次呼叫，{len(cache['relations'])} 筆關係，{run_secs / run_calls:.1f} 秒/次")
    except KeyboardInterrupt:
        print("\n中斷，已存檔；下次執行會接著跑")
    save_cache(cache)

    n_done = sum(u in done for u in all_units)
    print(f"\n本次呼叫 {run_calls} 次，單位 {n_done}/{total}，"
          f"保留 {len(cache['relations'])} 筆（本次新增 {len(cache['relations']) - kept_before}）")
    run_drops = {k: v - drops_before.get(k, 0) for k, v in st["drops"].items() if v - drops_before.get(k, 0)}
    print(f"本次捨棄：{run_drops or '無'}；累計：{st['drops']}")
    print(f"product 不在原文而清空：本次 {st['product_not_in_text'] - pnt_before}，累計 {st['product_not_in_text']}")
    if run_calls:
        print(f"{run_secs / run_calls:.1f} 秒/次")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
