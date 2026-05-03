# Apple/Spotify/Melon → YouTube Music Daily Sync

이 프로젝트는 Apple Music, Spotify, Melon의 주요 차트 및 플레이리스트를 YouTube Music으로 매일 자동 동기화하는 도구입니다.

## 🚀 주요 특징

- **통합 관리**: `sync_config.json` 설정을 통해 여러 소스의 플레이리스트를 한 번에 동기화합니다.
- **강력한 매칭 엔진**:
  - **다국어 매칭 (Multi-Language)**: 플랫폼별 영문/국문 메타데이터를 상호 참조하여 검색 정확도를 극대화합니다.
  - **변형 대응 (Variants Engine)**: "AKMU (악뮤)"와 "AKMU"를 동일인으로 인식하는 스마트 분리 및 병합 엔진을 탑재했습니다.
  - **프록시 데이터 (Proxy Match)**: Apple Music에서 검증된 Video ID를 Melon/Spotify 매칭 시 우선 활용하여 오차를 줄입니다.
  - **화제성 분석 (Hype Index)**: 서로 다른 플랫폼의 차트 데이터를 통합 분석하여 급상승 곡을 추출합니다.
- **스마트 수집**:
  - **Apple Music**: 지역별(KR/US) 페이지 스크래핑을 통한 다국어 정보 수집.
  - **Spotify**: 웹 임베드 스크래핑 및 **MusicBrainz**를 통한 앨범 정보 자동 보강.
  - **Melon**: 웹 차트 스크래핑 및 차트 날짜 정보 자동 추출.
- **유연한 실행**: 실행 경로에 상관없이 모든 리소스를 정확히 찾아가는 견고한 경로 처리 로직이 포함되어 있습니다.
- **데이터 아카이빙**: 매일의 차트 데이터를 JSON으로 보존하고, 최근 31일간의 이력을 `docs/api/history.json`에 저장하여 웹 대시보드 등에서 활용 가능합니다.

## 📋 현재 동기화 리스트

