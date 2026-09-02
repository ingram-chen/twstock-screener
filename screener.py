#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""台股選股看板 — 六面向評分，選出最值得買入的前三檔。

資料源全部免費、無金鑰：
  技術面   Yahoo Finance chart API (日K 6個月)
  基本面   TWSE BWIBBU_ALL (本益比/淨值比/殖利率) + t187ap05_L (月營收)
  籌碼面   TWSE T86 三大法人買賣超
  大戶     TDCC 集保戶股權分散表 (週資料，落盤後可算週變化)
  消息/輿論 Google News RSS 標題數 + 中文關鍵字情緒詞典
  市場氛圍  Yahoo ^TWII + 全市場站上月均價家數 + 融資餘額變化 (盤面級純量)

範圍：上市 (TWSE) 普通股。
# ponytail: 上櫃(TPEx)另有一組 openapi 端點，要涵蓋再接，別為了對稱先寫。

用法:
  python3 screener.py            # 抓資料 → 產生 dashboard.html
  python3 screener.py --selftest # 跑內建檢查
"""
import csv, email.utils, io, json, math, os, re, sys, time, html, urllib.parse
from datetime import datetime, date, timedelta, timezone

TPE = timezone(timedelta(hours=8))   # 台北時區。不靠機器 TZ —— 在 UTC 主機上會差 8 小時

import requests
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "cache")
os.makedirs(CACHE, exist_ok=True)      # 新機器上沒有這行會在第一個請求就死
if hasattr(sys.stdout, "reconfigure"):  # Windows 主控台預設不是 UTF-8，中文會炸
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
UA = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 45

# 評分權重 (加總為 1)。市場氛圍不在此列 — 它是盤面級純量，不能拿來排序個股。
WEIGHTS = {"tech": 0.25, "fund": 0.25, "chips": 0.25, "whale": 0.15, "news": 0.10}
WHALE_W_NO_DELTA = 0.05        # 只有水位沒有週變化時，大戶面向的降級權重
MIN_TURNOVER = 50_000_000     # 每日成交金額下限(元)，濾掉流動性陷阱
SHORTLIST = 60                # 第二階段深挖檔數。用月均價當代理技術面的誤差
                              # 比前三名的分差還大，漏斗要留足夠緩衝


def weights(have_delta):
    """大戶『水位』是結構性的（權值股常年偏高），不是買賣訊號。
    只有算得出週變化時才給它完整權重，否則降級並把權重挪給籌碼面。"""
    w = dict(WEIGHTS)
    if not have_delta:
        spare = w["whale"] - WHALE_W_NO_DELTA
        w["chips"] += spare / 2
        w["tech"] += spare / 2
        w["whale"] = WHALE_W_NO_DELTA
    assert abs(sum(w.values()) - 1) < 1e-9
    return w


# ---------------------------------------------------------------- 工具

def roc_to_ad(s):
    """民國日期字串 '1150831' -> date(2026,8,31)"""
    s = str(s).strip()
    return date(int(s[:-4]) + 1911, int(s[-4:-2]), int(s[-2:]))


def num(x, default=math.nan):
    """TWSE 的數字欄位可能是 '', '-', '1,234,567'、'--'。空值一律回 NaN，絕不回 0。"""
    if x is None:
        return default
    s = str(x).replace(",", "").replace("+", "").strip()
    if s in ("", "-", "--", "N/A", "nan", "null"):
        return default
    try:
        return float(s)
    except ValueError:
        return default


NET_CALLS = 0          # 實際發出的網路請求數。暖快取時不必為了禮貌而 sleep


def get(url, kind="json", cache_key=None, ttl=6 * 3600):
    """帶檔案快取的 GET。重跑不重抓。

    順序是「解析成功才落檔」而不是「先落檔再解析」—— 半截的下載（磁碟滿、
    Ctrl-C、proxy 錯誤頁）若先寫進快取就成了合法資料，TDCC 週快照更會永久卡住
    並在下週變成比較基準，安靜地製造出幾十個百分點的假變化。
    再用 temp + os.replace 原子換檔，避免中斷留下半截檔。"""
    global NET_CALLS
    path = os.path.join(CACHE, cache_key) if cache_key else None
    if path and os.path.exists(path) and time.time() - os.path.getmtime(path) < ttl:
        with open(path, "rb") as fh:
            return _parse(fh.read(), kind)

    # 無人看管的排程跑在別台機器上，一次瞬斷不該讓整晚的選股掛掉
    for attempt in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=TIMEOUT)
            r.raise_for_status()
            break
        except requests.RequestException as e:
            if attempt == 2:
                raise
            log(f"  重試 {attempt + 1}/2 ({type(e).__name__}): {url[:70]}")
            time.sleep(2 ** attempt)
    NET_CALLS += 1
    data = _parse(r.content, kind)      # 先解析，爛回應在這裡就炸，不會落檔
    if path:
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(r.content)
        os.replace(tmp, path)
    return data


def _parse(raw, kind):
    return json.loads(raw.decode("utf-8-sig")) if kind == "json" else raw.decode("utf-8-sig")


def pct_rank_by(s, groups, higher_is_better=True, min_n=5):
    """產業內百分位。本益比、淨值比、殖利率的合理區間隨產業差一個量級
    （金控 PB 0.8 vs 半導體 PB 4），全市場同池比會讓整個金融類股霸榜。

    樣本不足的產業併成一個「其他」組一起排，不要退回全市場 ——
    那等於把這個函數要修掉的跨產業偏誤，對那批股票原封不動放回去。"""
    g = groups.fillna("其他").astype(str)
    g = g.where(g.map(g.value_counts()) >= min_n, "其他")
    out = s.groupby(g).rank(pct=True, ascending=higher_is_better)
    return out.reindex(s.index).fillna(0.5)


def pct_rank(s, higher_is_better=True):
    """橫斷面百分位 (0..1)，1 = 最好。缺值 -> 0.5 中性，絕不當成 0。
    原始值尺度天差地遠(本益比~9 vs 法人買超~1e7)且厚尾，只能用排序。

    刻意不叫 ascending —— pandas 的 ascending 講的是「排名方向」不是「好壞方向」，
    兩者剛好相反，用錯整份榜單會靜靜地翻過來。這裡只暴露好壞方向。"""
    return s.rank(pct=True, ascending=higher_is_better).fillna(0.5)


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- 資料抓取

def fetch_universe():
    """回傳 (df, 交易日, 處置股檔數)。已濾掉 ETF/權證/DR、處置股、流動性不足者。"""
    q = get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL",
            cache_key="quotes.json")
    trade_date = roc_to_ad(q[0]["Date"])

    df = pd.DataFrame([{
        "code": r["Code"].strip(), "name": r["Name"].strip().rstrip("*"),
        "close": num(r["ClosingPrice"]), "high": num(r["HighestPrice"]),
        "low": num(r["LowestPrice"]), "open": num(r["OpeningPrice"]),
        "volume": num(r["TradeVolume"]), "turnover": num(r["TradeValue"]),
    } for r in q])

    # 普通股 = 四碼數字且不以 0 開頭，擋掉 00xxx ETF 與 5-6 碼權證。
    # 存託憑證擋不掉（9103 美德醫療-DR、9105 泰金寶-DR… 都是四碼），只能認名字。
    df = df[df["code"].str.fullmatch(r"[1-9]\d{3}") & ~df["name"].str.contains("DR$|-DR")]
    df = df[(df["turnover"] >= MIN_TURNOVER) & (df["close"] > 0)]

    mg = get("https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN",
             cache_key="margin.json")
    # 融資限額由證交所依流通股數設定，所以「融資餘額 ÷ 融資限額」就是正規化後的
    # 散戶槓桿水位，不必再去查發行股數。低 = 籌碼安定，主力比較不怕融資追繳的賣壓。
    mgd = {}
    for r in mg:
        lim, bal = num(r.get("融資限額"), 0), num(r.get("融資今日餘額"), 0)
        short = num(r.get("融券今日餘額"), 0)
        mgd[r["股票代號"].strip()] = {
            "margin_use": bal / lim * 100 if lim else math.nan,
            # 券資比的分母太小就純粹是雜訊（179 張融資配 358 張融券算出 200%）
            "short_ratio": short / bal * 100 if bal >= 1000 else math.nan,
        }
    mgdf = pd.DataFrame(mgd).T

    punish = get("https://openapi.twse.com.tw/v1/announcement/punish",
                 cache_key="punish.json")
    bad = {r["Code"].strip() for r in punish if r.get("Code", "").strip()}
    flags, drop = fetch_flags()
    df = df[~df["code"].isin(bad | drop)]
    df["flags"] = df["code"].map(lambda c: "；".join(flags.get(c, [])))

    avg = get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_AVG_ALL",
              cache_key=f"avg_{trade_date:%Y%m%d}.json")
    # 日期打進 cache key，讓「今天的收盤配昨天的月均價」在結構上不可能發生
    assert roc_to_ad(avg[0]["Date"]) == trade_date, f"月均價日期 {avg[0]['Date']} != 報價日"
    ma = {r["Code"].strip(): num(r["MonthlyAveragePrice"]) for r in avg}
    df["ma_month"] = df["code"].map(ma)

    df = df.set_index("code").join(fetch_monthly_volume()[0]).join(mgdf)
    # STOCK_DAY_AVG_ALL 的「月平均價」是<當月至今>均價，不是 20 日均線。每月第一個
    # 交易日它剛好等於當天收盤 —— 市場寬度會算出 0%、第一階段技術面代理變成常數 1.0，
    # 那天的漏斗等於完全沒看技術面。FMSRFK 的上月加權均價是穩定替代，且已經抓進來了。
    df["ref_price"] = (df["px_month_avg"] if (df["close"] == df["ma_month"]).mean() > .5
                       else df["ma_month"])
    # 籌碼面分母必須全體同一基準：一部分用整月成交量、另一部分用當日量×20，尺度不同，
    # 橫斷面排出來的名次是錯的而不只是差個常數。缺任何一檔就全體退回當日量。
    if int(df["vol_month"].isna().sum() + df["vol_month"].le(0).sum()):
        df["vol_month"] = math.nan
    return df, trade_date, len(bad)


def fetch_monthly_volume():
    """個股月成交量與週轉率。拿來當籌碼面的分母 ——
    用「當日」成交量去除「五日」買超，遇到當天爆量的股票分母會被灌大，訊號被稀釋。"""
    m = get("https://openapi.twse.com.tw/v1/exchangeReport/FMSRFK_ALL",
            cache_key="monthvol.json", ttl=24 * 3600)
    return pd.DataFrame([{
        "code": r["Code"].strip(), "vol_month": num(r["TradeVolumeB"]),
        "px_month_avg": num(r["WeightedAvgPriceAB"]),
    } for r in m]).set_index("code"), m[0]["Month"]


def fetch_value(trade_date):
    """本益比 / 淨值比 / 殖利率。空字串 = 無意義(虧損或未公布)，保持 NaN。"""
    v = get("https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL",
            cache_key=f"bwibbu_{trade_date:%Y%m%d}.json")
    assert roc_to_ad(v[0]["Date"]) == trade_date, f"本益比日期 {v[0]['Date']} != 報價日"
    return pd.DataFrame([{
        "code": r["Code"].strip(), "pe": num(r["PEratio"]),
        "pb": num(r["PBratio"]),
        # 全市場 1081 檔裡殖利率空字串 233 檔、"0.00" 0 檔 —— TWSE 用空字串表示
        # 「不配息」。當成缺值補中位數，等於讓 233 檔不配息的股票白拿半分。
        "yield": num(r["DividendYield"], 0.0),
    } for r in v]).set_index("code")


def fetch_revenue():
    """月營收年增率 / 月增率 — 台股最即時的免費基本面動能。"""
    rv = get("https://openapi.twse.com.tw/v1/opendata/t187ap05_L",
             cache_key="revenue.json", ttl=24 * 3600)
    return pd.DataFrame([{
        "code": r["公司代號"].strip(),
        "industry": r.get("產業別", "").strip(),
        "rev_ym": r.get("資料年月", ""),
        "rev_yoy": num(r["營業收入-去年同月增減(%)"]),
        "rev_mom": num(r["營業收入-上月比較增減(%)"]),
        "rev_cum_yoy": num(r["累計營業收入-前期比較增減(%)"]),
    } for r in rv]).set_index("code")


def _t86_one(d):
    """單日三大法人。必須檢查 stat=OK —— 日期給錯時 TWSE 回的是中文訊息而不是
    HTTP 錯誤，籌碼面會靜靜地變成全 0，整份排名跟著爛掉但不會有人發現。"""
    j = get(f"https://www.twse.com.tw/rwd/zh/fund/T86?date={d}&selectType=ALL&response=json",
            cache_key=f"t86_{d}.json", ttl=24 * 3600)
    if j.get("stat") != "OK" or "data" not in j:
        # 非交易日的回應是合法 JSON，get() 的解析檢查攔不到，會佔著快取一整天
        bad = os.path.join(CACHE, f"t86_{d}.json")
        if os.path.exists(bad):
            os.remove(bad)
        raise RuntimeError(f"T86 {d} 非交易日或取得失敗: {j.get('stat')}")
    f = j["fields"]
    i_fore, i_it, i_all = f.index("外陸資買賣超股數(不含外資自營商)"), \
        f.index("投信買賣超股數"), f.index("三大法人買賣超股數")
    return pd.DataFrame([{
        "code": r[0].strip(),
        "foreign_net": num(r[i_fore]),
        "trust_net": num(r[i_it]),
        "inst_net": num(r[i_all]),
    } for r in j["data"]]).set_index("code"), j["date"]


def fetch_quality():
    """獲利品質：毛利率、營業利益率、業外損益佔稅前的比重。

    台股「便宜」的最大假象來自集團交叉持股 —— BWIBBU 的本益比是拿含業外的 EPS
    去除，所以一家本業毛利 7% 的公司可以因為認列子公司收益而看起來本益比合理。
    用營業利益率排序自然把它排下去，完全不需要對盈餘品質下任何主觀判斷。

    # ponytail: 金融保險業不在 _ci 系列端點裡（要另 join _fh/_basi/_ins/_bd 四個
    #           schema 不同的變體）。缺值走既有的 0.5 中性路徑，別為了對稱多寫四份解析。
    """
    r17 = get("https://openapi.twse.com.tw/v1/opendata/t187ap17_L",
              cache_key="ratio.json", ttl=24 * 3600)
    q = pd.DataFrame([{
        "code": r["公司代號"].strip(),
        "gross_margin": num(r["毛利率(%)(營業毛利)/(營業收入)"]),
        "op_margin": num(r["營業利益率(%)(營業利益)/(營業收入)"]),
    } for r in r17]).set_index("code")

    r14 = get("https://openapi.twse.com.tw/v1/opendata/t187ap14_L",
              cache_key="eps.json", ttl=24 * 3600)
    rows = []
    for r in r14:
        op, non_op = num(r["營業利益"], 0.0), num(r["營業外收入及支出"], 0.0)
        pre = op + non_op
        rows.append({"code": r["公司代號"].strip(),
                     "eps": num(r["基本每股盈餘(元)"]),
                     # 業外佔比只當黃旗，不當排除 —— 台化的 65% 是持有台塑化的
                     # 權益法收益（經常性），端點又無法拆出一次性處分利益，硬排除會誤殺控股公司
                     "nonop_pct": abs(non_op) / abs(pre) * 100 if pre else math.nan})
    return q.join(pd.DataFrame(rows).set_index("code"))


def fetch_pledge():
    """董監持股質押比。質押代表大股東拿股票去借錢，股價跌破維持率會有斷頭賣壓，
    在台股是「大股東自己缺錢」最誠實的訊號，>30% 在下跌段的殺傷力是非線性的。

    格式是陷阱：端點只有 9 列，每列是一個百分比級距，公司資料整塊塞在『公司名稱』
    欄位裡（'3040      遠見  99.36\r\n2530      華建  92.69\r\n'），要正則拆。"""
    d = get("https://openapi.twse.com.tw/v1/opendata/t187ap09_L",
            cache_key="pledge.json", ttl=7 * 24 * 3600)
    txt = "\n".join(x.get("公司名稱", "") for x in d)
    pairs = re.findall(r"(\d{4})\s+(\S+?)\s+([\d.]+)", txt)
    assert len(pairs) > 500, f"質押比只解析出 {len(pairs)} 家，格式可能變了"
    return pd.Series({c: float(v) for c, _, v in pairs}, name="pledge")


def fetch_flags():
    """事件風險旗標。回傳 {代號: [說明,…]}，以及必須直接排除的集合。

    停資停券預告一支端點就同時涵蓋減資、現金增資、除權息 —— 停資停券必然
    先於事件發生，比等公開資訊觀測站公告更早。"""
    flags, drop = {}, set()

    def add(code, txt):
        code = str(code).strip()
        if code:
            flags.setdefault(code, []).append(txt)

    for r in get("https://openapi.twse.com.tw/v1/exchangeReport/BFI84U",
                 cache_key="events.json"):
        add(r["Code"], f"{r['Reason']}（停資券 {r['StartDate']}）")
    # 注意股連續次數是處置的前一階 —— 目前只排除已處置的，等於總是慢一步
    for r in get("https://openapi.twse.com.tw/v1/announcement/notetrans",
                 cache_key="notetrans.json"):
        add(r["Code"], f"注意股：{r.get('RecentlyMetAttentionSecuritiesCriteria', '')}")
    # 變更交易方法（含全額交割）與暫停交易：直接排除
    for ep, key, lab in [("exchangeReport/TWT85U", "Code", "變更交易方法"),
                         ("exchangeReport/TWTAWU", "Code", "暫停交易")]:
        for r in get(f"https://openapi.twse.com.tw/v1/{ep}",
                     cache_key=f"{lab}.json"):
            c = str(r.get(key, "")).strip()
            if c:
                drop.add(c)
                add(c, lab)
    return flags, drop


def fetch_chips(trade_date, n=5):
    """最近 N 個交易日的三大法人買賣超累計。
    單日買超雜訊太大（一筆鉅額轉倉就能翻轉排名），撐不起三成權重；五日累計才是趨勢。

    交易日曆直接問 T86 本人（stat=OK 就是交易日），不要拿行情商的 K 線索引反推 ——
    Yahoo 對 ^TWII 會回 close=null 的整根 K 棒，dropna 之後那天就憑空消失，
    五日窗會漏掉最新的交易日、改抓一天過期資料，前三名整個換人。"""
    frames, dates = [], []
    d = trade_date
    for _ in range(n + 20):          # 農曆年可連休六個交易日以上，窗口要夠寬
        if len(frames) >= n:
            break
        if d.weekday() < 5:          # 週末直接跳過，省下必然失敗的請求
            try:
                f, dd = _t86_one(d.strftime("%Y%m%d"))
                frames.append(f)
                dates.append(dd)
            except Exception as e:
                log(f"  T86 {d} 略過: {e}")
        d -= timedelta(days=1)
    if not frames:
        raise RuntimeError("T86 全部取得失敗")
    tot = pd.concat(frames).groupby(level=0).sum()
    # 買超「天數」跟買超「總額」是兩回事：外資一筆鉅額轉倉、ETF 成分股調整、
    # 除權息前的借券還券都會做出單日巨額買超但完全不代表看好。5 天買 5 天才是建倉。
    both = pd.concat(frames)
    tot["buy_days"] = both["inst_net"].gt(0).groupby(level=0).sum()
    tot["trust_days"] = both["trust_net"].gt(0).groupby(level=0).sum()
    d1 = frames[0][["inst_net", "trust_net"]].add_suffix("_1d")
    return tot.join(d1, how="outer"), dates


def fetch_whales():
    """集保戶股權分散表。級距 12-15 = 40萬股(400張)以上，級距 15 = 百萬股(千張)以上。
    已用 2330 驗證：級距17=合計=1..16 之和，占比 1-15 合計 ~100%。

    大戶比例的『水位』是結構性的(台積電常年 87%)，不是買進訊號 —— 真正的訊號是週變化。
    每週資料存成一檔，有上週檔就算 delta；第一次跑只有水位，看板會標明。"""
    raw = get("https://opendata.tdcc.com.tw/getOD.ashx?id=1-5",
              kind="text", cache_key="tdcc_latest.csv", ttl=24 * 3600)
    rows = list(csv.DictReader(io.StringIO(raw)))
    # 只缺一點點才是致命的：CSV 依代號排序，截斷點落在某檔中間會讓那檔的級距
    # 只加總一半，產生幾十個百分點的假變化並在大戶排名裡直接奪冠。缺很多反而安全
    #（have_delta 會因 NaN 過半自動降級）。全市場約 4000 檔 × 17 級距。
    assert len(rows) > 50000, f"TDCC 疑似截斷，只有 {len(rows)} 列"
    data_date = rows[0]["資料日期"].strip()
    snap = os.path.join(CACHE, f"tdcc_{data_date}.csv")
    if not os.path.exists(snap):
        tmp = snap + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(raw)
        os.replace(tmp, snap)

    def summarise(rs):
        agg = {}
        for r in rs:
            c, lvl = r["證券代號"].strip(), r["持股分級"].strip()
            p = num(r["占集保庫存數比例%"], 0.0)
            a = agg.setdefault(c, {"w400": 0.0, "w1000": 0.0, "retail": 0.0})
            if lvl in ("12", "13", "14", "15"):
                a["w400"] += p
            if lvl == "15":
                a["w1000"] += p
            if lvl in ("1", "2"):          # 1000股以下 + 1-5張 = 散戶
                a["retail"] += p
        return pd.DataFrame(agg).T

    cur = summarise(rows)

    prior = sorted(f for f in os.listdir(CACHE)
                   if re.fullmatch(r"tdcc_\d{8}\.csv", f) and f != f"tdcc_{data_date}.csv")
    gap = 99
    if prior:
        pd_date = prior[-1][5:13]
        gap = (date(int(data_date[:4]), int(data_date[4:6]), int(data_date[6:]))
               - date(int(pd_date[:4]), int(pd_date[4:6]), int(pd_date[6:]))).days
    if prior and gap <= 10:
        prev = summarise(list(csv.DictReader(open(os.path.join(CACHE, prior[-1]), encoding="utf-8-sig"))))
        cur["w400_chg"] = cur["w400"] - prev["w400"]
        cur["retail_chg"] = cur["retail"] - prev["retail"]
        mode = f"週變化 (對比 {prior[-1][5:13]})"
    else:
        # 隔三週再跑，prior[-1] 就是三週前的快照，變化量尺度差三倍卻仍以
        # 「週變化」之名拿滿權重。超過 10 天一律退回水位模式。
        cur["w400_chg"] = math.nan
        cur["retail_chg"] = math.nan
        mode = ("僅水位（尚無前週快照，下次執行起才有週變化）" if not prior
                else f"僅水位（最近的前一份快照已隔 {gap} 天，不足以視為週變化）")
    return cur, data_date, mode


def yahoo_history(symbol, rng="6mo", ttl=6 * 3600, asof=None):
    """回傳 DataFrame(close, volume)，失敗回 None。
    asof 用來把 Yahoo 的即時盤中資料切掉 —— 不切的話技術面會用到比報價日更新的價格。"""
    try:
        j = get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
                f"?range={rng}&interval=1d",
                cache_key=f"y_{symbol.replace('^','I')}.json", ttl=ttl)
        r = j["chart"]["result"][0]
        q = r["indicators"]["quote"][0]
        # adj = 還原股價。台股 8-9 月是除權息旺季，用原始收盤算均線與報酬率，
        # 除息當天的跳空缺口會被當成真的下跌。指標算 adj，畫面顯示仍用原始收盤。
        adj = (r["indicators"].get("adjclose") or [{}])[0].get("adjclose") or q["close"]
        d = pd.DataFrame({"close": q["close"], "adj": adj, "volume": q["volume"]},
                         index=pd.to_datetime(r["timestamp"], unit="s"))
        d = d.dropna(subset=["close", "adj"])
        if asof is not None:
            d = d[(d.index.tz_localize("UTC").tz_convert(TPE)).date <= asof]
        return d

    except Exception as e:
        log(f"  yahoo {symbol} 失敗: {e}")
        return None


POS = ["漲停", "大漲", "上漲", "走高", "攻頂", "衝高", "飆", "創新高", "新高", "看好",
       "調高", "上調", "利多", "旺季", "接單", "訂單", "拉貨", "突破", "成長", "獲利",
       "轉盈", "擴產", "併購", "簽約", "得標", "認證", "通過", "量產", "超預期", "優於預期",
       "強勁", "回溫", "受惠", "題材", "買超", "加碼", "目標價", "調升", "營收創", "大單",
       "報喜", "亮眼", "夯", "熱銷", "獨家", "合作", "投資", "點火", "領漲"]
NEG = ["跌停", "重挫", "大跌", "下跌", "走低", "摔", "挫", "崩", "破底", "新低", "下修",
       "調降", "看壞", "利空", "虧損", "衰退", "減產", "訴訟", "罰", "召回", "停工",
       "火災", "延宕", "掏空", "疑慮", "示警", "轉虧", "解約", "砍單", "庫存去化", "賣超",
       "評等調降", "下滑", "警示", "處置", "逃命", "低潮", "退燒", "失守", "殺", "逃",
       "認賠", "套牢", "利空出盡", "泡沫", "查稅", "停牌", "違約"]


def fetch_news(code, name, asof):
    """Google News RSS，只取 asof 之前 14 天內的標題，並按時效加權。
    用『"公司名" 代號』查以避免同名雜訊。

    # ponytail: 這是時效加權的關鍵字詞典，不是語意模型。要真情緒分析就把
    #           tone 換成 LLM 打分，其餘介面不用動。
    """
    q = urllib.parse.quote(f'"{name}" {code}')
    try:
        xml = get(f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant",
                  kind="text", cache_key=f"news_{code}.xml", ttl=6 * 3600)
    except Exception as e:
        log(f"  news {code} 失敗: {e}")
        return 0, 0.0, []

    # 新聞窗口收在交易日當晚，避免用『報價日之後』的消息回頭解釋當天的價
    cutoff_new = datetime.combine(asof, datetime.min.time(), TPE).timestamp() + 24 * 3600
    cutoff_old = cutoff_new - 14 * 86400

    kept, wp, wn = [], 0.0, 0.0
    for it in re.findall(r"<item>(.*?)</item>", xml, re.S):
        mt = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", it, re.S)
        md = re.search(r"<pubDate>(.*?)</pubDate>", it)
        if not (mt and md):
            continue
        try:
            ts = email.utils.parsedate_to_datetime(md.group(1)).timestamp()
        except Exception:
            continue
        if not (cutoff_old <= ts <= cutoff_new):
            continue
        title = html.unescape(re.sub(r"<.*?>", "", mt.group(1))).strip()
        age = (cutoff_new - ts) / 86400
        w = 1.0 if age <= 3 else (0.6 if age <= 7 else 0.35)   # 越新越算數
        p = sum(k in title for k in POS)
        n = sum(k in title for k in NEG)
        wp += w * min(p, 2)
        wn += w * min(n, 2)
        kept.append((ts, title, p - n))

    kept.sort(reverse=True)
    tone = (wp - wn) / max(wp + wn, 1.0)                       # -1..1
    return len(kept), tone, [t for _, t, _ in kept[:6]]


# ---------------------------------------------------------------- 面向評分

def score_fundamental(df):
    """基本面 0..1。缺值 = 0.5 中性。本益比 <=0 視為無意義(虧損)，不當便宜。"""
    pe = df["pe"].where(df["pe"] > 0)
    pb = df["pb"].where(df["pb"] > 0)
    ind = df["industry"]
    # 金融保險業的「月營收」含利息與投資收益，年增率是垃圾（產業內中位 30%、
    # 最高 4392%，非金融只有 15%）。產業內排名擋不住這件事 —— 在金融股「內部」
    # 排出來的名次本身沒有經濟意義，等於發一組隨機分數再送進總分。整項給中性。
    fin = ind.fillna("").str.contains("金融保險")
    acc = df["rev_yoy"] - df["rev_cum_yoy"]     # 單月年增 > 累計年增 = 動能在加速
    parts = pd.DataFrame({
        # 估值走產業內：金控 PB 0.8 vs 半導體 PB 4 是結構性的，同池比會讓金融股霸榜
        "pe":   pct_rank_by(pe, ind, higher_is_better=False),
        "pb":   pct_rank_by(pb, ind, higher_is_better=False),
        "yld":  pct_rank_by(df["yield"], ind),
        # 獲利率刻意「不」產業內排 —— 25% 的營業利益率在哪個產業都是 25%，
        # 跟本益比不同。把它也產業內排，等於讓低毛利產業在圈內互比後全體及格，
        # 正好抵銷了這一項存在的理由。營益率是本益比失真的解藥：本業毛利 7% 的
        # 公司靠認列子公司收益也能讓本益比看起來合理，用營益率排它自然掉下去。
        "opm":  pct_rank(df["op_margin"]),
        "gm":   pct_rank(df["gross_margin"]),
        "yoy":  pct_rank(df["rev_yoy"]).where(~fin, 0.5),
        "cum":  pct_rank_by(df["rev_cum_yoy"], ind).where(~fin, 0.5),
        "acc":  pct_rank(acc).where(~fin, 0.5),
    })
    w = {"pe": .14, "pb": .09, "yld": .09, "opm": .20, "gm": .08,
         "yoy": .20, "cum": .08, "acc": .12}
    assert abs(sum(w.values()) - 1) < 1e-9
    raw = sum(parts[k] * v for k, v in w.items())

    # 業外佔比高的時候，這些以本業為基礎的指標描述到的公司比例就下降了 ——
    # 台化 65%、南亞 68% 的稅前來自權益法認列子公司。這不是罪（是經常性收益，
    # 硬排除會誤殺集團控股公司），但基本面分數的「可信度」確實比較低。
    # 所以是往中性收縮而不是扣分：資訊量少就少講話，不是判它有罪。
    conf = 1 - ((df["nonop_pct"].fillna(0) - 40) / 60).clip(0, 1) * .5
    return 0.5 + (raw - 0.5) * conf


def score_chips(df):
    """籌碼面 0..1。法人買超必須除以成交量 —— 用絕對股數排序，台積電永遠第一。"""
    # 分母用「當月成交量」而非當日：五日買超配當日量，會讓爆量股的訊號被自己的量吃掉。
    # 月天數對所有股票一致，橫斷面排序不受影響。
    turn = df["vol_month"].where(df["vol_month"] > 0,
                                 df["volume"] * 20).replace(0, math.nan)
    inst = df["inst_net"] / turn
    trust = df["trust_net"] / turn
    # 三大法人合計 = 外陸資 + 外資自營 + 投信 + 自營（已用 T86 逐欄驗證相加相等）。
    # 再把 foreign 當獨立項加權，等於外資被算了兩次、實際載荷是名目的三倍。
    # 只留「合計」與「投信」：投信是台股最有訊號的短線買盤，值得單獨加權。
    # buy_days 是持續性：買 5 天跟一天買完 5 天的量，訊號品質完全不同。
    # margin_use 低 = 散戶槓桿少 = 籌碼安定，比較不怕融資追繳的賣壓打亂走勢。
    return (pct_rank(inst) * .40 + pct_rank(trust) * .25
            + pct_rank(df["buy_days"]) * .12 + pct_rank(df["trust_days"]) * .08
            + pct_rank(df["margin_use"], higher_is_better=False) * .15)


def risk_factor(df):
    """風險折扣 0.65~1.0，乘在總分上。

    刻意不做硬排除：慧洋-KY 是正常的航運公司，質押比高是要警覺不是要處決；
    真正該直接排除的（變更交易方法、暫停交易、處置）已經在母體階段擋掉了。"""
    # 質押 30% 以下不扣，之後線性到 70% 扣滿 —— 斷頭賣壓在跌破維持率後是非線性的
    pledge = ((df["pledge"].fillna(0) - 30) / 40).clip(0, 1) * .25
    notice = df["flags"].str.contains("注意股").astype(float) * .10
    return (1 - pledge - notice).clip(.65, 1.0)


def score_whale(df, have_delta):
    """大戶 0..1。有週變化就用變化(大戶增持/散戶減持)，沒有就退回水位並標明。"""
    if have_delta:
        # 剪刀差：大戶增持「且」散戶減持才是最強訊號。兩項各自排名會讓
        # 「大戶增 0.5、散戶也增 0.5」（一起追價）跟「大戶增 0.5、散戶減 0.5」
        # （籌碼從散戶轉到大戶）拿到一樣的分數。
        return pct_rank(df["w400_chg"] - df["retail_chg"])
    return pct_rank(df["w400"] - df["retail"])


def technicals(hist):
    """從日K 算出技術指標 dict，資料不足回 None。"""
    if hist is None or len(hist) < 65 or "adj" not in hist:
        return None
    c, v = hist["adj"], hist["volume"]
    ma5, ma20, ma60 = c.rolling(5).mean(), c.rolling(20).mean(), c.rolling(60).mean()
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, math.nan))
    last = -1
    d = {
        "px": float(hist["close"].iloc[last]),      # 顯示用原始收盤
        "adj": float(c.iloc[last]),                 # 指標用還原價
        "ma5": float(ma5.iloc[last]), "ma20": float(ma20.iloc[last]),
        "ma60": float(ma60.iloc[last]),
        "rsi": float(rsi.iloc[last]) if pd.notna(rsi.iloc[last]) else 50.0,
        "ret20": float(c.iloc[last] / c.iloc[-21] - 1) * 100,
        "ret60": float(c.iloc[last] / c.iloc[-61] - 1) * 100,
        "vol_ratio": float(v.iloc[-5:].mean() / max(v.iloc[-60:].mean(), 1)),
        "from_high": float(c.iloc[last] / c.iloc[-120:].max() - 1) * 100,
        "high_days": int(min(len(c), 120)),
    }
    # NaN 會穿過 min/max 一路傳到 score_tech，60 檔跑完才以「分數越界」爆掉，
    # 而訊息指向 score 不是來源。寧可早一步退回中性。
    return d if all(math.isfinite(v) for v in d.values()) else None


def score_news(n, k=6):
    """消息面 0..1，絕對尺度。

    刻意不做橫斷面百分位：其他五個面向的母體是全市場 428 檔，這裡卻只有候選集，
    在不同母體裡排名再加權相加，名目 10% 的權重會吃掉遠超比例的離散度。
    另外對樣本數收縮 —— 1 則新聞剛好命中一個正面詞，不該和 58 則一面倒同分。
    無新聞或關鍵字互相抵銷 = 0.5 中性，與其他面向的缺值政策一致。"""
    shrunk = n["tone"] * n["count"] / (n["count"] + k)
    return 0.5 + 0.5 * shrunk


def score_tech(t):
    """技術面 0..1。多頭排列 + 未過熱 + 動能 + 量增。

    全案只有這裡是絕對門檻而非百分位，所以刻意寫成連續斜坡：純布林的
    「收盤 > 月線」會讓差 0.08% 和差 10% 拿到完全一樣的分數，把雜訊放大成排名差距。
    動能與量能是駝峰不是單調遞增 —— 20 日漲 35%、量能爆 3 倍是拋物線末端，
    不是比漲 15%、量增 1.5 倍更好的買點。"""
    if not t:
        return 0.5

    def ramp(x, lo, hi):
        return min(max((x - lo) / (hi - lo), 0.0), 1.0)

    s = 0.0
    s += .18 * ramp(t["adj"] / t["ma20"] - 1, -.03, .05)     # 站上月線的「程度」
    s += .15 * ramp(t["ma5"] / t["ma20"] - 1, -.02, .04)
    s += .15 * ramp(t["ma20"] / t["ma60"] - 1, -.02, .06)

    r = t["rsi"]                                             # 45~70 甜蜜區，>85 歸零
    s += .18 * (1.0 if 45 <= r <= 70 else
                ramp(r, 30, 45) if r < 45 else 1 - ramp(r, 70, 85))

    m = t["ret20"]                                           # 動能駝峰
    s += .16 * (ramp(m, 0, 8) if m < 8 else
                1.0 if m <= 20 else 1 - .7 * ramp(m, 20, 40))

    v = t["vol_ratio"]                                       # 量能駝峰：爆量常是出貨
    s += .10 * (ramp(v, .9, 1.5) if v <= 1.8 else 1 - .5 * ramp(v, 1.8, 3.5))

    f = t["from_high"]                                       # 靠近高點好，正好在高點扣一點
    s += .08 * (ramp(f, -20, -3) if f <= -3 else 1 - .4 * ramp(f, -3, 0))
    return min(s, 1.0)


def market_mood(df, asof, twii):
    """市場氛圍 = 盤面級純量，對每一檔都一樣，所以不進個股排序，只當儀表板 + 建議加減碼。"""
    out = {}
    if twii is not None and len(twii) > 60:
        c = twii["close"]
        out["index"] = float(c.iloc[-1])
        out["idx_ma20"] = float(c.rolling(20).mean().iloc[-1])
        out["idx_ma60"] = float(c.rolling(60).mean().iloc[-1])
        out["idx_ret20"] = float(c.iloc[-1] / c.iloc[-21] - 1) * 100
    # 市場寬度：全市場站上月均價(≈20日均線)的比例 — 比單日漲跌家數穩定得多
    ok = df["ref_price"] > 0
    out["breadth"] = float((df.loc[ok, "close"] > df.loc[ok, "ref_price"]).mean() * 100)

    try:
        m = get("https://openapi.twse.com.tw/v1/exchangeReport/MI_MARGN", cache_key="margin.json")
        today = sum(num(r.get("融資今日餘額"), 0) for r in m)
        prev = sum(num(r.get("融資前日餘額"), 0) for r in m)
        out["margin_chg"] = (today / prev - 1) * 100 if prev else math.nan
    except Exception as e:
        log(f"  margin 失敗: {e}")
        out["margin_chg"] = math.nan

    # 只對「實際拿到的成分」正規化，缺哪一項就把它的權重從分母移掉
    score, got = 0.0, 0.0
    if out.get("index"):
        score += 25 if out["index"] > out["idx_ma20"] else 0
        score += 20 if out["idx_ma20"] > out["idx_ma60"] else 0
        score += 15 * min(max((out["idx_ret20"] + 3) / 9, 0), 1)
        got += 60
    score += 40 * min(max((out["breadth"] - 30) / 45, 0), 1)
    got += 40
    out["degraded"] = got < 100
    out["score"] = round(score / got * 100, 1)
    score = out["score"]
    out["label"] = ("多頭偏熱" if score >= 78 else "偏多" if score >= 58
                    else "中性震盪" if score >= 42 else "偏空" if score >= 25 else "空頭")
    out["advice"] = ("氛圍偏熱，分批進場、別追高" if score >= 78
                     else "順勢可為，建議標準部位" if score >= 58
                     else "多空拉鋸，建議減半部位、嚴設停損" if score >= 42
                     else "逆風，建議觀望或極小部位試單")
    return out


# ---------------------------------------------------------------- 主流程

def prune_cache(days=30, dry=False):
    """清掉過期的逐檔快取，回傳被清（或將被清）的檔名。

    tdcc_*.csv 絕對不能碰 —— 那是大戶週變化的比較基準，刪掉等於把累積的歷史丟了，
    而且不會有任何錯誤訊息，只會讓大戶面向默默降級回「僅水位」。"""
    cutoff = time.time() - days * 86400
    doomed = [f for f in os.listdir(CACHE)
              if not f.startswith("tdcc_")
              and os.path.getmtime(os.path.join(CACHE, f)) < cutoff]
    if not dry:
        for f in doomed:
            os.remove(os.path.join(CACHE, f))
        if doomed:
            log(f"      清掉 {len(doomed)} 個逾 {days} 天的快取檔")
    return doomed


def run():
    log("[1/6] 抓全市場報價 / 處置股 / 月均價 …")
    df, trade_date, n_punish = fetch_universe()
    today = datetime.now(TPE).date()
    stale = (today - trade_date).days
    log(f"      交易日 {trade_date}，符合條件 {len(df)} 檔（已排除 {n_punish} 檔處置股）")
    # 收盤前跑，證交所還沒出今天的資料，整份選股會靜靜地建立在昨天（或上週五）之上。
    # 交易日有印出來，但沒人會每次去核對，所以直接喊。
    if stale > 3:
        log(f"      ⚠ 報價已是 {stale} 天前 —— 連假或證交所尚未更新，本次結果不是最新盤")
    elif stale >= 1 and today.weekday() < 5:
        log(f"      ⚠ 今天是交易日但報價停在 {trade_date}"
            f"（證交所約 15:00 出行情、16:00 出法人）—— 收盤後再跑才是今天的盤")

    log("[2/6] 抓基本面（本益比/淨值比/殖利率、月營收）…")
    df = (df.join(fetch_value(trade_date)).join(fetch_revenue())
            .join(fetch_quality()).join(fetch_pledge()))

    log("[3/6] 抓籌碼面（三大法人，近 5 個交易日累計）…")
    twii = yahoo_history("%5ETWII", asof=trade_date)
    chips, t86_dates = fetch_chips(trade_date)
    assert t86_dates[0] == trade_date.strftime("%Y%m%d"), f"T86 日期不符: {t86_dates[0]}"
    assert len(t86_dates) == 5, f"五日籌碼只湊到 {len(t86_dates)} 天"
    df = df.join(chips)
    log(f"      累計 {len(t86_dates)} 日：{t86_dates[-1]} ~ {t86_dates[0]}")

    log("[4/6] 抓大戶（集保股權分散）…")
    whales, tdcc_date, whale_mode = fetch_whales()
    df = df.join(whales)
    log(f"      集保資料日 {tdcc_date}｜{whale_mode}")

    have_delta = df["w400_chg"].notna().sum() > len(df) * 0.5
    W = weights(have_delta)

    df["s_fund"] = score_fundamental(df)
    df["s_chips"] = score_chips(df)
    df["s_whale"] = score_whale(df, have_delta)
    # 第一階段用「收盤 vs 月均價（退化時改用上月加權均價）」當技術面代理，先篩出候選再花時間抓日K
    df["s_tech"] = pct_rank(df["close"] / df["ref_price"])
    df["s_news"] = 0.5
    df["risk"] = risk_factor(df)
    df["score"] = sum(df[f"s_{k}"] * v for k, v in W.items()) * df["risk"]
    n_risk = int((df["risk"] < 1).sum())
    log(f"      風險折扣：{n_risk} 檔（質押比>30% 或注意股）")

    cand = df.nlargest(SHORTLIST, "score").copy()
    log(f"[5/6] 深挖前 {len(cand)} 檔：日K技術面 + 新聞輿論 …")

    tech, news, hists = {}, {}, {}
    for i, (code, row) in enumerate(cand.iterrows(), 1):
        before = NET_CALLS
        hists[code] = yahoo_history(f"{code}.TW", asof=trade_date)
        t = technicals(hists[code])
        n_cnt, tone, heads = fetch_news(code, row["name"], trade_date)
        tech[code] = t
        news[code] = {"count": n_cnt, "tone": tone, "heads": heads}
        log(f"      {i:2d}/{len(cand)} {code} {row['name']} "
            f"tech={'ok' if t else 'n/a'} news={n_cnt} tone={tone:+.2f}")
        if NET_CALLS > before:      # 只在真的打了網路時才禮讓，暖快取不空等
            time.sleep(0.35)

    cand["s_tech"] = [score_tech(tech[c]) for c in cand.index]
    cand["s_news"] = [score_news(news[c]) for c in cand.index]
    cand["score"] = sum(cand[f"s_{k}"] * v for k, v in W.items()) * cand["risk"]
    cand = cand.sort_values("score", ascending=False)
    # 前三名是拿來當投資組合的，同產業三檔不叫分散 —— 產業風險會完全相關。
    # 每個產業只取分數最高的一檔，其餘照樣留在全表裡。
    seen, picks = set(), []
    for c in cand.index:
        ind = cand.at[c, "industry"]
        key = ind if pd.notna(ind) else c
        if key in seen:
            continue
        seen.add(key)
        picks.append(c)
        if len(picks) == 3:
            break
    cand["pick"] = cand.index.isin(picks)
    stage1 = {c: i for i, c in enumerate(df.nlargest(SHORTLIST, "score").index, 1)}
    log(f"      前三名在第一階段代理排序的名次：" +
        "、".join(f"{c}→#{stage1[c]}" for c in cand.head(3).index) +
        f"（漏斗深度 {SHORTLIST}）")

    log("[6/6] 市場氛圍 + 產生看板 …")
    mood = market_mood(df, trade_date, twii)

    for c in cand.index:
        assert 0 <= cand.at[c, "score"] <= 1, f"{c} 分數越界"

    # 先把整頁算完再開檔：open(...,"w") 會立刻把舊檔截成 0 bytes，
    # 若 render() 在那之後拋例外，這次沒更新就升級成「上一份看板也沒了」。
    page = render(cand, tech, news, mood, trade_date, tdcc_date,
                  whale_mode, len(df), W, hists, have_delta)
    out = os.path.join(HERE, "dashboard.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(page)
    hist = os.path.join(HERE, "picks.csv")
    new = not os.path.exists(hist)
    with open(hist, "a", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["交易日", "名次", "代號", "名稱", "收盤", "總分",
                        "技術", "基本", "籌碼", "大戶", "輿論", "旗標"])
        for i, (c, r) in enumerate(cand[cand["pick"]].iterrows(), 1):
            w.writerow([trade_date, i, c, r["name"], f"{r['close']:.2f}",
                        f"{r['score']*100:.1f}"]
                       + [f"{r[k]*100:.0f}" for k, _ in FACETS] + [r.get("flags", "")])

    prune_cache()
    log(f"\n完成 → {out}")
    print("\n=== 前三名 ===")
    for i, (code, r) in enumerate(cand.head(3).iterrows(), 1):
        print(f"{i}. {code} {r['name']}  總分 {r['score']*100:.1f}  "
              f"技{r.s_tech*100:.0f} 基{r.s_fund*100:.0f} 籌{r.s_chips*100:.0f} "
              f"戶{r.s_whale*100:.0f} 聞{r.s_news*100:.0f}")
    return cand, tech, news, mood, hists


# ---------------------------------------------------------------- 看板

FACETS = [("s_tech", "技術面"), ("s_fund", "基本面"), ("s_chips", "籌碼面"),
          ("s_whale", "大戶"), ("s_news", "消息輿論")]


def sparkline(hist, uid="0", days=60, w=132, h=34):
    """近 N 日收盤走勢縮圖。台股慣例紅漲綠跌，末點加粗。"""
    if hist is None or len(hist) < 5:
        return ""
    c = hist["close"].iloc[-days:].tolist()
    lo, hi = min(c), max(c)
    rng = (hi - lo) or 1
    step = (w - 4) / max(len(c) - 1, 1)
    pts = [(2 + i * step, h - 3 - (v - lo) / rng * (h - 6)) for i, v in enumerate(c)]
    d = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = d + f" L{pts[-1][0]:.1f},{h} L{pts[0][0]:.1f},{h} Z"
    up = c[-1] >= c[0]
    col = "var(--rise)" if up else "var(--fall)"
    uid = f"sg{uid}"   # 用股票代號當 id，三張卡的漸層才不會互相搶
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'aria-hidden="true" preserveAspectRatio="none">'
            f'<defs><linearGradient id="{uid}" x1="0" y1="0" x2="0" y2="1">'
            f'<stop offset="0" stop-color="{col}" stop-opacity=".22"/>'
            f'<stop offset="1" stop-color="{col}" stop-opacity="0"/></linearGradient></defs>'
            f'<path d="{area}" fill="url(#{uid})"/>'
            f'<path d="{d}" fill="none" stroke="{col}" stroke-width="1.6" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{pts[-1][0]:.1f}" cy="{pts[-1][1]:.1f}" r="2.6" fill="{col}"/></svg>')


def sign_span(v, spec="{:+.1f}", suffix=""):
    """紅漲綠跌 —— 台股慣例跟歐美相反，看板必須跟著台股走。"""
    if pd.isna(v):
        return '<span class="flat">—</span>'
    k = "rise" if v > 0 else ("fall" if v < 0 else "flat")
    return f'<span class="{k}">{spec.format(v)}{suffix}</span>'


def grade(v):
    """把 0..1 分數轉成一眼可辨的強弱等級，數字之外再給一個形。"""
    return "s3" if v >= .75 else "s2" if v >= .55 else "s1" if v >= .35 else "s0"


def reasons(code, r, t, n, have_delta):
    """把分數翻成人話。只講有數字撐腰的。

    守衛的欄位和格式化的欄位必須是同一個 —— 用 pe 當守衛卻一起印 pb/yield，
    缺值時看板上會直接出現 nan。g() 就是為了讓這件事不可能發生。"""
    def g(k, spec="{:.1f}", div=1.0):
        v = r.get(k)
        return spec.format(v / div) if pd.notna(v) else "—"

    out = []
    if t:
        if t["adj"] > t["ma20"] > t["ma60"]:
            out.append(f"多頭排列（還原價）：收 {t['adj']:.1f}｜月線 {t['ma20']:.1f}｜季線 {t['ma60']:.1f}")
        elif t["adj"] > t["ma20"]:
            out.append(f"站上月線 {t['ma20']:.1f}（季線 {t['ma60']:.1f}）")
        else:
            out.append(f"跌破月線 {t['ma20']:.1f}")
        out.append(f"RSI {t['rsi']:.0f}" + ("　過熱" if t["rsi"] > 80 else
                                            "　甜蜜區" if 45 <= t["rsi"] <= 70 else ""))
        out.append(f"20 日 {t['ret20']:+.1f}%、60 日 {t['ret60']:+.1f}%，"
                   f"距 {t['high_days']} 日高點 {t['from_high']:+.1f}%")
        if t["vol_ratio"] > 1.3:
            out.append(f"近 5 日均量為季均量 {t['vol_ratio']:.1f} 倍，量增價漲")
    if pd.notna(r.get("op_margin")):
        out.append(f"營業利益率 {g('op_margin')}%｜毛利率 {g('gross_margin')}%"
                   + (f"（業外佔稅前 {r['nonop_pct']:.0f}%）"
                      if pd.notna(r.get("nonop_pct")) and r["nonop_pct"] > 40 else ""))
    if pd.notna(r.get("buy_days")):
        out.append(f"法人五日買超 {int(r['buy_days'])}/5 天"
                   + (f"、投信 {int(r['trust_days'])}/5 天" if pd.notna(r.get("trust_days")) else ""))
    if pd.notna(r.get("margin_use")):
        out.append(f"融資使用率 {g('margin_use', '{:.2f}')}%"
                   + ("（籌碼安定）" if r["margin_use"] < 2 else ""))
    if pd.notna(r.get("rev_yoy")):
        out.append(f"月營收年增 {r['rev_yoy']:+.1f}%"
                   + (f"、累計年增 {r['rev_cum_yoy']:+.1f}%" if pd.notna(r.get("rev_cum_yoy")) else ""))
    if pd.notna(r.get("pe")) and r["pe"] > 0:
        out.append(f"本益比 {g('pe')}｜淨值比 {g('pb', '{:.2f}')}｜"
                   f"殖利率 {g('yield', '{:.2f}')}%（與同業比）")
    if pd.notna(r.get("inst_net")):
        out.append(f"三大法人近 5 日{'買超' if r['inst_net'] >= 0 else '賣超'} "
                   f"{abs(r['inst_net'])/1000:,.0f} 張，"
                   f"當日 {g('inst_net_1d', '{:+,.0f}', 1000)} 張")
    if pd.notna(r.get("trust_net")) and r["trust_net"] > 0:
        out.append(f"投信近 5 日買超 {r['trust_net']/1000:,.0f} 張")
    if have_delta and pd.notna(r.get("w400_chg")):
        out.append(f"400 張大戶持股週變化 {r['w400_chg']:+.2f} 個百分點，"
                   f"散戶 {g('retail_chg', '{:+.2f}')}")
    elif pd.notna(r.get("w400")):
        out.append(f"400 張大戶持股 {r['w400']:.1f}%，散戶 {r['retail']:.1f}%")
    if n["count"]:
        out.append(f"14 日內新聞 {n['count']} 則，時效加權語調 {n['tone']:+.2f}")
    return out


def tokens_of(tpl):
    return re.findall(r"⟦(\w+)⟧", tpl)


def render(cand, tech, news, mood, trade_date, tdcc_date, whale_mode,
           n_universe, W, hists, have_delta):
    # have_delta 由 run() 用全母體算一次傳進來。原本這裡用候選集重算，
    # 母體不同可能得到相反結果 —— 權重照全母體給，文案卻照候選集寫。

    cards = ""
    for rank, (code, r) in enumerate(cand[cand["pick"]].iterrows(), 1):
        t, n, hh = tech.get(code), news[code], hists.get(code)
        chg = math.nan
        if hh is not None and len(hh) > 1:
            chg = (hh["close"].iloc[-1] / hh["close"].iloc[-2] - 1) * 100
        rs = "".join(f"<li>{html.escape(x)}</li>" for x in reasons(code, r, t, n, have_delta))
        heads = "".join(f"<li>{html.escape(h)}</li>" for h in n["heads"]) \
            or "<li>近 14 日無相關新聞</li>"
        bars = "".join(
            f'<div class="fr"><span class="fl">{lb}</span>'
            f'<span class="fb {grade(r[k])}"><i style="width:{r[k]*100:.0f}%"></i></span>'
            f'<span class="fv">{r[k]*100:.0f}</span></div>' for k, lb in FACETS)
        risks = []
        if pd.notna(r.get("pledge")) and r["pledge"] > 5:
            risks.append(f"董監質押 {r['pledge']:.1f}%")
        if r.get("flags"):
            risks.append(str(r["flags"]))
        warn = (f'<div class="warn">⚠ {html.escape("；".join(risks))}</div>'
                if risks else "")
        ind = r.get("industry")
        cards += f"""
