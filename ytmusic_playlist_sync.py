from __future__ import annotations

import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any

from ytmusicapi import YTMusic

try:
    from ytmusicapi.auth.oauth import OAuthCredentials
except ImportError:  # pragma: no cover - compatibility with older ytmusicapi.
    OAuthCredentials = None


LOG = logging.getLogger("ytmusic_playlist_sync")


@lru_cache(maxsize=8192)
def normalize_text(value: str) -> str:
    """
    텍스트 정규화: 다국어 환경에서 안정적인 매칭을 위해 노이즈를 제거합니다.
    1. NFKC 정규화 및 소문자 변환
    2. 피처링(feat, ft) 정보 제거 (곡 제목 본연의 의미 유지)
    3. 일반적인 접미사(EP, Single) 및 특수문자 제거
    4. CJK(한글, 일어, 한자) 및 영문/숫자만 남김
    """
    value = unicodedata.normalize("NFKC", value).lower()
    # 괄호 안의 피처링 정보 제거
    value = re.sub(r"\([^)]*(feat\.?|ft\.?)[^)]*\)", " ", value)
    value = re.sub(r"\[[^\]]*(feat\.?|ft\.?)[^\]]*\]", " ", value)
    value = re.sub(r"\b(feat\.?|ft\.?)\b.*$", " ", value)
    value = re.sub(r"\s*-\s*(ep|single)\b.*$", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"[^0-9a-z가-힣\u3040-\u30ff\u4e00-\u9fff]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


class AliasManager:
    """
    플랫폼별로 다른 아티스트/곡명/앨범명 표기법을 관리합니다.
    matching_alias.json의 설정을 기반으로 하며, overrides를 통해 특정 곡을 강제 매칭할 수 있습니다.
    """
    def __init__(self, path: str | Path | None = None):
        self.artist_map: dict[str, list[str]] = {}
        self.title_map: dict[str, list[str]] = {}
        self.album_map: dict[str, list[str]] = {}
        self.overrides: dict[str, str] = {}
        
        if path is None:
            path = Path(__file__).parent / "matching_alias.json"
        self.load(path)

    def load(self, path: str | Path):
        p = Path(path)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            self._build_map(data.get("artists", []), self.artist_map)
            self._build_map(data.get("titles", []), self.title_map)
            self._build_map(data.get("albums", []), self.album_map)

            # Overrides 조회 시 정확한 비교를 위해 키를 정규화하여 저장
            self.overrides = {}
            for raw_key, video_id in data.get("overrides", {}).items():
                if "|" in raw_key:
                    parts = raw_key.split("|")
                    norm_key = "|".join(normalize_text(p) for p in parts)
                    self.overrides[norm_key] = video_id
                else:
                    self.overrides[normalize_text(raw_key)] = video_id
        except Exception as e:
            LOG.warning("Failed to load alias file %s: %s", path, e)

    def _build_map(self, clusters: list[list[str]], target_map: dict[str, list[str]]):
        for cluster in clusters:
            cleaned_cluster = [c.strip() for c in cluster if c.strip()]
            for item in cleaned_cluster:
                key = normalize_text(item)
                if key:
                    target_map[key] = list(set(target_map.get(key, []) + cleaned_cluster))

    def get_variants(self, value: str, category: str) -> list[str]:
        if not value:
            return []
        key = normalize_text(value)
        mapping = getattr(self, f"{category}_map", {})
        # Return a copy to prevent mutation of the cached alias mapping
        return list(mapping.get(key, [value]))


ALIASES = AliasManager()


@dataclass
class SourceTrack:
    """
    동기화 소스(Apple, Spotify, Melon 등)로부터 수집된 원본 곡 정보를 저장하는 클래스입니다.
    유튜브 뮤직 검색 및 매칭의 기준 데이터로 활용됩니다.
    """
    rank: int
    title: str
    artist: str
    album: str = ""
    apple_id: str = ""
    url: str = ""
    source: str = "track_lockup"
    artwork_url: str = ""


@dataclass
class MatchResult:
    """
    유튜브 뮤직 검색 및 매칭 결과를 담는 데이터 클래스입니다.
    소스 서비스의 메타데이터와 유튜브에서 검색된 실제 정보, 
    그리고 매칭의 신뢰도를 나타내는 각종 점수들을 포함합니다.
    """
    rank: int
    title: str
    artist: str
    album: str
    title_en: str = ""      # 영문 곡 제목 (매칭 보조용)
    artist_en: str = ""     # 영문 아티스트 명 (매칭 보조용)
    album_en: str = ""      # 영문 앨범 명 (매칭 보조용)
    title_ko: str = ""      # 국문 곡 제목 (매칭 보조용)
    artist_ko: str = ""     # 국문 아티스트 명 (매칭 보조용)
    album_ko: str = ""      # 국문 앨범 명 (매칭 보조용)
    apple_id: str = ""      # 소스 서비스의 고유 ID (AdamID 등)
    url: str = ""           # 원본 곡 상세 URL
    artwork_url: str = ""   # 앨범 아트워크 이미지 URL
    video_id: str | None = None # 매칭된 유튜브 뮤직 비디오 ID
    yt_title: str = ""      # 유튜브 검색 결과 제목
    yt_artist: str = ""     # 유튜브 검색 결과 아티스트
    yt_album: str = ""      # 유튜브 검색 결과 앨범
    score: float = 0.0      # 최종 합산 매칭 점수
    title_score: float = 0.0 # 제목 유사도 점수
    artist_score: float = 0.0 # 아티스트 유사도 점수
    album_score: float = 0.0 # 앨범 유사도 점수
    yt_result_type: str = "" # 결과 타입 (song, video 등)
    query: str = ""         # 매칭에 사용된 검색 쿼리
    status: str = "failed"  # 매칭 상태 (matched, failed, proxy_matched 등)


def match_from_prev(
    track: SourceTrack,
    prev: dict[str, Any],
    *,
    track_ko: SourceTrack | None = None,
    status: str = "cached_match",
) -> MatchResult:
    """Build a MatchResult reusing data from a previous match (cache or proxy)."""
    return MatchResult(
        rank=track.rank,
        title=track.title,
        artist=track.artist,
        album=track.album,
        title_en=prev.get("title_en", track.title),
        artist_en=prev.get("artist_en", track.artist),
        album_en=prev.get("album_en", track.album),
        title_ko=track_ko.title if track_ko else prev.get("title_ko", ""),
        artist_ko=track_ko.artist if track_ko else prev.get("artist_ko", ""),
        album_ko=track_ko.album if track_ko else prev.get("album_ko", ""),
        apple_id=track.apple_id,
        url=track.url,
        artwork_url=track.artwork_url,
        video_id=prev["video_id"],
        yt_title=prev.get("yt_title", ""),
        yt_artist=prev.get("yt_artist", ""),
        yt_album=prev.get("yt_album", ""),
        score=prev.get("score", 0.0),
        title_score=prev.get("title_score", 0.0),
        artist_score=prev.get("artist_score", 0.0),
        album_score=prev.get("album_score", 0.0),
        yt_result_type=prev.get("yt_result_type", "song"),
        query=prev.get("query", status),
        status=status,
    )


def load_dotenv(path: str | Path) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_or_arg(value: str | None, env_name: str, *, required: bool = True) -> str:
    resolved = value or os.environ.get(env_name, "")
    if required and not resolved:
        raise SystemExit(f"Missing required value: --{env_name.lower().replace('_', '-')} or {env_name}")
    return resolved


def unique_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        cleaned = value.strip()
        if not cleaned:
            continue
        key = normalize_text(cleaned)
        if key in seen:
            continue
        seen.add(key)
        unique.append(cleaned)
    return unique


@lru_cache(maxsize=1024)
def split_artist_names(value: str) -> list[str]:
    parts = re.split(r"\s*(?:,|&| and | x | X | with |\+)\s*", value)
    return [part.strip() for part in parts if part.strip()]


@lru_cache(maxsize=1024)
def _extract_parentheses_variants(value: str) -> list[str]:
    if not value: return []
    variants = [value]
    # 괄호나 구분자(/, |)를 기준으로 원곡명과 부제목을 분리하여 변형 생성
    match = re.search(r"^(.*?)\s*[\(\[/\uFF08]\s*(.*?)\s*[\)\]/\uFF09]\s*$", value)
    if match:
        main, sub = match.groups()
        if main: variants.append(main.strip())
        if sub: variants.append(sub.strip())
    
    # Try splitting by common delimiters if no parentheses match
    if len(variants) == 1:
        for delim in [" / ", " | ", " - "]:
            if delim in value:
                parts = value.split(delim)
                variants.extend([p.strip() for p in parts if p.strip()])
                break
    return variants


def artist_variants(artist: str) -> list[str]:
    # 1. Start with the direct name and its aliases
    base_variants = ALIASES.get_variants(artist, "artist")
    
    # 2. Add split artist parts (e.g. "A & B" -> ["A", "B"]) and their aliases
    expanded = []
    for v in base_variants:
        expanded.extend(_extract_parentheses_variants(v))
        parts = split_artist_names(v)
        for part in parts:
            expanded.extend(_extract_parentheses_variants(part))
            expanded.append(part)
            
    return unique_values(expanded)


def title_variants(title: str) -> list[str]:
    base_variants = ALIASES.get_variants(title, "title")
    expanded = []
    for v in base_variants:
        expanded.extend(_extract_parentheses_variants(v))
    return unique_values(expanded)


def album_variants(album: str) -> list[str]:
    if not album: return []
    base_variants = ALIASES.get_variants(album, "album")
    expanded = []
    for v in base_variants:
        expanded.extend(_extract_parentheses_variants(v))
    return unique_values(expanded)


def similarity(left: str, right: str) -> float:
    """
    두 텍스트 간의 유사도를 측정합니다.
    - 부분 일치(Subset matching) 감지: "Song (English Ver.)"과 "Song" 매칭 시 0.95 부여
    - CJK 문자 특화: 한국어/일본어 등은 짧은 단어라도 정보량이 많으므로 더 낮은 길이 임계값 적용
    - 토큰 기반 및 서퀀스 기반 유사도 결합
    """
    left_norm = normalize_text(left)
    right_norm = normalize_text(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0

    if left_norm in right_norm:
        if right_norm.startswith(left_norm):
            remaining = right_norm[len(left_norm):].strip()
            if not remaining or all(ord(c) < 128 or c.isspace() for c in remaining):
                return 0.95

    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    if left_tokens and right_tokens:
        intersection = left_tokens.intersection(right_tokens)
        containment = len(intersection) / len(left_tokens)
        coverage = len(intersection) / len(right_tokens)
        token_score = (containment * 0.8) + (coverage * 0.2)
    else:
        token_score = 0.0

    seq_score = SequenceMatcher(None, left_norm, right_norm).ratio()

    l_no_space, r_no_space = left_norm.replace(" ", ""), right_norm.replace(" ", "")
    no_space_score = 1.0 if l_no_space == r_no_space and l_no_space else 0.0

    if left_norm in right_norm or right_norm in left_norm:
        short, long = (left_norm, right_norm) if len(left_norm) < len(right_norm) else (right_norm, left_norm)
        len_ratio = len(short) / len(long)
        is_cjk = any('\u3040' <= c <= '\u30ff' or '\u4e00' <= c <= '\u9fff' or '\uac00' <= c <= '\ud7af' for c in short)
        threshold = 2 if is_cjk else 4
        
        if len(short) >= threshold:
            substr_score = max(0.9, 0.85 * (len_ratio ** 0.5))
        else:
            substr_score = 0.85 * (len_ratio ** 0.5)
    else:
        substr_score = 0.0

    return max(token_score, seq_score, substr_score, no_space_score)


def result_artists(result: dict[str, Any]) -> str:
    """유튜브 검색 결과에서 아티스트 명칭을 추출합니다."""
    artists = result.get("artists")
    if artists:
        return " ".join(a.get("name", "") for a in artists)
    
    # Fallback to 'author' or 'podcast' for videos/episodes
    author = result.get("author")
    if author:
        return author if isinstance(author, str) else author.get("name", "")
    
    podcast = result.get("podcast")
    if podcast:
        return podcast.get("name", "")
        
    return ""


def result_album(result: dict[str, Any]) -> str:
    album = result.get("album")
    if album:
        return album if isinstance(album, str) else album.get("name", "")
    
    # Check 'playlist' field (used for some result types)
    playlist = result.get("playlist")
    if playlist:
        return playlist.get("name", "")
        
    return ""


def is_song_result(result: dict[str, Any]) -> bool:
    # Strictly allow only songs and videos. Episodes (TV clips) are forbidden.
    allowed_types = ["song", "video", "노래", "동영상"]
    return result.get("resultType") in allowed_types and bool(result.get("videoId"))


def score_result(
    track_en: SourceTrack,
    result: dict[str, Any],
    track_ko: SourceTrack | None = None,
) -> tuple[float, float, float, float]:
    yt_title = result.get("title", "")
    yt_album = result_album(result)
    yt_artists = result_artists(result)
    
    # Title scoring using variants
    title_score_en = max(
        (similarity(tv, yt_title) for tv in title_variants(track_en.title)),
        default=0.0
    ) if track_en.title else 0.0

    title_score_ko = max(
        (similarity(tv, yt_title) for tv in title_variants(track_ko.title)),
        default=0.0
    ) if track_ko and track_ko.title else 0.0

    title_score = max(title_score_en, title_score_ko)

    # Album scoring using variants
    album_score_en = max(
        (similarity(av, yt_album) for av in album_variants(track_en.album)),
        default=0.0
    ) if track_en.album else 0.0

    album_score_ko = max(
        (similarity(av, yt_album) for av in album_variants(track_ko.album)),
        default=0.0
    ) if track_ko and track_ko.album else 0.0

    # Singles on YT often have album name = song title (e.g. "REDRED" album for "REDRED" song)
    # We give this a high score (0.9) but lower than a perfect actual album match (1.0)
    title_is_album = False
    if yt_album:
        yt_album_norm = normalize_text(yt_album)
        title_is_album = any(normalize_text(tv) == yt_album_norm for tv in title_variants(track_en.title))
        if not title_is_album and track_ko:
            title_is_album = any(normalize_text(tv) == yt_album_norm for tv in title_variants(track_ko.title))

    album_score = max(album_score_en, album_score_ko, 0.9 if title_is_album else 0.0)

    if not track_en.artist and (not track_ko or not track_ko.artist):
        album_multiplier = album_score if track_en.album or (track_ko and track_ko.album) else 1.0
        score = title_score * album_multiplier
        return score, title_score, 0.0, album_score

    # Artist scoring using variants
    artist_score_en = max(
        (similarity(artist_variant, yt_artists) for artist_variant in artist_variants(track_en.artist)),
        default=0.0
    ) if track_en.artist else 0.0

    artist_score_ko = max(
        (similarity(artist_variant, yt_artists) for artist_variant in artist_variants(track_ko.artist)),
        default=0.0
    ) if track_ko and track_ko.artist else 0.0

    artist_score = max(artist_score_en, artist_score_ko)

    # Album score should not kill the total score. Use a milder multiplier.
    # A score of 0.0 for album results in 0.7x, and 1.0 results in 1.0x.
    album_multiplier = 0.7 + (album_score * 0.3) if track_en.album or (track_ko and track_ko.album) else 1.0
    score = title_score * artist_score * album_multiplier

    # Version mismatch penalty (Preview, Teaser, Instrumental, etc.)
    # If the YT result has these but the source doesn't, it's likely a false positive.
    neg_markers = ["preview", "teaser", "instrumental", "inst", "karaoke", "performance", "live", "sped up", "slowed", "remix", "clip", "broadcast"]
    yt_combined = (yt_title + " " + yt_album).lower()
    apple_combined = (track_en.title + " " + track_en.artist + " " + track_en.album).lower()
    if track_ko:
        apple_combined += " " + (track_ko.title + " " + track_ko.artist + " " + track_ko.album).lower()

    for marker in neg_markers:
        if marker in yt_combined and marker not in apple_combined:
            score *= 0.5
            break

    return score, title_score, artist_score, album_score


def passes_match_gates(
    track: SourceTrack,
    *,
    score: float,
    title_score: float,
    artist_score: float,
    min_score: float,
    min_title_score: float,
    min_artist_score: float,
) -> bool:
    if score < min_score or title_score < min_title_score:
        return False
    if track.artist and artist_score < min_artist_score:
        return False
    return True


def search_queries_for_track(track: SourceTrack, track_ko: SourceTrack | None) -> list[str]:
    def gen_for_track(t: SourceTrack):
        artist_candidates = artist_variants(t.artist) if t.artist else [""]
        # Filter out very long variant lists to keep queries sane, but ensure we have basic ones
        primary_artist = artist_candidates[0] if artist_candidates else ""
        
        # Collect extra artists/aliases for more search variations
        extra_artists = [a for a in artist_candidates[1:5] if a] # Limit to 4 extra variants

        # Variant titles and albums
        titles = title_variants(t.title)
        albums = album_variants(t.album)
        album_clean = re.sub(r"\s*-\s*(EP|Single)\b.*$", "", t.album, flags=re.IGNORECASE).strip() if t.album else ""
        albums_clean = unique_values([album_clean] + [re.sub(r"\s*-\s*(EP|Single)\b.*$", "", a, flags=re.IGNORECASE).strip() for a in albums])

        def build_q(*parts):
            return " ".join(unique_values([p for p in parts if p]))

        queries = []
        # Search with various titles and primary artist
        for title in titles[:3]:
            queries.extend([
                build_q(title, primary_artist, album_clean),
                build_q(title, primary_artist),
                title
            ])

        # Search with extra artists
        for extra in extra_artists:
            queries.extend([
                build_q(t.title, extra, album_clean),
                build_q(t.title, extra)
            ])

        # Search with various albums
        for alb in albums_clean[:3]:
            queries.extend([
                build_q(alb, primary_artist),
                build_q(alb, primary_artist, "topic"),
                alb
            ])

        queries.extend([
            build_q(t.title, primary_artist, album_clean, "topic"),
            build_q(t.title, primary_artist, "topic"),
        ])
        return queries

    queries = gen_for_track(track)
    if track_ko:
        artist_candidates_ko = artist_variants(track_ko.artist) if track_ko.artist else [""]
        primary_artist_ko = artist_candidates_ko[0] if artist_candidates_ko else ""
        if primary_artist_ko:
            queries.append(f"{track.title} {primary_artist_ko} {track.album}".strip())
            queries.append(f"{track.title} {primary_artist_ko}".strip())
            queries.append(f"{track.title} {primary_artist_ko} topic".strip())
        queries.extend(gen_for_track(track_ko))

    return unique_values([query for query in queries if query])


def search_ytmusic_songs(ytmusic: YTMusic, query: str, limit: int) -> list[dict[str, Any]]:
    # Try with 'songs' filter first
    try:
        results = ytmusic.search(query, filter="songs", limit=limit)
        if results:
            return results
    except Exception as exc:
        LOG.warning("Filtered search failed for query '%s': %s", query, exc)

    # Fallback: search without filter and manually pick songs/videos
    # This is more robust against language-specific UI label changes (e.g. "노래" vs "Songs")
    try:
        all_results = ytmusic.search(query, limit=limit * 2)
        filtered = [
            r for r in all_results
            if r.get("resultType") in ["song", "video", "노래", "동영상"]
        ]
        return filtered[:limit]
    except Exception as exc:
        LOG.warning("Fallback search failed for query '%s': %s", query, exc)
        return []


def search_youtube_music(
    ytmusic: YTMusic,
    track: SourceTrack,
    track_ko: SourceTrack | None,
    *,
    min_score: float,
    min_title_score: float,
    min_artist_score: float,
    limit: int,
    ignore_video_ids: set[str] | None = None,
) -> MatchResult:
    # 1. Check manual overrides first from matching_alias.json
    t_norm = normalize_text(track.title)
    a_norm = normalize_text(track.artist)
    override_key = f"{t_norm}|{a_norm}"
    
    manual_video_id = ALIASES.overrides.get(override_key)
    if not manual_video_id and track_ko:
        tk_norm = normalize_text(track_ko.title)
        ak_norm = normalize_text(track_ko.artist)
        manual_video_id = ALIASES.overrides.get(f"{tk_norm}|{ak_norm}")

    if manual_video_id:
        LOG.info("Manual override found for '%s | %s' -> video_id: %s", track.title, track.artist, manual_video_id)
        return MatchResult(
            rank=track.rank,
            title=track.title,
            artist=track.artist,
            album=track.album,
            title_en=track.title,
            artist_en=track.artist,
            album_en=track.album,
            title_ko=track_ko.title if track_ko else "",
            artist_ko=track_ko.artist if track_ko else "",
            album_ko=track_ko.album if track_ko else "",
            apple_id=track.apple_id,
            url=track.url,
            video_id=manual_video_id,
            status="manual_override",
            score=1.0,
            query="manual_override"
        )

    # 2. Proceed with search if no override
    best_result: dict[str, Any] | None = None
    best_score = 0.0
    best_title_score = 0.0
    best_artist_score = 0.0
    best_album_score = 0.0
    best_query = ""
    seen_video_ids: set[str] = set(ignore_video_ids or [])

    for query in search_queries_for_track(track, track_ko):
        time.sleep(0.5)  # Add sleep to prevent rate limiting (429 Too Many Requests)
        try:
            results = search_ytmusic_songs(ytmusic, query, limit)
        except Exception as exc:
            LOG.warning("Search failed: %s (%s)", query, exc)
            continue

        for result in results:
            video_id = result.get("videoId")
            if not is_song_result(result) or video_id in seen_video_ids:
                continue
            seen_video_ids.add(video_id)
            candidate_score, title_score, artist_score, album_score = score_result(track, result, track_ko)
            if candidate_score > best_score:
                best_score = candidate_score
                best_title_score = title_score
                best_artist_score = artist_score
                best_album_score = album_score
                best_result = result
                best_query = query

        if passes_match_gates(
            track,
            score=best_score,
            title_score=best_title_score,
            artist_score=best_artist_score,
            min_score=min_score,
            min_title_score=min_title_score,
            min_artist_score=min_artist_score,
        ):
            break

    if not best_result or not passes_match_gates(
        track,
        score=best_score,
        title_score=best_title_score,
        artist_score=best_artist_score,
        min_score=min_score,
        min_title_score=min_title_score,
        min_artist_score=min_artist_score,
    ):
        return MatchResult(
            rank=track.rank,
            title=track.title,
            artist=track.artist,
            album=track.album,
            title_en=track.title,
            artist_en=track.artist,
            album_en=track.album,
            title_ko=track_ko.title if track_ko else "",
            artist_ko=track_ko.artist if track_ko else "",
            album_ko=track_ko.album if track_ko else "",
            apple_id=track.apple_id,
            url=track.url,
            video_id=None,
            yt_title=best_result.get("title", "") if best_result else "",
            yt_artist=result_artists(best_result) if best_result else "",
            yt_album=result_album(best_result) if best_result else "",
            score=round(best_score, 3),
            title_score=round(best_title_score, 3),
            artist_score=round(best_artist_score, 3),
            album_score=round(best_album_score, 3),
            yt_result_type=best_result.get("resultType", "") if best_result else "",
            query=best_query,
        )

    return MatchResult(
        rank=track.rank,
        title=track.title,
        artist=track.artist,
        album=track.album,
        title_en=track.title,
        artist_en=track.artist,
        album_en=track.album,
        title_ko=track_ko.title if track_ko else "",
        artist_ko=track_ko.artist if track_ko else "",
        album_ko=track_ko.album if track_ko else "",
        apple_id=track.apple_id,
        url=track.url,
        artwork_url=track.artwork_url,
        video_id=best_result["videoId"],
        yt_title=best_result.get("title", ""),
        yt_artist=result_artists(best_result),
        yt_album=result_album(best_result),
        score=round(best_score, 3),
        title_score=round(best_title_score, 3),
        artist_score=round(best_artist_score, 3),
        album_score=round(best_album_score, 3),
        yt_result_type=best_result.get("resultType", ""),
        query=best_query,
        status="matched",
    )


def chunked(values: list[Any], size: int) -> list[list[Any]]:
    return [values[i : i + size] for i in range(0, len(values), size)]


def get_existing_playlist_items(ytmusic: YTMusic, playlist_id: str) -> list[dict[str, str]]:
    try:
        playlist = ytmusic.get_playlist(playlist_id, limit=None)
    except Exception as exc:
        if "404" in str(exc):
            LOG.error(f"Playlist {playlist_id} not found (404). Please verify the ID is correct and the playlist is Public or Unlisted.")
        else:
            LOG.error(f"Failed to fetch playlist items for {playlist_id}: {exc}")
        return []

    items: list[dict[str, str]] = []
    for track in playlist.get("tracks", []):
        video_id = track.get("videoId")
        set_video_id = track.get("setVideoId")
        if video_id and set_video_id:
            items.append({"videoId": video_id, "setVideoId": set_video_id})
    return items


def update_ytmusic_playlist(
    ytmusic: YTMusic,
    playlist_id: str,
    video_ids: list[str],
    *,
    description: str = "",
    dry_run: bool,
) -> None:
    if description and not dry_run:
        try:
            ytmusic.edit_playlist(playlist_id, description=description)
            LOG.info("Updated playlist description")
        except Exception as exc:
            LOG.warning("Failed to update playlist description: %s", exc)

    existing_items = get_existing_playlist_items(ytmusic, playlist_id)
    LOG.info("Current YouTube Music playlist item count: %d", len(existing_items))

    if dry_run:
        LOG.info("Dry run enabled. Skipping playlist removal/addition.")
        return

    for chunk in chunked(existing_items, 50):
        for attempt in range(3):
            try:
                ytmusic.remove_playlist_items(playlist_id, chunk)
                LOG.info("Removed %d existing items", len(chunk))
                break
            except Exception as exc:
                LOG.warning("Failed to remove %d items (attempt %d): %s", len(chunk), attempt + 1, exc)
                time.sleep(2)
        time.sleep(1.0)

    for chunk in chunked(video_ids, 50):
        for attempt in range(3):
            try:
                ytmusic.add_playlist_items(playlist_id, chunk, duplicates=False)
                LOG.info("Added %d matched items", len(chunk))
                break
            except Exception as exc:
                LOG.warning("Failed to add %d items (attempt %d): %s", len(chunk), attempt + 1, exc)
                time.sleep(2)
        time.sleep(1.0)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def make_ytmusic(auth_file: str, client_id: str = "", client_secret: str = "", language: str = "en") -> YTMusic:
    yt = None
    if client_id or client_secret:
        if not client_id or not client_secret:
            raise SystemExit("Both YTMUSIC_OAUTH_CLIENT_ID and YTMUSIC_OAUTH_CLIENT_SECRET are required.")
        if OAuthCredentials is None:
            raise SystemExit("Installed ytmusicapi does not support OAuthCredentials.")
        yt = YTMusic(
            auth_file,
            language=language,
            oauth_credentials=OAuthCredentials(
                client_id=client_id,
                client_secret=client_secret,
            ),
        )
    else:
        yt = YTMusic(auth_file, language=language)

    # Force headers based on language to ensure metadata matches the source chart
    if language == "ko":
        yt.headers.update({"Accept-Language": "ko-KR,ko;q=0.9,en-US,en;q=0.8"})
    else:
        yt.headers.update({"Accept-Language": "en-US,en;q=0.9"})
    return yt


def load_match_cache(log_dir: Path) -> dict[str, dict[str, Any]]:
    """Loads previous match results from latest_matches_crawl.json to use as a cache."""
    latest_file = log_dir / "latest_matches_crawl.json"
    if not latest_file.exists():
        return {}
    try:
        import json
        with open(latest_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            cache = {}
            for item in data:
                # Only cache successful matches with a valid video_id
                if item.get("video_id") and item.get("status") in ("matched", "proxy_matched", "cached_match", "manual_override"):
                    # Use normalized title and artist as the key
                    key = f"{normalize_text(item.get('title', ''))}|{normalize_text(item.get('artist', ''))}"
                    cache[key] = item
            if cache:
                LOG.info("Loaded %d entries from match cache: %s", len(cache), latest_file.name)
            return cache
    except Exception as e:
        LOG.warning("Failed to load match cache from %s: %s", latest_file, e)
        return {}


def cleanup_old_logs(log_dir: Path, days: int = 7) -> None:
    """log_dir 내에서 생성된 지 'days'일 이상 된 파일을 삭제합니다."""
    now = time.time()
    cutoff = now - (days * 86400)
    if not log_dir.exists():
        return
    # rglob을 사용하여 하위 디렉토리 내의 모든 파일을 재귀적으로 검사
    for item in log_dir.rglob("*"):
        if item.is_file() and item.stat().st_mtime < cutoff:
            try:
                item.unlink()
                LOG.info("Deleted old log file: %s/%s", item.parent.name, item.name)
            except Exception as e:
                LOG.warning("Failed to delete %s: %s", item.name, e)
