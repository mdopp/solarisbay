"""Tests for the proxy-side image-attachment store (#202)."""

from __future__ import annotations

from solilos_chat.attachments import AttachmentStore, attach_to_messages


def test_add_and_read_batches_in_order(tmp_path):
    store = AttachmentStore(str(tmp_path))
    store.add("sess-1", ["a", "b"])
    store.add("sess-1", ["c"])
    assert store.batches("sess-1") == [["a", "b"], ["c"]]


def test_add_ignores_empty(tmp_path):
    store = AttachmentStore(str(tmp_path))
    store.add("sess-1", [])
    store.add("", ["x"])
    assert store.batches("sess-1") == []


def test_batches_missing_session_is_empty(tmp_path):
    store = AttachmentStore(str(tmp_path))
    assert store.batches("nope") == []
    assert store.batches("") == []


def test_delete_removes_store(tmp_path):
    store = AttachmentStore(str(tmp_path))
    store.add("sess-1", ["a"])
    store.delete("sess-1")
    assert store.batches("sess-1") == []
    # Idempotent — deleting a missing session is a no-op.
    store.delete("sess-1")


def test_session_id_is_path_sanitised(tmp_path):
    store = AttachmentStore(str(tmp_path))
    store.add("../../evil", ["x"])
    # The traversal-y id maps to a flat sanitised filename inside the root.
    files = [p.name for p in tmp_path.iterdir()]
    assert files == ["______evil.json"]
    assert store.batches("../../evil") == [["x"]]


def test_corrupt_store_reads_empty(tmp_path):
    (tmp_path / "sess-1.json").write_text("{not json", encoding="utf-8")
    store = AttachmentStore(str(tmp_path))
    assert store.batches("sess-1") == []


def test_attach_to_messages_correlates_placeholders_in_order():
    messages = [
        {"role": "user", "content": "first\n[screenshot]"},
        {"role": "assistant", "content": "ok"},
        {"role": "user", "content": "no image here"},
        {"role": "user", "content": "third\n[screenshot]"},
    ]
    attach_to_messages(messages, [["img-a"], ["img-b"]])
    assert messages[0]["images"] == ["img-a"]
    assert "images" not in messages[1]
    assert "images" not in messages[2]  # text-only turn skipped
    assert messages[3]["images"] == ["img-b"]


def test_attach_to_messages_no_batches_is_noop():
    messages = [{"role": "user", "content": "hi\n[screenshot]"}]
    attach_to_messages(messages, [])
    assert "images" not in messages[0]


def test_attach_to_messages_surplus_placeholders_left_bare():
    messages = [
        {"role": "user", "content": "a\n[screenshot]"},
        {"role": "user", "content": "b\n[screenshot]"},
    ]
    attach_to_messages(messages, [["only"]])
    assert messages[0]["images"] == ["only"]
    assert "images" not in messages[1]
