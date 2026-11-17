param(
    [Parameter(Mandatory = $true)][string]$RuntimeDirectory,
    [Parameter(Mandatory = $true)][string]$ScriptPath
)

# Compile/inspect bytecode using the exact CPython DLL shipped with ScholarFlow.
# Run this host in its own process: no application modules or user data are loaded.
$ErrorActionPreference = 'Stop'
$runtimeRoot = (Resolve-Path -LiteralPath $RuntimeDirectory).Path
$workerPath = (Resolve-Path -LiteralPath $ScriptPath).Path
$workerCode = [System.IO.File]::ReadAllText($workerPath, [System.Text.Encoding]::UTF8)

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class ScholarFlowPython313 {
    [DllImport("kernel32", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr LoadLibraryExW(string path, IntPtr handle, uint flags);
    [DllImport("kernel32", CharSet=CharSet.Ansi)]
    static extern IntPtr GetProcAddress(IntPtr library, string name);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    delegate void SetWide([MarshalAs(UnmanagedType.LPWStr)] string value);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    delegate void Initialize(int installSignals);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    delegate int RunCode([MarshalAs(UnmanagedType.LPUTF8Str)] string code, IntPtr flags);
    [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
    delegate int Finalize();

    static IntPtr Export(IntPtr library, string name) {
        IntPtr result = GetProcAddress(library, name);
        if (result == IntPtr.Zero) throw new Exception("Missing Python export: " + name);
        return result;
    }
    public static int Run(string root, string code) {
        IntPtr library = LoadLibraryExW(root + "\\python313.dll", IntPtr.Zero, 8);
        if (library == IntPtr.Zero)
            throw new Exception("Cannot load bundled Python: " + Marshal.GetLastWin32Error());
        foreach (string flag in new [] {
            "Py_IsolatedFlag", "Py_NoSiteFlag", "Py_IgnoreEnvironmentFlag", "Py_DontWriteBytecodeFlag"
        }) Marshal.WriteInt32(Export(library, flag), 1);
        var setPath = (SetWide)Marshal.GetDelegateForFunctionPointer(Export(library, "Py_SetPath"), typeof(SetWide));
        setPath(root + "\\base_library.zip;" + root);
        var initialize = (Initialize)Marshal.GetDelegateForFunctionPointer(Export(library, "Py_InitializeEx"), typeof(Initialize));
        var run = (RunCode)Marshal.GetDelegateForFunctionPointer(Export(library, "PyRun_SimpleStringFlags"), typeof(RunCode));
        var finalize = (Finalize)Marshal.GetDelegateForFunctionPointer(Export(library, "Py_FinalizeEx"), typeof(Finalize));
        initialize(0);
        int result = run(code, IntPtr.Zero);
        int finalResult = finalize();
        return result == 0 ? finalResult : result;
    }
}
'@

$result = [ScholarFlowPython313]::Run($runtimeRoot, $workerCode)
if ($result -ne 0) { throw "Bundled Python worker failed with code $result" }
