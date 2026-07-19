from solaris_chat.engine.importers.google_takeout.textnorm import normalize, track_key


def test_normalize_diacritics_and_case():
    assert normalize("Motörhead Beyoncé") == "motorhead beyonce"


def test_normalize_strips_parens_and_feat():
    assert normalize("Song (Remastered 2011) feat. Someone") == "song"


def test_normalize_ampersand():
    assert normalize("Simon & Garfunkel") == "simon and garfunkel"


def test_normalize_empty():
    assert normalize("") == ""
    assert normalize(None) == ""


def test_normalize_eszett_matches_ss():
    assert normalize("Großstadtgeflüster") == normalize("Grossstadtgefluster")


def test_track_key_combines_normalized():
    assert track_key("The Offspring", "The Kids") == "the offspring\tthe kids"
