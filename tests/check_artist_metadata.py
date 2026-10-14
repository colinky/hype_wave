"""Read-only live regression for the Topic channel that stopped Apple matching."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ytmusic_playlist_sync import _artist_name_for_metadata, bilingual_cache_read_only, make_ytmusic


def main():
    with bilingual_cache_read_only():
        expected = {"UCCa_4FfbVdc6zhWmRndKwfQ": "A2O LITE", "UCcy9NwrIzmzITEqiVstaN1A": "A2O"}
        for language in ("ko", "en"):
            client = make_ytmusic(None, language=language)
            observed = {artist_id: _artist_name_for_metadata(client, artist_id) for artist_id in expected}
            assert observed == expected, (language, observed)
    print("Verified live NEON artist headers: both exact linked IDs, ko/en names; no DB or playlist writes")


if __name__ == "__main__":
    main()
