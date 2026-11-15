# PyInstaller spec for the LiDAR Tree Inventory program (CLI + web app).
# Build from the repository root:  pyinstaller packaging/fip.spec --noconfirm
# Output: dist/lidar-tree-inventory/ containing the `fip` executable.
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH is injected by PyInstaller
PKG = ROOT / "python" / "forest_inventory_pipeline"

datas, binaries, hiddenimports = [], [], []

# packages with data files, dynamic imports or native libraries
for name in ("streamlit", "plotly", "ezdxf", "rosbags", "open3d", "CSF", "laspy", "lazrs"):
    try:
        d, b, h = collect_all(name)
    except Exception as exc:  # optional package missing: build without it
        print(f"[fip.spec] skipping {name}: {exc}")
        continue
    datas += d
    binaries += b
    hiddenimports += h

for name in ("streamlit", "plotly", "typer", "rich", "rosbags", "ezdxf", "open3d", "forest_inventory_pipeline"):
    try:
        datas += copy_metadata(name)
    except Exception:
        pass

# plugins are discovered with pkgutil at runtime, so collect every submodule explicitly
hiddenimports += collect_submodules("forest_inventory_pipeline")
hiddenimports += collect_submodules("rich")
hiddenimports += collect_submodules("sklearn.cluster")
hiddenimports += ["scipy.spatial.transform", "scipy.stats", "pandas", "yaml"]

# Streamlit runs the app from a real .py file on disk
datas += [(str(PKG / "app" / "streamlit_app.py"), "forest_inventory_pipeline/app")]
# example plugins and config shipped next to the program
datas += [(str(ROOT / "plugins"), "plugins"), (str(ROOT / "config"), "config")]

icon = ROOT / "packaging" / "assets" / ("icon.ico" if sys.platform == "win32" else "icon.png")

a = Analysis(  # noqa: F821
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[],  # use the installed package (it contains the compiled quickshift++ extension)
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "IPython", "jupyter", "notebook", "pytest"],
    noarchive=False,
)
# drop Open3D parts the pipeline never uses (TensorFlow/PyTorch ops, examples): ~80 MB
_DROP = ("open3d_tf_ops", "open3d_torch_ops", "open3d/examples", "open3d\\examples")
a.binaries = [b for b in a.binaries if not any(k in b[0] for k in _DROP)]
a.datas = [d for d in a.datas if not any(k in d[0] for k in _DROP)]

pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="fip",
    console=True,  # the console shows the server log; closing it stops the app
    icon=str(icon) if icon.exists() else None,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="lidar-tree-inventory")  # noqa: F821