<article class="card">
  <header>
    <div class="rk">第 {rank} 名</div>
    <h3><b>{html.escape(r['name'])}</b><span>{html.escape(code)}</span></h3>
    <div class="ind">{html.escape(str(ind)) if pd.notna(ind) else ''}</div>
  </header>
  <div class="quote">
    <div class="qpx"><b>{r['close']:,.2f}</b>{sign_span(chg, '{:+.2f}', '%')}</div>
    {sparkline(hh, html.escape(code))}
  </div>
  <div class="score"><b>{r['score']*100:.1f}</b><span>綜合分數<em>／100</em></span></div>
  <div class="facets">{bars}</div>
  {warn}
  <h4>入選理由</h4><ul class="why">{rs}</ul>
  <h4>近 14 日標題</h4><ul class="heads">{heads}</ul>
</article>"""

    def cell(v, spec="{:.1f}"):
        return spec.format(v) if pd.notna(v) else "—"

    rows = ""
    for rank, (code, r) in enumerate(cand.iterrows(), 1):
        t = tech.get(code)
        fs = "".join(f'<td class="n"><span class="pill {grade(r[k])}">'
                     f'{r[k]*100:.0f}</span></td>' for k, _ in FACETS)
        rows += (
            f'<tr{" class=top3" if r["pick"] else ""}>'
            f'<td class="n mut">{rank}</td><td class="mono">{code}</td>'
            f'<td class="nm">{html.escape(r["name"])}</td>'
            f'<td class="n mono">{r["close"]:,.2f}</td>'
            f'<td class="n mono tot">{r["score"]*100:.1f}</td>{fs}'
            f'<td class="n mono">{cell(r["pe"])}</td>'
            f'<td class="n mono">{sign_span(r["rev_yoy"], "{:+.1f}")}</td>'
            f'<td class="n mono">'
            f'{sign_span(r["inst_net"]/1000 if pd.notna(r["inst_net"]) else math.nan, "{:+,.0f}")}</td>'
            f'<td class="n mono">{cell(t["rsi"] if t else math.nan, "{:.0f}")}</td></tr>')

    idx_chg = mood.get("idx_ret20")
    tokens = {
        "TRADE": str(trade_date), "TDCC": html.escape(str(tdcc_date)),
        "WHALE_MODE": html.escape(whale_mode),
        "NU": str(n_universe), "NC": str(len(cand)),
        "CARDS": cards, "ROWS": rows,
        "MSCORE": f"{mood['score']:.0f}", "MLABEL": mood["label"],
        "MADVICE": mood["advice"],
        "IDX": f"{mood['index']:,.0f}" if mood.get("index") else "—",
        "IRET": sign_span(idx_chg, "{:+.2f}", "%") if idx_chg is not None else "—",
        "IMA": ("站上月線" if mood.get("index", 0) > mood.get("idx_ma20", 1e9) else "月線之下"),
        "BREADTH": f"{mood['breadth']:.0f}%",
        "MARGIN": sign_span(mood.get("margin_chg", math.nan), "{:+.2f}", "%"),
        "W": "　".join(f"{lb} {W[k[2:]]*100:.0f}%" for k, lb in FACETS),
        "GEN": datetime.now(TPE).strftime("%Y-%m-%d %H:%M"),
    }
    # 單次掃描：代入的內容不會被再掃一次，缺 key 時 KeyError 直接指名哪個欄位。
    return re.sub(r"⟦(\w+)⟧", lambda m: tokens[m.group(1)], TEMPLATE)


TEMPLATE = r"""<title>台股六面向選股台</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Noto+Serif+TC:wght@500;700&family=Noto+Sans+TC:wght@400;500;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
/* 台股紅漲綠跌，與歐美相反 —— 整份配色從這個事實長出來，
   中性色帶一點暖紅偏移，才不會像預設灰。 */
