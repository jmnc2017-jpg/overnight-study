"""
뉴스 점수화 룰셋 v4 - 종가배팅 (19~20시 매수 → 08~09시 매도)
==============================================================
핵심 질문: "좋은 뉴스냐?" 가 아니라 "내일 아침 새로 살 이유가 있냐?"

[v4 변경사항]
  FIX-1. 섹터 키워드 겹침 버그 수정
         - 방산 "수출" → "방산수출", "방산계약", "K방산수출" 등 구체화
         - 바이오 "기술수출" 과 방산 "수출" 충돌 완전 분리
         - 조선 "수주" 가 다른 섹터로 번지지 않도록 섹터 매칭 로직 개선
           (SECTOR 키워드는 섹터 판별 전용 / ISSUE 키워드는 점수 전용으로 역할 분리)

  FIX-2. 이슈 강도 최고점 로직 명확화
         - 기존: 단순 max() → 동점 키워드 전부 issues_hit 에 표시되나 점수는 1개만
         - 변경: best_kw / best_score 명시 추적, issues_hit = [최고점 키워드 + 나머지 매칭]
         - 디버그 출력에서 "어떤 키워드가 이슈 점수를 결정했는지" 명확히 표시

  ADD-3. 크롤러 연동 뼈대 추가
         - NaverFinanceCrawler: 시황종합 + 기업종목 뉴스 크롤링
         - run_pipeline(): 크롤링 → 점수화 → 출력 → CSV 저장 원스톱 실행

[점수 구성 - 100점 만점]
  1. 이슈 강도      30점  (최고점 키워드 1개만 채택)
  2. 종목 직접성    20점
  3. 시간 신선도    15점
  4. 시장 반응      15점
  5. 섹터 확산성    10점
  6. 악재 차감     -30점까지 (횡령/배임은 -50)

[등급]
  S  : 85점 이상  → 갭 후보 핵심
  A  : 70~84점   → 관찰 우선
  B  : 55~69점   → 참고
  제외: 55점 미만

[강한 후보 4조건]
  ① 15:30 이후 장마감 후 발생
  ② 종목명 제목 직접 언급
  ③ 시간외 반응 있음
  ④ 섹터 동반 움직임 있음
"""

import re
import time as time_module
import logging
import pandas as pd
from dataclasses import dataclass, field
from datetime import datetime, time, date
from typing import Optional

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# ══════════════════════════════════════════════════════════════
# 1. 키워드 사전
# ══════════════════════════════════════════════════════════════

class KeywordDict:

    # ── FIX-1: 섹터 키워드 ────────────────────────────────────
    # 규칙: 섹터 키워드는 "해당 섹터에서만 쓰이는 단어"로 구체화
    #       짧고 범용적인 단어("수출", "수주")는 섹터 키워드에서 제거하고
    #       섹터명+행위 조합("방산수출", "LNG선 수주")으로 대체
    SECTOR: dict[str, list[str]] = {
        "반도체": [
            "HBM", "HBM3E", "HBM4", "CXL", "DDR5",
            "엔비디아", "NVIDIA", "마이크론", "Micron", "TSMC",
            "SK하이닉스", "온디바이스", "NPU", "파운드리", "고대역폭메모리",
        ],
        "조선": [
            "LNG선", "VLCC", "컨테이너선", "암모니아선", "MRO",
            "해양플랜트", "카타르", "미국 해군", "美 해군", "선가",
            "HD현대중공업", "한화오션", "삼성중공업",
            # "수주" 단독 제거 → 아래 조선 수주 전용 복합 키워드로 대체
            "LNG선 수주", "선박 수주", "조선 수주",
        ],
        "방산": [
            # FIX: "수출" 단독 제거 → 방산 특화 복합 키워드로 대체
            "방산수출", "방산계약", "K방산", "K방산수출",
            "폴란드", "사우디", "UAE", "루마니아", "호주",
            "K2", "K9", "천무", "레드백", "AS21",
            "방위산업", "한화에어로스페이스", "현대로템", "LIG넥스원",
            "NATO", "방산 수출", "무기 수출",
        ],
        "전력_원전": [
            "원전", "SMR", "소형모듈원전", "변압기", "전력망", "ESS", "송전",
            "두산에너빌리티", "한전", "HD현대일렉트릭",
            "체코 원전", "UAE 원전", "체코원전", "UAE원전",
        ],
        "2차전지": [
            "리튬", "양극재", "음극재", "IRA", "전고체",
            "테슬라", "Tesla",
            "LG에너지솔루션", "삼성SDI", "SK온",
            "에코프로", "포스코퓨처엠", "전해질", "배터리",
        ],
        "바이오": [
            "FDA", "임상3상", "임상2상", "임상1상", "임상시험",
            "품목허가", "기술수출", "L/O", "LO",   # "기술수출"은 바이오 전용 유지
            "신약", "바이오시밀러", "항암제",
            "삼성바이오로직스", "셀트리온",
        ],
    }

    # ── FIX-2: 이슈 강도 키워드 ──────────────────────────────
    # 역할: 점수 산정 전용 (섹터 판별과 완전 분리)
    # 로직: 매칭된 키워드 중 최고점 1개만 issue 점수로 채택
    #       나머지 매칭 키워드는 issues_hit 에 표시만 함
    ISSUE: dict[str, int] = {
        # ── 대형 (+25~30) ──
        "대규모 수주":    30,
        "장기공급계약":   30,
        "공급계약":       28,
        "실적 서프라이즈": 28,
        "어닝서프라이즈": 28,
        "美 해군":        28,
        "미국 해군":      28,
        "최대실적":       27,
        "국가사업":       27,
        "MRO":            27,
        "수주":           25,   # 이슈 점수에는 유지 (섹터 판별에는 미사용)
        "정부정책":       25,
        "정책수혜":       25,
        "FDA 승인":       25,
        "품목허가":       25,
        # ── 중형 (+15~25) ──
        "임상 성공":      23,
        "HBM 수혜":       23,
        "기술수출":       22,
        "AI 수혜":        22,
        "흑자전환":       22,
        "업황 개선":      20,
        "업황개선":       20,
        "공급 부족":      20,
        "턴어라운드":     20,
        "가격 상승":      18,
        "실적개선":       18,
        "글로벌 테마":    18,
        "수출 호조":      17,
        # ── 증권사 리포트 (+10~15) ──
        "목표주가 상향":  15,
        "목표가 상향":    15,
        "강력매수":       15,
        "투자의견 상향":  14,
        "BUY":            12,
        "증권사 리포트":  10,
        # ── 인터뷰/홍보 (+3~8) ──
        "전략발표":       7,
        "사업계획":       6,
        "인터뷰":         5,
        "홍보":           3,
        "비전":           3,
    }

    # ── 악재 키워드 ───────────────────────────────────────────
    NEGATIVE: dict[str, int] = {
        "횡령":           -50,
        "배임":           -50,
        "유상증자":       -30,
        "투자주의":       -30,   # INSTANT_EXCLUDE 와 중복이지만 점수에도 반영
        "적자전환":       -25,
        "실적 쇼크":      -25,
        "어닝쇼크":       -25,
        "전환사채":       -20,
        "신주인수권부사채": -20,
        "대주주 매도":    -20,
        "대주주매도":     -20,
        "CB":             -20,
        "BW":             -20,
        "투자경고":       -18,
        "단기과열":       -15,
        "SELL":           -15,
        "과징금":         -15,
        "투자의견 하향":  -14,
        "소송":           -12,
        "목표가 하향":    -12,
        "리콜":           -12,
        "제재":           -13,
        "공급차질":       -10,
    }

    REACTION_POS: list[str] = [
        "급등", "상한가", "강세", "신고가", "외국인 순매수", "기관 순매수",
    ]
    REACTION_NEG: list[str] = [
        "급락", "하한가", "약세", "신저가", "외국인 순매도", "기관 순매도",
    ]

    INSTANT_EXCLUDE: list[str] = [
        "관리종목", "거래정지", "상장폐지", "투자주의",
    ]


