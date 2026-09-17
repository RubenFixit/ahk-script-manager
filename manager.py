# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

"""Backend for the AutoHotkey Script Manager.

The backend owns repository synchronization, TOML manifests, local state,
loader generation, and validation. The AutoHotkey front end invokes one command
at a time and reads the JSON response plus an optional TSV status table.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib
from typing import Any
from urllib.parse import urlsplit


CATALOG_VERSION = 1
DEFAULT_MANIFEST = "ahk-library.toml"
DEFAULT_MANAGER_HOTKEY = "#!m"
DEFAULT_REMOTE_TOGGLE_HOTKEY = "#!s"
DEFAULT_REMOTE_PROCESSES = ["mstsc.exe", "msrdc.exe", "msrdcw.exe", "vmware-view.exe"]
REPOSITORY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
HOTKEY = re.compile(r"^[#!+^]*([A-Za-z0-9]|F(?:[1-9]|1[0-9]|2[0-4]))$")
PROCESS_NAME = re.compile(r"^[A-Za-z0-9._-]+\.exe$", re.IGNORECASE)


class ManagerError(RuntimeError):
    """A user-actionable manager error."""


def run(command: list[str], cwd: Path | None = None) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except FileNotFoundError as exc:
        raise ManagerError(f"Required command is not installed: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise ManagerError(detail) from exc
    return completed.stdout.strip()


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def load_catalog(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "version": CATALOG_VERSION,
            "manager_hotkey": DEFAULT_MANAGER_HOTKEY,
            "remote_toggle_hotkey": DEFAULT_REMOTE_TOGGLE_HOTKEY,
            "remote_processes": DEFAULT_REMOTE_PROCESSES.copy(),
            "repositories": [],
        }
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    if data.get("version") != CATALOG_VERSION:
        raise ManagerError(f"Unsupported catalog version in {path}")
    repositories = data.get("repositories", [])
    if not isinstance(repositories, list):
        raise ManagerError("catalog.toml repositories must be an array of tables")
    data.setdefault("manager_hotkey", DEFAULT_MANAGER_HOTKEY)
    data.setdefault("remote_toggle_hotkey", DEFAULT_REMOTE_TOGGLE_HOTKEY)
    data.setdefault("remote_processes", DEFAULT_REMOTE_PROCESSES.copy())
    return data


def save_catalog(path: Path, catalog: dict[str, Any]) -> None:
    remote_processes = catalog.get("remote_processes", DEFAULT_REMOTE_PROCESSES)
    process_values = ", ".join(toml_string(str(item)) for item in remote_processes)
    lines = [
        f"version = {CATALOG_VERSION}",
        f"manager_hotkey = {toml_string(str(catalog.get('manager_hotkey', DEFAULT_MANAGER_HOTKEY)))}",
        f"remote_toggle_hotkey = {toml_string(str(catalog.get('remote_toggle_hotkey', DEFAULT_REMOTE_TOGGLE_HOTKEY)))}",
        f"remote_processes = [{process_values}]",
        "",
    ]
    for repo in catalog.get("repositories", []):
        lines.append("[[repositories]]")
        for key in ("id", "url", "ref", "manifest", "update"):
            lines.append(f"{key} = {toml_string(str(repo.get(key, '')))}")
        lines.append(f"local = {'true' if repo.get('local', False) else 'false'}")
        lines.append(f"trusted = {'true' if repo.get('trusted', False) else 'false'}")
        enabled = ", ".join(toml_string(str(item)) for item in repo.get("enabled", []))
        lines.append(f"enabled = [{enabled}]")
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(lines), encoding="utf-8")
    temporary.replace(path)


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"repositories": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def find_repo(catalog: dict[str, Any], repo_id: str) -> dict[str, Any]:
    for repo in catalog.get("repositories", []):
        if repo.get("id") == repo_id:
            return repo
        raise ManagerError(f"Script source is not in the catalog: {repo_id}")


def validate_repo_id(repo_id: str) -> None:
    if not REPOSITORY_ID.fullmatch(repo_id):
        raise ManagerError("Repository IDs may contain letters, numbers, dots, dashes, and underscores")


def derive_repo_id(url: str, local: bool = False) -> str:
    """Create a stable catalog ID from a local path or remote Git URL."""
    raw = url.strip().rstrip("/\\")
    if local:
        parts = [Path(raw).name]
    else:
        # Support scp-style SSH URLs such as git@github.com:owner/repo.git.
        scp_match = re.match(r"^[^@\s]+@[^:\s]+:(.+)$", raw)
        if scp_match:
            remote_path = scp_match.group(1)
        else:
            parsed = urlsplit(raw)
            remote_path = parsed.path if parsed.scheme or parsed.netloc else raw
        parts = [part for part in re.split(r"[/\\]+", remote_path) if part]

    if not parts:
        raise ManagerError("Could not derive a repository ID from the URL")
    parts[-1] = re.sub(r"\.git$", "", parts[-1], flags=re.IGNORECASE)
    repo_id = "-".join(parts)
    repo_id = re.sub(r"[^A-Za-z0-9]+", "-", repo_id).strip("-").lower()
    if not repo_id:
        raise ManagerError("Could not derive a repository ID from the URL")
    validate_repo_id(repo_id)
    return repo_id


def is_local_repo(repo: dict[str, Any]) -> bool:
    return bool(repo.get("local", False))


def repo_path(data_dir: Path, repo: dict[str, Any]) -> Path:
    if is_local_repo(repo):
        return Path(str(repo["url"])).expanduser().resolve()
    return (data_dir / "repositories" / str(repo["id"])).resolve()


def safe_child(root: Path, relative: str, label: str) -> Path:
    root = root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ManagerError(f"{label} escapes the repository: {relative}") from exc
    return candidate


def read_manifest(data_dir: Path, repo: dict[str, Any]) -> tuple[dict[str, Any], Path]:
    root = repo_path(data_dir, repo)
    if not root.is_dir():
        raise ManagerError(f"Script source has not been synchronized: {repo['id']}")
    manifest_path = safe_child(root, repo.get("manifest") or DEFAULT_MANIFEST, "Manifest path")
    if not manifest_path.is_file():
        raise ManagerError(f"Manifest not found: {manifest_path}")
    with manifest_path.open("rb") as stream:
        manifest = tomllib.load(stream)
    scripts = manifest.get("scripts", [])
    if not isinstance(scripts, list):
        raise ManagerError(f"Manifest scripts must be an array: {manifest_path}")
    seen: set[str] = set()
    for script in scripts:
        script_id = str(script.get("id", ""))
        if not script_id or script_id in seen:
            raise ManagerError(f"Script IDs must be non-empty and unique: {manifest_path}")
        seen.add(script_id)
        script_type = script.get("type", "standalone")
        if script_type not in {"include", "standalone"}:
            raise ManagerError(f"Unsupported script type for {script_id}: {script_type}")
        path = safe_child(root, str(script.get("path", "")), f"Script path for {script_id}")
        if not path.is_file():
            raise ManagerError(f"Script not found for {script_id}: {path}")
    return manifest, manifest_path


def git_commit(path: Path) -> str:
    if not (path / ".git").exists():
        return ""
    try:
        return run(["git", "rev-parse", "HEAD"], path)
    except ManagerError:
        # A local collection can still be used when Git metadata is unavailable
        # or the current security context does not trust its ownership.
        return ""


def manager_repo_path() -> Path:
    return Path(__file__).resolve().parent


def check_manager_update() -> dict[str, Any]:
    root = manager_repo_path()
    if not (root / ".git").is_dir():
        return {"message": "Automatic updates are unavailable because this is not a Git clone", "update_available": False}
    run(["git", "fetch", "--quiet", "origin"], root)
    try:
        upstream = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], root)
    except ManagerError as exc:
        raise ManagerError("The manager's current branch has no upstream branch") from exc
    local = run(["git", "rev-parse", "HEAD"], root)
    remote = run(["git", "rev-parse", upstream], root)
    available = local != remote and run(["git", "merge-base", local, remote], root) == local
    message = "A Script Manager update is available" if available else "Script Manager is up to date"
    return {"message": message, "update_available": available}


def update_manager() -> dict[str, Any]:
    root = manager_repo_path()
    if not (root / ".git").is_dir():
        raise ManagerError("Automatic updates require a Git clone of the Script Manager")
    if run(["git", "status", "--porcelain"], root):
        raise ManagerError("The Script Manager has local changes; update it manually to avoid overwriting them")
    before = run(["git", "rev-parse", "HEAD"], root)
    run(["git", "pull", "--ff-only"], root)
    after = run(["git", "rev-parse", "HEAD"], root)
    message = "Script Manager updated successfully" if before != after else "Script Manager is already up to date"
    return {"message": message, "updated": before != after}


def synchronize(data_dir: Path, catalog: dict[str, Any], state: dict[str, Any], repo_id: str) -> dict[str, Any]:
    repo = find_repo(catalog, repo_id)
    root = repo_path(data_dir, repo)
    if is_local_repo(repo):
        if not root.is_dir():
            raise ManagerError(f"Local script source does not exist: {root}")
        commit = git_commit(root)
        read_manifest(data_dir, repo)
        return {"message": f"Validated local script source {repo_id}", "commit": commit}

    root.parent.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        if root.exists():
            raise ManagerError(f"Managed source directory is not a Git clone: {root}")
        run(["git", "clone", "--no-checkout", str(repo["url"]), str(root)])

    previous = git_commit(root)
    run(["git", "fetch", "origin", "--tags", "--prune"], root)
    requested_ref = str(repo.get("ref", "")).strip()
    candidates = []
    if requested_ref:
        candidates.extend([f"origin/{requested_ref}", requested_ref])
    else:
        try:
            candidates.append(run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], root))
        except ManagerError:
            candidates.append("origin/main")

    target = ""
    for candidate in candidates:
        try:
            target = run(["git", "rev-parse", "--verify", f"{candidate}^{{commit}}"], root)
            break
        except ManagerError:
            continue
    if not target:
        raise ManagerError(f"Could not resolve source revision: {requested_ref or 'remote default'}")

    run(["git", "checkout", "--detach", target], root)
    read_manifest(data_dir, repo)
    repo_state = state.setdefault("repositories", {}).setdefault(repo_id, {})
    if previous and previous != target:
        repo_state["previous_commit"] = previous
    repo_state["active_commit"] = target
    save_state(data_dir / "state.json", state)
    return {"message": f"Synchronized script source {repo_id}", "commit": target, "previous_commit": previous}


def rollback(data_dir: Path, catalog: dict[str, Any], state: dict[str, Any], repo_id: str) -> dict[str, Any]:
    repo = find_repo(catalog, repo_id)
    if is_local_repo(repo):
        raise ManagerError("Local script sources are not modified by the manager")
    repo_state = state.get("repositories", {}).get(repo_id, {})
    previous = repo_state.get("previous_commit")
    if not previous:
        raise ManagerError(f"No rollback revision is recorded for {repo_id}")
    root = repo_path(data_dir, repo)
    current = git_commit(root)
    run(["git", "checkout", "--detach", str(previous)], root)
    read_manifest(data_dir, repo)
    repo_state["active_commit"] = previous
    repo_state["previous_commit"] = current
    save_state(data_dir / "state.json", state)
    return {"message": f"Rolled back {repo_id}", "commit": previous}


def catalog_rows(data_dir: Path, catalog: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for repo in catalog.get("repositories", []):
        root = repo_path(data_dir, repo)
        enabled = set(str(item) for item in repo.get("enabled", []))
        repo_name = str(repo.get("id", ""))
        status = "local" if is_local_repo(repo) else ("ready" if (root / ".git").exists() else "not synced")
        try:
            manifest, _ = read_manifest(data_dir, repo)
            repo_name = str(manifest.get("library", {}).get("name", repo_name))
            for script in manifest.get("scripts", []):
                rows.append(
                    {
                        "repo_id": str(repo["id"]),
                        "repo_name": repo_name,
                        "script_id": str(script["id"]),
                        "script_name": str(script.get("name", script["id"])),
                        "type": str(script.get("type", "standalone")),
                        "enabled": "yes" if script["id"] in enabled else "no",
                        "status": status,
                        "repo_path": str(root),
                    }
                )
        except ManagerError as exc:
            rows.append(
                {
                    "repo_id": str(repo["id"]),
                    "repo_name": repo_name,
                    "script_id": "",
                    "script_name": "",
                    "type": "",
                    "enabled": "",
                    "status": str(exc),
                    "repo_path": str(root),
                }
            )
    return rows


def source_rows(data_dir: Path, catalog: dict[str, Any]) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for repo in catalog.get("repositories", []):
        root = repo_path(data_dir, repo)
        source_name = str(repo.get("id", ""))
        status = "local" if is_local_repo(repo) else ("ready" if (root / ".git").exists() else "not synced")
        try:
            manifest, _ = read_manifest(data_dir, repo)
            source_name = str(manifest.get("library", {}).get("name", source_name))
        except ManagerError as exc:
            status = str(exc)
        rows.append(
            {
                "source_id": str(repo.get("id", "")),
                "source_name": source_name,
                "status": status,
                "ref": str(repo.get("ref", "")) or "default",
                "revision": git_commit(root)[:12] if root.is_dir() else "",
                "source_path": str(root),
                "url": str(repo.get("url", "")),
                "trusted": "yes" if repo.get("trusted", False) else "no",
            }
        )
    return rows


def write_table(path: Path, rows: list[dict[str, str]]) -> None:
    columns = ("repo_id", "repo_name", "script_id", "script_name", "type", "enabled", "status", "repo_path")
    lines = ["\t".join(columns)]
    for row in rows:
        values = [str(row.get(column, "")).replace("\t", " ").replace("\r", " ").replace("\n", " ") for column in columns]
        lines.append("\t".join(values))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_source_table(path: Path, rows: list[dict[str, str]]) -> None:
    columns = ("source_id", "source_name", "status", "ref", "revision", "source_path", "url", "trusted")
    lines = ["\t".join(columns)]
    for row in rows:
        values = [str(row.get(column, "")).replace("\t", " ").replace("\r", " ").replace("\n", " ") for column in columns]
        lines.append("\t".join(values))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def set_enabled(catalog: dict[str, Any], repo_id: str, script_id: str, enabled: bool) -> None:
    repo = find_repo(catalog, repo_id)
    values = [str(item) for item in repo.get("enabled", [])]
    if enabled and script_id not in values:
        values.append(script_id)
    if not enabled:
        values = [item for item in values if item != script_id]
    repo["enabled"] = values


def locate_autohotkey(explicit: Path | None = None) -> Path:
    if explicit:
        explicit = explicit.expanduser().resolve()
        if explicit.is_file():
            return explicit
        raise ManagerError(f"The AutoHotkey executable used to launch the manager no longer exists: {explicit}")

    local_app_data = Path(os.environ.get("LOCALAPPDATA", ""))
    user_profile = Path(os.environ.get("USERPROFILE", ""))
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "AutoHotkey" / "v2" / "AutoHotkey64.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "AutoHotkey" / "AutoHotkey.exe",
        local_app_data / "Programs" / "AutoHotkey" / "v2" / "AutoHotkey64.exe",
        local_app_data / "Programs" / "AutoHotkey" / "AutoHotkey.exe",
        user_profile / "scoop" / "apps" / "autohotkey" / "current" / "AutoHotkey.exe",
    ]
    executable = shutil.which("AutoHotkey64.exe") or shutil.which("AutoHotkey.exe")
    if executable:
        candidates.insert(0, Path(executable))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ManagerError("AutoHotkey v2 was not found")


def enabled_scripts(data_dir: Path, catalog: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any], Path]]:
    result = []
    for repo in catalog.get("repositories", []):
        manifest, _ = read_manifest(data_dir, repo)
        enabled = set(str(item) for item in repo.get("enabled", []))
        root = repo_path(data_dir, repo)
        for script in manifest.get("scripts", []):
            if script["id"] in enabled:
                result.append((repo, script, safe_child(root, str(script["path"]), "Script path")))
    return result


def ahk_path(path: Path) -> str:
    return str(path).replace("`", "``")


def ahk_single_quoted(value: str) -> str:
    return value.replace("`", "``").replace("'", "`'")


def validate_manager_hotkey(value: str) -> None:
    if value and not HOTKEY.fullmatch(value):
        raise ManagerError("Manager shortcut must use AutoHotkey notation such as #!m, or be blank to disable it")


def normalize_remote_processes(value: str | list[str]) -> list[str]:
    items = value.split(",") if isinstance(value, str) else value
    result = []
    for item in items:
        process = str(item).strip().lower()
        if not process:
            continue
        if not PROCESS_NAME.fullmatch(process):
            raise ManagerError(f"Remote client process must be an executable name ending in .exe: {process}")
        if process not in result:
            result.append(process)
    return result


def generate_loader(data_dir: Path, catalog: dict[str, Any], validation: bool = False) -> Path:
    generated = data_dir / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    path = generated / ("validate-loader.ahk" if validation else "active-loader.ahk")
    lines = ["#Requires AutoHotkey v2.0", "#SingleInstance Force" if not validation else "#SingleInstance Off"]
    manager_hotkey = str(catalog.get("manager_hotkey", DEFAULT_MANAGER_HOTKEY)).strip()
    remote_toggle_hotkey = str(catalog.get("remote_toggle_hotkey", DEFAULT_REMOTE_TOGGLE_HOTKEY)).strip()
    remote_processes = normalize_remote_processes(catalog.get("remote_processes", DEFAULT_REMOTE_PROCESSES))
    validate_manager_hotkey(manager_hotkey)
    validate_manager_hotkey(remote_toggle_hotkey)
    manager_script = ahk_single_quoted(str(manager_repo_path() / "manager.ahk"))
    lines.extend([
        "global ASM_ManualRemoteMode := false",
        "global ASM_AutoRemoteMode := false",
        "global ASM_LastSuspended := false",
        "#SuspendExempt true",
    ])
    if manager_hotkey:
        lines.append(f"{manager_hotkey}::Run('\"' A_AhkPath '\" \"{manager_script}\"')")
    if remote_toggle_hotkey:
        lines.append(f"{remote_toggle_hotkey}::ASM_ToggleRemoteMode()")
    lines.extend([
        "#SuspendExempt false",
        "ASM_ToggleRemoteMode(*) {",
        "    global ASM_ManualRemoteMode := !ASM_ManualRemoteMode",
        "    ASM_ApplyRemoteMode(true)",
        "}",
        "ASM_CheckRemoteWindow(*) {",
        "    global ASM_AutoRemoteMode",
        "    try processName := StrLower(WinGetProcessName(\"A\"))",
        "    catch",
        "        processName := \"\"",
        f"    remoteProcesses := [{', '.join(toml_string(item) for item in remote_processes)}]",
        "    isRemote := false",
        "    for remoteProcess in remoteProcesses {",
        "        if processName = remoteProcess {",
        "            isRemote := true",
        "            break",
        "        }",
        "    }",
        "    if isRemote != ASM_AutoRemoteMode {",
        "        ASM_AutoRemoteMode := isRemote",
        "        ASM_ApplyRemoteMode(true)",
        "    }",
        "}",
        "ASM_ApplyRemoteMode(showNotice := false) {",
        "    global ASM_ManualRemoteMode, ASM_AutoRemoteMode, ASM_LastSuspended",
        "    shouldSuspend := ASM_ManualRemoteMode || ASM_AutoRemoteMode",
        "    if shouldSuspend = ASM_LastSuspended",
        "        return",
        "    ASM_LastSuspended := shouldSuspend",
        "    Suspend(shouldSuspend)",
        "    if showNotice",
        "        TrayTip(shouldSuspend ? \"Managed hotkeys paused\" : \"Managed hotkeys active\", \"AutoHotkey Script Manager\")",
        "}",
    ])
    if remote_processes:
        lines.append("SetTimer(ASM_CheckRemoteWindow, 250)")
    if validation:
        lines.append("SetTimer(() => ExitApp(), -300)")
    for repo, script, script_path in enabled_scripts(data_dir, catalog):
        if script.get("type", "standalone") != "include":
            continue
        if not repo.get("trusted", False):
            raise ManagerError(f"Include-mode script requires a trusted repository: {repo['id']}/{script['id']}")
        lines.append(f"#Include {ahk_path(script_path)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return path


def validate_loader(data_dir: Path, catalog: dict[str, Any], autohotkey: Path | None = None) -> None:
    loader = generate_loader(data_dir, catalog, validation=True)
    executable = locate_autohotkey(autohotkey)
    try:
        completed = subprocess.run(
            [str(executable), "/ErrorStdOut", str(loader)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        raise ManagerError("AutoHotkey validation did not finish") from exc
    if completed.returncode:
        raise ManagerError((completed.stdout or completed.stderr or "AutoHotkey validation failed").strip())


def activate(data_dir: Path, catalog: dict[str, Any], autohotkey: Path | None = None) -> dict[str, Any]:
    validate_loader(data_dir, catalog, autohotkey)
    loader = generate_loader(data_dir, catalog)
    executable = locate_autohotkey(autohotkey)
    subprocess.Popen(
        [str(executable), str(loader)],
        cwd=loader.parent,
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0,
        close_fds=True,
    )
    standalone = [f"{repo['id']}/{script['id']}" for repo, script, _ in enabled_scripts(data_dir, catalog) if script.get("type") == "standalone"]
    return {
        "message": "Trusted include-mode loader activated" + (f"; standalone scripts require explicit launch: {', '.join(standalone)}" if standalone else ""),
        "loader": str(loader),
    }


def launch_standalone(
    data_dir: Path,
    catalog: dict[str, Any],
    repo_id: str,
    script_id: str,
    autohotkey: Path | None = None,
) -> dict[str, Any]:
    repo = find_repo(catalog, repo_id)
    manifest, _ = read_manifest(data_dir, repo)
    for script in manifest.get("scripts", []):
        if script.get("id") == script_id:
            if script.get("type", "standalone") != "standalone":
                raise ManagerError("Only standalone scripts can be launched separately")
            path = safe_child(repo_path(data_dir, repo), str(script["path"]), "Script path")
            executable = locate_autohotkey(autohotkey)
            subprocess.Popen(
                [str(executable), str(path)],
                cwd=path.parent,
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS) if os.name == "nt" else 0,
                close_fds=True,
            )
            return {"message": f"Started {repo_id}/{script_id}"}
    raise ManagerError(f"Script is not in the manifest: {repo_id}/{script_id}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--response", type=Path)
    parser.add_argument("--table", type=Path)
    parser.add_argument("--autohotkey", type=Path)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init")
    subparsers.add_parser("list")
    subparsers.add_parser("sources")

    add = subparsers.add_parser("add")
    add.add_argument("repo_id")
    add.add_argument("url")
    add.add_argument("--ref", default="")
    add.add_argument("--manifest", default=DEFAULT_MANIFEST)
    add.add_argument("--local", action="store_true")
    add.add_argument("--trusted", action="store_true")

    add_url = subparsers.add_parser("add-url")
    add_url.add_argument("url")
    add_url.add_argument("--id", dest="repo_id", default="")
    add_url.add_argument("--ref", default="")
    add_url.add_argument("--manifest", default=DEFAULT_MANIFEST)
    add_url.add_argument("--local", action="store_true")
    add_url.add_argument("--trusted", action="store_true")

    remove = subparsers.add_parser("remove")
    remove.add_argument("repo_id")

    for name in ("sync", "rollback"):
        command = subparsers.add_parser(name)
        command.add_argument("repo_id")

    for name in ("enable", "disable", "launch"):
        command = subparsers.add_parser(name)
        command.add_argument("repo_id")
        command.add_argument("script_id")

    subparsers.add_parser("validate")
    subparsers.add_parser("activate")
    shortcut = subparsers.add_parser("set-manager-hotkey")
    shortcut.add_argument("hotkey")
    settings = subparsers.add_parser("set-settings")
    settings.add_argument("--manager-hotkey", required=True)
    settings.add_argument("--remote-toggle-hotkey", required=True)
    settings.add_argument("--remote-processes", required=True)
    subparsers.add_parser("self-update-check")
    subparsers.add_parser("self-update")
    return parser


def execute(args: argparse.Namespace) -> dict[str, Any]:
    data_dir: Path = args.data_dir.expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = data_dir / "catalog.toml"
    catalog = load_catalog(catalog_path)
    state = load_state(data_dir / "state.json")

    if args.command == "init":
        if not catalog_path.exists():
            save_catalog(catalog_path, catalog)
        return {"message": f"Manager data initialized at {data_dir}"}
    if args.command == "list":
        rows = catalog_rows(data_dir, catalog)
        if args.table:
            write_table(args.table, rows)
        return {"message": f"Found {len(rows)} script entries", "rows": rows}
    if args.command == "sources":
        rows = source_rows(data_dir, catalog)
        if args.table:
            write_source_table(args.table, rows)
        return {"message": f"Found {len(rows)} script sources", "rows": rows}
    if args.command in {"add", "add-url"}:
        repo_id = args.repo_id or derive_repo_id(args.url, args.local)
        validate_repo_id(repo_id)
        if any(repo.get("id") == repo_id for repo in catalog.get("repositories", [])):
            raise ManagerError(f"Source ID already exists: {repo_id}")
        if args.local and not Path(args.url).expanduser().is_dir():
            raise ManagerError(f"Local script source does not exist: {args.url}")
        catalog.setdefault("repositories", []).append(
            {
                "id": repo_id,
                "url": args.url,
                "ref": args.ref,
                "manifest": args.manifest,
                "update": "prompt",
                "local": args.local,
                "trusted": args.trusted,
                "enabled": [],
            }
        )
        save_catalog(catalog_path, catalog)
        return {"message": f"Added script source {repo_id}", "repo_id": repo_id}
    if args.command == "remove":
        before = len(catalog.get("repositories", []))
        catalog["repositories"] = [repo for repo in catalog.get("repositories", []) if repo.get("id") != args.repo_id]
        if len(catalog["repositories"]) == before:
            raise ManagerError(f"Script source is not in the catalog: {args.repo_id}")
        save_catalog(catalog_path, catalog)
        return {"message": f"Removed script source {args.repo_id} from the catalog; cached files were retained"}
    if args.command == "sync":
        return synchronize(data_dir, catalog, state, args.repo_id)
    if args.command == "rollback":
        return rollback(data_dir, catalog, state, args.repo_id)
    if args.command in {"enable", "disable"}:
        repo = find_repo(catalog, args.repo_id)
        manifest, _ = read_manifest(data_dir, repo)
        if args.script_id not in {script.get("id") for script in manifest.get("scripts", [])}:
            raise ManagerError(f"Script is not in the manifest: {args.repo_id}/{args.script_id}")
        set_enabled(catalog, args.repo_id, args.script_id, args.command == "enable")
        save_catalog(catalog_path, catalog)
        return {"message": f"{'Enabled' if args.command == 'enable' else 'Disabled'} {args.repo_id}/{args.script_id}"}
    if args.command == "validate":
        validate_loader(data_dir, catalog, args.autohotkey)
        return {"message": "Enabled include-mode scripts passed AutoHotkey validation"}
    if args.command == "activate":
        return activate(data_dir, catalog, args.autohotkey)
    if args.command == "set-manager-hotkey":
        hotkey = args.hotkey.strip()
        validate_manager_hotkey(hotkey)
        catalog["manager_hotkey"] = hotkey
        save_catalog(catalog_path, catalog)
        return {"message": "Manager shortcut disabled" if not hotkey else f"Manager shortcut set to {hotkey}"}
    if args.command == "set-settings":
        manager_hotkey = args.manager_hotkey.strip()
        remote_toggle_hotkey = args.remote_toggle_hotkey.strip()
        validate_manager_hotkey(manager_hotkey)
        validate_manager_hotkey(remote_toggle_hotkey)
        catalog["manager_hotkey"] = manager_hotkey
        catalog["remote_toggle_hotkey"] = remote_toggle_hotkey
        catalog["remote_processes"] = normalize_remote_processes(args.remote_processes)
        save_catalog(catalog_path, catalog)
        return {"message": "Script Manager settings saved"}
    if args.command == "self-update-check":
        return check_manager_update()
    if args.command == "self-update":
        return update_manager()
    if args.command == "launch":
        return launch_standalone(data_dir, catalog, args.repo_id, args.script_id, args.autohotkey)
    raise ManagerError(f"Unsupported command: {args.command}")


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = {"ok": True, **execute(args)}
        exit_code = 0
    except (ManagerError, tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        result = {"ok": False, "message": str(exc)}
        exit_code = 1
    payload = json.dumps(result, indent=2) + "\n"
    if args.response:
        args.response.parent.mkdir(parents=True, exist_ok=True)
        args.response.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
