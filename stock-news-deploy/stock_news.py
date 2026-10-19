#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
股票新闻抓取工具（轻量版）

功能：
  1. 抓取个股实时新闻（东方财富，免费无需注册）
  2. 存入本地 SQLite，按 URL 自动去重，累积历史
  3. 打印最近新闻，并标记本次「新增」的

用法：
  python stock_news.py 600519               # 抓茅台新闻，默认显示最近 10 条
  python stock_news.py 600519 --limit 5     # 只看最近 5 条
  python stock_news.py 600519 --full        # 显示完整新闻内容
  python stock_news.py --market             # 看全市场财联社电报（实时快讯）
  python stock_news.py --list               # 查看本地已存的历史新闻
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime

# 强制 UTF-8 输出，避免 Windows 控制台中文乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

# 绕过系统代理：国内财经接口直连，代理挂了会导致请求失败
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(SCRIPT_DIR, "news.db")


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS news (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            stock_code TEXT,
            title TEXT,
            content TEXT,
            source TEXT,
            publish_time TEXT,
            url TEXT,
            fetched_at TEXT
        )
    """)
    c.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_news_dedup
        ON news(stock_code, url)
    """)
    conn.commit()
    return conn


def _normalize_stock_news(code, df):
    """东方财富个股新闻 DataFrame -> 统一 dict 列表"""
    rows = []
    for _, r in df.iterrows():
        url = str(r.get("新闻链接", "")).strip()
        if not url or url == "nan":
            # 无链接时用 标题+时间 兜底去重
            url = f"{r.get('新闻标题', '')}|{r.get('发布时间', '')}"
        rows.append({
            "stock_code": code,
            "title": str(r.get("新闻标题", "")).strip(),
            "content": str(r.get("新闻内容", "")).strip(),
            "source": str(r.get("文章来源", "")).strip(),
            "publish_time": str(r.get("发布时间", "")).strip(),
            "url": url,
        })
    return rows


def _normalize_market_news(df):
    """财联社电报 DataFrame -> 统一 dict 列表"""
    rows = []
    for _, r in df.iterrows():
        title = str(r.get("标题", "")).strip()
        pub = str(r.get("发布日期", "")).strip()
        rows.append({
            "stock_code": "MARKET",
            "title": title,
            "content": str(r.get("内容", "")).strip(),
            "source": "财联社",
            "publish_time": pub,
            "url": f"{title}|{pub}",
        })
    return rows


def fetch_stock_news(code):
    import akshare as ak
    return ak.stock_news_em(symbol=code)


def fetch_market_news():
    import akshare as ak
    return ak.stock_info_global_cls(symbol="全部")


def save_news(conn, rows):
    """保存新闻，返回本次真正新增的条目列表（用于去重后识别新消息）"""
    c = conn.cursor()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_rows = []
    for r in rows:
        try:
            c.execute(
                "INSERT OR IGNORE INTO news "
                "(stock_code, title, content, source, publish_time, url, fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (r["stock_code"], r["title"], r["content"], r["source"],
                 r["publish_time"], r["url"], now),
            )
            if c.rowcount > 0:
                new_rows.append(r)
        except sqlite3.Error:
            continue
    conn.commit()
    return new_rows


def print_news(items, show_full=False):
    if not items:
        print("（无新闻）")
        return
    for i, it in enumerate(items, 1):
        print(f"\n【{i}】{it['title']}")
        print(f"    来源: {it['source']}   时间: {it['publish_time']}"
              + ("   [新]" if it.get("is_new") else ""))
        content = it["content"]
        if content and content != "nan":
            text = content if show_full else content[:120]
            print(f"    内容: {text}" + ("" if show_full else "..."))
        else:
            print("    内容: （无）")
    print()


def main():
    ap = argparse.ArgumentParser(description="股票新闻抓取工具")
    ap.add_argument("code", nargs="?", help="股票代码，如 600519")
    ap.add_argument("--limit", type=int, default=10, help="显示最近 N 条（默认 10）")
    ap.add_argument("--full", action="store_true", help="显示完整新闻内容")
    ap.add_argument("--market", action="store_true", help="抓全市场财联社电报")
    ap.add_argument("--list", action="store_true", help="查看本地已存历史")
    args = ap.parse_args()

    conn = init_db()

    if args.list:
        c = conn.cursor()
        c.execute("SELECT stock_code, title, source, publish_time FROM news "
                  "ORDER BY id DESC LIMIT 50")
        rows = c.fetchall()
        print(f"本地历史新闻共 {len(rows)} 条（最近 50 条）：")
        for code, title, source, pub in rows:
            print(f"  [{code}] {title}  ({source} {pub})")
        conn.close()
        return

    if args.market:
        print("正在抓取财联社电报（全市场实时快讯）...")
        df = fetch_market_news()
        rows = _normalize_market_news(df)
        new_rows = save_news(conn, rows)
        print(f"本次抓取 {len(rows)} 条，新增 {len(new_rows)} 条")
        print_news(rows[:args.limit], args.full)
        conn.close()
        return

    if not args.code:
        ap.error("请提供股票代码，例如：python stock_news.py 600519")

    print(f"正在抓取个股 {args.code} 的最新新闻...")
    df = fetch_stock_news(args.code)
    rows = _normalize_stock_news(args.code, df)
    new_rows = save_news(conn, rows)
    print(f"本次抓取 {len(rows)} 条，新增 {len(new_rows)} 条（数据库累计去重后保存）")

    new_urls = {r["url"] for r in new_rows}
    display = rows[:args.limit]
    items = []
    for r in display:
        items.append({
            "title": r["title"], "content": r["content"],
            "source": r["source"], "publish_time": r["publish_time"],
            "is_new": r["url"] in new_urls,
        })
    print_news(items, args.full)
    conn.close()


if __name__ == "__main__":
    main()
