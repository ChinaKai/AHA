from __future__ import annotations

import contextlib
import json
from pathlib import Path
import tempfile
import unittest

from aha_cli.web.game_routes import game_route_response


def response_body(response: bytes) -> bytes:
    return response.split(b"\r\n\r\n", 1)[1]


def write_config(root: Path, workspace: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps({"webgame_workspace": workspace}), encoding="utf-8")


class GameRouteTests(unittest.TestCase):
    def test_catalog_scans_configured_webgame_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "my_project" / "aha" / ".aha"
            workspace = Path(tmp) / "my_project" / "aha_game"
            write_config(root, "../aha_game")
            (workspace / "alpha").mkdir(parents=True)
            (workspace / "beta").mkdir()
            (workspace / ".hidden").mkdir()
            (workspace / "alpha" / "index.html").write_text("<main>alpha</main>", encoding="utf-8")
            (workspace / "alpha" / "game.json").write_text(
                json.dumps({"title": "Alpha Game", "description": "First game"}),
                encoding="utf-8",
            )
            (workspace / "beta" / "index.html").write_text("<main>beta</main>", encoding="utf-8")
            (workspace / ".hidden" / "index.html").write_text("<main>hidden</main>", encoding="utf-8")

            catalog = game_route_response(root, "run-001", "GET", "/api/games")

        self.assertIsNotNone(catalog)
        payload = json.loads(response_body(catalog or b""))
        self.assertEqual([game["id"] for game in payload["games"]], ["alpha", "beta"])
        self.assertEqual(payload["games"][0]["title"], "Alpha Game")
        self.assertEqual(payload["games"][0]["description"], "First game")
        self.assertEqual(payload["games"][0]["href"], "/games/alpha/")
        self.assertTrue(payload["games"][0]["available"])

    def test_game_root_serves_project_owned_index_and_relative_assets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            workspace = Path(tmp) / "aha_game"
            project = workspace / "newgame"
            outside = Path(tmp) / "outside.txt"
            write_config(root, str(workspace))
            (project / "assets" / "levels").mkdir(parents=True)
            (project / "assets" / ".private").mkdir(parents=True)
            (project / "index.html").write_text(
                '<!doctype html><link rel="stylesheet" href="theme.css"><main>newgame</main>',
                encoding="utf-8",
            )
            (project / "theme.css").write_text("main { color: #fff; }\n", encoding="utf-8")
            (project / "assets" / "levels" / "level-2.json").write_text('{"level":2}\n', encoding="utf-8")
            (project / ".env").write_text("SECRET=1\n", encoding="utf-8")
            (project / "assets" / ".private" / "key.txt").write_text("secret\n", encoding="utf-8")
            outside.write_text("outside\n", encoding="utf-8")
            with contextlib.suppress(OSError, NotImplementedError):
                (project / "outside-link.txt").symlink_to(outside)

            html = game_route_response(root, "run-001", "GET", "/games/newgame/")
            css = game_route_response(root, "run-001", "GET", "/games/newgame/theme.css")
            nested = game_route_response(root, "run-001", "GET", "/games/newgame/assets/assets/levels/level-2.json")
            blocked = game_route_response(root, "run-001", "GET", "/games/newgame/assets/../config.json")
            hidden_file = game_route_response(root, "run-001", "GET", "/games/newgame/assets/.env")
            hidden_dir = game_route_response(root, "run-001", "GET", "/games/newgame/assets/assets/.private/key.txt")
            symlink = game_route_response(root, "run-001", "GET", "/games/newgame/assets/outside-link.txt")

        self.assertIsNotNone(html)
        self.assertIn(b"newgame", response_body(html or b""))
        self.assertIsNotNone(css)
        self.assertIn(b"color", response_body(css or b""))
        self.assertIsNotNone(nested)
        self.assertIn(b'"level":2', response_body(nested or b""))
        self.assertIsNotNone(blocked)
        self.assertTrue((blocked or b"").startswith(b"HTTP/1.1 404 Not Found"))
        self.assertIsNotNone(hidden_file)
        self.assertTrue((hidden_file or b"").startswith(b"HTTP/1.1 404 Not Found"))
        self.assertIsNotNone(hidden_dir)
        self.assertTrue((hidden_dir or b"").startswith(b"HTTP/1.1 404 Not Found"))
        self.assertIsNotNone(symlink)
        self.assertTrue((symlink or b"").startswith(b"HTTP/1.1 404 Not Found"))

    def test_game_json_can_select_project_owned_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            workspace = Path(tmp) / "aha_game"
            project = workspace / "configured"
            write_config(root, str(workspace))
            (project / "web").mkdir(parents=True)
            (project / "game.json").write_text(json.dumps({"webgame_entry": "web/play.html"}), encoding="utf-8")
            (project / "web" / "play.html").write_text("<main>configured-entry</main>", encoding="utf-8")
            (project / "index.html").write_text("<main>default-entry</main>", encoding="utf-8")

            html = game_route_response(root, "run-001", "GET", "/games/configured/")

        self.assertIsNotNone(html)
        self.assertIn(b"configured-entry", response_body(html or b""))
        self.assertNotIn(b"default-entry", response_body(html or b""))

    def test_game_routes_require_webgame_workspace_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".aha"
            project = Path(tmp) / "game"
            (project / "demo").mkdir(parents=True)
            (project / "demo" / "index.html").write_text("<main>demo</main>", encoding="utf-8")

            catalog = game_route_response(root, "run-001", "GET", "/api/games")
            html = game_route_response(root, "run-001", "GET", "/games/demo/")

        self.assertIsNotNone(catalog)
        self.assertEqual(json.loads(response_body(catalog or b""))["games"], [])
        self.assertIsNotNone(html)
        self.assertTrue((html or b"").startswith(b"HTTP/1.1 404 Not Found"))

    def test_game_resources_are_not_embedded_in_aha_static_files(self) -> None:
        static_games = Path(__file__).resolve().parents[1] / "src" / "aha_cli" / "web" / "static" / "games"
        self.assertFalse(static_games.exists() and any(static_games.rglob("*")))