:root{
  --ground:#f7f4f2; --panel:#fffdfc; --ink:#1c1614; --mut:#847872; --line:#e7dfda;
  --rise:#cf2233; --fall:#0f9a68; --accent:#cf2233; --accent-soft:#f5e3e2;
  --s3:#cf2233; --s2:#d98a2b; --s1:#9a8f88; --s0:#c6bdb7;
  --shadow:0 1px 2px rgba(40,20,15,.06),0 8px 24px -16px rgba(40,20,15,.22);
  --f-disp:"Noto Serif TC",Georgia,"Songti TC",serif;
  --f-body:"Noto Sans TC",-apple-system,"PingFang TC","Microsoft JhengHei",sans-serif;
  --f-mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --ground:#14100f; --panel:#1d1817; --ink:#f0e9e5; --mut:#9c8f88; --line:#2e2624;
  --rise:#ff5a63; --fall:#2fcf94; --accent:#ff5a63; --accent-soft:#3a2020;
  --s3:#ff5a63; --s2:#e2a44a; --s1:#8b7f78; --s0:#4a413d;
  --shadow:0 1px 2px rgba(0,0,0,.5),0 8px 24px -16px rgba(0,0,0,.8);
}}
:root[data-theme="dark"]{
  --ground:#14100f; --panel:#1d1817; --ink:#f0e9e5; --mut:#9c8f88; --line:#2e2624;
  --rise:#ff5a63; --fall:#2fcf94; --accent:#ff5a63; --accent-soft:#3a2020;
  --s3:#ff5a63; --s2:#e2a44a; --s1:#8b7f78; --s0:#4a413d;
  --shadow:0 1px 2px rgba(0,0,0,.5),0 8px 24px -16px rgba(0,0,0,.8);
}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--ink);
  font:400 15px/1.7 var(--f-body);-webkit-font-smoothing:antialiased}
