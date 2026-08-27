from __future__ import annotations

import unittest
from pathlib import Path

from app.core.symbol_resolver import PatternResolver, SymbolStatus, resolve_symbol


class SymbolResolverTests(unittest.TestCase):
    def test_compact_wildcard_pattern(self) -> None:
        rx = PatternResolver._regex("8B48??52 E8????????")
        self.assertIsNotNone(rx.search(bytes.fromhex("8B481152E801020304")))
        self.assertIsNone(rx.search(bytes.fromhex("8B491152E801020304")))

    def test_stale_profile_symbol_is_not_callable(self) -> None:
        path = Path(r"D:\WeGameApps\笑傲江湖OL\bin\xajh.exe")
        if not path.is_file():
            self.skipTest("xajh.exe is not installed")
        result = resolve_symbol(
            "skill_cast_note", pe_path=path, module_base=0x400000
        )
        self.assertEqual(result.status, SymbolStatus.STALE)
        self.assertFalse(result.callable)
        self.assertIsNone(result.address)


if __name__ == "__main__":
    unittest.main()
