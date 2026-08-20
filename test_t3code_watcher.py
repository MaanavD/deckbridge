import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from t3code_watcher import (
    T3CodeWatcher, annotate_remote_agents, merge_agent_sides,
    snapshot_agents, thread_status,
)


class T3CodeWatcherTests(unittest.TestCase):
    def test_authoritative_status_mapping(self):
        self.assertEqual(thread_status({"hasPendingUserInput": True}), "blocked")
        self.assertEqual(thread_status({"hasPendingApprovals": True}), "blocked")
        self.assertEqual(thread_status({"session": {"status": "running"}}), "working")
        self.assertEqual(thread_status({"latestTurn": {"state": "completed"}}), "done")
        self.assertEqual(thread_status({"session": {"status": "ready"}}), "idle")

    def test_snapshot_omits_settled_threads(self):
        payload = {
            "threads": [
                {"id": "open-1", "title": "Still open"},
                {
                    "id": "settled-1", "title": "Put away",
                    "settledOverride": "settled",
                    "settledAt": "2026-08-19T23:50:36.883Z",
                },
                {
                    "id": "auto-settled", "title": "Aged out",
                    "settledAt": "2026-08-19T23:50:36.883Z",
                },
                {
                    "id": "unsettled-1", "title": "Brought back",
                    "settledOverride": "unsettled",
                    "settledAt": "2026-08-19T23:50:36.883Z",
                },
            ],
        }
        names = [agent["name"] for agent in snapshot_agents(payload, "http://127.0.0.1:3773", "env-1")]
        self.assertEqual(names, ["Still open", "Brought back"])

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
        self.assertEqual(agents[0]["environment_id"], "env-1")
        self.assertEqual(agents[0]["url"], "t3code://app/#/env-1/thread-1")
        self.assertEqual(agents[0]["web_url"], "http://127.0.0.1:3773/env-1/thread-1")

    def test_remote_threads_keep_the_local_app_route_and_drop_loopback_web_urls(self):
        agents = annotate_remote_agents(
            snapshot_agents({
                "threads": [{
                    "id": "remote-1", "title": "Hermes work",
                    "session": {"status": "running"},
                }],
            }, "http://127.0.0.1:3773", "env-remote"),
            "hermes",
            "maanav-hermes",
        )
        self.assertEqual(agents[0]["ssh_host"], "hermes")
        self.assertEqual(agents[0]["environment_label"], "maanav-hermes")
        self.assertEqual(agents[0]["url"], "t3code://app/#/env-remote/remote-1")
        self.assertEqual(agents[0]["web_url"], "")
        self.assertEqual(agents[0]["status"], "working")

    def test_merge_keeps_the_last_good_side_when_one_server_is_down(self):
        local = [{"name": "local", "ssh_host": ""}]
        remote = [{"name": "remote", "ssh_host": "hermes"}]
        self.assertEqual(
            merge_agent_sides(local, remote, None, [{"name": "new", "ssh_host": "hermes"}]),
            local + [{"name": "new", "ssh_host": "hermes"}],
        )
        self.assertIsNone(merge_agent_sides(local, remote, None, None))

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

    def test_poll_failures_never_launch_or_activate_t3(self):
        opens = []

        def fake_open(*args, **kwargs):
            opens.append(args)
            raise AssertionError("poll recovery must not launch T3")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            watcher = T3CodeWatcher(
                root / "missing-runtime.json", root / "token", root / "state.json",
                interval=0.01, opener=fake_open,
            )
            watcher.poll_local = lambda: (_ for _ in ()).throw(ValueError("401"))
            with mock.patch.object(watcher, "interval", 0.01), mock.patch(
                "t3code_watcher.time.sleep", side_effect=SystemExit
            ):
                with self.assertRaises(SystemExit):
                    watcher.run()
        self.assertEqual(opens, [])

    def test_ssh_poll_is_noninteractive_and_merges_remote_threads(self):
        dump = {
            "payload": {
                "threads": [{
                    "id": "remote-1", "title": "Remote agent",
                    "session": {"status": "running"},
                }],
            },
            "origin": "http://127.0.0.1:3773",
            "environment_id": "env-remote",
            "hostname": "maanav-hermes",
        }

        def fake_open(argv, **kwargs):
            self.assertEqual(argv[0], "ssh")
            self.assertIn("-oBatchMode=yes", argv)
            self.assertIn("hermes", argv)
            self.assertEqual(argv[-2:], ["python3", "-"])
            self.assertIn("server-runtime.json", kwargs.get("input") or "")
            completed = mock.Mock()
            completed.returncode = 0
            completed.stdout = json.dumps(dump)
            completed.stderr = ""
            return completed

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, token, state = root / "runtime.json", root / "token", root / "state.json"
            runtime.write_text(json.dumps({
                "origin": "http://127.0.0.1:3773", "environmentId": "env-local",
            }))
            token.write_text("secret")
            os.chmod(token, 0o600)
            watcher = T3CodeWatcher(
                runtime, token, state, ssh_host="hermes", opener=fake_open,
            )
            watcher.poll_local = lambda: [{
                "name": "Local thread", "session_id": "local-1", "ssh_host": "",
            }]
            agents = watcher.poll_once()
        names = [agent["name"] for agent in agents]
        self.assertIn("Local thread", names)
        self.assertIn("Remote agent", names)
        remote = next(agent for agent in agents if agent["name"] == "Remote agent")
        self.assertEqual(remote["ssh_host"], "hermes")
        self.assertEqual(remote["environment_id"], "env-remote")
        self.assertEqual(remote["environment_label"], "maanav-hermes")


if __name__ == "__main__":
    unittest.main()
