"""Build ..\\Build\\Directory Snapshotter.exe (one file; run through the project's build.bat -> build-app.bat).

    python build.py <version>

The version (from the project's Website\\version.txt, or ..\\Website\\version.txt when none is given) is
bundled into the exe as version.txt - the update check reads it. The website download
(Directory-Snapshotter-V<version>.exe) is a copy made by package-app.bat.

Needs PyInstaller (python -m pip install --user pyinstaller). The exe's icon is
icon-injector/ds_appicon.ico, handed to PyInstaller here. It must NOT be put on
afterwards with rcedit: rcedit drops the program PyInstaller appends to a
one-file exe, leaving a ~380 KB exe that cannot start (which is what happened to
the V1.0.0 download). Without ds_appicon.ico, an icon is generated
(assets/app.ico) from the same shapes the app draws at runtime.
"""

import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE.parent / "Build"
VERSION_FILE = HERE.parent / "Website" / "version.txt"
sys.path.insert(0, str(HERE))
from directory_snapshotter import APP_NAME, icon_rects  # noqa: E402

ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def render_png(size):
    px = [[(0, 0, 0, 0)] * size for _ in range(size)]
    for color, x1, y1, x2, y2 in icon_rects("app", size):
        rgba = (int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16), 255)
        for y in range(max(0, y1), min(size, y2)):
            row = px[y]
            for x in range(max(0, x1), min(size, x2)):
                row[x] = rgba
    raw = b"".join(b"\x00" + bytes(c for p in row for c in p) for row in px)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def write_ico(path):
    images = [render_png(s) for s in ICON_SIZES]
    offset = 6 + 16 * len(images)
    header = struct.pack("<HHH", 0, 1, len(images))
    entries = b""
    for size, data in zip(ICON_SIZES, images):
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(header + entries + b"".join(images))


def main():
    if len(sys.argv) > 1:
        version = sys.argv[1]
    elif VERSION_FILE.is_file():
        version = VERSION_FILE.read_text(encoding="utf-8").strip()
    else:
        raise SystemExit("Give the version: python build.py 1.0.1")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"The version should look like 1.0.1 - it is {version!r}")
    print(f"Building {APP_NAME} V{version} ...")

    ico = HERE / "icon-injector" / "ds_appicon.ico"
    if not ico.is_file():
        ico = HERE / "assets" / "app.ico"
        write_ico(ico)
    work = Path(tempfile.mkdtemp(prefix="dsnap-build-"))
    try:
        (work / "version.txt").write_text(version + "\n", encoding="utf-8")
        subprocess.check_call([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
            "--name", APP_NAME, "--icon", str(ico),
            "--add-data", f"{work / 'version.txt'}{os.pathsep}.",
            "--distpath", str(work / "dist"), "--workpath", str(work / "build"), "--specpath", str(work),
            str(HERE / "directory_snapshotter.py"),
        ])
        BUILD.mkdir(exist_ok=True)
        out = BUILD / (APP_NAME + ".exe")
        shutil.copy2(work / "dist" / (APP_NAME + ".exe"), out)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(HERE / "__pycache__", ignore_errors=True)
    print(f"\nBuilt: {out}")


if __name__ == "__main__":
    main()
