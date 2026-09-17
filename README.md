# AutoHotkey Repository Manager

AutoHotkey Repository Manager is a Windows desktop manager for AutoHotkey v2
scripts distributed across Git repositories. One repository may publish many
independently selectable scripts through an `ahk-library.toml` manifest.

The application uses:

- `manager.ahk` for the tray menu, management window, and script lifecycle.
- `manager.py` for Git operations, TOML parsing, validation, loader generation,
  and rollback state.
- Git for host-independent repository synchronization, including GitHub,
  GitLab, and self-hosted servers.
- `uv` to run the Python backend with the declared Python version.

## Requirements

- Windows
- AutoHotkey v2
- Git
- uv

## Run

Run `manager.ahk` with AutoHotkey v2. Its machine-local catalog, managed clones,
generated loader, and rollback state are stored under
`%LOCALAPPDATA%\AhkRepoManager`.

Use **Add repository** to register either a Git URL or an existing local clone.
Choose trust only for repositories whose scripts may be combined into the
generated `#Include` loader. Third-party scripts should normally use
`type = "standalone"` in their manifest.

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

