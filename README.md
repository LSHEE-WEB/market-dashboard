# 원자재 시황 대시보드

환율·유가·석유화학/펄프 시세를 한 화면에 모아 보는 개인용 웹사이트입니다.
**서버·DB·회원가입 없이** GitHub만으로 무료 운영됩니다.

| 구분 | 품목 | 출처 | 수집 방식 |
|---|---|---|---|
| 환율 | 원/달러, 원/위안, 원/100엔 | 서울외국환중개 매매기준율 | 자동 수집 (실패 시 Yahoo Finance로 대체) |
| 유가 | WTI, 브렌트 | NYMEX·ICE 선물 (Yahoo Finance) + TradingView 실시간 차트 | 자동 수집 + 실시간 위젯 |
| 석유화학·펄프 | 나프타, 에틸렌, 펄프(SBHK) | 산업통상부 원자재가격정보 | 자동 수집 |

## 동작 방식

```
GitHub Actions (30분마다)          GitHub Pages (누구나 접속)
 scripts/fetch_data.py  ──커밋──▶  data/prices.json  ◀──읽기── index.html
        │
        └─ 직전 대비 ±5% 이상이면 → GitHub 이슈 생성(이메일 알림) / 텔레그램(선택)
```

- 사이트를 **열 때마다** 최신 `prices.json`을 불러오고, 열어둔 동안에도 5분마다 자동 갱신합니다.
- 유가 실시간 차트는 TradingView 위젯이 직접 그리므로 항상 실시간입니다.
- 환율은 처음 실행할 때 최근 약 4개월치를 자동으로 백필합니다. 나프타·에틸렌·펄프는 수집 시작 시점부터 데이터가 쌓입니다.

---

## 설치 (약 10분, 한 번만)

### 1. 저장소 만들기
1. [github.com](https://github.com) 로그인 → 오른쪽 위 **+ → New repository**
2. 이름 예: `market-dashboard`, **Public** 선택 → Create
3. **Add file → Upload files**로 이 폴더의 파일을 전부 올립니다.
   - `.github` 폴더는 숨김 폴더라 끌어다 놓기가 안 될 수 있습니다. 그럴 땐 **Add file → Create new file**에서
     파일명에 `.github/workflows/update-data.yml`을 입력하고 내용을 붙여넣으세요.

### 2. 쓰기 권한 켜기
**Settings → Actions → General → Workflow permissions** → **Read and write permissions** 선택 → Save

### 3. 웹사이트 공개 (GitHub Pages)
**Settings → Pages → Build and deployment**
- Source: **Deploy from a branch**
- Branch: **main** / **(root)** → Save

1~2분 뒤 `https://<내아이디>.github.io/market-dashboard/` 주소가 생깁니다.

### 4. 첫 수집 실행
**Actions** 탭 → 왼쪽 **시황 데이터 자동 수집** → **Run workflow**.
초록색 ✔가 뜨면 사이트를 새로고침하세요. 이후로는 30분마다 자동 실행됩니다.

---

## 알림 설정

기본 기준은 **직전 관측치 대비 ±5%** 입니다.
(환율·유가 = 전 영업일 대비 / 나프타·에틸렌·펄프 = 직전 발표치 대비)

1. **사이트 화면**: 상단에 빨간 알림 배너가 뜨고 해당 카드가 강조됩니다.
2. **브라우저 알림**: 사이트에서 "브라우저 알림 켜기"를 누르면, 사이트를 열었을 때 새 급변동이 있으면 팝업이 뜹니다.
3. **이메일 (기본 켜짐)**: 급변동 시 저장소에 `[시황 알림]` 이슈가 자동 생성되고, GitHub가 저장소 주인에게 이메일을 보냅니다.
   - 메일이 안 오면: 저장소 오른쪽 위 **Watch → All Activity** 선택, 그리고 GitHub **Settings → Notifications**에서 Email이 켜져 있는지 확인.
4. **텔레그램 (선택, 휴대폰 푸시)**
   1. 텔레그램에서 `@BotFather` → `/newbot` → 봇 토큰 받기
   2. 만든 봇에게 아무 메시지 보내기 → 브라우저에서 `https://api.telegram.org/bot<토큰>/getUpdates` 열어 `chat.id` 확인
   3. 저장소 **Settings → Secrets and variables → Actions → New repository secret**
      - `TELEGRAM_BOT_TOKEN` = 봇 토큰, `TELEGRAM_CHAT_ID` = chat id

**기준 바꾸기**: Settings → Secrets and variables → Actions → **Variables** 탭 → `ALERT_THRESHOLD` = `3` (3%) 등.
같은 변동은 한 번만 알립니다.

---

## 문제 해결

- **카드에 "최근 수집 실패"가 보일 때**: 원출처 사이트 구조가 바뀐 경우입니다. 수집기가 받아온 원본을
  `data/debug/` 폴더에 저장해 두니, 이 파일을 보면 원인을 찾을 수 있습니다.
- **산업통상부 데이터**: 산업통상부 원자재가격정보 페이지는 KoreaPDS가 제공하는 표
  (`https://www.koreapds.com/wacs/wms/kpds_price.php`)를 보여줍니다. 수집기는 이 표에서 `나프타`, `에틸렌`,
  `SBHK/활엽수 펄프` 행을 찾아 값을 읽습니다. 주소가 바뀌면 Variables에 `KPDS_URL`을 추가하고
  워크플로의 env에 `KPDS_URL: ${{ vars.KPDS_URL }}`을 넣으세요.
- **환율이 Yahoo로 표시될 때**: 서울외국환중개 접속이 실패해 대체 소스를 쓴 것입니다(카드에 표시됨).
- **GitHub 안내**: 공개 저장소에서 60일 동안 활동이 없으면 예약 실행이 멈출 수 있습니다. 데이터 커밋이 계속 생기므로
  보통 문제없지만, 멈추면 Actions 탭에서 다시 Enable 하세요.
- **로컬에서 확인**: `python -m http.server` 실행 후 `http://localhost:8000` 접속
  (`index.html`을 더블클릭으로 열면 데이터 파일을 못 읽습니다).

## 파일 구성

```
index.html                         대시보드 화면 (이 파일 하나가 사이트)
data/prices.json                   수집된 데이터 (자동 갱신)
scripts/fetch_data.py              수집기 + 알림
scripts/requirements.txt
.github/workflows/update-data.yml  30분마다 실행되는 자동 수집
```
