# Overnight Screener Automation

## 1. 필요한 파일

아래 파일들을 GitHub 저장소 루트에 둡니다.

- overnight_screener_v6_6_claude_comment.py
- overnight_dashboard_v3_synced.html
- telegram_notifier.py
- requirements.txt
- .github/workflows/overnight.yml

## 2. GitHub Secrets 등록

GitHub 저장소 → Settings → Secrets and variables → Actions → New repository secret

필수:
- KIS_APP_KEY
- KIS_APP_SECRET
- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID

선택:
- ANTHROPIC_API_KEY
- DASHBOARD_URL
- MARKET_NASDAQ_FUTURE
- MARKET_SOX
- MARKET_USDKRW

## 3. GitHub Pages 켜기

Settings → Pages

- Source: Deploy from a branch
- Branch: main
- Folder: /docs

생성 URL 예:
https://깃허브아이디.github.io/저장소명/

이 URL을 DASHBOARD_URL Secret에 넣으면 텔레그램 메시지에도 링크가 붙습니다.

## 4. 실행 시간

workflow는 한국시간 기준 평일:
- 15:40
- 19:00

에 자동 실행됩니다.

GitHub Actions cron은 UTC 기준이라 workflow 내부에는:
- 06:40 UTC
- 10:00 UTC

로 들어가 있습니다.

## 5. 수동 실행

GitHub 저장소 → Actions → Overnight Screener → Run workflow