.wrap{max-width:1240px;margin:0 auto;padding:40px 22px 80px;
  display:flex;flex-direction:column;gap:26px}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:3px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}

/* 報頭 */
.mast{display:flex;flex-wrap:wrap;align-items:flex-end;gap:14px 20px;
  border-bottom:2px solid var(--ink);padding-bottom:14px}
h1{font:700 30px/1.15 var(--f-disp);margin:0;letter-spacing:.01em;text-wrap:balance}
.mast .meta{font:400 12.5px/1.6 var(--f-mono);color:var(--mut);margin-left:auto;text-align:right}

/* 大盤紙帶 */
.tape{background:var(--panel);border:1px solid var(--line);border-radius:4px;
  box-shadow:var(--shadow);padding:18px 20px;display:flex;flex-wrap:wrap;gap:24px 34px;align-items:center}
.tape .lead{flex:1 1 260px;min-width:240px}
.tape .eyebrow{font:500 11px/1 var(--f-body);letter-spacing:.16em;color:var(--mut);
  text-transform:uppercase;margin-bottom:9px}
.tape .lead b{font:700 27px/1 var(--f-mono);display:inline-block}
.tape .lead .tag{font:500 12.5px/1 var(--f-body);margin-left:9px;padding:4px 9px;
  border-radius:3px;background:var(--accent-soft);color:var(--accent)}
