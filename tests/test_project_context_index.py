from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from aha_cli.services.project_context_index import (
    ProjectContextExtractor,
    _record_path_hint_score,
    _record_score,
    build_project_context_index,
    discover_project_repos,
    format_project_context_reference,
    load_project_context_index,
    project_context_index_status,
    query_project_context_index,
    query_project_context_index_cache,
    run_project_context_extractors,
)
from aha_cli.store.knowledge import init_knowledge_base, write_entry


class ProjectContextIndexTests(unittest.TestCase):
    def test_builds_embedded_style_index_and_queries_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            workspace = Path(tmp) / "repo"
            (workspace / "drivers" / "net").mkdir(parents=True)
            (workspace / "drivers" / "net" / "foo.c").write_text("int foo_probe(void) { return 0; }\n", encoding="utf-8")
            (workspace / "drivers" / "net" / "Kconfig").write_text("config FOO_NET\n\tbool \"Foo net\"\n", encoding="utf-8")
            (workspace / "arch" / "arm" / "boot" / "dts").mkdir(parents=True)
            (workspace / "arch" / "arm" / "boot" / "dts" / "board.dts").write_text(
                "/dts-v1/;\n/ { compatible = \"vendor,board\"; };\n",
                encoding="utf-8",
            )
            (workspace / "Makefile").write_text("obj-y += drivers/\n", encoding="utf-8")

            result = build_project_context_index(root, workspace)
            status = project_context_index_status(root, workspace)
            query = query_project_context_index(result["index"], "foo net")
            index_exists = Path(result["paths"]["index"]).exists()
            summary_exists = Path(result["paths"]["summary"]).exists()

        self.assertEqual(result["status"], "fresh")
        self.assertEqual(status["status"], "fresh")
        self.assertEqual(result["index"]["limits"]["source"], "filesystem")
        self.assertIn("c", result["index"]["flavors"])
        self.assertIn("kconfig", result["index"]["flavors"])
        self.assertIn("dts", result["index"]["flavors"])
        self.assertIn("extractors", result["index"])
        self.assertIn("errors", result["index"])
        self.assertTrue(index_exists)
        self.assertTrue(summary_exists)
        self.assertIn("drivers/net/foo.c", [item["path"] for item in result["index"]["files"]])
        self.assertIn("foo_probe", [item["name"] for item in result["index"]["symbols"]])
        self.assertIn("FOO_NET", [item["name"] for item in result["index"]["configs"]])
        self.assertIn("obj-y", [item["name"] for item in result["index"]["build"]])
        self.assertIn("vendor,board", [item["name"] for item in result["index"]["device_tree"]])
        self.assertIn("drivers/net/foo.c", [item["path"] for item in query["files"]])
        self.assertIn("foo_probe", [item["name"] for item in query["symbols"]])
        self.assertIn("FOO_NET", [item["name"] for item in query["configs"]])
        self.assertGreaterEqual(query["section_totals"]["symbols"], 1)

    def test_writes_sharded_manifest_and_loads_logical_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            (workspace / "foo.c").write_text("int foo_probe(void) { return 0; }\n", encoding="utf-8")

            result = build_project_context_index(root, workspace)
            index_path = Path(result["paths"]["index"])
            manifest = json.loads(index_path.read_text(encoding="utf-8"))
            file_shards = manifest["storage"]["sections"]["files"]["shards"]
            shard_exists = (index_path.parent / file_shards[0]["path"]).exists()
            manifest_only = load_project_context_index(root, workspace)
            loaded = load_project_context_index(root, workspace, hydrate=True)
            cache_query = query_project_context_index_cache(root, workspace, "foo_probe")
            status = project_context_index_status(root, workspace, verify_worktree=False)

        self.assertEqual(manifest["storage"]["format"], "sharded-jsonl")
        self.assertNotIn("files", manifest)
        self.assertTrue(shard_exists)
        self.assertIsNotNone(manifest_only)
        self.assertNotIn("files", manifest_only)
        self.assertIsNotNone(loaded)
        self.assertIn("foo.c", [item["path"] for item in loaded["files"]])
        self.assertIn("foo_probe", [item["name"] for item in loaded["symbols"]])
        self.assertIsNotNone(cache_query)
        self.assertIn("foo_probe", [item["name"] for item in cache_query["symbols"]])
        self.assertEqual(status["counts"]["files"], 1)

    def test_prioritizes_platform_build_records_before_buildroot_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            workspace = Path(tmp) / "repo"
            (workspace / "buildroot-dist" / "package" / "bulk").mkdir(parents=True)
            (workspace / "platform" / "sigmastar" / "package" / "wyze_app").mkdir(parents=True)
            (workspace / "platform" / "sigmastar" / "Config.in").write_text(
                "config BR2_PLATFORM_NAME\n\tstring \"platform\"\n",
                encoding="utf-8",
            )
            (workspace / "platform" / "sigmastar" / "package" / "wyze_app" / "sigmastar_wyze_app.mk").write_text(
                "SIGMASTAR_WYZE_APP_VERSION = 0.0.1\n"
                "SIGMASTAR_WYZE_APP_DEPENDENCIES = fw_localsdk\n",
                encoding="utf-8",
            )
            (workspace / "buildroot-dist" / "package" / "bulk" / "bulk.mk").write_text(
                "\n".join(f"BULK_PACKAGE_{index}_VERSION = {index}" for index in range(20)),
                encoding="utf-8",
            )
            config = {"knowledge": {"project_context_index": {"max_records_per_extractor": 2}}}

            result = build_project_context_index(root, workspace, config=config)
            query = query_project_context_index(result["index"], "sigmastar_wyze_app")

        self.assertEqual(len(result["index"]["build"]), 2)
        self.assertTrue(any(item["path"].startswith("platform/") for item in result["index"]["build"]))
        self.assertIn("SIGMASTAR_WYZE_APP_VERSION", [item["name"] for item in query["build"]])

    def test_config_assignments_from_defconfig_are_indexed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            workspace = Path(tmp) / "repo"
            (workspace / "platform" / "sigmastar" / "configs").mkdir(parents=True)
            (workspace / "platform" / "sigmastar" / "configs" / "ss306_vega_defconfig").write_text(
                "BR2_PACKAGE_SIGMASTAR_WYZE_APP=y\n"
                "# BR2_PACKAGE_UNUSED is not set\n",
                encoding="utf-8",
            )

            result = build_project_context_index(root, workspace)
            query = query_project_context_index(result["index"], "ss306 SIGMASTAR_WYZE_APP")

        self.assertIn("config-value", [item["kind"] for item in result["index"]["configs"]])
        self.assertIn("BR2_PACKAGE_SIGMASTAR_WYZE_APP", [item["name"] for item in query["configs"]])

    def test_buildroot_package_records_group_config_build_and_defconfig(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            workspace = Path(tmp) / "repo"
            package_dir = workspace / "platform" / "sigmastar" / "package" / "wyze_app" / "sigmastar_wyze_app"
            package_dir.mkdir(parents=True)
            (package_dir / "Config.in").write_text(
                "config BR2_PACKAGE_SIGMASTAR_WYZE_APP\n"
                "\tbool \"sigmastar app\"\n",
                encoding="utf-8",
            )
            (package_dir / "sigmastar_wyze_app.mk").write_text(
                "SIGMASTAR_WYZE_APP_VERSION = 0.0.1\n"
                "SIGMASTAR_WYZE_APP_DEPENDENCIES = fw_localsdk \\\n",
                encoding="utf-8",
            )
            (workspace / "platform" / "sigmastar" / "configs").mkdir(parents=True)
            (workspace / "platform" / "sigmastar" / "configs" / "ss306_defconfig").write_text(
                "BR2_PACKAGE_SIGMASTAR_WYZE_APP=y\n",
                encoding="utf-8",
            )
            (workspace / "app_source" / "wyze_app").mkdir(parents=True)
            (workspace / "app_source" / "wyze_app" / "main.c").write_text(
                "int wyze_main(void) { return 0; }\n",
                encoding="utf-8",
            )

            result = build_project_context_index(root, workspace)
            query = query_project_context_index_cache(root, workspace, "sigmastar_wyze_app")
            fuzzy_query = query_project_context_index_cache(root, workspace, "sigmastar wyze")

        self.assertIn("buildroot-external", result["index"]["profiles"])
        self.assertIn("embedded-c-app", result["index"]["profiles"])
        self.assertIn("sigmastar_wyze_app", [item["name"] for item in result["index"]["packages"]])
        package = result["index"]["packages"][0]
        self.assertEqual(package["config_path"], "platform/sigmastar/package/wyze_app/sigmastar_wyze_app/Config.in")
        self.assertEqual(package["mk_path"], "platform/sigmastar/package/wyze_app/sigmastar_wyze_app/sigmastar_wyze_app.mk")
        self.assertIn("BR2_PACKAGE_SIGMASTAR_WYZE_APP", package["config_symbols"])
        self.assertIn("fw_localsdk", package["dependencies"])
        self.assertNotIn("\\", package["dependencies"])
        self.assertTrue(package["enabled_in"])
        self.assertIsNotNone(query)
        self.assertIn("sigmastar_wyze_app", [item["name"] for item in query["packages"]])
        self.assertIsNotNone(fuzzy_query)
        self.assertIn("sigmastar_wyze_app", [item["name"] for item in fuzzy_query["packages"]])
        reference = format_project_context_reference(query, budget_chars=700)
        self.assertLessEqual(len(reference), 700)
        self.assertIn("Project map reference", reference)
        self.assertIn("sigmastar_wyze_app", reference)
        self.assertIn("Read exact files by path", reference)

    def test_cache_query_expands_natural_language_through_navigation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            workspace = Path(tmp) / "repo"
            service_dir = workspace / "src" / "app" / "services"
            service_dir.mkdir(parents=True)
            (service_dir / "weixin.py").write_text(
                "def refresh_access_token():\n    return 'ok'\n",
                encoding="utf-8",
            )
            (service_dir / "weixin_notifications.py").write_text(
                "def send_weixin_notification():\n    return True\n",
                encoding="utf-8",
            )
            (service_dir / "prompt_context.py").write_text(
                "def context_token_budget():\n    return 0\n",
                encoding="utf-8",
            )

            result = build_project_context_index(root, workspace)
            raw = query_project_context_index(result["index"], "通知失效")
            project_key = result["index"]["project_key"]
            init_knowledge_base(root, {})
            write_entry(
                root,
                config={},
                scope="project",
                kind="navigation",
                project_key_value=project_key,
                title="项目导航",
                slug="index",
                body="## 模块索引\n- [微信通知](modules/weixin.md)\n",
            )
            write_entry(
                root,
                config={},
                scope="project",
                kind="navigation",
                project_key_value=project_key,
                title="微信通知模块",
                slug="modules/weixin",
                body=(
                    "处理微信通知、通知失效和 access token 刷新。\n"
                    "关键文件 `src/app/services/weixin.py` "
                    "`src/app/services/weixin_notifications.py` "
                    "`src/app/services/deleted.py`。\n"
                    "入口符号 `WeixinClient.refresh_token` 不是文件路径。\n"
                ),
            )

            query = query_project_context_index_cache(root, workspace, "通知失效", config={})
            reference = format_project_context_reference(query or {}, budget_chars=700)

        self.assertEqual(raw["total_matches"], 0)
        self.assertIsNotNone(query)
        self.assertTrue(query["resolution"]["used_navigation"])
        self.assertIn("modules/weixin", [item["slug"] for item in query["resolution"]["nav_routes"]])
        self.assertIn("src/app/services/weixin.py", query["resolution"]["path_hints"])
        self.assertNotIn("src/app/services/deleted.py", query["resolution"]["path_hints"])
        self.assertIn("src/app/services/deleted.py", query["resolution"]["stale_path_hints"])
        self.assertNotIn("WeixinClient.refresh_token", query["resolution"]["stale_path_hints"])
        self.assertIn("src/app/services/weixin.py", [item["path"] for item in query["files"]])
        self.assertIn("nav route: modules/weixin", reference)

    def test_stale_path_hints_downrank_records(self) -> None:
        active = {"path": "src/app/services/weixin.py", "kind": "python", "name": "weixin"}
        stale = {"path": "src/app/services/deleted.py", "kind": "python", "name": "deleted"}
        hints = {
            "path_hints": ["src/app/services/weixin.py"],
            "stale_path_hints": ["src/app/services/deleted.py"],
        }

        self.assertGreater(_record_path_hint_score(active, hints), 0)
        self.assertLess(_record_path_hint_score(stale, hints), 0)

    def test_exact_symbol_match_promotes_owning_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            workspace = Path(tmp) / "repo"
            (workspace / "src" / "camera").mkdir(parents=True)
            (workspace / "src" / "camera" / "control.c").write_text(
                "int nightvision_get_mode(void) { return 0; }\n",
                encoding="utf-8",
            )
            (workspace / "tests" / "app" / "function_protocol").mkdir(parents=True)
            (workspace / "tests" / "app" / "function_protocol" / "GetFunctionList.c").write_text(
                "int unrelated_protocol_function(void) { return 0; }\n",
                encoding="utf-8",
            )
            build_project_context_index(root, workspace)

            query = query_project_context_index_cache(
                root,
                workspace,
                "nightvision_get_mode app function protocol",
                max_files=1,
            )

        self.assertIsNotNone(query)
        self.assertIn("nightvision_get_mode", [item["name"] for item in query["symbols"]])
        self.assertEqual([item["path"] for item in query["files"]], ["src/camera/control.c"])

    def test_weak_navigation_match_does_not_expand_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            workspace = Path(tmp) / "repo"
            workspace.mkdir(parents=True)
            (workspace / "map_service.py").write_text("def map_status():\n    return None\n", encoding="utf-8")

            result = build_project_context_index(root, workspace)
            project_key = result["index"]["project_key"]
            init_knowledge_base(root, {})
            write_entry(
                root,
                config={},
                scope="project",
                kind="navigation",
                project_key_value=project_key,
                title="无关模块",
                slug="index",
                body="## 模块索引\n- [无关模块](modules/unrelated.md)\n",
            )
            write_entry(
                root,
                config={},
                scope="project",
                kind="navigation",
                project_key_value=project_key,
                title="无关模块",
                slug="modules/unrelated",
                body="这个模块正文里偶然提到 map 一次，但没有相关文件。",
            )

            query = query_project_context_index_cache(root, workspace, "map 搜索自然语言", config={})

        self.assertIsNotNone(query)
        self.assertNotIn("resolution", query)

    def test_query_scoring_weights_names_and_paths_over_weak_values(self) -> None:
        terms = ["snapshot"]
        weak_download_record = {
            "name": "agent-proxy",
            "path": "platform/sigmastar/package/agent-proxy/agent-proxy.mk",
            "variables": [
                {
                    "name": "AGENT_PROXY_SITE",
                    "value": "https://git.kernel.org/pub/scm/utils/kernel/kgdb/agent-proxy.git/snapshot",
                }
            ],
        }
        path_record = {
            "name": "jpeg_snapshot",
            "path": "buildroot-dist/package/jpeg_snapshot/jpeg_snapshot.mk",
        }

        self.assertGreater(_record_score(path_record, terms), _record_score(weak_download_record, terms) * 3)

    def test_reference_without_matches_is_empty(self) -> None:
        reference = format_project_context_reference(
            {
                "query": "missing",
                "total_matches": 0,
                "files": [],
                "packages": [],
                "symbols": [],
                "configs": [],
                "build": [],
            }
        )

        self.assertEqual(reference, "")
        self.assertNotIn("Read exact files by path", reference)

    def test_git_submodule_files_are_scanned_as_separate_repos_without_untracked_outputs(self) -> None:
        def init_repo(path: Path) -> None:
            path.mkdir(parents=True)
            subprocess.run(["git", "init", "-q"], cwd=path, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=path, check=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True)

        def commit_all(path: Path, message: str) -> None:
            subprocess.run(["git", "add", "."], cwd=path, check=True)
            subprocess.run(["git", "commit", "-q", "-m", message], cwd=path, check=True)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            dependency = Path(tmp) / "dependency"
            workspace = Path(tmp) / "repo"
            init_repo(dependency)
            (dependency / "lib.c").write_text("int dep_probe(void) { return 0; }\n", encoding="utf-8")
            commit_all(dependency, "initial dependency")

            init_repo(workspace)
            (workspace / "README.md").write_text("root\n", encoding="utf-8")
            commit_all(workspace, "initial root")
            subprocess.run(
                [
                    "git",
                    "-c",
                    "protocol.file.allow=always",
                    "submodule",
                    "add",
                    str(dependency),
                    "app_source/dependency",
                ],
                cwd=workspace,
                check=True,
                stdout=subprocess.DEVNULL,
            )
            commit_all(workspace, "add submodule")
            (workspace / "build.log").write_text("temporary output\n", encoding="utf-8")

            result = build_project_context_index(root, workspace)
            repos = discover_project_repos(workspace)
            loaded = load_project_context_index(root, workspace, hydrate=True)
            cache_query = query_project_context_index_cache(root, workspace, "dep_probe")

        self.assertTrue(any(repo.get("role") == "submodule" for repo in repos))
        self.assertIn("app_source/dependency/lib.c", [item["path"] for item in result["index"]["files"]])
        self.assertNotIn("build.log", [item["path"] for item in result["index"]["files"]])
        self.assertIsNotNone(loaded)
        self.assertIn("dep_probe", [item["name"] for item in loaded["symbols"]])
        self.assertIsNotNone(cache_query)
        self.assertIn("dep_probe", [item["name"] for item in cache_query["symbols"]])

    def test_extractor_registry_returns_empty_sections_and_records_failures(self) -> None:
        def fail_extractor(workspace: Path, files: list[dict], flavors: list[str], config: dict) -> dict[str, list[dict]]:
            del workspace, files, flavors, config
            raise RuntimeError("boom")

        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            files = [{"path": "drivers/net/foo.c", "kind": "c", "size": 1, "mtime": 1, "ext": ".c"}]
            empty_result = run_project_context_extractors(workspace, files, ["c"])
            failed_result = run_project_context_extractors(
                workspace,
                files,
                ["c"],
                extractors=[
                    ProjectContextExtractor("bad", ("c",), ("symbols",), fail_extractor),
                ],
            )

        self.assertEqual(empty_result["sections"]["symbols"], [])
        self.assertEqual(empty_result["errors"], [])
        self.assertIn("c-symbols", [item["name"] for item in empty_result["extractors"]])
        self.assertIn("skipped", [item["status"] for item in empty_result["extractors"]])
        self.assertEqual(failed_result["extractors"][0]["status"], "failed")
        self.assertIn("RuntimeError: boom", failed_result["errors"][0]["error"])

    def test_status_reports_failed_when_index_is_unreadable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            (workspace / "foo.c").write_text("int foo(void) { return 0; }\n", encoding="utf-8")
            result = build_project_context_index(root, workspace)
            Path(result["paths"]["index"]).write_text("{", encoding="utf-8")
            status = project_context_index_status(root, workspace)

        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["error"], "index is unreadable")

    def test_status_marks_non_git_index_stale_when_files_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            (workspace / "foo.c").write_text("int foo(void) { return 0; }\n", encoding="utf-8")

            build_project_context_index(root, workspace)
            fresh = project_context_index_status(root, workspace)
            (workspace / "foo.c").write_text("int foo(void) { return 123; }\n", encoding="utf-8")
            quick = project_context_index_status(root, workspace, verify_worktree=False)
            stale = project_context_index_status(root, workspace)
            build_project_context_index(root, workspace)
            refreshed = project_context_index_status(root, workspace)

        self.assertEqual(fresh["status"], "fresh")
        self.assertEqual(quick["status"], "fresh")
        self.assertEqual(quick["stale_check"], "head-only")
        self.assertFalse(quick["verified_worktree"])
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(refreshed["status"], "fresh")

    def test_status_marks_index_stale_when_git_worktree_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "home"
            workspace = Path(tmp) / "repo"
            workspace.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=workspace, check=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=workspace, check=True)
            (workspace / "foo.c").write_text("int foo(void) { return 0; }\n", encoding="utf-8")
            subprocess.run(["git", "add", "foo.c"], cwd=workspace, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=workspace, check=True)

            build_project_context_index(root, workspace)
            fresh = project_context_index_status(root, workspace)
            (workspace / "foo.c").write_text("int foo(void) { return 1; }\n", encoding="utf-8")
            quick = project_context_index_status(root, workspace, verify_worktree=False)
            stale = project_context_index_status(root, workspace)
            build_project_context_index(root, workspace)
            refreshed = project_context_index_status(root, workspace)

        self.assertEqual(fresh["status"], "fresh")
        self.assertEqual(quick["status"], "fresh")
        self.assertEqual(quick["stale_check"], "head-only")
        self.assertFalse(quick["verified_worktree"])
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(refreshed["status"], "fresh")


if __name__ == "__main__":
    unittest.main()
