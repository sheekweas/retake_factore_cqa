import copy
import json
import os
import unittest
from unittest.mock import patch
from io import BytesIO
from urllib.parse import parse_qs, urlsplit
from server.data import normalize_dataset, fetch_dataset


def fixture():
    return {"conversation_id": "synthetic-example", "channel": "voice call", "language": "KZ", "transcript": [
        {"timestamp": "2026-01-01T10:00:00+05:00", "speaker": "bot", "text": "демонстрационный пример, не официальный чек-лист"},
        {"timestamp": "2026-01-01T10:00:05+05:00", "speaker": "operator", "text": "Сәлеметсіз бе"}], "audio_url": None}


class DataTests(unittest.TestCase):
    def test_roles_timestamps_ids_and_offsets(self):
        source = fixture()
        before = copy.deepcopy(source)
        result = normalize_dataset([source])[0]
        self.assertEqual(source, before)
        self.assertEqual(result["transcript"][1]["id"], "synthetic-example:1")
        self.assertEqual(result["transcript"][1]["audio_offset_seconds"], 5)
        self.assertEqual(result["transcript"][0]["speaker"], "bot")
        self.assertEqual(result["transcript"][1]["timestamp"], source["transcript"][1]["timestamp"])

    def test_envelope_and_languages(self):
        for language in ("RU", "KZ", "EN"):
            record = dict(fixture(), language=language)
            result = normalize_dataset({"isSuccess": True, "data": {"dataset_group_id": "TEST", "records": [record]}})
            self.assertEqual(result[0]["dataset_group_id"], "TEST")
        self.assertEqual(normalize_dataset({"records": []}), [])

    def test_invalid_role_and_duplicate_rejected(self):
        record = fixture()
        record["transcript"][0]["speaker"] = "assistant"
        with self.assertRaises(ValueError): normalize_dataset([record])
        with self.assertRaises(ValueError): normalize_dataset([fixture(), fixture()])
        with self.assertRaises(ValueError): normalize_dataset({"isSuccess": False})

    def test_fetch_preserves_query_and_hides_errors(self):
        payload = json.dumps({"records": [fixture()]}).encode()
        with patch.dict(os.environ, {"CQA_DEV_URL": "https://example.invalid/api?sig=private&group_id=DEV"}):
            with patch("server.data.urlopen", return_value=BytesIO(payload)) as opened:
                result = fetch_dataset("CONTROL")
                self.assertEqual(result[0]["dataset_group_id"], "CONTROL")
                query = parse_qs(urlsplit(opened.call_args.args[0].full_url).query)
                self.assertEqual(query, {"sig": ["private"], "group_id": ["CONTROL"]})
            with patch("server.data.urlopen", side_effect=RuntimeError("private-url")):
                with self.assertRaises(ValueError) as error: fetch_dataset()
                self.assertNotIn("private", str(error.exception))


if __name__ == "__main__": unittest.main()