.gauge{height:5px;background:var(--line);border-radius:99px;margin-top:12px;overflow:hidden}
.gauge i{display:block;height:100%;background:var(--accent);border-radius:99px}
.tstat{display:flex;flex-wrap:wrap;gap:22px 30px}
.tstat div b{display:block;font:500 18px/1.25 var(--f-mono)}
.tstat div span{font:400 11.5px/1 var(--f-body);color:var(--mut);letter-spacing:.03em}
.advice{flex:1 1 100%;font-size:13.5px;color:var(--mut);border-top:1px solid var(--line);
  padding-top:12px;margin-top:2px}
.advice b{color:var(--ink);font-weight:500}

/* 前三名 */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(322px,1fr));gap:18px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:4px;
  box-shadow:var(--shadow);padding:22px;display:flex;flex-direction:column;gap:15px}
.card:first-child{border-top:3px solid var(--accent)}
.card header{display:grid;gap:3px}
.rk{font:500 11px/1 var(--f-body);letter-spacing:.18em;color:var(--accent)}
.card h3{margin:2px 0 0;font:400 15px/1.2 var(--f-mono);color:var(--mut);
  display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.card h3 b{font:700 25px/1.15 var(--f-disp);color:var(--ink);letter-spacing:.02em}
.ind{font-size:12px;color:var(--mut)}
.quote{display:flex;align-items:flex-end;justify-content:space-between;gap:14px}
.qpx b{font:500 23px/1 var(--f-mono);font-variant-numeric:tabular-nums}
.qpx span{font:500 13px/1 var(--f-mono);margin-left:8px}
.spark{flex:none;opacity:.95}
.rise{color:var(--rise)} .fall{color:var(--fall)} .flat{color:var(--mut)}
.score{display:flex;align-items:baseline;gap:10px;border-top:1px solid var(--line);
  border-bottom:1px solid var(--line);padding:13px 0}
.score b{font:600 40px/1 var(--f-mono);color:var(--accent);font-variant-numeric:tabular-nums}
.score span{font-size:12px;color:var(--mut)} .score em{font-style:normal;opacity:.7}
.facets{display:grid;gap:7px}
.fr{display:flex;align-items:center;gap:10px;font-size:12.5px}
.fl{width:56px;flex:none;color:var(--mut)}
.fb{flex:1;height:5px;background:var(--line);border-radius:99px;overflow:hidden}
.fb i{display:block;height:100%;border-radius:99px;background:var(--s1)}
.fb.s3 i{background:var(--s3)} .fb.s2 i{background:var(--s2)} .fb.s0 i{background:var(--s0)}
.fv{width:24px;text-align:right;font:500 12px/1 var(--f-mono);color:var(--mut);
  font-variant-numeric:tabular-nums}
.card h4{font:500 11px/1 var(--f-body);letter-spacing:.16em;color:var(--mut);
  text-transform:uppercase;margin:6px 0 -4px}
.card .warn{font-size:12.5px;background:var(--accent-soft);color:var(--accent);
  padding:8px 11px;border-radius:3px;border-left:2px solid var(--accent)}
.why,.heads{margin:0;padding:0;list-style:none;display:grid;gap:6px}
.why li{font-size:13px;padding-left:13px;position:relative}
.why li::before{content:"";position:absolute;left:0;top:.62em;width:5px;height:1.5px;
  background:var(--accent)}
.heads li{font-size:12.5px;line-height:1.55;color:var(--mut);padding-left:13px;
  position:relative;text-wrap:pretty}
.heads li::before{content:"·";position:absolute;left:2px;color:var(--s1)}

/* 全表 */
h2{font:700 17px/1.3 var(--f-disp);margin:0 0 -12px}
.tbl{background:var(--panel);border:1px solid var(--line);border-radius:4px;
  box-shadow:var(--shadow);overflow-x:auto}
table{border-collapse:collapse;width:100%;min-width:1020px;font-size:13px}
th,td{padding:9px 11px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}
thead th{font:500 11px/1.3 var(--f-body);letter-spacing:.06em;color:var(--mut);
  position:sticky;top:0;background:var(--panel);border-bottom:1.5px solid var(--ink);z-index:1}
tbody tr:last-child td{border-bottom:0}
td.n,th.n{text-align:right} .mono{font-family:var(--f-mono);font-variant-numeric:tabular-nums}
.mut{color:var(--mut)} .nm{font-weight:500}
td.tot{font-weight:600;color:var(--accent);font-size:14px}
tr.top3{background:var(--accent-soft)}
.pill{display:inline-block;min-width:30px;padding:2px 6px;border-radius:3px;
  font:500 11.5px/1.4 var(--f-mono);color:var(--panel);background:var(--s1)}
.pill.s3{background:var(--s3)} .pill.s2{background:var(--s2)}
.pill.s0{background:var(--s0);color:var(--mut)}

.note{font-size:12.5px;line-height:1.95;color:var(--mut);border-top:1px solid var(--line);
  padding-top:18px;display:grid;gap:9px}
.note b{color:var(--ink);font-weight:500;font-family:var(--f-body)}
.note .warn{border-left:2px solid var(--accent);padding-left:11px}
</style>
<div class="wrap">

<div class="mast">
  <h1>台股六面向選股台</h1>
  <div class="meta">交易日 ⟦TRADE⟧　·　上市普通股 ⟦NU⟧ 檔 → 深挖 ⟦NC⟧ 檔 → 前三名<br>
  權重　⟦W⟧　·　產生於 ⟦GEN⟧</div>
</div>

<section class="tape">
  <div class="lead">
    <div class="eyebrow">市場氛圍　盤面級指標，不參與個股排序</div>
    <b>⟦MSCORE⟧</b><span class="tag">⟦MLABEL⟧</span>
    <div class="gauge"><i style="width:⟦MSCORE⟧%"></i></div>
  </div>
  <div class="tstat">
    <div><b>⟦IDX⟧</b><span>加權指數 · ⟦IMA⟧</span></div>
    <div><b>⟦IRET⟧</b><span>指數 20 日</span></div>
    <div><b>⟦BREADTH⟧</b><span>站上均價家數比</span></div>
    <div><b>⟦MARGIN⟧</b><span>融資餘額日變化</span></div>
  </div>
  <div class="advice"><b>操作氛圍：</b>⟦MADVICE⟧</div>
</section>

<div class="cards">⟦CARDS⟧</div>

<h2>候選名單全表</h2>
<div class="tbl"><table>
<thead><tr>
<th class="n">#</th><th>代號</th><th>名稱</th><th class="n">收盤</th><th class="n">總分</th>
<th class="n">技術</th><th class="n">基本</th><th class="n">籌碼</th><th class="n">大戶</th>
<th class="n">輿論</th><th class="n">本益比</th><th class="n">營收YoY%</th>
<th class="n">法人5日(張)</th><th class="n">RSI</th>
</tr></thead>
<tbody>⟦ROWS⟧</tbody></table></div>

<div class="note">
<div><b>資料來源</b>　證交所 OpenAPI（報價、本益比／淨值比／殖利率、月營收、融資融券、處置股）·
證交所 T86 三大法人買賣超（近 5 個交易日累計）· 集保結算所股權分散表（資料日 ⟦TDCC⟧）·
Yahoo Finance 日 K · Google News RSS。</div>
<div><b>方法</b>　每個面向先在母體內做橫斷面百分位（0–100），缺值一律給 50 中性分，再加權相加，最後乘上風險折扣（董監質押比 &gt;30%、注意股）。前三名每個產業只取一檔 —— 三檔同產業的風險會完全相關，不叫分散。
本益比／淨值比／殖利率在<b>同產業內</b>排名，避免金融股靠結構性低本益比霸榜；營收成長率跨產業可比，用全市場排名。
法人買超取近 5 個交易日累計並除以月成交量，避免單日鉅額轉倉翻轉排名、也避免大型股靠絕對量霸榜；交易日曆直接向證交所 T86 查證，不從行情商的 K 線索引反推。</div>
<div><b>消息面</b>　取交易日前 14 天內的新聞標題，越新權重越高（3 日內 1.0、7 日內 0.6、14 日內 0.35），
以中文關鍵字詞典計算語調，非語意模型。技術面日 K 已切齊報價日，不會用到隔日盤中價，且均線／RSI／報酬率一律用<b>還原股價</b>計算 ——八九月是除權息旺季，用原始收盤會把除息跳空當成真的下跌。卡片上的股價仍是原始收盤。</div>
<div><b>大戶面向</b>　⟦WHALE_MODE⟧。大戶持股「水位」是結構性的（權值股因保管銀行常年偏高），
真正的訊號是週變化；累積兩週快照後本欄自動切換為變化量，在只有水位的期間本面向權重已自動降為 5%。</div>
<div><b>範圍限制</b>　僅含<b>上市（TWSE）普通股</b>，已排除 ETF、權證、存託憑證、處置股，
以及日成交金額低於 5,000 萬元者。<b>不含上櫃（TPEx）</b>。</div>
<div class="warn"><b>免責</b>　本看板是量化資訊整理，不是投資建議。資料可能延遲或有誤，
分數只反映所選權重下的相對排序，不代表未來報酬。投資請自行判斷並承擔風險。</div>
</div>
</div>
"""


# ---------------------------------------------------------------- 自我檢查

def selftest():
    assert roc_to_ad("1150831") == date(2026, 8, 31)
    assert math.isnan(num("")) and math.isnan(num("-")) and num("1,234") == 1234
    s = pd.Series([1, 2, math.nan, 4])
    r = pct_rank(s)
    assert r.between(0, 1).all() and not r.isna().any() and r.iloc[2] == 0.5
    assert r.iloc[3] > r.iloc[0], "higher_is_better=True 要讓大的分數高"
    r2 = pct_rank(s, higher_is_better=False)
    assert r2.iloc[0] > r2.iloc[3], "higher_is_better=False 要讓小的分數高"

    # 面向方向性：把一檔的訊號改好，該面向分數必須上升
    d = pd.DataFrame({"pe": [30., 10, 20], "pb": [3., 1, 2], "yield": [1., 5, 3],
                      "rev_yoy": [-10., 40, 5], "rev_cum_yoy": [-5., 30, 2],
                      "op_margin": [1., 20, 8], "gross_margin": [5., 40, 18],
                      "nonop_pct": [10., 10, 10],
                      "industry": ["電子"] * 3},
                     index=["bad", "good", "mid"])
    # 金融保險業的營收年增率必須被中性化，否則它只是個「金融股偵測器」
    # 只留營收欄位有差、其餘全部打平，否則 good 靠估值本來就贏，測不到中性化
    flat = pd.DataFrame({"pe": [10.] * 3, "pb": [1.] * 3, "yield": [3.] * 3,
                         "op_margin": [10.] * 3, "gross_margin": [20.] * 3, "nonop_pct": [10.] * 3,
                         "rev_yoy": [4000., 10, 20], "rev_cum_yoy": [3000., 8, 15],
                         "industry": ["電子"] * 3}, index=["a", "b", "c"])
    assert score_fundamental(flat)["a"] > score_fundamental(flat)["c"], "非金融的營收項該生效"
    fin = flat.copy(); fin["industry"] = "金融保險業"
    ff = score_fundamental(fin)
    assert abs(ff["a"] - ff["c"]) < 1e-9, f"金融股營收項未中性化: {dict(ff)}"
    f = score_fundamental(d)
    assert f["good"] > f["mid"] > f["bad"], f"基本面方向反了: {dict(f)}"
    c = pd.DataFrame({"volume": [1e6] * 3, "vol_month": [2e7] * 3, "inst_net": [-5e5, 5e5, 0.],
                      "trust_net": [-1e5, 1e5, 0.], "buy_days": [0., 5, 2],
                      "trust_days": [0., 4, 2], "margin_use": [12., 0.5, 4.]},
                     index=["bad", "good", "mid"])
    ch = score_chips(c)
    assert ch["good"] > ch["mid"] > ch["bad"], f"籌碼面方向反了: {dict(ch)}"
    wdf = pd.DataFrame({"w400_chg": [-1., 2, 0.5], "retail_chg": [1., -2, -0.5],
                        "w400": [50., 80, 65], "retail": [30., 10, 20]},
                       index=["bad", "good", "mid"])
    for hd in (True, False):
        wsc = score_whale(wdf, hd)
        assert wsc["good"] > wsc["mid"] > wsc["bad"], f"大戶方向反了 (delta={hd})"

    # TDCC 級距對照：級距17 = 合計 = 1..16 之和，占比 1-15 加總 ~100
    raw = get("https://opendata.tdcc.com.tw/getOD.ashx?id=1-5", kind="text",
              cache_key="tdcc_latest.csv", ttl=24 * 3600)
    rows = [r for r in csv.DictReader(io.StringIO(raw)) if r["證券代號"].strip() == "2330"]
    tot = sum(num(r["股數"], 0) for r in rows if r["持股分級"].strip() != "17")
    lvl17 = [num(r["股數"], 0) for r in rows if r["持股分級"].strip() == "17"][0]
    assert abs(tot - lvl17) / lvl17 < 1e-6, "TDCC 級距對照不符"
    assert 99 < sum(num(r["占集保庫存數比例%"], 0) for r in rows
                    if r["持股分級"].strip() not in ("16", "17")) < 101

    df, td, _ = fetch_universe()
    assert df.index.str.fullmatch(r"[1-9]\d{3}").all(), "母體混進非普通股"
    assert (df["turnover"] >= MIN_TURNOVER).all(), "母體混進低流動性股"
    assert not df["name"].str.contains("DR$|-DR").any(), "母體混進存託憑證"
    ch, t86d = fetch_chips(td)
    assert {"buy_days", "trust_days"} <= set(ch.columns)
    assert ch["buy_days"].between(0, 5).all(), "買超天數超出 0~5"
    assert t86d[0] == td.strftime("%Y%m%d"), "T86 日期與報價日不符"
    assert len(t86d) == 5, f"五日籌碼只取到 {len(t86d)} 天"
    assert "inst_net_1d" in ch.columns
    # 五日窗必須「連續」：窗內每個平日，不是在清單裡，就是 T86 自己說它不是交易日。
    # 這正是漏掉 2026-08-28 的那個 bug 的性質 —— 用跨幾個日曆日或比對 ^TWII 都驗不出來
    # （壞窗口 0824~0831 只跨 7 天，兩種寬鬆檢查都會通過）。
    def _d(x):
        return date(int(x[:4]), int(x[4:6]), int(x[6:]))

    have = {_d(x) for x in t86d}
    cur, holes = _d(t86d[-1]), []
    while cur < _d(t86d[0]):
        cur += timedelta(days=1)
        if cur.weekday() < 5 and cur not in have:
            try:
                _t86_one(cur.strftime("%Y%m%d"))
                holes.append(cur)          # T86 有資料卻沒被納入 = 日曆漏了一天
            except Exception:
                pass                       # T86 拒絕 = 國定假日，本來就不該在清單裡
    assert not holes, f"五日窗漏掉真實交易日 {holes}：{t86d}"

    # 消息面是絕對尺度、對樣本數收縮，且無新聞 = 中性
    assert score_news({"tone": 0.0, "count": 0}) == 0.5
    assert score_news({"tone": 1.0, "count": 1}) < score_news({"tone": 1.0, "count": 50})
    assert 0 <= score_news({"tone": -1.0, "count": 99}) < 0.1
    assert all(0 <= score_news({"tone": t_, "count": c_}) <= 1
               for t_ in (-1, 0, 1) for c_ in (0, 1, 100))

    hy = yahoo_history("2357.TW", asof=td)
    assert "adj" in hy and hy["adj"].notna().all(), "還原股價缺失"
    assert (hy["adj"] != hy["close"]).any(), "adj 與 close 完全相同，還原價可能沒抓到"
    mv, mv_month = fetch_monthly_volume()
    # .any() 只要一列為正就通過，守不住「缺值就全體換分母」那道硬分支
    assert mv["vol_month"].gt(0).mean() > 0.9 and mv_month.isdigit()
    t = technicals(hy)
    assert t and 0 <= score_tech(t) <= 1
    base = dict(px=100., adj=100., ma5=100., ma20=100., ma60=100., rsi=55.,
                ret20=10., ret60=5., vol_ratio=1.2, from_high=-5.)
    # 沒有斷崖：月線上下各 0.1% 的分差要遠小於整項權重
    a = score_tech({**base, "adj": 99.9}), score_tech({**base, "adj": 100.1})
    assert abs(a[1] - a[0]) < .02, f"月線門檻仍是斷崖: {a}"
    # 駝峰：漲太多、量太大反而該扣分
    assert score_tech({**base, "ret20": 45.}) < score_tech({**base, "ret20": 15.}), "動能未做駝峰"
    assert score_tech({**base, "vol_ratio": 3.5}) < score_tech({**base, "vol_ratio": 1.5}), "量能未做駝峰"
    assert score_tech({**base, "rsi": 88.}) < score_tech({**base, "rsi": 60.}), "RSI 過熱未扣分"
    gs = pd.Series(["金融"] * 3 + ["半導體"] * 3, index=list("abcdef"))
    vs = pd.Series([.8, 1.0, 1.2, 3.0, 4.0, 5.0], index=list("abcdef"))
    pr = pct_rank_by(vs, gs, higher_is_better=False, min_n=3)
    assert pr["a"] == pr["d"], "產業內排名應讓各產業最便宜者同分，而非讓低PB產業霸榜"
    assert pr["a"] > pr["c"] and pr["d"] > pr["f"]
    # 樣本不足的產業要併成「其他」一起排，不是退回全市場
    gs2 = pd.Series(["金融"] * 3 + ["半導體"] * 3 + ["觀光", "水泥"], index=list("abcdefgh"))
    vs2 = pd.Series([.8, 1., 1.2, 3., 4., 5., 9., 10.], index=list("abcdefgh"))
    pr2 = pct_rank_by(vs2, gs2, higher_is_better=False, min_n=3)
    # g、h 兩檔小產業被併進「其他」互相比，而不是丟回 8 檔全市場池
    assert pr2["g"] == 1.0 and pr2["h"] == 0.5, f"小產業未併組: {dict(pr2)}"
    assert abs(sum(WEIGHTS.values()) - 1) < 1e-9
    for hd in (True, False):
        assert abs(sum(weights(hd).values()) - 1) < 1e-9
    assert "⟦" in TEMPLATE and TEMPLATE.count("⟦") == TEMPLATE.count("⟧")
    hh = yahoo_history("2330.TW")
    # Python 把 nan 格式化成小寫 —— 原本寫 "NaN" 的版本對真的含 nan 的圖也會通過
    _sp = sparkline(hh, "2330")
    assert _sp.startswith("<svg") and "nan" not in _sp.lower()
    assert 'class="rise"' in sign_span(1.5) and 'class="fall"' in sign_span(-1.5)
    assert grade(.9) == "s3" and grade(.1) == "s0"
    # 樣板 token 與 render() 實際提供的欄位必須完全對得上。
    # 只數 ⟦ 和 ⟧ 的數量是騙人的：打成 ⟦MSCOR⟧ 計數依然平衡、selftest 依然綠燈，
    # 要到正式跑到 render() 才炸 —— 而那時舊看板已經被截斷了。
    want = set(tokens_of(TEMPLATE))
    fake = pd.DataFrame({
        "name": ["甲公司", "乙公司", "丙<script>"], "close": [100., 50., 25.],
        "industry": ["電子工業", "水泥工業", math.nan],
        "score": [.8, .7, .6], "s_tech": [.9, .5, .3], "s_fund": [.8, .6, .4],
        "s_chips": [.7, .5, .3], "s_whale": [.6, .5, .4], "s_news": [.5, .5, .5],
        "pe": [12., math.nan, 8.], "pb": [1.5, math.nan, .8],
        "yield": [3., 0., math.nan], "rev_yoy": [20., math.nan, -5.],
        "rev_cum_yoy": [15., math.nan, -3.], "volume": [1e6, 2e6, 3e6],
        "inst_net": [1e6, math.nan, -5e5], "inst_net_1d": [2e5, math.nan, -1e5],
        "trust_net": [3e5, math.nan, 0.], "w400": [70., 60., 50.],
        "retail": [10., 20., 30.], "w400_chg": [1.5, math.nan, -.5],
        "retail_chg": [-1., math.nan, .5],
        "op_margin": [15., math.nan, 3.], "gross_margin": [30., math.nan, 7.],
        "nonop_pct": [10., math.nan, 65.], "pledge": [0., math.nan, 45.],
        "buy_days": [5., math.nan, 1.], "trust_days": [3., math.nan, 0.],
        "margin_use": [0.5, math.nan, 12.], "short_ratio": [1., math.nan, 50.],
        "flags": ["", "除息（停資券 1150901）", "注意股：連續四次"],
        "pick": [True, True, True],
    }, index=["1101", "2330", "9999"])
    fnews = {c: {"count": 3, "tone": .5, "heads": ["標題 <b>x</b> ⟦GEN⟧ 與 ⟦BOGUS⟧"]}
             for c in fake.index}
    ftech = {c: technicals(hh) for c in fake.index}
    page = render(fake, ftech, fnews, market_mood(df, td, yahoo_history("%5ETWII", asof=td)),
                  td, "20260828", "測試", 400, weights(False), {c: hh for c in fake.index}, False)
    # 單次掃描的性質：新聞標題裡的 ⟦GEN⟧／⟦BOGUS⟧ 是外部文字，要原封不動留著 ——
    # 不被當成樣板欄位代換掉（循序 replace 會），也不讓未知欄位炸掉整份輸出。
    assert "⟦GEN⟧" in page and "⟦BOGUS⟧" in page, "外部文字裡的 ⟦⟧ 被當成樣板欄位處理了"
    assert "&lt;b&gt;" in page and "<script>" not in page, "外部文字未逸出"
    assert "nan" not in page.lower().replace("finance", ""), "看板印出了 nan"
    assert set(tokens_of(TEMPLATE)) == want and len(want) > 10

    mood = market_mood(df, td, yahoo_history("%5ETWII", asof=td))
    assert 0 < mood["breadth"] < 100, \
        f"市場寬度 {mood['breadth']}% —— 均價基準退化了（月初的月平均價 == 當日收盤）"
    assert (df["close"] != df["ref_price"]).mean() > 0.5, "均線基準與收盤價幾乎全等"

    pl = fetch_pledge()
    assert pl.between(0, 100).all() and len(pl) > 500
    fl, dr = fetch_flags()
    assert isinstance(dr, set) and "" not in dr, "空代號混進排除名單"
    rk = risk_factor(fake)
    assert rk["1101"] == 1.0 and rk["9999"] < .9, f"風險折扣沒生效: {dict(rk)}"
    assert rk.between(.65, 1.0).all()

    # prune 絕不能碰 tdcc 快照 —— 刪掉就沒有大戶週變化，而且靜默無聲
    # 乾跑 —— 測試不該把使用者的快取清掉
    assert not [f for f in prune_cache(days=0, dry=True) if f.startswith("tdcc_")], \
        "prune 會刪到集保快照"
    assert prune_cache(days=99999, dry=True) == [], "prune 的天數門檻沒生效"

    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
    else:
        run()
