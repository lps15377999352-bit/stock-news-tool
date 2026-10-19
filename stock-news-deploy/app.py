#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
股票新闻助手 —— 本地网页应用（后端）

功能：
  - 抓取自选股实时新闻（akshare，存 SQLite）
  - 点按钮调用 DeepSeek 把新闻翻译成大白话 + 判断利好/利空

启动后浏览器自动打开。关闭本进程即停止服务。
"""

import hashlib
import json
import os
import sqlite3
import sys
import threading
import webbrowser
from datetime import datetime

# 绕过系统代理：国内财经接口直连，代理挂了会导致请求失败
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

# embeddable Python 不会自动把脚本目录加入 sys.path
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import requests
from flask import Flask, jsonify, request, send_from_directory

import stock_news

# ---------- 配置 ----------
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")
CONFIG = {}
if os.path.exists(CONFIG_PATH):
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        CONFIG = json.load(f)

# 优先读环境变量（云部署 Render），本地用 config.json 兜底
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "") or CONFIG.get("deepseek_api_key", "")
DEEPSEEK_MODEL = CONFIG.get("deepseek_model", "deepseek-v4-flash")
DEEPSEEK_URL = CONFIG.get("deepseek_base_url", "https://api.deepseek.com/chat/completions")
PORT = int(os.environ.get("PORT", CONFIG.get("port", 5000)))

WATCHLIST_PATH = os.path.join(SCRIPT_DIR, "watchlist.txt")

app = Flask(__name__)
FETCH_STATE = {"fetching": False, "message": ""}


# ---------- 数据层 ----------
def ensure_analysis_table():
    conn = sqlite3.connect(stock_news.DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS analysis (
            news_id INTEGER PRIMARY KEY,
            plain TEXT,
            sentiment TEXT,
            impact TEXT,
            analyzed_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def ensure_debate_table():
    conn = sqlite3.connect(stock_news.DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS debate (
            news_id INTEGER PRIMARY KEY,
            bull TEXT,
            bear TEXT,
            conclusion TEXT,
            sentiment TEXT,
            action TEXT,
            impact TEXT,
            analyzed_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def load_watchlist():
    codes = []
    if os.path.exists(WATCHLIST_PATH):
        with open(WATCHLIST_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    codes.append(line.split()[0])
    return codes


def save_watchlist(codes):
    with open(WATCHLIST_PATH, "w", encoding="utf-8") as f:
        for code in codes:
            f.write(code + "\n")


# ---------- DeepSeek 分析 ----------
def call_deepseek(prompt):
    """调用 DeepSeek，返回去掉围栏后的文本内容"""
    resp = requests.post(
        DEEPSEEK_URL,
        headers={"Authorization": f"Bearer {DEEPSEEK_KEY}",
                 "Content-Type": "application/json"},
        json={
            "model": DEEPSEEK_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "stream": False,
        },
        timeout=90,
    )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"].strip()
    # 去掉可能的 ```json 围栏
    if content.startswith("```"):
        content = content.strip("`")
        if content.lower().startswith("json"):
            content = content[4:]
        content = content.strip()
    return content


def analyze_with_deepseek(news):
    prompt = (
        "你是一名资深A股分析师。请把下面的财经新闻翻译成通俗易懂的大白话，"
        "并判断对股价的影响。\n\n"
        f"【新闻标题】{news['title']}\n"
        f"【来源】{news['source']}\n"
        f"【时间】{news['publish_time']}\n"
        f"【内容】{news['content']}\n\n"
        "请严格按以下 JSON 格式输出（不要输出其他任何内容）：\n"
        '{"plain": "大白话总结（2-3句话，通俗易懂）", '
        '"sentiment": "利好/利空/中性", '
        '"impact": "对股价的潜在影响（一句话）"}'
    )
    content = call_deepseek(prompt)
    try:
        return json.loads(content)
    except Exception:
        return {"plain": content, "sentiment": "未知", "impact": ""}


def debate_with_deepseek(news):
    """多角色辩论：看多研究员 + 看空研究员 + 投资经理裁决（三次调用）"""
    base = (f"【新闻标题】{news['title']}\n"
            f"【来源】{news['source']}\n"
            f"【时间】{news['publish_time']}\n"
            f"【内容】{news['content']}\n")

    bull_prompt = (
        "你是一位激进的看多A股分析师。请针对下面这条新闻，尽力找出对股价有利的因素，"
        "给出看多理由（2-3点，具体有说服力）。\n\n" + base +
        "请严格按 JSON 输出：{\"view\": \"看多理由\"}"
    )
    bear_prompt = (
        "你是一位谨慎的看空A股分析师。请针对下面这条新闻，尽力找出对股价不利的风险和隐忧，"
        "给出看空理由（2-3点，具体有说服力）。\n\n" + base +
        "请严格按 JSON 输出：{\"view\": \"看空理由\"}"
    )
    bull_content = call_deepseek(bull_prompt)
    bear_content = call_deepseek(bear_prompt)
    try:
        bull = json.loads(bull_content).get("view", bull_content)
    except Exception:
        bull = bull_content
    try:
        bear = json.loads(bear_content).get("view", bear_content)
    except Exception:
        bear = bear_content

    manager_prompt = (
        "你是一位资深投资经理。下面是一条新闻，以及看多、看空两位分析师的观点。"
        "请综合权衡双方论点后给出最终判断。\n\n" + base +
        f"【看多观点】\n{bull}\n\n【看空观点】\n{bear}\n\n"
        "请严格按 JSON 输出：{\"conclusion\": \"最终结论（大白话2-3句）\", "
        "\"sentiment\": \"利好/利空/中性\", \"action\": \"买入/卖出/观望\", "
        "\"impact\": \"对股价的影响（一句话）\"}"
    )
    manager_content = call_deepseek(manager_prompt)
    try:
        manager = json.loads(manager_content)
    except Exception:
        manager = {"conclusion": manager_content, "sentiment": "中性",
                   "action": "观望", "impact": ""}
    return {"bull": bull, "bear": bear,
            "conclusion": manager.get("conclusion", ""),
            "sentiment": manager.get("sentiment", "中性"),
            "action": manager.get("action", "观望"),
            "impact": manager.get("impact", "")}


# ---------- 行情 / K线 / 搜索 ----------
def _sina_symbol(code):
    code = str(code).strip()
    return ("sh" if code[:1] in "569" else "sz") + code


def fetch_quotes(codes):
    """腾讯实时行情（含 PE/PB/市值/换手率），不封 IP，比新浪字段更全更准"""
    if not codes:
        return {}
    prefixed = []
    key_of = {}
    SH_INDEX = {"000300", "000905", "000016", "000688", "000852", "000010"}
    for c in codes:
        c = str(c).strip()
        low = c.lower()
        if low.startswith(("sh", "sz", "bj")):
            p = low
        elif c.startswith("92"):
            p = "bj" + c
        elif c in SH_INDEX or c.startswith(("5", "6", "9")):
            p = "sh" + c
        elif c.startswith(("4", "8")):
            p = "bj" + c
        else:
            p = "sz" + c
        prefixed.append(p)
        key_of[p] = c

    url = "https://qt.gtimg.cn/q=" + ",".join(prefixed)
    r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
    r.encoding = "gbk"

    def f(vals, i):
        try:
            return float(vals[i]) if vals[i] else 0.0
        except (ValueError, IndexError):
            return 0.0

    result = {}
    for line in r.text.strip().split(";"):
        if not line.strip() or "=" not in line or '"' not in line:
            continue
        key = line.split("=")[0].split("_")[-1]
        vals = line.split('"')[1].split("~")
        if len(vals) < 53:
            continue
        code = key_of.get(key, key[2:])
        result[code] = {
            "code": code,
            "name": vals[1],
            "price": f(vals, 3),
            "last_close": f(vals, 4),
            "change_amt": f(vals, 31),
            "change_pct": f(vals, 32),
            "high": f(vals, 33),
            "low": f(vals, 34),
            "turnover_pct": f(vals, 38),
            "pe_ttm": f(vals, 39),
            "float_mcap_yi": f(vals, 44),
            "mcap_yi": f(vals, 45),
            "pb": f(vals, 46),
        }
    return result


def cls_telegraph(page_size=50):
    """财联社电报（全市场实时快讯，v1 API + 本地签名，零 key）"""
    params = {"appName": "CailianpressWeb", "os": "web", "sv": "7.7.5",
              "last_time": "", "refresh_type": "1", "rn": str(page_size)}
    qs = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
    url = f"https://www.cls.cn/v1/roll/get_roll_list?{qs}&sign={sign}"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.cls.cn/"}
    r = requests.get(url, headers=headers, timeout=10)
    d = r.json()
    rows = []
    for item in (d.get("data") or {}).get("roll_data") or []:
        ts = item.get("ctime")
        t = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
        nid = item.get("id", "")
        rows.append({
            "id": nid,
            "title": item.get("title", "") or item.get("brief", ""),
            "content": item.get("content", "") or item.get("brief", ""),
            "time": t,
            "url": f"https://www.cls.cn/detail/{nid}" if nid else "",
        })
    return rows


def fetch_market():
    """大盘指数（上证/深证/创业板）"""
    import akshare as ak
    df = ak.stock_zh_index_spot_sina()
    targets = ["上证指数", "深证成指", "创业板指"]
    out = []
    for _, r in df.iterrows():
        name = str(r["名称"])
        if name in targets:
            out.append({"name": name, "code": str(r["代码"]),
                        "price": float(r["最新价"]),
                        "change_pct": float(r["涨跌幅"])})
    order = {n: i for i, n in enumerate(targets)}
    out.sort(key=lambda x: order.get(x["name"], 99))
    return out


def fetch_kline(code, limit=180):
    """日K线数据，返回 ECharts 需要的 [日期, 开, 收, 低, 高]（腾讯源，支持 ETF）"""
    import akshare as ak
    import datetime as dt
    end = dt.date.today().strftime("%Y%m%d")
    start = (dt.date.today() - dt.timedelta(days=int(limit * 1.8) + 30)).strftime("%Y%m%d")
    df = ak.stock_zh_a_hist_tx(symbol=_sina_symbol(code), start_date=start,
                               end_date=end, adjust="qfq")
    df = df.tail(limit)
    out = []
    for _, r in df.iterrows():
        out.append([str(r["date"]), float(r["open"]), float(r["close"]),
                    float(r["low"]), float(r["high"])])
    return out


STOCK_LIST_CACHE = {"data": None, "ts": 0}


def fetch_stock_list():
    """A股代码-名称列表（搜索用，缓存 30 分钟）"""
    import akshare as ak
    import time
    if STOCK_LIST_CACHE["data"] and (time.time() - STOCK_LIST_CACHE["ts"]) < 1800:
        return STOCK_LIST_CACHE["data"]
    df = ak.stock_info_a_code_name()
    data = [{"code": str(r["code"]), "name": str(r["name"])}
            for _, r in df.iterrows()]
    STOCK_LIST_CACHE["data"] = data
    STOCK_LIST_CACHE["ts"] = time.time()
    return data


# ---------- 微头条草稿 ----------
GENERATE_STATE = {"generating": False}


def ensure_drafts_table():
    conn = sqlite3.connect(stock_news.DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id TEXT,
            news_title TEXT,
            news_content TEXT,
            news_url TEXT,
            generated_text TEXT,
            status TEXT DEFAULT 'draft',
            created_at TEXT
        )
    """)
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_drafts_news ON drafts(news_id)")
    conn.commit()
    conn.close()


