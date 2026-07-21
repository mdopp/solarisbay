import json
from datetime import datetime, timezone

from solaris_chat.engine.importers.google_takeout import music_shopping as m


def _entry(title, artist, vid, time="2026-07-15T10:00:00.000Z", topic=True):
    name = f"{artist} - Topic" if topic else artist
    return {
        "header": "YouTube Music",
        "title": title + " angesehen",
        "titleUrl": f"https://music.youtube.com/watch?v={vid}",
        "subtitles": [{"name": name}],
        "time": time,
    }


HIST = json.dumps(
    [
        _entry("Anti-Hero", "Taylor Swift", "a"),
        _entry("Anti-Hero", "Taylor Swift", "a", "2026-07-14T10:00:00.000Z"),
        _entry("Missing One", "Artist One", "vidX"),
        _entry("Missing Two", "Artist Two", "vidY"),
        {
            "header": "YouTube",
            "title": "Ad angesehen",
            "titleUrl": "https://www.youtube.com/watch?v=z",
        },
        _entry("Old Track", "Old Artist", "old", "2019-01-01T10:00:00.000Z"),
    ]
).encode()


def _seed_cache(paths, mapping):
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    (paths.data_dir / "ytmusic_album_cache.json").write_text(json.dumps(mapping))


def test_aggregate_filters_nonmusic_and_counts():
    plays = m.aggregate_plays(HIST)
    assert len(plays) == 4
    ah = next(p for p in plays.values() if p["title"] == "Anti-Hero")
    assert ah["count"] == 2 and ah["topic"] is True


def test_since_drops_old_plays():
    since = datetime(2020, 1, 1, tzinfo=timezone.utc)
    titles = {p["title"] for p in m.aggregate_plays(HIST, since=since).values()}
    assert "Old Track" not in titles and "Anti-Hero" in titles


def _songs(res):
    return {s["title"] for g in res["groups"] for s in g["songs"]}


def test_owned_excluded_from_missing(music_dir, paths):
    music_dir.add("Taylor Swift", "Midnights", "Anti-Hero")
    res = m.analyze(HIST, paths, cap=0)
    assert "Anti-Hero" not in _songs(res)
    assert res["owned_matches"] >= 1


def test_min_plays_filter(music_dir, paths):
    assert m.analyze(HIST, paths, min_plays=2, cap=0)["unique_tracks"] == 1


def test_resolve_off_groups_by_artist(music_dir, paths):
    res = m.analyze(HIST, paths, resolve=False)
    assert all(g["album"] == m.UNRESOLVED_LABEL for g in res["groups"])
    assert res["resolved_tracks"] == 0


def test_resolve_uses_cache_no_network(music_dir, paths):
    _seed_cache(paths, {"vidX": {"album": "Album A", "artist": "Artist A"}})
    res = m.analyze(HIST, paths, resolve=True, cap=0)
    assert "Album A" in {g["album"] for g in res["groups"]}
    assert res["resolved_tracks"] >= 1


def test_set_cover_minimises_albums(music_dir, paths):
    hist = json.dumps(
        [
            _entry("Dup Song", "Art", "vd1"),  # appears on AlbumX ...
            _entry("Dup Song", "Art", "vd2"),  # ... and on AlbumY
            _entry("Solo Song", "Art", "vd3"),  # only on AlbumX
        ]
    ).encode()
    _seed_cache(
        paths,
        {
            "vd1": {"album": "AlbumX", "artist": "Art"},
            "vd2": {"album": "AlbumY", "artist": "Art"},
            "vd3": {"album": "AlbumX", "artist": "Art"},
        },
    )
    res = m.analyze(hist, paths, resolve=True, cap=0)
    # AlbumX covers both songs, so AlbumY is dropped — fewest albums to buy.
    assert {g["album"] for g in res["groups"]} == {"AlbumX"}
    x = next(g for g in res["groups"] if g["album"] == "AlbumX")
    assert len(x["songs"]) == 2


def test_categories_topic_vs_other(music_dir, paths):
    hist = json.dumps(
        [
            _entry("Music Song", "Bandy", "m1"),  # "- Topic" → Musik
            _entry("Pod Ep", "Some Show", "p1", topic=False),  # not Topic → Sonstiges
        ]
    ).encode()
    res = m.analyze(hist, paths, resolve=False)
    cats = {g["category"] for g in res["groups"]}
    assert "Musik" in cats and "Sonstiges" in cats
    assert set(res["categories"]) >= {"Musik", "Sonstiges"}


def test_exports(music_dir, paths):
    res = m.analyze(HIST, paths, resolve=False)
    assert m.to_csv(res["groups"]).startswith(
        "Kategorie,Artist,Album,Song,Abspielungen"
    )
    md = m.to_markdown(res["groups"])
    assert md.startswith("# Musik-Einkaufsliste") and "### " in md


def test_to_notes_frontmatter(music_dir, paths):
    res = m.analyze(HIST, paths, resolve=False)
    note = m.to_notes(res["groups"], "mdopp", "2026-01-01T00:00:00+00:00")
    assert note.startswith("---")
    assert "type: music-wishlist" in note and "resident: mdopp" in note
    assert "# Musik-Einkaufsliste" in note


