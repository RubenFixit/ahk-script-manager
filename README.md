# AutoHotkey Script Manager

AutoHotkey Script Manager is a Windows desktop manager for AutoHotkey v2
scripts distributed across Git repositories. One repository may publish many
independently selectable scripts through an `ahk-library.toml` manifest.

The application uses:

- `manager.ahk` for the tray menu, management window, and script lifecycle.
- `manager.py` for Git operations, TOML parsing, validation, loader generation,
  and rollback state.
- Git for host-independent repository synchronization, including GitHub,
  GitLab, and self-hosted servers.
- `uv` to run the Python backend with the declared Python version.

The front end passes its own AutoHotkey v2 executable path to the backend, so
validation and activation work with system-wide, per-user, Scoop, and other
nonstandard AutoHotkey installations.

## Requirements

- Windows
- AutoHotkey v2
- Git
- uv

## Run

Run `manager.ahk` with AutoHotkey v2. Its machine-local catalog, managed clones,
generated loader, and rollback state are stored under
`%LOCALAPPDATA%\AhkScriptManager`.

On startup, the manager quietly checks its Git upstream for a newer revision.
When an update is available, it asks for confirmation before installing it and
restarting. It never installs an update without approval. Automatic updates
require a Git clone and are blocked when the manager has local changes.

Use **Add source** to register either a Git URL or an existing local clone that
publishes one or more AutoHotkey scripts.
Remote repository IDs are derived from their namespace and repository name, so
`https://github.com/RubenFixit/ahk-script-library.git` becomes
`rubenfixit-ahk-script-library`. Local repositories use their folder name.
Choose trust only for repositories whose scripts may be combined into the
generated `#Include` loader. Third-party scripts should normally use
`type = "standalone"` in their manifest.

## Using the interface

The main window manages scripts discovered from all configured sources:

- **Reload** rereads the local catalog and manifests. It does not download or
  start anything.
- **Manage sources** opens the source-level window described below.
- **Enable/Disable** changes whether the selected script belongs in the desired
  active configuration. It does not immediately restart running scripts.
- **Apply changes** validates the enabled include-mode scripts, generates their
  combined loader, and starts or restarts that loader. Use this after changing
  which include-mode modules are enabled.
- **Launch standalone** starts the selected `standalone` script in its own
  AutoHotkey process. Include-mode scripts are applied with **Apply changes**.
- **Open folder** opens the selected script's source directory.
- **Settings** changes the manager shortcut, manual Remote Mode shortcut, and
  the remote-client process list. The manager shortcut defaults to
  **Windows+Alt+M** (`#!m`), and manual Remote Mode defaults to
  **Windows+Alt+S** (`#!s`). Leave either shortcut blank to disable it.

The **Manage Script Sources** window lists each repository once:

- **Add source** registers a Git URL or local folder, derives a stable source
  ID, and performs its initial synchronization.
- **Reload** rereads source status without downloading changes.
- **Sync** fetches and validates the selected managed source at its configured
  branch, tag, or commit. For a local source, it validates the current folder.
- **Roll back** returns a managed clone to its previously active revision.
- **Remove** removes the source from the catalog but retains downloaded files.
- **Open folder** opens the selected source directory.

The enable/apply split is intentional: enablement edits the desired script set,
while applying performs validation and changes the running include-mode loader.
The generated active loader owns the manager shortcut, so it remains available
when the manager window is hidden. Saving the shortcut setting applies and
restarts the loader immediately.

The loader automatically pauses managed hotkeys while a configured remote
client owns the active window, allowing those keystrokes to reach the remote
computer. The defaults cover classic Remote Desktop (`mstsc.exe`), Microsoft's
newer Remote Desktop clients (`msrdc.exe` and `msrdcw.exe`), and Omnissa/VMware
Horizon Client (`vmware-view.exe`). Focus another application to restore local
hotkeys. Windows+Alt+S also toggles Remote Mode manually and remains available
while managed hotkeys are paused. Both the process list and toggle are editable
in **Settings**.

## Repository manifest

Repositories publish an `ahk-library.toml` file. A repository may expose any
number of scripts:

```toml
[library]
name = "Example AutoHotkey Collection"
description = "Useful Windows automation scripts"
requires_autohotkey = ">=2.0"

[[scripts]]
id = "clipboard-tool"
name = "Clipboard Tool"
path = "scripts/clipboard-tool.ahk"
type = "include"
default_enabled = false

[[scripts]]
id = "standalone-monitor"
name = "Standalone Monitor"
path = "scripts/standalone-monitor.ahk"
type = "standalone"
default_enabled = false
```

`include` scripts are composed into one generated loader and require a trusted
repository. `standalone` scripts run in their own AutoHotkey process.

## Safety model

- Remote repositories are cloned into a manager-owned directory.
- Updates require an explicit user action and confirmation.
- A managed clone is validated before its revision becomes active.
- The previous managed revision is retained in state for rollback.
- Manifest and script paths must remain inside their repository.
- Include-mode code is allowed only from repositories marked trusted.
- The generated loader is syntax-checked with AutoHotkey before launch.

The manager cannot determine whether arbitrary automation code is benevolent.
Review third-party changes before enabling or launching them.

## License

MIT
