"""
telegram_notifier.py

screened_stocks.json을 읽어서 텔레그램으로 TOP 후보 요약을 전송합니다.

필수 환경변수:
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID

실행:
python telegram_notifier.py
"""

import os
import json
import html
import requests
from pathlib import Path


JSON_PATH = Path("screened_stocks.json")


def fmt_pct(v):
    try:
        v = float(v)
        return f"{v:+.1f}%"
    except Exception:
        return "-"


def fmt_num(v):
    try:
        return f"{int(float(v)):,}"
    except Exception:
        return "-"


def pick(stock, *keys, default=None):
    for k in keys:
        if k in stock and stock[k] not in (None, ""):
            return stock[k]
    return default


def build_message(data, top_n=5):
    stocks = data.get("stocks", [])[:top_n]

    market = data.get("market", {})
    if isinstance(market, dict):
        market_line = (
            f"코스피 {fmt_pct(market.get('kospi'))} / "
            f"코스닥 {fmt_pct(market.get('kosdaq'))} / "
            f"나스닥선물 {fmt_pct(market.get('nasdaq_future'))} / "
            f"SOX {fmt_pct(market.get('sox'))} / "
            f"환율 {fmt_pct(market.get('usdkrw'))} | "
            f"{market.get('mode', '-')}"
        )
    else:
        market_line = str(market or "-")

    lines = []
    lines.append("📚 <b>오버나잇 후보 TOP5</b>")
    lines.append(f"🌏 {html.escape(market_line)}")
    lines.append("")

    for i, s in enumerate(stocks, 1):
        name = html.escape(str(s.get("name", "-")))
        ticker = html.escape(str(s.get("ticker", "-")))
        flags = html.escape(str(s.get("flags", "")))
        score = pick(s, "total_score", "score", default="-")
        material = html.escape(str(pick(s, "material", "material_type", default="기타")))
        nxt = html.escape(str(pick(s, "after_status", "nxt", default="-")))
        after = fmt_pct(s.get("after_change"))
        rr = pick(s, "rr_ratio", "rr", "risk_reward", default=0)
        try:
            rr_f = float(rr)
            rr_mark = "✅" if rr_f >= 1.2 else "⚠️" if rr_f >= 0.7 else "❌"
            rr_txt = f"{rr_f:.2f} {rr_mark}"
        except Exception:
            rr_txt = "-"

        expected = fmt_pct(pick(s, "expected_gap_pct", "expected_gap", default=None))
        target = fmt_num(s.get("target_price"))
        risk = fmt_num(pick(s, "risk_price", "stop_price", default=None))
        comment = html.escape(str(pick(s, "ai_comment", "comment", default="")))

        lines.append(f"{i}. <b>{name}</b> ({ticker}) {flags}")
        lines.append(f"   점수 {score}점 | 재료 {material} | NXT {nxt} | 시간외 {after}")
        lines.append(f"   기대 {expected} | 목표 ₩{target} | 리스크 ₩{risk} | RR {rr_txt}")
        if comment:
            lines.append(f"   💬 {comment[:90]}")
        lines.append("")

    dashboard_url = os.environ.get("DASHBOARD_URL", "").strip()
    if dashboard_url:
        lines.append(f"🔗 대시보드: {html.escape(dashboard_url)}")

    return "\n".join(lines)


def send_telegram(message):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        print("TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 없어 전송을 건너뜁니다.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    r = requests.post(url, json=payload, timeout=15)
    if not r.ok:
        raise RuntimeError(f"텔레그램 전송 실패: {r.status_code} {r.text}")

    print("✅ 텔레그램 전송 완료")
    return True


def main():
    if not JSON_PATH.exists():
        raise FileNotFoundError(f"{JSON_PATH} 파일이 없습니다. 먼저 스크리너를 실행하세요.")

    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    msg = build_message(data)
    print(msg)
    send_telegram(msg)


if __name__ == "__main__":
    main()