def load_weibo_prompt():
    path = os.path.join(SCRIPT_DIR, "weibo_prompt.txt")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def get_settings():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    else:
        cfg = {}
    return {"limit": cfg.get("draft_limit", 10),
            "auto_generate": cfg.get("auto_generate", False)}


def set_settings(limit, auto_generate):
    if not os.path.exists(CONFIG_PATH):
        return
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    cfg["draft_limit"] = limit
    cfg["auto_generate"] = auto_generate
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def generate_weibo_draft(news):
    """用提示词 + 快讯内容，调 DeepSeek 生成微头条草稿"""
    text = news.get("content") or news.get("title", "")
    prompt = (load_weibo_prompt() + "\n\n【今日素材（财经快讯）】\n"
              + text + "\n\n请直接输出完整微头条成品。")
    return call_deepseek(prompt)


def generate_drafts_for_new_news(limit):
    """检测财联社新快讯，为未生成草稿的快讯生成草稿（limit=0 表示不限）"""
    try:
        telegraph = cls_telegraph(50)
    except Exception as e:
        return {"generated": 0, "error": str(e)}
    conn = sqlite3.connect(stock_news.DB_PATH)
    c = conn.cursor()
    c.execute("SELECT news_id FROM drafts")
    existing = set(r[0] for r in c.fetchall())
    generated = 0
    for item in telegraph:
        if limit > 0 and generated >= limit:
            break
        nid = str(item.get("id", ""))
        if not nid or nid in existing:
            continue
        try:
            text = generate_weibo_draft(item)
            c.execute("INSERT INTO drafts (news_id, news_title, news_content, "
                      "news_url, generated_text, status, created_at) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (nid, item.get("title", ""), item.get("content", ""),
                       item.get("url", ""), text, "draft",
                       datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            conn.commit()
            generated += 1
        except Exception as e:
            print(f"生成草稿失败 {nid}: {e}")
    conn.close()
    return {"generated": generated}


# ---------- 后台抓取 ----------
def fetch_in_background(codes):
    FETCH_STATE["fetching"] = True
    FETCH_STATE["message"] = ""
    try:
        conn = stock_news.init_db()
        for code in codes:
            df = stock_news.fetch_stock_news(code)
            rows = stock_news._normalize_stock_news(code, df)
            stock_news.save_news(conn, rows)
        conn.close()
        FETCH_STATE["message"] = "ok"
    except Exception as e:
        FETCH_STATE["message"] = str(e)
    finally:
        FETCH_STATE["fetching"] = False


# ---------- 路由 ----------
@app.route("/")
def index():
    return send_from_directory(SCRIPT_DIR, "index.html")


@app.route("/api/watchlist", methods=["GET", "POST"])
def api_watchlist():
    if request.method == "GET":
        return jsonify(load_watchlist())
    data = request.get_json()
    action = data.get("action")
    code = str(data.get("code", "")).strip()
    codes = load_watchlist()
    if action == "add" and code and code not in codes:
        codes.append(code)
    elif action == "remove" and code in codes:
        codes.remove(code)
    save_watchlist(codes)
    return jsonify(codes)


@app.route("/api/status")
def api_status():
    return jsonify(FETCH_STATE)


@app.route("/api/fetch", methods=["POST"])
def api_fetch():
    if FETCH_STATE["fetching"]:
        return jsonify({"ok": False, "message": "正在抓取中，请稍候"})
    codes = load_watchlist()
    if not codes:
        return jsonify({"ok": False, "message": "自选股为空，请先添加股票"})
    threading.Thread(target=fetch_in_background, args=(codes,), daemon=True).start()
    return jsonify({"ok": True, "message": f"开始抓取 {len(codes)} 只股票"})


@app.route("/api/news")
def api_news():
    limit = request.args.get("limit", 60, type=int)
    offset = request.args.get("offset", 0, type=int)
    conn = sqlite3.connect(stock_news.DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("""
        SELECT n.id, n.stock_code, n.title, n.content, n.source, n.publish_time, n.url,
               a.plain, a.sentiment, a.impact
        FROM news n LEFT JOIN analysis a ON n.id = a.news_id
        ORDER BY n.id DESC LIMIT ? OFFSET ?
    """, (limit, offset))
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/analyze", methods=["POST"])
def api_analyze():
    data = request.get_json()
    news_id = data.get("news_id")
    conn = sqlite3.connect(stock_news.DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM news WHERE id = ?", (news_id,))
    news = c.fetchone()
    if not news:
        conn.close()
        return jsonify({"ok": False, "message": "新闻不存在"}), 404
    news = dict(news)

    # 已有缓存则直接返回
    c.execute("SELECT plain, sentiment, impact FROM analysis WHERE news_id = ?", (news_id,))
    cached = c.fetchone()
    if cached:
        conn.close()
        return jsonify({"ok": True, "cached": True, **dict(cached)})

    try:
        result = analyze_with_deepseek(news)
    except Exception as e:
        conn.close()
        return jsonify({"ok": False, "message": f"分析失败: {e}"}), 500

    plain = result.get("plain", "")
    sentiment = result.get("sentiment", "中性")
    impact = result.get("impact", "")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT OR REPLACE INTO analysis (news_id, plain, sentiment, impact, analyzed_at) "
              "VALUES (?, ?, ?, ?, ?)", (news_id, plain, sentiment, impact, now))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "cached": False, "plain": plain,
                    "sentiment": sentiment, "impact": impact})


