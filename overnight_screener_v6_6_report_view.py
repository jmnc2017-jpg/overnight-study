"""
==============================================
 국장 오버나잇 스터디 - 통합 스크리닝 v6.3
 KIS API + 뉴스 스코어링 통합본
==============================================

변경사항 (v4 → v5):
  1. news_scorer_v4.py + news_collector.py 연동
     → news_score 실제 계산 (기존 0점 고정 → 최대 100점)
  2. market_signal.py (한투 API) 연동
     → 시간외 단일가 실데이터 → 뉴스 시장반응 점수에 반영
  3. 최종 점수 = 차트/수급(overnight) + 뉴스 점수
  4. API 키 .env 파일로 분리
  5. 출력에 뉴스 키워드 컬럼 추가

수정사항 (리뷰 반영):
  - 랭킹 중복 종목 제거
  - 고점근접 판정 보수화
  - 단기 급등 과열 패널티 추가
  - market_signal.py 시간외 데이터 재사용
  - 시간외 반응 계단식 가중치 적용
  - 상따/과열 위험 패널티 강화
  - 당일 종가 위치 기반 마감 강도 반영

[파일 구성]
  overnight_screener_v5.py  ← 메인 (이 파일)
  news_scorer_v4.py         ← 뉴스 점수 엔진
  news_collector.py         ← RSS 수집기
  market_signal.py          ← 한투 API 시장반응
  .env                      ← API 키 (KIS_APP_KEY, KIS_APP_SECRET)

[설치]
  pip install pandas requests beautifulsoup4 lxml python-dotenv

[실행]
  python overnight_screener_v5.py
  python overnight_screener_v5.py --no-news    # 뉴스 스코어 없이 빠르게
  python overnight_screener_v5.py --top 30     # 상위 30개 출력
"""

import os
import sys
import json
import time
import warnings
import requests
import pandas as pd
from datetime import datetime, timedelta, date
from pathlib import Path

warnings.filterwarnings("ignore")

# ── .env 로드 ──────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv 없어도 환경변수 직접 설정 시 동작

# ──────────────────────────────────────────
# ⚙️  설정값
# ──────────────────────────────────────────
CONFIG = {
    # API 키: .env 파일에서 읽음 (없으면 아래 직접 입력)
    "APP_KEY":    os.environ.get("KIS_APP_KEY",    ""),
    "APP_SECRET": os.environ.get("KIS_APP_SECRET", ""),

    # 1차 필터 (종목 수집 기준)
    "min_change_pct":   0.0,   # 최소 등락률 (%)
    "min_vol_ratio":    0.0,   # 최소 거래량 배율
    "min_trade_amount": 0,    # 최소 거래대금 (억원)
    # 시가총액 필터: API에서 시총 필드가 잡히는 경우에만 적용. 0이면 비활성화.
    # 너무 작은 종목의 갭/호가 리스크를 줄이려면 1000~1500 정도 추천.
    "min_market_cap":   1500,  # 최소 시가총액 (억원)
    "top_n":            20,    # 최종 출력 종목 수

    # 뉴스 점수 가중치
    # 최종 점수 = 차트/수급 점수 + news_weight * 뉴스점수
    # 뉴스 100점 만점 → 0.3 가중치면 최대 30점 추가
    "news_weight": 0.3,

    # 시간외 반응 가중치: 단순 after_change*6보다 실전 체감에 맞게 계단식 적용
    "after_strong_pct": 2.0,
    "after_strong_bonus": 10,
    "after_good_pct": 1.0,
    "after_good_bonus": 5,
    "after_weak_pct": -1.0,
    "after_weak_penalty": -5,
    "after_bad_pct": -2.0,
    "after_bad_penalty": -10,

    # NXT/시간외 전략 기준: 반응 없는 종목은 다음날 갭 전략 후보에서 감점
    # after_price가 없으면 시간외/NXT 데이터 미확인 또는 미지원으로 보고 강한 감점
    # after_price가 있어도 등락률이 거의 0이면 체결 반응 부재로 감점
    # NXT 미지원은 네 전략에서 직접 매매 활용이 어렵기 때문에 강하게 감점한다.
    "after_no_data_penalty": -30,
    "after_no_reaction_penalty": -12,
    "after_reaction_min_abs_pct": 0.05,

    # NXT 미지원이 확실한 종목은 수동 블랙리스트로 관리한다.
    # 예: 대한광통신(010170)은 NXT 미지원으로 확인되어 시간외 데이터가 잡혀도 전략 후보에서 제외 처리.
    "nxt_unsupported_tickers": {"010170"},

    # v6.2 NXT/시간외 품질 점수
    # 시간외 등락률뿐 아니라 거래대금/거래량/체결강도까지 반영한다.
    "after_amount_good_uk": 5,      # 시간외 거래대금 5억 이상
    "after_amount_strong_uk": 20,   # 시간외 거래대금 20억 이상
    "after_amount_super_uk": 50,    # 시간외 거래대금 50억 이상
    "after_amount_bonus_good": 3,
    "after_amount_bonus_strong": 5,
    "after_amount_bonus_super": 8,
    "after_volume_ratio_good": 0.03,    # 시간외 거래량 / 본장 거래량 3% 이상
    "after_volume_ratio_strong": 0.08,  # 8% 이상
    "after_volume_ratio_bonus_good": 3,
    "after_volume_ratio_bonus_strong": 6,

    # 과열 패널티: 단기 오버나잇에서 이미 너무 오른 종목 추격 방지
    "overheat_pct_1": 8.0,
    "overheat_penalty_1": -8,
    "overheat_pct_2": 12.0,
    "overheat_penalty_2": -15,
    "overheat_pct_3": 25.0,
    "overheat_penalty_3": -25,
    "extreme_vol_ratio": 30.0,
    "extreme_vol_penalty": -8,

    # 마감 강도: 당일 종가가 고가권이면 보너스, 저가권이면 패널티
    "close_strong_pos": 0.80,
    "close_strong_bonus": 8,
    "close_good_pos": 0.65,
    "close_good_bonus": 4,
    "close_weak_pos": 0.35,
    "close_weak_penalty": -8,

    # 윗꼬리 패널티: 고가 대비 종가가 많이 밀린 종목 감점
    "upper_wick_warn_pct": 4.0,
    "upper_wick_warn_penalty": -6,
    "upper_wick_bad_pct": 8.0,
    "upper_wick_bad_penalty": -12,

    # 뉴스 등급 직접 보너스: 기존 news_weight 외에 강한 뉴스에는 추가 가점
    "news_grade_bonus": {"S": 15, "A": 8, "B": 4},

    # v6.1 캔들/차트 강화
    # 전일 고가 돌파, N봉 연속상승, 장대양봉, 갭상승 출발을 별도 점수화한다.
    "prev_high_break_bonus": 8,
    "prev_high_break_strong_pct": 2.0,
    "prev_high_break_strong_bonus": 4,
    "up_streak_3_bonus": 5,
    "up_streak_5_bonus": 8,
    "long_bull_body_pct": 5.0,
    "long_bull_bonus": 6,
    "gap_up_pct": 3.0,
    "gap_up_penalty": -4,
    "gap_up_overheat_pct": 7.0,
    "gap_up_overheat_penalty": -10,

    # v6.3 기대갭/리스크 계산
    # 목표가/리스크선은 다음날 NXT 08~09시 대응용 참고값이다.
    "expected_gap_base_pct": 0.8,
    "expected_gap_min_pct": 0.3,
    "expected_gap_max_pct": 3.5,
    # 기대갭이 너무 낮으면 수수료/슬리피지 대비 매매 매력이 낮으므로 감점한다.
    "expected_gap_actionable_pct": 0.5,
    "expected_gap_low_penalty": -8,
    "risk_base_pct": 1.8,
    "risk_min_pct": 1.0,
    "risk_max_pct": 3.5,

    # v6.5 시황 + 뉴스/재료 엔진
    # 국내 지수는 KIS API 우선, 실패 시 yfinance/환경변수 순으로 보완한다.
    # 해외/환율은 yfinance가 설치되어 있으면 자동 수집하고, 없으면 .env 수동값을 사용한다.
    "use_market_engine": True,
    "market_score_strong": 5,
    "market_score_neutral": 0,
    "market_score_watch": -5,
    "market_score_danger": -12,
    "market_extra_penalty_non_nxt_y_in_danger": -5,
    "market_env_nasdaq_fut": "NASDAQ_FUTURES_CHANGE_PCT",
    "market_env_sox": "SOX_CHANGE_PCT",
    "market_env_usdkrw": "USDKRW_CHANGE_PCT",

    # v6.6 AI/Claude 코멘트
    # 기본은 규칙 기반 코멘트로 즉시 동작한다.
    # Claude API를 쓰려면 .env에 ANTHROPIC_API_KEY를 넣고 use_claude_comment=True로 변경.
    "use_claude_comment": False,
    "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY", ""),
    "anthropic_model": os.environ.get("ANTHROPIC_MODEL", "claude-3-5-haiku-latest"),
    "ai_comment_max_stocks": 20,
    "ai_comment_max_len": 54,

    # 출력 파일
    "json_path": "screened_stocks.json",
    "csv_path":  "screened_stocks.csv",

    # 시간외 데이터가 0으로만 나올 때 True로 바꾸면 API 원문 일부를 보여줌
    "debug_after_market": False,
}

BASE_URL = "https://openapi.koreainvestment.com:9443"
_token_cache = {"token": None, "expires": None}


# ──────────────────────────────────────────
# 🔧 유틸
# ──────────────────────────────────────────
def safe_int(val, default=0):
    try:
        v = str(val).replace(",", "").strip()
        return int(float(v)) if v and v != "null" else default
    except:
        return default

def safe_float(val, default=0.0):
    try:
        v = str(val).replace(",", "").strip()
        return float(v) if v and v != "null" else default
    except:
        return default


# ──────────────────────────────────────────
# 🔑 토큰
# ──────────────────────────────────────────
def get_token():
    if _token_cache["token"] and datetime.now() < _token_cache["expires"]:
        return _token_cache["token"]

    app_key    = CONFIG["APP_KEY"]
    app_secret = CONFIG["APP_SECRET"]

    if not app_key or not app_secret:
        raise EnvironmentError(
            "API 키가 없습니다.\n"
            ".env 파일에 KIS_APP_KEY와 KIS_APP_SECRET을 입력하세요."
        )

    r = requests.post(f"{BASE_URL}/oauth2/tokenP", json={
        "grant_type": "client_credentials",
        "appkey": app_key,
        "appsecret": app_secret,
    }, timeout=10)
    token = r.json().get("access_token")
    if not token:
        raise Exception(f"토큰 발급 실패: {r.json()}")
    _token_cache.update({"token": token, "expires": datetime.now() + timedelta(hours=23)})
    print("  ✅ KIS API 토큰 발급 완료")
    return token

def hdrs(tr_id):
    return {
        "Content-Type": "application/json; charset=utf-8",
        "authorization": f"Bearer {get_token()}",
        "appkey": CONFIG["APP_KEY"],
        "appsecret": CONFIG["APP_SECRET"],
        "tr_id": tr_id,
        "custtype": "P",
    }



