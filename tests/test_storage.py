import tempfile
import sqlite3
from pathlib import Path
import unittest
from concurrent.futures import ThreadPoolExecutor
from server.storage import Store


class StorageTests(unittest.TestCase):
    def test_insert_once_preserves_seed_and_returns_isolated_snapshot(self):
        root = Path(__file__).resolve().parents[1] / '.local'
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            path = Path(directory) / 'store.sqlite'
            store = Store(path)
            initial = store.put_if_absent('checklists', 'official', {'version': 1, 'criteria': [1]})
            initial['criteria'].append(2)
            repeated = Store(path).put_if_absent('checklists', 'official', {'version': 2})
            self.assertEqual(repeated, {'version': 1, 'criteria': [1]})
            self.assertEqual(len(store.list('checklists')), 1)

    def test_related_updates_are_atomic(self):
        root = Path(__file__).resolve().parents[1] / ".local"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            store = Store(Path(directory) / 'store.sqlite')
            store.put('test', 'original', {'value': 1})
            with self.assertRaises(sqlite3.IntegrityError):
                store.put_many([('test', 'original', {'value': 2}), ('test', None, {'value': 3})])
            self.assertEqual(store.get('test', 'original'), {'value': 1})
            store.put_many([('test', 'original', {'value': 2}), ('test', 'decision', {'value': 3})])
            self.assertEqual(store.get('test', 'original'), {'value': 2})
            self.assertEqual(store.get('test', 'decision'), {'value': 3})

    def test_roundtrip_restart_and_copy_isolation(self):
        root = Path(__file__).resolve().parents[1] / ".local"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            path = Path(directory) / "store.sqlite"
            store = Store(path)
            value = {"text": "Сәлем / Привет", "list": [1]}
            store.put("dialogs", "a", value)
            value["list"].append(2)
            self.assertEqual(Store(path).get("dialogs", "a")["list"], [1])
            self.assertIsNone(store.get("dialogs", "missing"))
            store.put("dialogs", "a", {"text": "updated"})
            self.assertEqual(len(store.list("dialogs")), 1)

    def test_concurrent_writes(self):
        root = Path(__file__).resolve().parents[1] / ".local"
        root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            store = Store(Path(directory) / "store.sqlite")
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda i: store.put("test", str(i), {"i": i}), range(20)))
            self.assertEqual(len(store.list("test")), 20)


if __name__ == "__main__": unittest.main()