# ══════════════════════════════════════════════════════════════
# 2. 데이터 클래스
# ══════════════════════════════════════════════════════════════

@dataclass
class ScoreBreakdown:
    issue:      int = 0
    direct:     int = 0
    freshness:  int = 0
    reaction:   int = 0
    spread:     int = 0
    deduction:  int = 0

    @property
    def total(self) -> int:
        return self.issue + self.direct + self.freshness + self.reaction + self.spread + self.deduction

    def detail_str(self) -> str:
        return (
            f"이슈{self.issue:+2d} | 직접성{self.direct:+2d} | "
            f"신선도{self.freshness:+2d} | 반응{self.reaction:+2d} | "
            f"확산{self.spread:+2d} | 악재{self.deduction:+2d}"
        )


@dataclass
class ScoredArticle:
    title:          str
    summary:        str
    url:            str
    press:          str
    published_at:   str

    score:          ScoreBreakdown  = field(default_factory=ScoreBreakdown)
    grade:          str             = "제외"
    sectors:        list[str]       = field(default_factory=list)
    issue_best_kw:  str             = ""       # FIX-2: 이슈 점수 결정 키워드
    issues_hit:     list[str]       = field(default_factory=list)   # 전체 매칭 키워드
    negatives_hit:  list[str]       = field(default_factory=list)
    direct_type:    str             = "매크로"
    strong_flags:   list[str]       = field(default_factory=list)
    excluded:       bool            = False

    @property
    def total(self) -> int:
        return self.score.total


# ══════════════════════════════════════════════════════════════
# 3. 점수 계산기
# ══════════════════════════════════════════════════════════════

