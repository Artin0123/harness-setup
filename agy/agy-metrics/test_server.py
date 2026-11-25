#!/usr/bin/env python3
"""Unit tests for agy-metrics/server.py (zero external dependencies)."""

import json
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import server


def _encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _pb_varint(field_num: int, val: int) -> bytes:
    return _encode_varint((field_num << 3) | 0) + _encode_varint(val)


def _pb_bytes(field_num: int, payload: bytes) -> bytes:
    return _encode_varint((field_num << 3) | 2) + _encode_varint(len(payload)) + payload


class TestServerCore(unittest.TestCase):
    def test_is_valid_cid(self):
        self.assertTrue(server.is_valid_cid("cf242d80-cc7d-4cfe-897c-9ce8cfc8ccd3"))
        self.assertTrue(server.is_valid_cid("abc_123-XYZ"))
        self.assertFalse(server.is_valid_cid(""))
        self.assertFalse(server.is_valid_cid("../secret"))
        self.assertFalse(server.is_valid_cid("a/b"))
        self.assertFalse(server.is_valid_cid("a\\b"))
        self.assertFalse(server.is_valid_cid("id with space"))

    def test_model_to_bucket(self):
        self.assertEqual(server.model_to_bucket("Gemini 4 Neon (Medium)"), "gemini-5h")
        self.assertEqual(server.model_to_bucket("Claude Opus 5.5 (High)"), "3p-5h")
        self.assertEqual(server.model_to_bucket("GPT-OSS 120B (Medium)"), "3p-5h")
        self.assertEqual(server.model_to_bucket(""), "gemini-5h")

    def test_parse_gen_row(self):
        usage = (
            _pb_varint(2, 1200)   # uncachedIn
            + _pb_varint(3, 500)  # totalOut
            + _pb_varint(5, 8800) # cachedIn
            + _pb_varint(9, 150)  # think
            + _pb_varint(10, 350) # reply
        )
        ttft = _pb_varint(1, 1) + _pb_varint(2, 500_000_000)       # 1.5s
        streaming = _pb_varint(1, 2) + _pb_varint(2, 500_000_000)  # 2.5s
        m10 = _pb_varint(4, 512000)
        m9 = _pb_bytes(10, m10)
        kv = _pb_bytes(1, b"last_step_index") + _pb_bytes(2, b"14")
        m1 = (
            _pb_bytes(4, usage)
            + _pb_bytes(9, m9)
            + _pb_bytes(11, ttft)
            + _pb_bytes(12, streaming)
            + _pb_bytes(20, kv)
            + _pb_bytes(21, b"Gemini 4 Neon (Medium)")
        )
        blob = _pb_bytes(1, m1)
        row = server.parse_gen_row(7, blob)
        self.assertIsNotNone(row)
        self.assertEqual(row["idx"], 7)
        self.assertEqual(row["lastStepIndex"], 14)
        self.assertEqual(row["model"], "Gemini 4 Neon (Medium)")
        self.assertEqual(row["uncachedIn"], 1200)
        self.assertEqual(row["cachedIn"], 8800)
        self.assertEqual(row["contextTokens"], 10000)
        self.assertEqual(row["maxContextTokens"], 512000)
        self.assertEqual(row["totalOut"], 500)
        self.assertAlmostEqual(row["ttft"], 1.5, places=6)
        self.assertAlmostEqual(row["streaming"], 2.5, places=6)
        self.assertAlmostEqual(row["tps"], 200.0, places=6)

    def test_group_by_turns_and_attach_turn_quotas(self):
        reqs = [
            {"lastStepIndex": 1, "uncachedIn": 100, "cachedIn": 900, "totalOut": 50, "ttft": 1.0, "streaming": 1.0},
            {"lastStepIndex": 3, "uncachedIn": 200, "cachedIn": 800, "totalOut": 100, "ttft": 0.5, "streaming": 1.5},
            {"lastStepIndex": 12, "uncachedIn": 300, "cachedIn": 1700, "totalOut": 200, "ttft": 2.0, "streaming": 2.0},
        ]
        user_steps = [(0, "11/25 01:00"), (10, "11/25 01:05")]
        turns = server.group_by_turns(reqs, user_steps)
        # Returned newest turn first
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["turnNo"], 2)
        self.assertEqual((turns[0]["startReq"], turns[0]["endReq"]), (3, 3))
        self.assertEqual(turns[1]["turnNo"], 1)
        self.assertEqual((turns[1]["startReq"], turns[1]["endReq"]), (1, 2))

        metrics = {"turns": turns}
        deltas = [
            {"reqCount": 1, "deltaPct": 0.25},
            {"reqCount": 2, "deltaPct": 0.35},
            {"reqCount": 3, "deltaPct": 1.125},
        ]
        merged = server.attach_turn_quotas(metrics, deltas)
        self.assertAlmostEqual(merged["turns"][0]["quotaDeltaPct"], 1.125, places=4)
        self.assertAlmostEqual(merged["turns"][1]["quotaDeltaPct"], 0.6, places=4)
        # Ensure original cached dict was not mutated
        self.assertNotIn("quotaDeltaPct", turns[0])

    def test_get_active_payload_new_cid_does_not_fallback(self):
        new_cid = "00000000-0000-0000-0000-000000000000"
        payload = server.get_active_payload(requested_cid=new_cid)
        self.assertEqual(payload["conversation"], {"id": new_cid})
        self.assertIsNone(payload["metrics"])
        self.assertEqual(payload["quota"]["convDeltas"], [])
        self.assertIsNone(payload["quota"]["convTotalDeltaPct"])

    def test_token_expired_auto_refreshes_transparently(self):
        with tempfile.TemporaryDirectory() as td:
            qt = server.QuotaTracker(Path(td) / "quota.db")
            fake_401 = urllib.error.HTTPError(
                url="https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
                code=401,
                msg="Unauthorized",
                hdrs=None,
                fp=None,
            )
            fake_models = {
                "models": {
                    "gemini-4-neon": {"quotaInfo": {"remainingFraction": 0.95, "resetTime": "2099-01-01T00:00:00Z"}},
                }
            }
            tokens = [
                ("ya29.expired", "2020-01-01T00:00:00Z", "u@test"),
                ("ya29.expired", "2020-01-01T00:00:00Z", "u@test"),
                ("ya29.fresh", "2099-01-01T00:00:00Z", "u@test"),
            ]
            with mock.patch.object(server, "read_oauth_token", side_effect=tokens), \
                 mock.patch.object(server, "trigger_agy_token_refresh", return_value=True) as cli_mock, \
                 mock.patch.object(qt, "_post_v1", side_effect=[fake_401, fake_models]):
                self.assertTrue(qt.refresh(force=True))
                cli_mock.assert_called_once()
                st = qt.get_state()
                self.assertIsNone(st["error"])
                self.assertEqual(st["version"], 1)

    def test_token_expired_fallback_and_immediate_recovery_on_token_change(self):
        with tempfile.TemporaryDirectory() as td:
            qt = server.QuotaTracker(Path(td) / "quota.db")
            fake_401 = urllib.error.HTTPError(
                url="https://daily-cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels",
                code=401,
                msg="Unauthorized",
                hdrs=None,
                fp=None,
            )

            with mock.patch.object(server, "read_oauth_token", return_value=("ya29.expired", "2020-01-01T00:00:00Z", "u@test")), \
                 mock.patch.object(server, "trigger_agy_token_refresh", return_value=False), \
                 mock.patch.object(qt, "_post_v1", side_effect=fake_401):
                self.assertFalse(qt.refresh(force=True))
                st = qt.get_state()
                self.assertIn("Token 已過期", st["error"])
                self.assertFalse(qt.has_new_token_while_errored())

            with mock.patch.object(server, "read_oauth_token", return_value=("ya29.expired", "2020-01-01T00:00:00Z", "u@test")), \
                 mock.patch.object(qt, "_post_v1") as post_mock:
                self.assertFalse(qt.refresh(force=True))
                post_mock.assert_not_called()

            fake_models = {
                "models": {
                    "gemini-4-neon": {"quotaInfo": {"remainingFraction": 0.95, "resetTime": "2099-01-01T00:00:00Z"}},
                }
            }
            with mock.patch.object(server, "read_oauth_token", return_value=("ya29.fresh", "2099-01-01T00:00:00Z", "u@test")), \
                 mock.patch.object(qt, "_post_v1", side_effect=[fake_models]):
                self.assertTrue(qt.has_new_token_while_errored())
                self.assertTrue(qt.refresh(force=True))
                st = qt.get_state()
                self.assertIsNone(st["error"])
                self.assertEqual(st["version"], 1)
                self.assertFalse(qt.has_new_token_while_errored())

    def test_http_routes_backend_only(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3) as resp:
                self.assertEqual(resp.status, 200)
                body = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(body, {"service": "agy-metrics", "status": "ok"})

            for removed in (
                "/userscript/agy-metrics-overlay.user.js",
                "/manifest.webmanifest",
                "/api/conversations",
                "/api/metrics?id=abc",
                "/api/quota",
                "/api/events",
            ):
                with self.assertRaises(urllib.error.HTTPError) as ctx:
                    urllib.request.urlopen(f"http://127.0.0.1:{port}{removed}", timeout=3)
                self.assertEqual(ctx.exception.code, 404, f"Expected 404 for {removed}")
                ctx.exception.close()
        finally:
            httpd.shutdown()
            httpd.server_close()


class TestEnsureServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        hooks_dir = str(Path(__file__).resolve().parent / "hooks")
        if hooks_dir not in sys.path:
            sys.path.insert(0, hooks_dir)
        import ensure_server as es  # noqa: E402
        cls.es = es

    def test_get_latest_conversation_id_skips_child_conversations(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conversation_summaries.db"
            con = sqlite3.connect(str(db_path))
            try:
                con.execute(
                    "CREATE TABLE conversation_summaries ("
                    "conversation_id TEXT PRIMARY KEY, "
                    "last_modified_time DATETIME NOT NULL, "
                    "parent_conversation_id TEXT NOT NULL DEFAULT '')"
                )
                con.executemany(
                    "INSERT INTO conversation_summaries VALUES (?, ?, ?)",
                    [
                        ("aaaaaaaa-0000-0000-0000-000000000001", "2026-11-25 06:00:00+00:00", ""),
                        ("bbbbbbbb-0000-0000-0000-000000000002", "2026-11-25 08:00:00+00:00", ""),
                        ("cccccccc-0000-0000-0000-000000000003", "2026-11-25 09:00:00+00:00", "bbbbbbbb-0000-0000-0000-000000000002"),
                    ],
                )
                con.commit()
            finally:
                con.close()
            self.assertEqual(
                self.es.get_latest_conversation_id(db_path),
                "bbbbbbbb-0000-0000-0000-000000000002",
            )

    def test_get_latest_conversation_id_rejects_invalid_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conversation_summaries.db"
            con = sqlite3.connect(str(db_path))
            try:
                con.execute(
                    "CREATE TABLE conversation_summaries ("
                    "conversation_id TEXT PRIMARY KEY, "
                    "last_modified_time DATETIME NOT NULL, "
                    "parent_conversation_id TEXT NOT NULL DEFAULT '')"
                )
                con.execute(
                    "INSERT INTO conversation_summaries VALUES (?, ?, ?)",
                    ("bad; echo pwned", "2026-11-25 09:00:00+00:00", ""),
                )
                con.commit()
            finally:
                con.close()
            self.assertEqual(self.es.get_latest_conversation_id(db_path), "")

    def test_get_latest_conversation_id_missing_db_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(self.es.get_latest_conversation_id(Path(tmpdir) / "missing.db"), "")

    def test_wait_and_open_remote_opens_url_when_new_pid_connects(self):
        with (
            mock.patch.object(self.es, "list_agy_pids", side_effect=[{100}, {100, 200}]),
            mock.patch.object(self.es, "has_established_https", return_value=True) as mock_443,
            mock.patch("time.sleep"),
            mock.patch("webbrowser.open") as mock_open,
        ):
            self.es.wait_and_open_remote({100}, timeout_sec=5.0)
            mock_443.assert_called_once_with({200})
            mock_open.assert_called_once_with(self.es.REMOTE_URL)


if __name__ == "__main__":
    unittest.main()
