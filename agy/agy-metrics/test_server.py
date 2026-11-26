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
        self.assertEqual(payload["accountEmail"], "")
        self.assertEqual(payload["quota"]["convDeltas"], [])
        self.assertIsNone(payload["quota"]["convTotalDeltaPct"])

    def test_get_active_payload_includes_account_email_for_active_conversation(self):
        cid = "aaaaaaaa-0000-0000-0000-000000000099"
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            conv_dir = base / "conversations"
            log_dir = base / "log"
            conv_dir.mkdir()
            log_dir.mkdir()
            (conv_dir / f"{cid}.db").write_bytes(b"")
            (log_dir / "cli-20261126_160000.log").write_text(
                "I1126 16:00:01 server_oauth.go:226] OAuth: authenticated successfully as active@example.com,\n"
                f"I1126 16:00:05 latency_breakdown.go:168] SEND_USER_CASCADE_MESSAGE_LATENCY latency breakdown: map[cascade_id:{cid} status:OK]\n",
                encoding="utf-8",
            )
            fake_metrics = {
                "id": cid,
                "latest": {"model": "Gemini 4 Neon (Medium)", "contextTokens": 1000, "maxContextTokens": 512000},
                "totals": {"requests": 1, "totalIn": 1000, "cachedIn": 800, "uncachedIn": 200, "totalOut": 100},
                "turns": [],
                "mtimeMs": 123456,
            }
            with mock.patch.object(server, "BASE_DIR", base), \
                 mock.patch.object(server, "inspect_conversation", return_value=fake_metrics), \
                 mock.patch.object(server.QUOTA_TRACKER, "get_state", return_value={"convDeltas": [], "convTotalDeltaPct": None}):
                payload = server.get_active_payload(requested_cid=cid)
            self.assertEqual(payload["accountEmail"], "active@example.com")

    def test_get_conversation_last_account_from_cli_logs(self):
        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td)
            (log_dir / "cli-20261126_100000.log").write_text(
                "I1126 10:00:01 server_oauth.go:226] OAuth: authenticated successfully as first@example.com\n"
                "I1126 10:01:00 latency_breakdown.go:168] SEND_USER_CASCADE_MESSAGE_LATENCY latency breakdown: "
                "map[cascade_id:aaaaaaaa-0000-0000-0000-000000000001 status:OK]\n"
                "I1126 10:02:00 auth_manager.go:314] Logging out current user\n"
                "I1126 10:02:10 server_oauth.go:226] OAuth: authenticated successfully as second@example.com\n"
                "I1126 10:03:00 latency_breakdown.go:168] SEND_USER_CASCADE_MESSAGE_LATENCY latency breakdown: "
                "map[cascade_id:bbbbbbbb-0000-0000-0000-000000000002 status:OK]\n",
                encoding="utf-8",
            )
            # Newer log only views conversation aaaaaaaa under second@example.com without sending a message
            (log_dir / "cli-20261126_110000.log").write_text(
                "I1126 11:00:01 server_oauth.go:226] OAuth: authenticated successfully as second@example.com\n"
                "I1126 11:00:02 common.go:496] Resuming conversation aaaaaaaa-0000-0000-0000-000000000001\n",
                encoding="utf-8",
            )
            self.assertEqual(
                server.get_conversation_last_account("aaaaaaaa-0000-0000-0000-000000000001", log_dir=log_dir),
                "first@example.com",
            )
            self.assertEqual(
                server.get_conversation_last_account("bbbbbbbb-0000-0000-0000-000000000002", log_dir=log_dir),
                "second@example.com",
            )
            self.assertEqual(
                server.get_conversation_last_account("cccccccc-0000-0000-0000-000000000003", log_dir=log_dir),
                "",
            )

    def test_read_oauth_token_prefers_keyring_and_disambiguates_with_cli_log(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            log_dir = base / "log"
            log_dir.mkdir()
            (base / "antigravity-oauth-token").write_text(
                json.dumps({
                    "id_token_email": "ssh@example.com",
                    "token": {"access_token": "tok-ssh", "expiry": "2026-11-26T20:00:00Z"},
                }),
                encoding="utf-8",
            )
            desktop_cred = json.dumps({
                "id_token_email": "desktop@example.com",
                "token": {"access_token": "tok-desktop", "expiry": "2026-11-26T12:00:00Z"},
            })
            with mock.patch.object(server, "BASE_DIR", base), \
                 mock.patch.object(server, "_read_windows_credential", return_value=desktop_cred):
                # Without cli log, desktop keyring wins even if SSH file has a later expiry
                tok, _, email = server.read_oauth_token()
                self.assertEqual((tok, email), ("tok-desktop", "desktop@example.com"))

                # When active cli log confirms ssh@example.com, disambiguates to file token
                (log_dir / "cli-20261126_150000.log").write_text(
                    "I1126 15:00:01 server_oauth.go:226] OAuth: authenticated successfully as ssh@example.com\n",
                    encoding="utf-8",
                )
                tok2, _, email2 = server.read_oauth_token()
                self.assertEqual((tok2, email2), ("tok-ssh", "ssh@example.com"))

    def test_quota_tracker_resets_baseline_on_account_switch_without_bogus_delta(self):
        with tempfile.TemporaryDirectory() as td:
            qt = server.QuotaTracker(Path(td) / "quota.db")
            models_a = {
                "models": {
                    "gemini-4-neon": {"quotaInfo": {"remainingFraction": 0.90, "resetTime": "2099-01-01T05:00:00Z"}},
                }
            }
            models_b_lower = {
                "models": {
                    "gemini-4-neon": {"quotaInfo": {"remainingFraction": 0.40, "resetTime": "2099-01-01T06:00:00Z"}},
                }
            }
            with mock.patch.object(server, "read_oauth_token", return_value=("tok-a", "2099-01-01T00:00:00Z", "a@example.com")), \
                 mock.patch.object(qt, "_post_v1", return_value=models_a):
                self.assertTrue(qt.refresh(force=True))

            # Simulate pending accumulated requests on Account A before switching to Account B
            with qt._lock:
                qt._bucket_acc["gemini-5h"]["acc_reqs"] = 2
                qt._bucket_acc["gemini-5h"]["last_cid"] = "aaaaaaaa-0000-0000-0000-000000000001"
                qt._bucket_acc["gemini-5h"]["last_model"] = "Gemini 4 Neon (Medium)"
                qt._bucket_acc["gemini-5h"]["last_req_count"] = 2

            with mock.patch.object(server, "read_oauth_token", return_value=("tok-b", "2099-01-01T00:00:00Z", "b@example.com")), \
                 mock.patch.object(qt, "_post_v1", return_value=models_b_lower):
                self.assertTrue(qt.has_auth_change())
                self.assertTrue(qt.refresh(force=False, min_interval=999.0))

            # Must NOT record a bogus 50% cross-account delta; baseline must now be Account B's 0.40
            st = qt.get_state(cid="aaaaaaaa-0000-0000-0000-000000000001", active_bucket="gemini-5h")
            self.assertEqual(st["convDeltas"], [])
            self.assertIsNone(st["convTotalDeltaPct"])
            with qt._lock:
                self.assertAlmostEqual(qt._bucket_acc["gemini-5h"]["fraction"], 0.40, places=6)
                self.assertEqual(qt._bucket_acc["gemini-5h"]["acc_reqs"], 0)

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

    def test_get_latest_conversation_skips_child_and_invalid_projects(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "conversation_summaries.db"
            proj_dir = root / "projects"
            proj_dir.mkdir()
            (proj_dir / "11111111-0000-0000-0000-000000000001.json").write_text("{}", encoding="utf-8")
            (proj_dir / "default-cli-project.json").write_text("{}", encoding="utf-8")
            con = sqlite3.connect(str(db_path))
            try:
                con.execute(
                    "CREATE TABLE conversation_summaries ("
                    "conversation_id TEXT PRIMARY KEY, "
                    "project_id TEXT NOT NULL DEFAULT '', "
                    "last_modified_time DATETIME NOT NULL, "
                    "parent_conversation_id TEXT NOT NULL DEFAULT '')"
                )
                con.executemany(
                    "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?)",
                    [
                        ("aaaaaaaa-0000-0000-0000-000000000001", "11111111-0000-0000-0000-000000000001", "2026-11-25 06:00:00+00:00", ""),
                        ("bbbbbbbb-0000-0000-0000-000000000002", "default-cli-project", "2026-11-25 08:00:00+00:00", ""),
                        ("cccccccc-0000-0000-0000-000000000003", "22222222-0000-0000-0000-000000000002", "2026-11-25 08:30:00+00:00", ""),
                        ("dddddddd-0000-0000-0000-000000000004", "outside-of-project", "2026-11-25 09:00:00+00:00", ""),
                        ("eeeeeeee-0000-0000-0000-000000000005", "11111111-0000-0000-0000-000000000001", "2026-11-25 10:00:00+00:00", "aaaaaaaa-0000-0000-0000-000000000001"),
                    ],
                )
                con.commit()
            finally:
                con.close()
            # Skips child conversation, outside-of-project, and deleted project; picks default-cli-project
            self.assertEqual(
                self.es.get_latest_conversation(db_path, projects_dir=proj_dir),
                ("bbbbbbbb-0000-0000-0000-000000000002", "default-cli-project"),
            )
            # When NO project json exists, falls back to latest top-level conversation with empty pid
            empty_proj_dir = root / "empty_projects"
            empty_proj_dir.mkdir()
            self.assertEqual(
                self.es.get_latest_conversation(db_path, projects_dir=empty_proj_dir),
                ("dddddddd-0000-0000-0000-000000000004", ""),
            )

    def test_get_latest_conversation_rejects_invalid_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "conversation_summaries.db"
            con = sqlite3.connect(str(db_path))
            try:
                con.execute(
                    "CREATE TABLE conversation_summaries ("
                    "conversation_id TEXT PRIMARY KEY, "
                    "project_id TEXT NOT NULL DEFAULT '', "
                    "last_modified_time DATETIME NOT NULL, "
                    "parent_conversation_id TEXT NOT NULL DEFAULT '')"
                )
                con.execute(
                    "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?)",
                    ("bad; echo pwned", "", "2026-11-25 09:00:00+00:00", ""),
                )
                con.commit()
            finally:
                con.close()
            self.assertEqual(self.es.get_latest_conversation(db_path), ("", ""))

    def test_get_latest_conversation_missing_db_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(self.es.get_latest_conversation(Path(tmpdir) / "missing.db"), ("", ""))

    def test_get_active_user_email_from_token_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            self.assertEqual(self.es.get_active_user_email(base), "")
            (base / "antigravity-oauth-token").write_text("{not-json", encoding="utf-8")
            self.assertEqual(self.es.get_active_user_email(base), "")
            (base / "antigravity-oauth-token").write_text(
                json.dumps({
                    "id_token_email": "alice@example.com",
                    "token": {"access_token": "tok", "expiry": "2026-11-26T12:00:00Z"},
                }),
                encoding="utf-8",
            )
            self.assertEqual(self.es.get_active_user_email(base), "alice@example.com")

    def test_get_active_user_email_chained_auth_and_log_disambiguation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir)
            cli_dir = fake_home / ".gemini" / "antigravity-cli"
            log_dir = cli_dir / "log"
            log_dir.mkdir(parents=True)
            (cli_dir / "antigravity-oauth-token").write_text(
                json.dumps({
                    "id_token_email": "ssh@example.com",
                    "token": {"expiry": "2026-11-26T20:00:00Z"},
                }),
                encoding="utf-8",
            )
            desktop_cred = json.dumps({
                "id_token_email": "desktop@example.com",
                "token": {"expiry": "2026-11-26T12:00:00Z"},
            })
            with (
                mock.patch("pathlib.Path.home", return_value=fake_home),
                mock.patch.object(self.es, "_read_windows_credential", return_value=desktop_cred),
            ):
                # Desktop keyring wins by default (ChainedAuth order) even if SSH file has later expiry
                self.assertEqual(self.es.get_active_user_email(), "desktop@example.com")

                # Active cli log disambiguates to ssh@example.com when both exist
                (log_dir / "cli-20261126_150000.log").write_text(
                    "I1126 15:00:01 server_oauth.go:226] OAuth: authenticated successfully as ssh@example.com\n",
                    encoding="utf-8",
                )
                self.assertEqual(self.es.get_active_user_email(), "ssh@example.com")

            # On SSH where Credential Manager is unavailable, falls back to file
            with (
                mock.patch("pathlib.Path.home", return_value=fake_home),
                mock.patch.object(self.es, "_read_windows_credential", return_value=None),
            ):
                self.assertEqual(self.es.get_active_user_email(), "ssh@example.com")

    def test_build_remote_url(self):
        self.assertEqual(
            self.es.build_remote_url("7c598ac2-f03d-4548-a073-aad41497bcc6-v2"),
            self.es.REMOTE_URL,
        )
        self.assertEqual(
            self.es.build_remote_url("7c598ac2-f03d-4548-a073-aad41497bcc6-v2", authuser="alice@example.com"),
            "https://antigravity.google.com/remote?path=%2Fview%2Fremote-control&authuser=alice%40example.com",
        )
        self.assertEqual(
            self.es.build_remote_url(
                "7c598ac2-f03d-4548-a073-aad41497bcc6-v2",
                "51019dc7-497c-46f1-a9b0-3326b303304a",
                "9c8f0368-9c1b-41b4-8e87-ae309004fe68",
            ),
            "https://antigravity.google.com/r/7c598ac2-f03d-4548-a073-aad41497bcc6-v2"
            "?p=c%2F51019dc7-497c-46f1-a9b0-3326b303304a%3Fproject%3D9c8f0368-9c1b-41b4-8e87-ae309004fe68",
        )
        self.assertEqual(
            self.es.build_remote_url(
                "7c598ac2-f03d-4548-a073-aad41497bcc6-v2",
                "51019dc7-497c-46f1-a9b0-3326b303304a",
                "9c8f0368-9c1b-41b4-8e87-ae309004fe68",
                authuser="alice@example.com",
            ),
            "https://antigravity.google.com/r/7c598ac2-f03d-4548-a073-aad41497bcc6-v2"
            "?p=c%2F51019dc7-497c-46f1-a9b0-3326b303304a%3Fproject%3D9c8f0368-9c1b-41b4-8e87-ae309004fe68"
            "&authuser=alice%40example.com",
        )

    def test_wait_and_open_remote_reads_new_log_and_opens_direct_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir)
            (log_dir / "cli-20261125_160000.log").write_text(
                "[remote-control-00000000-0000-0000-0000-000000000000-v2] Connection status: Connected\n",
                encoding="utf-8",
            )
            (log_dir / "cli-20261125_163251.log").write_text(
                "I1125 16:32:52.950144 734 remote_control_v2.go:2235] "
                "[remote-control-7c598ac2-f03d-4548-a073-aad41497bcc6-v2] Connection status: Connected\n",
                encoding="utf-8",
            )
            with mock.patch("time.sleep"), mock.patch("webbrowser.open") as mock_open:
                self.es.wait_and_open_remote(
                    "cli-20261125_160000.log",
                    cid="51019dc7-497c-46f1-a9b0-3326b303304a",
                    project_id="9c8f0368-9c1b-41b4-8e87-ae309004fe68",
                    log_dir=log_dir,
                    timeout_sec=5.0,
                )
                mock_open.assert_called_once_with(
                    "https://antigravity.google.com/r/7c598ac2-f03d-4548-a073-aad41497bcc6-v2"
                    "?p=c%2F51019dc7-497c-46f1-a9b0-3326b303304a%3Fproject%3D9c8f0368-9c1b-41b4-8e87-ae309004fe68"
                )
                mock_open.reset_mock()
                self.es.wait_and_open_remote(
                    "cli-20261125_160000.log",
                    cid="51019dc7-497c-46f1-a9b0-3326b303304a",
                    project_id="9c8f0368-9c1b-41b4-8e87-ae309004fe68",
                    authuser="alice@example.com",
                    log_dir=log_dir,
                    timeout_sec=5.0,
                )
                mock_open.assert_called_once_with(
                    "https://antigravity.google.com/r/7c598ac2-f03d-4548-a073-aad41497bcc6-v2"
                    "?p=c%2F51019dc7-497c-46f1-a9b0-3326b303304a%3Fproject%3D9c8f0368-9c1b-41b4-8e87-ae309004fe68"
                    "&authuser=alice%40example.com"
                )

    def test_wait_and_open_remote_prefers_auth_email_from_matched_log(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log_dir = Path(tmpdir)
            (log_dir / "cli-20261126_150000.log").write_text(
                "I1126 15:00:01 server_oauth.go:226] OAuth: authenticated successfully as old@example.com\n"
                "I1126 15:00:02 auth_manager.go:314] Logging out current user\n"
                "I1126 15:00:03 server_oauth.go:226] OAuth: authenticated successfully as fresh@example.com\n"
                "I1126 15:00:04 remote_control_v2.go:2235] "
                "[remote-control-7c598ac2-f03d-4548-a073-aad41497bcc6-v2] Connection status: Connected\n",
                encoding="utf-8",
            )
            with mock.patch("time.sleep"), mock.patch("webbrowser.open") as mock_open:
                self.es.wait_and_open_remote(
                    "cli-20261126_140000.log",
                    cid="51019dc7-497c-46f1-a9b0-3326b303304a",
                    project_id="default-cli-project",
                    authuser="stale@example.com",
                    log_dir=log_dir,
                    timeout_sec=5.0,
                )
                mock_open.assert_called_once_with(
                    "https://antigravity.google.com/r/7c598ac2-f03d-4548-a073-aad41497bcc6-v2"
                    "?p=c%2F51019dc7-497c-46f1-a9b0-3326b303304a%3Fproject%3Ddefault-cli-project"
                    "&authuser=fresh%40example.com"
                )

    def test_main_modes_default_open_remote_and_fixed(self):
        cid = "51019dc7-497c-46f1-a9b0-3326b303304a"
        pid = "9c8f0368-9c1b-41b4-8e87-ae309004fe68"
        with (
            mock.patch.object(self.es, "is_listening", return_value=True),
            mock.patch.object(self.es, "get_latest_conversation", return_value=(cid, pid)) as mock_latest,
            mock.patch.object(self.es, "get_active_user_email", return_value=""),
            mock.patch.object(self.es, "spawn_detached") as mock_spawn,
        ):
            self.es.main([])
            mock_latest.assert_not_called()
            mock_spawn.assert_not_called()

            self.es.main(["--open-remote"])
            mock_latest.assert_called_once()
            self.assertIn("--_cid", mock_spawn.call_args.args[0])
            self.assertIn(cid, mock_spawn.call_args.args[0])
            self.assertNotIn("--_authuser", mock_spawn.call_args.args[0])

            mock_spawn.reset_mock()
            with mock.patch.object(self.es, "get_active_user_email", return_value="alice@example.com"):
                self.es.main(["--open-remote"])
                spawn_args = mock_spawn.call_args.args[0]
                self.assertIn("--_authuser", spawn_args)
                self.assertEqual(spawn_args[spawn_args.index("--_authuser") + 1], "alice@example.com")

            mock_latest.reset_mock()
            mock_spawn.reset_mock()
            with mock.patch.object(self.es, "get_active_user_email", return_value="alice@example.com"):
                self.es.main(["--open-remote", "fixed"])
                mock_latest.assert_not_called()
                spawn_args = mock_spawn.call_args.args[0]
                self.assertNotIn("--_cid", spawn_args)
                self.assertIn("--_authuser", spawn_args)
                self.assertEqual(spawn_args[spawn_args.index("--_authuser") + 1], "alice@example.com")

            mock_spawn.reset_mock()
            with (
                mock.patch.object(self.es, "get_latest_conversation", return_value=("", "")),
                mock.patch.object(self.es, "get_active_user_email", return_value="alice@example.com"),
            ):
                self.es.main(["--open-remote"])
                mock_spawn.assert_called_once()
                spawn_args = mock_spawn.call_args.args[0]
                self.assertNotIn("--_cid", spawn_args)
                self.assertIn("--_authuser", spawn_args)
                self.assertEqual(spawn_args[spawn_args.index("--_authuser") + 1], "alice@example.com")

        with mock.patch.object(self.es, "wait_and_open_remote") as mock_wait:
            self.es.main([
                "--_wait-remote", "cli-20261125_160000.log",
                "--_cid", cid,
                "--_pid", pid,
                "--_authuser", "alice@example.com",
            ])
            mock_wait.assert_called_once_with(
                "cli-20261125_160000.log",
                cid=cid,
                project_id=pid,
                authuser="alice@example.com",
            )


class TestLatestCid(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        agy_dir = str(Path(__file__).resolve().parent.parent)
        if agy_dir not in sys.path:
            sys.path.insert(0, agy_dir)
        import latest_cid as lc  # noqa: E402
        cls.lc = lc

    def test_prefers_valid_local_project_and_falls_back_to_latest_top_level(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "conversation_summaries.db"
            proj_dir = root / "projects"
            proj_dir.mkdir()
            (proj_dir / "default-cli-project.json").write_text("{}", encoding="utf-8")
            con = sqlite3.connect(str(db_path))
            try:
                con.execute(
                    "CREATE TABLE conversation_summaries ("
                    "conversation_id TEXT PRIMARY KEY, "
                    "project_id TEXT NOT NULL DEFAULT '', "
                    "last_modified_time DATETIME NOT NULL, "
                    "parent_conversation_id TEXT NOT NULL DEFAULT '')"
                )
                con.executemany(
                    "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?)",
                    [
                        ("aaaaaaaa-0000-0000-0000-000000000001", "default-cli-project", "2026-11-26 07:00:00+00:00", ""),
                        ("bbbbbbbb-0000-0000-0000-000000000002", "outside-of-project", "2026-11-26 08:00:00+00:00", ""),
                        ("cccccccc-0000-0000-0000-000000000003", "missing-project-id", "2026-11-26 09:00:00+00:00", ""),
                    ],
                )
                con.commit()
            finally:
                con.close()

            self.assertEqual(
                self.lc.get_latest_conversation_id(db_path, projects_dir=proj_dir),
                "aaaaaaaa-0000-0000-0000-000000000001",
            )
            empty_proj_dir = root / "empty_projects"
            empty_proj_dir.mkdir()
            self.assertEqual(
                self.lc.get_latest_conversation_id(db_path, projects_dir=empty_proj_dir),
                "cccccccc-0000-0000-0000-000000000003",
            )


if __name__ == "__main__":
    unittest.main()
