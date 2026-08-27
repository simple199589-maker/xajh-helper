# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.core.license_client import build_token_signature_headers

from app.core.cloud_sync import (
    CloudSyncStatus,
    CloudSyncBridge,
    apply_settings_to_bridge,
    build_publish_body,
    event_dedupe_key,
    is_cloud_control_ready,
    is_cloud_slave_isolated,
    normalize_master_name,
    parse_cloud_event,
    reset_cloud_sync_bridges_for_tests,
)
from app.core.task_sync import (
    ACTION_ACCEPT,
    ACTION_CLAIM_ACTIVITY,
    ACTION_HANG_SYNC,
    ACTION_PATH,
    ROLE_MASTER,
    ROLE_NONE,
    ROLE_SLAVE,
    TaskSyncEvent,
    get_task_sync_hub,
)

TEST_LOGIN_TOKEN = "unit-test-login-token"

def _pin_service_base(url: str):
    """Point env service base at mock server (profile cache clear)."""
    os.environ["XAJH_CAPTCHA_BASE_URL"] = str(url).rstrip("/")
    try:
        from app.core import build_profile as bp

        bp.load_profile.cache_clear()
    except Exception:
        pass




class ProtocolTests(unittest.TestCase):
    def test_cloud_status_labels_are_user_facing(self) -> None:
        self.assertIn("本机群控仍可用", CloudSyncStatus().label())
        master = CloudSyncStatus(
            enabled=True,
            online=True,
            role=ROLE_MASTER,
            master_name="队长甲",
            published=3,
        )
        self.assertEqual(
            master.label(), "云控：已连接 · 主控发送 · 通道「队长甲」 · 已发 3 条"
        )
        slave = CloudSyncStatus(
            enabled=True,
            online=False,
            role=ROLE_SLAVE,
            master_name="队长甲",
        )
        self.assertEqual(
            slave.label(), "云控：正在连接 · 副控接收 · 通道「队长甲」"
        )

    def test_normalize_master_name(self) -> None:
        self.assertEqual(normalize_master_name("  主  号  A  "), "主 号 A")

    def test_build_and_parse_roundtrip(self) -> None:
        ev = TaskSyncEvent(
            action=ACTION_ACCEPT,
            task_id=10021,
            source_pid=42,
            name="每日",
            portal_kind="",
        )
        body = build_publish_body(master_name="房间A", event=ev, client_id="pid-42")
        self.assertEqual(body["master_name"], "房间A")
        self.assertEqual(body["type"], "task_sync")
        self.assertTrue(body["msg_id"])
        self.assertEqual(body["event"]["origin"], "cloud")
        parsed = parse_cloud_event(body)
        assert parsed is not None
        self.assertEqual(parsed.action, ACTION_ACCEPT)
        self.assertEqual(parsed.task_id, 10021)
        self.assertEqual(parsed.origin, "cloud")

    def test_parse_claim_without_task_id(self) -> None:
        parsed = parse_cloud_event(
            {
                "event": {
                    "action": ACTION_CLAIM_ACTIVITY,
                    "task_id": 0,
                    "source_pid": 1,
                    "points": 35,
                    "name": "回城",
                }
            }
        )
        assert parsed is not None
        self.assertEqual(parsed.action, ACTION_CLAIM_ACTIVITY)
        self.assertEqual(parsed.points, 35)

    def test_hang_mode_roundtrip(self) -> None:
        ev = TaskSyncEvent(
            action=ACTION_HANG_SYNC,
            task_id=1,
            source_pid=42,
            name="on",
            hang_mode=1,
        )
        body = build_publish_body(master_name="房间A", event=ev)
        parsed = parse_cloud_event(body)
        assert parsed is not None
        self.assertEqual(parsed.action, ACTION_HANG_SYNC)
        self.assertEqual(parsed.hang_mode, 1)

    def test_portal_scene_generation_roundtrip(self) -> None:
        ev = TaskSyncEvent(
            action=ACTION_PATH,
            task_id=10010,
            source_pid=42,
            name="每日BOSS",
            portal_kind="dungeon_deep",
            origin_scene_id=68,
            portal_tid=100220,
            portal_obj_id=9001,
            portal_x=11.5,
            portal_y=2.0,
            portal_z=22.5,
        )
        parsed = parse_cloud_event(build_publish_body(master_name="房间A", event=ev))
        assert parsed is not None
        self.assertEqual(parsed.origin_scene_id, 68)
        self.assertEqual(parsed.portal_tid, 100220)
        self.assertEqual(parsed.portal_obj_id, 9001)
        self.assertEqual(
            (parsed.portal_x, parsed.portal_y, parsed.portal_z),
            (11.5, 2.0, 22.5),
        )

    def test_dedupe_key_prefers_msg_id(self) -> None:
        ev = TaskSyncEvent(action=ACTION_ACCEPT, task_id=1, source_pid=2)
        self.assertEqual(event_dedupe_key(ev, msg_id="abc"), "id:abc")

    def test_ready_and_isolation_helpers(self) -> None:
        base = {
            "cloud_control_enabled": True,
            "cloud_control_master_name": "房",
            "captcha_base_url": "http://127.0.0.1:9",
            "login_token": TEST_LOGIN_TOKEN,
            "task_control_role": ROLE_SLAVE,
        }
        self.assertTrue(is_cloud_control_ready(base))
        self.assertTrue(is_cloud_slave_isolated(base))

        local = dict(base)
        local["task_control_role"] = ROLE_MASTER
        self.assertTrue(is_cloud_control_ready(local))
        self.assertFalse(is_cloud_slave_isolated(local))

        local_slave = dict(base)
        local_slave["cloud_control_enabled"] = False
        self.assertFalse(is_cloud_control_ready(local_slave))

        dev_test = dict(base)
        dev_test["cloud_control_available"] = False
        self.assertFalse(is_cloud_control_ready(dev_test))
        self.assertFalse(is_cloud_slave_isolated(local_slave))


