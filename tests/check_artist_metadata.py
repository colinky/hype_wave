"""Read-only live regression for the Topic channel that stopped Apple matching."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ytmusic_playlist_sync import bilingual_cache_read_only, get_verified_video_metadata, make_ytmusic


def main():
    with bilingual_cache_read_only():
        metadata = get_verified_video_metadata(make_ytmusic(None, language="en"), "rlYOsUfpi3M")
    assert metadata and metadata["video_id"] == "rlYOsUfpi3M"
    assert metadata["title_ko"] == metadata["title_en"] == "NEON"
    assert metadata["artist_ids"] == ["UCCa_4FfbVdc6zhWmRndKwfQ", "UCcy9NwrIzmzITEqiVstaN1A"]
    assert metadata["artist_ko"] == metadata["artist_en"] == "A2O, A2O LITE"
    print("Verified live NEON metadata: exact video, both performer IDs, ko/en names; no DB or playlist writes")


if __name__ == "__main__":
    main()
