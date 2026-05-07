"""
news_collector.py - RSS 기반 뉴스 수집기
==========================================
실전 검증된 14개 RSS 피드로 안정적 수집
→ 셀렉터 깨짐 없음 / 봇차단 없음 / ~1000건/회

[피드 구성]
  연합뉴스  2개  (economy, market)
  구글뉴스  7개  (시황 + 섹터별 키워드)
  매일경제  2개  (finance, stock)
  한국경제  2개  (economy, finance)
  아시아경제 1개 (stock)

의존성: pip install requests beautifulsoup4 lxml
"""

import re
import logging
import time
from datetime import datetime, date, timezone, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# 1. 실전 검증 RSS 피드 목록
# ══════════════════════════════════════════════════════════════

RSS_SOURCES: dict[str, str] = {
    # 연합뉴스
    "yonhap_economy": "https://www.yna.co.kr/rss/economy.xml",
    "yonhap_market":  "https://www.yna.co.kr/rss/market.xml",
    # 구글뉴스 (시황 + 섹터 키워드)
    "google_market":  "https://news.google.com/rss/search?q=코스피+코스닥+증시&hl=ko&gl=KR&ceid=KR:ko",
    "google_hbm":     "https://news.google.com/rss/search?q=HBM+반도체+수주&hl=ko&gl=KR&ceid=KR:ko",
    "google_defense": "https://news.google.com/rss/search?q=방산+수출+계약&hl=ko&gl=KR&ceid=KR:ko",
    "google_ship":    "https://news.google.com/rss/search?q=LNG선+조선+수주&hl=ko&gl=KR&ceid=KR:ko",
    "google_nuclear": "https://news.google.com/rss/search?q=원전+SMR+수주&hl=ko&gl=KR&ceid=KR:ko",
    "google_bio":     "https://news.google.com/rss/search?q=FDA+임상+기술수출&hl=ko&gl=KR&ceid=KR:ko",
    "google_issue":   "https://news.google.com/rss/search?q=유상증자+실적+어닝&hl=ko&gl=KR&ceid=KR:ko",
    # 매일경제
    "mk_finance":     "https://www.mk.co.kr/rss/30000001/",
    "mk_stock":       "https://www.mk.co.kr/rss/50200011/",
    # 한국경제
    "hankyung_eco":   "https://www.hankyung.com/feed/economy",
    "hankyung_stock": "https://www.hankyung.com/feed/finance",
    # 아시아경제
    "asiae_stock":    "https://www.asiae.co.kr/rss/stock.htm",
}

SOURCE_PRESS: dict[str, str] = {
    "yonhap_economy": "연합뉴스",  "yonhap_market":  "연합뉴스",
    "google_market":  "구글뉴스",  "google_hbm":     "구글뉴스",
    "google_defense": "구글뉴스",  "google_ship":    "구글뉴스",
    "google_nuclear": "구글뉴스",  "google_bio":     "구글뉴스",
    "google_issue":   "구글뉴스",
    "mk_finance":     "매일경제",  "mk_stock":       "매일경제",
    "hankyung_eco":   "한국경제",  "hankyung_stock": "한국경제",
    "asiae_stock":    "아시아경제",
}

HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/rss+xml, application/xml, text/xml, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
}

KST = timezone(timedelta(hours=9))


# ══════════════════════════════════════════════════════════════
# 2. pubDate 파싱 유틸
# ══════════════════════════════════════════════════════════════

def _parse_pubdate(raw: str) -> tuple:
    """RSS pubDate → (HH:MM 문자열, date 객체 or None)"""
    if not raw:
        return "", None

    raw = raw.strip()

    # ISO 8601: "2026-05-07T16:10:00+09:00"
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(raw[:25], fmt)
            if dt.tzinfo:
                dt = dt.astimezone(KST)
            return dt.strftime("%H:%M"), dt.date()
        except ValueError:
            pass

    # RFC 2822: "Wed, 07 May 2026 16:10:00 +0900"
    try:
        raw_clean = re.sub(r"\s+(UT|GMT|EST|PST|KST)$", "", raw)
        dt = datetime.strptime(raw_clean, "%a, %d %b %Y %H:%M:%S %z")
        dt = dt.astimezone(KST)
        return dt.strftime("%H:%M"), dt.date()
    except ValueError:
        pass

    # 시간만 추출
    m = re.search(r"(\d{1,2}:\d{2})", raw)
    return (m.group(1) if m else ""), None


# ══════════════════════════════════════════════════════════════
# 3. 단일 RSS 파싱
# ══════════════════════════════════════════════════════════════

