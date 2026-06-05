import json
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from web import api


class ExportViewerRoutesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_export_dir = api.config.EXPORT_DIR
        api.config.EXPORT_DIR = self.tmp.name
        self.addCleanup(lambda: setattr(api.config, "EXPORT_DIR", self.old_export_dir))

        export_dir = os.path.join(self.tmp.name, "export_sample")
        images_dir = os.path.join(export_dir, "images")
        os.makedirs(images_dir)
        with open(os.path.join(export_dir, "export.json"), "w", encoding="utf-8") as f:
            json.dump(
                {
                    "export_type": "selected",
                    "total_conversations": 1,
                    "conversations": [
                        {
                            "conversation_id": "cid-1",
                            "title": "测试会话",
                            "type": "group",
                            "messages": [
                                {
                                    "message_id": 1,
                                    "sender_name": "Alice",
                                    "content": "[图片: images/a.png]",
                                    "created_at": 1780644000000,
                                    "created_at_str": "2026-06-05 10:00:00",
                                    "image_export_paths": ["images/a.png"],
                                },
                                {
                                    "message_id": 2,
                                    "sender_name": "Bob",
                                    "content": "第二条",
                                    "created_at": 1780644060000,
                                    "created_at_str": "2026-06-05 10:01:00",
                                }
                            ],
                        },
                        {
                            "conversation_id": "504456221:2616493571",
                            "title": "",
                            "type": "single",
                            "messages": [
                                {
                                    "message_id": 3,
                                    "sender_id": 2616493571,
                                    "sender_name": "何梓敏",
                                    "content": "好的",
                                    "created_at": 1780644120000,
                                    "created_at_str": "2026-06-05 10:02:00",
                                },
                                {
                                    "message_id": 4,
                                    "sender_id": 504456221,
                                    "sender_name": "袁红",
                                    "content": "好",
                                    "created_at": 1780644180000,
                                    "created_at_str": "2026-06-05 10:03:00",
                                },
                            ],
                        }
                    ],
                },
                f,
                ensure_ascii=False,
            )
        with open(os.path.join(images_dir, "a.png"), "wb") as f:
            f.write(b"png")

        self.client = TestClient(api.app)

    def test_reads_export_json_from_directory_export(self):
        response = self.client.get("/api/export-viewer/export_sample")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["conversations"][0]["title"], "测试会话")

    def test_returns_export_summary_without_message_bodies(self):
        response = self.client.get("/api/export-viewer/export_sample/summary")

        self.assertEqual(response.status_code, 200)
        conv = next(
            c for c in response.json()["conversations"]
            if c["conversation_id"] == "cid-1"
        )
        self.assertEqual(conv["conversation_id"], "cid-1")
        self.assertEqual(conv["message_count"], 2)
        self.assertNotIn("messages", conv)

    def test_export_summary_is_sorted_by_last_message_time_descending(self):
        response = self.client.get("/api/export-viewer/export_sample/summary")

        self.assertEqual(response.status_code, 200)
        conversations = response.json()["conversations"]
        self.assertEqual(
            [c["conversation_id"] for c in conversations],
            ["504456221:2616493571", "cid-1"],
        )

    def test_single_conversation_summary_uses_sender_name_for_display_title(self):
        response = self.client.get("/api/export-viewer/export_sample/summary")

        self.assertEqual(response.status_code, 200)
        conv = next(
            c for c in response.json()["conversations"]
            if c["conversation_id"] == "504456221:2616493571"
        )
        self.assertEqual(conv["conversation_id"], "504456221:2616493571")
        self.assertEqual(conv["display_title"], "袁红")

    def test_returns_paginated_messages_for_one_conversation(self):
        response = self.client.get(
            "/api/export-viewer/export_sample/messages",
            params={"cid": "cid-1", "limit": 1, "offset": 1},
        )

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["total"], 2)
        self.assertEqual(len(data["messages"]), 1)
        self.assertEqual(data["messages"][0]["message_id"], 2)

    def test_serves_files_from_export_directory(self):
        response = self.client.get("/api/export-viewer/export_sample/files/images/a.png")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"png")

    def test_rejects_paths_outside_export_directory(self):
        response = self.client.get("/api/export-viewer/export_sample/files/%2E%2E/export.json")

        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
