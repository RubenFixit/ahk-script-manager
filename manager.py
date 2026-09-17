# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

"""Backend for the AutoHotkey Module Manager.

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
REPOSITORY_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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
        return {"version": CATALOG_VERSION, "repositories": []}
    with path.open("rb") as stream:
        data = tomllib.load(stream)
    if data.get("version") != CATALOG_VERSION:
        raise ManagerError(f"Unsupported catalog version in {path}")
    repositories = data.get("repositories", [])
    if not isinstance(repositories, list):
        raise ManagerError("catalog.toml repositories must be an array of tables")
    return data


def save_catalog(path: Path, catalog: dict[str, Any]) -> None:
    lines = [f"version = {CATALOG_VERSION}", ""]
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
    raise ManagerError(f"Repository is not in the catalog: {repo_id}")


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
        raise ManagerError(f"Repository has not been synchronized: {repo['id']}")
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


def synchronize(data_dir: Path, catalog: dict[str, Any], state: dict[str, Any], repo_id: str) -> dict[str, Any]:
    repo = find_repo(catalog, repo_id)
    root = repo_path(data_dir, repo)
    if is_local_repo(repo):
        if not root.is_dir():
            raise ManagerError(f"Local repository does not exist: {root}")
        commit = git_commit(root)
        read_manifest(data_dir, repo)
        return {"message": f"Validated local repository {repo_id}", "commit": commit}

    root.parent.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        if root.exists():
            raise ManagerError(f"Managed repository directory is not a Git clone: {root}")
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
        raise ManagerError(f"Could not resolve repository revision: {requested_ref or 'remote default'}")

    run(["git", "checkout", "--detach", target], root)
    read_manifest(data_dir, repo)
    repo_state = state.setdefault("repositories", {}).setdefault(repo_id, {})
    if previous and previous != target:
        repo_state["previous_commit"] = previous
    repo_state["active_commit"] = target
    save_state(data_dir / "state.json", state)
    return {"message": f"Synchronized {repo_id}", "commit": target, "previous_commit": previous}


def rollback(data_dir: Path, catalog: dict[str, Any], state: dict[str, Any], repo_id: str) -> dict[str, Any]:
    repo = find_repo(catalog, repo_id)
    if is_local_repo(repo):
        raise ManagerError("Local repositories are not modified by the manager")
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


def locate_autohotkey() -> Path:
    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "AutoHotkey" / "v2" / "AutoHotkey64.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "AutoHotkey" / "AutoHotkey.exe",
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


def generate_loader(data_dir: Path, catalog: dict[str, Any], validation: bool = False) -> Path:
    generated = data_dir / "generated"
    generated.mkdir(parents=True, exist_ok=True)
    path = generated / ("validate-loader.ahk" if validation else "active-loader.ahk")
    lines = ["#Requires AutoHotkey v2.0", "#SingleInstance Force" if not validation else "#SingleInstance Off"]
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


def validate_loader(data_dir: Path, catalog: dict[str, Any]) -> None:
    loader = generate_loader(data_dir, catalog, validation=True)
    executable = locate_autohotkey()
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


def activate(data_dir: Path, catalog: dict[str, Any]) -> dict[str, Any]:
    validate_loader(data_dir, catalog)
    loader = generate_loader(data_dir, catalog)
    executable = locate_autohotkey()
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


def launch_standalone(data_dir: Path, catalog: dict[str, Any], repo_id: str, script_id: str) -> dict[str, Any]:
    repo = find_repo(catalog, repo_id)
    manifest, _ = read_manifest(data_dir, repo)
    for script in manifest.get("scripts", []):
        if script.get("id") == script_id:
            if script.get("type", "standalone") != "standalone":
                raise ManagerError("Only standalone scripts can be launched separately")
            path = safe_child(repo_path(data_dir, repo), str(script["path"]), "Script path")
            executable = locate_autohotkey()
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
        return {"message": f"Found {len(rows)} module sources", "rows": rows}
    if args.command in {"add", "add-url"}:
        repo_id = args.repo_id or derive_repo_id(args.url, args.local)
        validate_repo_id(repo_id)
        if any(repo.get("id") == repo_id for repo in catalog.get("repositories", [])):
            raise ManagerError(f"Repository ID already exists: {repo_id}")
        if args.local and not Path(args.url).expanduser().is_dir():
            raise ManagerError(f"Local repository does not exist: {args.url}")
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
        return {"message": f"Added repository {repo_id}", "repo_id": repo_id}
    if args.command == "remove":
        before = len(catalog.get("repositories", []))
        catalog["repositories"] = [repo for repo in catalog.get("repositories", []) if repo.get("id") != args.repo_id]
        if len(catalog["repositories"]) == before:
            raise ManagerError(f"Repository is not in the catalog: {args.repo_id}")
        save_catalog(catalog_path, catalog)
        return {"message": f"Removed {args.repo_id} from the catalog; cached files were retained"}
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
        validate_loader(data_dir, catalog)
        return {"message": "Enabled include-mode scripts passed AutoHotkey validation"}
    if args.command == "activate":
        return activate(data_dir, catalog)
    if args.command == "launch":
        return launch_standalone(data_dir, catalog, args.repo_id, args.script_id)
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
