"""`.contacts` create parses a raw blob into name / email / phone (#1001).

The create card sends the whole typed line as one `value`; downstream cross-source
dedup (#994) needs clean structured fields to match on, so the parse must pull the
email- and phone-shaped runs out and keep the rest as the name.
"""

from __future__ import annotations

from solaris_chat.server import _parse_contact_input


def test_name_only():
    assert _parse_contact_input("michael dopp") == ("michael dopp", "", "")


def test_name_and_phone():
    assert _parse_contact_input("michael dopp 01775524222") == (
        "michael dopp",
        "",
        "01775524222",
    )


def test_name_and_email():
    assert _parse_contact_input("michael dopp mdopp@web.de") == (
        "michael dopp",
        "mdopp@web.de",
        "",
    )


def test_name_phone_and_email():
    assert _parse_contact_input("michael dopp 01775524222 mdopp@web.de") == (
        "michael dopp",
        "mdopp@web.de",
        "01775524222",
    )


def test_messy_whitespace():
    assert _parse_contact_input("  Michael   Dopp\t 0177 552 4222   mdopp@web.de ") == (
        "Michael Dopp",
        "mdopp@web.de",
        "0177 552 4222",
    )


def test_email_only():
    assert _parse_contact_input("mdopp@web.de") == ("", "mdopp@web.de", "")


def test_phone_only():
    assert _parse_contact_input("+49 177 5524222") == ("", "", "+49 177 5524222")
