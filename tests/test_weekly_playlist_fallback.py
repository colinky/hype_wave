import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from hype_db import connect, export_frontend_history
from hype_db_reports import _apple_history_anchor_jobs
import sync_all
import ytmusic_to_ytmusic_crawl as crawl
import youtube_weekly_playlist as weekly


START, END = "2026-11-13", "2026-11-19"
STARTED = "2026-11-23T07:05:00+00:00"


def chart():
    return [
        dict(
            rank=i,
            song_id=f"v{i:010d}",
            video_id=f"v{i:010d}",
            title=f"Song {i}",
            artist="Artist",
            source=weekly.SOURCE,
            source_unavailable=i == 39,
        )
        for i in range(1, 101)
    ]


def reference(rows, date="13–19 Nov 2026"):
    body = "".join(
        f'<tr><td>{t["rank"]}</td><td><div><span title="{t["title"]}"></span></div>'
        f"<div><span>{t['artist']}</span></div></td></tr>"
        for t in rows[:20]
    )
    return (
        f'<h1>YouTube Top Songs South Korea</h1><button aria-label="Chart date, {date}">Weekly · {date}</button>'
        f"<table><tbody>{body}</tbody></table>"
    )


class WeeklyFallbackTests(unittest.TestCase):
    def test_original_id_stays_with_its_disabled_playlist_slot(self):
        client = Mock()
        header = {"author": {"id": "FEmusic_charts"}, "title": "Top 100 Songs South Korea", "trackCount": 100}
        contents = [
            {
                "musicResponsiveListItemRenderer": {
                    "playlistItemData": {"videoId": row["video_id"]},
                    "rank": row["rank"],
                }
            }
            for row in chart()
        ]
        shelf = {"playlistId": weekly.PLAYLIST_ID, "contents": contents}

        def parse(items):
            renderer = items[0]["musicResponsiveListItemRenderer"]
            available = renderer["rank"] != 39
            return [
                dict(
                    title=f"Song {renderer['rank']}",
                    artists=[{"name": "artist", "id": "a"}],
                    videoId=renderer["playlistItemData"]["videoId"] if available else None,
                    isAvailable=available,
                )
            ]

        with (
            patch.object(
                weekly,
                "nav",
                side_effect=lambda data, path: shelf if path[-1] == "musicPlaylistShelfRenderer" else header,
            ),
            patch.object(weekly, "parse_playlist_header_meta", return_value=header),
            patch.object(weekly, "parse_playlist_items", side_effect=parse),
        ):
            rows = weekly.official_playlist_rows(client)
            self.assertEqual(
                (rows[38]["rank"], rows[38]["video_id"], rows[38]["source_unavailable"]),
                (39, "v0000000039", True),
            )
            self.assertEqual(rows[39]["rank"], 40)
            contents[38]["musicResponsiveListItemRenderer"]["playlistItemData"]["videoId"] = None
            self.assertTrue(
                weekly.official_playlist_rows(client)[38]["song_id"].startswith("fallback:ytmusic:playlist:")
            )
            header["author"]["id"] = "another-publisher"
            with self.assertRaises(ValueError):
                weekly.official_playlist_rows(client)

    def test_dated_top_twenty(self):
        rows = chart()
        weekly.verify_week(rows, reference(rows), START, END)
        for html in (
            reference(rows, "6–12 Nov 2026"),
            reference(rows).replace("South Korea", "Japan"),
            reference(rows).replace("Song 20", "Song 20 (Live)"),
            reference(rows).replace("Artist", "Another Artist", 1),
            reference(rows[:19]),
            reference(rows[:5] + rows[6:]),
        ):
            with self.subTest(), self.assertRaises(ValueError):
                weekly.verify_week(rows, html, START, END)
        rows[2].update(title="삐그덕 (seesaw) (feat. 아이유(IU))", artist="Crush")
        localized = reference(rows).replace(rows[2]["title"], "seesaw (feat. IU)")
        weekly.verify_week(rows, localized, START, END)
        with self.assertRaises(ValueError):
            weekly.verify_week(rows, localized.replace("feat. IU", "feat. Other"), START, END)
        weekly.verify_week(rows, reference(rows, "29 Dec 2023–4 Jan 2024"), "2023-12-29", "2024-01-04")

    def test_wrong_week_csv_falls_through_and_accepts_missing_video_in_dated_csv(self):
        from requests import Response

        response = Response()
        response.url = "https://charts.youtube.com/youtube-charts-top-songs-kr-weekly-20261119.csv"
        response._content = (
            "Rank,Track Name,Artist Names,YouTube URL\n"
            + "\n".join(f"{i},Song,Artist,n/a" for i in range(1, 101))
        ).encode()
        self.assertEqual(len(crawl.dated_csv_rows(response, START, END)), 100)
        self.assertFalse(crawl.dated_csv_rows(response, "2026-11-06", "2026-11-12"))
        response._content += b"\n101,Overflow,Artist,n/a"
        self.assertFalse(crawl.dated_csv_rows(response, START, END))

    def test_cache_then_search_and_playability(self):
        row = dict(original_video_id="origin", original_title="Song", original_artist_or_channel="Artist")
        client = Mock()

        def player(vid):
            return dict(
                playabilityStatus={"status": "UNPLAYABLE" if vid == "origin" else "OK"},
                videoDetails=dict(
                    videoId=vid,
                    title="Song (Live)" if vid == "live" else "Song",
                    channelId="other" if vid == "wrong" else "owner",
                    author="Artist",
                    lengthSeconds="200",
                ),
            )

        client.get_song.side_effect = player
        client.search.return_value = [
            dict(videoId=v, artists=[dict(name="Artist", id="owner")]) for v in ["wrong", "live", "same"]
        ]
        result = weekly.unavailable_replacement(client, row, {"video_id": "same"}, 10)
        self.assertEqual(result["resolved_video_id"], "same")
        client.search.assert_not_called()
        result = weekly.unavailable_replacement(client, row, {}, 10)
        self.assertEqual(result["resolved_video_id"], "same")
        client.search.return_value = [dict(videoId="wrong", artists=[dict(name="Artist", id="other")])]
        self.assertEqual(weekly.unavailable_replacement(client, row, {}, 10)["mapping_reason"], weekly.HELD)
        client.get_song.side_effect = RuntimeError("lookup outage")
        with self.assertRaisesRegex(RuntimeError, "lookup outage"):
            weekly.unavailable_replacement(client, row, {"video_id": "same"}, 10)

    def test_fallback_persists_all_ranks_and_accounts_for_held_slot(self):
        rows = chart()
        client = Mock()
        client.get_song.return_value = dict(
            playabilityStatus={"status": "UNPLAYABLE"},
            videoDetails={
                "videoId": rows[38]["video_id"],
                "title": "Song 39",
                "author": "Artist",
                "channelId": "artist",
                "lengthSeconds": "200",
            },
        )
        client.search.return_value = []
        actual = [r["video_id"] for r in rows if not r["source_unavailable"]]
        client.get_playlist.return_value = {
            "trackCount": len(actual),
            "tracks": [{"videoId": vid, "setVideoId": "slot" + vid} for vid in actual],
        }
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            argv = [
                "weekly",
                "--db-path",
                str(db),
                "--env-file",
                str(Path(tmp) / "empty"),
                "--yt-auth",
                "fixture",
                "--yt-playlist-id",
                "destination",
                "--chart-period-end",
                END,
            ]
            with (
                patch.dict(
                    os.environ,
                    HYPE_DB_BACKEND="sqlite",
                    HYPE_DEFER_HISTORY_EXPORT="1",
                    HYPE_MATCH_STARTED_AT=STARTED,
                ),
                patch.object(sys, "argv", argv),
                patch.object(crawl, "make_ytmusic", return_value=client),
                patch.object(weekly, "official_playlist_rows", return_value=rows) as fallback,
                patch.object(crawl.http_session, "get", return_value=Mock(text=reference(rows))),
                patch.object(crawl, "extract_chart_entries_from_youtube_charts", return_value=[]) as official,
            ):
                self.assertEqual(crawl.main(), 0)
                with connect(db, read_only=True) as conn:
                    self.assertTrue(sync_all.youtube_week_ready(conn, "Weekly-Hot-100", END))
                    slots = conn.execute(
                        "SELECT rank_order FROM playlist_order ORDER BY rank_order"
                    ).fetchall()
                    self.assertEqual([r[0] for r in slots], list(range(1, 101)))
                    result = sync_all.publication_result(
                        conn, dict(target_id="destination", job_name="Weekly-Hot-100"), STARTED
                    )
                    self.assertEqual((result["mode"], result["published"]), ("partial", 99))
                    self.assertEqual(result["excluded"][0]["position"], 39)
                # The history date is the next day of the completed Apple source.
                with connect(db) as conn:
                    uid = conn.execute(
                        "SELECT track_uid FROM platform_song_ids WHERE song_id=?", (actual[0],)
                    ).fetchone()[0]
                    conn.execute("INSERT INTO platform_song_ids VALUES ('apple','apple-fixture',?)", (uid,))
                    conn.execute("""INSERT INTO track_list(service,song_id,title_en,artist_en)
                                    VALUES ('apple','apple-fixture','Song 1','Artist')""")
                    for job in _apple_history_anchor_jobs():
                        conn.execute(
                            "INSERT INTO playlist_order VALUES ('apple',?,'default','2026-11-22','apple-fixture',1)",
                            (job,),
                        )
                        conn.execute(
                            """INSERT INTO match_runs(run_id,service,job_name,reference_period,started_at,
                                     created_at,status,completed_at) VALUES (?,'apple',?,'2026-11-22',?,?,'completed',?)""",
                            (job, job, STARTED, STARTED, STARTED),
                        )
                    conn.commit()
                history = export_frontend_history(db, Path(tmp) / "history.json", expected_date="2026-11-23")
                self.assertEqual(history["rankings"]["2026-11-23"][actual[0]]["ytmusic_rank"], 1)
                # The same source must not be persisted or published as a different week.
                with patch.object(sys, "argv", argv + ["--chart-period-end", "2026-11-26"]):
                    self.assertEqual(crawl.main(), 1)
                with connect(db, read_only=True) as conn:
                    self.assertEqual(
                        conn.execute(
                            "SELECT DISTINCT reference_period FROM playlist_order WHERE service='ytmusic'"
                        ).fetchone()[0],
                        "2026-W47",
                    )
                    self.assertEqual(
                        conn.execute("SELECT count(*) FROM playlist_update_runs").fetchone()[0], 1
                    )
                    self.assertEqual(
                        conn.execute(
                            "SELECT expected_chart_period_end FROM chart_source_audit WHERE status='source_error'"
                        ).fetchone()[0],
                        "2026-11-26",
                    )
                official.return_value = [
                    {
                        **r,
                        "source": "youtube_charts_weekly_browse_api",
                        "source_unavailable": False,
                        "chart_period_start": START,
                        "chart_period_end": END,
                    }
                    for r in rows
                ]
                # The normal path must never contact either playlist fallback source.
                fallback.reset_mock()
                with patch.object(sys, "argv", argv + ["--db-only", "--no-resolve", "--dry-run"]):
                    self.assertEqual(crawl.main(), 0)
                fallback.assert_not_called()
