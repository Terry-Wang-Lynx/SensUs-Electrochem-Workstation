import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pa_host import v52_ble


@pytest.mark.parametrize('cached', [True, False])
def test_scan_miss_uses_verified_windows_identity_without_starting_acquisition(monkeypatch, cached):
    class BleakDeviceNotFoundError(Exception):
        pass
    target = SimpleNamespace(address='E1:DF:A0:57:D8:78', name='pA-V5.2')
    instances = []
    class Client:
        def __init__(self, device, **options):
            self.device, self.commands, self.closed = device, [], False
            instances.append(self)
        async def __aenter__(self):
            if isinstance(self.device, str):
                raise BleakDeviceNotFoundError('No advertising report')
            return self
        async def __aexit__(self, *args):
            self.closed = True
        async def start_notify(self, characteristic, callback):
            self.characteristic = characteristic
    monkeypatch.setitem(v52_ble.sys.modules, 'bleak', SimpleNamespace(BleakClient=Client))
    monkeypatch.setattr(v52_ble.sys, 'platform', 'win32')
    resolver = AsyncMock(return_value=target if cached else None)
    monkeypatch.setattr(v52_ble, '_windows_cached_device', resolver)
    async def scenario():
        async with v52_ble.BleSession(target.address):
            assert instances[-1].device is target
            assert instances[-1].characteristic == v52_ble.NUS_TX
    if cached:
        asyncio.run(scenario())
        assert instances[-1].closed
        assert len(instances) == 2
    else:
        with pytest.raises(BleakDeviceNotFoundError):
            asyncio.run(scenario())
        assert len(instances) == 1
    resolver.assert_awaited_once_with(target.address)
    assert all(not client.commands for client in instances)


@pytest.mark.parametrize('platform', ['win32', 'linux'])
def test_unrelated_connection_errors_do_not_retry_or_hide_the_error(monkeypatch, platform):
    class Client:
        def __init__(self, *args, **kwargs):
            pass
        async def __aenter__(self):
            raise PermissionError('Access denied')
    monkeypatch.setitem(v52_ble.sys.modules, 'bleak', SimpleNamespace(BleakClient=Client))
    monkeypatch.setattr(v52_ble.sys, 'platform', platform)
    resolver = AsyncMock()
    monkeypatch.setattr(v52_ble, '_windows_cached_device', resolver)
    async def scenario():
        async with v52_ble.BleSession('E1:DF:A0:57:D8:78'):
            pass
    with pytest.raises(PermissionError):
        asyncio.run(scenario())
    resolver.assert_not_awaited()
