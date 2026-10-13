"""MOPS 重大訊息的資本支出公告（取得設備、廠務工程、廠房）→ 「發布公司向某交易相對人採購」的關係。

解析規則複製自 supply-chain-radar 專案的 radar/material.py（CAPEX_SUBJECT、NOT_CAPEX、FX／UNIT／AMOUNT、
is_capex、parse_amount、_field、parse_counterparty、parse_capex 的標的萃取），兩邊必須保持同步；
這裡不 import radar 套件，也不要求主題、量級、signal_type。

資料來源是 radar.db 與 backtest.db（唯讀開啟，不會寫入），取 source 為「重大訊息-…」且有內文的公告。
公告是固定格式的表格，只取寫明的事實：標的、金額、交易相對人。

另有 load_contracts()：同一批公告裡的重要合約、承攬、得標、供貨／銷售協議（src "contract"），
要先判斷發布公司是賣方或買方，看不出來就不收；已被 capex 收走的設備／工程／廠房公告不重複收。
"""

import re
import sqlite3
import sys
from pathlib import Path

from llm_rel import _key, build_name_index
from official import load_listed

RADAR_DATA = Path(__file__).resolve().parent.parent / "supply-chain-radar" / "data"
DBS = ["radar.db", "backtest.db"]

# ---- 以下複製自 radar/material.py ----
CAPEX_SUBJECT = re.compile("機器設備|設備|廠務|廠房|興建|新建|擴建|委建|土地|不動產|建物|建築物改良")
# 處分、租約、更正／補充（避免和原公告重複計算）、股權交易都不算新的資本支出
NOT_CAPEX = re.compile("處分|出售|終止|取消|解約|更正|補充|變更|使用權|租賃|承租|股份(?!有限)|股權|都市更新|合建")

# 約略匯率（新台幣）
FX = {
    "新台幣": 1, "新臺幣": 1, "台幣": 1, "臺幣": 1, "NT$": 1, "NTD": 1, "TWD": 1,
    "美金": 32, "美元": 32, "USD": 32, "US$": 32, "人民幣": 4.5, "RMB": 4.5, "CNY": 4.5,
    "日圓": 0.21, "日幣": 0.21, "JPY": 0.21, "歐元": 36, "EUR": 36, "泰銖": 0.95, "THB": 0.95,
    "馬幣": 7.5, "MYR": 7.5, "墨西哥比索": 1.7, "越南盾": 0.0013, "VND": 0.0013,
}
UNIT = {"億": 1e8, "仟萬": 1e7, "千萬": 1e7, "佰萬": 1e6, "百萬": 1e6, "萬": 1e4, "仟": 1e3, "千": 1e3}
_CUR = "|".join(re.escape(c) for c in sorted(FX, key=len, reverse=True))
_UNIT = "|".join(sorted(UNIT, key=len, reverse=True))
AMOUNT = re.compile(rf"({_CUR})\s*(?:NT\$)?\s*約?\s*([\d,]+(?:\.\d+)?)\s*({_UNIT})?")


def is_capex(subject: str) -> bool:
    return bool(CAPEX_SUBJECT.search(subject)) and not NOT_CAPEX.search(subject)


def _to_twd(m: re.Match) -> float:
    return float(m.group(2).replace(",", "")) * UNIT.get(m.group(3) or "", 1) * FX[m.group(1)]


def parse_amount(body: str) -> tuple[float | None, str]:
    """回傳 (新台幣金額, 原文片段)。優先取「總金額」後面的數字；同時有外幣和台幣時取台幣。"""
    best = None
    for i in (m.end() for m in re.finditer("總金額", body)):
        window = body[i : i + 60]
        hits = list(AMOUNT.finditer(window))
        # 「…及交易總金額:」這種欄位標題後面，先出現的可能是單價或下一個「總金額」
        if not hits or hits[0].start() > 25 or re.search("總金額|單價|單位價格", window[: hits[0].start()]):
            continue
        twd = [h for h in hits if FX[h.group(1)] == 1] or hits[:1]
        h = twd[0]
        cand = (_to_twd(h), body[max(0, i - 5) : i + h.end()])
        if best is None or cand[0] > best[0]:
            best = cand
    if best:
        return best
    # 沒有「總金額」欄位（例如「累計交易金額為新台幣…」）：取全文最大的台幣金額
    hits = [h for h in AMOUNT.finditer(body) if FX[h.group(1)] == 1] or list(AMOUNT.finditer(body))
    if not hits:
        return None, ""
    h = max(hits, key=_to_twd)
    return _to_twd(h), body[max(0, h.start() - 8) : h.end()]


