from solaris_chat.engine.importers.google_takeout import catalog


def test_podcast_recognised():
    assert catalog.classify("Fest & Flauschig", "Folge 300") == "Podcast"
    assert catalog.classify("Gemischtes Hack", "irgendwas") == "Podcast"


def test_hoerspiel_recognised():
    assert catalog.classify("Benjamin Blümchen", "Der Zoo") == "Hörspiel"
    assert catalog.classify("SomeChannel", "Bibi und Tina - Das Fest") == "Hörspiel"


def test_music_not_misclassified():
    assert catalog.classify("Taylor Swift", "Anti-Hero") is None
    assert catalog.classify("The Offspring", "The Kids Aren't Alright") is None
