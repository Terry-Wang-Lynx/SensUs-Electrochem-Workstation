import json
from pathlib import Path
import pytest
from pa_host.automation import _write_json

def test_transient_windows_reader_keeps_previous_gate_until_replace(tmp_path,monkeypatch):
    p=tmp_path/'gate.json';p.write_text('{"old":true}')
    original=Path.replace;calls=[]
    def replace(src,dst):
        calls.append(1)
        if len(calls)<3:
            assert json.loads(p.read_text())=={'old':True}
            e=PermissionError('sharing');e.winerror=32;raise e
        return original(src,dst)
    monkeypatch.setattr(Path,'replace',replace)
    _write_json(p,{'new':True})
    assert len(calls)==3 and json.loads(p.read_text())=={'new':True}
    assert not list(tmp_path.glob('*.tmp'))

def test_permanent_sharing_failure_is_bounded_and_preserves_previous(tmp_path,monkeypatch):
    p=tmp_path/'gate.json';p.write_text('{"old":true}');calls=[]
    def replace(src,dst):
        calls.append(1);e=PermissionError('sharing');e.winerror=32;raise e
    monkeypatch.setattr(Path,'replace',replace)
    with pytest.raises(PermissionError):_write_json(p,{'new':True})
    assert len(calls)==6 and json.loads(p.read_text())=={'old':True}
    assert not list(tmp_path.glob('*.tmp'))