def _field(body: str, pattern: str) -> str:
    m = re.search(pattern, body)
    return m.group(1).strip(" 。;；") if m else ""


def parse_counterparty(subject: str, body: str) -> str | None:
    name = _field(body, r"(?:交易|契約)相對人[^:：]*[:：]\s*(?:(?:交易|契約)相對人\s*[:：])?\s*([^；;，,。]{2,60})")
    name = re.split(r"[（(]非?關係人|[；;，,。]|\s\d+\.|與公司|與本公司", name)[0].strip()
    if not name or name == "無" or re.search(r"公告|自然人|不適用|簽約|^[(（除]", name):
        m = re.search(r"向(?!關係人)(.{2,30}?)(?:取得|購買|訂購)", subject)
        name = m.group(1).strip() if m else ""
    return name.replace("(股)", "").replace("（股）", "") or None


def parse_item(subject: str, body: str) -> str:
    """parse_capex 裡的標的萃取。body 已壓成單行空白。"""
    item = _field(body, r"標的物之名稱及性質[^:：]*[:：]\s*(.+?)\s*(?:2\.|$)") or _field(
        body, r"契約種類\s*[:：]\s*(.+?)\s*(?:2\.|$)")
    item = re.split(r"[，,；;（(]", item)[0].strip()
    if "地號" in item:
        item = "土地"
    if not item or len(item) > 20:
        kw = CAPEX_SUBJECT.search(subject).group(0)
        item = {"設備": "機器設備", "廠務": "廠務工程", "委建": "廠房", "興建": "廠房", "新建": "廠房"}.get(kw, kw)
    return item
# ---- 複製結束 ----

# 交易相對人欄位常見的非公司內容（自然人、不揭露、多位地主…）。對照實際資料後調整
_NOT_COMPANY = re.compile(r"自然人|個人|不適用|^無|詳|註|多位|地主|共有人|等\d*人|若干|未揭露|免揭露|不揭露|依|請|公告|尚未|未定|待|園區$|"
                          r"^(?:非?關係人|(?:子|母|本|集團)公司|關係企業|不特定.*)$|^\W*$")
# 欄位前綴、後面附的關係說明（「（本公司百分之百轉投資」「為本公司100%持有之子公司」）不是名稱的一部分
_PREFIX = re.compile(r"^(?:交易對象|交易相對人)(?:為)?\s*[:：]?\s*[「『]?")
_SUFFIX = re.compile(r"[」』]?\s*(?:[（(]\s*(?:本公司|子公司|非?關係|其|以下)?\s*簡稱|[（(]\s*(?:本公司|子公司|非?關係|其)|為本公司|為子公司|\s其$|\s非?關係人$|為.{0,6}之子公司).*$")
# 「及／與／和」是常見的公司名用字（和碩、台灣…及…），只有前面剛好是「…公司」才當作分隔
_SPLIT = re.compile(r"[、，,/／]|(?<=公司)(?:及|與|和|暨)")


def _clean(name: str) -> str:
    name = _SUFFIX.sub("", _PREFIX.sub("", re.sub(r"\s+", " ", name).strip()))
    return name.strip(" 。;；:：()（）「」『』")


def canon(name: str) -> str:
    """同一家公司在不同公告裡的寫法（…股份有限公司／…有限公司／…公司）合成一個鍵。"""
    return re.sub(r"(?:股份)?(?:有限)?公司$", "", name.strip()).lower().replace(" ", "")