class NewsScorer:

    GRADE_TABLE = [(85, "S"), (70, "A"), (55, "B")]

    def __init__(
        self,
        watchlist:      Optional[dict[str, str]] = None,
        subsidiary_map: Optional[dict[str, str]] = None,
    ):
        self.kd  = KeywordDict()
        self.wl  = watchlist      or {}
        self.sub = subsidiary_map or {}

    def score(
        self,
        article,
        market_data:         Optional[dict] = None,
        sector_spread_count: int = 0,
        keyword_history:     Optional[dict] = None,   # 반복뉴스 감점용
    ) -> ScoredArticle:

        title   = _get(article, "title",        "")
        summary = _get(article, "summary",      "")
        url     = _get(article, "url",          "")
        press   = _get(article, "press",        "")
        pub     = _get(article, "published_at", "")
        text    = title + " " + summary

        # 즉시 제외
        for kw in self.kd.INSTANT_EXCLUDE:
            if kw in text:
                return ScoredArticle(
                    title=title, summary=summary, url=url, press=press,
                    published_at=pub, excluded=True, grade="제외",
                    negatives_hit=[f"{kw}(즉시제외)"],
                )

        # 6개 차원 계산
        issue_score, best_kw, all_hits = self._score_issue(text)
        deduction = self._score_deduction(text)

        # 악재 하한 적용
        has_extreme = any(kw in text for kw in ["횡령", "배임"])
        deduction   = max(deduction, -50 if has_extreme else -30)

        # [4] 시장반응: market_data 있으면 market_score 우선 사용
        if market_data and "market_score" in market_data:
            reaction = market_data["market_score"]
        else:
            reaction = self._score_reaction(text, market_data)

        bd = ScoreBreakdown(
            issue     = issue_score,
            direct    = self._score_direct(text),
            freshness = self._score_freshness(pub),
            reaction  = reaction,
            spread    = self._score_spread(sector_spread_count),
            deduction = deduction,
        )

        # 반복뉴스 감점 (GPT 2순위)
        repeat_deduction = 0
        repeat_kws       = []
        if keyword_history is not None:
            try:
                from market_signal import calc_repeat_deduction
                repeat_deduction, repeat_kws = calc_repeat_deduction(title, keyword_history)
            except ImportError:
                pass

        sectors = self._find_sectors(text)
        total_adjusted = bd.total + repeat_deduction
        grade   = self._calc_grade(total_adjusted)
        flags   = self._strong_flags(pub, text, market_data, sector_spread_count)

        art = ScoredArticle(
            title=title, summary=summary, url=url, press=press, published_at=pub,
            score=bd, grade=grade, sectors=sectors,
            issue_best_kw  = best_kw,
            issues_hit     = all_hits,
            negatives_hit  = self._find_neg_hits(text) + (repeat_kws if repeat_kws else []),
            direct_type    = self._classify_direct(text),
            strong_flags   = flags,
            excluded       = False,
        )
        # 반복감점 저장 (출력용)
        art._repeat_deduction = repeat_deduction
        return art

    # ── [1] 이슈 강도 - FIX-2 ────────────────────────────────
    def _score_issue(self, text: str) -> tuple[int, str, list[str]]:
        """
        Returns:
            (best_score, best_keyword, all_matched_keywords)
        best_score : 이슈 점수 (최고점 1개만)
        best_keyword : 점수 결정 키워드 (디버그용)
        all_matched : 매칭된 전체 키워드 목록 (표시용)
        """
        best_score = 0
        best_kw    = ""
        all_hits   = []

        for kw, pts in self.kd.ISSUE.items():
            if kw in text:
                all_hits.append(f"{kw}({pts:+d})")
                if pts > best_score:
                    best_score = pts
                    best_kw    = kw

        return min(best_score, 30), best_kw, all_hits

    # ── [2] 종목 직접성 ───────────────────────────────────────
    def _score_direct(self, text: str) -> int:
        if any(s in text for s in self.wl):   return 20
        if any(s in text for s in self.sub):  return 15
        if self._sector_hit_count(text) >= 2: return 10
        return 5

    def _classify_direct(self, text: str) -> str:
        if any(s in text for s in self.wl):   return "직접언급"
        if any(s in text for s in self.sub):  return "계열사"
        return "섹터전체" if self._sector_hit_count(text) >= 2 else "매크로"

    def _sector_hit_count(self, text: str) -> int:
        return sum(
            1 for kws in self.kd.SECTOR.values()
            for kw in kws if kw in text
        )

    # ── [3] 시간 신선도 ───────────────────────────────────────
    def _score_freshness(self, pub: str) -> int:
        t = _parse_time(pub)
        if t is None:               return 5
        if t >= time(15, 30):       return 15   # 장마감 후
        if t >= time(13,  0):       return 10   # 오후 장중
        if t >= time( 6,  0):       return 5    # 오전
        return 2                                # 새벽/전일

    # ── [4] 시장 반응 ─────────────────────────────────────────
    def _score_reaction(self, text: str, md: Optional[dict]) -> int:
        if md:
            chg = md.get("change_pct", 0)
            ah  = md.get("after_hours_pct", 0)
            if chg > 0 and ah > 0:    return 15
            if chg == 0 and ah > 0:   return 12
            if chg > 0 and ah <= 0:   return 5
            if chg < 0:               return -10
            return 2
        for kw in self.kd.REACTION_POS:
            if kw in text: return 8
        for kw in self.kd.REACTION_NEG:
            if kw in text: return -5
        return 0

    # ── [5] 섹터 확산성 ───────────────────────────────────────
    def _score_spread(self, count: int) -> int:
        if count >= 3: return 10
        if count == 2: return 7
        if count == 1: return 5
        return 3

    # ── [6] 악재 차감 ─────────────────────────────────────────
    def _score_deduction(self, text: str) -> int:
        return sum(
            pts for kw, pts in self.kd.NEGATIVE.items()
            if kw in text and pts != -99
        )

    def _find_neg_hits(self, text: str) -> list[str]:
        return [
            f"{kw}({pts:+d})" for kw, pts in self.kd.NEGATIVE.items()
            if kw in text and pts != -99
        ]

    # ── 강한 후보 4조건 ───────────────────────────────────────
    def _strong_flags(self, pub, text, md, spread_count) -> list[str]:
        flags = []
        t = _parse_time(pub)
        if t and t >= time(15, 30):
            flags.append("①장마감후")
        if any(s in text for s in self.wl):
            flags.append("②직접언급")
        if md and md.get("after_hours_pct", 0) > 0:
            flags.append("③시간외반응")
        elif any(kw in text for kw in self.kd.REACTION_POS):
            flags.append("③시간외반응(추정)")
        if spread_count >= 2:
            flags.append("④섹터동반")
        return flags

    # ── FIX-1: 섹터 매칭 ─────────────────────────────────────
    def _find_sectors(self, text: str) -> list[str]:
        """
        섹터별로 매칭된 키워드 수를 세어 2개 이상일 때만 해당 섹터로 인정.
        단일 키워드 오염(예: "수출" 하나로 방산 섹터 매칭) 방지.
        """
        matched = []
        for sector, kws in self.kd.SECTOR.items():
            hits = sum(1 for kw in kws if kw in text)
            # 바이오/방산 같이 특화 키워드가 명확한 섹터는 1개도 인정
            # 단, "수출", "수주" 같은 범용 단어만 걸리는 경우 제외하기 위해
            # 핵심 키워드 (길이 3자 이상 or 영문) 가 1개라도 있어야 인정
            core_hits = sum(
                1 for kw in kws
                if kw in text and (len(kw) >= 3 or re.search(r"[A-Za-z]", kw))
            )
            if core_hits >= 1:
                matched.append(sector)
        return matched

    # ── 등급 ──────────────────────────────────────────────────
    @staticmethod
    def _calc_grade(total: int) -> str:
        for threshold, grade in NewsScorer.GRADE_TABLE:
            if total >= threshold:
                return grade
        return "제외"


