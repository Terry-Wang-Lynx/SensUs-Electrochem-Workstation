import asyncio
import pytest
from pa_host import v52_ble
from test_v52_ble import fake_bleak

def test_stop_drains_ack_of_timed_out_start_but_requires_own_ack(fake_bleak):
    async def run():
        async with v52_ble.BleSession('test', command_timeout=.03) as session:
            fake_bleak.responses['START 15']=[]
            with pytest.raises(TimeoutError):await session.command('START 15')
            fake_bleak.responses['STOP']=['OK START','OK STOP']
            assert await session.command('STOP',cleanup=True)==['OK STOP']
    asyncio.run(run())

def test_late_start_alone_never_proves_stopped(fake_bleak):
    async def run():
        async with v52_ble.BleSession('test', command_timeout=.03) as session:
            fake_bleak.responses['START 15']=[]
            with pytest.raises(TimeoutError):await session.command('START 15')
            fake_bleak.responses['STOP']=['OK START']
            with pytest.raises(TimeoutError):await session.command('STOP',cleanup=True)
    asyncio.run(run())

def test_unrelated_start_reply_is_still_protocol_error(fake_bleak):
    async def run():
        async with v52_ble.BleSession('test',command_timeout=.03) as session:
            fake_bleak.responses['STOP']=['OK START','OK STOP']
            with pytest.raises(RuntimeError,match='unexpected reply'):await session.command('STOP',cleanup=True)
    asyncio.run(run())

def test_cancelled_start_ack_is_drained_during_cleanup(fake_bleak):
    async def run():
        async with v52_ble.BleSession('test', command_timeout=.1) as session:
            fake_bleak.responses['START 15']=[]
            pending=asyncio.create_task(session.command('START 15'))
            await asyncio.sleep(.01)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):await pending
            fake_bleak.responses['STOP']=['OK START','OK STOP']
            assert await session.command('STOP',cleanup=True)==['OK STOP']
    asyncio.run(run())