def split_counterparties(raw: str) -> list[str]:
    """一欄寫了好幾家時拆開；拆出來有太短的片段就當作不是清單，整串保留。"""
    parts = [p for p in (_clean(p) for p in _SPLIT.split(raw)) if p and p != "無"]   # 「A公司/無」的「無」是關係欄位
    if len(parts) > 1 and not all(len(p) >= 2 for p in parts):
        return [_clean(raw)]
    return parts


def _rows(db: str):
    con = sqlite3.connect(f"file:{RADAR_DATA / db}?mode=ro", uri=True)
    try:
        yield from con.execute(
            "SELECT id, title, url, published, company_code, body FROM documents "
            "WHERE source LIKE '重大訊息%' AND body IS NOT NULL AND body != '' ORDER BY published")
    finally:
        con.close()


def make_code_mapper(listed: dict):
    """交易相對人名稱 → 上市櫃代號（對不到回 None）。capex 與 contract 兩條路共用。"""
    idx = build_name_index(listed)
    fulls = [(_key(c["full"]), code) for code, c in listed.items() if c["full"]]

    def map_code(name: str) -> str | None:   # 同 llm_rel.Validator.map_code：精確比對，再試唯一前綴
        # parse_counterparty 把「(股)」拿掉後只剩「…科技公司」，補一個去掉尾巴「公司」的版本才對得上全名
        for k in dict.fromkeys((_key(name), _key(re.sub(r"公司$", "", name)))):
            if k in idx:
                return idx[k]
            if len(k) >= 3:
                hit = {code for fk, code in fulls if fk.startswith(k)}
                if len(hit) == 1:
                    return hit.pop()
        return None

    return map_code


def load_capex(stats: dict | None = None, listed: dict | None = None) -> list[dict]:
    """每筆 {issuer, role, item, amount_twd, date, counterparty, code, name, url, title, doc_id, db}。
    role：buyer＝發布公司向相對人買（預設）、seller＝發布公司是賣方（得標、承攬…）。
    code：交易相對人對得到上市櫃代號時才有；自己和自己交易、非公司的相對人已排除。stats 給了就填統計。"""
    listed = listed or load_listed()
    map_code = make_code_mapper(listed)

    st = stats if stats is not None else {}
    for k in ("announcements", "with_cp", "self", "range", "dropped_names"):
        st.setdefault(k, {})
    out, seen = [], set()
    for db in DBS:
        if not (RADAR_DATA / db).exists():
            continue
        dates = []
        for doc_id, title, url, published, issuer, body in _rows(db):
            if not issuer or not is_capex(title or ""):
                continue
            flat = re.sub(r"\s+", " ", body)
            raw = parse_counterparty(title, flat)
            st["announcements"][db] = st["announcements"].get(db, 0) + 1
            if not raw:
                continue
            day = (published or "")[:10]
            item = parse_item(title, flat)
            amount, _ = parse_amount(flat)
            # 預設發布公司是買方；公告字眼明確是賣方（得標、承攬、接獲、銷售…）才翻轉，其餘維持原樣
            # 土地標售的「得標」是買地，不是賣方
            seller = (not re.search("土地|地號|標售|不動產", title or "")
                      and contract_direction(re.sub(r"\s+", "", title or ""), flat, contract_main(flat)) == "supplier")
            role = "seller" if seller else "buyer"
            got = False
            for cp in split_counterparties(raw):
                if _NOT_COMPANY.search(cp) or len(cp) < 2:
                    st["dropped_names"][cp] = st["dropped_names"].get(cp, 0) + 1
                    continue
                code = map_code(cp)
                if code == issuer:
                    st["self"][db] = st["self"].get(db, 0) + 1
                    continue
                got = True
                key = (issuer, day, item, code or canon(cp), amount, role)
                if key in seen:
                    continue
                seen.add(key)
                dates.append(day)
                out.append({"issuer": issuer, "role": role, "item": item, "amount_twd": amount, "date": day, "counterparty": cp,
                            "code": code, "key": code or "n:" + canon(cp), "name": listed[code]["name"] if code else cp, "url": url,
                            "title": title, "doc_id": doc_id, "db": db})
            if got:
                st["with_cp"][db] = st["with_cp"].get(db, 0) + 1
        st["range"][db] = (min(dates), max(dates)) if dates else None
    st["mapped"] = sum(1 for r in out if r["code"])
    return out


