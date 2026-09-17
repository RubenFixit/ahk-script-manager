#Requires AutoHotkey v2.0
#SingleInstance Force

global ManagerGui := 0
global ScriptList := 0
global StatusText := 0

A_TrayMenu.Delete()
A_TrayMenu.Add("Open Module Manager", (*) => ShowManager())
A_TrayMenu.Add("Activate Enabled Scripts", (*) => ActivateScripts())
A_TrayMenu.Add()
A_TrayMenu.Add("Exit", (*) => ExitApp())
A_TrayMenu.Default := "Open Module Manager"

InitializeManager()
ShowManager()

InitializeManager() {
    result := RunBackend("init")
    if !result.ok
        MsgBox(result.message, "AutoHotkey Module Manager", "Iconx")
}

ShowManager() {
    global ManagerGui, ScriptList, StatusText
    if IsObject(ManagerGui) {
        ManagerGui.Show()
        RefreshList()
        return
    }

    ManagerGui := Gui("+Resize", "AutoHotkey Module Manager")
    ManagerGui.SetFont("s10", "Segoe UI")
    ScriptList := ManagerGui.AddListView("xm ym w900 r18", ["Repo ID", "Script ID", "Repository", "Script", "Type", "Enabled", "Status", "Location"])
    ScriptList.ModifyCol(1, 0)
    ScriptList.ModifyCol(2, 0)
    ScriptList.ModifyCol(3, 150)
    ScriptList.ModifyCol(4, 210)
    ScriptList.ModifyCol(5, 85)
    ScriptList.ModifyCol(6, 75)
    ScriptList.ModifyCol(7, 155)
    ScriptList.ModifyCol(8, 220)
    ScriptList.OnEvent("DoubleClick", (*) => ToggleSelected())

    ManagerGui.AddButton("xm w90", "&Refresh").OnEvent("Click", (*) => RefreshList())
    ManagerGui.AddButton("x+8 w120", "&Add source").OnEvent("Click", (*) => AddSource())
    ManagerGui.AddButton("x+8 w90", "&Sync").OnEvent("Click", (*) => SyncSelected())
    ManagerGui.AddButton("x+8 w110", "&Enable/Disable").OnEvent("Click", (*) => ToggleSelected())
    ManagerGui.AddButton("x+8 w90", "&Activate").OnEvent("Click", (*) => ActivateScripts())
    ManagerGui.AddButton("x+8 w105", "&Launch script").OnEvent("Click", (*) => LaunchSelected())
    ManagerGui.AddButton("x+8 w95", "Open &folder").OnEvent("Click", (*) => OpenSelectedFolder())
    StatusText := ManagerGui.AddText("xm w900", "Ready")

    ManagerGui.OnEvent("Close", (*) => ManagerGui.Hide())
    ManagerGui.OnEvent("Size", ResizeManager)
    ManagerGui.Show("w930 h520")
    RefreshList()
}

ResizeManager(guiObj, minMax, width, height) {
    global ScriptList, StatusText
    if (minMax = -1)
        return
    ScriptList.Move(, , Max(400, width - 30), Max(180, height - 105))
    StatusText.Move(, height - 35, Max(400, width - 30))
}

BackendCommandPrefix(responsePath, tablePath := "") {
    backend := A_ScriptDir "\manager.py"
    dataDir := ManagerDataDir()
    command := 'uv run --script "' backend '" --data-dir "' dataDir '" --response "' responsePath '"'
    if (tablePath != "")
        command .= ' --table "' tablePath '"'
    return command
}

RunBackend(arguments, tablePath := "") {
    responsePath := A_Temp "\ahk-module-manager-response-" A_TickCount ".json"
    try FileDelete(responsePath)
    command := BackendCommandPrefix(responsePath, tablePath) " " arguments
    try exitCode := RunWait(command, A_ScriptDir, "Hide")
    catch Error as err {
        return {ok: false, message: "Could not start the Python backend. Install uv and ensure it is on PATH.`n`n" err.Message}
    }
    if !FileExist(responsePath)
        return {ok: false, message: "The Python backend did not return a response."}
    payload := FileRead(responsePath, "UTF-8")
    try FileDelete(responsePath)
    ok := RegExMatch(payload, '"ok"\s*:\s*true')
    message := "Backend command failed."
    if RegExMatch(payload, 's)"message"\s*:\s*"((?:\\.|[^"\\])*)"', &match)
        message := JsonUnescape(match[1])
    repoId := ""
    if RegExMatch(payload, 's)"repo_id"\s*:\s*"((?:\\.|[^"\\])*)"', &repoMatch)
        repoId := JsonUnescape(repoMatch[1])
    return {ok: !!ok, message: message, repoId: repoId, exitCode: exitCode}
}

