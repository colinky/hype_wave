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
  - `HYPE_DB_BACKEND=aiven`으로 Aiven PostgreSQL을 사용합니다. GitHub Actions도 같은 전용 계정·연결 설정을 사용합니다.
  - `HYPE_DB_BACKEND=sqlite`로 로컬 SQLite를 명시적으로 선택할 수 있습니다. Aiven 연결 실패 시 다른 DB로 전환하지 않습니다.
  - 트랙 정규화, 플랫폼별 순위, 매칭 이력, Hype 리포트를 DB에 누적합니다.
- **화제성 분석 (Hype Wave)**: Apple Music · Melon Gen-Z · YouTube Music 3개 차트를 통합한 Hype Index로 급상승 곡을 발굴합니다.
- **프론트엔드 대시보드**: `docs/api/history.json`에 최근 31일 이력을 누적하여 GitHub Pages에서 시각화합니다.

---

## 📋 현재 동기화 작업 (11개)

예정 실행 요일과 내부 실행 기준일은 UTC 기준입니다. 원본에 명시된 차트 날짜와 집계 기간은 그대로 사용합니다.

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

> ★ = Hype Wave 입력 소스 (`hype_group: melon_genz`, 가중치 0.4)

### YouTube Music (1개)

| Job Name | Source | Target | Frequency |
| :--- | :--- | :--- | :--- |
| **Weekly-Hot-100** ★ | [YouTube Charts: KR Weekly](https://charts.youtube.com/charts/TopSongs/kr/weekly) | [YTM](https://music.youtube.com/playlist?list=PLtawHGpcUVZWJV93R_C0xb8rnW1wx79Yx) | Monday |

> ★ = Hype Wave 입력 소스 (`hype_group: ytmusic`, 가중치 0.2)

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
| Apple Music KR Top 100 | `apple` | 0.4 | 빠른 반응 신호 |
| Melon Gen-Z 차트 | `melon_genz` | 0.4 | 10·20대 중심 트렌드 |
| YouTube Music Weekly Hot 100 | `ytmusic` | 0.2 | 숏폼, 알고리즘 효과 |

Hype Wave는 “이미 가장 대중적인 곡”을 다시 나열하는 차트가 아닙니다. Apple Music의 빠른 진입 신호, Melon Gen-Z의 트렌드, YouTube Music의 확산도를 함께 보면서 **지금 커지고 있지만 아직 완전히 대중 차트로 굳어지지 않은 곡**을 찾기 위한 차트입니다.

### 지수 계산 방식

각 소스 차트의 순위(1~100)를 `hype_scoring.py`의 `calculate_rank_score()`로 점수화합니다:
- **Power Score**: `1.1 × (100 / √rank − 10) + 1` — 상위권 파급력 강조
- **Exponential Score**: 지수 감쇠 함수 — 순위 구간별 차등 반영
- 두 점수의 평균을 최종 랭크 점수로 사용합니다.

가중치(`apple: 0.4`, `melon_genz: 0.4`, `ytmusic: 0.2`)를 곱한 후 합산하여 **Hype Index**를 산출합니다.

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
```

### DB 구성

| 항목 | 용도 |
| :--- | :--- |
| Aiven PostgreSQL | 운영 및 로컬 기본 DB. `HYPE_DB_BACKEND=aiven`과 Aiven URI·호스트·CA 사용 |
| Supabase PostgreSQL | 원본 백업·명시적인 복구 작업에서만 사용 |
| `hype_wave_data.db` | `HYPE_DB_BACKEND=sqlite`에서 사용하는 로컬 DB |
| 번역 캐시 | 선택한 PostgreSQL의 번역 테이블을 사용. SQLite 모드에서는 `ytmusic_cache.db` 사용 |

주요 테이블:

| 테이블 | 설명 |
| :--- | :--- |
| `tracks` | 정규화된 트랙 엔티티와 canonical YouTube video ID |
| `platform_song_ids` | Apple/Melon/Spotify/YTMusic song_id와 `track_uid` 매핑 |
| `yt_video_ids` | YouTube video ID와 `track_uid` 매핑 |
| `track_list` | 플랫폼별 원본 메타데이터와 artwork |
| `playlist_order` | 서비스 · 작업 · 기준일별 원본 차트 순위 |
| `match_runs` / `match_attempts` / `match_candidates` | 매칭 실행 이력과 후보 |
| `metadata_lookup_index` | 제목/아티스트/앨범 기반 bulk cache lookup |
| `playlist_update_runs` / `playlist_update_items` | YouTube Music playlist 업데이트 audit |

Aiven의 테이블·인덱스·RLS·권한은 관리자 계정으로 미리 설치합니다. 실행 계정 `hype_sync`는 업무 데이터만 수정하며 스키마 생성·변경을 하지 않습니다. Supabase PostgreSQL 사용 시 `hype_db.py`가 hot path에 필요한 index를 1회 확인하고 누락된 index를 생성합니다. `sync_all.py`는 부모 프로세스에서 index를 먼저 확인한 뒤 child 작업에는 `HYPE_SKIP_POSTGRES_INDEX_CHECK=1`을 전달해 중복 원격 확인을 줄입니다.

### 매칭 파이프라인 DB I/O

1. **매칭 전 raw chart 저장**
   - `playlist_order`, `tracks`, `platform_song_ids`, `track_list`에 원본 순위를 bulk upsert합니다.
   - 원본 차트 보존을 위해 매칭 시작 전에 명시적으로 commit합니다.
2. **매칭 전 cache prefetch**
   - `get_bulk_cached_matches()`가 현재 작업 tracks만 대상으로 `manual_overrides`, `platform_song_ids`, `metadata_lookup_index`, `yt_video_ids`, `tracks`, `track_list`를 bulk 조회합니다.
   - Spotify도 전체 cache load 없이 현재 playlist tracks만 targeted 조회합니다.
3. **매칭 루프**
   - DB 왕복 없이 메모리 cache와 YouTube Music 검색 결과를 사용합니다.
4. **매칭 후 결과 저장**
   - `match_runs`, `tracks`, `yt_video_ids`, `platform_song_ids`, `track_list`, `metadata_lookup_index`, `match_attempts`, `match_candidates`를 bulk upsert합니다.
   - 오래된 `match_attempts` / `match_candidates`는 DB별 SQL로 정리합니다.
5. **프론트엔드 history export**
   - `sync_all.py`에서는 각 child 작업마다 export하지 않고, 전체 작업 완료 후 `docs/api/history.json`을 1회 갱신합니다.
   - 기본 동작은 최신 날짜만 갱신하고 31일을 초과한 history를 pruning합니다.

---

## 🛠 설정 및 운영

### 로컬 Aiven 설정

`.env.example`의 형식으로 `.secrets/.env`에 `HYPE_DB_BACKEND=aiven`, `AIVEN_DB_URI`, `AIVEN_DB_HOST`, `AIVEN_DB_CA_CERTIFICATE`를 설정합니다. URI는 `hype_sync` 계정과 `sslmode=require`를 사용합니다. 인증서는 한 줄 문자열에 `\n`으로 줄바꿈을 표현하며 실행 중 권한 600의 임시 PEM 파일로 생성됩니다. 로컬 절대 경로를 Secret에 저장하지 않습니다.

`sync_all.py`와 개별 크롤러는 환경 파일을 읽습니다. 이미 설정된 셸 환경변수가 우선하므로 이전 `HYPE_DB_BACKEND` 값이 남아 있으면 실행 시 명시적으로 지정합니다. 선택값이 없으면 기존 Supabase/SQLite 동작을 유지합니다.

2026-09-19 이전 백업과 검증 기록은 `backups/aiven_migration_20260919/`에 보관하며 Git에서 제외합니다. 관리자 접속 정보는 `.secrets/aiven-migration-admin.env`에 별도 보관하고 앱에는 제공하지 않습니다. 상세 기록: [Aiven 이전 기록](AIVEN_MIGRATION_PLAN.md).

### 1. GitHub Secrets 설정

`Settings` → `Secrets and variables` → `Actions`에 아래 값을 추가합니다:

| Secret 이름 | 설명 |
| :--- | :--- |
| `YTMUSIC_BROWSER_JSON` | YouTube Music 인증 정보 (`browser.json` 전체 내용) |
| `AIVEN_DB_URI` | 전용 `hype_sync` 계정 URI. `sslmode=require` 유지 |
| `AIVEN_DB_HOST` | URI와 동일한 Aiven 호스트 |
| `AIVEN_DB_CA_CERTIFICATE` | 인증서 내용. 여러 줄 PEM 또는 `\n`으로 줄바꿈을 표시한 한 줄 문자열 |

### 2. GitHub Actions 권한 설정

`Settings` → `Actions` → `General` → **Workflow permissions** → **Read and write permissions** 선택

### 3. YouTube Music 인증 (browser.json 생성)

```bash
pip install -r requirements.txt
ytmusicapi browser
```

생성된 `browser.json`의 내용을 `YTMUSIC_BROWSER_JSON` Secret에 저장합니다.

#### Supabase 공개 접근 차단

대시보드는 `docs/api/history.json`을 읽고, Actions는 `AIVEN_DB_URI`의 `hype_sync` 계정으로 PostgreSQL에 직접 접속합니다. 브라우저용 `anon` / `authenticated` 및 미사용 `service_role`의 앱 DB 권한은 필요하지 않습니다.

기존 Supabase에는 `db_migrations/20260918_restrict_public_api.sql`을 `postgres` 계정으로 적용했습니다. 이 스크립트는 public 테이블의 RLS를 활성화하고, 테이블·뷰·함수·시퀀스와 public 스키마의 공개 접근을 차단합니다. 서버 계정 권한은 유지하며, 검증 실패 시 전체 변경을 롤백합니다. 새 public 테이블도 생성 시 RLS를 활성화해야 합니다.

Aiven에는 `db_migrations/20260919_aiven_runtime_access.sql`을 적용했습니다. 20개 테이블에 RLS를 켜고 `PUBLIC` 접근을 회수했으며, `hype_sync`만 업무 데이터를 처리합니다. `schema_migrations`는 조회만 허용합니다. 새 테이블도 관리자 마이그레이션에서 RLS·정책·권한을 함께 설정해야 합니다.

향후 Supabase Data API를 직접 사용하는 기능을 추가할 때는 필요한 객체의 최소 권한과 RLS 정책을 함께 설계합니다. 전체 공개 권한을 복구하지 않습니다.

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
| `schedule` | - | 예정 실행 요일 (UTC 기준, 예: `"Friday"`). 미완료 주간 작업은 이후 일일 실행에서 재시도 |
| `shuffle` | - | 플레이리스트 저장 시 곡 순서 랜덤화 |
| `include_in_hype` | - | Hype Wave 계산 입력 소스 여부 |
| `hype_group` | - | Hype 그룹 식별자 |
| `hype_weight` | - | Hype 지수 가중치 (0.0 ~ 1.0) |

### 5. 별칭 및 오버라이드 관리 (`matching_alias.json`)

플랫폼마다 다른 아티스트/곡명 표기를 통합하거나, 매칭 실패 시 수동으로 Video ID를 지정할 수 있습니다.

```json
{
    "artists": [
        ["Woo", "우원재"]
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
# 전체 작업 순차 실행 (실제 플레이리스트 업데이트 포함)
HYPE_DB_BACKEND=aiven python sync_all.py

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

# 다운로드한 YouTube 주간 CSV를 DB에만 반영 (집계 종료일 명시)
HYPE_DEFER_HISTORY_EXPORT=1 python ytmusic_to_ytmusic_crawl.py \
    --youtube-charts-csv youtube-charts-top-songs-kr-weekly-20261001.csv \
    --chart-period-end 2026-10-01 --yt-auth .secrets/browser.json --db-only

# DB 수동 관리
HYPE_DB_BACKEND=sqlite python heal_split_tracks.py --db-path hype_wave_data.db --dry-run
python heal_split_tracks.py --db-path hype_wave_data.db # split track UID 정리
```

누락된 YouTube 주차가 여러 개면 오래된 주차부터 가져옵니다. CSV의 영상 URL이 `n/a`인 곡도 순위·제목·가수는 보존하며, 내부 `fallback:` 식별자로 저장하고 영상 매칭은 미확정으로 남깁니다. 원본에 없는 영상 ID를 생성하지 않습니다.

YouTube Charts 조회는 `429` 및 일시적인 서버 오류에 최대 3회 재시도하며, `Retry-After`가 있으면 그 대기 시간을 따릅니다. 영어·한국어 중 한 요청이 실패해도 다른 요청의 유효한 차트는 유지하고, 실패 이유는 언어와 함께 경고 로그에 남깁니다.

차트 페이지가 빈 `INNERTUBE_API_KEY`를 배포하면 키 없는 데이터 요청은 실제 브라우저에서도 `RESOURCE_EXHAUSTED` 429를 받을 수 있습니다. 수집기는 공식 YouTube 페이지의 현재 공개 설정을 읽어 API 요청과 브라우저의 차트 요청에 적용합니다. 키를 고정하거나 개인 인증정보·제3자 키를 사용하지 않습니다. 브라우저가 보내는 최소 조회 문맥을 사용하며 페이지 초기화용 방문자 정보는 전송하지 않습니다. 이 보완 후에도 공급자의 할당량 제한이 간헐적으로 재현됐습니다. API와 브라우저는 같은 공식 데이터 서버를 사용하므로 둘 다 429를 받을 수 있습니다.

HTTP/API 수집이 실패하거나 주간 데이터가 불완전하면 Playwright WebKit으로 공식 대한민국 주간 차트를 조회합니다. 공식 응답의 국가·곡 차트 유형, 중복·누락 없는 원본 1–100위, 7일 집계 기간, 화면에 표시된 기간과 요청 기간의 일치를 검증한 뒤 기존 저장·매칭 경로로 넘깁니다. 브라우저는 최신 주차만 수집합니다.

둘 다 실패하면 `snapshots/youtube-charts/`의 검증된 공식 응답을 재사용합니다. 명시한 집계 주 또는 최신 완료된 목요일(UTC)의 주차와 정확히 일치해야 하며, 국가·유형·100위·원본 영상 ID·집계 기간·수집 시각·SHA-256을 다시 검증합니다. 이전 주차를 새 주차로 바꾸지 않습니다. 로그와 DB 원본 수집 기록에 `collection_method=verified_snapshot`, 원래 `source_collected_at`, 체크섬을 남기며 재사용 시 수집 시각을 갱신하지 않습니다. 새로운 공식 응답을 검증한 운영 실행은 공개 차트 스냅샷을 저장하고 Actions가 다른 작업의 성공 여부와 관계없이 이를 커밋합니다. 해당 주차의 유효한 스냅샷도 없으면 수집 실패로 처리하고 기존 차트와 플레이리스트를 보존합니다. 과거 주차는 기간을 명시한 스냅샷 또는 CSV를 사용합니다.

로컬에서는 `python -m playwright install webkit`으로 브라우저를 설치합니다. `python tests/check_youtube_charts_browser.py`는 정상 수집과 강제 HTTP 장애의 대체 수집을 실행하고, 동일한 집계 기간·100위 영상 순서를 검증하며 실제 사용한 수집 방식과 원래 수집 시각을 출력합니다. DB·플레이리스트를 변경하지 않습니다. Actions는 `python -m playwright install --with-deps webkit`으로 브라우저와 Linux 의존성을 설치하고, 별도의 `YouTube Charts Browser Check` 작업으로 같은 검증을 수행합니다.

---

### 게시 차단 및 복구

`pending recovery`는 이전 플레이리스트 변경의 복구가 끝나지 않아 **게시만 차단**된 상태입니다. 현재 실행의 수집·매칭 완료 여부는 별도로 검증하며, 차단 시 종료 코드 `5`를 사용해 `sync_all.py`가 게시 실패로 분류합니다. 과거 복구 기록이 있다는 이유만으로 다른 오류를 게시 실패로 바꾸지는 않습니다.

플레이리스트는 기존 항목의 `setVideoId`를 유지하며 변경된 곡만 갱신합니다. 새 곡을 추가하고 기존 제목·길이·버전·아티스트 ID 검사로 검증한 뒤, 빠진 곡만 삭제하고 필요한 항목만 이동합니다. 동일 곡으로 검증된 대체 영상 ID도 재사용하며, 순서 변경은 가장 긴 공통 순서를 유지해 이동 횟수를 줄입니다. 기존 부분 게시 정책의 제외 곡과 원본 요청은 별도로 기록합니다.

추가·삭제·이동은 의도를 먼저 DB에 저장하고 성공 응답을 기록한 다음 진행합니다. 이동은 최대 50개를 한 요청으로 묶으며, 전체 항목과 순서를 다시 조회해 모든 이동이 적용된 경우에만 성공을 기록합니다. 로그의 `moved`는 이동한 곡 수, `move_requests`는 실제 이동 API 요청 수입니다. 이전 상태나 묶음의 중간 상태가 잠깐 조회되면 조회만 재시도하며, 일부 이동만 적용되거나 응답 유실·기록 실패가 발생하면 변경 요청을 재전송하지 않습니다. 복구는 이전의 개별 이동 기록과 새 묶음 기록을 모두 읽고, 남아 있는 원래 항목을 우선 유지해 변경분과 순서만 되돌립니다. 복구 적용에는 기존 실행 중지 확인과 DB 소유권 검증이 계속 필요합니다. `tests/test_playlist_delta.py`는 외부 접속 없이 실제 SQLite 감사 기록으로 이 동작을 검증하며 GitHub Actions에서도 실행됩니다.

먼저 오류에 표시된 실행 ID와 플레이리스트 ID로 현재 상태를 조회합니다. 기본 실행은 DB와 플레이리스트를 변경하지 않습니다.

```bash
python reconcile_playlist_update.py \
    --run-id RUN_ID \
    --playlist-id PLAYLIST_ID \
    --yt-auth .secrets/browser.json
```

결과가 `blocked`라면 복구 기록을 삭제하거나 성공 상태로 바꾸지 않습니다. 응답의 `reason`을 확인하고, 현재 상태와 변경 이력 및 곡 식별 정보의 검증을 충족한 뒤 명시적으로 복구해야 합니다. 복구 가능한 결과를 적용할 때는 예약·수동 동기화 작업을 모두 멈춘 뒤 같은 명령에 `--apply --workers-quiescent`를 추가합니다.

변경 직후 `trackCount`와 실제 목록 길이가 다르면 전체 목록을 최대 3회 조회합니다(1초·2초 대기). 표시 곡 수가 계속 다르더라도 DB에 저장된 전체 항목 번호와 순서가 일치하면 목록의 완전성을 확인할 수 있으며, 곡 동일성은 별도로 검증합니다. 이 증거가 부족하거나 항목 식별 정보가 유효하지 않으면 게시를 중단하고, 변경 요청은 재전송하지 않습니다.

## ⚙️ GitHub Actions 워크플로우

2026-09-19 전환 완료: main 반영, 실제 러너 연결 점검, 전체 동기화 #196 및 GitHub Pages 배포가 모두 성공했습니다. 제한 계정 `hype_sync`, TLS 1.3, 20개 테이블 RLS를 확인했고 예약 실행을 재개했습니다. 30초 간격 관측에서 실행 계정 연결은 최대 2개(제한 5개)였습니다.

`daily-sync.yml` — 매일 UTC 07:05(KST 16:05)에 실행 예약 (수동 트리거 가능)

실행 요일은 UTC로 판단하므로 시작이 지연되어 KST 자정을 지나도 UTC 예정 요일의 작업을 실행합니다. 가장 최근 예정 요일부터 수집·매칭과 게시를 모두 마친 기록이 없는 주간 작업은 다음 일일 실행에서 재시도합니다. 이 재시도는 최근 예정 회차를 대상으로 하며, 과거 여러 누락 주차의 복원은 별도 수동 작업으로 수행합니다.

`sync_all.py`는 시작 시 UTC 날짜를 한 번 정하고 `HYPE_SYNC_DATE`로 모든 하위 작업에 전달합니다. 처리 중 자정을 넘어도 이 실행의 내부 기준일은 같습니다. 독립 실행은 UTC 오늘을 사용합니다. Apple 일간 차트의 기준일은 이 날짜에서 하루를 뺀 값이고, Hype 표시 날짜는 다시 하루를 더한 값입니다. 예를 들어 UTC 10월 5일 15:46 실행은 Apple 기준일 10월 4일, Hype 날짜 10월 5일로 기록합니다. 원본에 날짜가 없는 경우의 기준일도 이 UTC 날짜를 사용하며, YouTube·멜론 원본에 명시된 집계 날짜와 Apple의 `--reference-period`는 바꾸지 않습니다. 기존 DB 날짜를 일괄 이동하지 않습니다.

```
1. Checkout → Python 3.11 설정 → 의존성 설치 → 회귀 테스트
2. YouTube Music 인증 파일 복원
3. sync_all.py 실행 (sync_config.json 기반 전체 작업)
4. docs/api/history.json 변경 시 자동 커밋 & 푸시 (GitHub Pages 업데이트)
```

---

## 📦 의존성

```
requests>=2.31.0
PyJWT[crypto]>=2.8.0
ytmusicapi>=1.10.0
beautifulsoup4>=4.12.0
psycopg2-binary>=2.9.0
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
├── heal_split_tracks.py           # split track UID 정리
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

### Aiven 운영 연결

`daily-sync.yml`은 GitHub Secrets의 AIVEN_DB_URI, AIVEN_DB_HOST, AIVEN_DB_CA_CERTIFICATE를 전달합니다. Secret에는 변수 이름과 `=`, 바깥 따옴표를 제외한 값만 저장합니다. 인증서 파일은 코드가 실행 중 생성하므로 로컬 파일 경로를 Secret에 저장하지 않습니다. URI에는 관리자 대신 제한된 `hype_sync` 계정을 사용하고 `sslmode=require`를 유지합니다.

테이블과 인덱스는 관리자가 설치하며 실행 중 자동 생성하지 않습니다.

기존 SUPABASE_DB_URL은 명시적인 복구·원본 백업용으로만 보관합니다. Aiven 선택 후 설정·연결·캐시 오류가 나면 다른 DB로 대체하지 않고 실패 처리합니다. Supabase와 Aiven에 서로 다른 쓰기가 발생했다면 단순 URL 변경 대신 양쪽 데이터를 백업·대조한 뒤 복구합니다.

운영 전환은 2026-09-19 전체 동기화 및 Pages 배포로 확인했습니다. 이후 연결 점검 전용 코드와 테스트 워크플로를 제거했으며, 수동 실행과 예약 실행은 모두 동기화를 수행합니다.