# ══════════════════════════════════════════════════════════════
# 4. ADD-3: 네이버 금융 크롤러
# ══════════════════════════════════════════════════════════════

class NaverFinanceCrawler:
    """
    네이버 금융 뉴스 크롤러 v3
    ─────────────────────────────────────────────────────
    [네이버 봇차단 대응]
    finance.naver.com/news 는 requests 직접 요청 시 빈 HTML 반환 (봇 차단)
    → 아래 3가지 방법으로 우회

    방법 A. 네이버 뉴스 검색 (search.naver.com) - 가장 안정적
    방법 B. 네이버 금융 RSS 피드
    방법 C. 섹터별 키워드 검색 (당일 호재/악재 타겟 수집)

    의존성: pip install requests beautifulsoup4
    """

    HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.naver.com/",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    }

    # 방법 A: 네이버 뉴스 검색 쿼리 목록
    SEARCH_QUERIES = [
        # 시장 전체
        "코스피 코스닥 증시",
        "증시 시황",
        # 섹터 핵심 키워드
        "HBM 반도체 수주",
        "LNG선 조선 수주",
        "방산 수출 계약",
        "원전 SMR 전력",
        "배터리 양극재 IRA",
        "FDA 임상 기술수출",
        # 이슈성
        "실적 서프라이즈 어닝",
        "유상증자 CB BW",
    ]

    # 방법 B: 네이버 금융 RSS
    RSS_URLS = [
        "https://finance.naver.com/news/news_list.naver?mode=maknews",
        "https://finance.naver.com/news/news_list.naver?mode=companynews",
    ]

    def __init__(self, delay: float = 0.8):
        self.delay   = delay
        self._session = None

    def _get_session(self):
        """requests Session with cookie + header"""
        try:
            import requests
        except ImportError:
            raise ImportError("pip install requests")
        if self._session is None:
            s = requests.Session()
            s.headers.update(self.HEADERS)
            # 네이버 메인 먼저 방문해서 쿠키 획득
            try:
                s.get("https://www.naver.com", timeout=5)
            except Exception:
                pass
            self._session = s
        return self._session

    # ── 방법 A: 네이버 뉴스 검색 ─────────────────────────────

    def fetch_by_search(self, query: str, pages: int = 2) -> list[dict]:
        """
        search.naver.com 뉴스 검색으로 수집
        당일(pd=4) 최신순(sort=1) 필터
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            raise ImportError("pip install beautifulsoup4")

        session  = self._get_session()
        articles = []

        for page in range(1, pages + 1):
            start = 1 if page == 1 else (page - 1) * 10 + 1
            params = {
                "where": "news",
                "query": query,
                "sort":  "1",    # 최신순
                "pd":    "4",    # 오늘
                "start": start,
            }
            try:
                resp = session.get(
                    "https://search.naver.com/search.naver",
                    params=params, timeout=10
                )
                resp.raise_for_status()
                soup = BeautifulSoup(resp.text, "html.parser")

                items = soup.select("div.news_area")
                for item in items:
                    a = item.select_one("a.news_tit")
                    if not a:
                        continue
                    title   = a.get_text(strip=True)
                    url     = a.get("href", "")
                    press   = (item.select_one("a.info.press") or item.select_one("a.press") or a)
                    press   = press.get_text(strip=True) if press != a else ""
                    date_el = item.select_one("span.info")
                    pub     = date_el.get_text(strip=True) if date_el else ""
                    summary_el = item.select_one("a.dsc_txt_wrap, div.dsc_wrap a, .api_txt_lines")
                    summary = summary_el.get_text(strip=True) if summary_el else ""

                    if title and len(title) > 5:
                        articles.append({
                            "title": title, "summary": summary,
                            "url": url, "press": press,
                            "published_at": pub,
                        })

            except Exception as e:
                logger.warning(f"검색 실패 [{query} p{page}]: {e}")

            time_module.sleep(self.delay)

        return articles

    # ── 방법 B: RSS 피드 ──────────────────────────────────────

    def fetch_rss(self, rss_url: str) -> list[dict]:
        """
        네이버 금융 RSS 파싱
        RSS는 JavaScript 렌더링 불필요 → 봇차단 낮음
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            raise ImportError("pip install beautifulsoup4")

        session  = self._get_session()
        articles = []

        try:
            resp = session.get(rss_url, timeout=10)
            resp.raise_for_status()
            # RSS XML 파싱
            soup = BeautifulSoup(resp.content, "xml")
            items = soup.find_all("item")

            if not items:
                # xml 파서 없으면 html.parser 로 재시도
                soup  = BeautifulSoup(resp.content, "html.parser")
                items = soup.find_all("item")

            for item in items:
                title   = item.find("title")
                link    = item.find("link") or item.find("guid")
                pub     = item.find("pubDate") or item.find("dc:date")
                desc    = item.find("description")

                title   = title.get_text(strip=True)   if title else ""
                url     = link.get_text(strip=True)    if link  else ""
                pub     = pub.get_text(strip=True)     if pub   else ""
                summary = desc.get_text(strip=True)    if desc  else ""

                # pubDate "Wed, 07 May 2025 16:10:00 +0900" → "16:10" 추출
                t_match = re.search(r"(\d{2}:\d{2}):\d{2}", pub)
                pub_fmt = t_match.group(1) if t_match else pub

                if title and len(title) > 5:
                    articles.append({
                        "title": title, "summary": summary,
                        "url": url, "press": "",
                        "published_at": pub_fmt,
                    })

        except Exception as e:
            logger.warning(f"RSS 파싱 실패 [{rss_url}]: {e}")

        return articles

    # ── HTML 디버그 ───────────────────────────────────────────

    def debug_html(self, url: str = None) -> None:
        """
        실제 응답 HTML 출력 (봇차단 여부 확인용)
        python news_scorer_v4.py --debug 로 실행
        """
        url = url or "https://finance.naver.com/news/news_list.naver?mode=maknews"
        session = self._get_session()
        try:
            resp = session.get(url, timeout=10)
            print(f"\n[DEBUG] status={resp.status_code}  encoding={resp.encoding}")
            print(f"[DEBUG] URL: {url}")
            print("[DEBUG] 응답 앞 2000자:")
            print(resp.text[:2000])
            print("\n[DEBUG] 'articleSubject' 포함 여부:", "articleSubject" in resp.text)
            print("[DEBUG] 'news_area' 포함 여부:", "news_area" in resp.text)
            print("[DEBUG] 'news_tit' 포함 여부:", "news_tit" in resp.text)
        except Exception as e:
            print(f"[DEBUG] 요청 실패: {e}")

    # ── 통합 수집 ─────────────────────────────────────────────

    def fetch_all(self, pages: int = 2) -> list[dict]:
        """
        방법 A (검색) + 방법 B (RSS) 통합 수집
        기대 수집량: 100~200건
        """
        all_articles = []

        # 방법 B: RSS 먼저 (빠름)
        logger.info("📡 RSS 수집 중...")
        for rss_url in self.RSS_URLS:
            batch = self.fetch_rss(rss_url)
            all_articles.extend(batch)
            logger.info(f"  [RSS] {rss_url.split('=')[-1]}: {len(batch)}건")
            time_module.sleep(self.delay)

        # 방법 A: 검색 쿼리
        logger.info("🔍 검색 수집 중...")
        for query in self.SEARCH_QUERIES:
            batch = self.fetch_by_search(query, pages=pages)
            all_articles.extend(batch)
            logger.info(f"  [검색] '{query}': {len(batch)}건")
            time_module.sleep(self.delay)

        # 중복 제거 (URL 기준)
        seen, unique = set(), []
        for a in all_articles:
            key = a.get("url", "")
            if key and key not in seen:
                seen.add(key)
                unique.append(a)
            elif not key:
                unique.append(a)   # URL 없는 것도 포함

        logger.info(f"총 {len(unique)}건 수집 완료 (중복 제거 후)")
        return unique


