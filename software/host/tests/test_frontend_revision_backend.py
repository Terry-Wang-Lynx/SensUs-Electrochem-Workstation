from pathlib import Path

from pa_host import gui_server


def test_revision_tracks_served_frontend_content_without_writing_or_hardware(tmp_path, monkeypatch):
    monkeypatch.setattr(gui_server, "GUI_DIR", tmp_path)
    for name in ("index.html", "styles.css", "app.js", "high_throughput.js"):
        (tmp_path / name).write_text(name)
    first = gui_server._frontend_revision()
    assert first == gui_server._frontend_revision()
    (tmp_path / "high_throughput.js").write_text("new capture feedback")
    second = gui_server._frontend_revision()
    assert second != first
    assert len(second) == 16
    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", lambda path: (_ for _ in ()).throw(AssertionError("cache was reread")))
        assert second == gui_server._frontend_revision()


def test_missing_revision_inputs_return_unknown_not_a_false_matching_build(tmp_path, monkeypatch):
    monkeypatch.setattr(gui_server, "GUI_DIR", tmp_path)
    assert gui_server._frontend_revision() is None
