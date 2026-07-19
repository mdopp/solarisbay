from solaris_chat.engine.importers import google_takeout as gt


def test_google_takeout_kind_registered():
    imp = gt.get("google_takeout")
    assert imp is not None
    assert isinstance(imp, gt.Importer)  # satisfies the runtime-checkable protocol


def test_registry_roundtrip():
    class _Dummy:
        def detect(self, manifest):
            return []

        def plan(self, archive, selections):
            return gt.ImportPlan(kind="dummy")

        def run(self, plan, progress):
            return []

    gt.register("dummy", _Dummy())
    try:
        assert gt.get("dummy") is not None
        assert gt.get("dummy").plan(None, None).kind == "dummy"
    finally:
        gt.REGISTRY.pop("dummy", None)


def test_stub_run_not_wired_yet():
    import pytest

    imp = gt.get("google_takeout")
    with pytest.raises(NotImplementedError):
        imp.detect(None)