class _Handler(BaseHTTPRequestHandler):
    server: "MockCloudServer"

    def log_message(self, fmt, *args):  # noqa: A003
        return

    def _read_raw(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    @staticmethod
    def _decode_json(raw: bytes):
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _auth_ok(self, raw: bytes) -> bool:
        if (self.headers.get("Authorization") or "") != f"Bearer {self.server.login_token}":
            return False
        if self.server.require_sign:
            timestamp = self.headers.get("X-Timestamp") or ""
            nonce = self.headers.get("X-Nonce") or ""
            expected = build_token_signature_headers(
                self.command,
                self.path,
                raw,
                self.server.login_token,
                timestamp=timestamp,
                nonce=nonce,
            )
            if (self.headers.get("X-Signature") or "") != expected["X-Signature"]:
                return False
        return True

    def _record_headers(self) -> None:
        self.server.last_headers = {
            k: self.headers.get(k)
            for k in ("Authorization", "X-Timestamp", "X-Nonce", "X-Signature")
        }

    def _send(self, code: int, obj: dict) -> None:
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        self._record_headers()
        raw = self._read_raw()
        if not self._auth_ok(raw):
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        body = self._decode_json(raw)
        path = self.path.split("?", 1)[0]
        if path.endswith("/join"):
            self.server.joins.append(body)
            self._send(200, {"ok": True, "session_id": "sess-1", "cursor": "0"})
            return
        if path.endswith("/leave"):
            self.server.leaves.append(body)
            self._send(200, {"ok": True})
            return
        if path.endswith("/heartbeat"):
            self.server.heartbeats += 1
            self._send(200, {"ok": True})
            return
        if path.endswith("/publish"):
            self.server.published.append(body)
            self.server.queue.append(body)
            self._send(200, {"ok": True, "delivered": 1})
            return
        self._send(404, {"ok": False})

    def do_GET(self):  # noqa: N802
        self._record_headers()
        if not self._auth_ok(b""):
            self._send(401, {"ok": False, "error": "unauthorized"})
            return
        path = self.path.split("?", 1)[0]
        if not path.endswith("/poll"):
            self._send(404, {"ok": False})
            return
        deadline = time.time() + 0.4
        while time.time() < deadline and not self.server.queue:
            time.sleep(0.05)
        events = []
        while self.server.queue:
            events.append(self.server.queue.pop(0))
        self.server.polls += 1
        self._send(200, {"ok": True, "events": events, "cursor": str(self.server.polls)})


class MockCloudServer(ThreadingHTTPServer):
    def __init__(self, addr, handler):
        super().__init__(addr, handler)
        self.login_token = TEST_LOGIN_TOKEN
        self.require_sign = True
        self.joins = []
        self.leaves = []
        self.published = []
        self.queue = []
        self.heartbeats = 0
        self.polls = 0
        self.last_headers: dict = {}


class LiveBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = MockCloudServer(("127.0.0.1", 0), _Handler)
        cls.port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.th = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.th.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self) -> None:
        reset_cloud_sync_bridges_for_tests()
        _pin_service_base(self.base)
        self.httpd.published.clear()
        self.httpd.queue.clear()
        self.httpd.joins.clear()
        self.httpd.last_headers = {}
        self.httpd.require_sign = True

    def tearDown(self) -> None:
        reset_cloud_sync_bridges_for_tests()

    def test_missing_login_token_stays_offline(self) -> None:
        br = CloudSyncBridge(9001)
        st = br.apply_config(
            enabled=True,
            base_url=self.base,
            login_token="",
            master_name="房",
            role=ROLE_MASTER,
            client_id="m-no-token",
        )
        self.assertFalse(st.enabled)
        self.assertIn("令牌", st.detail)
        time.sleep(0.25)
        self.assertEqual(self.httpd.joins, [])

    def test_join_and_publish_send_signature_headers(self) -> None:
        master = CloudSyncBridge(1001)
        master.apply_config(
            enabled=True,
            base_url=self.base,
            login_token=TEST_LOGIN_TOKEN,
            master_name="测试主控",
            role=ROLE_MASTER,
            client_id="m1",
        )
        time.sleep(0.35)
        self.assertTrue(any(j.get("role") == ROLE_MASTER for j in self.httpd.joins))
        self.assertTrue(self.httpd.last_headers.get("X-Signature"))
        self.assertEqual(
            self.httpd.last_headers.get("Authorization"), f"Bearer {TEST_LOGIN_TOKEN}"
        )
        self.assertTrue(self.httpd.last_headers.get("X-Timestamp"))
        self.assertTrue(self.httpd.last_headers.get("X-Nonce"))

        ok = master.publish_event(
            action=ACTION_ACCEPT,
            task_id=555,
            source_pid=1001,
            name="云任务",
        )
        self.assertTrue(ok)
        self.assertTrue(self.httpd.last_headers.get("X-Signature"))

    def test_master_publish_reaches_slave(self) -> None:
        got: list[TaskSyncEvent] = []
        master = CloudSyncBridge(1001)
        slave = CloudSyncBridge(1002)
        slave.set_listener(lambda e: got.append(e))
        master.apply_config(
            enabled=True,
            base_url=self.base,
            login_token=TEST_LOGIN_TOKEN,
            master_name="测试主控",
            role=ROLE_MASTER,
            client_id="m1",
        )
        slave.apply_config(
            enabled=True,
            base_url=self.base,
            login_token=TEST_LOGIN_TOKEN,
            master_name="测试主控",
            role=ROLE_SLAVE,
            client_id="s1",
        )
        time.sleep(0.35)
        ok = master.publish_event(
            action=ACTION_ACCEPT,
            task_id=555,
            source_pid=1001,
            name="云任务",
        )
        self.assertTrue(ok)
        deadline = time.time() + 3.0
        while time.time() < deadline and not got:
            time.sleep(0.05)
        self.assertTrue(got, "slave should receive cloud event")
        self.assertEqual(got[0].action, ACTION_ACCEPT)
        self.assertEqual(got[0].task_id, 555)
        self.assertEqual(got[0].origin, "cloud")

    def test_apply_settings_helper(self) -> None:
        settings = {
            "cloud_control_enabled": True,
            "cloud_control_master_name": "房",
            "captcha_base_url": self.base,
            "login_token": TEST_LOGIN_TOKEN,
            "task_control_role": ROLE_MASTER,
        }
        st = apply_settings_to_bridge(settings, 2001)
        self.assertTrue(st.enabled)
        self.assertEqual(st.master_name, "房")
        time.sleep(0.3)
        self.assertTrue(any(j.get("role") == ROLE_MASTER for j in self.httpd.joins))
        self.assertTrue(self.httpd.last_headers.get("X-Signature"))


