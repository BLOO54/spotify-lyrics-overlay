"""Troubleshooting helper: show which timed-lyrics versions LRCLIB has for a song
and which one the app would pick. Prints timing information only (no lyrics text).

    python tools/check_song.py "birdbrain" "Jamie Paige" 245
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.lyrics_provider import LrclibClient, fits_song, lyrics_end  # noqa: E402


def main(title, artist, duration):
    c = LrclibClient(None)
    items = c._search_all(title, artist)
    got = c._get_json("/get", {"track_name": title, "artist_name": artist, "duration": int(duration)})
    print(f"Song: {title} - {artist}, Spotify length {duration:.0f} s")
    print(f"Exact /get match: {'yes' if got else 'no'}"
          + (f" (id {got.get('id')}, length {got.get('duration')}, synced={'yes' if got.get('syncedLyrics') else 'no'}, "
             f"lyrics end {lyrics_end(got.get('syncedLyrics')):.1f} s, fits={fits_song(got.get('syncedLyrics'), duration)})"
             if got else ""))
    print(f"Search results: {len(items)}")
    for score, it in c._rank(items, title, artist, duration):
        syn = it.get("syncedLyrics")
        print(f"  score {score:6.1f}  id {it.get('id')}  '{it.get('trackName')}' / '{it.get('artistName')}'  "
              f"length {it.get('duration')}  synced={'yes' if syn else 'no'}  "
              f"lyrics end {lyrics_end(syn):6.1f} s  fits={fits_song(syn, duration)}")
    pick = c.fetch(title, artist, "", duration)
    print(f"App would use: synced={'yes' if pick.synced else 'no'}, lyrics end {lyrics_end(pick.synced):.1f} s, "
          f"fits={fits_song(pick.synced, duration)}")
    print(f"Versions offered by Ctrl+Alt+V: {len(c.alternatives(title, artist, '', duration))}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], float(sys.argv[3]))