@app.route("/api/debate", methods=["POST"])
def api_debate():
    data = request.get_json()
    news_id = data.get("news_id")
    conn = sqlite3.connect(stock_news.DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM news WHERE id = ?", (news_id,))
    news = c.fetchone()
    if not news:
        conn.close()
        return jsonify({"ok": False, "message": "新闻不存在"}), 404
    news = dict(news)

    c.execute("SELECT bull, bear, conclusion, sentiment, action, impact "
              "FROM debate WHERE news_id = ?", (news_id,))
    cached = c.fetchone()
    if cached:
        conn.close()
        return jsonify({"ok": True, "cached": True, **dict(cached)})

    try:
        result = debate_with_deepseek(news)
    except Exception as e:
        conn.close()
        return jsonify({"ok": False, "message": f"辩论分析失败: {e}"}), 500

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    c.execute("INSERT OR REPLACE INTO debate "
              "(news_id, bull, bear, conclusion, sentiment, action, impact, analyzed_at) "
              "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
              (news_id, result["bull"], result["bear"], result["conclusion"],
               result["sentiment"], result["action"], result["impact"], now))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "cached": False, **result})


@app.route("/api/market")
def api_market():
    try:
        return jsonify(fetch_market())
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/quotes")
def api_quotes():
    codes = load_watchlist()
    if not codes:
        return jsonify([])
    try:
        quotes = fetch_quotes(codes)
        return jsonify([quotes[c] for c in codes if c in quotes])
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/kline")
def api_kline():
    code = request.args.get("code", "").strip()
    limit = request.args.get("limit", 180, type=int)
    if not code:
        return jsonify({"ok": False, "message": "缺少股票代码"}), 400
    try:
        return jsonify(fetch_kline(code, limit))
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/stocks/search")
def api_stocks_search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    try:
        stocks = fetch_stock_list()
        ql = q.lower()
        res = [s for s in stocks if ql in s["code"] or ql in s["name"].lower()][:20]
        return jsonify(res)
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/telegraph")
def api_telegraph():
    try:
        return jsonify(cls_telegraph(50))
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/drafts")
def api_drafts():
    conn = sqlite3.connect(stock_news.DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM drafts ORDER BY id DESC")
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return jsonify(rows)


@app.route("/api/drafts/<int:did>", methods=["DELETE"])
def api_draft_delete(did):
    conn = sqlite3.connect(stock_news.DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM drafts WHERE id = ?", (did,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/drafts/batch-delete", methods=["POST"])
def api_drafts_batch_delete():
    data = request.get_json()
    ids = data.get("ids", [])
    if not ids:
        return jsonify({"ok": False, "message": "未选择草稿"}), 400
    conn = sqlite3.connect(stock_news.DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM drafts WHERE id IN ({})".format(",".join("?" * len(ids))), ids)
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "deleted": c.rowcount})