# ──────────────────────────────────────────
# 🌏 v6.5 시황 + 뉴스/재료 엔진
# ──────────────────────────────────────────
def _first_numeric_from_dict(row: dict, keys: list, default=0.0):
    """후보 키 목록에서 숫자처럼 읽히는 첫 값을 반환한다."""
    if not isinstance(row, dict):
        return default
    for k in keys:
        if k in row and row.get(k) not in (None, "", "null"):
            v = safe_float(row.get(k), default)
            if v != default:
                return v
    return default


def fetch_kis_index_change(index_code: str):
    """KIS 국내 지수 현재 등락률 조회.

    index_code 예시:
      - 0001: KOSPI
      - 1001: KOSDAQ

    KIS 지수 API는 계정/문서 버전에 따라 필드명이 조금 다르게 내려올 수 있어
    응답 후보 키를 넓게 잡고, 실패하면 None을 반환한다.
    """
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-index-price"
    params = {
        "fid_cond_mrkt_div_code": "U",
        "fid_input_iscd": index_code,
    }
    try:
        r = requests.get(url, headers=hdrs("FHPUP02100000"), params=params, timeout=5)
        data = r.json()
        output = data.get("output", data.get("output1", {}))
        if isinstance(output, list):
            output = output[0] if output else {}
        if not isinstance(output, dict) or not output:
            return None

        change = _first_numeric_from_dict(output, [
            "bstp_nmix_prdy_ctrt", "bstp_prdy_ctrt", "prdy_ctrt",
            "prdy_vrss_rate", "idx_prdy_ctrt", "hts_prdy_ctrt"
        ], None)
        price = _first_numeric_from_dict(output, [
            "bstp_nmix_prpr", "bstp_prpr", "stck_prpr", "idx_prpr"
        ], 0.0)
        if change is None:
            return None
        return {"value": price, "change_pct": round(float(change), 2), "source": "KIS"}
    except Exception:
        return None


def fetch_yfinance_change(symbol: str):
    """yfinance가 설치되어 있을 때 해외지수/환율 변화율을 가져온다.

    설치하지 않아도 스크리너는 정상 동작한다.
    필요 시: pip install yfinance
    """
    try:
        import yfinance as yf
        hist = yf.Ticker(symbol).history(period="5d", interval="1d", auto_adjust=False)
        if hist is None or len(hist) < 2:
            return None
        close = hist["Close"].dropna()
        if len(close) < 2:
            return None
        last = float(close.iloc[-1])
        prev = float(close.iloc[-2])
        if prev <= 0:
            return None
        return {"value": last, "change_pct": round((last / prev - 1) * 100, 2), "source": "yfinance"}
    except Exception:
        return None


def fetch_env_change(env_key: str):
    """.env에 수동 입력한 변화율을 읽는다. 예: NASDAQ_FUTURES_CHANGE_PCT=0.35"""
    try:
        raw = os.environ.get(env_key, "")
        if raw in (None, ""):
            return None
        return {"value": 0.0, "change_pct": round(safe_float(raw, 0.0), 2), "source": ".env"}
    except Exception:
        return None


def get_market_item(name, change_data):
    """시황 항목 기본 포맷."""
    if not change_data:
        return {"name": name, "change_pct": None, "value": 0.0, "source": "-"}
    return {
        "name": name,
        "change_pct": change_data.get("change_pct"),
        "value": change_data.get("value", 0.0),
        "source": change_data.get("source", "-"),
    }


def collect_market_overview():
    """시황 데이터를 모아 시장판정/점수보정을 만든다.

    - KOSPI/KOSDAQ: KIS 지수 API 우선, 실패 시 yfinance fallback
    - 나스닥선물/SOX/환율: yfinance 우선, 실패 시 .env 수동값
    """
    if not CONFIG.get("use_market_engine", True):
        return {
            "enabled": False,
            "mode": "미사용",
            "score_adjust": 0,
            "items": {},
            "summary": "시황 + 뉴스/재료 엔진 OFF",
        }

    items = {}
    kospi = fetch_kis_index_change("0001") or fetch_yfinance_change("^KS11")
    kosdaq = fetch_kis_index_change("1001") or fetch_yfinance_change("^KQ11")
    nasdaq_fut = fetch_yfinance_change("NQ=F") or fetch_env_change(CONFIG.get("market_env_nasdaq_fut", "NASDAQ_FUTURES_CHANGE_PCT"))
    sox = fetch_yfinance_change("^SOX") or fetch_env_change(CONFIG.get("market_env_sox", "SOX_CHANGE_PCT"))
    usdkrw = fetch_yfinance_change("KRW=X") or fetch_env_change(CONFIG.get("market_env_usdkrw", "USDKRW_CHANGE_PCT"))

    items["kospi"] = get_market_item("코스피", kospi)
    items["kosdaq"] = get_market_item("코스닥", kosdaq)
    items["nasdaq_fut"] = get_market_item("나스닥선물", nasdaq_fut)
    items["sox"] = get_market_item("SOX", sox)
    items["usdkrw"] = get_market_item("환율", usdkrw)

    score = 0.0
    reasons = []

    ksp = items["kospi"]["change_pct"]
    if ksp is not None:
        if ksp >= 0.7:
            score += 1; reasons.append("코스피 강세")
        elif ksp <= -0.7:
            score -= 1; reasons.append("코스피 약세")

    kqd = items["kosdaq"]["change_pct"]
    if kqd is not None:
        if kqd >= 0.7:
            score += 1; reasons.append("코스닥 강세")
        elif kqd <= -0.7:
            score -= 1; reasons.append("코스닥 약세")

    nq = items["nasdaq_fut"]["change_pct"]
    if nq is not None:
        if nq >= 0.3:
            score += 1; reasons.append("나스닥선물 강세")
        elif nq <= -0.5:
            score -= 1; reasons.append("나스닥선물 약세")

    sx = items["sox"]["change_pct"]
    if sx is not None:
        if sx >= 0.7:
            score += 1; reasons.append("SOX 강세")
        elif sx <= -1.0:
            score -= 1; reasons.append("SOX 약세")

    fx = items["usdkrw"]["change_pct"]
    if fx is not None:
        # 원/달러가 크게 오르는 날은 외국인 수급/성장주에 부담으로 간주
        if fx >= 0.6:
            score -= 1; reasons.append("환율 상승 부담")
        elif fx <= -0.4:
            score += 0.5; reasons.append("환율 안정")

    if score >= 2:
        mode = "강세장"
        score_adjust = CONFIG.get("market_score_strong", 5)
    elif score <= -2:
        mode = "위험장"
        score_adjust = CONFIG.get("market_score_danger", -12)
    elif score <= -1:
        mode = "경계장"
        score_adjust = CONFIG.get("market_score_watch", -5)
    else:
        mode = "중립장"
        score_adjust = CONFIG.get("market_score_neutral", 0)

    return {
        "enabled": True,
        "mode": mode,
        "raw_score": round(score, 2),
        "score_adjust": score_adjust,
        "items": items,
        "reasons": reasons,
        "summary": make_market_summary(items, mode, reasons),
    }


def format_market_pct(v):
    if v is None:
        return "-"
    return f"{v:+.2f}%"


def make_market_summary(items, mode, reasons):
    return (
        f"코스피 {format_market_pct(items['kospi']['change_pct'])} / "
        f"코스닥 {format_market_pct(items['kosdaq']['change_pct'])} / "
        f"나스닥선물 {format_market_pct(items['nasdaq_fut']['change_pct'])} / "
        f"SOX {format_market_pct(items['sox']['change_pct'])} / "
        f"환율 {format_market_pct(items['usdkrw']['change_pct'])} | "
        f"시장판정: {mode}"
        + (f" ({', '.join(reasons[:3])})" if reasons else "")
    )


def print_market_summary(market_info):
    """리포트 상단 시황 요약 출력."""
    if not market_info or not market_info.get("enabled", False):
        print("\n[시황] 시황 + 뉴스/재료 엔진 OFF")
        return
    print("\n" + "=" * 90)
    print(" 🌏 시황 요약")
    print("=" * 90)
    print(f" {market_info.get('summary', '')}")
    print(f" 점수 보정: {market_info.get('score_adjust', 0):+d}점")


def calc_market_score_adjustment(market_info, after_status=""):
    """시장판정에 따른 점수 보정.

    위험장에서는 NXT Y가 아닌 후보를 추가로 더 보수적으로 본다.
    """
    if not market_info or not market_info.get("enabled", False):
        return 0
    adj = int(market_info.get("score_adjust", 0) or 0)
    if market_info.get("mode") == "위험장" and after_status != "Y":
        adj += CONFIG.get("market_extra_penalty_non_nxt_y_in_danger", -5)
    return adj

# ──────────────────────────────────────────
# 📡 STEP 1 - 전종목 등락률 랭킹
# ──────────────────────────────────────────
def fetch_ranking(market_code, market_name):
    url     = f"{BASE_URL}/uapi/domestic-stock/v1/ranking/fluctuation"
    results = []
    seen_tickers = set()
    min_chg = CONFIG["min_change_pct"]

    # NOTE:
    # 현재 API 파라미터에는 명시적인 페이지 번호가 없다.
    # 같은 응답이 반복될 수 있으므로 중복 종목을 제거하고,
    # 추가 종목이 없으면 반복을 종료한다.
    for fno in range(1, 21):
        params = {
            "fid_cond_mrkt_div_code": "J",
            "fid_cond_scr_div_code":  "20170",
            "fid_input_iscd":         market_code,
            "fid_rank_sort_cls_code": "0",
            "fid_input_cnt_1":        "0",
            "fid_prc_cls_code":       "0",
            "fid_input_price_1":      "",
            "fid_input_price_2":      "",
            "fid_vol_cnt":            "",
            "fid_trgt_cls_code":      "0",
            "fid_trgt_exls_cls_code": "0",
            "fid_div_cls_code":       "0",
            "fid_rsfl_rate1":         "",
            "fid_rsfl_rate2":         "",
        }
        try:
            r    = requests.get(url, headers=hdrs("FHPST01700000"), params=params, timeout=10)
            data = r.json()
            rows = data.get("output", [])
            if not rows:
                break

            added_count = 0
            for item in rows:
                ticker = item.get("stck_shrn_iscd", "")
                if not ticker or ticker in seen_tickers:
                    continue
                seen_tickers.add(ticker)

                chg = safe_float(item.get("prdy_ctrt", 0))
                if min_chg > 0 and chg < min_chg:
                    continue

                name = item.get("hts_kor_isnm", "")
                if any(k in name for k in ["ETF", "ETN", "레버리지", "인버스", "스팩", "SPAC"]):
                    continue

                trade_amt = round(safe_int(item.get("acml_tr_pbmn", 0)) / 1e8, 1)
                volume    = safe_int(item.get("acml_vol", 0))
                prev_vol  = safe_int(item.get("prdy_vol", 0))
                vol_ratio = round(volume / prev_vol, 2) if prev_vol > 0 else 0.0

                results.append({
                    "ticker":       ticker,
                    "name":         name,
                    "market":       market_name,
                    "close":        safe_int(item.get("stck_prpr", 0)),
                    "change_pct":   chg,
                    "trade_amount": trade_amt,
                    "vol_ratio":    vol_ratio,
                    "volume":       volume,
                    "prev_vol":     prev_vol,
                })
                added_count += 1

            # 페이지 파라미터가 없어 동일 데이터가 반복되면 즉시 종료
            if added_count == 0 or len(rows) < 100:
                break

        except Exception as e:
            print(f"    ⚠️  랭킹 오류 (페이지{fno}): {e}")
            break

        time.sleep(0.05)

    return results

