import importlib
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import config
import scheduler
from web import api


class ConfigBoolTest(unittest.TestCase):
    def test_local_sync_defaults_to_disabled(self):
        with patch.dict(os.environ, {}, clear=True):
            importlib.reload(config)
            self.addCleanup(importlib.reload, config)

            self.assertFalse(config.SYNC_LOCAL_DINGTALK_DATA)

    def test_env_bool_accepts_false_values(self):
        with patch.dict(os.environ, {"SYNC_LOCAL_DINGTALK_DATA": "false"}):
            self.assertTrue(hasattr(config, "_env_bool"))
            self.assertFalse(config._env_bool("SYNC_LOCAL_DINGTALK_DATA", True))

    def test_env_bool_accepts_true_values(self):
        with patch.dict(os.environ, {"SYNC_LOCAL_DINGTALK_DATA": "yes"}):
            self.assertTrue(hasattr(config, "_env_bool"))
            self.assertTrue(config._env_bool("SYNC_LOCAL_DINGTALK_DATA", False))


class SyncConfigRoutesTest(unittest.TestCase):
    def setUp(self):
        self.old_sync_setting = getattr(api.config, "SYNC_LOCAL_DINGTALK_DATA", True)
        api.config.SYNC_LOCAL_DINGTALK_DATA = False
        self.addCleanup(
            lambda: setattr(
                api.config,
                "SYNC_LOCAL_DINGTALK_DATA",
                self.old_sync_setting,
            )
        )
        self.client = TestClient(api.app)

    def test_api_config_reports_local_sync_disabled(self):
        response = self.client.get("/api/config")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("sync_local_dingtalk_data", data)
        self.assertFalse(data["sync_local_dingtalk_data"])

    def test_sync_status_reports_local_sync_disabled(self):
        response = self.client.get("/api/sync/status")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("sync_enabled", data)
        self.assertFalse(data["sync_enabled"])
        self.assertFalse(data["is_syncing"])

    def test_sync_trigger_is_rejected_when_local_sync_disabled(self):
        with patch("web.api.do_sync") as do_sync:
            response = self.client.post("/api/sync/trigger")

        self.assertEqual(response.status_code, 403)
        self.assertIn("disabled", response.json()["detail"])
        do_sync.assert_not_called()


class SchedulerConfigTest(unittest.TestCase):
    def setUp(self):
        self.old_sync_setting = getattr(scheduler.config, "SYNC_LOCAL_DINGTALK_DATA", True)
        scheduler.config.SYNC_LOCAL_DINGTALK_DATA = False
        self.addCleanup(
            lambda: setattr(
                scheduler.config,
                "SYNC_LOCAL_DINGTALK_DATA",
                self.old_sync_setting,
            )
        )

    def test_setup_scheduler_does_not_register_sync_job_when_disabled(self):
        sync_scheduler = scheduler.setup_scheduler()
        self.addCleanup(
            lambda: sync_scheduler.shutdown()
            if getattr(sync_scheduler, "running", False)
            else None
        )

        self.assertEqual(sync_scheduler.get_jobs(), [])


class RealtimeDatabaseUnavailableRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        self.old_db_path = api.config.DECRYPTED_DB_PATH
        api.config.DECRYPTED_DB_PATH = os.path.join(self.tmp.name, "empty.db")
        with open(api.config.DECRYPTED_DB_PATH, "wb"):
            pass
        self.addCleanup(
            lambda: setattr(api.config, "DECRYPTED_DB_PATH", self.old_db_path)
        )

        self.client = TestClient(api.app, raise_server_exceptions=False)

    def test_conversations_returns_empty_when_decrypted_database_is_not_ready(self):
        response = self.client.get("/api/conversations")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"total": 0, "conversations": [], "database_ready": False},
        )

    def test_messages_returns_empty_when_decrypted_database_is_not_ready(self):
        response = self.client.get("/api/conversations/cid-1/messages")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"total": 0, "messages": [], "database_ready": False},
        )

    def test_search_returns_empty_when_decrypted_database_is_not_ready(self):
        response = self.client.get("/api/search", params={"q": "hello"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "query": "hello",
                "total": 0,
                "messages": [],
                "database_ready": False,
            },
        )

    def test_stats_returns_zeroes_when_decrypted_database_is_not_ready(self):
        response = self.client.get("/api/stats")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "total_conversations": 0,
                "total_messages": 0,
                "single_chats": 0,
                "group_chats": 0,
                "total_users": 0,
                "database_ready": False,
            },
        )


if __name__ == "__main__":
    unittest.main()
