"""Recover the Korean weekly Top Songs chart from YouTube Charts' own playlist."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import date

from bs4 import BeautifulSoup
from ytmusicapi.continuations import get_continuations_2025
from ytmusicapi.navigation import (
    CONTENT,
    RESPONSIVE_HEADER,
    SECTION,
    SECTION_LIST_ITEM,
    TAB_CONTENT,
    TWO_COLUMN_RENDERER,
    nav,
)
from ytmusicapi.parsers.playlists import parse_playlist_header_meta, parse_playlist_items

from ytmusic_playlist_sync import _read_metadata_with_retry

PLAYLIST_ID = "PL4fGSI1pDJn6jXS_Tv_N9B8Z0HTRVJE0m"
REFERENCE_URL = "https://chartex.com/charts/youtube/weekly/south-korea"
SOURCE = "youtube_charts_official_playlist"
HELD = "unavailable_no_verified_recording"


def official_playlist_rows(client):
    # The pinned ytmusicapi parser omits videoId on greyed-out items. Read the
    # source ID and the parsed metadata from the same renderer, including continuations.
    def request(body):
        return client._send_request("browse", body)

    payload = request({"browseId": "VL" + PLAYLIST_ID})
    header = nav(payload, [*TWO_COLUMN_RENDERER, *TAB_CONTENT, *SECTION_LIST_ITEM, *RESPONSIVE_HEADER])
    meta = parse_playlist_header_meta(header)
    shelf = nav(
        payload, [*TWO_COLUMN_RENDERER, "secondaryContents", *SECTION, *CONTENT, "musicPlaylistShelfRenderer"]
    )
    if (
        shelf.get("playlistId") != PLAYLIST_ID
        or meta.get("author", {}).get("id") != "FEmusic_charts"
        or meta["title"] != "Top 100 Songs South Korea"
        or meta["trackCount"] != 100
    ):
        raise ValueError("Unexpected official KR Top Songs playlist identity or count")

    def parse_rows(contents):
        rows = []
        for item in contents:
            if "continuationItemRenderer" in item:
                continue
            renderer = item["musicResponsiveListItemRenderer"]
            (track,) = parse_playlist_items([item])
            video = renderer.get("playlistItemData", {}).get("videoId")
            if track.get("videoId") and video and track["videoId"] != video:
                raise ValueError("Conflicting video IDs in a single playlist slot")
            video = video or track.get("videoId") or ""
            artists = [artist["name"] for artist in track["artists"] if artist.get("name")]
            title = track["title"]
            if not title or not artists or (video and not re.fullmatch(r"[A-Za-z0-9_-]{11}", video)):
                raise ValueError("Incomplete official playlist slot")
            unavailable = track.get("isAvailable") is False
            if not video and not unavailable:
                raise ValueError("Available official playlist slot has no video ID")
            song_id = (
                video
                or "fallback:ytmusic:playlist:"
                + hashlib.sha256((title + "\0" + "\0".join(artists)).encode()).hexdigest()
            )
            rows.append(
                dict(
                    title=title,
                    artist=", ".join(artists),
                    album=(track.get("album") or {}).get("name", ""),
                    video_id=video,
                    song_id=song_id,
                    source_unavailable=unavailable,
                    source=SOURCE,
                    duration_seconds=track.get("duration_seconds") or 0,
                    artist_ids=[a["id"] for a in track["artists"]]
                    if all(a.get("id") for a in track["artists"])
                    else [],
                )
            )
        return rows

    rows = parse_rows(shelf["contents"])
    rows.extend(get_continuations_2025(shelf, 101 - len(rows), request, parse_rows))
    if len(rows) != 100 or len({r["song_id"] for r in rows}) != 100:
        raise ValueError("Official weekly playlist must contain 100 distinct songs")
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return rows


def _key(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum())


def _names(credits):
    # Keep credited guests, including an English name in a Korean credit (아이유(IU)).
    names = []
    for name in re.split(r",|\s+&\s+", credits):
        if name.strip():
            names.append({_key(name)} | {_key(alias) for alias in re.findall(r"\(([A-Za-z0-9 .-]+)\)", name)})
    return names


def _song_identity(title, artists):
    main_names = _names(artists)
    credit = re.search(r"\s*\((?:feat\.?|ft\.?|narr\.?)\s+(.+)\)$", title, re.I)
    if credit:
        artists += ", " + credit[1]
        title = title[: credit.start()]
    titles = {_key(title)}
    translation = re.fullmatch(r"(?=.*[가-힣]).+?\s+\(([a-zA-Z0-9 '’-]+)\)", title)
    if translation and not re.search(
        r"\b(live|remix|ver|version|instrumental|acoustic|edit|remaster(?:ed)?|sped|slowed|english|japanese|korean|chinese)\b",
        translation[1],
        re.I,
    ):
        titles.add(_key(translation[1]))
    return titles, main_names, _names(artists)


def verify_week(rows, html, start, end):
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if first.weekday() != 4 or last.weekday() != 3 or (last - first).days != 6:
        raise ValueError("Expected a Friday–Thursday YouTube chart period")
    page = BeautifulSoup(html, "html.parser")
    heading = page.find("h1")
    if not heading or heading.get_text(strip=True) != "YouTube Top Songs South Korea":
        raise ValueError("Wrong reference country or chart type")
    button = page.select_one('button[aria-label^="Chart date,"]')
    period = button.get_text(" ", strip=True) if button else ""
    start_format = "%d" if first.month == last.month else "%d %b" if first.year == last.year else "%d %b %Y"
    label = first.strftime(start_format) + "–" + last.strftime("%d %b %Y")

    def display_date(value):
        return re.sub(r"\b0(?=\d)", "", re.sub(r"\s*[-–—]\s*", "–", value)).replace("Sept ", "Sep ")

    if display_date(period) != "Weekly · " + display_date(label):
        raise ValueError(f"Reference date {period!r} does not confirm {start}..{end}")
    top = page.select("table tbody tr")[:20]
    if len(top) != 20 or len(rows) != 100:
        raise ValueError("Incomplete corroborating top 20 or official top 100")
    for rank, (row, tr) in enumerate(zip(rows, top), 1):
        cells = tr.find_all("td", recursive=False)
        if len(cells) < 2 or cells[0].get_text(strip=True) != str(rank) or row["rank"] != rank:
            raise ValueError(f"Reference rank mismatch at {rank}")
        name = cells[1].select_one('a[title][href^="/song/"], span[title]')
        credits = name.parent.find_next_sibling("div") if name else None
        if not credits:
            raise ValueError(f"Missing reference credits at {rank}")
        artists = ", ".join(
            tag.get_text(strip=True) for tag in credits.select(':scope > a[href^="/artist/"], :scope > span')
        )
        if not artists:
            raise ValueError(f"Missing reference artist at {rank}")
        titles, main, names = _song_identity(row["title"], row["artist"])
        other_titles, other_main, other_names = _song_identity(name["title"], artists)
        if (
            not titles.intersection(other_titles)
            or not any(left & right for left in main for right in other_main)
            or not all(any(n & m for m in other_names) for n in names)
            or not all(any(m & n for n in names) for m in other_names)
        ):
            raise ValueError(f"Dated top 20 differs from the official playlist at rank {rank}")


def collect_official_playlist(client, session, start, end):
    rows = official_playlist_rows(client)
    response = session.get(REFERENCE_URL, timeout=20)
    response.raise_for_status()
    verify_week(rows, response.text, start, end)
    for row in rows:
        row.update(chart_period_start=start, chart_period_end=end, collection_method="official_playlist")
    return rows


def unavailable_replacement(client, row, cached, limit):
    """A read error must abort before playlist mutation; only confirmed exclusions are held."""
    original = row["original_video_id"]
    manual = cached.get("status") == "manual_override"
    held = dict(mapping_status="failed", mapping_reason=HELD, resolved_video_id="", mapping_score=0.0)
    if not client or not original:
        return held

    def player(video_id):
        payload = _read_metadata_with_retry(client.get_song, video_id)
        state = payload.get("playabilityStatus", {}).get("status")
        details = payload.get("videoDetails") or {}
        if (
            state not in {"OK", "UNPLAYABLE"}
            or (details and details.get("videoId") != video_id)
            or (state == "OK" and not details)
        ):
            raise RuntimeError(f"Unable to confirm playability of {video_id}")
        return state, details

    def resolved(video_id, track):
        return dict(
            mapping_status="manual_override" if manual else "resolved_to_song",
            mapping_reason="verified_playable_replacement",
            mapping_score=1.0,
            resolved_video_id=video_id,
            resolved_title=track.get("title") or row["original_title"],
            resolved_artist=", ".join(a["name"] for a in track.get("artists", []))
            or row["original_artist_or_channel"],
            resolved_album=(track.get("album") or {}).get("name") or row.get("album", ""),
        )

    target = cached.get("video_id")
    if manual:
        return resolved(target, {}) if target and player(target)[0] == "OK" else held
    source_state, source = player(original)
    if source_state == "OK":
        return {
            **resolved(original, {}),
            "mapping_status": "already_song",
            "mapping_reason": "source_player_ok",
        }
    artist_ids = set(row.get("artist_ids") or [])
    source_artist = row["original_artist_or_channel"]
    source_channel = (
        source.get("channelId") if _key(source.get("author") or "") == _key(source_artist) else None
    )
    source_title = row["original_title"]
    length = int(source.get("lengthSeconds") or row.get("duration_seconds_original") or 0)

    def equivalent(video_id):
        state, details = player(video_id)
        if state != "OK" or not length or abs(length - int(details.get("lengthSeconds") or 0)) > 2:
            return None
        # A disabled song may have no watch queue at all. Its exact player
        # channel can still verify another recording on that same channel.
        same_channel = source_channel and source_channel == details.get("channelId")
        if (
            same_channel
            and _key(source_title) == _key(source.get("title") or "") == _key(details.get("title") or "")
            and _key(source_artist) == _key(details.get("author") or "")
        ):
            return resolved(video_id, {})
        if artist_ids:
            watch = _read_metadata_with_retry(client.get_watch_playlist, videoId=video_id, limit=3)
            for track in watch["tracks"]:
                if (
                    track.get("videoId") == video_id
                    and _key(track.get("title") or "") == _key(source_title)
                    and {a.get("id") for a in track.get("artists", [])} == artist_ids
                    and (
                        not row.get("album")
                        or _key(row["album"]) == _key((track.get("album") or {}).get("name", ""))
                    )
                ):
                    return resolved(video_id, track)
        return None

    seen = {original}
    if target and target not in seen:
        seen.add(target)
        match = equivalent(target)
        if match:
            return match
    query = row["original_title"] + " " + row["original_artist_or_channel"]
    for kind in ("songs", "videos"):
        results = client.search(query, filter=kind, limit=limit)
        for result in results[:limit]:
            video_id = result.get("videoId")
            if not video_id or video_id in seen:
                continue
            seen.add(video_id)
            candidate_ids = {a.get("id") for a in result.get("artists") or []}
            names = ", ".join(a.get("name", "") for a in result.get("artists") or [])
            if not candidate_ids.intersection(artist_ids | {source_channel}) and _key(names) != _key(
                source_artist
            ):
                continue
            match = equivalent(video_id)
            if match:
                return match
    return held
