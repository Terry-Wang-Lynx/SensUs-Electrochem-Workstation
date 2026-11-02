import asyncio
import pytest

from pa_host import v52_ble
from test_v52_ble import fake_bleak


def test_read_only_firmware_help_accepts_its_documented_multiline_response(fake_bleak):
    lines=['H,CONFIG ch fsr_nA offset_code conv_code we_dac re_dac cp', 'H,DAC name uv', 'OK HELP']
    fake_bleak.responses['HELP']=lines
    async def exercise():
        async with v52_ble.BleSession('test') as session:
            assert await session.command('HELP')==lines
    asyncio.run(exercise())
    assert fake_bleak.instances[0].commands==['HELP']


def test_common_mode_migration_stages_unused_dacs_before_atomic_channel_routing(fake_bleak):
    result = v52_ble.configure_device('test', {'potential_v': .2})
    commands = fake_bleak.instances[0].commands
    assert result['dac_pair'] == 'CD'
    assert not any(c.startswith(('DAC A ', 'DAC B ')) for c in commands)
    assert commands.index('DAC C 1200000') < next(i for i,c in enumerate(commands) if c.startswith('CONFIG '))
    assert commands.index('DAC D 1000000') < next(i for i,c in enumerate(commands) if c.startswith('CONFIG '))
    assert all(c['we_dac']==2 and c['re_dac']==3 for c in result['status']['channels'].values())
    assert result['actual']['potential_uv'] == 199875
    assert not any(c.startswith('START') for c in commands)


def test_same_verified_bias_is_reused_without_dac_voltage_writes(fake_bleak):
    result = v52_ble.configure_device('test', {'working_electrode_v': .4,'potential_v':-.2})
    assert result['dac_pair']=='AB'
    assert not any(c.startswith('DAC ') for c in fake_bleak.instances[0].commands)


def test_wrong_staged_voltage_never_routes_an_electrode_to_it(fake_bleak,monkeypatch):
    original=fake_bleak.write_gatt_char
    async def wrong(client,characteristic,payload,response):
        if payload==b'STATUS\n' and any(c.startswith('DAC ') for c in client.commands):
            client.dacs['C']=0
        await original(client,characteristic,payload,response)
    monkeypatch.setattr(fake_bleak,'write_gatt_char',wrong)
    with pytest.raises(RuntimeError,match='Staged DAC'):
        v52_ble.configure_device('test',{'potential_v':.2})
    assert not any(c.startswith('CONFIG ') for c in fake_bleak.instances[0].commands)


def test_partly_routed_previous_attempt_reuses_the_already_staged_target(fake_bleak):
    async def exercise():
        async with v52_ble.BleSession('test') as session:
            c=fake_bleak.instances[0]
            c.dacs.update(C=3200,D=2667)
            c.channels[1][3:5]=[2,3]
            result=await v52_ble.configure_session(session,{'potential_v':.2})
            assert result['dac_pair']=='CD'
            assert not any(x.startswith('DAC ') for x in c.commands)
            assert all(v[3:5]==[2,3] for v in c.channels.values())
    asyncio.run(exercise())
