from solaris_chat.engine.importers.google_takeout import library
from solaris_chat.engine.importers.google_takeout.paths import ImporterPaths
from solaris_chat.engine.importers.google_takeout.textnorm import track_key


def test_owned_and_owns(music_dir, paths):
    music_dir.add("Taylor Swift", "Midnights", "Anti-Hero")
    keys, by = library.owned_index(paths)
    assert track_key("Taylor Swift", "Anti-Hero") in keys
    assert library.owns(keys, by, "Taylor Swift", "Anti-Hero") is True
    assert library.library_size() == 1


def test_owns_fuzzy_matches_tag_typo(music_dir, paths):
    music_dir.add("Jamiroquai", "Synkronized", "Failling")  # real-world library typo
    keys, by = library.owned_index(paths)
    assert library.owns(keys, by, "Jamiroquai", "Falling") is True
    assert library.owns(keys, by, "Jamiroquai", "Totally Other Song") is False


def test_owns_ignores_the_prefix(music_dir, paths):
    music_dir.add("Smashing Pumpkins", "Adore", "Ava Adore")
    keys, by = library.owned_index(paths)
    assert library.owns(keys, by, "The Smashing Pumpkins", "Ava Adore") is True


def test_owns_compilation_artist_in_title():
    # Bravo-Hits style: artist tag is the compilation, real artist is in the title.
    keys, by = set(), {}
    library.add_owned(keys, by, "Bravo Hits 28", "Dr. Ring-Ding - Ring Of Fire")
    assert library.owns(keys, by, "Dr. Ring-Ding", "Ring Of Fire") is True


def test_track_number_prefix_stripped(music_dir, paths):
    music_dir.add("Band", "Album", "03 - Real Title")
    keys, _by = library.owned_index(paths)
    assert track_key("Band", "Real Title") in keys


def test_cache_reused_when_unchanged(music_dir, paths):
    music_dir.add("A", "B", "C")
    library.owned_keys(paths)
    sig = library.signature_of(library.list_audio_files(paths))
    assert library.cached_index(sig) is not None


def test_empty_library(music_dir, paths):
    assert library.owned_keys(paths) == set()


def test_audiobooks_folder_included(tmp_path):
    (tmp_path / "music").mkdir()
    ab = tmp_path / "audiobooks" / "Paw Patrol"
    ab.mkdir(parents=True)
    (ab / "Kapitel 10.mp3").write_bytes(b"")
    paths = ImporterPaths(
        radicale_data=tmp_path / "radicale",
        music_dir=tmp_path / "music",
        data_dir=tmp_path / "data",
        audiobooks_dir=tmp_path / "audiobooks",
    )
    library._cache.update({"sig": None, "keys": set(), "by_artist": {}, "count": 0})
    keys, _by = library.owned_index(paths)
    assert track_key("Paw Patrol", "Kapitel 10") in keys
