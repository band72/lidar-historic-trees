# Packaging: Linux and Windows installers

Installers are built by GitHub Actions (`.github/workflows/release.yml`):

- **Push a tag** `vX.Y.Z` → builds both installers, tests them, and publishes a GitHub Release.
- **Run the workflow manually** (Actions → *Release installers* → *Run workflow*) → builds and
  tests both, and uploads them as workflow artifacts without publishing a release.

| Output | Contents |
|---|---|
| `LiDAR-Tree-Inventory-<v>-windows-x64-setup.exe` | Inno Setup installer. Installs per user (no admin needed; "all users" can be picked), adds Start-menu and optional desktop shortcuts, can optionally add `fip` to PATH, and includes an uninstaller. |
| `LiDAR-Tree-Inventory-<v>-linux-x86_64.tar.gz` | The frozen program plus `install.sh`, which adds the `fip` command, a menu entry and an uninstaller. |

Both installers contain a PyInstaller bundle (`packaging/fip.spec`) with Python and every dependency included. The single `fip` program starts the web app when run with no arguments, and otherwise acts as the CLI.

## Build locally (Linux)

```bash
python3 -m venv /tmp/pkgvenv
/tmp/pkgvenv/bin/pip install open3d-cpu -r packaging/requirements-build.txt
/tmp/pkgvenv/bin/pip install --no-deps --no-build-isolation .
/tmp/pkgvenv/bin/python packaging/make_icon.py
/tmp/pkgvenv/bin/pyinstaller packaging/fip.spec --noconfirm
dist/lidar-tree-inventory/fip self-test
```

The Linux build uses `open3d-cpu`, because the default Open3D wheel bundles CUDA: 768 MB versus 218 MB for the library, and the pipeline only uses the CPU. The package is installed with `--no-deps` so pip doesn't also pull in the full `open3d`. On Windows, the regular `open3d` wheel is CPU-only already.

## Build locally (Windows)

You need Python 3.12, Visual Studio Build Tools (C++), CMake ≥ 3.28 and Inno Setup 6. Then:

```powershell
pip install open3d -r packaging\requirements-build.txt
pip install --no-deps --no-build-isolation .
python packaging\make_icon.py
pyinstaller packaging\fip.spec --noconfirm
dist\lidar-tree-inventory\fip.exe self-test
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" /DAppVersion=0.1.0 packaging\windows\installer.iss
```

## Checks each build runs

1. the full test suite on that OS
2. `fip self-test` on the frozen program: plugins, the demo pipeline with an accuracy check, exports, and a headless run of the web app
3. a CLI run that writes CSV, DXF and GeoJSON
4. the installer itself: a silent install, then `fip self-test` on the installed program, then uninstall
