import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
from pa_host import collect_ble
from test_collect_ble import collector_env
from test_v52_ble import fake_bleak

def denied():
    e=PermissionError(13,'sharing conflict');e.winerror=32;return e

@pytest.mark.parametrize('winerror',[32,None])
def test_transient_read_retries_without_extending_cached_deadline(tmp_path,monkeypatch,winerror):
    if winerror is None and collect_ble.os.name!='nt':pytest.skip('Windows CRT EACCES')
    gate=tmp_path/'gate.json';gate.write_text(json.dumps(dict(run_token='r',attempt_id='a',deadline_unix_s=time.time()+100)))
    cached=time.time()+10
    args=SimpleNamespace(start_gate=gate,run_token='r',attempt_id='a',column_deadline_unix_s=cached)
    read=Path.read_text;calls=[]
    def flaky(path,*a,**kw):
        calls.append(path)
        if len(calls)<3:
            e=denied();e.winerror=winerror;raise e
        return read(path,*a,**kw)
    monkeypatch.setattr(Path,'read_text',flaky)
    assert asyncio.run(collect_ble._shared_column_deadline(args))==cached
    assert len(calls)==3

@pytest.mark.parametrize('persistent',[False,True])
@pytest.mark.parametrize('stage',['starting','running'])
def test_after_start_read_contention_recovers_or_confirms_stop(collector_env,monkeypatch,persistent,stage):
    fake,args=collector_env
    args.fault_policy='guided';args.attempt_id='a';args.start_gate=args.out.parent/'gate.json'
    args.start_gate.write_text(json.dumps(dict(run_token=args.run_token,attempt_id='a',deadline_unix_s=time.time()+1)))
    read=Path.read_text;failures=[]
    def flaky(path,*a,**kw):
        started=fake.instances and any(x.startswith('START ') for x in fake.instances[0].commands)
        running=started and json.loads(read(args.state_file)).get('phase')=='running'
        if path==args.start_gate and started and (stage=='starting' or running):
            if persistent or not failures:
                failures.append(1);raise denied()
        return read(path,*a,**kw)
    monkeypatch.setattr(Path,'read_text',flaky)
    monkeypatch.setattr(collect_ble,'NO_DATA_MIN_SECONDS',10)
    result=asyncio.run(collect_ble.collect(args))
    state=json.loads(read(args.state_file))
    assert state['stop_confirmed']
    assert result==(1 if persistent else 0)
    assert len(failures)==(6 if persistent else 1)
    commands=fake.instances[0].commands
    assert 'STOP' in commands[next(i for i,x in enumerate(commands) if x.startswith('START '))+1:]

def test_retry_observes_stop_and_keeps_event_loop_responsive(tmp_path,monkeypatch):
    args=SimpleNamespace(start_gate=tmp_path/'gate',cmd_file=tmp_path/'cmd')
    monkeypatch.setattr(Path,'read_text',lambda *a,**kw:(_ for _ in ()).throw(denied()))
    async def exercise():
        stop=asyncio.Event()
        async def request_stop():
            await asyncio.sleep(.002);stop.set()
        task=asyncio.create_task(request_stop())
        with pytest.raises(asyncio.CancelledError):await collect_ble._shared_column_deadline(args,stop)
        await task
    asyncio.run(exercise())

def test_deadline_expiry_during_contention_returns_original_deadline(tmp_path,monkeypatch):
    deadline=time.time()+.003
    args=SimpleNamespace(start_gate=tmp_path/'gate',column_deadline_unix_s=deadline)
    monkeypatch.setattr(Path,'read_text',lambda *a,**kw:(_ for _ in ()).throw(denied()))
    assert asyncio.run(collect_ble._shared_column_deadline(args))==deadline

@pytest.mark.parametrize('content',['{bad',json.dumps(dict(run_token='wrong',attempt_id='a'))])
def test_invalid_gate_is_not_hidden(tmp_path,content):
    gate=tmp_path/'gate';gate.write_text(content)
    args=SimpleNamespace(start_gate=gate,run_token='r',attempt_id='a')
    with pytest.raises((ValueError,RuntimeError)):asyncio.run(collect_ble._shared_column_deadline(args))

def test_other_access_errors_are_not_retried(tmp_path,monkeypatch):
    args=SimpleNamespace(start_gate=tmp_path/'gate');calls=[]
    def fail(*a,**kw):
        calls.append(1);e=PermissionError(13,'not a Windows sharing error');e.winerror=87;raise e
    monkeypatch.setattr(Path,'read_text',fail)
    with pytest.raises(PermissionError):asyncio.run(collect_ble._shared_column_deadline(args))
    assert len(calls)==1

def test_write_state_and_stop_requested_retry_windows_sharing_contention(tmp_path,monkeypatch):
    state_path=tmp_path/'state.json'
    replace=Path.replace;replace_calls=[]
    def flaky_replace(self,target):
        replace_calls.append(target)
        if len(replace_calls)<3:
            e=PermissionError(13,'access denied');e.winerror=5;raise e
        return replace(self,target)
    monkeypatch.setattr(Path,'replace',flaky_replace)
    collect_ble._write_state(state_path,{'phase':'running'})
    assert len(replace_calls)==3
    assert json.loads(state_path.read_text(encoding='utf-8'))=={'phase':'running'}

    cmd_path=tmp_path/'commands.txt'
    cmd_path.write_text('STOP\n',encoding='utf-8')
    open_fn=Path.open;open_calls=[]
    def flaky_open(self,*a,**kw):
        if self==cmd_path:
            open_calls.append(self)
            if len(open_calls)<3:
                raise denied()
        return open_fn(self,*a,**kw)
    monkeypatch.setattr(Path,'open',flaky_open)
    assert collect_ble._stop_requested(cmd_path,asyncio.Event()) is True
    assert len(open_calls)==3