# ──────────────────────────────────────────
# 📡 STEP 1-2 - 거래대금/전일거래량 보완
# ──────────────────────────────────────────
def fetch_trade_info(ticker):
    """
    현재가 API로 거래대금 + 전일거래량 + 당일 고가/저가/현재가 + 시가총액 가져오기.
    랭킹 API에 없는 필드 보완용이며, 마감 강도/윗꼬리/시총 필터 계산에도 사용한다.
    """
    url    = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": ticker}
    try:
        r      = requests.get(url, headers=hdrs("FHKST01010100"), params=params, timeout=5)
        output = r.json().get("output", {})
        if not output:
            return 0.0, 0, 0, 0, 0, 0, 0

        trade_amt = round(safe_int(output.get("acml_tr_pbmn", 0)) / 1e8, 1)
        volume    = safe_int(output.get("acml_vol", 0))
        vol_ratio = safe_float(output.get("prdy_vrss_vol_rate", 0))
        close     = safe_int(output.get("stck_prpr", 0))
        high      = safe_int(output.get("stck_hgpr", 0))
        low       = safe_int(output.get("stck_lwpr", 0))
        # KIS 현재가 output의 시가총액 필드는 환경/상품별로 다를 수 있어 후보 필드를 넓게 잡는다.
        market_cap = 0
        for key in ("hts_avls", "stck_avls", "mktcap", "market_cap", "lstn_stcn"):
            market_cap = safe_int(output.get(key, 0))
            if market_cap:
                break
        # hts_avls는 보통 억원 단위, 일부 필드는 원/주식수일 수 있어 과도한 값은 억원으로 보정한다.
        if market_cap > 100000000:
            market_cap = round(market_cap / 1e8)
        return trade_amt, volume, vol_ratio, close, high, low, market_cap
    except:
        return 0.0, 0, 0, 0, 0, 0, 0


def enrich_stock_list(stocks: list) -> list:
    """
    종목 리스트에 거래대금/전일거래량 추가
    """
    print(f"  📊 거래대금/거래량 보완 중 ({len(stocks)}종목)...")
    for i, s in enumerate(stocks):
        ticker = s.get("ticker", "")
        if not ticker:
            continue
        trade_amt, volume, vol_ratio, close, high, low, market_cap = fetch_trade_info(ticker)
        s["trade_amount"] = trade_amt
        s["volume"]       = volume
        s["vol_ratio"]    = round(vol_ratio / 100, 2)
        if close > 0:
            s["close"] = close
        s["day_high"]     = high
        s["day_low"]      = low
        s["market_cap"]   = market_cap
        s["close_position"] = calc_close_position(s.get("close", 0), high, low)
        s["upper_wick_pct"] = calc_upper_wick_pct(s.get("close", 0), high)
        time.sleep(0.05)

        if (i + 1) % 10 == 0:
            print(f"    ... {i+1}/{len(stocks)} 완료")

    return stocks


# ──────────────────────────────────────────
# 📡 STEP 2 - 52주 신고가 + 이평 정배열
# ──────────────────────────────────────────
def get_chart_info(ticker):
    """일봉 기반 차트/캔들 정보를 계산한다.

    KIS 일봉 응답은 보통 최신일이 앞쪽([0])으로 내려온다는 전제로 계산한다.
    v6.1부터는 기존 고점근접/정배열 외에 아래 값을 함께 반환한다.
      - prev_high_break_pct: 전일 고가 대비 금일 종가 돌파율
      - up_streak: 종가 기준 연속 상승 봉 수
      - long_bull: 장대양봉 여부
      - gap_up_pct: 전일 종가 대비 금일 시가 갭상승률
    """
    url = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-price"
    params = {
        "fid_cond_mrkt_div_code": "J",
        "fid_input_iscd":         ticker,
        "fid_org_adj_prc":        "0",
        "fid_period_div_code":    "D",
    }

    empty = {
        "is_near_high": False,
        "ma_aligned": False,
        "prev_high_break": False,
        "prev_high_break_pct": 0.0,
        "up_streak": 0,
        "long_bull": False,
        "gap_up_pct": 0.0,
    }

    try:
        r      = requests.get(url, headers=hdrs("FHKST01010400"), params=params, timeout=5)
        output = r.json().get("output", [])
        if len(output) < 2:
            return empty

        days = []
        for o in output:
            close = safe_float(o.get("stck_clpr", 0))
            high  = safe_float(o.get("stck_hgpr", 0))
            low   = safe_float(o.get("stck_lwpr", 0))
            open_ = safe_float(o.get("stck_oprc", 0))
            if close > 0 and high > 0 and low > 0 and open_ > 0:
                days.append({"close": close, "high": high, "low": low, "open": open_})

        if len(days) < 2:
            return empty

        closes = [d["close"] for d in days]
        highs  = [d["high"] for d in days]

        ma_aligned = False
        if len(closes) >= 60:
            ma5  = sum(closes[:5])  / 5
            ma20 = sum(closes[:20]) / 20
            ma60 = sum(closes[:60]) / 60
            ma_aligned = ma5 > ma20 > ma60

        lookback = min(len(highs), 252)
        period_high = max(highs[:lookback]) if lookback else 0
        is_near_high = closes[0] >= period_high * 0.95 if period_high > 0 else False

        today = days[0]
        prev  = days[1]

        prev_high_break_pct = 0.0
        if prev["high"] > 0:
            prev_high_break_pct = round((today["close"] / prev["high"] - 1) * 100, 2)
        prev_high_break = prev_high_break_pct > 0

        # 연속 상승 봉: 최신일 close가 직전일 close보다 높고, 그 흐름이 몇 봉 이어졌는지
        up_streak = 0
        for i in range(0, len(days) - 1):
            if days[i]["close"] > days[i + 1]["close"]:
                up_streak += 1
            else:
                break

        body_pct = 0.0
        if today["open"] > 0:
            body_pct = round((today["close"] / today["open"] - 1) * 100, 2)
        long_bull = body_pct >= CONFIG.get("long_bull_body_pct", 5.0)

        gap_up_pct = 0.0
        if prev["close"] > 0:
            gap_up_pct = round((today["open"] / prev["close"] - 1) * 100, 2)

        return {
            "is_near_high": is_near_high,
            "ma_aligned": ma_aligned,
            "prev_high_break": prev_high_break,
            "prev_high_break_pct": prev_high_break_pct,
            "up_streak": up_streak,
            "long_bull": long_bull,
            "gap_up_pct": gap_up_pct,
        }
    except Exception:
        return empty

# ──────────────────────────────────────────
# 📡 STEP 3 - 외국인/기관 수급
# ──────────────────────────────────────────
def get_foreign_inst(ticker):
    url    = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-investor"
    params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": ticker}
    try:
        r      = requests.get(url, headers=hdrs("FHKST01010900"), params=params, timeout=5)
        output = r.json().get("output", [])
        if not output:
            return 0, 0
        t       = output[0]
        foreign = safe_int(t.get("frgn_ntby_qty", 0))
        inst    = safe_int(t.get("orgn_ntby_qty", 0))
        return (1 if foreign > 0 else -1 if foreign < 0 else 0,
                1 if inst    > 0 else -1 if inst    < 0 else 0)
    except:
        return 0, 0


# ──────────────────────────────────────────
# 📡 STEP 4 - 시간외 단일가
# ──────────────────────────────────────────
def _pick_first_number(row: dict, keys: list, default=0):
    """여러 후보 키 중 처음으로 값이 잡히는 숫자를 반환한다."""
    for k in keys:
        if k in row and row.get(k) not in (None, "", "null"):
            v = safe_float(row.get(k), default)
            if v != default:
                return v
    return default


def _normalize_amount_to_uk(value):
    """시간외 거래대금을 억원 단위로 정규화한다.

    KIS 필드가 원 단위/천원 단위/이미 억원 단위로 내려올 수 있어
    값 크기를 보고 보수적으로 환산한다.
    """
    v = safe_float(value, 0)
    if v <= 0:
        return 0.0
    # 원 단위로 보이는 경우
    if v >= 100000000:
        return round(v / 1e8, 1)
    # 천원 단위로 보이는 경우
    if v >= 100000:
        return round(v / 100000, 1)
    # 이미 억원 단위로 보이는 경우
    return round(v, 1)


def get_after_market(ticker, base_close=0):
    """v6.2 시간외/NXT 조회.

    반환값:
        (price, change_pct, info)

    info에는 시간외 거래량/거래대금/체결강도 후보값을 최대한 파싱해 담는다.
    KIS 응답 필드명이 계정/시점/API 버전에 따라 다를 수 있어 후보 키를 넓게 잡았다.
    """
    url    = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-overtimeprice"
    params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": ticker}

    price_keys = [
        "ovtm_untp_prpr", "ovtm_prpr", "stck_prpr",
        "bsop_hour_cls_prpr", "hour_cls_prpr", "prpr",
        "ovtm_untp_cls_prpr", "ovtm_cls_prpr"
    ]
    change_keys = [
        "ovtm_untp_prdy_vrss_rate", "ovtm_untp_prdy_ctrt",
        "ovtm_prdy_vrss_rate", "ovtm_prdy_ctrt",
        "prdy_ctrt", "stck_prdy_ctrt", "prdy_vrss_rate",
        "ovtm_untp_prdy_vrss"
    ]
    volume_keys = [
        "ovtm_untp_acml_vol", "ovtm_acml_vol", "ovtm_vol",
        "acml_vol", "cntg_vol", "total_vol", "ovtm_untp_vol"
    ]
    amount_keys = [
        "ovtm_untp_tr_pbmn", "ovtm_tr_pbmn", "ovtm_untp_acml_tr_pbmn",
        "ovtm_acml_tr_pbmn", "acml_tr_pbmn", "tr_pbmn",
        "ovtm_tr_amt", "ovtm_untp_tr_amt", "total_tr_amt"
    ]
    empty_info = {
        "after_volume": 0,
        "after_amount": 0.0,
        "after_raw_keys": [],
        "after_source": "api",
    }

    try:
        r    = requests.get(url, headers=hdrs("FHPST02320000"), params=params, timeout=5)
        data = r.json()

        outputs = []
        for key in ("output1", "output2", "output"):
            out = data.get(key)
            if isinstance(out, list):
                outputs.extend([x for x in out if isinstance(x, dict)])
            elif isinstance(out, dict):
                outputs.append(out)

        if not outputs:
            if CONFIG.get("debug_after_market"):
                print(f"    ⚠️ 시간외 응답 없음 {ticker}: {str(data)[:300]}")
            return 0, 0.0, empty_info

        selected = None
        for row in outputs:
            price_try = int(_pick_first_number(row, price_keys, 0))
            change_try = _pick_first_number(row, change_keys, 0.0)
            volume_try = int(_pick_first_number(row, volume_keys, 0))
            amount_try = _pick_first_number(row, amount_keys, 0.0)
            if price_try or change_try or volume_try or amount_try:
                selected = row
                break
        if selected is None:
            selected = outputs[0]

        price  = int(_pick_first_number(selected, price_keys, 0))
        change = _pick_first_number(selected, change_keys, 0.0)
        volume = int(_pick_first_number(selected, volume_keys, 0))
        amount_raw = _pick_first_number(selected, amount_keys, 0.0)
        amount = _normalize_amount_to_uk(amount_raw)
        if change == 0 and price > 0 and base_close and base_close > 0:
            change = ((price / base_close) - 1) * 100

        info = {
            "after_volume": volume,
            "after_amount": amount,
            "after_raw_keys": list(selected.keys())[:30],
            "after_source": "api",
        }

        if CONFIG.get("debug_after_market") and price == 0 and change == 0 and volume == 0 and amount == 0:
            print(f"    ⚠️ 시간외 파싱 실패 {ticker}: keys={list(selected.keys())[:30]} raw={str(selected)[:500]}")

        return price, round(change, 2), info
    except Exception as e:
        if CONFIG.get("debug_after_market"):
            print(f"    ⚠️ 시간외 조회 오류 {ticker}: {e}")
        return 0, 0.0, empty_info