JsonUnescape(value) {
    value := StrReplace(value, "\n", "`n")
    value := StrReplace(value, "\r", "`r")
    value := StrReplace(value, "\t", "`t")
    value := StrReplace(value, '\"', '"')
    value := StrReplace(value, "\\", "\")
    return value
}

QuoteArg(value) {
    return '"' StrReplace(value, '"', '\"') '"'
}

SetStatus(message, isError := false) {
    global StatusText
    if IsObject(StatusText)
        StatusText.Text := message
    if isError
        MsgBox(message, "AutoHotkey Module Manager", "Iconx")
}

RefreshList() {
    global ScriptList
    tablePath := A_Temp "\ahk-module-manager-list-" A_TickCount ".tsv"
    try FileDelete(tablePath)
    result := RunBackend("list", tablePath)
    if !result.ok {
        SetStatus(result.message, true)
        return
    }
    ScriptList.Delete()
    if FileExist(tablePath) {
        lines := StrSplit(Trim(FileRead(tablePath, "UTF-8"), "`r`n"), "`n")
        for index, line in lines {
            if (index = 1 || Trim(line) = "")
                continue
            fields := StrSplit(Trim(line, "`r"), "`t")
            while fields.Length < 8
                fields.Push("")
            ScriptList.Add(, fields*)
        }
        try FileDelete(tablePath)
    }
    SetStatus(result.message)
}

SelectedRow(requireScript := false) {
    global ScriptList
    row := ScriptList.GetNext()
    if !row {
        MsgBox("Select a repository or script first.", "AutoHotkey Module Manager", "Icon!")
        return 0
    }
    info := {
        row: row,
        repoId: ScriptList.GetText(row, 1),
        scriptId: ScriptList.GetText(row, 2),
        enabled: ScriptList.GetText(row, 6),
        path: ScriptList.GetText(row, 8)
    }
    if requireScript && info.scriptId = "" {
        MsgBox("The selected repository has no available script entry.", "AutoHotkey Module Manager", "Icon!")
        return 0
    }
    return info
}

AddSource() {
    urlResult := InputBox("Git URL or local folder containing the module collection:", "Add source", "w600")
    if urlResult.Result != "OK"
        return
    refResult := InputBox("Branch, tag, or commit. Leave blank to use the remote default:", "Add source")
    if refResult.Result != "OK"
        return
    manifestResult := InputBox("Manifest path inside the source repository:", "Add source",, "ahk-library.toml")
    if manifestResult.Result != "OK"
        return

    isLocal := DirExist(urlResult.Value)
    prompt := "Trust this module source for include mode?`n`nOnly choose Yes for code you trust; included scripts share one AutoHotkey process."
    trusted := MsgBox(prompt, "Module source trust", "YesNo Icon?") = "Yes"
    arguments := "add-url " QuoteArg(urlResult.Value)
    if refResult.Value != ""
        arguments .= " --ref " QuoteArg(refResult.Value)
    arguments .= " --manifest " QuoteArg(manifestResult.Value)
    if isLocal
        arguments .= " --local"
    if trusted
        arguments .= " --trusted"
    result := RunBackend(arguments)
    SetStatus(result.message, !result.ok)
    if result.ok {
        syncResult := RunBackend("sync " QuoteArg(result.repoId))
        SetStatus(syncResult.message, !syncResult.ok)
        RefreshList()
    }
}

SyncSelected() {
    info := SelectedRow()
    if !IsObject(info)
        return
    if MsgBox("Synchronize " info.repoId " now? A managed clone will move to its configured revision after validation.", "Synchronize repository", "YesNo Icon?") != "Yes"
        return
    result := RunBackend("sync " QuoteArg(info.repoId))
    SetStatus(result.message, !result.ok)
    RefreshList()
}

ToggleSelected() {
    info := SelectedRow(true)
    if !IsObject(info)
        return
    command := info.enabled = "yes" ? "disable" : "enable"
    result := RunBackend(command " " QuoteArg(info.repoId) " " QuoteArg(info.scriptId))
    SetStatus(result.message, !result.ok)
    RefreshList()
}

ActivateScripts() {
    result := RunBackend("activate")
    SetStatus(result.message, !result.ok)
}

LaunchSelected() {
    info := SelectedRow(true)
    if !IsObject(info)
        return
    result := RunBackend("launch " QuoteArg(info.repoId) " " QuoteArg(info.scriptId))
    SetStatus(result.message, !result.ok)
}

OpenSelectedFolder() {
    info := SelectedRow()
    if !IsObject(info)
        return
    if DirExist(info.path)
        Run('explorer.exe "' info.path '"')
    else
        MsgBox("Repository directory does not exist yet.", "AutoHotkey Module Manager", "Icon!")
}

ManagerDataDir() {
    localAppData := EnvGet("LOCALAPPDATA")
    currentDir := localAppData "\AhkModuleManager"
    legacyDir := localAppData "\AhkRepoManager"
    if !DirExist(currentDir) && DirExist(legacyDir) {
        ; Copy instead of moving because an active generated loader may still
        ; have files open in the legacy directory during an upgrade.
        try DirCopy(legacyDir, currentDir, true)
    }
    return currentDir
}
