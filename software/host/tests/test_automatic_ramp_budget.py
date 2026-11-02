import pytest

from pa_host.emm_motion import MotionError
from test_emm_motion import MotionTestClock
from test_powered_manual_driver import powered_bus


@pytest.mark.parametrize('hard_timeout', [None, 6])
def test_54mm_return_can_finish_drive_ramp_but_explicit_deadline_stays_hard(monkeypatch, hard_timeout):
    bus,serial,_=powered_bus(enabled=True)
    bus.axes['X']['max_speed_mm_s']=20
    bus.confirm_reference();bus.enable('X');bus.enable('Z')
    clock=MotionTestClock();clock.attach(monkeypatch,serial)
    original_wait=bus._stop_event.wait
    def wait(timeout=None):
        clock.sleep(timeout or 0)
        return original_wait(0)
    monkeypatch.setattr(bus._stop_event,'wait',wait)
    serial.move_completes=False
    write=serial.write;read=bus._read_axis
    trajectory={}
    def delayed_move(frame):
        if frame[1]==0xFD:
            delta=round(int.from_bytes(frame[6:10],'big')*65536/3200)*(-1 if frame[2] else 1)
            trajectory.update(end=clock.monotonic()+9.7,raw=serial.positions[frame[0]]+delta)
        elif frame[1]==0xFE:
            trajectory.clear();serial.speeds[frame[0]]=0;serial.statuses[frame[0]]=3
        return write(frame)
    def delayed_feedback(axis,**options):
        if axis=='X' and trajectory:
            done=clock.monotonic()>=trajectory['end']
            serial.speeds[1]=0 if done else 150
            serial.statuses[1]=3 if done else 1
            if done:
                serial.positions[1]=trajectory['raw']
                for targets in serial.targets.values():targets[1]=trajectory['raw']
        return read(axis,**options)
    monkeypatch.setattr(serial,'write',delayed_move)
    monkeypatch.setattr(bus,'_read_axis',delayed_feedback)
    bus._move_timeout=hard_timeout
    serial.writes.clear()
    if hard_timeout:
        with pytest.raises(MotionError) as caught:bus.move_to('X',54,20)
        assert caught.value.code=='move_timeout'
        assert caught.value.stop_result['stop_confirmed']
    else:
        result=bus.move_to('X',54,20)
        assert result['position']['X']==pytest.approx(54,abs=.001)
        assert not any(f[1]==0xFE for f in serial.writes)
    assert len([f for f in serial.writes if f[1]==0xFD])==1
