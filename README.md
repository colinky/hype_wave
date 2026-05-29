# Hype Wave — Cross-Platform Music Chart Sync & Trend Analysis

Apple Music · Spotify · Melon · YouTube Music 차트를 통합 분석하여 화제성 기반 플레이리스트를 자동 생성하는 도구입니다.

> **Live Dashboard**: [colinky.github.io/hype_wave](https://colinky.github.io/hype_wave)

---

## 🚀 주요 특징

- **통합 오케스트레이션**: `sync_config.json` 하나로 11개 작업을 순차 실행합니다.
- **강력한 매칭 엔진**:
  - **다국어 매칭**: Apple Music KR/US, Melon, Spotify의 영문/국문 메타데이터를 상호 참조합니다.
  - **변형 대응 (Variants Engine)**: "AKMU (악뮤)"와 "AKMU"를 동일 엔티티로 인식합니다.
  - **프록시 데이터**: 선행 작업에서 검증된 Video ID를 후속 작업에서 우선 활용합니다.
  - **수동 오버라이드**: `matching_alias.json`의 `overrides` 필드로 특정 곡을 강제 매칭할 수 있습니다.
- **DB 기반 아키텍처**:
  - SQLite DB (`hype_wave_data.db`, `ytmusic_cache.db`)를 중심으로 트랙 정규화, 순위 이력, 캐시를 관리합니다.
  - **Supabase Storage**: GitHub Actions 실행 간 DB 파일을 클라우드에 영속 보관합니다.
- **화제성 분석 (Hype Wave)**: Apple Music · Melon Gen-Z · YouTube Music 3개 차트를 통합한 Hype Index로 급상승 곡을 발굴합니다.
- **프론트엔드 대시보드**: `docs/api/history.json`에 최근 31일 이력을 누적하여 GitHub Pages에서 시각화합니다.

---

## 📋 현재 동기화 작업 (11개)

### Apple Music (4개)

| Job Name | Source | Target | Frequency | Limit |
| :--- | :--- | :--- | :--- | :--- |
| **KR-Top-Songs** | [오늘의 TOP: 대한민국](https://music.apple.com/kr/new/top-charts/songs) | [YTM](https://music.youtube.com/playlist?list=PLtawHGpcUVZXESVtlr4-HkbpRJQhhYZnr) | Daily | 200 |
| **KR-Top-100** ★ | [Top 100: South Korea](https://music.apple.com/us/playlist/top-100-south-korea/pl.d3d10c32fbc540b38e266367dc8cb00c) | [YTM](https://music.youtube.com/playlist?list=PLtawHGpcUVZWeko8ek3LWy9gav1sg9FFw) | Daily | 100 |
| **Seoul-Top-25** | [Top 25: Seoul](https://music.apple.com/us/playlist/top-25-seoul/pl.d6f003a501da4b3c9d33b0c7b8cfa0ae) | [YTM](https://music.youtube.com/playlist?list=PLtawHGpcUVZWrHIqj-huLBKgOiKVFkNLJ) | Daily | 25 |
| **Busan-Top-25** | [Top 25: Busan](https://music.apple.com/us/playlist/top-25-busan/pl.b4a7b0c2558941f68b329cde7774139a) | [YTM](https://music.youtube.com/playlist?list=PLtawHGpcUVZUUiQ122rfx-_OFMJNgPlFT) | Daily | 25 |

> ★ = Hype Wave 입력 소스 (`hype_group: apple`, 가중치 0.4)

### Spotify (2개)

| Job Name | Source | Target | Frequency | Shuffle |
| :--- | :--- | :--- | :--- | :--- |
| **Hot-Hits-Korea** | [Hot Hits Korea](https://open.spotify.com/playlist/37i9dQZF1DWT9uTRZAYj0c) | [YTM](https://music.youtube.com/playlist?list=PLtawHGpcUVZXOcgkI1oL0--mJiKZ5eGMI) | Friday | - |
| **Fresh-Indie-Korea** | [Fresh Finds Korea](https://open.spotify.com/playlist/37i9dQZF1DX7vZYLzFGQXc) & [Indie Korea](https://open.spotify.com/playlist/37i9dQZF1DXdTb8AG95jne) | [YTM](https://music.youtube.com/playlist?list=PLtawHGpcUVZUx53FaaLkidRHrOB7ebxDH) | Friday | ✅ |

### Melon (3개)

| Job Name | Source | Target | Frequency | Limit |
| :--- | :--- | :--- | :--- | :--- |
| **Top-100-Weekly** | [멜론 주간 차트](https://www.melon.com/chart/week/index.htm) | [YTM](https://music.youtube.com/playlist?list=PLtawHGpcUVZUzXhe99b4vMV9YF7IIFvir) | Monday | 100 |
| **Top-100-Daily** | [멜론 일간 차트](https://www.melon.com/chart/day/index.htm) | [YTM](https://music.youtube.com/playlist?list=PLtawHGpcUVZVwAYjPC7Jvc3BTZkQ1G966) | Daily | 100 |
| **Gen-Z-Daily** ★ | [멜론 세대별 차트 (Gen1 + Gen2)](https://kkosvc.melon.com/mwk/chart/gen.htm) | [YTM](https://music.youtube.com/playlist?list=PLtawHGpcUVZXC7USbMWiv9sbdFAHpTKqL) | Daily | 100 |

> ★ = Hype Wave 입력 소스 (`hype_group: melon_genz`, 가중치 0.6)

### YouTube Music (1개)

| Job Name | Source | Target | Frequency |
| :--- | :--- | :--- | :--- |
| **Weekly-Hot-100** ★ | [YouTube Charts: KR Weekly](https://charts.youtube.com/charts/TopSongs/kr/weekly) | [YTM](https://music.youtube.com/playlist?list=PLtawHGpcUVZWJV93R_C0xb8rnW1wx79Yx) | Sunday |

> ★ = Hype Wave 입력 소스 (`hype_group: ytmusic`, 가중치 0.0 — 순위 참조만)

### Hype Wave (1개)

| Job Name | Source | Target | Frequency | Limit |
| :--- | :--- | :--- | :--- | :--- |
| **Hype-Wave-Daily** | Apple + Melon Gen-Z + YTMusic 통합 | [YTM](https://music.youtube.com/playlist?list=PLtawHGpcUVZXKVkfIElFeaWB6g6d1o9Dr) | Daily | 100 |

---

## 🌊 Hype Wave Chart

`hype_moment.py` → `hype_db.py` → `hype_scoring.py`를 통해 생성되는 차트입니다.  
단순 합산 순위가 아닌 **Hype Index(화제성 지수)**를 기반으로 합니다.

### 데이터 소스 & 가중치

| 소스 | hype_group | 가중치 | 역할 |
| :--- | :--- | :--- | :--- |
| Apple Music KR Top 100 | `apple` | 0.4 | 트렌드 선행 지표 |
| Melon Gen-Z 차트 | `melon_genz` | 0.6 | 대중 인기 지표 |
| YouTube Music Weekly Hot 100 | `ytmusic` | 0.0 | 순위 참조 (보조) |

### 지수 계산 방식

각 소스 차트의 순위(1~100)를 `hype_scoring.py`의 `calculate_rank_score()`로 점수화합니다:
- **Power Score**: `1.1 × (100 / √rank − 10) + 1` — 상위권 파급력 강조
- **Exponential Score**: 지수 감쇠 함수 — 순위 구간별 차등 반영
- 두 점수의 평균을 최종 랭크 점수로 사용합니다.

가중치(`apple: 0.4`, `melon_genz: 0.6`)를 곱한 후 합산하여 **Hype Index**를 산출합니다.

### 모멘텀 지표

- **WAVE (🌊)**: Apple Music Top 100에 진입했으나 Melon에는 아직 없는 곡. 대중적 인기 폭발 직전 상태.
- **NEW WAVE (✨)**: 전날 차트에 없었으나 오늘 새로 진입하며 WAVE 조건도 만족하는 '가장 신선한' 트렌드 곡.

### 데이터 활용

- 모든 결과가 `docs/api/history.json`에 최근 31일간 누적됩니다.
- [HYPE WAVE Dashboard](https://colinky.github.io/hype_wave)에서 시각화된 차트를 확인할 수 있습니다.

---

## 🏗 아키텍처

```
sync_config.json                      ← 작업 정의 (v2 스키마)
       │
   sync_all.py                        ← 오케스트레이터 (순차 실행)
       │
       ├── apple_music_to_ytmusic_crawl.py    ← Apple Music 크롤러
       ├── spotify_to_ytmusic_crawl.py        ← Spotify 크롤러
       ├── melon_to_ytmusic_crawl.py          ← Melon 차트 크롤러
       ├── melon_gen_to_ytmusic_crawl.py      ← Melon 세대별 차트 크롤러
       ├── ytmusic_to_ytmusic_crawl.py        ← YouTube Music 차트 크롤러
       └── hype_moment.py                     ← Hype Wave 생성기
              │
              ├── hype_db.py           ← DB 스키마 · 트랙 정규화 · 리포트
              └── hype_scoring.py      ← 순위 점수 계산 함수
       │
   ytmusic_playlist_sync.py           ← 공통 유틸 (매칭 엔진, 플레이리스트 동기화)
   matching_alias.json                ← 별칭 · 오버라이드 설정
   sync_db_storage.py                 ← Supabase DB 업/다운로드
```

### DB 파일

| 파일 | 용도 |
| :--- | :--- |
| `hype_wave_data.db` | 트랙 정규화, 플랫폼별 순위 이력, Hype 리포트 |
| `ytmusic_cache.db` | YTMusic 검색 결과 캐시 (중복 API 호출 방지) |

두 DB 파일은 `.gitignore`에 의해 Git에서 제외되며, `sync_db_storage.py`를 통해 Supabase Storage에 영속 보관됩니다.

---

## 🛠 설정 및 운영

### 1. GitHub Secrets 설정

`Settings` → `Secrets and variables` → `Actions`에 아래 값을 추가합니다:

| Secret 이름 | 설명 |
| :--- | :--- |
| `YTMUSIC_BROWSER_JSON` | YouTube Music 인증 정보 (`browser.json` 전체 내용) |
| `SUPABASE_URL` | Supabase 프로젝트 URL |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase 서비스 롤 키 |

### 2. GitHub Actions 권한 설정

`Settings` → `Actions` → `General` → **Workflow permissions** → **Read and write permissions** 선택

### 3. YouTube Music 인증 (browser.json 생성)

```bash
pip install -r requirements.txt
ytmusicapi browser
```

생성된 `browser.json`의 내용을 `YTMUSIC_BROWSER_JSON` Secret에 저장합니다.

### 4. 동기화 작업 관리 (`sync_config.json` v2)

새로운 플레이리스트를 추가하려면 `sync_config.json` 배열에 아래 형식으로 추가합니다:

```json
{
    "job_name": "My-Chart",
    "enabled": true,
    "service": "apple | spotify | melon | melon_gen | ytmusic | hypex",
    "frequency": "daily | weekly",
    "list_type": "chart | playlist",
    "playlist_name": "my_chart_to_ytm",
    "source_urls": ["https://..."],
    "target_id": "YouTube-Playlist-ID",
    "entity_limit": 100,
    "schedule": "Monday",
    "shuffle": true,
    "include_in_hype": true,
    "hype_group": "apple | melon_genz | ytmusic",
    "hype_weight": 0.4
}
```

#### 필드 설명

| 필드 | 필수 | 설명 |
| :--- | :--- | :--- |
| `job_name` | ✅ | 작업 고유 이름 (로그 디렉토리명으로도 사용) |
| `service` | ✅ | 소스 서비스 (`apple`, `spotify`, `melon`, `melon_gen`, `ytmusic`, `hypex`) |
| `playlist_name` | ✅ | 내부 플레이리스트 식별자 |
| `source_urls` | ✅ | 소스 URL 배열 (`melon_gen`의 경우 `{gen, url, weight}` 객체 배열) |
| `target_id` | ✅ | 대상 YouTube Music 플레이리스트 ID |
| `enabled` | - | 작업 활성화 여부 (기본: `true`) |
| `frequency` | - | 실행 빈도 (`daily` / `weekly`) |
| `entity_limit` | - | 수집할 곡 수 제한 |
| `schedule` | - | 특정 요일에만 실행 (KST 기준, 예: `"Friday"`) |
| `shuffle` | - | 플레이리스트 저장 시 곡 순서 랜덤화 |
| `archive` | - | `crawl/` 디렉토리에 원본 데이터 보존 |
| `include_in_hype` | - | Hype Wave 계산 입력 소스 여부 |
| `hype_group` | - | Hype 그룹 식별자 |
| `hype_weight` | - | Hype 지수 가중치 (0.0 ~ 1.0) |

### 5. 별칭 및 오버라이드 관리 (`matching_alias.json`)

플랫폼마다 다른 아티스트/곡명 표기를 통합하거나, 매칭 실패 시 수동으로 Video ID를 지정할 수 있습니다.

```json
{
    "artists": [
        ["Young B", "YANGHONGWON", "양홍원"]
    ],
    "titles": [
        ["가나다", "ABC"]
    ],
    "albums": [],
    "overrides": {
        "소문의 낙원|AKMU (악뮤)": "VIDEO_ID_HERE"
    }
}
```

- `artists` / `titles` / `albums`: 동일 엔티티의 다른 표기를 묶는 클러스터 배열
- `overrides`: `"제목|아티스트": "video_id"` 형식으로 특정 곡을 강제 매칭

---

## 💻 로컬 실행

```bash
# 전체 작업 순차 실행
python sync_all.py

# 개별 크롤러 실행
python apple_music_to_ytmusic_crawl.py \
    --apple-playlist-urls "URL" \
    --yt-playlist-id "PLAYLIST_ID" \
    --yt-auth .secrets/browser.json \
    --job-name "KR-Top-Songs" \
    --playlist-name "app_krcc_to_ytm" \
    --apple-chart-limit 200

python melon_to_ytmusic_crawl.py \
    --melon-urls "URL" \
    --yt-playlist-id "PLAYLIST_ID" \
    --yt-auth .secrets/browser.json \
    --job-name "Top-100-Daily" \
    --playlist-name "mel_krdc_to_ytm"

# 드라이 런 (실제 업데이트 없음)
python apple_music_to_ytmusic_crawl.py --apple-playlist-urls "URL" --yt-playlist-id "ID" --dry-run

# DB 수동 관리
python sync_db_storage.py download          # Supabase에서 DB 다운로드
python sync_db_storage.py upload            # Supabase로 DB 업로드
python sync_db_storage.py download ytmusic_cache.db  # 특정 파일만
```

모든 매칭 로그는 `logs/{job_name}/` 디렉토리에 JSON 형태로 저장되어 사후 분석이 가능합니다.

---

## ⚙️ GitHub Actions 워크플로우

`daily-sync.yml` — 매일 KST 16:02에 자동 실행 (수동 트리거 가능)

```
1. Checkout → Python 3.11 설정 → 의존성 설치
2. Supabase에서 DB 다운로드 (hype_wave_data.db, ytmusic_cache.db)
3. sync_all.py 실행 (sync_config.json 기반 전체 작업)
4. 업데이트된 DB를 Supabase에 업로드
5. docs/api/history.json 변경 시 자동 커밋 & 푸시 (GitHub Pages 업데이트)
6. logs/ 를 Actions Artifact로 업로드
```

---

## 📦 의존성

```
requests>=2.31.0
PyJWT[crypto]>=2.8.0
ytmusicapi>=1.10.0
beautifulsoup4>=4.12.0
```

```bash
pip install -r requirements.txt
```

---

## 📁 프로젝트 구조

```
hype_wave/
├── sync_config.json               # 동기화 작업 정의 (v2 스키마)
├── sync_all.py                    # 통합 오케스트레이터
├── apple_music_to_ytmusic_crawl.py
├── spotify_to_ytmusic_crawl.py
├── melon_to_ytmusic_crawl.py
├── melon_gen_to_ytmusic_crawl.py
├── ytmusic_to_ytmusic_crawl.py
├── hype_moment.py                 # Hype Wave 차트 생성기
├── hype_db.py                     # DB 스키마 · 트랙 정규화 · 리포트 쿼리
├── hype_scoring.py                # 순위 점수 계산 (Power + Exponential)
├── ytmusic_playlist_sync.py       # 공통 매칭 엔진 · 플레이리스트 동기화
├── sync_db_storage.py             # Supabase Storage 업/다운로드
├── matching_alias.json            # 별칭 클러스터 · 수동 오버라이드
├── requirements.txt
├── .github/workflows/
│   └── daily-sync.yml             # GitHub Actions 워크플로우
├── docs/
│   ├── index.html                 # Hype Wave 프론트엔드 대시보드
│   └── api/
│       └── history.json           # 최근 31일 Hype 차트 이력 (자동 생성)
└── logs/                          # 작업별 매칭 로그 (자동 생성, gitignore)
```