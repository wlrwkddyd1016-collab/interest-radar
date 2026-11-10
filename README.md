# 관심사 레이더

연령대·성별마다 요즘 무엇을 많이 찾는지 매일 아침 7시에 자동으로 업데이트되는 사이트입니다.

## 작동 방식

1. **키워드 모으기**: 유튜브 한국 인기 급상승 영상(카테고리 9개)에서 여러 채널이 함께 쓴 태그를 모으고, 구글 한국 급상승 검색어도 함께 모읍니다. 하루에 키워드 40개를 씁니다.
2. **연령·성별 비율 계산**: 모은 키워드를 네이버 데이터랩에 넣어 최근 7일 검색량을 10대~60대+ × 남/녀, 모두 12개 그룹으로 나눠 조회합니다.
3. **사이트 갱신**: GitHub Actions가 매일 자동으로 실행해 `docs/data/날짜.json`을 저장하고, GitHub Pages가 사이트를 보여줍니다.

> 유튜브는 연령·성별별 검색 데이터를 공개하지 않습니다. 그래서 키워드는 유튜브에서 가져오고, 연령·성별 비율은 같은 키워드의 **네이버 검색량**으로 계산합니다.
> 네이버 데이터랩은 요청마다 값을 0~100으로 다시 맞추기 때문에, 요청끼리 비교할 수 있도록 매번 기준어("날씨")를 함께 조회해 그 검색량을 1로 둡니다.

## 사이트 화면

- **많이 찾는 것**: 고른 연령·성별의 검색량 순위
- **유독 더 찾는 것**: 전체 평균보다 몇 배 더 찾는지 순위. 그 층만의 취향이 보입니다.
- **한눈에 보기**: 키워드 × 12개 그룹 히트맵

---

## 설정 방법 (처음 한 번만, 약 15분)

### 1. 네이버 API 키 받기 (필수)
2026년 7월 31일부터 데이터랩 API는 네이버 개발자센터가 아니라 **네이버 클라우드 플랫폼의 NAVER API HUB**에서 신청합니다.
1. https://console.ncloud.com 에 로그인합니다. 가입과 결제수단 등록은 직접 해야 합니다.
2. 왼쪽 위 **Menu → All Services → Application Services → NAVER API HUB**로 들어갑니다.
3. 왼쪽 메뉴에서 **Application**을 누르고 **[Application 등록]**을 누릅니다.
4. API 목록에서 **검색어 트렌드**를 체크하고 **[다음]**을 누릅니다. 이름은 아무거나 입력하고 **[완료]**를 누릅니다.
5. 만든 Application을 선택하고 **API 관리 → [인증 정보]**를 누르면 나오는 **Client ID**와 **Client Secret**을 적어둡니다.

이 프로그램은 하루 약 130회를 호출합니다. 요금과 무료 한도는 https://www.ncloud.com/product/applicationService/NAVER%20API%20HUB 에서 확인하세요.
2026년 7월 31일 이전에 개발자센터에서 받은 키가 있다면, 환경변수 `NAVER_API=legacy`를 추가해 그 키를 그대로 쓸 수 있습니다.

### 2. 유튜브 API 키 받기 (권장, 무료)
1. https://console.cloud.google.com/ 에서 새 프로젝트를 만듭니다.
2. **API 및 서비스 → 라이브러리**에서 **YouTube Data API v3**을 사용 설정합니다.
3. **사용자 인증 정보 → 사용자 인증 정보 만들기 → API 키**를 누르면 키가 나옵니다.

이 키가 없으면 구글 급상승 검색어만으로 동작합니다.

### 3. 내 PC에서 먼저 실행해보기 (선택)
`.env.example`을 복사해 `.env`를 만들고 키를 채운 뒤 실행합니다.
```
python collect.py
python -m http.server 8765 --directory docs
```
브라우저에서 http://localhost:8765 를 엽니다.

### 4. GitHub에 올리기
1. GitHub에서 새 저장소를 만듭니다. Public으로 만들어야 Pages를 무료로 쓸 수 있습니다.
2. 이 폴더 전체를 올립니다. `.env`는 `.gitignore`에 있어서 올라가지 않습니다.
3. 저장소에서 **Settings → Secrets and variables → Actions → New repository secret**을 눌러 아래 3개를 추가합니다.
   - `NAVER_CLIENT_ID`
   - `NAVER_CLIENT_SECRET`
   - `YOUTUBE_API_KEY`
4. **Settings → Pages**에서 Source를 *Deploy from a branch*, Branch를 `main` / `/docs`로 설정하고 저장합니다.
5. **Actions** 탭에서 *매일 데이터 수집*을 열고 **Run workflow**를 눌러 첫 수집을 돌립니다.
6. 몇 분 뒤 `https://내아이디.github.io/저장소이름/` 에서 사이트를 볼 수 있습니다.

이후에는 매일 한국시간 오전 7시에 자동으로 갱신됩니다. 지난 날짜도 사이트 오른쪽 위에서 골라 볼 수 있습니다.

## 바꿀 수 있는 것
- 키워드 개수: `collect.py`의 `MAX_KEYWORDS` (기본 40, 네이버 한도 안에서 최대 약 300)
- 기준어: 환경변수 `ANCHOR_KEYWORD` (기본 "날씨")
- 제외할 일반 태그: `collect.py`의 `STOPWORDS`
- 실행 시간: `.github/workflows/daily.yml`의 `cron` (UTC 기준, `0 22` = 한국 오전 7시)
