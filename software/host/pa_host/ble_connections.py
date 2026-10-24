"""Independent, read-only standby connections with explicit collector handoff."""

from __future__ import annotations

import asyncio
import copy
import threading
import time

from . import v52_ble


class BleConnections:
    def __init__(self, session_factory=None):
        self._factory = session_factory or v52_ble.BleSession
        self._lock = threading.RLock()
        self._loop = None
        self._thread = None
        self._sessions = {}
        self._guards = {}
        self._states = {}

    def snapshot(self):
        with self._lock:
            return copy.deepcopy(self._states)

    def _update(self, address, **fields):
        with self._lock:
            self._states.setdefault(address, {"address": address, "name": "", "protocol_verified": False})
            self._states[address].update(fields, updated_unix_s=time.time())

    def _run(self, operation, *args):
        with self._lock:
            if self._loop is None:
                self._loop = asyncio.new_event_loop()
                self._thread = threading.Thread(target=self._serve, args=(self._loop,), daemon=True,
                                                name="ble-connections")
                self._thread.start()
            future = asyncio.run_coroutine_threadsafe(operation(*args), self._loop)
        return future.result()

    def _serve(self, loop):
        asyncio.set_event_loop(loop)
        monitor = loop.create_task(self._monitor())
        try:
            loop.run_forever()
        finally:
            monitor.cancel()
            loop.run_until_complete(asyncio.gather(monitor, return_exceptions=True))
            loop.close()

    async def _monitor(self):
        while True:
            await asyncio.sleep(.2)
            for address, session in list(self._sessions.items()):
                try:
                    session.check_health()
                except Exception as error:
                    self._update(address, state="error", error=str(error), protocol_verified=False)

    def _address(self, address):
        if not isinstance(address, str) or not address.strip() or len(address) > 200:
            raise ValueError("请指定有效的蓝牙地址 / UUID")
        value = address.strip()
        with self._lock:
            return next((saved for saved in self._states if saved.casefold() == value.casefold()), value)

    def connect(self, address, name=""):
        return self._run(self._connect, self._address(address), name)

    async def _connect(self, address, name):
        async with self._guards.setdefault(address, asyncio.Lock()):
            session = self._sessions.get(address)
            if session is not None:
                try:
                    session.check_health()
                    if self.snapshot()[address]["state"] == "connected" and self.snapshot()[address]["protocol_verified"]:
                        return self.snapshot()[address]
                except Exception:
                    pass
                await self._disconnect_session(address)
            self._update(address, state="connecting", name=name, error="", protocol_verified=False)
            session = self._factory(address)
            entered = False
            try:
                await session.__aenter__()
                entered = True
                info = v52_ble.parse_info(await session.command("INFO"))
                status = v52_ble.parse_status(await session.command("STATUS"))
                session.check_health()
                self._sessions[address] = session
                self._update(address, state="connected", protocol_verified=True,
                             firmware={"info": info, "status": status, "protocol_verified": True, "board_profile": "v52"})
            except BaseException as error:
                if entered:
                    self._sessions[address] = session
                    try:
                        await self._disconnect_session(address)
                    except Exception:
                        pass
                self._update(address, state="error", error=str(error), protocol_verified=False)
                raise
            return self.snapshot()[address]

    def disconnect(self, address):
        address = self._address(address)
        if address not in self.snapshot():
            return
        return self._run(self._disconnect, address)

    async def _disconnect_session(self, address):
        session = self._sessions.get(address)
        if session is not None:
            try:
                await asyncio.wait_for(session.__aexit__(None, None, None), timeout=15)
            except Exception as error:
                self._update(address, state="error", error=str(error), protocol_verified=False)
                raise
            self._sessions.pop(address, None)
        self._update(address, state="disconnected", error="", protocol_verified=False)

    async def _disconnect(self, address):
        async with self._guards.setdefault(address, asyncio.Lock()):
            await self._disconnect_session(address)

    def configure(self, address, settings):
        return self._run(self._command, self._address(address), settings)

    def stop(self, address):
        return self._run(self._stop, self._address(address))

    async def _stop(self, address):
        async with self._guards.setdefault(address, asyncio.Lock()):
            session = self._sessions.get(address)
            if session is not None and (session.disconnected.is_set() or not session.client.is_connected):
                await self._disconnect_session(address)
                session = None
            try:
                if session is None:
                    self._update(address, state="connecting", error="", protocol_verified=False)
                    session = self._factory(address)
                    await session.__aenter__()
                    self._sessions[address] = session
                await session.command("STOP", cleanup=True)
                status = v52_ble.parse_status(await session.command("STATUS", cleanup=True))
                if status["mask"] != 0:
                    raise RuntimeError("V5.2 STOP not confirmed")
                self._update(address, state="connected", error="")
                return {"stop_confirmed": True, "status": status}
            except Exception as error:
                self._update(address, state="error", error=str(error), protocol_verified=False)
                raise

    async def _command(self, address, settings):
        await self._connect(address, "")
        async with self._guards[address]:
            session = self._sessions[address]
            try:
                result = await v52_ble.configure_session(session, settings)
                self._update(address, state="connected", error="", protocol_verified=True, firmware=result)
                return result
            except Exception as error:
                self._update(address, state="error", error=str(error), protocol_verified=False)
                raise

    def close(self):
        with self._lock:
            loop, thread = self._loop, self._thread
        if loop is None:
            return
        for address in self.snapshot():
            self.disconnect(address)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=5)
        with self._lock:
            self._loop = None
            self._thread = None
            self._guards.clear()
