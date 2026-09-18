from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "manager.py"
SPEC = importlib.util.spec_from_file_location("ahk_script_manager", MODULE_PATH)
assert SPEC and SPEC.loader
manager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manager)


class CatalogTests(unittest.TestCase):
    def test_remote_id_uses_namespace_and_repository(self) -> None:
        self.assertEqual(
            manager.derive_repo_id("https://github.com/RubenFixit/ahk-script-library.git"),
            "rubenfixit-ahk-script-library",
        )

    def test_nested_namespace_and_ssh_urls(self) -> None:
        self.assertEqual(
            manager.derive_repo_id("git@gitlab.com:Company/Tools/AHK-Library.git"),
            "company-tools-ahk-library",
        )

    def test_local_id_uses_folder_name(self) -> None:
        self.assertEqual(manager.derive_repo_id(r"C:\Projects\My Scripts", local=True), "my-scripts")

    def test_catalog_round_trip(self) -> None:
        catalog = {
            "version": 1,
            "manager_hotkey": "#!m",
            "remote_toggle_hotkey": "#!s",
            "remote_auto_enabled": True,
            "remote_processes": ["mstsc.exe", "vmware-view.exe"],
            "repositories": [
                {
                    "id": "sample",
                    "url": "https://example.invalid/sample.git",
                    "ref": "v1.0.0",
                    "manifest": "ahk-library.toml",
                    "update": "prompt",
                    "local": False,
                    "trusted": True,
                    "enabled": ["one", "two"],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.toml"
            manager.save_catalog(path, catalog)
            self.assertEqual(manager.load_catalog(path), catalog)

    def test_legacy_remote_defaults_gain_current_horizon_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.toml"
            path.write_text(
                'version = 1\nremote_processes = ["mstsc.exe", "msrdc.exe", "msrdcw.exe", "vmware-view.exe"]\n',
                encoding="utf-8",
            )
            self.assertIn("horizon-client.exe", manager.load_catalog(path)["remote_processes"])

    def test_safe_child_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(manager.ManagerError):
                manager.safe_child(Path(directory), "../outside.ahk", "Script")


class BackendRobustnessTests(unittest.TestCase):
    def test_unexpected_exception_still_produces_a_response(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            response_path = Path(directory) / "response.json"
            argv = ["manager.py", "--data-dir", directory, "--response", str(response_path), "list"]
            with patch.object(manager.sys, "argv", argv), \
                    patch.object(manager, "execute", side_effect=PermissionError("Access is denied")):
                exit_code = manager.main()

            self.assertEqual(exit_code, 1)
            payload = json.loads(response_path.read_text(encoding="utf-8"))
            self.assertFalse(payload["ok"])
            self.assertIn("Access is denied", payload["message"])


class RepositoryLookupTests(unittest.TestCase):
    def test_find_repo_locates_entry_not_first_in_list(self) -> None:
        catalog = {"repositories": [{"id": "a"}, {"id": "b"}]}
        self.assertEqual(manager.find_repo(catalog, "b")["id"], "b")

    def test_find_repo_raises_for_unknown_id(self) -> None:
        catalog = {"repositories": [{"id": "a"}]}
        with self.assertRaises(manager.ManagerError):
            manager.find_repo(catalog, "missing")


class LocalSyncTests(unittest.TestCase):
    @staticmethod
    def _catalog(root: Path) -> dict:
        return {"repositories": [{"id": "lib", "url": str(root), "local": True, "manifest": "ahk-library.toml"}]}

    def test_pulls_when_behind_and_working_tree_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            commits = iter(["local1", "local2"])

            def fake_run(command: list[str], cwd: Path | None = None) -> str:
                if command[:2] == ["git", "symbolic-ref"]:
                    raise manager.ManagerError("no upstream")
                if command[:3] == ["git", "rev-parse", "--verify"]:
                    return "remote1"
                if command[:2] == ["git", "status"]:
                    return ""
                return ""

            with patch.object(manager, "git_commit", side_effect=lambda _: next(commits)), \
                    patch.object(manager, "run", side_effect=fake_run), \
                    patch.object(manager, "read_manifest", return_value=({}, root / "ahk-library.toml")):
                result = manager.synchronize(root / "data", self._catalog(root), {}, "lib")

            self.assertIn("Pulled latest changes", result["message"])
            self.assertEqual(result["commit"], "local2")

    def test_reports_dirty_working_tree_instead_of_pulling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()

            def fake_run(command: list[str], cwd: Path | None = None) -> str:
                if command[:2] == ["git", "symbolic-ref"]:
                    raise manager.ManagerError("no upstream")
                if command[:3] == ["git", "rev-parse", "--verify"]:
                    return "remote1"
                if command[:2] == ["git", "status"]:
                    return " M scripts/one.ahk"
                if command[:2] == ["git", "pull"]:
                    self.fail("should not pull with a dirty working tree")
                return ""

            with patch.object(manager, "git_commit", return_value="local1"), \
                    patch.object(manager, "run", side_effect=fake_run), \
                    patch.object(manager, "read_manifest", return_value=({}, root / "ahk-library.toml")):
                result = manager.synchronize(root / "data", self._catalog(root), {}, "lib")

            self.assertIn("has local changes", result["message"])


class ManifestTests(unittest.TestCase):
    def test_repository_can_publish_multiple_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.ahk").write_text("#Requires AutoHotkey v2.0\n", encoding="utf-8")
            (root / "two.ahk").write_text("#Requires AutoHotkey v2.0\n", encoding="utf-8")
            (root / "ahk-library.toml").write_text(
                """[library]
name = "Test"

[[scripts]]
id = "one"
path = "one.ahk"
type = "include"

[[scripts]]
id = "two"
path = "two.ahk"
type = "standalone"
""",
                encoding="utf-8",
            )
            repo = {"id": "test", "url": str(root), "local": True, "manifest": "ahk-library.toml"}
            manifest, _ = manager.read_manifest(root / "data", repo)
            self.assertEqual([script["id"] for script in manifest["scripts"]], ["one", "two"])


class SelfUpdateTests(unittest.TestCase):
    def test_update_check_reports_fast_forward_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()

            def fake_run(command: list[str], cwd: Path | None = None) -> str:
                joined = " ".join(command)
                if "--symbolic-full-name" in command:
                    return "origin/main"
                if joined.endswith("rev-parse HEAD"):
                    return "local"
                if joined.endswith("rev-parse origin/main"):
                    return "remote"
                if command[1] == "merge-base":
                    return "local"
                return ""

            with patch.object(manager, "manager_repo_path", return_value=root), patch.object(manager, "run", side_effect=fake_run):
                result = manager.check_manager_update()
            self.assertTrue(result["update_available"])

    def test_self_update_refuses_local_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            with patch.object(manager, "manager_repo_path", return_value=root), patch.object(manager, "run", return_value=" M manager.py"):
                with self.assertRaisesRegex(manager.ManagerError, "local changes"):
                    manager.update_manager()


class LoaderSettingsTests(unittest.TestCase):
    def test_default_manager_hotkey_is_written_to_loader(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loader = manager.generate_loader(root, {"version": 1, "repositories": []})
            content = loader.read_text(encoding="utf-8-sig")
            self.assertIn("#!m::Run", content)
            self.assertIn("#!s::ASM_ToggleRemoteMode", content)
            self.assertIn('"mstsc.exe"', content)
            self.assertIn('"vmware-view.exe"', content)
            self.assertIn('"horizon-client.exe"', content)
            self.assertNotIn("TrayTip(", content)
            self.assertLess(content.index("#!s::ASM_ToggleRemoteMode"), content.index("#SuspendExempt false"))
            self.assertGreater(content.index("#!m::Run"), content.index("#SuspendExempt false"))

    def test_invalid_manager_hotkey_is_rejected(self) -> None:
        with self.assertRaises(manager.ManagerError):
            manager.validate_manager_hotkey("Run('bad')")

    def test_punctuation_manager_hotkeys_are_allowed(self) -> None:
        manager.validate_manager_hotkey("^!.")
        manager.validate_manager_hotkey("^!,")

    def test_remote_processes_are_normalized_and_validated(self) -> None:
        self.assertEqual(manager.normalize_remote_processes("MSTSC.EXE, vmware-view.exe, mstsc.exe"), ["mstsc.exe", "vmware-view.exe"])
        with self.assertRaises(manager.ManagerError):
            manager.normalize_remote_processes("not a process")

    def test_disabling_auto_remote_mode_preserves_processes_without_timer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog = {
                "version": 1,
                "remote_auto_enabled": False,
                "remote_processes": ["mstsc.exe", "horizon-client.exe"],
                "repositories": [],
            }
            loader = manager.generate_loader(Path(directory), catalog)
            content = loader.read_text(encoding="utf-8-sig")
            self.assertIn('"horizon-client.exe"', content)
            self.assertNotIn("SetTimer(ASM_CheckRemoteWindow", content)


if __name__ == "__main__":
    unittest.main()