def get_after_market_from_signal(signal: dict):
    """market_signal.py 결과가 있으면 시간외 데이터를 재사용한다.

    반환값: (price, change_pct, info) 또는 None
    """
    if not signal:
        return None

    price = safe_int(
        signal.get("after_price",
        signal.get("overtime_price",
        signal.get("price", 0)))
    )
    change = safe_float(
        signal.get("overtime_change_pct",
        signal.get("after_hours_pct",
        signal.get("after_change_pct",
        signal.get("after_change",
        signal.get("overtime_rate",
        signal.get("overtime_change_rate", 0))))))
    )
    volume = safe_int(
        signal.get("after_volume",
        signal.get("overtime_volume",
        signal.get("ovtm_volume",
        signal.get("volume", 0))))
    )
    amount = _normalize_amount_to_uk(
        signal.get("after_amount",
        signal.get("overtime_amount",
        signal.get("after_trade_amount",
        signal.get("overtime_trade_amount", 0))))
    )
    strength = safe_float(
        signal.get("after_strength",
        signal.get("overtime_strength",
        signal.get("trade_strength", 0)))
    )

    if price == 0 and change == 0 and volume == 0 and amount == 0:
        return None

    info = {
        "after_volume": volume,
        "after_amount": amount,
        "after_raw_keys": list(signal.keys())[:30],
        "after_source": "market_signal",
    }
    return price, round(change, 2), info


# ──────────────────────────────────────────
# 📊 STEP 5 - 뉴스 스코어링 (핵심 추가)
# ──────────────────────────────────────────
def run_news_scoring(stock_list: list, market_signals: dict) -> dict:
    """
    RSS 뉴스 수집 → 점수화 → 종목별 뉴스 점수 반환

    Args:
        stock_list    : [{"ticker": "000660", "name": "SK하이닉스", ...}, ...]
        market_signals: {ticker: {"market_score": int, ...}}  market_signal.py 결과

    Returns:
        {
          "000660": {
            "news_score": 85,
            "news_grade": "S",
            "news_kw": "HBM 수주(+30)",
            "news_title": "SK하이닉스, HBM3E...",
          },
          ...
        }
    """
    result = {}

    # ── 뉴스 수집 ─────────────────────────────────────────────
    try:
        from news_collector import collect_all
        print("  📡 RSS 뉴스 수집 중...")
        articles = collect_all(delay=0.3, today_only=True)
        print(f"     → {len(articles)}건 수집 완료")
    except ImportError:
        print("  ⚠️  news_collector.py 없음 → 뉴스 스코어 건너뜀")
        return result
    except Exception as e:
        print(f"  ⚠️  뉴스 수집 실패: {e}")
        return result

    if not articles:
        print("  ⚠️  수집된 기사 없음")
        return result

    # ── 점수화 ────────────────────────────────────────────────
    try:
        from news_scorer_v4 import make_scorer, ScoringPipeline, DEFAULT_WATCHLIST
    except ImportError:
        print("  ⚠️  news_scorer_v4.py 없음 → 뉴스 스코어 건너뜀")
        return result

    # 관심 종목: DEFAULT_WATCHLIST + 오늘 스크리닝 종목 추가
    extra_wl = {s["name"]: "기타" for s in stock_list}
    watchlist = {**DEFAULT_WATCHLIST, **extra_wl}

    scorer   = make_scorer(extra_watchlist={s["name"]: "기타" for s in stock_list})
    pipeline = ScoringPipeline(scorer)

    # market_signal 결과를 뉴스 점수의 [4]시장반응으로 연결
    # market_signals: {ticker: signal_dict} → url 기반이 아니라 종목명 기반으로 매핑
    name_to_market = {}
    for s in stock_list:
        ticker = s["ticker"]
        if ticker in market_signals:
            name_to_market[s["name"]] = market_signals[ticker]

    # 기사별 market_data 매핑 (제목에 종목명 포함 시 해당 market_signal 연결)
    market_data_map = {}
    for art in articles:
        title = art.get("title", "")
        for name, sig in name_to_market.items():
            if name in title:
                market_data_map[art.get("url", title)] = {
                    "market_score":    sig.get("market_score", 0),
                    "change_pct":      sig.get("close_change_pct", 0),
                    "after_hours_pct": sig.get("overtime_change_pct") or 0,
                }
                break

    print(f"  📊 뉴스 점수화 중...")
    scored = pipeline.run(articles, market_data_map=market_data_map)

    # ── 종목별 최고점 기사 매핑 ───────────────────────────────
    stock_names = {s["name"]: s["ticker"] for s in stock_list}

    for art in scored:
        if art.excluded:
            continue
        title = art.title
        for name, ticker in stock_names.items():
            if name in title:
                existing = result.get(ticker, {})
                if art.total > existing.get("news_score", -999):
                    result[ticker] = {
                        "news_score": art.total,
                        "news_grade": art.grade,
                        "news_kw":    art.issue_best_kw or "",
                        "news_title": art.title[:50],
                        "news_flags": " ".join(art.strong_flags),
                    }
                break

    matched = len([v for v in result.values() if v.get("news_score", 0) > 0])
    print(f"     → 뉴스 매칭 종목: {matched}개")
    return result


# ──────────────────────────────────────────
# 📊 점수 계산 보조 함수
# ──────────────────────────────────────────
def calc_close_position(close, high, low):
    """당일 저가~고가 범위에서 종가가 어느 위치인지 0~1로 계산한다."""
    close = safe_float(close, 0)
    high  = safe_float(high, 0)
    low   = safe_float(low, 0)
    if close <= 0 or high <= 0 or low <= 0 or high <= low:
        return 0.0
    return round(max(0.0, min(1.0, (close - low) / (high - low))), 3)


def calc_upper_wick_pct(close, high):
    """고가 대비 종가가 얼마나 밀렸는지 %로 계산한다."""
    close = safe_float(close, 0)
    high  = safe_float(high, 0)
    if close <= 0 or high <= 0 or high <= close:
        return 0.0
    return round((high - close) / close * 100, 2)


def calc_upper_wick_penalty(upper_wick_pct):
    """윗꼬리가 길면 장중 매물 출회로 보고 감점한다."""
    if upper_wick_pct >= CONFIG["upper_wick_bad_pct"]:
        return CONFIG["upper_wick_bad_penalty"]
    if upper_wick_pct >= CONFIG["upper_wick_warn_pct"]:
        return CONFIG["upper_wick_warn_penalty"]
    return 0


def calc_news_grade_bonus(news_grade):
    """뉴스 등급이 높을수록 기존 뉴스 점수 외에 직접 보너스를 추가한다."""
    return CONFIG.get("news_grade_bonus", {}).get(news_grade, 0)


def is_nxt_unsupported(ticker):
    """수동 관리하는 NXT 미지원 종목 여부."""
    ticker = str(ticker or "").zfill(6)
    return ticker in CONFIG.get("nxt_unsupported_tickers", set())


def classify_after_status(after_price, after_change, after_info=None, ticker=""):
    """시간외/NXT 데이터와 실제 반응 여부를 분리한다.

    Returns:
        status: 표 출력용 문자열
          - "Y"      : 시간외/NXT 등락 반응 있음
          - "보합"   : 거래/가격 데이터는 있으나 등락률 반응 미미
          - "미지원" : 데이터 없음. NXT 미지원/거래없음/API누락 가능성
        has_data: 시간외 가격·거래량·거래대금 중 하나라도 잡혔는지
        has_reaction: 전략에 쓸 만한 등락 반응이 있는지
    """
    after_info = after_info or {}
    if is_nxt_unsupported(ticker):
        return "미지원", False, False

    after_price = safe_int(after_price, 0)
    after_change = safe_float(after_change, 0.0)
    after_volume = safe_int(after_info.get("after_volume", 0), 0)
    after_amount = safe_float(after_info.get("after_amount", 0), 0)
    min_abs = CONFIG.get("after_reaction_min_abs_pct", 0.05)

    has_data = after_price > 0 or after_volume > 0 or after_amount > 0
    has_reaction = abs(after_change) >= min_abs

    if has_reaction:
        return "Y", has_data, True
    if has_data:
        return "보합", True, False
    return "미지원", False, False


def calc_after_quality_score(after_info=None, regular_volume=0):
    """v6.2 시간외 수급 품질 점수.

    시간외 등락률 자체는 calc_after_score에서 반영하고,
    여기서는 거래대금/거래량비 같은 '반응의 질'을 추가 반영한다.
    """
    after_info = after_info or {}
    amount = safe_float(after_info.get("after_amount", 0), 0)  # 억원
    volume = safe_int(after_info.get("after_volume", 0), 0)
    regular_volume = safe_int(regular_volume, 0)

    score = 0
    if amount >= CONFIG.get("after_amount_super_uk", 50):
        score += CONFIG.get("after_amount_bonus_super", 8)
    elif amount >= CONFIG.get("after_amount_strong_uk", 20):
        score += CONFIG.get("after_amount_bonus_strong", 5)
    elif amount >= CONFIG.get("after_amount_good_uk", 5):
        score += CONFIG.get("after_amount_bonus_good", 3)

    after_volume_ratio = 0.0
    if volume > 0 and regular_volume > 0:
        after_volume_ratio = volume / regular_volume
        after_info["after_volume_ratio"] = round(after_volume_ratio, 4)
        if after_volume_ratio >= CONFIG.get("after_volume_ratio_strong", 0.08):
            score += CONFIG.get("after_volume_ratio_bonus_strong", 6)
        elif after_volume_ratio >= CONFIG.get("after_volume_ratio_good", 0.03):
            score += CONFIG.get("after_volume_ratio_bonus_good", 3)
    else:
        after_info["after_volume_ratio"] = 0.0

    return score