# ══════════════════════════════════════════════════════════════
# 5. 배치 파이프라인
# ══════════════════════════════════════════════════════════════

class ScoringPipeline:

    def __init__(self, scorer: NewsScorer):
        self.scorer = scorer

    def run(
        self,
        articles:           list,
        market_data_map:    Optional[dict[str, dict]] = None,
        sector_spread_map:  Optional[dict[str, int]]  = None,
        keyword_history:    Optional[dict]             = None,
    ) -> list[ScoredArticle]:
        result = []
        for art in articles:
            url = _get(art, "url", "")
            md  = (market_data_map  or {}).get(url)
            sc  = (sector_spread_map or {}).get(url, 0)
            result.append(self.scorer.score(
                art,
                market_data=md,
                sector_spread_count=sc,
                keyword_history=keyword_history,
            ))
        return result

    def filter_grade(self, scored: list[ScoredArticle], min_grade: str = "B") -> list[ScoredArticle]:
        order     = {"S": 4, "A": 3, "B": 2, "제외": 0}
        threshold = order.get(min_grade, 0)
        return [a for a in scored if order.get(a.grade, 0) >= threshold and not a.excluded]

    def strong_candidates(self, scored: list[ScoredArticle]) -> list[ScoredArticle]:
        """4조건 중 3개 이상 & A등급 이상"""
        return [
            a for a in scored
            if not a.excluded
            and len(a.strong_flags) >= 3
            and a.grade in ("S", "A")
        ]

    def top(self, scored: list[ScoredArticle], n: int = 20) -> list[ScoredArticle]:
        return sorted(
            [a for a in scored if not a.excluded],
            key=lambda x: x.total, reverse=True
        )[:n]

    def by_sector(self, scored: list[ScoredArticle]) -> dict[str, list[ScoredArticle]]:
        result: dict[str, list] = {}
        for a in scored:
            if a.excluded: continue
            for sec in a.sectors:
                result.setdefault(sec, []).append(a)
            if not a.sectors:
                result.setdefault("기타", []).append(a)
        return result


# ══════════════════════════════════════════════════════════════
# 6. 리포터
# ══════════════════════════════════════════════════════════════

GRADE_EMOJI = {"S": "🚀", "A": "🟢", "B": "🔵", "제외": "⚫"}