| Task ID | Source Service | Source Playlist | Target YT Music | Shuffle |
| :--- | :--- | :--- | :--- | :--- |
| **Apple-KR-Top-Songs** | Apple Music | [오늘의 TOP 100: 대한민국](https://music.apple.com/kr/new/top-charts/songs) | [이동](https://music.youtube.com/playlist?list=PLtawHGpcUVZXESVtlr4-HkbpRJQhhYZnr) | - |
| **Apple-KR-Top-100** | Apple Music | [Top 100: South Korea](https://music.apple.com/us/playlist/top-100-south-korea/pl.d3d10c32fbc540b38e266367dc8cb00c) | [이동](https://music.youtube.com/playlist?list=PLtawHGpcUVZWeko8ek3LWy9gav1sg9FFw) | - |
| **Apple-Seoul-Top-25** | Apple Music | [Top 25: Seoul](https://music.apple.com/us/playlist/top-25-seoul/pl.d6f003a501da4b3c9d33b0c7b8cfa0ae) | [이동](https://music.youtube.com/playlist?list=PLtawHGpcUVZWrHIqj-huLBKgOiKVFkNLJ) | - |
| **Apple-Busan-Top-25** | Apple Music | [Top 25: Busan](https://music.apple.com/us/playlist/top-25-busan/pl.b4a7b0c2558941f68b329cde7774139a) | [이동](https://music.youtube.com/playlist?list=PLtawHGpcUVZUUiQ122rfx-_OFMJNgPlFT) | - |
| **Spotify-Hot-Hits-Korea** | Spotify | [Hot Hits Korea Top 100](https://open.spotify.com/playlist/37i9dQZF1DWT9uTRZAYj0c) | [이동](https://music.youtube.com/playlist?list=PLtawHGpcUVZXOcgkI1oL0--mJiKZ5eGMI) | - |
| **Spotify-Fresh-Indie-Korea** | Spotify | [Fresh Finds Korea](https://open.spotify.com/playlist/37i9dQZF1DX7vZYLzFGQXc) & [Indie Korea](https://open.spotify.com/playlist/37i9dQZF1DXdTb8AG95jne) | [이동](https://music.youtube.com/playlist?list=PLtawHGpcUVZUx53FaaLkidRHrOB7ebxDH) | ✅ |
| **Melon-KR-Top-100-Weekly** | Melon | [멜론 주간 차트](https://www.melon.com/chart/week/index.htm) | [이동](https://music.youtube.com/playlist?list=PLtawHGpcUVZUzXhe99b4vMV9YF7IIFvir) | - |
| **Melon-KR-Top-100-Daily** | Melon | [멜론 일간 차트](https://www.melon.com/chart/day/index.htm) | [이동](https://music.youtube.com/playlist?list=PLtawHGpcUVZVwAYjPC7Jvc3BTZkQ1G966) | - |
| **Hype-Wave-Daily** | - | [Top 100: South Korea](https://music.apple.com/us/playlist/top-100-south-korea/pl.d3d10c32fbc540b38e266367dc8cb00c) & [멜론 일간 차트](https://www.melon.com/chart/day/index.htm) | [이동](https://music.youtube.com/playlist?list=PLtawHGpcUVZXKVkfIElFeaWB6g6d1o9Dr) | - |

## 🌊 Hype Wave Chart란?

`hype_moment.py`를 통해 생성되는 이 차트는 단순 합산 순위가 아닌 **Hype Index(화제성 지수)**를 기반으로 합니다.

### 1. 지수 계산 방식 (Hype Index)
- **선형 점수(Linear)**: 순위권 내의 안정적인 인기를 반영합니다.
- **역수 점수(Reciprocal)**: 1~10위권 내의 최상위권 파급력을 강조합니다.
- **모멘텀 가중치**: 트렌드에 민감한 **Apple Music** 순위가 높지만, 아직 일반 대중 차트인 **Melon**에 진입하지 못했거나 낮은 곡에 가점을 주어 "발굴" 효과를 극대화합니다.

### 2. 특별 지표 (Momentum Indicators)
- **WAVE (🌊)**: Apple Music Top 100에는 진입했으나 Melon에는 아직 없는 곡입니다. 곧 대중적인 인기로 이어질 가능성이 높은 '폭발 직전' 상태를 의미합니다.
- **NEW WAVE (✨)**: 전날 차트에는 없었으나 오늘 Apple Music 차트에 새롭게 진입하며 WAVE 조건을 만족한 '가장 신선한' 트렌드 곡입니다.

### 3. 데이터 활용
- 모든 결과는 `docs/api/history.json`에 누적됩니다.
- 이 데이터를 활용하여 [HYPE WAVE](https://colinky.github.io/hype_wave)에서 시각화된 차트를 확인할 수 있습니다.

## 🛠 설정 및 운영

### 1. GitHub Secrets 설정
GitHub repository의 `Settings -> Secrets -> Actions`에 아래 값을 추가합니다.
- `YTMUSIC_BROWSER_JSON`: YouTube Music 인증 정보 (`browser.json` 내용 전체)

### 2. GitHub Actions 권한 설정
자동 커밋 및 푸시를 위해 저장소 권한 설정이 필요합니다.
1. `Settings` > `Actions` > `General` 이동
2. **Workflow permissions** 섹션에서 **Read and write permissions** 선택 후 저장

### 3. YouTube Music 인증 (browser.json 생성)
```bash
# 로컬에서 실행하여 인증 파일 생성
pip install -r requirements.txt
ytmusicapi browser
```
생성된 `browser.json`의 내용을 복사하여 위 Secret에 저장합니다.

### 4. 동기화 작업 관리 (`sync_config.json`)
새로운 플레이리스트를 추가하려면 `sync_config.json`에 아래와 같이 추가합니다.
```json
{
  "name": "작업 이름",
  "enabled": true,
  "type": "apple", "spotify", 또는 "melon",
  "source_urls": ["플레이리스트 URL"],
  "target_id": "유튜브 플레이리스트 ID",
  "shuffle": true,       // (선택사항) 저장 시 곡 순서 랜덤 섞기 여부
  "archive": true,       // (선택사항) crawl/ 디렉토리에 원본 데이터 보존 여부
  "schedule": "Monday",  // (선택사항) 특정 요일에만 실행 시 설정 (KST 기준)
  "apple_chart_limit": 100, // (apple 전용) 수집할 곡 수 제한
  "use_musicbrainz": true,  // (spotify 전용) MusicBrainz를 통한 정보 보강 여부
  "limit": 100           // (hypex 전용) 생성될 플레이리스트 곡 수
}
```

### 5. 별칭 관리 (`matching_alias.json`)
아티스트 이름이 플랫폼마다 달라 매칭이 안 될 때 사용합니다. (예: Young B ↔ 양홍원 ↔ YANGHONGWON)
```json
{
  "artists": [
    ["Young B", "YANGHONGWON", "양홍원"]
  ]
}
```

## 💻 로컬 실행
```bash
# 모든 작업 순차 실행
python sync_all.py

# 특정 작업 드라이 런 (실제 업데이트 없음)
python apple_music_to_ytmusic_crawl.py --apple-playlist-urls <URL> --yt-playlist-id <ID> --dry-run
python melon_to_ytmusic_crawl.py --melon-urls <URL> --yt-playlist-id <ID> --dry-run
```

*모든 매칭 로그는 `logs/` 디렉토리에 JSON 형태로 저장되어 사후 분석이 가능합니다.*

---

# Apple/Spotify/Melon → YouTube Music Daily Sync

This project is a tool that automatically synchronizes major charts and playlists from Apple Music, Spotify, and Melon to YouTube Music every day.  

## 🚀 Features

- **Unified Management**: Synchronizes playlists from multiple sources at once through `sync_config.json` settings.
- **Powerful Matching Engine**:
  - **Multi-Language Matching**: Maximizes search accuracy by cross-referencing English/Korean metadata from each platform.
  - **Variants Engine**: Smart separation and merging engine that recognizes "AKMU (악뮤)" and "AKMU" as the same entity.
  - **Proxy Match**: Maximizes search accuracy by cross-referencing English/Korean metadata from each platform.
  - **Hype Index**: Integrates chart data from different platforms to extract trending songs.
- **Smart Collection**:
  - **Apple Music**: Extracts multi-language information by scraping regional (KR/US) pages.
  - **Spotify**: Scrapes web embeds and automatically supplements album information with **MusicBrainz**.
  - **Melon**: Scrapes web charts and automatically extracts chart date information.
- **Robust Execution**: Includes solid path processing logic that accurately finds all resources regardless of execution path.
- **Data Archiving**: Preserves daily chart data in JSON format and saves the recent 31 days of history in `docs/api/history.json` for use in web dashboards, etc.

## 📋 Current Synchronization List

| Task ID | Source Service | Source Playlist | Target YT Music | Shuffle |
| :--- | :--- | :--- | :--- | :--- |
| **Apple-KR-Top-Songs** | Apple Music | [Today's Top 100: South Korea](https://music.apple.com/kr/new/top-charts/songs) | [Link](https://music.youtube.com/playlist?list=PLtawHGpcUVZXESVtlr4-HkbpRJQhhYZnr) | - |
| **Apple-KR-Top-100** | Apple Music | [Top 100: South Korea](https://music.apple.com/us/playlist/top-100-south-korea/pl.d3d10c32fbc540b38e266367dc8cb00c) | [Link](https://music.youtube.com/playlist?list=PLtawHGpcUVZWeko8ek3LWy9gav1sg9FFw) | - |
| **Apple-Seoul-Top-25** | Apple Music | [Top 25: Seoul](https://music.apple.com/us/playlist/top-25-seoul/pl.d6f003a501da4b3c9d33b0c7b8cfa0ae) | [Link](https://music.youtube.com/playlist?list=PLtawHGpcUVZWrHIqj-huLBKgOiKVFkNLJ) | - |
| **Apple-Busan-Top-25** | Apple Music | [Top 25: Busan](https://music.apple.com/us/playlist/top-25-busan/pl.b4a7b0c2558941f68b329cde7774139a) | [Link](https://music.youtube.com/playlist?list=PLtawHGpcUVZUUiQ122rfx-_OFMJNgPlFT) | - |
| **Spotify-Hot-Hits-Korea** | Spotify | [Hot Hits Korea Top 100](https://open.spotify.com/playlist/37i9dQZF1DWT9uTRZAYj0c) | [Link](https://music.youtube.com/playlist?list=PLtawHGpcUVZXOcgkI1oL0--mJiKZ5eGMI) | - |
| **Spotify-Fresh-Indie-Korea** | Spotify | [Fresh Finds Korea](https://open.spotify.com/playlist/37i9dQZF1DX7vZYLzFGQXc) & [Indie Korea](https://open.spotify.com/playlist/37i9dQZF1DXdTb8AG95jne) | [Link](https://music.youtube.com/playlist?list=PLtawHGpcUVZUx53FaaLkidRHrOB7ebxDH) | ✅ |
| **Melon-KR-Top-100-Weekly** | Melon | [Melon Weekly Chart](https://www.melon.com/chart/week/index.htm) | [Link](https://music.youtube.com/playlist?list=PLtawHGpcUVZUzXhe99b4vMV9YF7IIFvir) | - |
| **Melon-KR-Top-100-Daily** | Melon | [Melon Daily Chart](https://www.melon.com/chart/day/index.htm) | [Link](https://music.youtube.com/playlist?list=PLtawHGpcUVZVwAYjPC7Jvc3BTZkQ1G966) | - |
| **Hype-Wave-Daily** | - | [Apple Music Top 100: South Korea](https://music.apple.com/us/playlist/top-100-south-korea/pl.d3d10c32fbc540b38e266367dc8cb00c) & [Melon Daily Chart](https://www.melon.com/chart/day/index.htm) | [Link](https://music.youtube.com/playlist?list=PLtawHGpcUVZXKVkfIElFeaWB6g6d1o9Dr) | - |

## 🌊 What is Hype Wave Chart?

The chart generated through `hype_moment.py` is based on the **Hype Index** rather than simple combined rankings.

### 1. How to calculate the Hype Index
- **Linear Score**: Reflects the stable popularity within the rankings.
- **Reciprocal Score**: Emphasizes the top-tier influence of songs ranked 1-10.
- **Momentum Weight**: Gives extra points to songs that are trending on **Apple Music** but have not yet entered or are ranked low on the general public chart, **Melon**, maximizing the "discovery" effect.

### 2. Special Indicators (Momentum Indicators)
- **WAVE (🌊)**: Songs that have entered the Apple Music Top 100 but are not yet on Melon. This indicates a 'just before explosion' state with high potential to lead to mainstream popularity.
- **NEW WAVE (✨)**: Songs that were not on the chart the previous day but newly entered the Apple Music chart today, satisfying the WAVE condition. These are the 'freshest' trending songs.

### 3. Data Usage
- All results are cumulative in `docs/api/history.json`.
- This data can be used to view the visualized chart at [HYPE WAVE](https://colinky.github.io/hype_wave).

## 🛠 Configuration and Operation

### 1. GitHub Secrets Configuration
Add the following value to `Settings -> Secrets -> Actions` of the GitHub repository.
- `YTMUSIC_BROWSER_JSON`: YouTube Music authentication information (full content of `browser.json`)

### 2. GitHub Actions Permission Settings
Repository permission settings are required for automatic commit and push.
1. Go to `Settings` > `Actions` > `General`
2. In the **Workflow permissions** section, select **Read and write permissions** and save.

### 3. YouTube Music Authentication (Generating browser.json)
```bash
# Run locally to generate authentication file
pip install -r requirements.txt
ytmusicapi browser
```
Copy the contents of the generated `browser.json` and save them to the Secret above.

### 4. Synchronization Task Management (`sync_config.json`)
To add a new playlist, add it to `sync_config.json` as follows.
```json
{
  "name": "Task Name",
  "enabled": true,
  "type": "apple", "spotify", or "melon",
  "source_urls": ["Playlist URL"],
  "target_id": "YouTube Playlist ID",
  "shuffle": true,       // (Optional) Whether to shuffle the song order when saving
  "archive": true,       // (Optional) Whether to save the original data in the crawl/ directory
  "schedule": "Monday",  // (Optional) Set to run only on specific days (based on KST)
  "apple_chart_limit": 100, // (apple only) Limit on the number of songs to collect
  "use_musicbrainz": true,  // (spotify only) Whether to supplement information through MusicBrainz
  "limit": 100           // (hypex only) Limit on the number of songs to be generated
}
```

### 5. Alias Management (`matching_alias.json`)
Used when artist names differ across platforms, causing matching failures. (e.g., Young B ↔ 양홍원 ↔ YANGHONGWON)
```json
{
  "artists": [
    ["Young B", "YANGHONGWON", "양홍원"]
  ]
}
```

## 💻 Local Execution
```bash
# Run all tasks sequentially
python sync_all.py

# Dry run specific tasks (no actual updates)
python apple_music_to_ytmusic_crawl.py --apple-playlist-urls <URL> --yt-playlist-id <ID> --dry-run
python melon_to_ytmusic_crawl.py --melon-urls <URL> --yt-playlist-id <ID> --dry-run
```

*All matching logs are saved in `logs/` directory for post-analysis.*