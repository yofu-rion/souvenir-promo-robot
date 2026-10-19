"""M5Stack リモコン（m5remote/）との Bluetooth LE 通信。Pi がセントラルになって探し、つなぎ直す。

  - EVT（notify、M5 → Pi）：押したボタンの JSON。{"c":"start"} {"c":"adj","v":-10} {"c":"mode","v":"calib"}
  - STATE（write、Pi → M5）：画面に出す内容の JSON。{"m":"remote","run":true,"t":["行1","行2"]}

制御ループ（50 Hz）を止めないよう、BLE は別スレッドの asyncio で動かす。ループとは queue でやりとりする。
"""
from __future__ import annotations

import asyncio
import json
import logging
import queue
import re
import subprocess
import threading

log = logging.getLogger(__name__)

SERVICE_UUID = '6e3f0001-5a1e-4c1b-9f5b-7d2a1c0e5a01'
EVT_UUID = '6e3f0002-5a1e-4c1b-9f5b-7d2a1c0e5a01'
STATE_UUID = '6e3f0003-5a1e-4c1b-9f5b-7d2a1c0e5a01'


class RemoteLink:
    def __init__(self, name: str = 'SRR-Remote', scan_timeout: float = 10.0):
        self.name, self.scan_timeout = name, scan_timeout
        self.events: queue.Queue = queue.Queue()
        self.connected = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._latest: bytes | None = None
        self._wake: asyncio.Event | None = None
        self._running = False
        self._thread: threading.Thread | None = None

    # ---- 制御ループから呼ぶ ----
    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._thread_main, name='remote', daemon=True)
        self._thread.start()

    def poll(self) -> list[dict]:
        out = []
        while True:
            try:
                out.append(self.events.get_nowait())
            except queue.Empty:
                return out

    def send(self, msg: dict) -> None:
        """最新の 1 件だけ送る（送り切る前に次が来たら古い方は捨てる）。"""
        data = json.dumps(msg, ensure_ascii=False, separators=(',', ':')).encode()
        loop, wake = self._loop, self._wake
        if loop is None or wake is None:
            return

        def put():
            self._latest = data
            wake.set()
        loop.call_soon_threadsafe(put)

    def close(self, timeout: float = 3.0) -> None:
        """接続を切ってから終わる。切らずにプロセスが終わると BlueZ が接続を残し、M5Stack が広告を出せなくなる。"""
        self._running = False
        if self._loop is not None and self._wake is not None:
            self._loop.call_soon_threadsafe(self._wake.set)
        if self._thread is not None:
            self._thread.join(timeout)

    # ---- BLE スレッド ----
    def _thread_main(self) -> None:
        try:
            asyncio.run(self._main())
        except Exception:
            log.exception('リモコンの通信が止まった')

    async def _main(self) -> None:
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError:
            log.warning('bleak がないのでリモコンを使わない（pip install bleak）')
            return
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        await asyncio.to_thread(self._drop_stale)
        while self._running:
            try:
                dev = await BleakScanner.find_device_by_filter(
                    lambda d, ad: (ad.local_name or d.name) == self.name, timeout=self.scan_timeout)
            except Exception as e:  # アダプタが使えないときなど
                log.warning('リモコンを探せない: %s', e)
                await asyncio.sleep(5)
                continue
            if dev is None:
                continue
            gone = asyncio.Event()
            try:
                async with BleakClient(dev, disconnected_callback=lambda _c: gone.set()) as client:
                    await client.start_notify(EVT_UUID, self._on_event)
                    self.connected = True
                    log.info('リモコンにつないだ（%s）', dev.address)
                    self.events.put({'c': 'hello'})
                    while self._running and not gone.is_set():
                        if self._latest is None:
                            self._wake.clear()
                            waiter = asyncio.create_task(self._wake.wait())
                            gone_w = asyncio.create_task(gone.wait())
                            await asyncio.wait({waiter, gone_w}, return_when=asyncio.FIRST_COMPLETED)
                            waiter.cancel()
                            gone_w.cancel()
                            continue
                        data, self._latest = self._latest, None
                        await client.write_gatt_char(STATE_UUID, data, response=True)
            except Exception as e:
                log.warning('リモコンとの接続が切れた: %s', e)
            finally:
                if self.connected:
                    log.info('リモコンと切れた。探し直す')
                self.connected = False
                await asyncio.sleep(1)

    def _drop_stale(self) -> None:
        """前のプロセスが切らずに終わって残った接続を切る（つながったままだと見つけられない）。"""
        try:
            out = subprocess.run(['bluetoothctl', 'devices', 'Connected'], capture_output=True, text=True,
                                 timeout=5).stdout
        except (OSError, subprocess.TimeoutExpired):
            return
        for addr, name in re.findall(r'^Device ([0-9A-F:]{17}) (.+)$', out, re.M):
            if name.strip() == self.name:
                log.info('残っていたリモコンの接続を切る（%s）', addr)
                subprocess.run(['bluetoothctl', 'disconnect', addr], capture_output=True, timeout=10)

    def _on_event(self, _sender, data: bytearray) -> None:
        try:
            ev = json.loads(bytes(data).decode())
        except (UnicodeDecodeError, ValueError):
            log.warning('リモコンから読めないデータ: %r', bytes(data))
            return
        if isinstance(ev, dict) and 'c' in ev:
            self.events.put(ev)


class MockRemote:
    """テスト用。push() で押したボタンを入れ、sent に送った内容が残る。"""

    def __init__(self):
        self.connected = True
        self.pending: list[dict] = []
        self.sent: list[dict] = []

    def start(self):
        pass

    def push(self, cmd: str, value=None) -> None:
        ev = {'c': cmd}
        if value is not None:
            ev['v'] = value
        self.pending.append(ev)

    def poll(self):
        out, self.pending = self.pending, []
        return out

    def send(self, msg):
        self.sent.append(msg)

    def close(self):
        pass
