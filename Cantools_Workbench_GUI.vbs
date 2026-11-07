Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
rootDir = fso.GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = rootDir
pythonwPath = rootDir & "\.venv\Scripts\pythonw.exe"
guiScript = rootDir & "\tools\cantools_gui.py"

If fso.FileExists(pythonwPath) Then
    shell.Run """" & pythonwPath & """ """ & guiScript & """", 0, False
Else
    MsgBox "未找到虚拟环境: " & pythonwPath, 16, "Cantools Workbench"
End If