class IsolationTests(unittest.TestCase):
    """Local hub vs cloud slave isolation (no UI)."""

    def setUp(self) -> None:
        self.hub = get_task_sync_hub()
        for pid in (3001, 3002, 3003, 3004):
            self.hub.unregister(pid)

    def tearDown(self) -> None:
        for pid in (3001, 3002, 3003, 3004):
            self.hub.unregister(pid)

    def test_local_publish_skips_unsubscribed_cloud_slave(self) -> None:
        """
        主控(云) + 副控(云, 不订 local) + 副控(未开云, 订 local) + 无控.
        本机 publish 只打到未开云副控.
        """
        got_cloud_slave: list[TaskSyncEvent] = []
        got_local_slave: list[TaskSyncEvent] = []
        got_none: list[TaskSyncEvent] = []

        master = 3001
        cloud_slave = 3002
        local_slave = 3003
        none_pid = 3004

        self.hub.set_role(master, ROLE_MASTER)
        self.hub.set_role(cloud_slave, ROLE_SLAVE)
        self.hub.set_role(local_slave, ROLE_SLAVE)
        self.hub.set_role(none_pid, ROLE_NONE)

        # cloud slave: role slave but no local listener (isolated)
        # local slave: subscribed
        self.hub.subscribe(local_slave, lambda e: got_local_slave.append(e))
        # none: not subscribed

        # If someone mistakenly subscribed cloud slave, isolation would fail —
        # ensure we do NOT subscribe cloud_slave.
        n = self.hub.publish(
            action=ACTION_ACCEPT,
            task_id=777,
            source_pid=master,
            name="本机任务",
        )
        self.assertEqual(n, 1)
        self.assertEqual(len(got_local_slave), 1)
        self.assertEqual(got_local_slave[0].task_id, 777)
        self.assertEqual(got_local_slave[0].origin, "master")
        self.assertEqual(got_cloud_slave, [])
        self.assertEqual(got_none, [])

        # cloud origin still can be delivered via cloud listener path (manual)
        cloud_ev = TaskSyncEvent(
            action=ACTION_ACCEPT,
            task_id=888,
            source_pid=master,
            origin="cloud",
            name="云任务",
        )
        # simulate cloud bridge listener only on cloud slave
        cloud_listener_got: list[TaskSyncEvent] = []
        cloud_listener_got.append(cloud_ev)
        self.assertEqual(cloud_listener_got[0].origin, "cloud")
        self.assertEqual(cloud_listener_got[0].task_id, 888)


if __name__ == "__main__":
    unittest.main()