# ---- 重大訊息合約公告（src "contract"）：重要合約、承攬、得標、供貨／銷售／採購協議 ----
# 和 capex 不同，這裡要先判斷「發布公司是賣方還是買方」，看不出來就不收
CONTRACT_TITLE = re.compile("合約|契約|訂單|供貨|供應協議|採購協議|銷售協議|承攬|得標|標得|決標")
# 依序比對，第一個命中的就是丟掉的原因（統計用）。融資、租賃、合建、糾紛、更正、資產買賣都不是供應關係
CONTRACT_DROP = [
    ("financing", re.compile("授信|借款|貸款|聯貸|租賃|承租|出租|融資|衍生性|背書|保證|擔保")),
    ("realestate_jv", re.compile("都市更新|合建|合作興建|共同開發|標售|土地買賣")),
    ("dispute_end", re.compile("和解|訴訟|爭議|仲裁|終止|解除|到期|撤銷|取消")),
    # 更正、補充會和原公告重複；追加／追減的金額有時是增減額有時是總額，不能直接相加
    ("correction", re.compile("更正|補充|補行|說明|變更|追加|追減")),
    ("asset_trade", re.compile("取得|購置|處分|股權|股份(?!有限)|董事|會計師")),
    ("tentative", re.compile("擬|預計")),   # 董事會通過、尚未簽約
]
# 賣方：發布公司接案、出貨。「X 向本公司採購」「接受 X 委託」雖然出現買方字眼，其實發布公司是賣方，先挖掉再比對
_SUP_SPECIAL = re.compile(r"向本公司(?:之|全資)?(?:子公司)?[^，。；]{0,25}?(?:採購|購買|訂購)|(?:接受|受)[^，。；]{0,15}委託|(?:委託|委由)本公司")
_SUPPLIER = re.compile("承攬|得標|標得|接獲|承接|銷售|供貨|出售產品|授權予")
# 「採購金額」是欄位名；「設計、採購與建造」是統包（EPC）的工項；「委託生產／製造」不一定是發布公司下單（CDMO、代工），都不算買方字眼
_NOT_BUYER = re.compile(r"設計[、，]\s*採購|採購(?=金額|總|價)|委託(?=生產|製造|代工|加工)")
_BUYER = re.compile(r"委託|委由|發包|採購|委建|向[^，。；]{1,30}?(?:購買|購入|訂購)")
# 契約種類欄（委建類公告）是「發布公司把工程發包出去」的固定格式，標題常寫「承攬契約」但發布公司其實是業主
_BUYER_KIND = re.compile("委建|工程")
# 匿名相對人（「美國某公司」「I公司」）畫不成節點
_ANON = re.compile(r"某|客戶|^[A-Za-z]公司$|^[一-鿿]{0,6}[A-Za-z]公司$")
# 經銷／代理：發布公司可能是經銷商（向對方進貨再賣），也可能是原廠，內文的「銷售」兩種都會出現，不猜
_DISTRIB = re.compile("經銷|代理|分銷")
_ITEM_LABEL = re.compile(r"(?:工程內容|契約內容|契約名稱|契約標的|工程名稱|合約項目|承攬契約內容)\s*[:：]\s*[「『]?")
_ITEM_END = re.compile(r"[」』；;。，,]|\s(?:工程採購|契約總|工程契約|簽約日期|工程地點|限制條款)|[（(]\d[）)]")
_MAIN = re.compile(r"(?:契約)?主要內容[^:：]*[:：]\s*(.+?)\s*(?=\d{1,2}\.\s*(?:限制條款|專業估價))|發生緣由\s*[:：]\s*(.+?)\s*(?=\d{1,2}\.\s*因應措施)")
_CP_FIELD = re.compile(r"(?:契約或承諾|契約|交易)相對人[^:：]*[:：]\s*(.+?)\s+\d{1,2}\.\s*\S")
_CP_CUT = re.compile(r"與(?:本)?公司之?關係|其與公司|[（(]非?關係人|[；;，,。]|\s非?關係人|\s其\s|為本公司|\s\d+\.")