def fetch_rss(url: str, source_name: str = "") -> list:
    """단일 RSS URL → 기사 리스트"""
    press    = SOURCE_PRESS.get(source_name, source_name)
    articles = []

    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        resp = session.get(url, timeout=10, allow_redirects=True)
        resp.raise_for_status()

        items = []
        for parser in ("lxml-xml", "xml", "html.parser"):
            try:
                soup  = BeautifulSoup(resp.content, parser)
                items = soup.find_all("item")
                if items:
                    break
            except Exception:
                continue

        for item in items:
            # 제목
            title_tag = item.find("title")
            title = title_tag.get_text(strip=True) if title_tag else ""
            title = re.sub(r"<!\[CDATA\[|\]\]>", "", title).strip()
            title = re.sub(r"<[^>]+>", "", title).strip()
            if not title or len(title) < 5:
                continue

            # URL
            link_tag = item.find("link") or item.find("guid")
            art_url  = link_tag.get_text(strip=True) if link_tag else ""

            # 발행일
            pub_tag = (item.find("pubDate") or item.find("pubdate")
                       or item.find("dc:date"))
            pub_raw = pub_tag.get_text(strip=True) if pub_tag else ""
            pub_time, pub_date = _parse_pubdate(pub_raw)

            # 요약
            desc_tag = item.find("description")
            summary  = desc_tag.get_text(strip=True) if desc_tag else ""
            summary  = re.sub(r"<[^>]+>", "", summary).strip()[:200]

            # 구글 뉴스: 제목 끝 " - 언론사" 분리
            actual_press = press
            if source_name.startswith("google"):
                m = re.search(r"\s+-\s+([^-]+)$", title)
                if m:
                    actual_press = m.group(1).strip()
                    title = title[:m.start()].strip()

            articles.append({
                "title":        title,
                "summary":      summary,
                "url":          art_url,
                "press":        actual_press,
                "published_at": pub_time,
                "pub_date":     pub_date,
                "source":       source_name,
            })

    except Exception as e:
        logger.warning(f"RSS 파싱 실패 [{source_name}]: {e}")

    return articles


# ══════════════════════════════════════════════════════════════
# 4. 통합 수집
# ══════════════════════════════════════════════════════════════

def collect_all(
    sources:    Optional[dict] = None,
    delay:      float = 0.3,
    today_only: bool  = True,
) -> list:
    """
    전체 RSS 수집 → 중복 제거 → (선택) 오늘 기사 필터

    Args:
        sources    : RSS 딕셔너리. 기본값: RSS_SOURCES (14개)
        delay      : 요청 간 딜레이 (초)
        today_only : True면 오늘 날짜 기사만 반환

    Returns:
        list[dict]  title / summary / url / press / published_at / pub_date / source
    """
    sources      = sources or RSS_SOURCES
    all_articles = []
    today        = date.today()

    for name, url in sources.items():
        batch = fetch_rss(url, source_name=name)
        all_articles.extend(batch)
        logger.info(f"  [{name:16s}] {len(batch):4d}건")
        time.sleep(delay)

    # 오늘 기사 필터 (날짜 파싱 실패한 것은 포함)
    if today_only:
        filtered = [
            a for a in all_articles
            if a.get("pub_date") is None or a["pub_date"] == today
        ]
        skipped = len(all_articles) - len(filtered)
        if skipped:
            logger.info(f"  오래된 기사 제외: {skipped}건")
        all_articles = filtered

    # URL 기준 중복 제거
    seen, unique = set(), []
    for a in all_articles:
        key = a.get("url") or a.get("title", "")
        if key and key not in seen:
            seen.add(key)
            unique.append(a)

    logger.info(f"최종 수집: {len(unique)}건")
    return unique


# ══════════════════════════════════════════════════════════════
# 5. 통계 출력
# ══════════════════════════════════════════════════════════════

def print_stats(articles: list) -> None:
    from collections import Counter
    print(f"\n  📰 수집 결과: 총 {len(articles)}건")
    print(f"  {'─'*50}")

    src_cnt = Counter(a.get("source", "?") for a in articles)
    for src, cnt in src_cnt.most_common():
        bar = "█" * min(cnt // 3, 20)
        print(f"    {src:18s}  {cnt:4d}건  {bar}")

    time_cnt = {"장마감후(15:30↑)": 0, "장중(09~15:30)": 0,
                "오전(~09:00)": 0, "시간미상": 0}
    for a in articles:
        t = a.get("published_at", "")
        m = re.match(r"(\d{1,2}):(\d{2})", t)
        if not m:
            time_cnt["시간미상"] += 1
        else:
            h, mn = int(m.group(1)), int(m.group(2))
            if h > 15 or (h == 15 and mn >= 30):
                time_cnt["장마감후(15:30↑)"] += 1
            elif h >= 9:
                time_cnt["장중(09~15:30)"] += 1
            else:
                time_cnt["오전(~09:00)"] += 1

    print(f"\n  ⏰ 시간대 분포")
    for label, cnt in time_cnt.items():
        if cnt:
            print(f"    {label:18s}  {cnt:4d}건")
    print(f"  {'─'*50}")


# ══════════════════════════════════════════════════════════════
# 6. 단독 실행
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )

    print("\n" + "=" * 55)
    print("  📡 RSS 뉴스 수집 테스트")
    print("=" * 55)

    articles = collect_all(delay=0.3, today_only=True)
    print_stats(articles)

    print(f"\n  최신 10건 샘플")
    print(f"  {'─'*55}")
    for a in articles[:10]:
        t   = a.get("published_at", "?????")
        src = a.get("source", "")[:14]
        prs = a.get("press", "")[:10]
        print(f"  [{t}] [{src:14s}] [{prs:10s}] {a['title'][:40]}")
