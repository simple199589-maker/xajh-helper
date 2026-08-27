from __future__ import annotations

import unittest

from app.core.session_store import GameSession, SessionStore


class SessionStoreIdentityTests(unittest.TestCase):
    def test_injected_role_binding_is_immutable_per_mount(self) -> None:
        store = SessionStore()
        session = store.mount(GameSession(pid=12345, hwnd=10))

        bound = store.bind_injected_role(12345, "900001", "甲")

        self.assertIs(bound, session)
        self.assertEqual(session.role_id, "900001")
        self.assertEqual(session.role_name, "甲")
        self.assertIsNone(store.bind_injected_role(12345, "900002", "乙"))
        self.assertEqual(session.role_id, "900001")
        self.assertEqual(session.role_name, "甲")


if __name__ == "__main__":
    unittest.main()