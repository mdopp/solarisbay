import json

from solaris_chat.engine.importers.google_takeout.importers import contacts as con

VCF = b"""BEGIN:VCARD
VERSION:3.0
FN:Max Mustermann
EMAIL:max@example.com
END:VCARD
BEGIN:VCARD
VERSION:3.0
FN:Erika Mueller
UID:erika-123
END:VCARD
"""


def _user_root(paths, user):
    return paths.radicale_data / "collections" / "collection-root" / user


def test_preview():
    p = con.preview("c.vcf", VCF)
    assert p["cards"] == 2
    assert "Max Mustermann" in p["samples"]


def test_import_writes_two_cards_and_props(paths):
    rep = con.do_import(paths.radicale_data, "conu1", "c.vcf", VCF)
    assert rep["written"] == 2
    cdir = _user_root(paths, "conu1") / "contacts"
    assert len(list(cdir.glob("*.vcf"))) == 2
    assert json.loads((cdir / ".Radicale.props").read_text())["tag"] == "VADDRESSBOOK"


def test_generated_uid_for_card_without_one(paths):
    con.do_import(paths.radicale_data, "conu2", "c.vcf", VCF)
    cdir = _user_root(paths, "conu2") / "contacts"
    # the card with an explicit UID keeps it
    assert (cdir / "erika-123.vcf").exists()