class ScoringReporter:

    def print_article(self, a: ScoredArticle, verbose: bool = True) -> None:
        emoji = GRADE_EMOJI.get(a.grade, "⚪")
        flags = " ".join(a.strong_flags) if a.strong_flags else ""
        print(f"\n  {emoji}[{a.grade}] {a.total:+3d}점  {a.title}")
        if verbose:
            print(f"       {a.score.detail_str()}")
            if a.sectors:
                print(f"       섹터: {', '.join(a.sectors)}")
            # FIX-2: 이슈 결정 키워드 명시
            if a.issue_best_kw:
                print(f"       이슈결정: [{a.issue_best_kw}]  전체매칭: {', '.join(a.issues_hit[:4])}")
            if a.negatives_hit:
                print(f"       악재: {', '.join(a.negatives_hit)}")
            if flags:
                print(f"       ★ 강한후보: {flags}")

    def print_summary(self, scored: list[ScoredArticle]) -> None:
        valid    = [a for a in scored if not a.excluded]
        excluded = [a for a in scored if a.excluded]

        grade_cnt: dict[str, int] = {}
        for a in valid:
            grade_cnt[a.grade] = grade_cnt.get(a.grade, 0) + 1

        avg_s = sum(a.total for a in valid) / len(valid) if valid else 0
        total_s = sum(a.total for a in valid)

        print("\n" + "═" * 65)
        print(f"  📊 뉴스 점수화 결과  [{date.today()}]")
        print("═" * 65)
        print(f"  수집 기사    : {len(scored)}건  (제외 {len(excluded)}건)")
        print(f"  평균 점수    : {avg_s:+.1f}점  (합산 {total_s:+d})")
        print(f"  등급 분포    : ", end="")
        for grade in ["S", "A", "B", "제외"]:
            cnt = grade_cnt.get(grade, 0)
            if cnt:
                print(f"{GRADE_EMOJI[grade]}{grade}:{cnt}  ", end="")
        print()

        # 섹터별 평균
        sector_scores: dict[str, list] = {}
        for a in valid:
            for sec in a.sectors:
                sector_scores.setdefault(sec, []).append(a.total)

        if sector_scores:
            print("\n  ── 섹터별 평균 점수 ──────────────────────")
            for sec, scores in sorted(
                sector_scores.items(),
                key=lambda x: sum(x[1]) / len(x[1]), reverse=True
            ):
                avg = sum(scores) / len(scores)
                bar = "█" * min(int(abs(avg) / 4), 12)
                print(f"    {sec:12s}  avg {avg:+5.1f}  ({len(scores)}건)  {bar}")

        print("═" * 65)

    # ── 종목명 추출 유틸 ─────────────────────────────────────
    @staticmethod
    def _extract_stock(article: ScoredArticle) -> str:
        """
        기사에서 종목명 추출.
        direct_type == "직접언급" 인 경우 제목에서 watchlist 종목명 검색.
        없으면 빈 문자열 반환.
        """
        from news_scorer_v4 import DEFAULT_WATCHLIST
        title = article.title
        for name in DEFAULT_WATCHLIST:
            if name in title:
                return name
        return ""

    # ── 종목 기준 그룹핑 ──────────────────────────────────────
    @staticmethod
    def _group_by_stock(candidates: list[ScoredArticle]) -> tuple:
        """
        candidates → (종목별 그룹 dict, 종목미상 리스트)

        Returns:
            stock_groups: {"삼성전자": [article, ...], ...}  (최고점 내림차순 정렬)
            unknown:      [article, ...]  종목명 없는 기사
        """
        from news_scorer_v4 import DEFAULT_WATCHLIST
        stock_groups: dict[str, list] = {}
        unknown: list = []

        for art in candidates:
            matched = ""
            for name in DEFAULT_WATCHLIST:
                if name in art.title:
                    matched = name
                    break
            if matched:
                stock_groups.setdefault(matched, []).append(art)
            else:
                unknown.append(art)

        # 각 종목 그룹 내부를 점수 내림차순 정렬
        for name in stock_groups:
            stock_groups[name].sort(key=lambda x: x.total, reverse=True)

        # 종목 그룹을 최고점 내림차순 정렬
        sorted_groups = dict(
            sorted(stock_groups.items(), key=lambda x: x[1][0].total, reverse=True)
        )
        return sorted_groups, unknown

    def print_strong_candidates(self, pipeline: ScoringPipeline, scored: list[ScoredArticle]) -> None:
        """
        강한 후보를 종목 기준으로 그룹핑해서 출력.
        같은 종목 뉴스가 여러 건이면 최고점 대표 기사 1개만 표시.
        """
        candidates   = pipeline.strong_candidates(scored)
        stock_groups, unknown = self._group_by_stock(candidates)
        total_stocks = len(stock_groups) + (1 if unknown else 0)

        print(f"\n  🎯 강한 후보  총 {len(candidates)}건 → {total_stocks}종목")
        print("  " + "─" * 62)

        # ── 종목별 출력 ───────────────────────────────────────
        for stock_name, arts in stock_groups.items():
            best  = arts[0]                              # 최고점 대표 기사
            count = len(arts)
            avg   = sum(a.total for a in arts) / count
            emoji = GRADE_EMOJI.get(best.grade, "⚪")
            flags = " ".join(best.strong_flags) if best.strong_flags else ""

            print(f"\n  {emoji}[{best.grade}] {best.total:+3d}점  "
                  f"{stock_name} / 관련뉴스 {count}건 / 평균 {avg:.0f}점")
            print(f"       대표뉴스: {best.title[:60]}")
            if flags:
                print(f"       플래그:   {flags}")
            if best.sectors:
                print(f"       섹터:     {', '.join(best.sectors)}")
            # 추가 기사가 있으면 제목만 간략히
            for sub in arts[1:3]:
                print(f"       └ {sub.total:+3d}점  {sub.title[:55]}")
            if count > 3:
                print(f"       └ ... 외 {count-3}건")

        # ── 종목미상 그룹 ─────────────────────────────────────
        if unknown:
            print(f"\n  ⚪[종목미상]  {len(unknown)}건")
            print("  " + "─" * 40)
            for art in sorted(unknown, key=lambda x: x.total, reverse=True):
                emoji = GRADE_EMOJI.get(art.grade, "⚪")
                flags = " ".join(art.strong_flags) if art.strong_flags else ""
                print(f"  {emoji}[{art.grade}] {art.total:+3d}점  {art.title[:60]}")
                if flags:
                    print(f"       플래그: {flags}")

        print("  " + "─" * 62)

    def print_top(self, pipeline: ScoringPipeline, scored: list[ScoredArticle], n: int = 10) -> None:
        """상위 N개 기사 출력 (기사 단위 유지)."""
        tops = pipeline.top(scored, n)
        print(f"\n  📈 점수 상위 {n}개")
        print("  " + "─" * 60)
        for a in tops:
            self.print_article(a)

    def print_top_stocks(self, pipeline: ScoringPipeline, scored: list[ScoredArticle], n: int = 10) -> None:
        """
        종목 기준 Top N 출력 (run.py "오늘 밤 매수 검토" 용)
        같은 종목 여러 기사 → 최고점으로 대표
        """
        candidates   = pipeline.strong_candidates(scored)
        stock_groups, unknown = self._group_by_stock(candidates)

        # 종목 없는 경우 전체 상위 기사에서 종목 추출 시도
        if not stock_groups and not unknown:
            all_valid = [a for a in scored if not a.excluded and a.grade in ("S", "A", "B")]
            stock_groups, unknown = self._group_by_stock(
                sorted(all_valid, key=lambda x: x.total, reverse=True)[:n*3]
            )

        print(f"\n  🏆 오늘 밤 매수 검토 목록  (종목 기준 Top {n})")
        print("  " + "═" * 58)

        rank = 1
        for stock_name, arts in list(stock_groups.items())[:n]:
            best  = arts[0]
            count = len(arts)
            avg   = sum(a.total for a in arts) / count
            emoji = GRADE_EMOJI.get(best.grade, "⚪")
            flags = " ".join(best.strong_flags) if best.strong_flags else "-"
            print(f"  {rank:2d}. {emoji}[{best.grade}] {best.total:+3d}점  {stock_name}")
            print(f"       관련뉴스 {count}건  평균 {avg:.0f}점")
            print(f"       {best.title[:58]}")
            print(f"       {flags}")
            rank += 1

        print("  " + "═" * 58)

    def to_dataframe(self, scored: list[ScoredArticle]) -> pd.DataFrame:
        rows = []
        for a in scored:
            rows.append({
                "grade":         a.grade,
                "total":         a.total,
                "title":         a.title,
                "press":         a.press,
                "published_at":  a.published_at,
                "issue":         a.score.issue,
                "issue_best_kw": a.issue_best_kw,   # FIX-2
                "direct":        a.score.direct,
                "freshness":     a.score.freshness,
                "reaction":      a.score.reaction,
                "spread":        a.score.spread,
                "deduction":     a.score.deduction,
                "sectors":       "|".join(a.sectors),
                "issues_hit":    "|".join(a.issues_hit[:4]),
                "negatives_hit": "|".join(a.negatives_hit),
                "direct_type":   a.direct_type,
                "strong_flags":  " ".join(a.strong_flags),
                "excluded":      a.excluded,
                "url":           a.url,
            })
        return pd.DataFrame(rows)

    def save_csv(self, scored: list[ScoredArticle], path: str = None) -> str:
        path = path or f"news_scored_{date.today().isoformat()}.csv"
        self.to_dataframe(scored).to_csv(path, index=False, encoding="utf-8-sig")
        print(f"\n  💾 저장: {path}")
        return path