def calc_after_score(after_change, after_has_data=True, after_has_reaction=True):
    """시간외 반응을 계단식 점수로 변환한다.

    네 전략은 NXT/시간외 반응을 다음날 갭 힌트로 쓰는 구조라서,
    데이터가 없거나 0.0% 보합이면 명확하게 감점한다.
    """
    if not after_has_data:
        return CONFIG.get("after_no_data_penalty", -18)
    if not after_has_reaction:
        return CONFIG.get("after_no_reaction_penalty", -12)

    if after_change >= CONFIG["after_strong_pct"]:
        return CONFIG["after_strong_bonus"]
    if after_change >= CONFIG["after_good_pct"]:
        return CONFIG["after_good_bonus"]
    if after_change <= CONFIG["after_bad_pct"]:
        return CONFIG["after_bad_penalty"]
    if after_change <= CONFIG["after_weak_pct"]:
        return CONFIG["after_weak_penalty"]
    return 0


def calc_overheat_penalty(change_pct, vol_ratio):
    """급등률/거래량 과열에 따른 추격매수 위험 패널티."""
    penalty = 0
    if change_pct >= CONFIG["overheat_pct_3"]:
        penalty += CONFIG["overheat_penalty_3"]
    elif change_pct >= CONFIG["overheat_pct_2"]:
        penalty += CONFIG["overheat_penalty_2"]
    elif change_pct >= CONFIG["overheat_pct_1"]:
        penalty += CONFIG["overheat_penalty_1"]

    if vol_ratio >= CONFIG["extreme_vol_ratio"]:
        penalty += CONFIG["extreme_vol_penalty"]

    return penalty


def calc_close_strength_score(close_position):
    """종가가 당일 고가권에 가까운지 반영한다."""
    if close_position >= CONFIG["close_strong_pos"]:
        return CONFIG["close_strong_bonus"]
    if close_position >= CONFIG["close_good_pos"]:
        return CONFIG["close_good_bonus"]
    if 0 < close_position <= CONFIG["close_weak_pos"]:
        return CONFIG["close_weak_penalty"]
    return 0


def calc_candle_score(chart_info):
    """v6.1 캔들 분석 점수.

    전일 고가 돌파와 연속 상승은 추세 지속 가점,
    과도한 갭상승 출발은 이미 시초에 에너지를 쓴 것으로 보고 감점한다.
    """
    if not isinstance(chart_info, dict):
        return 0

    score = 0
    break_pct = safe_float(chart_info.get("prev_high_break_pct", 0), 0)
    up_streak = safe_int(chart_info.get("up_streak", 0), 0)
    gap_up_pct = safe_float(chart_info.get("gap_up_pct", 0), 0)

    if chart_info.get("prev_high_break"):
        score += CONFIG.get("prev_high_break_bonus", 8)
        if break_pct >= CONFIG.get("prev_high_break_strong_pct", 2.0):
            score += CONFIG.get("prev_high_break_strong_bonus", 4)

    if up_streak >= 5:
        score += CONFIG.get("up_streak_5_bonus", 8)
    elif up_streak >= 3:
        score += CONFIG.get("up_streak_3_bonus", 5)

    if chart_info.get("long_bull"):
        score += CONFIG.get("long_bull_bonus", 6)

    if gap_up_pct >= CONFIG.get("gap_up_overheat_pct", 7.0):
        score += CONFIG.get("gap_up_overheat_penalty", -10)
    elif gap_up_pct >= CONFIG.get("gap_up_pct", 3.0):
        score += CONFIG.get("gap_up_penalty", -4)

    return score


# ──────────────────────────────────────────
# 📊 점수 계산 (통합)
# ──────────────────────────────────────────
def calc_score(change_pct, vol_ratio, is_52w_high, ma_aligned,
               foreign, inst, after_change, news_score=0, news_grade="",
               close_position=0, upper_wick_pct=0, chart_info=None,
               after_has_data=True, after_has_reaction=True,
               after_info=None, regular_volume=0):
    """
    차트/수급 점수 + 뉴스 점수 통합

    차트/수급:
      등락률×2 + 거래량×5 + 고점근접(10) + 정배열(15)
      + 외국인(±15) + 기관(±15)
    시간외/NXT:
      데이터 없음/반응 없음은 감점, +1%/+2% 이상은 보너스, -1%/-2% 이하는 패널티
    마감 강도:
      종가가 당일 고가권이면 보너스, 저가권이면 패널티
    과열/윗꼬리 패널티:
      급등률·거래량 과열과 고가 대비 종가 밀림을 추격매수 리스크로 반영
    뉴스 등급 보너스:
      기존 news_weight 외에 S/A/B 등급 직접 보너스 추가
    """
    chart  = min(change_pct * 2, 20) + min(vol_ratio * 5, 20)
    chart += (10 if is_52w_high else 0) + (15 if ma_aligned else 0)

    supply  = (15 if foreign > 0 else -10 if foreign < 0 else 0)
    supply += (15 if inst    > 0 else -10 if inst    < 0 else 0)

    after_score = calc_after_score(after_change, after_has_data, after_has_reaction)
    after_quality_score = calc_after_quality_score(after_info or {}, regular_volume=regular_volume)
    close_strength_score = calc_close_strength_score(close_position)
    news_contrib = round(news_score * CONFIG["news_weight"])
    news_grade_bonus = calc_news_grade_bonus(news_grade)
    overheat_penalty = calc_overheat_penalty(change_pct, vol_ratio)
    upper_wick_penalty = calc_upper_wick_penalty(upper_wick_pct)
    candle_score = calc_candle_score(chart_info or {})

    total = (
        round(chart)
        + round(supply)
        + after_score
        + after_quality_score
        + close_strength_score
        + news_contrib
        + news_grade_bonus
        + overheat_penalty
        + upper_wick_penalty
        + candle_score
    )

    return (
        round(chart),
        round(supply),
        after_score,
        after_quality_score,
        close_strength_score,
        news_contrib,
        news_grade_bonus,
        overheat_penalty,
        upper_wick_penalty,
        candle_score,
        max(0, round(total)),
    )



def make_signal_flags(r):
    """결과표용 조건별 아이콘 플래그를 만든다.

    🔥 초강세 캔들: 고가권 마감 + 짧은 윗꼬리
    🚀 시간외/NXT 강세: 시간외 +1% 이상
    📈 연속상승: 3봉 이상 상승
    📰 뉴스 강세: 뉴스 S/A 등급
    ⚠️ 경고: 급등 과열 또는 긴 윗꼬리
    """
    flags = []

    close_position = safe_float(r.get("close_position", 0), 0)
    close_strength_pct = close_position * 100
    upper_wick_pct = safe_float(r.get("upper_wick_pct", 0), 0)
    after_change = safe_float(r.get("after_change", 0), 0)
    up_streak = safe_int(r.get("up_streak", 0), 0)
    news_grade = r.get("news_grade", "")
    change_pct = safe_float(r.get("change_pct", 0), 0)
    vol_ratio = safe_float(r.get("vol_ratio", 0), 0)

    if close_strength_pct >= 90 and upper_wick_pct <= 2:
        flags.append("🔥")

    after_amount = safe_float(r.get("after_amount", 0), 0)
    after_vr = safe_float(r.get("after_volume_ratio", 0), 0)

    # 🚀는 반드시 NXT 지원 + 시간외 등락률이 플러스일 때만 표시한다.
    # 거래대금만 크고 가격이 보합/하락이면 다음날 갭 강세 신호로 보지 않는다.
    # 최종 조건:
    #   1) 시간외 +1.0% 이상
    #   2) 시간외 +0.5% 이상 + 외대금 10억 이상
    #   3) 시간외 +0.3% 이상 + 외대금 30억 이상 + 외비 1.0% 이상
    after_status = r.get("after_status", "미지원")
    if (
        after_status == "Y"
        and (
            after_change >= 1.0
            or (after_change >= 0.5 and after_amount >= 10)
            or (after_change >= 0.3 and after_amount >= 30 and after_vr >= 0.01)
        )
    ):
        flags.append("🚀")

    if up_streak >= 3:
        flags.append("📈")

    if news_grade in ("S", "A"):
        flags.append("📰")

    if (
        change_pct >= 25
        or upper_wick_pct >= 8
        or vol_ratio >= CONFIG.get("extreme_vol_ratio", 30.0)
        or after_change <= -1.0
    ):
        flags.append("⚠️")

    return "".join(flags)


def get_rr_badge(rr):
    """RR 숫자를 빠르게 읽기 위한 상태 아이콘을 반환한다.

    ✅ : 리스크 대비 보상 우위
    ⚠️ : 애매, 관찰
    ❌ : 기대값 낮음
    """
    rr = safe_float(rr, 0)
    if rr >= 1.2:
        return "✅"
    if rr >= 0.7:
        return "⚠️"
    if rr > 0:
        return "❌"
    return "-"


