# Windows desktop development

The repository's primary desktop target is Windows x64. The native window uses pywebview and Microsoft Edge WebView2. For UI work, the normal backend-plus-Vite workflow in the root README avoids packaging.

To run the native development launcher after starting the backend and building the frontend, install the Windows-specific packages from `desktop/requirements-win-amd64-cp314.in` in a suitable isolated environment and review `desktop/launcher.py`. The release lock targets the currently validated Windows x64 CPython 3.14.3 build environment; it is a release-toolchain lock, not a claim that the application source only supports that Python version.

`desktop/scholarflow.spec`, `desktop/installer.iss`, `desktop/release_tool.py`, and the release scripts are retained as source. Creating a distributable installer requires the full locked wheelhouse, Node.js, Python, PyInstaller, and Inno Setup. The complete offline build cache is intentionally not included in this source archive.

The dependency review records an unresolved license-text discrepancy for `proxy-tools 0.1.0` (package metadata says MIT while its source header refers to BSD and a missing license file). Resolve this upstream issue and review all actual bundled licenses before distributing generated binaries.
