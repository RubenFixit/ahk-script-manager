from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest


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

    def test_safe_child_rejects_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(manager.ManagerError):
                manager.safe_child(Path(directory), "../outside.ahk", "Script")


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


if __name__ == "__main__":
    unittest.main()