# ──────────────────────────────────────────
# 🧠 v6.5 뉴스/재료 분류 엔진
# ──────────────────────────────────────────
def classify_material(r):
    """뉴스 제목/키워드/종목 특성을 기반으로 재료를 빠르게 분류한다.

    v6.5.1 보강:
      - 전선/전력, 바이오 카테고리 추가
      - 한솔테크닉스/해성디에스 등 반도체 밸류체인 종목명 보완
      - 재료 우선순위와 점수 차등 강화
      - 우선주/품절주/단순테마는 리스크성 재료로 강하게 감점
    """
    name = str(r.get("name", ""))
    title = str(r.get("news_title", ""))
    kw = str(r.get("news_kw", ""))
    flags = str(r.get("news_flags", ""))
    text = f"{name} {title} {kw} {flags}".lower()

    change_pct = safe_float(r.get("change_pct", 0), 0)
    vol_ratio = safe_float(r.get("vol_ratio", 0), 0)
    market_cap = safe_float(r.get("market_cap", 0), 0)
    trade_amount = safe_float(r.get("trade_amount", 0), 0)
    news_score = safe_float(r.get("news_score", 0), 0)

    # ── 0) 구조적 리스크성 재료: 표시 우선순위 최상단 ─────────────
    # 우선주 감지: 종목명 끝의 '우', '우B', '우선주' 등
    if name.endswith("우") or "우b" in text or "우선주" in text:
        return "우선주", "우선주/괴리율성 움직임 가능", -12

    # 품절주/초소형주성 감지: 시총 작고 급등·거래량 과열이면 경고성 분류
    if market_cap and market_cap < 2500 and (change_pct >= 20 or vol_ratio >= 10):
        return "품절주", "소형주 급등·호가 변동성 주의", -10

    # ── 1) 고신뢰 뉴스성 재료: 실적 > 수주 > HBM > 정책/원전 ────
    if any(k in text for k in ["실적", "영업이익", "순이익", "매출", "흑자", "턴어라운드", "가이던스", "어닝", "컨센서스", "실적개선"]):
        return "실적기반", "실적/턴어라운드 기반 재료", 12

    if any(k in text for k in ["수주", "계약", "공급", "납품", "epc", "선정", "낙찰", "공급계약", "장기공급"]):
        return "수주", "계약·공급·수주성 재료", 10

    # 반도체/HBM: 뉴스 키워드 + 대표 밸류체인 종목명 보완
    semi_names = [
        "한솔테크닉스", "해성디에스", "아스플로", "코리아써키트", "대덕전자", "대덕",
        "이수페타시스", "심텍", "하나마이크론", "한미반도체", "주성엔지니어링",
        "원익ips", "리노공업", "테크윙", "피에스케이", "오픈엣지테크놀로지"
    ]
    if any(k.lower() in text for k in ["hbm", "cxl", "ddr5", "반도체", "ai반도체", "엔비디아", "nvidia", "pcb", "패키징", "후공정", "유리기판", "ai 서버"]):
        return "HBM/반도체", "반도체·AI 밸류체인 재료", 8
    if any(n.lower() in text for n in semi_names):
        return "HBM/반도체", "반도체·AI 밸류체인 종목군", 6

    if any(k in text for k in ["원전", "원자력", "smr", "한수원", "체코", "필리핀 원자력"]):
        return "원전", "원전/인프라 정책·수주 기대", 7

    if any(k in text for k in ["정부", "정책", "국책", "보조금", "규제완화", "인허가", "대통령", "장관", "법안", "국가전략", "세액공제"]):
        return "정책", "정부정책/규제 변화 재료", 6

    # ── 2) 섹터 분류 보강 ─────────────────────────────────────
    # 전선/전력/광통신/데이터센터 인프라
    power_names = [
        "제룡산업", "대한광통신", "가온전선", "대한전선", "일진전기", "ls에코에너지", "ls전선아시아",
        "제룡전기", "효성중공업", "hd현대일렉트릭", "광명전기", "보성파워텍", "세명전기", "대원전선"
    ]
    if any(k in text for k in ["전력", "전선", "변압기", "송전", "배전", "전력망", "전력기기", "hvdc", "초고압", "케이블", "광케이블", "광통신", "데이터센터", "ai 전력"]):
        return "전선/전력", "전력망·데이터센터 인프라 재료", 6
    if any(n.lower() in text for n in power_names):
        return "전선/전력", "전력망·광통신 인프라 종목군", 4

    # 바이오/제약/의료기기
    bio_names = ["리센스메디컬", "툴젠", "진원생명과학", "이연제약", "테고사이언스", "드림씨아이에스"]
    if any(k in text for k in ["바이오", "제약", "임상", "fda", "신약", "치료제", "의료기기", "허가", "품목허가", "유전자", "세포치료", "crispr", "cro"]):
        return "바이오", "바이오·제약·의료기기 재료", 3
    if any(n.lower() in text for n in bio_names):
        return "바이오", "바이오·제약·의료기기 종목군", 2

    if any(k in text for k in ["m&a", "인수", "합병", "매각", "지분", "투자유치", "유상증자", "무상증자"]):
        return "지배구조", "지분/증자/M&A성 재료", 3

    # ── 3) 리스크성 단순 테마 ─────────────────────────────────
    # 뉴스는 약한데 가격만 급등한 경우
    if news_score <= 0 and change_pct >= 15 and vol_ratio >= 5:
        return "단순테마", "뉴스 미확인 급등·테마성 가능", -6

    if news_score > 0:
        return "섹터/뉴스", "뉴스는 있으나 핵심 재료 분류 불명확", 2

    return "기타", "가격/수급 중심 후보", 0


def calc_expected_gap_penalty(expected_gap_pct):
    """기대갭이 너무 낮은 후보는 실전 진입 매력이 낮으므로 감점한다."""
    expected_gap_pct = safe_float(expected_gap_pct, 0)
    if 0 < expected_gap_pct < CONFIG.get("expected_gap_actionable_pct", 0.5):
        return CONFIG.get("expected_gap_low_penalty", -8)
    return 0

# ──────────────────────────────────────────
# 🖨️  출력
# ──────────────────────────────────────────

# ──────────────────────────────────────────
# 🎯 v6.3 기대갭/리스크 계산
# ──────────────────────────────────────────
def get_tick_unit(price):
    """국내주식 호가 단위에 맞춰 목표가/리스크선을 보기 좋게 보정한다."""
    price = safe_int(price, 0)
    if price < 2000:
        return 1
    if price < 5000:
        return 5
    if price < 20000:
        return 10
    if price < 50000:
        return 50
    if price < 200000:
        return 100
    if price < 500000:
        return 500
    return 1000


