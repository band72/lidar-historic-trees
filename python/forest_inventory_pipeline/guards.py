"""
Checks before processing: separately stitched clouds loaded together, and too little memory.
"""

from __future__ import annotations

from pathlib import Path

from .errors import InputDataError

def _file_extent(path: Path, data) -> tuple[float, float, float, float] | None:
    """A file's whole XY extent (file units): the LAS header (even when cropped while
    reading), else the points as read."""
    if path.suffix.lower() in (".las", ".laz"):
        try:
            import laspy

            with laspy.open(str(path)) as fh:
                lo, hi = fh.header.mins, fh.header.maxs
            return float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])
        except Exception:
            pass
    if data is None or data.xyz is None or len(data.xyz) == 0:
        return None
    lo, hi = data.xyz[:, :2].min(axis=0), data.xyz[:, :2].max(axis=0)
    return float(lo[0]), float(lo[1]), float(hi[0]), float(hi[1])


def _las_crs(path: Path) -> str | None:
    try:
        import laspy

        from .crs import from_las_header

        with laspy.open(str(path)) as fh:
            info = from_las_header(fh.header)
        return info.crs or info.wkt or None
    except Exception:
        return None


def separate_frames(extents) -> None:
    """
    Refuse several files that look like separate SLAM/odometry runs, each in its own frame.

    A run stitched by lidar odometry (KISS-ICP, FAST-LIO, ...) is in local coordinates with
    the origin at its first scan, and declares no coordinate system. Several such files loaded
    together stack unrelated places on top of each other (2022-03-08: six drive segments,
    233 M points, extent 556 x 305 m, and an out-of-memory kill). So when two or more files
    have no coordinate system and every one's extent contains the origin, loading stops.

    :param extents: [(path, (xmin, ymin, xmax, ymax) or None, crs or None)]
    :raises InputDataError: with what to do instead.
    """
    if len(extents) < 2 or any(crs for _, _, crs in extents):
        return
    def has_origin(e):
        return e is not None and e[0] <= 1.0 and e[2] >= -1.0 and e[1] <= 1.0 and e[3] >= -1.0

    if all(has_origin(e) for _, e, _ in extents):
        names = ", ".join(p.name for p, _, _ in extents[:4]) + (" ..." if len(extents) > 4 else "")
        raise InputDataError(
            f"{len(extents)} files ({names}) each have their own coordinates starting at "
            "(0, 0) and no coordinate system: they look like separate SLAM/KISS-ICP runs, "
            "which do not line up. Use the app's 'Whole drive' panel or `fip drive`: it "
            "measures each file on its own, then combines the results (fip georeference, "
            "fip merge). If they really share one frame, use --same-frame (command line)."
        )


#: peak memory of the stages per input point (bytes). 2022-03-08 drive: 6.9 GB for 44 M points
#: (157 B); a 233 M point run was killed while clustering, holding 40.6 GB and having asked
#: for 51 GB (219 B): the peak lies above that, so the estimate uses 220
BYTES_PER_POINT = 220


def available_memory_bytes() -> int | None:
    """Memory available to new allocations (Linux /proc/meminfo, Windows API); None if unknown."""
    import sys

    try:
        if sys.platform.startswith("linux"):
            with open("/proc/meminfo", encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) * 1024
        elif sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return int(st.ullAvailPhys)
    except (OSError, ValueError, AttributeError):
        return None
    return None


def check_memory(n_points: int) -> None:
    """
    Stop before processing ``n_points`` when the estimated peak cannot fit in the memory
    available now (instead of being killed by the system minutes later).

    :raises InputDataError: with the estimate, the memory available, and what to do instead.
    """
    free = available_memory_bytes()
    if free is None:
        return
    need = n_points * BYTES_PER_POINT
    if need > free:
        gb = 1e9
        raise InputDataError(
            f"{n_points / 1e6:.0f} M points need about {need / gb:.0f} GB of memory, but only "
            f"{free / gb:.0f} GB is available. Process fewer files at once (e.g. one segment), "
            "crop the cloud (--crop), or process it in tiles (command line: --tile 100). "
            "Closing other programs frees memory too. To run anyway: --no-memory-check."
        )


def peak_memory_bytes() -> int | None:
    """Peak memory of this process so far (Linux, macOS, Windows); None if unknown."""
    import sys

    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD)] + [
                    (n, ctypes.c_size_t) for n in (
                        "PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                        "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                        "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]

            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            psapi = ctypes.windll.psapi
            psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            if psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                return int(pmc.PeakWorkingSetSize)
            return None
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)
    except Exception:
        return None