def contract_main(flat: str) -> str:
    """主要內容欄（重大契約）或發生緣由欄（一般重大訊息）。取不到回空字串。"""
    m = _MAIN.search(flat)
    return (m and (m.group(1) or m.group(2)) or "").strip()


def contract_direction(title: str, flat: str, main: str) -> str | None:
    """回傳 "supplier"（發布公司是賣方）、"buyer"（買方）、None（看不出來）。"""
    kind = _field(flat, r"契約種類\s*[:：]\s*([^ ]{1,15})")
    if kind:
        return "buyer" if _BUYER_KIND.search(kind) else None
    if _DISTRIB.search(title + main):
        return None
    text = _NOT_BUYER.sub("", _SUP_SPECIAL.sub("承接", title + " " + main))   # 特殊句型換成賣方字眼，不要被當成買方
    sup, buy = bool(_SUPPLIER.search(text)), bool(_BUYER.search(text))
    return "supplier" if sup and not buy else "buyer" if buy and not sup else None


def parse_contract_counterparty(flat: str) -> list[str]:
    m = _CP_FIELD.search(flat)
    if not m:
        return []
    raw = re.sub(r"[（(]\d+[）)]", "、", m.group(1))   # 「(1)A (2)B」多家
    raw = re.sub(r"[（(][^（()）]*[）)]", "", raw)   # 括號裡的英文名、簡稱不要，也免得被逗號切成半個括號
    raw = re.sub(r"(公司)\s+(?=及|與|和|暨)", r"\1", raw)
    parts = []
    for x in raw.split("、"):
        x = re.sub(r"^(?:(?:契約之?|交易)相對人|交易對象)\s*[:：]?\s*", "", x.strip())
        parts.append(re.sub(r"等(?:公司|廠商)$", "", _CP_CUT.split(x.replace("(股)", "").replace("（股）", ""))[0].strip()))
    return split_counterparties("、".join(p for p in parts if p))


_TITLE_VERB = re.compile("簽訂|簽署|續訂|標得|得標")
_TITLE_LEAD = re.compile(r"^(?:代.{0,60}?)?公告(?:本公司)?|^本公司")


def contract_item(title: str, main: str) -> str:
    """標的：優先取內文「工程內容／契約名稱」這類欄位，否則取標題去掉「公告本公司與…簽訂」的前綴。≤40 字。"""
    item = ""
    if m := _ITEM_LABEL.search(main):
        item = _ITEM_END.split(main[m.end():])[0].strip()
    if len(item) < 4:
        t = re.sub(r"^[^：]{1,30}（\w+）：", "", title.strip())
        m = _TITLE_VERB.search(t)   # 動詞後面才是標的；沒有動詞就只去掉「公告本公司」
        item = (t[m.end():] if m else _TITLE_LEAD.sub("", t)).strip(" :：「」『』")
    if len(item) < 6:   # 標題只有「重要合約」這類泛稱，改用內文第一句
        item = re.split(r"[，,；;。]", main)[0].strip() or item
    return item if len(item) <= 40 else item[:39] + "…"


def _amount_fallback(main: str) -> float | None:
    """「金額約11.3億元」沒寫幣別，parse_amount 抓不到；公告都是台灣公司，當新台幣。"""
    m = re.search(r"金額(?:約|為)?\s*([\d,]+(?:\.\d+)?)\s*(億|萬)?元", main)
    return float(m.group(1).replace(",", "")) * UNIT.get(m.group(2) or "", 1) if m else None