def round_to_tick(price, direction="nearest"):
    """호가 단위 반올림. 목표가는 올림, 리스크선은 내림으로 보수 처리."""
    price = safe_float(price, 0)
    if price <= 0:
        return 0
    tick = get_tick_unit(price)
    if direction == "up":
        return int(((price + tick - 1) // tick) * tick)
    if direction == "down":
        return int((price // tick) * tick)
    return int(round(price / tick) * tick)


def calc_expected_gap_and_risk(r):
    """다음날 NXT 대응용 기대갭/목표가/리스크선/RR을 계산한다."""
    close = safe_int(r.get("close", 0), 0)
    if close <= 0:
        return {"expected_gap_pct": 0.0, "target_price": 0, "risk_pct": 0.0, "risk_price": 0, "rr_ratio": 0.0}

    after_status = r.get("after_status", "미지원")
    after_change = safe_float(r.get("after_change", 0), 0)
    after_amount = safe_float(r.get("after_amount", 0), 0)
    after_vr = safe_float(r.get("after_volume_ratio", 0), 0)
    close_pos = safe_float(r.get("close_position", 0), 0)
    upper_wick = safe_float(r.get("upper_wick_pct", 0), 0)
    change_pct = safe_float(r.get("change_pct", 0), 0)
    vol_ratio = safe_float(r.get("vol_ratio", 0), 0)
    up_streak = safe_int(r.get("up_streak", 0), 0)
    news_grade = r.get("news_grade", "")

    gap = CONFIG.get("expected_gap_base_pct", 0.8)
    if after_status == "Y":
        gap += min(after_change * 0.45, 1.4) if after_change > 0 else max(after_change * 0.35, -0.8)
    elif after_status == "보합":
        gap -= 0.25
    else:
        gap -= 0.6

    if after_amount >= 50:
        gap += 0.45
    elif after_amount >= 20:
        gap += 0.30
    elif after_amount >= 5:
        gap += 0.15

    if after_vr >= 0.08:
        gap += 0.45
    elif after_vr >= 0.03:
        gap += 0.25
    elif after_vr >= 0.01:
        gap += 0.10

    if close_pos >= 0.9:
        gap += 0.35
    elif close_pos >= 0.8:
        gap += 0.20
    elif 0 < close_pos <= 0.35:
        gap -= 0.35

    if up_streak >= 3:
        gap += 0.20
    if news_grade == "S":
        gap += 0.45
    elif news_grade == "A":
        gap += 0.30
    elif news_grade == "B":
        gap += 0.15

    if change_pct >= 25:
        gap -= 0.8
    elif change_pct >= 20:
        gap -= 0.45
    elif change_pct >= 12:
        gap -= 0.25

    if upper_wick >= 8:
        gap -= 0.65
    elif upper_wick >= 4:
        gap -= 0.30

    gap = round(max(CONFIG.get("expected_gap_min_pct", 0.3), min(CONFIG.get("expected_gap_max_pct", 4.0), gap)), 2)

    risk = CONFIG.get("risk_base_pct", 1.8)
    if change_pct >= 25:
        risk += 0.7
    elif change_pct >= 20:
        risk += 0.5
    elif change_pct >= 12:
        risk += 0.25
    if vol_ratio >= 20:
        risk += 0.35
    elif vol_ratio >= 10:
        risk += 0.20
    if upper_wick >= 8:
        risk += 0.40
    elif upper_wick >= 4:
        risk += 0.20
    if after_change <= -2:
        risk = max(CONFIG.get("risk_min_pct", 1.0), risk - 0.45)
    elif after_change <= -1:
        risk = max(CONFIG.get("risk_min_pct", 1.0), risk - 0.25)
    if after_status == "미지원":
        risk += 0.25
    risk = round(max(CONFIG.get("risk_min_pct", 1.0), min(CONFIG.get("risk_max_pct", 3.5), risk)), 2)

    target_price = round_to_tick(close * (1 + gap / 100), "up")
    risk_price = round_to_tick(close * (1 - risk / 100), "down")

    # RR은 퍼센트 추정치가 아니라 실제 호가 보정 후 가격 기준으로 계산한다.
    # reward = 목표가 - 현재가 / risk = 현재가 - 손절가
    reward_amt = max(0, target_price - close)
    risk_amt = max(0, close - risk_price)
    rr = round(reward_amt / risk_amt, 2) if risk_amt > 0 else 0.0

    # 호가 보정 후 실제 표시용 수익률/손절률도 함께 저장한다.
    actual_reward_pct = round((reward_amt / close) * 100, 2) if close > 0 else 0.0
    actual_risk_pct = round((risk_amt / close) * 100, 2) if close > 0 else 0.0

    return {
        "expected_gap_pct": gap,
        "target_price": target_price,
        "risk_pct": risk,
        "risk_price": risk_price,
        "rr_ratio": rr,
        "actual_reward_pct": actual_reward_pct,
        "actual_risk_pct": actual_risk_pct,
    }



# ──────────────────────────────────────────
# 💬 v6.6 AI/Claude 코멘트
# ──────────────────────────────────────────
def _shorten_comment(text, max_len=None):
    max_len = max_len or CONFIG.get("ai_comment_max_len", 54)
    text = str(text or "").replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len - 1].rstrip() + "…"


def build_rule_based_comment(r, market_info=None):
    """Claude API 없이도 항상 동작하는 한줄 코멘트 생성기."""
    parts = []
    warnings = []

    material = r.get("material_type", "기타")
    after_status = r.get("after_status", "")
    after_change = safe_float(r.get("after_change", 0), 0)
    rr = safe_float(r.get("rr_ratio", 0), 0)
    close_pos = safe_float(r.get("close_position", 0), 0)
    upper_wick = safe_float(r.get("upper_wick_pct", 0), 0)
    up_streak = safe_int(r.get("up_streak", 0), 0)
    expected_gap = safe_float(r.get("expected_gap_pct", 0), 0)
    foreign = safe_int(r.get("foreign", 0), 0)
    inst = safe_int(r.get("inst", 0), 0)

    if material and material != "기타":
        parts.append(f"{material} 재료")
    if close_pos >= 0.9:
        parts.append("고가권 마감")
    elif close_pos >= 0.75:
        parts.append("상단권 마감")
    if after_status == "Y" and after_change >= 1.0:
        parts.append(f"시간외 +{after_change:.1f}%")
    elif after_status == "Y" and after_change > 0:
        parts.append("시간외 소폭 양호")
    if foreign > 0 and inst > 0:
        parts.append("외인·기관 동반매수")
    elif inst > 0:
        parts.append("기관 유입")
    elif foreign > 0:
        parts.append("외국인 유입")
    if up_streak >= 3:
        parts.append(f"{up_streak}봉 상승")

    if after_status == "미지원":
        warnings.append("NXT 미지원")
    elif after_status == "보합":
        warnings.append("시간외 보합")
    if after_change <= -1.0:
        warnings.append("시간외 약세")
    if upper_wick >= 8:
        warnings.append("윗꼬리 과다")
    elif upper_wick >= 5:
        warnings.append("윗꼬리 부담")
    if rr < 0.7:
        warnings.append("RR 낮음")
    if r.get("material_type") in ("우선주", "품절주", "단순테마"):
        warnings.append("재료 리스크")

    if rr >= 1.2 and after_status == "Y" and after_change > 0:
        tail = "익일 갭 시도 가능"
    elif expected_gap >= 1.0 and rr >= 0.7:
        tail = "관찰 가능"
    elif warnings:
        tail = "추격 주의"
    else:
        tail = "보수적 관찰"

    main = " + ".join(parts[:3]) if parts else "재료 확인 필요"
    if warnings:
        return _shorten_comment(f"{main} / {', '.join(warnings[:2])} → {tail}")
    return _shorten_comment(f"{main} → {tail}")


def build_claude_comment(r, market_info=None):
    """ANTHROPIC_API_KEY가 있을 때 Claude API로 한줄 코멘트를 생성한다. 실패 시 규칙 기반으로 대체."""
    api_key = CONFIG.get("anthropic_api_key", "")
    if not api_key:
        return build_rule_based_comment(r, market_info)

    prompt = (
        "너는 한국 주식 종가배팅/NXT 전략용 리포트 작성자다. "
        "아래 데이터를 보고 45자 안팎의 한국어 한줄 코멘트만 작성해라. "
        "과장 금지, 매수 추천 금지, 리스크가 있으면 함께 언급.\n\n"
        f"종목명: {r.get('name')}\n"
        f"점수: {r.get('total_score')}\n"
        f"재료: {r.get('material_type')} / 뉴스키워드: {r.get('news_kw')}\n"
        f"NXT: {r.get('after_status')} / 시간외: {r.get('after_change')}% / 외대금: {r.get('after_amount')}억 / 외비: {safe_float(r.get('after_volume_ratio',0))*100:.1f}%\n"
        f"마감강도: {safe_float(r.get('close_position',0))*100:.0f}% / 윗꼬리: {r.get('upper_wick_pct')}% / 연속상승: {r.get('up_streak')}봉\n"
        f"기대갭: {r.get('expected_gap_pct')}% / RR: {r.get('rr_ratio')} / 판단: {get_rr_badge(safe_float(r.get('rr_ratio',0)))}\n"
        f"시황: {(market_info or {}).get('mode','')} {(market_info or {}).get('reason','')}\n"
    )

    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CONFIG.get("anthropic_model", "claude-3-5-haiku-latest"),
                "max_tokens": 80,
                "temperature": 0.2,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=12,
        )
        data = resp.json()
        content = data.get("content", [])
        if content and isinstance(content, list):
            txt = content[0].get("text", "").strip().strip('"')
            if txt:
                return _shorten_comment(txt)
    except Exception:
        pass
    return build_rule_based_comment(r, market_info)


def attach_ai_comments(candidates, market_info=None):
    """상위 후보에 AI 코멘트를 붙인다. 기본은 빠른 규칙 기반, 옵션으로 Claude API 사용."""
    use_claude = bool(CONFIG.get("use_claude_comment") and CONFIG.get("anthropic_api_key"))
    max_n = CONFIG.get("ai_comment_max_stocks", 20)
    for idx, r in enumerate(candidates):
        if idx < max_n:
            r["ai_comment"] = build_claude_comment(r, market_info) if use_claude else build_rule_based_comment(r, market_info)
        else:
            r["ai_comment"] = build_rule_based_comment(r, market_info)
    return candidates



def _fmt_price_krw(price):
    price = safe_int(price, 0)
    return f"₩{price:,}" if price > 0 else "₩-"


def _fmt_price_plain(price):
    price = safe_int(price, 0)
    return f"₩{price:,}" if price > 0 else "₩-"


def _fmt_pct_signed(value, dash_zero=False, digits=1):
    value = safe_float(value, 0)
    if dash_zero and abs(value) < 0.0001:
        return "-"
    return f"{value:+.{digits}f}%"


def _fmt_market_line(market_info):
    if not market_info or not market_info.get("enabled", False):
        return "[시황] 시황 데이터 없음"
    items = market_info.get("items", {}) or {}
    def gp(key):
        return format_market_pct((items.get(key, {}) or {}).get("change_pct"))
    parts = [
        f"코스피 {gp('kospi')}",
        f"코스닥 {gp('kosdaq')}",
        f"나스닥선물 {gp('nasdaq_fut')}",
        f"SOX {gp('sox')}",
        f"환율 {gp('usdkrw')}",
    ]
    mode = market_info.get("mode", "-")
    adj = market_info.get("score_adjust", 0)
    return f"[시황] {' / '.join(parts)} | {mode} ({adj:+d}점)"


def _fmt_supply_arrow(v):
    return "↑" if v > 0 else ("↓" if v < 0 else "-")


def _fmt_news_score(r):
    grade_emoji = {"S": "🚀", "A": "🟢", "B": "🔵"}.get(r.get("news_grade", ""), "")
    score = safe_int(r.get("news_score", 0), 0)
    kw = str(r.get("news_kw", "") or "").strip()
    if score > 0 and kw:
        return f"뉴스 {grade_emoji}{score}점/{kw}"
    if score > 0:
        return f"뉴스 {grade_emoji}{score}점"
    return "뉴스 -"


def _fmt_material(r):
    material = str(r.get("material_type", "기타") or "기타")
    reason = str(r.get("material_reason", "") or "").strip()
    if reason and reason != material:
        return f"{material}"
    return material


def print_report(candidates, market_info=None):
    """v6.6 최종 리포트형 출력.

    기존 표 형태 대신 사람이 읽는 종목별 카드형으로 출력한다.
    CSV/JSON 저장 데이터는 그대로 유지된다.
    """
    print("\n" + "=" * 90)
    print(" 📚 오버나잇 후보 리포트")
    print("=" * 90)
    print(" " + _fmt_market_line(market_info))
    print("-" * 90)

    for i, r in enumerate(candidates, 1):
        ticker = r.get("ticker", "")
        name = r.get("name", "")
        close = safe_int(r.get("close", 0), 0)
        flags = make_signal_flags(r)

        score = safe_int(r.get("total_score", 0), 0)
        material = _fmt_material(r)
        rr = safe_float(r.get("rr_ratio", 0), 0)
        rr_badge = get_rr_badge(rr)
        nxt = r.get("after_status", "미지원")

        cp = safe_float(r.get("close_position", 0), 0)
        cp_txt = f"고가권 {cp*100:.0f}%" if cp > 0 else "고가권 -"
        prev_break = safe_float(r.get("prev_high_break_pct", 0), 0)
        prev_txt = f"전일고가돌파 {prev_break:+.1f}%" if abs(prev_break) > 0.0001 else "전일고가돌파 -"
        up_streak = safe_int(r.get("up_streak", 0), 0)
        streak_txt = f"{up_streak}봉연속상승" if up_streak > 0 else "연속상승 -"
        gap_up = safe_float(r.get("gap_up_pct", 0), 0)
        gap_txt = f"갭 {gap_up:+.1f}%" if abs(gap_up) > 0.0001 else "갭 -"
        wick = safe_float(r.get("upper_wick_pct", 0), 0)
        wick_txt = f"윗꼬리 {wick:.1f}%" if wick > 0 else "윗꼬리 -"

        foreign = _fmt_supply_arrow(safe_int(r.get("foreign", 0), 0))
        inst = _fmt_supply_arrow(safe_int(r.get("inst", 0), 0))
        after = safe_float(r.get("after_change", 0), 0)
        after_amt = safe_float(r.get("after_amount", 0), 0)
        after_vr = safe_float(r.get("after_volume_ratio", 0), 0)
        after_amt_txt = f"외대금 {after_amt:.1f}억" if after_amt > 0 else "외대금 -"
        after_vr_txt = f"외비 {after_vr*100:.1f}%" if after_vr > 0 else "외비 -"

        exp_gap = safe_float(r.get("expected_gap_pct", 0), 0)
        target = safe_int(r.get("target_price", 0), 0)
        risk = safe_int(r.get("risk_price", 0), 0)
        risk_pct = safe_float(r.get("actual_risk_pct", r.get("risk_pct", 0)), 0)

        comment = str(r.get("ai_comment", "") or "").strip()
        if not comment:
            comment = build_rule_based_comment(r, market_info)

        print(f"{i}. {name} ({ticker}) {_fmt_price_krw(close)} {flags}")
        print(f"   점수: {score}점 | 재료: {material} | RR {rr:.2f} {rr_badge} | {_fmt_news_score(r)}")
        print(f"   {cp_txt} | {prev_txt} | {streak_txt} | {gap_txt} | {wick_txt}")
        print(f"   기관{inst} / 외국인{foreign} | NXT {nxt} | 시간외 {after:+.1f}% | {after_amt_txt} | {after_vr_txt}")
        print(f"   목표가 {_fmt_price_plain(target)}({exp_gap:+.1f}%) | 리스크선 {_fmt_price_plain(risk)}(-{risk_pct:.1f}%)")
        print(f"   💬 \"{comment}\"")
        print()

def save_outputs(candidates, today, market_info=None):
    out = {
        "date": today,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "market": market_info or {},
        "stocks": candidates,
    }
    with open(CONFIG["json_path"], "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    pd.DataFrame(candidates).to_csv(CONFIG["csv_path"], index=False, encoding="utf-8-sig")
    print(f"  ✅ 저장: {CONFIG['json_path']}, {CONFIG['csv_path']}")


# ──────────────────────────────────────────
# ▶️  메인
# ──────────────────────────────────────────
def main():
    # 인수 파싱
    use_news = "--no-news" not in sys.argv
    use_market = "--no-market" not in sys.argv
    top_n    = CONFIG["top_n"]
    if "--top" in sys.argv:
        try:
            top_n = int(sys.argv[sys.argv.index("--top") + 1])
        except (IndexError, ValueError):
            pass

    today = datetime.today()
    while today.weekday() >= 5:
        today -= timedelta(days=1)
    today_str = today.strftime("%Y-%m-%d")

    print("\n" + "="*76)
    print(" 🚀 오버나잇 스터디 스크리닝 v6.6 (Claude 코멘트)")
    print("="*76)
    print(f" 기준일: {today_str}")
    if not use_news:
        print(" ⚡ 뉴스 스코어 OFF (--no-news)")
    if not use_market:
        print(" 🌏 시황 + 뉴스/재료/AI 코멘트 엔진 OFF (--no-market)")
    print()

    try:
        token = get_token()
        # market_signal.py 에 토큰 공유 (403 방지)
        try:
            from market_signal import set_shared_token
            set_shared_token(token, CONFIG["APP_KEY"], CONFIG["APP_SECRET"])
        except ImportError:
            pass
    except Exception as e:
        print(f"\n⚠️  {e}")
        input("아무 키나 누르면 종료...")
        return

    # ── STEP 0: 시황 + 뉴스/재료 엔진 ────────────────────────────────────
    market_info = {"enabled": False, "mode": "미사용", "score_adjust": 0, "items": {}, "summary": "시황 + 뉴스/재료 엔진 OFF"}
    if use_market:
        try:
            print("  [STEP 0] 시황 데이터 수집 중...")
            market_info = collect_market_overview()
            print(f"    → {market_info.get('summary', '')}")
        except Exception as e:
            print(f"  ⚠️  시황 수집 실패: {e}")
            market_info = {"enabled": False, "mode": "수집실패", "score_adjust": 0, "items": {}, "summary": "시황 수집 실패"}

    # ── STEP 1: 전종목 시세 ────────────────────────────────────
    print("  [STEP 1] 전종목 시세 수집 중...")
    all_stocks = []
    for mc, mn in [("0001", "KOSPI"), ("1001", "KOSDAQ")]:
        stocks = fetch_ranking(mc, mn)
        before = len(stocks)
        stocks = [s for s in stocks if
                  s["vol_ratio"]    >= CONFIG["min_vol_ratio"] and
                  s["trade_amount"] >= CONFIG["min_trade_amount"]]
        print(f"    [{mn}] 랭킹:{before}종목 → 필터 후:{len(stocks)}종목")
        all_stocks += stocks

    if not all_stocks:
        print("\n⚠️  조건 통과 종목 없음.")
        print("   → 장 마감 후(15:30~) 실행하거나 CONFIG 조건을 낮춰보세요.")
        input("아무 키나 누르면 종료...")
        return

    # ── STEP 1-2: 거래대금/거래량 보완 ──────────────────────
    all_stocks = enrich_stock_list(all_stocks)
    all_stocks = [s for s in all_stocks if
                  s["vol_ratio"]    >= CONFIG["min_vol_ratio"] and
                  s["trade_amount"] >= CONFIG["min_trade_amount"]]

    min_mc = CONFIG.get("min_market_cap", 0)
    if min_mc > 0:
        before_mc = len(all_stocks)
        # market_cap이 0이면 API에서 시총 필드를 못 받은 종목이라 제외하지 않고 살려둔다.
        all_stocks = [s for s in all_stocks if s.get("market_cap", 0) == 0 or s.get("market_cap", 0) >= min_mc]
        print(f"    거래량/거래대금/시총 필터 후: {before_mc}종목 → {len(all_stocks)}종목")
    else:
        print(f"    거래량/거래대금 필터 후: {len(all_stocks)}종목")

    if not all_stocks:
        print("\n⚠️  거래량/거래대금 조건 통과 종목 없음.")
        input("아무 키나 누르면 종료...")
        return


    # ── STEP 2: 뉴스 스코어링 (병렬로 먼저 실행) ──────────────
    news_map     = {}
    market_sigs  = {}

    if use_news:
        print(f"\n  [STEP 2] 뉴스 수집 + 점수화 중...")

        # market_signal.py로 스크리닝 종목 시간외 데이터 수집
        try:
            from market_signal import get_market_signals_batch
            tickers = [s["ticker"] for s in all_stocks[:30]]  # 상위 30개만
            print(f"  📈 시간외 데이터 수집 중 ({len(tickers)}종목)...")
            market_sigs = get_market_signals_batch(tickers, delay=0.1)
        except ImportError:
            print("  ⚠️  market_signal.py 없음 → 시간외 데이터 생략")
        except Exception as e:
            print(f"  ⚠️  시간외 수집 실패: {e}")

        news_map = run_news_scoring(all_stocks, market_sigs)

    # ── STEP 3~5: 차트/수급/시간외 + 점수 통합 ────────────────
    print(f"\n  [STEP 3~5] v6.2 캔들/수급/NXT 시간외 수집 중... ({len(all_stocks)}종목)")
    results = []

    for idx, s in enumerate(all_stocks, 1):
        chart_info = get_chart_info(s["ticker"])
        is_52w = chart_info.get("is_near_high", False)
        ma     = chart_info.get("ma_aligned", False)
        foreign, inst = get_foreign_inst(s["ticker"])

        # 뉴스 단계에서 market_signal.py로 이미 시간외 데이터를 받았다면 재사용
        reused_after = get_after_market_from_signal(market_sigs.get(s["ticker"], {}))
        if reused_after:
            after_p, after, after_info = reused_after
        else:
            after_p, after, after_info = get_after_market(s["ticker"], base_close=s.get("close", 0))

        # NXT 미지원 확정 종목은 API 값이 잡혀도 전략 후보에서 제외 처리
        if is_nxt_unsupported(s.get("ticker", "")):
            after_p, after = 0, 0.0
            after_info = {"after_volume": 0, "after_amount": 0.0, "after_volume_ratio": 0.0, "after_source": "nxt_unsupported"}

        after_status, after_has_data, after_has_reaction = classify_after_status(
            after_p, after, after_info, ticker=s.get("ticker", "")
        )

        # 뉴스 점수 가져오기
        news_info   = news_map.get(s["ticker"], {})
        news_score  = news_info.get("news_score", 0)
        news_grade  = news_info.get("news_grade", "")
        news_kw     = news_info.get("news_kw", "")
        news_title  = news_info.get("news_title", "")
        news_flags  = news_info.get("news_flags", "")

        chart_sc, supply_sc, after_score, after_quality_score, close_strength_score, news_contrib, news_grade_bonus, overheat_penalty, upper_wick_penalty, candle_score, total_sc = calc_score(
            s["change_pct"], s["vol_ratio"],
            is_52w, ma, foreign, inst, after,
            news_score=news_score,
            news_grade=news_grade,
            close_position=s.get("close_position", 0),
            upper_wick_pct=s.get("upper_wick_pct", 0),
            chart_info=chart_info,
            after_has_data=after_has_data,
            after_has_reaction=after_has_reaction,
            after_info=after_info,
            regular_volume=s.get("volume", 0),
        )

        expected_info_base = {
            **s,
            "foreign": foreign,
            "inst": inst,
            "after_change": after,
            "after_status": after_status,
            "after_amount": safe_float(after_info.get("after_amount", 0), 0),
            "after_volume_ratio": safe_float(after_info.get("after_volume_ratio", 0), 0),
            "close_position": s.get("close_position", 0),
            "upper_wick_pct": s.get("upper_wick_pct", 0),
            "prev_high_break_pct": chart_info.get("prev_high_break_pct", 0),
            "up_streak": chart_info.get("up_streak", 0),
            "gap_up_pct": chart_info.get("gap_up_pct", 0),
            "news_grade": news_grade,
        }
        expected_info = calc_expected_gap_and_risk(expected_info_base)
        expected_gap_penalty = calc_expected_gap_penalty(expected_info.get("expected_gap_pct", 0))
        market_score_adjust = calc_market_score_adjustment(market_info, after_status=after_status)

        material_base = {
            **expected_info_base,
            "news_score": news_score,
            "news_grade": news_grade,
            "news_kw": news_kw,
            "news_title": news_title,
            "news_flags": news_flags,
        }
        material_type, material_reason, material_score_adjust = classify_material(material_base)
        final_total_score = max(0, total_sc + expected_gap_penalty + market_score_adjust + material_score_adjust)

        results.append({
            **s,
            "is_52w_high":  is_52w,
            "ma_aligned":   ma,
            "foreign":      foreign,
            "inst":         inst,
            "after_price":  after_p,
            "after_change": after,
            "after_status": after_status,
            "after_has_data": after_has_data,
            "after_has_reaction": after_has_reaction,
            "after_volume": safe_int(after_info.get("after_volume", 0), 0),
            "after_amount": safe_float(after_info.get("after_amount", 0), 0),
            "after_volume_ratio": safe_float(after_info.get("after_volume_ratio", 0), 0),
            "after_source": after_info.get("after_source", ""),
            "chart_score":  chart_sc,
            "supply_score": supply_sc,
            "news_score":   news_score,
            "news_grade":   news_grade,
            "news_kw":      news_kw,
            "news_title":   news_title,
            "news_flags":   news_flags,
            "after_score":   after_score,
            "after_quality_score": after_quality_score,
            "close_position": s.get("close_position", 0),
            "close_strength_score": close_strength_score,
            "upper_wick_pct": s.get("upper_wick_pct", 0),
            "prev_high_break": chart_info.get("prev_high_break", False),
            "prev_high_break_pct": chart_info.get("prev_high_break_pct", 0),
            "up_streak": chart_info.get("up_streak", 0),
            "long_bull": chart_info.get("long_bull", False),
            "gap_up_pct": chart_info.get("gap_up_pct", 0),
            "candle_score": candle_score,
            "news_contrib": news_contrib,
            "news_grade_bonus": news_grade_bonus,
            "overheat_penalty": overheat_penalty,
            "upper_wick_penalty": upper_wick_penalty,
            "expected_gap_pct": expected_info.get("expected_gap_pct", 0),
            "target_price": expected_info.get("target_price", 0),
            "risk_pct": expected_info.get("risk_pct", 0),
            "risk_price": expected_info.get("risk_price", 0),
            "rr_ratio": expected_info.get("rr_ratio", 0),
            "actual_reward_pct": expected_info.get("actual_reward_pct", 0),
            "actual_risk_pct": expected_info.get("actual_risk_pct", 0),
            "expected_gap_penalty": expected_gap_penalty,
            "market_mode": market_info.get("mode", ""),
            "market_score_adjust": market_score_adjust,
            "material_type": material_type,
            "material_reason": material_reason,
            "material_score_adjust": material_score_adjust,
            "total_score":  final_total_score,
            "memo":         "",
        })

        if idx % 10 == 0:
            print(f"    ... {idx}/{len(all_stocks)} 완료")
        time.sleep(0.06)

    results.sort(key=lambda x: x["total_score"], reverse=True)
    top = results[:top_n]
    top = attach_ai_comments(top, market_info=market_info)

    print_report(top, market_info=market_info)
    save_outputs(top, today_str, market_info=market_info)

    # 뉴스 매칭 종목 별도 출력
    news_matched = [r for r in top if r.get("news_score", 0) >= 55]
    if news_matched:
        print(f"\n  🎯 뉴스 B등급 이상 종목 ({len(news_matched)}개)")
        print("  " + "─"*60)
        for r in sorted(news_matched, key=lambda x: x["news_score"], reverse=True):
            grade_emoji = {"S": "🚀", "A": "🟢", "B": "🔵"}.get(r["news_grade"], "")
            print(f"  {grade_emoji} {r['name']:<12}  뉴스{r['news_score']:+3d}점({r['news_grade']})  "
                  f"키워드: {r['news_kw']}  │  {r['news_title']}")
            if r.get("news_flags"):
                print(f"       강한후보: {r['news_flags']}")
        print()

    print(f"\n 🎯 총 {len(top)}개 후보 추출 완료!")
    print(f" → {CONFIG['json_path']} 를 대시보드에 붙여넣으세요.")
    input("\n아무 키나 누르면 종료...")


if __name__ == "__main__":
    main()
