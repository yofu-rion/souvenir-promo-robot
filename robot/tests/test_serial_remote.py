import json
import os
import time
import unittest

from srr.remote import SerialLink


def wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class SerialLinkTest(unittest.TestCase):
    """疑似端末を M5Stack の代わりにして、USB シリアルのリモコンを通す。"""

    def setUp(self):
        self.master, slave = os.openpty()
        self.link = SerialLink(os.ttyname(slave))
        os.close(slave)
        self.link.start()

    def tearDown(self):
        self.link.close()
        os.close(self.master)

    def test_events_and_state(self):
        self.assertTrue(wait(lambda: self.link.connected))
        # つないだら hello を出して、画面を送り直させる
        self.assertTrue(wait(lambda: self.link.events.qsize() > 0))
        self.assertEqual(self.link.poll(), [{'c': 'hello'}])
        # M5Stack の起動ログは読み捨て、JSON のボタンだけを拾う
        os.write(self.master, b'ets Jul 29 2019\r\n{"c":"prog","v":1}\r\n{"c":"start"}\n')
        got = []
        self.assertTrue(wait(lambda: got.extend(self.link.poll()) or len(got) >= 2))
        self.assertEqual(got, [{'c': 'prog', 'v': 1}, {'c': 'start'}])
        # 画面の内容は 1 行の JSON で送る（日本語はそのまま）
        self.link.send({'m': 'remote', 'run': False, 't': ['停止中']})
        buf = b''
        end = time.monotonic() + 3
        while b'\n' not in buf and time.monotonic() < end:
            buf += os.read(self.master, 1024)
        self.assertEqual(json.loads(buf.split(b'\n')[0]), {'m': 'remote', 'run': False, 't': ['停止中']})


if __name__ == '__main__':
    unittest.main()