def test_progress_events(music_dir, paths):
    evs = list(m.analyze_iter(HIST, paths, resolve=False))
    assert {"parse", "match", "done"} <= {e["stage"] for e in evs}
    assert evs[-1]["pct"] == 100 and "result" in evs[-1]


# --- parsing / categorisation --------------------------------------------


def _hist(title, channel, vid="v"):
    return json.dumps(
        [
            {
                "header": "YouTube Music",
                "title": title + " angesehen",
                "titleUrl": f"https://music.youtube.com/watch?v={vid}",
                "subtitles": [{"name": channel}],
            }
        ]
    ).encode()


def test_parses_artist_title_from_non_topic_upload():
    p = list(
        m.aggregate_plays(
            _hist("Grossstadtgeflüster - Ich muss gar nichts", "KingFreakyFreak")
        ).values()
    )[0]
    assert p["artist"] == "Grossstadtgeflüster" and p["title"] == "Ich muss gar nichts"
    assert p["topic"] is False and p["hoerspiel"] is False


def test_strips_video_tags():
    p = list(
        m.aggregate_plays(_hist("Band - Song (Official Video)", "Uploader")).values()
    )[0]
    assert p["artist"] == "Band" and p["title"] == "Song"


def test_topic_title_not_split():
    p = list(m.aggregate_plays(_hist("A - B", "Artist - Topic")).values())[0]
    assert p["artist"] == "Artist" and p["title"] == "A - B" and p["topic"] is True


def test_hoerspiel_detected_and_not_music(music_dir, paths):
    hist = _hist("Kapitel 10: Paw Patrol - Der Mighty Kinofilm", "SomeChannel")
    p = list(m.aggregate_plays(hist).values())[0]
    assert p["hoerspiel"] is True and p["title"].startswith("Kapitel 10")  # not split
    res = m.analyze(hist, paths, resolve=False)
    assert any(g["category"] == "Hörspiel" for g in res["groups"])


def test_podcast_from_seed_catalog(music_dir, paths):
    hist = _hist("Nur der Anfang", "Fest & Flauschig")  # no "Folge N" signal
    p = list(m.aggregate_plays(hist).values())[0]
    assert p["podcast"] is True and p["artist"] == "Fest & Flauschig"
    res = m.analyze(hist, paths, resolve=False)
    assert any(g["category"] == "Podcast" for g in res["groups"])


# --- album resolution is validated (never attach a wrong album) -----------


class _FakeYT:
    def __init__(self, watch=None, search=None):
        self._w, self._s = watch, search or []

    def get_watch_playlist(self, videoId, limit=1):
        if self._w is None:
            raise RuntimeError("no watch playlist")
        return {"tracks": [self._w]}

    def search(self, q, filter=None, limit=5):
        return self._s


def test_lookup_uses_direct_album_when_title_matches(monkeypatch):
    monkeypatch.setattr(
        m,
        "_yt_client",
        lambda: _FakeYT(
            watch={
                "title": "Anti-Hero",
                "album": {"name": "Midnights"},
                "artists": [{"name": "Taylor Swift"}],
            }
        ),
    )
    assert m._lookup_album("v", "Taylor Swift", "Anti-Hero") == (
        "Midnights",
        "Taylor Swift",
    )


def test_lookup_rejects_mix_and_searches(monkeypatch):
    # watch returns an unrelated mix track (title mismatch) → reject; search finds
    # the real artist album. This is the "everything lands in one wrong album" fix.
    monkeypatch.setattr(
        m,
        "_yt_client",
        lambda: _FakeYT(
            watch={
                "title": "Some Mix Song",
                "album": {"name": "Kiddisht"},
                "artists": [{"name": "Kidda"}],
            },
            search=[
                {
                    "title": "Ich muss gar nichts",
                    "album": {"name": "Muss laut sein"},
                    "artists": [{"name": "Grossstadtgeflüster"}],
                }
            ],
        ),
    )
    assert m._lookup_album("v", "Grossstadtgeflüster", "Ich muss gar nichts") == (
        "Muss laut sein",
        "Grossstadtgeflüster",
    )


def test_lookup_returns_none_when_nothing_matches(monkeypatch):
    monkeypatch.setattr(
        m,
        "_yt_client",
        lambda: _FakeYT(
            watch={"title": "Mix", "album": {"name": "X"}, "artists": [{"name": "Y"}]},
            search=[
                {
                    "title": "Other",
                    "album": {"name": "Z"},
                    "artists": [{"name": "Nope"}],
                }
            ],
        ),
    )
    assert m._lookup_album("v", "Grossstadtgeflüster", "Ich muss gar nichts") == (
        None,
        None,
    )


def test_set_cover_prefers_real_artist_over_various(music_dir, paths):
    hist = json.dumps(
        [
            _entry("Song1", "Real", "s1a"),
            _entry("Song1", "Real", "s1b"),
            _entry("Song2", "Real", "s2a"),
            _entry("Song2", "Real", "s2b"),
        ]
    ).encode()
    _seed_cache(
        paths,
        {
            "s1a": {"album": "Comp", "artist": "Various Artists"},
            "s1b": {"album": "Album", "artist": "Real"},
            "s2a": {"album": "Comp", "artist": "Various Artists"},
            "s2b": {"album": "Album", "artist": "Real"},
        },
    )
    res = m.analyze(hist, paths, resolve=True, cap=0)
    assert {g["album"] for g in res["groups"]} == {"Album"}
