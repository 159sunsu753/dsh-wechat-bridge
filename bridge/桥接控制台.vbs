' 桥接控制台.vbs - open bridge_control.pyw without a console window.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
pythonw = sh.ExpandEnvironmentStrings("%USERPROFILE%") & "\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\pythonw.exe"
If Not fso.FileExists(pythonw) Then pythonw = "pythonw.exe"
sh.Run """" & pythonw & """ """ & dir & "\bridge_control.pyw""", 0, False
