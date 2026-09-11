#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
持仓关键位云端监控脚本（GitHub Actions 版）
===========================================
功能：
  - 拉取 11 只持仓+自选监控最新行情（腾讯行情公开接口，港股/A股ETF）
  - 与预设江恩关键位比对，输出触发状态（击球区/卖区/止损警告）
  - 通过 Server 酱推送到微信
模式（按北京时间自动判断，也可用环境变量 MODE 覆盖）：
  - intraday：10:30 / 14:30 盘中价格监控
  - close   ：16:30 收盘复盘（含恒指收盘、当日涨跌、明日关注位）
用法：python monitor.py
依赖：仅 Python 3 标准库，无第三方包。
"""

import os
import re
import json
import urllib.request
import urllib.parse
import datetime

# ============ 配置区 ============
# SendKey 读取顺序：环境变量 SENDKEY（GitHub Actions Secret）→ 同目录 sendkey.local.txt（本地定时任务用）
SENDKEY = os.environ.get("SENDKEY", "")
if not SENDKEY:
    try:
        _cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sendkey.local.txt")
        with open(_cfg, "r", encoding="utf-8") as _f:
            SENDKEY = _f.read().strip()
    except Exception:
        pass
# ================================

# 持仓与关键位：代码(腾讯行情), 名称, 成本, 数量, 击球区(低,高), 卖区(低,高), 跌破警告线, 止损线
STOCKS = [
    ("hk01797", "东方甄选",   30.70, 7000, (None, 20.50), (26.00, 27.70), 20.12, 17.59),
    ("hk01952", "云顶新耀",   78.23, 2000, (None, None),  (31.00, 34.00), 23.52, 22.70),
    ("hk09995", "荣昌生物",  115.69,  500, (63.30, 65.50), (78.00, 86.50), None, 63.30),
    ("hk06855", "亚盛医药",   36.10,  500, (None, None),  (35.00, 37.00), 31.50, None),
    ("hk02590", "极智嘉-W",   10.01, 1200, (None, None),  ( 9.50, 10.10),  8.50, None),
    ("hk02367", "巨子生物",   36.21,  200, (24.20, 25.00), (27.60, 29.50), 24.20, None),
    ("hk02186", "绿叶制药",    1.898,1500, (None, None),  ( 1.95,  2.00),  1.79, None),
    ("sh512880", "证券ETF国泰", 1.093,4400, (None, None), ( 1.09,  1.10),  1.03, None),
    # 新增自选监控 3 只（双向点位：卖区 + 回补击球区）
    ("hk03696", "英矽智能",    0.00,    0, (45.00, 47.50), (55.50, 57.50), 49.95, 42.70),
    ("hk01357", "美图公司",    0.00,    0, ( 3.56,  3.83), ( 4.48,  5.00),  4.25,  3.56),
    ("sz300688", "创业黑马",   0.00,    0, (29.00, 30.00), (32.50, 33.50), 29.99, 28.00),
    # A组新增观察（A股烟蒂低位，形态待确认；击球区=加仓时机）
    ("sz000568", "泸州老窖",   0.00,    0, (73.40, 76.00), (87.00, 91.50), 73.37, None),
    ("sz000858", "五粮液",     0.00,    0, (70.40, 72.00), (80.50, 85.80), 70.40, None),
    ("sh601166", "兴业银行",   0.00,    0, (17.70, 18.20), (19.00, 19.50), 17.73, None),
]

QUOTE_URL = "https://qt.gtimg.cn/q={codes}"
KLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,6,qfq"

# A 组观察票（形态确认对象）
GROUP_A = ["sz000568", "sz000858", "sh601166"]


def fetch_kline(code, days=6):
    """拉取最近 N 日日K，返回 [(date, open, close, high, low), ...]（时间正序）。"""
    url = KLINE_URL % code
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
        data = json.loads(raw)
        node = data["data"][code]
        key = "qfqday" if "qfqday" in node else "day"
        rows = node[key][-days:]
        out = []
        for r in rows:
            out.append((r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4])))
        return out
    except Exception:
        return []


def detect_bottom_pattern(klines):
    """简化底部形态检测（江恩×蜡烛图）：连续下跌后 锤子线/看涨吞没/放量阳线收上击球区。"""
    if len(klines) < 4:
        return False, "K线不足"
    last = klines[-1]
    d, o, c, h, lo = last
    body = abs(c - o)
    low_shadow = min(o, c) - lo
    up_shadow = h - max(o, c)
    prev3 = klines[:-1]
    down_days = sum(1 for k in prev3 if k[2] < k[1])  # 前3日阴线数
    hammer = body > 0 and low_shadow >= 2 * body and up_shadow <= body * 1.5
    yang = c > o
    engulf = c > o and o <= prev3[-1][2] and c > prev3[-1][1] and prev3[-1][2] < prev3[-1][1]
    pattern = []
    if down_days >= 2:
        if hammer:
            pattern.append("锤子线")
        if engulf:
            pattern.append("看涨吞没")
        if yang and body > 0:
            pattern.append("阳线企稳")
    if not pattern:
        return False, "近6日无底部形态（下跌%d日，形态待观察）" % down_days
    return True, " ".join(pattern)


def build_group_a_pattern(quotes):
    """收盘模式下对 A 组三只做形态确认，返回提示段落。"""
    lines = []
    for code in GROUP_A:
        name = dict((s[0], s[1]) for s in STOCKS).get(code, code)
        q = quotes.get(code)
        klines = fetch_kline(code)
        hit, desc = detect_bottom_pattern(klines)
        strike = None
        for s in STOCKS:
            if s[0] == code:
                strike = s[4]
                break
        zone = ""
        if strike and strike[0] and strike[1]:
            zone = " 击球区%.2f~%.2f" % strike
        if not klines:
            lines.append("- %s：日K获取失败" % name)
            continue
        last = klines[-1]
        if hit:
            lines.append("- 🔔 %s：%s（%s 收盘%.2f%s），底部形态候选 → 小仓分批低吸，待次日确认加码" % (
                name, desc, last[0], last[2], zone))
        else:
            lines.append("- %s：%s（最新收盘%.2f）" % (name, desc, last[2]))
    return "\n".join(lines)


def get_mode():
    """按北京时间判断运行模式；环境变量 MODE 可覆盖。"""
    env = os.environ.get("MODE", "").strip().lower()
    if env in ("intraday", "close"):
        return env
    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)  # 北京时间
    if now.hour == 16:
        return "close"
    return "intraday"


def fetch_quotes(codes):
    """批量拉取行情，返回 {代码: {name,price,prev,high,low}}。"""
    url = QUOTE_URL.format(codes=",".join(codes))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        raw = urllib.request.urlopen(req, timeout=15).read().decode("gbk", errors="ignore")
    except Exception as e:
        return {}, "行情接口请求失败: %s" % e

    quotes = {}
    for line in raw.split(";"):
        m = re.search(r'v_(\w+)="([^"]*)"', line.strip())
        if not m:
            continue
        code = m.group(1)
        parts = m.group(2).split("~")
        if len(parts) < 7:
            continue
        try:
            quotes[code] = {
                "name": parts[1],
                "price": float(parts[3]),
                "prev": float(parts[4]),
                "high": float(parts[5]),
                "low": float(parts[6]),
            }
        except (ValueError, IndexError):
            continue
    return quotes, ""


def check_level(stock, q):
    """比对关键位，返回 (状态标签, 建议, 涨跌幅)。"""
    code, name, cost, qty, strike, sell, warn, stop = stock
    price = q["price"]
    tag, advice = "未触达", "持有观察"

    if warn and price < warn:
        tag = "跌破警告"
        advice = "按预案减仓一半，收盘不回补则继续减"
    if stop and price < stop:
        tag = "止损警告"
        advice = "严格止损离场，不摊平"
    if sell and sell[0] and sell[1] and sell[0] <= price <= sell[1]:
        tag = "卖区"
        advice = "分批减仓兑现，回收资金"
    if strike and strike[0] and strike[1] and strike[0] <= price <= strike[1]:
        tag = "击球区"
        advice = "加仓时机：小仓分批低吸（形态确认后加码，FOMC前仅限小仓）"

    chg = (price - q["prev"]) / q["prev"] * 100 if q["prev"] else 0.0
    return tag, advice, chg


def level_desc(stock):
    """输出单只票的江恩关键位明细字符串。"""
    code, name, cost, qty, strike, sell, warn, stop = stock
    parts = []
    if strike and strike[0] and strike[1]:
        parts.append("回补击球区 %.2f~%.2f" % strike)
    if sell and sell[0] and sell[1]:
        parts.append("卖区 %.2f~%.2f" % sell)
    if warn:
        parts.append("跌破警告 <%.2f" % warn)
    if stop:
        parts.append("止损 <%.2f" % stop)
    if not parts:
        return ""
    return "  ".join(parts)


def build_report(quotes, close_mode):
    lines = []
    triggered = []
    markers = {
        "未触达": "", "击球区": "【击球区】", "卖区": "【卖区】",
        "跌破警告": "⚠️【跌破警告】", "止损警告": "🚨【止损警告】",
    }
    for stock in STOCKS:
        code = stock[0]
        q = quotes.get(code)
        if not q:
            lines.append("- %s：行情获取失败" % stock[1])
            continue
        tag, advice, chg = check_level(stock, q)
        if tag != "未触达":
            triggered.append(stock[1])
        verb = "收盘" if close_mode else ""
        lines.append("- %s %s %s%.3f（%+.1f%%）%s %s" % (
            stock[1], code[2:], verb, q["price"], chg, markers[tag], advice))
    if not quotes:
        lines.append("（全部行情获取失败，请检查网络/接口）")
    return "\n".join(lines), triggered


def build_levels():
    """生成全部股票的江恩关键位明细段落。"""
    return "\n".join("- %s：%s" % (s[1], level_desc(s)) for s in STOCKS if level_desc(s))


def push(title, desp):
    if not SENDKEY:
        print("[WARN] SENDKEY 未配置，跳过推送")
        print("TITLE:", title)
        print("DESP:", desp)
        return False
    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode("utf-8")
    url = "https://sctapi.ftqq.com/%s.send" % SENDKEY
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "Mozilla/5.0"})
    try:
        resp = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
        print("推送响应:", resp[:200])
        return '"code":0' in resp or '"errno":0' in resp
    except Exception as e:
        print("推送失败:", e)
        return False


def main():
    mode = get_mode()
    print("运行模式:", mode)
    codes = [s[0] for s in STOCKS]
    quotes, err = fetch_quotes(codes)
    if err:
        print("ERROR:", err)

    now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)
    datestr = now.strftime("%Y-%m-%d %H:%M")
    levels = build_levels()

    # 小美纪律解读（三要素 + 情绪周期 + 江恩法则，固定文本）
    discipline = (
        "🧭 **小美思考原理解读**\n"
        "- **三要素辩证**：竞争格局定方向（政策/产业/流动性大方向）、情绪位置定节奏（退潮期不重拳出击，放弃诱惑）、流动性做验证（成交量/南向/ETF 印证才出手）——三者共振才动手。\n"
        "- **情绪周期**：试错→发酵→高潮→退潮→冰点。当前处退潮期，亏钱效应扩散时，宁可错过不可做错。\n"
        "- **江恩24法则**：13 绝不摊平亏损（深套票不补仓摊平）；2/16 止损单设好不撤销、破位严格止损；4 盈利保护=上移止损。\n"
        "- **风险演绎**：风险有来源有减弱节点（解铃还须系铃人），9/16 FOMC 落地前不确定性未出清，防守为主。"
    )
    # 近期大事（截至 9/11 已知；动态部分由豆包定时任务检索补充）
    events = (
        "📰 **近期大事（截至9/11，动态以豆包任务检索为准）**\n"
        "- 恒指五连跌，测试 24500 江恩支撑（25000 已失守）；\n"
        "- 9/16 FOMC：加息概率约 71%（8月PPI 5.4% 超预期、油价破 100 推升通胀预期）；\n"
        "- 南向资金当日净买入约 21.8 亿港元，内资承接但量能有限；\n"
        "- 创新药/港股医药板块整体弱势，消息面以 BD 与医保谈判预期为主，落地前勿提前押注。"
    )

    if mode == "close":
        body, triggered = build_report(quotes, close_mode=True)
        pattern = build_group_a_pattern(quotes)
        title = "持仓收盘复盘 %s" % datestr[:10]
        desp = "## 持仓收盘复盘（%s）\n%s\n\n🔔 **A组形态确认**\n%s\n\n📐 **江恩关键位明细**\n%s\n\n%s\n\n%s\n\n⚠️ 免责声明：技术分析方法演示，不构成投资建议；行情以官方披露为准。" % (
            datestr, body, pattern, levels, events, discipline)
    else:
        body, triggered = build_report(quotes, close_mode=False)
        if triggered:
            title = "持仓提醒：%s 触达关键位" % "、".join(triggered[:3])
        else:
            title = "持仓监控-无触达"
        desp = "## 持仓关键位监控（%s）\n%s\n\n📐 **江恩关键位明细**\n%s\n\n%s\n\n%s\n\n⚠️ 免责声明：技术分析方法演示，不构成投资建议；行情以官方披露为准。" % (
            datestr, body, levels, events, discipline)

    print("===== 消息内容 =====")
    print(title)
    print(desp)
    push(title, desp)


if __name__ == "__main__":
    main()