# ══════════════════════════════════════════════════════════════
# 7. 유틸
# ══════════════════════════════════════════════════════════════

def _get(obj, key: str, default=""):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _parse_time(s: str) -> Optional[time]:
    for fmt in ("%H:%M", "%Y-%m-%d %H:%M", "%m/%d %H:%M", "%Y.%m.%d %H:%M"):
        try:
            return datetime.strptime((s or "").strip(), fmt).time()
        except (ValueError, AttributeError):
            continue
    m = re.search(r"(\d{1,2}):(\d{2})", s or "")
    if m:
        try:
            return time(int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    return None


DEFAULT_WATCHLIST: dict[str, str] = {
    "SK하이닉스":       "반도체",
    "삼성전자":         "반도체",
    "HD현대중공업":     "조선",
    "한화오션":         "조선",
    "삼성중공업":       "조선",
    "한화에어로스페이스": "방산",
    "현대로템":         "방산",
    "LIG넥스원":        "방산",
    "두산에너빌리티":   "전력_원전",
    "HD현대일렉트릭":   "전력_원전",
    "LG에너지솔루션":   "2차전지",
    "삼성SDI":          "2차전지",
    "에코프로비엠":     "2차전지",
    "삼성바이오로직스": "바이오",
    "셀트리온":         "바이오",
}


def make_scorer(
    extra_watchlist: Optional[dict[str, str]] = None,
    subsidiary_map:  Optional[dict[str, str]] = None,
) -> NewsScorer:
    wl = {**DEFAULT_WATCHLIST, **(extra_watchlist or {})}
    return NewsScorer(watchlist=wl, subsidiary_map=subsidiary_map or {})


# ══════════════════════════════════════════════════════════════
# 8. 원스톱 실행 함수 (크롤러 연동)
# ══════════════════════════════════════════════════════════════

def run_pipeline(
    pages:           int  = 3,
    save_csv:        bool = True,
    verbose:         bool = True,
    extra_watchlist: Optional[dict[str, str]] = None,
    use_market_data: bool = True,
    target_date:     Optional[str] = None,
) -> list[ScoredArticle]:
    """
    RSS 수집 → FDR 시장반응 → 점수화 → 출력 → CSV 저장

    Args:
        pages           : 카테고리별 크롤링 페이지 수 (기본 3)
        save_csv        : CSV 저장 여부
        verbose         : 상세 출력 여부
        extra_watchlist : 추가 종목 {"종목명": "섹터"}
        use_market_data : FDR 시장 데이터 연동 여부 (기본 True)
        target_date     : 주가 기준일 "YYYYMMDD" (기본 오늘)

    Usage:
        from news_scorer_v4 import run_pipeline
        scored = run_pipeline(pages=5)
    """
    # ── 1. 뉴스 수집 (RSS 우선) ──────────────────────────────
    logger.info("🚀 뉴스 수집 시작 (RSS)")
    try:
        from news_collector import collect_all
        articles = collect_all(delay=0.3)
    except ImportError:
        logger.warning("news_collector.py 없음 → 기존 크롤러 사용")
        crawler  = NaverFinanceCrawler(delay=0.5)
        articles = crawler.fetch_all(pages=pages)

    if not articles:
        logger.warning("수집된 기사 없음.")
        return []

    logger.info(f"수집 완료: {len(articles)}건")

    # ── 2. FDR 시장 반응 실데이터 ───────────────────────────
    market_data_map:   dict[str, dict] = {}
    sector_spread_map: dict[str, int]  = {}

    if use_market_data:
        try:
            from market_signal import collect_market_signals
            watchlist = {**DEFAULT_WATCHLIST, **(extra_watchlist or {})}
            market_data_map, sector_spread_map = collect_market_signals(
                articles=articles,
                watchlist=watchlist,
                td=target_date,
                verbose=verbose,
            )
        except ImportError:
            logger.warning("market_signal.py 없음 → 텍스트 추론으로 대체")
        except Exception as e:
            logger.warning(f"시장 데이터 수집 실패: {e}")

    # ── 3. 점수화 ────────────────────────────────────────────
    logger.info(f"📊 점수화 시작 ({len(articles)}건)")
    scorer   = make_scorer(extra_watchlist)
    pipeline = ScoringPipeline(scorer)
    reporter = ScoringReporter()

    scored = pipeline.run(articles, market_data_map, sector_spread_map)

    # ── 4. 출력 ──────────────────────────────────────────────
    reporter.print_summary(scored)

    if verbose:
        reporter.print_strong_candidates(pipeline, scored)
        reporter.print_top(pipeline, scored, n=10)

    if save_csv:
        reporter.save_csv(scored)

    return scored


# ══════════════════════════════════════════════════════════════
# 9. 더미 데모 (크롤러 없이 동작 확인)
# ══════════════════════════════════════════════════════════════

def run_demo():
    DUMMY = [
        # FIX-1 검증: 셀트리온은 바이오만, 방산 섹터 오염 X
        {"title": "셀트리온, FDA 품목허가 임상3상 기술수출 L/O 계약",
         "summary": "", "url": "u1", "press": "파이낸셜뉴스", "published_at": "18:00"},
        # FIX-1 검증: "방산수출" 키워드로만 방산 섹터 매칭
        {"title": "현대로템, 폴란드 K2 전차 방산수출 방산계약 체결",
         "summary": "", "url": "u2", "press": "연합뉴스", "published_at": "15:55"},
        # FIX-2 검증: 이슈결정 키워드 명확히 표시
        {"title": "한화오션, 美 해군 MRO 대규모 수주 계약 체결",
         "summary": "", "url": "u3", "press": "매일경제", "published_at": "16:10"},
        {"title": "SK하이닉스, HBM3E 엔비디아 공급계약… 실적 서프라이즈 전망",
         "summary": "", "url": "u4", "press": "한국경제", "published_at": "15:48"},
        {"title": "두산에너빌리티, 체코 SMR 원전 우선협상 선정",
         "summary": "", "url": "u5", "press": "조선일보", "published_at": "17:05"},
        {"title": "에코프로비엠 IRA 수혜 양극재 공급계약 체결",
         "summary": "", "url": "u6", "press": "서울경제", "published_at": "16:30"},
        {"title": "A사 유상증자 CB 발행 결정… 주주 희석 우려",
         "summary": "", "url": "u7", "press": "이데일리", "published_at": "10:20"},
        {"title": "B사 대표 횡령 혐의 수사… 적자전환 가능성",
         "summary": "", "url": "u8", "press": "한국경제TV", "published_at": "11:30"},
        {"title": "삼성SDI, 증권사 목표가 상향 BUY 유지",
         "summary": "", "url": "u9", "press": "키움증권", "published_at": "08:30"},
        {"title": "조선업 업황 개선… LNG선 선가 상승 지속",
         "summary": "", "url": "u10", "press": "머니투데이", "published_at": "09:15"},
    ]

    market_data_map   = {
        "u4": {"change_pct": 2.1, "after_hours_pct": 0.9},
        "u6": {"change_pct": 3.2, "after_hours_pct": 1.8},
    }
    sector_spread_map = {
        "u3": 3,
        "u6": 2,
    }

    scorer   = make_scorer()
    pipeline = ScoringPipeline(scorer)
    reporter = ScoringReporter()

    scored = pipeline.run(DUMMY, market_data_map, sector_spread_map)
    reporter.print_summary(scored)
    reporter.print_strong_candidates(pipeline, scored)
    reporter.print_top(pipeline, scored, n=8)


if __name__ == "__main__":
    import sys

    if "--debug" in sys.argv:
        # HTML 구조 + 검색 동작 확인
        # python news_scorer_v4.py --debug
        print("=" * 60)
        print("  [1] 네이버 금융 HTML 응답 디버그")
        print("=" * 60)
        NaverFinanceCrawler().debug_html()

        print("\n" + "=" * 60)
        print("  [2] 네이버 검색 테스트 (코스피 증시 시황)")
        print("=" * 60)
        results = NaverFinanceCrawler().fetch_by_search("코스피 증시 시황", pages=1)
        print(f"검색 결과: {len(results)}건")
        for r in results[:5]:
            print(f"  - [{r['published_at']}] {r['title'][:60]}")

        print("\n" + "=" * 60)
        print("  [3] RSS 테스트")
        print("=" * 60)
        rss_results = NaverFinanceCrawler().fetch_rss(
            "https://finance.naver.com/news/news_list.naver?mode=maknews"
        )
        print(f"RSS 결과: {len(rss_results)}건")
        for r in rss_results[:5]:
            print(f"  - [{r['published_at']}] {r['title'][:60]}")

    elif "--live" in sys.argv:
        # 실제 크롤링: python news_scorer_v4.py --live [--pages N]
        pages = int(sys.argv[sys.argv.index("--pages") + 1]) if "--pages" in sys.argv else 2
        run_pipeline(pages=pages, save_csv=True, verbose=True)

    else:
        # 더미 데모 (기본): python news_scorer_v4.py
        run_demo()