def load_contracts(stats: dict | None = None, listed: dict | None = None) -> list[dict]:
    """每筆 {issuer, role, item, amount_twd, date, counterparty, code, key, name, url, title, doc_id, db}。
    role：supplier＝發布公司賣給相對人；buyer＝發布公司向相對人買。方向看不出來、相對人是匿名／非公司的都不收。
    相對人有好幾家時金額留空（公告只寫合計，不能每家都算一次）。"""
    listed = listed or load_listed()
    map_code = make_code_mapper(listed)
    st = stats if stats is not None else {}
    for k in ("dropped", "by_role", "dropped_names"):
        st.setdefault(k, {})
    for k in ("candidates", "direction_unknown", "no_counterparty", "kept_announcements"):
        st.setdefault(k, 0)
    bump = lambda d, k: d.__setitem__(k, d.get(k, 0) + 1)
    out, seen = [], set()
    for db in DBS:
        if not (RADAR_DATA / db).exists():
            continue
        for doc_id, title, url, published, issuer, body in _rows(db):
            raw_title, title = title or "", re.sub(r"\s+", "", title or "")
            if not issuer or not CONTRACT_TITLE.search(title):
                continue
            st["candidates"] += 1
            reason = next((r for r, rx in CONTRACT_DROP if rx.search(title)), None)
            flat = re.sub(r"\s+", " ", body)
            main = contract_main(flat)
            if not reason and is_capex(title):
                # capex 收得到的就是 capex 的（含它翻成賣方的）；capex 讀不到相對人的賣方公告（「契約或承諾相對人」欄位、
                # 土地標售除外）才由這裡補，不然兩邊都會漏掉
                lost = (not re.search("土地|地號|標售|不動產", title) and not parse_counterparty(raw_title or "", flat)
                        and contract_direction(title, flat, main) == "supplier")
                if not lost:
                    reason = "capex_dup"
            if reason:
                bump(st["dropped"], reason)
                continue
            role = contract_direction(title, flat, main)
            if not role:
                st["direction_unknown"] += 1
                continue
            cps = []
            for cp in parse_contract_counterparty(flat):
                if _NOT_COMPANY.search(cp) or _ANON.search(cp) or len(cp) < 2:
                    bump(st["dropped_names"], cp)
                else:
                    cps.append(cp)
            if not cps:
                st["no_counterparty"] += 1
                continue
            day = (published or "")[:10]
            item = contract_item(title, main)
            amount = None
            if len(cps) == 1 and re.search("金額|總價|價款", main):   # 沒有金額字眼時 parse_amount 會抓到單價、管理費
                amount = parse_amount(main)[0] or _amount_fallback(main)
                amount = round(amount) if amount and amount >= 1e4 else None   # 小於一萬多半是單價
            got = False
            for cp in cps:
                code = map_code(cp)
                if code == issuer:
                    continue
                key = (issuer, role, day, item, code or canon(cp), amount)
                if key in seen:
                    continue
                seen.add(key)
                got = True
                bump(st["by_role"], role)
                out.append({"issuer": issuer, "role": role, "item": item, "amount_twd": amount, "date": day,
                            "counterparty": cp, "code": code, "key": code or "n:" + canon(cp),
                            "name": listed[code]["name"] if code else cp, "url": url, "title": re.sub(r"\s+", " ", raw_title),
                            "doc_id": doc_id, "db": db})
            st["kept_announcements"] += got
    st["mapped"] = sum(1 for r in out if r["code"])
    return out


if __name__ == "__main__":
    import time

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    t = time.time()
    stats: dict = {}
    rows = load_capex(stats)
    print(f"耗時 {time.time() - t:.1f}s；筆數 {len(rows)}；對到台股代號 {stats['mapped']}")
    for k in ("announcements", "with_cp", "self", "range"):
        print(k, stats[k])
    print("被丟掉的相對人 top20：", sorted(stats["dropped_names"].items(), key=lambda kv: -kv[1])[:20])
    for r in rows[:5] + [r for r in rows if r["code"]][:5]:
        print({k: r[k] for k in ("issuer", "date", "item", "amount_twd", "counterparty", "code", "db")})
    cst: dict = {}
    crows = load_contracts(cst)
    print(f"合約公告：{len(crows)} 筆；", {k: v for k, v in cst.items() if k != "dropped_names"})
    for r in crows[:8]:
        a, b = (r["issuer"], r["name"]) if r["role"] == "buyer" else (r["name"], r["issuer"])
        print(f"  {a} ← {b}: {r['item']} {r['amount_twd'] or ''} {r['date']}")
