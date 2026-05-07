"""
market_signal.py  ─  한국투자증권 API 연동 버전
------------------------------------------------------
수정사항:
  - 토큰을 매번 새로 발급하지 않고 캐시 파일에서 재사용
  - overnight_screener_v5.py 에서 발급한 토큰 공유
  - 403 Forbidden 오류 수정
------------------------------------------------------
"""

import os
import time
import json
import requests
from datetime import datetime, timedelta
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

BASE_URL = "https://openapi.koreainvestment.com:9443"
TOKEN_CACHE_FILE = Path(".kis_token_cache.json")
VOLUME_SURGE_THRESHOLD = 2.0

# ── 외부 토큰 주입용 전역 변수 ────────────────────────
_SHARED_TOKEN  = None
_SHARED_KEY    = None
_SHARED_SECRET = None

def set_shared_token(token: str, app_key: str, app_secret: str):
    """overnight_screener_v5.py 에서 토큰 주입"""
    global _SHARED_TOKEN, _SHARED_KEY, _SHARED_SECRET
    _SHARED_TOKEN  = token
    _SHARED_KEY    = app_key
    _SHARED_SECRET = app_secret


# ── 토큰 관리 ─────────────────────────────────────────
class KISTokenManager:
    def __init__(self):
        self.app_key    = _SHARED_KEY    or os.environ.get("KIS_APP_KEY",    "")
        self.app_secret = _SHARED_SECRET or os.environ.get("KIS_APP_SECRET", "")
        self._token         = _SHARED_TOKEN
        self._token_expires = None

        if not self.app_key or not self.app_secret:
            raise EnvironmentError("KIS_APP_KEY 또는 KIS_APP_SECRET 환경변수가 없습니다.")

    def get_token(self) -> str:
        # 1순위: 외부 주입 토큰
        if self._token:
            return self._token

        # 2순위: 캐시 파일
        cached = self._load_token_cache()
        if cached:
            self._token         = cached["token"]
            self._token_expires = datetime.fromisoformat(cached["expires"])
            if self._is_token_valid():
                return self._token

        # 3순위: 신규 발급
        return self._issue_token()

    def _is_token_valid(self) -> bool:
        if not self._token or not self._token_expires:
            return False
        return datetime.now() < self._token_expires - timedelta(minutes=10)

    def _issue_token(self) -> str:
        resp = requests.post(
            f"{BASE_URL}/oauth2/tokenP",
            json={"grant_type": "client_credentials",
                  "appkey": self.app_key, "appsecret": self.app_secret},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()

        self._token         = data["access_token"]
        expires_in          = int(data.get("expires_in", 86400))
        self._token_expires = datetime.now() + timedelta(seconds=expires_in)
        self._save_token_cache()
        print(f"[KIS] 토큰 발급 완료 (만료: {self._token_expires.strftime('%Y-%m-%d %H:%M')})")
        return self._token

    def _load_token_cache(self):
        if TOKEN_CACHE_FILE.exists():
            try:
                return json.loads(TOKEN_CACHE_FILE.read_text())
            except Exception:
                pass
        return None

    def _save_token_cache(self):
        try:
            TOKEN_CACHE_FILE.write_text(json.dumps({
                "token":   self._token,
                "expires": self._token_expires.isoformat()
            }))
        except Exception:
            pass


# ── 공통 헤더 ─────────────────────────────────────────
def _headers(token: str, tr_id: str, app_key: str, app_secret: str) -> dict:
    return {
        "Content-Type":  "application/json; charset=utf-8",
        "authorization": f"Bearer {token}",
        "appkey":        app_key,
        "appsecret":     app_secret,
        "tr_id":         tr_id,
        "custtype":      "P",
    }


# ── API 호출 ──────────────────────────────────────────
def _get_stock_price(token, app_key, app_secret, stock_code):
    url    = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-price"
    params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": stock_code}
    resp   = requests.get(url, headers=_headers(token, "FHKST01010100", app_key, app_secret),
                          params=params, timeout=10)
    resp.raise_for_status()
    return resp.json().get("output", {})


def _get_overtime_price(token, app_key, app_secret, stock_code):
    url    = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-overtime-price"
    params = {"fid_cond_mrkt_div_code": "J", "fid_input_iscd": stock_code}
    resp   = requests.get(url, headers=_headers(token, "FHKST02010300", app_key, app_secret),
                          params=params, timeout=10)
    resp.raise_for_status()
    return resp.json().get("output", {})


def _get_daily_chart(token, app_key, app_secret, stock_code, days=25):
    url    = f"{BASE_URL}/uapi/domestic-stock/v1/quotations/inquire-daily-price"
    today  = datetime.now().strftime("%Y%m%d")
    params = {
        "fid_cond_mrkt_div_code": "J",
        "fid_input_iscd":         stock_code,
        "fid_input_date_1":       (datetime.now() - timedelta(days=days+10)).strftime("%Y%m%d"),
        "fid_input_date_2":       today,
        "fid_period_div_code":    "D",
        "fid_org_adj_prc":        "1",
    }
    resp = requests.get(url, headers=_headers(token, "FHKST03010100", app_key, app_secret),
                        params=params, timeout=10)
    resp.raise_for_status()
    return resp.json().get("output2", [])


# ── 핵심 함수 ─────────────────────────────────────────
def get_market_signal(stock_code: str, token_manager: KISTokenManager = None) -> dict:
    result = {
        "stock_code":          stock_code,
        "close_change_pct":    0.0,
        "overtime_change_pct": None,
        "volume_surge_ratio":  1.0,
        "volume_surge":        False,
        "market_score":        0,
        "signal_basis":        "none",
        "error":               None,
    }

    try:
        if token_manager is None:
            token_manager = KISTokenManager()

        token      = token_manager.get_token()
        app_key    = token_manager.app_key
        app_secret = token_manager.app_secret

        # 1) 당일 종가 등락률
        price_data = _get_stock_price(token, app_key, app_secret, stock_code)
        if price_data:
            result["close_change_pct"] = float(price_data.get("prdy_ctrt", 0))
            result["_today_volume"]    = int(price_data.get("acml_vol", 0))

        # 2) 시간외 단일가
        try:
            ot_data = _get_overtime_price(token, app_key, app_secret, stock_code)
            if ot_data:
                result["overtime_change_pct"] = float(ot_data.get("ovtm_unpr_prdy_ctrt", 0))
        except Exception:
            result["overtime_change_pct"] = None

        # 3) 거래량 급증
        try:
            chart = _get_daily_chart(token, app_key, app_secret, stock_code, days=25)
            if len(chart) >= 5:
                vols      = [int(d.get("acml_vol", 0)) for d in chart[:20] if d.get("acml_vol")]
                avg_vol   = sum(vols) / len(vols) if vols else 1
                today_vol = result.get("_today_volume", 0)
                ratio     = today_vol / avg_vol if avg_vol > 0 else 1.0
                result["volume_surge_ratio"] = round(ratio, 2)
                result["volume_surge"]       = ratio >= VOLUME_SURGE_THRESHOLD
        except Exception:
            pass

        result["market_score"], result["signal_basis"] = _calc_market_score(result)

    except Exception as e:
        result["error"] = str(e)

    return result


def _calc_market_score(data: dict) -> tuple:
    pct   = data.get("overtime_change_pct")
    basis = "overtime"

    if pct is None:
        pct   = data.get("close_change_pct", 0.0)
        basis = "close"

    if   pct >= 5.0:  score = 15
    elif pct >= 3.0:  score = 12
    elif pct >= 1.0:  score = 9
    elif pct >= 0.0:  score = 6
    elif pct >= -1.0: score = 3
    else:             score = 0

    if data.get("volume_surge"):
        score  = min(15, score + 2)
        basis += "+volume"

    return score, basis


# ── 배치 처리 ─────────────────────────────────────────
def get_market_signals_batch(stock_codes: list, delay: float = 0.3) -> dict:
    manager = KISTokenManager()
    results = {}

    for i, code in enumerate(stock_codes):
        print(f"  [{i+1}/{len(stock_codes)}] {code} 조회 중...", end=" ")
        sig = get_market_signal(code, token_manager=manager)
        results[code] = sig

        if sig["error"]:
            print(f"❌ {sig['error']}")
        else:
            ot  = f"{sig['overtime_change_pct']:+.1f}%" if sig["overtime_change_pct"] is not None else "N/A"
            vol = "🔥급증" if sig["volume_surge"] else ""
            print(f"종가{sig['close_change_pct']:+.1f}% / 시간외{ot} / 점수{sig['market_score']}점 {vol}")

        if i < len(stock_codes) - 1:
            time.sleep(delay)

    return results


if __name__ == "__main__":
    print("=" * 50)
    print("market_signal.py 테스트")
    print("=" * 50)
    signals = get_market_signals_batch(["000660", "005930"])
    for code, sig in signals.items():
        if not sig["error"]:
            ot = f"{sig['overtime_change_pct']:+.1f}%" if sig["overtime_change_pct"] is not None else "N/A"
            print(f"{code}  종가{sig['close_change_pct']:+.1f}%  시간외{ot}  "
                  f"거래량{sig['volume_surge_ratio']:.1f}배  → {sig['market_score']}점")
