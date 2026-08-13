import json
import os
import tempfile
import unittest
from pathlib import Path

from t3code_watcher import T3CodeWatcher, snapshot_agents, thread_status


class T3CodeWatcherTests(unittest.TestCase):
    def test_authoritative_status_mapping(self):
        self.assertEqual(thread_status({"hasPendingUserInput": True}), "blocked")
        self.assertEqual(thread_status({"hasPendingApprovals": True}), "blocked")
        self.assertEqual(thread_status({"session": {"status": "running"}}), "working")
        self.assertEqual(thread_status({"latestTurn": {"state": "completed"}}), "done")
        self.assertEqual(thread_status({"session": {"status": "ready"}}), "idle")

    def test_snapshot_uses_title_provider_identity_and_exact_routes(self):
        payload = {
            "projects": [{"id": "p1", "workspaceRoot": "/repo"}],
            "threads": [{
                "id": "thread-1", "projectId": "p1", "title": "Useful title",
                "modelSelection": {"instanceId": "claudeAgent"},
                "latestTurn": {"state": "completed", "completedAt": "2026-08-13T12:00:00Z"},
            }],
        }
        agents = snapshot_agents(payload, "http://127.0.0.1:3773", "env-1")
        self.assertEqual(agents[0]["name"], "Useful title")
        self.assertEqual(agents[0]["source"], "t3code-claude")
        self.assertEqual(agents[0]["session_id"], "thread-1")
        self.assertEqual(agents[0]["url"], "t3code://app/#/env-1/thread-1")
        self.assertEqual(agents[0]["web_url"], "http://127.0.0.1:3773/env-1/thread-1")

    def test_endpoint_rejects_non_loopback_and_credential_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, token = root / "runtime.json", root / "token"
            state = root / "state.json"
            runtime.write_text(json.dumps({"origin": "https://example.com"}))
            token.write_text("secret")
            os.chmod(token, 0o600)
            watcher = T3CodeWatcher(runtime, token, state)
            with self.assertRaises(ValueError):
                watcher.endpoint()
            runtime.write_text(json.dumps({"origin": "http://127.0.0.1:3773", "environmentId": "e"}))
            self.assertEqual(watcher.endpoint()[1], "e")
            os.chmod(token, 0o644)
            with self.assertRaises(PermissionError):
                watcher.credential()


if __name__ == "__main__":
    unittest.main()
