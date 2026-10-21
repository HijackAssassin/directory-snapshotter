# Directory Snapshotter

A Windows app that takes a "screenshot" of your folders: it records the tree of folder and file names (with each
file's size and date) without copying any file contents. Snapshots are small files you can save to several drives
at once, then browse later in a built-in explorer view - or re-create on disk as a tree of empty placeholder files -
so you always know what you had.

**Download:** https://teamwolfgamers.online/directory-snapshotter

The app checks that page for a newer version each time it starts (pattywatty.duckdns.org is the backup address).

## Running from source

Python 3 on Windows (only the standard library): `python directory_snapshotter.py`, or `Directory Snapshotter.bat`
to start it without a console window.

## Building

`python build.py <version>` builds `..\Build\Directory Snapshotter.exe` (one file) with PyInstaller
(`python -m pip install pyinstaller`). The snapshot format (`.dsnap`) is described at the top of
`directory_snapshotter.py`.

## More

Made by Patrick - more apps and tools at https://teamwolfgamers.online