@app.route("/api/generate-draft", methods=["POST"])
def api_generate_draft():
    data = request.get_json()
    nid = str(data.get("id", ""))
    title = data.get("title", "")
    content = data.get("content", "")
    url = data.get("url", "")
    if not nid or not (content or title):
        return jsonify({"ok": False, "message": "缺少新闻内容"}), 400
    conn = sqlite3.connect(stock_news.DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id FROM drafts WHERE news_id = ?", (nid,))
    if c.fetchone():
        conn.close()
        return jsonify({"ok": False, "message": "该快讯已生成过草稿"}), 409
    try:
        text = generate_weibo_draft({"title": title, "content": content})
        c.execute("INSERT INTO drafts (news_id, news_title, news_content, "
                  "news_url, generated_text, status, created_at) "
                  "VALUES (?,?,?,?,?,?,?)",
                  (nid, title, content, url, text, "draft",
                   datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        conn.close()
        return jsonify({"ok": False, "message": str(e)}), 500


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        return jsonify(get_settings())
    data = request.get_json()
    limit = data.get("limit", 10)
    auto = data.get("auto_generate", False)
    set_settings(limit, auto)
    return jsonify(get_settings())


@app.route("/api/generate-drafts", methods=["POST"])
def api_generate_drafts():
    if GENERATE_STATE["generating"]:
        return jsonify({"ok": False, "message": "正在生成中"})

    def run():
        GENERATE_STATE["generating"] = True
        try:
            generate_drafts_for_new_news(get_settings()["limit"])
        finally:
            GENERATE_STATE["generating"] = False

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"ok": True, "message": "开始生成草稿"})


if __name__ == "__main__":
    ensure_analysis_table()
    ensure_debate_table()
    ensure_drafts_table()
    # 本地运行时自动打开浏览器；云部署（Render）不打开
    if os.environ.get("RENDER") != "true":
        threading.Timer(1.2, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    print(f"股票新闻助手已启动：端口 {PORT}")
    app.run(host="0.0.0.0", port=PORT, debug=False)
