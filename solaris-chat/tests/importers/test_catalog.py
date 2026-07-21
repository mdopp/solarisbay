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


def test_classify_memoized_per_track():
    # A watch history replays the same track many times; the (LLM) classifier must
    # run at most ONCE per (artist, title) — else the import fires thousands of
    # redundant ollama calls (#943).
    calls = []

    def counting(artist, title):
        calls.append((artist, title))
        return "Musik"

    catalog.set_llm_classifier(counting)  # also clears the memo
    try:
        catalog.classify("A", "B")
        catalog.classify("A", "B")
        catalog.classify("C", "D")
        assert calls == [("A", "B"), ("C", "D")]  # ("A","B") only once
    finally:
        catalog.set_llm_classifier(None)
