"""Directory Snapshotter

Takes a "figurative screenshot" of folders: records the tree of folder and file
names (plus original size / modified time as metadata) without copying any file
contents. Snapshots are small compressed files that can be saved to several
drives at once and browsed later in a built-in explorer view, or re-created on
disk as a tree of empty 0-byte placeholder files.

Snapshot file format (.dsnap): gzip-compressed UTF-8 text with two lines.
  line 1: header JSON  (name, created, computer, roots with settings + stats)
  line 2: body JSON    {"trees": [node, ...]}  one tree per root folder
  node:   {"n": name, "m": mtime, "d": [child nodes], "f": [[name, size, mtime], ...],
           "x": 1 if access denied, "u": 1 if not scanned, "l": 1 if a link/junction}
Keys starting with "_" are computed after loading (folder totals) and never saved.
"""

import fnmatch
import gzip
import json
import os
import queue
import re
import socket
import stat
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path

import tkinter as tk
import tkinter.font as tkfont
from tkinter import filedialog, messagebox, ttk

import update_check

APP_NAME = "Directory Snapshotter"
EXT = ".dsnap"
FORMAT_ID = "directory-snapshot"
FORMAT_VERSION = 1
CONFIG_DIR = Path(os.environ.get("APPDATA") or Path.home()) / "DirectorySnapshotter"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULT_SAVE_DIR = Path.home() / "Documents" / "Directory Snapshots"
SEARCH_LIMIT = 2000
GRID_ICON_SIZES = (48, 96, 160)     # zoom level 0 = tree view; 1..3 = icon view at these sizes
UI_FONT_SIZE = 10
INVALID_NAME_CHARS = '<>:"/\\|?*'


# ---------------------------------------------------------------- helpers

def human_size(n):
    if n is None:
        return ""
    if n < 1024:
        return f"{n:,} bytes"
    for unit in ("KB", "MB", "GB", "TB"):
        n /= 1024
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}"


def fmt_time(ts):
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def safe_name(name):
    cleaned = "".join("_" if c in INVALID_NAME_CHARS or ord(c) < 32 else c for c in name).strip(" .")
    return cleaned or "Snapshot"


def long_path(p):
    """Prefix Windows paths so deep trees don't hit the 260-char limit."""
    p = os.path.abspath(str(p))
    if os.name != "nt" or p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def unique_path(folder, name):
    base = Path(folder) / f"{safe_name(name)}{EXT}"
    i = 2
    while base.exists():
        base = Path(folder) / f"{safe_name(name)} ({i}){EXT}"
        i += 1
    return base


def plural(n, word):
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def node_name(n):
    return n[0] if isinstance(n, list) else n["n"]


def chain_path(chain):
    return os.path.join(chain[0]["n"], *(node_name(n) for n in chain[1:])) if chain else ""


def same_chain(a, b):
    return len(a) == len(b) and all(x is y for x, y in zip(a, b))


# ---------------------------------------------------------------- model

@dataclass
class Source:
    path: str
    subfolders: bool = True
    max_depth: int = 0      # levels of subfolders to scan; 0 = unlimited
    files: bool = True      # False = record folders only
    hidden: bool = False    # include hidden/system items
    exclude: str = ""       # comma-separated name patterns, e.g. "node_modules, *.tmp"

    @classmethod
    def from_dict(cls, d):
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})

    def describe(self):
        if not self.subfolders:
            return "Top level only"
        if self.max_depth:
            return f"{self.max_depth} level{'s' if self.max_depth != 1 else ''}"
        return "All"


class Cancelled(Exception):
    pass


def _is_hidden(entry):
    if entry.name.startswith("."):
        return True
    try:
        attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return False
    return bool(attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))


def scan_source(src, progress, cancel):
    """Walk one source folder and return (tree, stats). Never reads file contents."""
    root_path = os.path.abspath(src.path)
    patterns = [p.strip().lower() for p in src.exclude.split(",") if p.strip()]
    limit = 1 if not src.subfolders else (src.max_depth or None)
    stats = {"folders": 0, "files": 0, "bytes": 0, "errors": 0}
    try:
        root_mtime = int(os.stat(root_path).st_mtime)
    except OSError:
        root_mtime = 0
    root = {"n": root_path, "m": root_mtime, "d": [], "f": []}
    stack = [(root_path, root, 0)]
    last_report = 0.0

    while stack:
        if cancel.is_set():
            raise Cancelled
        path, node, depth = stack.pop()
        try:
            entries = os.scandir(path)
        except OSError:
            stats["errors"] += 1
            node["x"] = 1
            continue
        with entries:
            for e in entries:
                try:
                    if not src.hidden and _is_hidden(e):
                        continue
                    if patterns and any(fnmatch.fnmatch(e.name.lower(), p) for p in patterns):
                        continue
                    is_link = e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction())
                    if e.is_dir():
                        st = e.stat(follow_symlinks=False)
                        child = {"n": e.name, "m": int(st.st_mtime), "d": [], "f": []}
                        node["d"].append(child)
                        stats["folders"] += 1
                        if is_link:
                            child["l"] = 1      # don't follow links/junctions (avoids loops)
                        elif limit is not None and depth + 1 >= limit:
                            child["u"] = 1      # name recorded, contents not scanned
                        else:
                            stack.append((e.path, child, depth + 1))
                    elif src.files:
                        st = e.stat(follow_symlinks=False)
                        node["f"].append([e.name, st.st_size, int(st.st_mtime)])
                        stats["files"] += 1
                        stats["bytes"] += st.st_size
                except OSError:
                    stats["errors"] += 1
        now = time.monotonic()
        if now - last_report > 0.1:
            last_report = now
            progress(dict(stats), path)

    progress(dict(stats), root_path)
    return root, stats


def compute_folder_totals(trees):
    """Annotate every folder with recursive totals: _s bytes, _nf files, _nd folders,
    _p True when some contents are unknown (access denied / not scanned)."""
    for root in trees:
        stack = [(root, False)]
        while stack:
            node, children_done = stack.pop()
            dirs = node.get("d", ())
            if not children_done:
                stack.append((node, True))
                stack.extend((d, False) for d in dirs)
                continue
            files = node.get("f", ())
            size = sum(f[1] for f in files)
            nf, nd = len(files), len(dirs)
            partial = bool(node.get("x") or node.get("u"))
            for d in dirs:
                size += d["_s"]
                nf += d["_nf"]
                nd += d["_nd"]
                partial = partial or d["_p"]
            node["_s"], node["_nf"], node["_nd"], node["_p"] = size, nf, nd, partial


def build_snapshot_bytes(header, trees):
    text = json.dumps(header, ensure_ascii=False) + "\n" + json.dumps(
        {"trees": trees}, ensure_ascii=False, separators=(",", ":"))
    return gzip.compress(text.encode("utf-8", "surrogatepass"), compresslevel=6)


def write_bytes_atomic(path, data):
    tmp = Path(str(path) + ".part")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def read_header(path):
    with gzip.open(path, "rb") as f:
        header = json.loads(f.readline().decode("utf-8", "surrogatepass"))
    if header.get("format") != FORMAT_ID:
        raise ValueError("Not a Directory Snapshotter file")
    return header


def read_snapshot(path):
    with gzip.open(path, "rb") as f:
        header = json.loads(f.readline().decode("utf-8", "surrogatepass"))
        if header.get("format") != FORMAT_ID:
            raise ValueError("Not a Directory Snapshotter file")
        body = json.loads(f.read().decode("utf-8", "surrogatepass"))
    return header, body["trees"]


def export_root_names(trees, full_path):
    """Relative folder each snapshot root is recreated under.
    Default:        C:\\Users\\Patrick\\Desktop\\testing -> testing
    full_path=True: C:\\Users\\Patrick\\Desktop\\testing -> C Volume\\Users\\Patrick\\Desktop\\testing"""
    names, used = [], set()
    for root in trees:
        drive, rest = os.path.splitdrive(root["n"])
        if drive.startswith(("\\\\", "//")):
            volume = " ".join(p for p in re.split(r"[\\/]", drive) if p)   # \\server\share -> "server share"
        elif drive:
            volume = drive.rstrip(":") + " Volume"
        else:
            volume = "Root"
        parts = [p for p in re.split(r"[\\/]", rest) if p]
        rel = Path(volume, *parts) if full_path else Path(parts[-1] if parts else volume)
        candidate, i = rel, 2
        while str(candidate).lower() in used:
            candidate = rel.with_name(f"{rel.name} ({i})")
            i += 1
        used.add(str(candidate).lower())
        names.append(candidate)
    return names


def export_empty_tree(trees, base, full_path, progress, cancel):
    """Re-create the snapshot on disk as real folders with 0-byte files."""
    base = Path(base)
    done = errors = 0
    last = 0.0
    for root, rel in zip(trees, export_root_names(trees, full_path)):
        stack = [(base / rel, root)]
        while stack:
            if cancel.is_set():
                raise Cancelled
            dest, node = stack.pop()
            try:
                os.makedirs(long_path(dest), exist_ok=True)
            except OSError:
                errors += 1
                continue
            for name, _size, mtime in node.get("f", []):
                fp = long_path(dest / name)
                try:
                    open(fp, "wb").close()
                    if mtime:
                        os.utime(fp, (mtime, mtime))
                except OSError:
                    errors += 1
                done += 1
            for d in node.get("d", []):
                stack.append((dest / d["n"], d))
                done += 1
            now = time.monotonic()
            if now - last > 0.1:
                last = now
                progress(done)
    return done, errors


# ---------------------------------------------------------------- config

def load_config():
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    cfg.setdefault("sources", [])
    cfg.setdefault("save_dirs", [str(DEFAULT_SAVE_DIR)])
    cfg.setdefault("known_snapshots", [])
    cfg.setdefault("export_dir", str(Path.home() / "Downloads"))
    cfg.setdefault("export_full_path", False)
    return cfg


def save_config(cfg):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- icons

# Shapes on a 16-unit grid: ((x1, y1, x2, y2), fill, border). Drawn at any pixel size.
ICON_SHAPES = {
    "folder": [((1, 3, 7, 6), "#E9B444", "#C98A22"), ((1, 5, 15, 14), "#F5C451", "#C98A22")],
    "file": [((3, 1, 13, 15), "#FFFFFF", "#8A8F98"), ((5.5, 5, 10.5, 6), "#C5CAD1", None),
             ((5.5, 8, 10.5, 9), "#C5CAD1", None), ((5.5, 11, 9, 12), "#C5CAD1", None)],
    "drive": [((1, 4, 15, 13), "#AEB6C1", "#5B6470"), ((11, 9, 13, 11), "#3FB950", None)],
    "app": [((0.5, 2, 7, 5), "#E9B444", "#B7791F"), ((0.5, 4, 15.5, 14), "#F5C451", "#B7791F"),
            ((6, 8, 15.5, 15.5), "#2F6FEB", "#1D4ED8"), ((9, 10, 12.5, 13.5), "#FFFFFF", None),
            ((7, 9, 8.5, 10), "#BFD3FF", None)],
}


def icon_rects(kind, size):
    """Pixel rectangles (color, x1, y1, x2, y2) for an icon at `size` px, in draw order."""
    s = size / 16
    bw = max(1, round(s * 0.75))
    out = []
    for (x1, y1, x2, y2), fill, border in ICON_SHAPES[kind]:
        r = [round(x1 * s), round(y1 * s), round(x2 * s), round(y2 * s)]
        r[2], r[3] = max(r[2], r[0] + 1), max(r[3], r[1] + 1)
        if border:
            out.append((border, *r))
            if r[2] - r[0] > 2 * bw and r[3] - r[1] > 2 * bw:
                out.append((fill, r[0] + bw, r[1] + bw, r[2] - bw, r[3] - bw))
        else:
            out.append((fill, *r))
    return out


def make_icon(master, kind, size):
    img = tk.PhotoImage(master=master, width=size, height=size)
    for color, x1, y1, x2, y2 in icon_rects(kind, size):
        img.put(color, to=(x1, y1, x2, y2))
    return img


# ---------------------------------------------------------------- dialogs

def center_on(win, parent):
    win.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - win.winfo_width()) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - win.winfo_height()) // 3
    win.geometry(f"+{max(x, 0)}+{max(y, 0)}")


class SourceDialog(tk.Toplevel):
    def __init__(self, parent, src):
        super().__init__(parent)
        self.title("Folder settings")
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        self.src = src
        self.v_sub = tk.BooleanVar(value=src.subfolders)
        self.v_depth = tk.StringVar(value=str(src.max_depth))
        self.v_files = tk.BooleanVar(value=src.files)
        self.v_hidden = tk.BooleanVar(value=src.hidden)
        self.v_excl = tk.StringVar(value=src.exclude)

        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=src.path, style="Heading.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        ttk.Checkbutton(f, text="Include subfolders", variable=self.v_sub, command=self._sync).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Subfolder levels to scan (0 = all):").grid(row=2, column=0, sticky="w", padx=(22, 8), pady=4)
        self.spin = ttk.Spinbox(f, from_=0, to=999, width=6, textvariable=self.v_depth)
        self.spin.grid(row=2, column=1, sticky="w")
        ttk.Checkbutton(f, text="Include files (off = record folders only)", variable=self.v_files).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))
        ttk.Checkbutton(f, text="Include hidden and system items", variable=self.v_hidden).grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Skip items named (comma-separated, wildcards allowed):").grid(row=5, column=0, columnspan=2, sticky="w", pady=(12, 2))
        ttk.Entry(f, textvariable=self.v_excl, width=52).grid(row=6, column=0, columnspan=2, sticky="we")
        ttk.Label(f, text="e.g.  node_modules, .git, *.tmp", style="Muted.TLabel").grid(row=7, column=0, columnspan=2, sticky="w")
        btns = ttk.Frame(f)
        btns.grid(row=8, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(btns, text="OK", command=self._ok, default="active").pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left")
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self._sync()
        center_on(self, parent)
        self.grab_set()
        self.wait_window()

    def _sync(self):
        self.spin.state(["!disabled"] if self.v_sub.get() else ["disabled"])

    def _ok(self):
        try:
            depth = max(0, int(self.v_depth.get() or 0))
        except ValueError:
            messagebox.showerror("Folder settings", "Subfolder levels must be a whole number.", parent=self)
            return
        self.result = Source(self.src.path, self.v_sub.get(), depth, self.v_files.get(),
                             self.v_hidden.get(), self.v_excl.get().strip())
        self.destroy()


class ExportDialog(tk.Toplevel):
    """Asks where to recreate the tree, what to call the parent folder, and whether to keep full paths."""

    def __init__(self, parent, header, trees, initial_dir, full_path):
        super().__init__(parent)
        self.title("Recreate as 0-byte folder tree")
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        self.trees = trees
        self.v_dir = tk.StringVar(value=initial_dir)
        self.v_name = tk.StringVar(value=safe_name(header.get("name", "Snapshot")))
        self.v_full = tk.BooleanVar(value=full_path)
        tot = header.get("totals", {})
        count = tot.get("folders", 0) + tot.get("files", 0)

        f = ttk.Frame(self, padding=16)
        f.pack(fill="both", expand=True)
        f.columnconfigure(0, weight=1)
        ttk.Label(f, text="Create inside:").grid(row=0, column=0, sticky="w")
        row = ttk.Frame(f)
        row.grid(row=1, column=0, columnspan=2, sticky="we", pady=(2, 10))
        row.columnconfigure(0, weight=1)
        ttk.Entry(row, textvariable=self.v_dir, width=60).grid(row=0, column=0, sticky="we")
        ttk.Button(row, text="Browse…", command=self._browse).grid(row=0, column=1, padx=(6, 0))
        ttk.Label(f, text="Parent folder name:").grid(row=2, column=0, sticky="w")
        name_entry = ttk.Entry(f, textvariable=self.v_name, width=60)
        name_entry.grid(row=3, column=0, columnspan=2, sticky="we", pady=(2, 10))
        ttk.Checkbutton(f, text="Include each folder's full original path (e.g. C Volume\\Users\\…\\testing)",
                        variable=self.v_full).grid(row=4, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="Will create:", style="Heading.TLabel").grid(row=5, column=0, sticky="w", pady=(14, 2))
        self.v_preview = tk.StringVar()
        ttk.Label(f, textvariable=self.v_preview, style="Muted.TLabel", justify="left",
                  wraplength=560).grid(row=6, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text=f"{count:,} empty folders and 0-byte files. No real file contents are involved.",
                  style="Muted.TLabel").grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))
        btns = ttk.Frame(f)
        btns.grid(row=8, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(btns, text="Create", command=self._ok, default="active").pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="left")
        for v in (self.v_dir, self.v_name, self.v_full):
            v.trace_add("write", lambda *_: self._update_preview())
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self._update_preview()
        center_on(self, parent)
        name_entry.focus_set()
        name_entry.select_range(0, "end")
        self.grab_set()
        self.wait_window()

    def _browse(self):
        d = filedialog.askdirectory(parent=self, title="Choose where to recreate the folder tree",
                                    initialdir=self.v_dir.get() or None)
        if d:
            self.v_dir.set(os.path.normpath(d))

    def _update_preview(self):
        base = Path(self.v_dir.get().strip() or ".") / (self.v_name.get().strip() or "…")
        rels = export_root_names(self.trees, self.v_full.get())
        lines = [str(base / r) for r in rels[:4]]
        if len(rels) > 4:
            lines.append(f"…and {len(rels) - 4} more")
        self.v_preview.set("\n".join(lines) or str(base))

    def _ok(self):
        target = self.v_dir.get().strip()
        name = self.v_name.get().strip()
        if not target or not os.path.isdir(target):
            messagebox.showerror(self.title(), "Choose a folder that exists to create the tree in.", parent=self)
            return
        if not name or name.strip(".") == "" or any(c in INVALID_NAME_CHARS for c in name):
            messagebox.showerror(self.title(), f"The folder name can't be empty or contain any of  {INVALID_NAME_CHARS}",
                                 parent=self)
            return
        base = Path(target) / name
        if base.exists() and any(base.iterdir()) and not messagebox.askyesno(
                self.title(), f"{base}\n\nalready exists and isn't empty. Add the tree into it anyway?", parent=self):
            return
        self.result = (os.path.normpath(target), base, self.v_full.get())
        self.destroy()


# ---------------------------------------------------------------- column fitting

class ColumnFitter:
    """Keeps a Treeview's columns filling the box (flex columns take the leftover width)
    until the user drags a column border; after that their widths are left alone."""

    def __init__(self, tree, fixed, flex, min_flex=80):
        self.tree, self.fixed, self.flex, self.min_flex = tree, fixed, flex, min_flex
        self.user_sized = False
        self._dragging = False
        tree.bind("<Configure>", lambda _e: self.fit(), add="+")
        tree.bind("<ButtonPress-1>", self._press, add="+")
        tree.bind("<ButtonRelease-1>", self._release, add="+")

    def _press(self, e):
        self._dragging = self.tree.identify_region(e.x, e.y) == "separator"

    def _release(self, _e):
        if self._dragging:
            self.user_sized = True
        self._dragging = False

    def displayed(self):
        cols = self.tree["displaycolumns"]
        if cols in ("#all", ("#all",)):
            cols = self.tree["columns"]
        return ["#0", *cols]

    def fit(self, force=False):
        if self.user_sized and not force:
            return
        avail = self.tree.winfo_width() - 4
        if avail < 50:
            return
        cols = self.displayed()
        flex = [c for c in cols if c in self.flex]
        fixed = [c for c in cols if c not in self.flex]
        used = sum(self.fixed[c] for c in fixed)
        rest = avail - used
        if rest < self.min_flex * len(flex):          # box too narrow: shrink everything proportionally
            scale = avail / (used + self.min_flex * len(flex))
            for c in fixed:
                self.tree.column(c, width=max(30, int(self.fixed[c] * scale)))
            rest = avail - sum(int(self.tree.column(c, "width")) for c in fixed)
        else:
            for c in fixed:
                self.tree.column(c, width=self.fixed[c])
        total_w = sum(self.flex[c] for c in flex) or 1
        for c in flex:
            self.tree.column(c, width=max(30, int(rest * self.flex[c] / total_w)))


# ---------------------------------------------------------------- icon grid (zoomed-in view)

FILE_TYPE_COLORS = (
    ({"jpg", "jpeg", "png", "gif", "bmp", "webp", "heic", "tif", "tiff", "svg", "raw", "cr2", "nef", "arw", "psd"}, "#2E9E5B"),
    ({"mp4", "mkv", "mov", "avi", "wmv", "webm", "m4v", "flv", "mts", "m2ts"}, "#7B4FD6"),
    ({"mp3", "wav", "flac", "aac", "ogg", "m4a", "wma", "opus"}, "#E0782B"),
    ({"doc", "docx", "pdf", "txt", "rtf", "odt", "md", "xls", "xlsx", "csv", "ppt", "pptx"}, "#2F6FEB"),
    ({"zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso"}, "#9A6B3A"),
    ({"exe", "msi", "bat", "cmd", "ps1", "dll", "com", "lnk"}, "#D14343"),
)


def file_badge_color(ext):
    return next((color for exts, color in FILE_TYPE_COLORS if ext in exts), "#6B7280")


def draw_folder(c, x, y, s, has_items=True, muted=False):
    def pts(*p):
        return [v for px, py in p for v in (x + px * s, y + py * s)]
    back, back_o, front, front_o = (("#C9CDD3", "#A5ABB3", "#E3E6EA", "#B9BEC6") if muted else
                                    ("#E9AD3B", "#C98D24", "#FFD263", "#DDA73C"))
    lw = max(1, round(s / 64))
    c.create_polygon(pts((.04, .16), (.36, .16), (.44, .25), (.96, .25), (.96, .86), (.04, .86)),
                     fill=back, outline=back_o, width=lw)
    if has_items:
        c.create_rectangle(x + .12 * s, y + .30 * s, x + .88 * s, y + .80 * s, fill="#FFFFFF", outline="#D0D4DA", width=lw)
        for yy in (.37, .44):
            c.create_line(x + .20 * s, y + yy * s, x + .80 * s, y + yy * s, fill="#E1E4E8", width=lw)
    c.create_polygon(pts((.04, .40), (.96, .40), (.96, .86), (.04, .86)), fill=front, outline=front_o, width=lw)


def draw_file(c, x, y, s, name):
    def pts(*p):
        return [v for px, py in p for v in (x + px * s, y + py * s)]
    lw = max(1, round(s / 64))
    c.create_polygon(pts((.20, .06), (.62, .06), (.80, .24), (.80, .94), (.20, .94)),
                     fill="#FFFFFF", outline="#9AA0A6", width=lw)
    c.create_polygon(pts((.62, .06), (.62, .24), (.80, .24)), fill="#E3E6EA", outline="#9AA0A6", width=lw)
    for yy in (.34, .42, .50):
        c.create_line(x + .30 * s, y + yy * s, x + .70 * s, y + yy * s, fill="#D5D9DE", width=lw)
    ext = os.path.splitext(name)[1][1:].lower()
    if ext and s >= 40:
        c.create_rectangle(x + .10 * s, y + .60 * s, x + .68 * s, y + .80 * s, fill=file_badge_color(ext), outline="")
        c.create_text(x + .39 * s, y + .70 * s, text=ext.upper()[:4], fill="#FFFFFF",
                      font=("Segoe UI", -max(8, int(.13 * s)), "bold"))


def wrap_label(font, text, width, max_lines):
    lines, rest = [], text
    while rest and len(lines) < max_lines:
        if font.measure(rest) <= width:
            lines.append(rest)
            rest = ""
            break
        lo, hi = 1, len(rest)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(rest[:mid]) <= width:
                lo = mid
            else:
                hi = mid - 1
        cut = lo
        space = rest.rfind(" ", 0, cut + 1)
        if space > 0:
            cut = space
        lines.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest and lines:
        last = lines[-1]
        while last and font.measure(last + "…") > width:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


class IconGrid(ttk.Frame):
    """Explorer-style large-icon view. Only visible rows are drawn, so huge folders stay fast.
    Items are tuples (kind, node, full_path, chain, label)."""

    MARGIN = 10

    def __init__(self, parent, on_select, on_open, on_up, on_back, on_zoom):
        super().__init__(parent)
        self.on_select, self.on_open, self.on_up, self.on_back, self.on_zoom = on_select, on_open, on_up, on_back, on_zoom
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.font = tkfont.nametofont("TkDefaultFont")
        self.canvas = tk.Canvas(self, background="#FFFFFF", highlightthickness=1, highlightbackground="#ACACAC",
                                highlightcolor="#ACACAC", takefocus=1,
                                yscrollincrement=int(self.font.metrics("linespace") * 1.5))
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self._yview)
        self.sb.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=self.sb.set)
        self.items, self.size, self.sel, self.hover = [], 96, None, None
        self.cols, self.tw, self.th, self.pad, self.lines = 1, 1, 1, 6, 2
        self._wrap = {}
        self.empty_text = ""
        self._reveal_pending = False
        c = self.canvas
        c.bind("<Configure>", lambda _e: self._layout())
        c.bind("<Button-1>", self._click)
        c.bind("<Double-1>", self._double)
        c.bind("<MouseWheel>", self._wheel)
        c.bind("<Control-MouseWheel>", lambda e: (self.on_zoom(1 if e.delta > 0 else -1), "break")[1])
        c.bind("<Motion>", self._motion)
        c.bind("<Leave>", lambda _e: self._set_hover(None))
        c.bind("<Left>", lambda _e: self._move(-1))
        c.bind("<Right>", lambda _e: self._move(1))
        c.bind("<Up>", lambda _e: self._move(-self.cols))
        c.bind("<Down>", lambda _e: self._move(self.cols))
        c.bind("<Home>", lambda _e: self._move(-len(self.items)))
        c.bind("<End>", lambda _e: self._move(len(self.items)))
        c.bind("<Return>", lambda _e: self.selected() and self.on_open(self.selected()))
        c.bind("<BackSpace>", lambda _e: self.on_up())
        c.bind("<Alt-Left>", lambda _e: self.on_back())
        for key in ("<Control-plus>", "<Control-equal>", "<Control-KP_Add>"):
            c.bind(key, lambda _e: self.on_zoom(1))
        for key in ("<Control-minus>", "<Control-KP_Subtract>"):
            c.bind(key, lambda _e: self.on_zoom(-1))

    # ----- public

    def set_items(self, items, size, select=None, empty_text="This folder is empty."):
        self.items, self.size, self.empty_text = items, size, empty_text
        self.sel = next((i for i, it in enumerate(items) if select is not None and it[1] is select), None)
        self.hover = None
        self._wrap.clear()
        self.canvas.yview_moveto(0)
        self._reveal_pending = self.sel is not None
        self._layout()

    def set_size(self, size):
        self.size = size
        self._wrap.clear()
        self._reveal_pending = self.sel is not None
        self._layout()

    def selected(self):
        return self.items[self.sel] if self.sel is not None and self.sel < len(self.items) else None

    def focus(self):
        self.canvas.focus_set()

    # ----- layout / drawing

    def _layout(self):
        s = self.size
        self.pad = max(6, s // 12)
        self.lines = 2 if s < 90 else 3
        self.tw = max(int(s * 1.5), s + 48)
        self.th = self.pad + s + 6 + self.lines * self.font.metrics("linespace") + self.pad
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        self.cols = max(1, (w - 2 * self.MARGIN) // self.tw)
        rows = -(-len(self.items) // self.cols)
        self.canvas.configure(scrollregion=(0, 0, w, max(h, 2 * self.MARGIN + rows * self.th)))
        if self._reveal_pending and w > 1:
            self._reveal_pending = False
            self._ensure_visible(self.sel, redraw=False)
        self._redraw()

    def _origin(self, i):
        r, col = divmod(i, self.cols)
        return self.MARGIN + col * self.tw, self.MARGIN + r * self.th

    def _redraw(self):
        c = self.canvas
        c.delete("all")
        if not self.items:
            c.create_text(c.winfo_width() / 2, 40, text=self.empty_text, fill="#6B7280", font=self.font)
            return
        top = c.canvasy(0)
        bottom = top + c.winfo_height()
        r0 = max(0, int((top - self.MARGIN) // self.th))
        r1 = int((bottom - self.MARGIN) // self.th) + 1
        for i in range(r0 * self.cols, min(len(self.items), (r1 + 1) * self.cols)):
            self._draw_tile(i)

    def _colors(self, i):
        if i == self.sel:
            return "#CCE8FF", "#99D1FF"
        if i == self.hover:
            return "#E5F3FF", "#E5F3FF"
        return "", ""

    def _draw_tile(self, i):
        c = self.canvas
        kind, node, _full, _chain, label = self.items[i]
        x, y = self._origin(i)
        fill, outline = self._colors(i)
        c.create_rectangle(x + 2, y + 2, x + self.tw - 2, y + self.th - 2, fill=fill, outline=outline, tags=(f"bg{i}",))
        s = self.size
        ix, iy = x + (self.tw - s) / 2, y + self.pad
        note = False
        if kind == "d":
            note = bool(node.get("x") or node.get("u") or node.get("l"))
            draw_folder(c, ix, iy, s, has_items=bool(node.get("d") or node.get("f")), muted=note)
        else:
            draw_file(c, ix, iy, s, node[0])
        if i not in self._wrap:
            self._wrap[i] = "\n".join(wrap_label(self.font, label, self.tw - 12, self.lines))
        c.create_text(x + self.tw / 2, iy + s + 6, text=self._wrap[i], anchor="n", justify="center",
                      font=self.font, fill="#9A6700" if note else "#1F2328")

    def _restyle(self, i):
        if i is not None:
            fill, outline = self._colors(i)
            self.canvas.itemconfigure(f"bg{i}", fill=fill, outline=outline)

    # ----- interaction

    def _index_at(self, ex, ey):
        cx, cy = self.canvas.canvasx(ex) - self.MARGIN, self.canvas.canvasy(ey) - self.MARGIN
        if cx < 0 or cy < 0:
            return None
        col, row = int(cx // self.tw), int(cy // self.th)
        i = row * self.cols + col
        return i if col < self.cols and i < len(self.items) else None

    def _select(self, i):
        old, self.sel = self.sel, i
        self._restyle(old)
        self._restyle(i)
        if i is not None:
            self._ensure_visible(i)
        self.on_select(self.selected())

    def _set_hover(self, i):
        if i != self.hover:
            old, self.hover = self.hover, i
            self._restyle(old)
            self._restyle(i)

    def _click(self, e):
        self.canvas.focus_set()
        self._select(self._index_at(e.x, e.y))

    def _double(self, e):
        i = self._index_at(e.x, e.y)
        if i is not None:
            self.on_open(self.items[i])

    def _motion(self, e):
        self._set_hover(self._index_at(e.x, e.y))

    def _move(self, delta):
        if self.items:
            i = 0 if self.sel is None else max(0, min(len(self.items) - 1, self.sel + delta))
            self._select(i)
        return "break"

    def _wheel(self, e):
        self.canvas.yview_scroll(int(-e.delta / 120 * 3), "units")
        self._redraw()

    def _yview(self, *args):
        self.canvas.yview(*args)
        self._redraw()

    def _ensure_visible(self, i, redraw=True):
        if i is None:
            return
        y0 = self._origin(i)[1]
        y1 = y0 + self.th + self.MARGIN
        top, h = self.canvas.canvasy(0), self.canvas.winfo_height()
        total = float(self.canvas.cget("scrollregion").split()[3] or 1)
        if y0 < top:
            self.canvas.yview_moveto(max(0, y0 - self.MARGIN) / total)
        elif y1 > top + h:
            self.canvas.yview_moveto((y1 - h) / total)
        else:
            return
        if redraw:
            self._redraw()


# ---------------------------------------------------------------- app

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.cfg = load_config()
        self.sources = [Source.from_dict(d) for d in self.cfg["sources"]]
        self.ui_queue = queue.Queue()
        self.cancel_event = None
        self.busy = False

        self._setup_style()
        w, h = int(1150 * self.ui_scale), int(720 * self.ui_scale)
        self.geometry(f"{w}x{h}")
        self.minsize(int(800 * self.ui_scale), int(500 * self.ui_scale))

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=8)
        self.tab_take = ttk.Frame(self.nb, padding=12)
        self.tab_saved = ttk.Frame(self.nb, padding=12)
        self.nb.add(self.tab_take, text="  Take Snapshot  ")
        self.nb.add(self.tab_saved, text="  Saved Snapshots  ")
        self._build_take_tab()
        self._build_saved_tab()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(50, self._poll)
        self.refresh_snapshot_list()
        self._start_update_check()

    # ---------- update check

    def _start_update_check(self):
        """Ask the website for the latest version, off the UI thread. Says nothing
        unless there is an update (no internet just means no bar)."""
        def work():
            try:
                offer = update_check.check()
            except Exception:
                return
            if update_check.is_newer(offer["version"]):
                self.post(lambda: self._show_update(offer))
        threading.Thread(target=work, daemon=True).start()

    def _show_update(self, offer):
        """A bar above the tabs: what's out, Download (opens it in the browser) and Later."""
        bar = ttk.Frame(self, style="Update.TFrame", padding=(12, 6))
        bar.pack(fill="x", padx=8, pady=(8, 0), before=self.nb)
        ttk.Label(bar, text=f"Update available: {APP_NAME} V{offer['version']}",
                  style="Update.TLabel").pack(side="left")
        ttk.Label(bar, text=f"You have V{update_check.APP_VERSION}.",
                  style="UpdateMuted.TLabel").pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Later", command=bar.destroy).pack(side="right")
        ttk.Button(bar, text="Download", command=lambda: webbrowser.open(offer["download"])).pack(
            side="right", padx=(0, 6))

    # ---------- infrastructure

    def _setup_style(self):
        self.ui_scale = max(1.0, self.winfo_fpixels("1i") / 96)
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        base = tkfont.nametofont("TkDefaultFont")
        base.configure(size=UI_FONT_SIZE)
        tkfont.nametofont("TkTextFont").configure(size=UI_FONT_SIZE)
        bold = base.copy()
        bold.configure(weight="bold")
        big = base.copy()
        big.configure(size=12, weight="bold")
        self._fonts = [bold, big]
        style.configure("Heading.TLabel", font=bold)
        style.configure("Title.TLabel", font=big)
        style.configure("Muted.TLabel", foreground="#6B7280")
        style.configure("Run.TButton", font=bold, padding=(18, 6))
        style.configure("Update.TFrame", background="#E8F0FE")
        style.configure("Update.TLabel", background="#E8F0FE", foreground="#1D4ED8", font=bold)
        style.configure("UpdateMuted.TLabel", background="#E8F0FE", foreground="#4B5563")

        base_px = round(16 * self.ui_scale)
        self.icons = {k: make_icon(self, k, base_px) for k in ("folder", "file", "drive")}
        row_h = max(base_px + round(6 * self.ui_scale), int(base.metrics("linespace") * 1.5))
        style.configure("Treeview", rowheight=row_h)
        self.grid_sizes = [round(px * self.ui_scale) for px in GRID_ICON_SIZES]

        app_icons = [make_icon(self, "app", s) for s in (16, 32, 48, 64)]
        self._app_icons = app_icons
        self.iconphoto(True, *reversed(app_icons))

    def post(self, fn):
        """Run fn on the UI thread (safe to call from worker threads)."""
        self.ui_queue.put(fn)

    def _poll(self):
        try:
            while True:
                self.ui_queue.get_nowait()()
        except queue.Empty:
            pass
        self.after(50, self._poll)

    def _persist(self):
        self.cfg["sources"] = [asdict(s) for s in self.sources]
        save_config(self.cfg)

    def _on_close(self):
        if self.busy and not messagebox.askyesno(APP_NAME, "A task is still running. Stop it and quit?"):
            return
        if self.cancel_event:
            self.cancel_event.set()
        self._persist()
        self.destroy()

    # ---------- tab 1: take snapshot

    def _build_take_tab(self):
        t = self.tab_take
        t.columnconfigure(0, weight=1)
        t.rowconfigure(1, weight=3)
        t.rowconfigure(4, weight=1)

        ttk.Label(t, text="1. Folders to snapshot", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        box = ttk.Frame(t)
        box.grid(row=1, column=0, sticky="nsew", pady=(4, 12))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        cols = ("sub", "files", "hidden", "exclude")
        self.src_tree = ttk.Treeview(box, columns=cols, show="tree headings", selectmode="extended")
        self.src_tree.heading("#0", text="Folder")
        self.src_tree.heading("sub", text="Subfolders")
        self.src_tree.heading("files", text="Files")
        self.src_tree.heading("hidden", text="Hidden items")
        self.src_tree.heading("exclude", text="Skipping")
        self.src_tree.column("#0", width=int(380 * self.ui_scale), stretch=True)
        for c, wd in (("sub", 120), ("files", 70), ("hidden", 100), ("exclude", 200)):
            self.src_tree.column(c, width=int(wd * self.ui_scale), stretch=c == "exclude")
        self.src_tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.src_tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.src_tree.configure(yscrollcommand=sb.set)
        self.src_tree.bind("<Double-1>", lambda _e: self.edit_source())
        self.src_tree.bind("<Delete>", lambda _e: self.remove_source())
        side = ttk.Frame(box)
        side.grid(row=0, column=2, sticky="n", padx=(8, 0))
        ttk.Button(side, text="Add Folder…", command=self.add_source).pack(fill="x")
        ttk.Button(side, text="Settings…", command=self.edit_source).pack(fill="x", pady=4)
        ttk.Button(side, text="Remove", command=self.remove_source).pack(fill="x")
        ttk.Label(side, text="Double-click a folder\nto change its settings.", style="Muted.TLabel",
                  justify="left").pack(fill="x", pady=(12, 0))

        ttk.Label(t, text="2. Save snapshot to (one copy in each location)", style="Title.TLabel").grid(row=3, column=0, sticky="w")
        box2 = ttk.Frame(t)
        box2.grid(row=4, column=0, sticky="nsew", pady=(4, 12))
        box2.columnconfigure(0, weight=1)
        box2.rowconfigure(0, weight=1)
        self.dest_list = tk.Listbox(box2, height=4, activestyle="none", relief="solid", borderwidth=1,
                                    highlightthickness=0, font=tkfont.nametofont("TkDefaultFont"))
        self.dest_list.grid(row=0, column=0, sticky="nsew")
        self.dest_list.bind("<Delete>", lambda _e: self.remove_dest())
        side2 = ttk.Frame(box2)
        side2.grid(row=0, column=2, sticky="n", padx=(8, 0))
        ttk.Button(side2, text="Add Location…", command=self.add_dest).pack(fill="x")
        ttk.Button(side2, text="Remove", command=self.remove_dest).pack(fill="x", pady=4)

        bottom = ttk.Frame(t)
        bottom.grid(row=5, column=0, sticky="we")
        bottom.columnconfigure(1, weight=1)
        ttk.Label(bottom, text="3. Snapshot name:", style="Title.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.v_name = tk.StringVar(value=self._default_name())
        ttk.Entry(bottom, textvariable=self.v_name).grid(row=0, column=1, sticky="we")
        self.run_btn = ttk.Button(bottom, text="Run Snapshot", style="Run.TButton", command=self.run_snapshot)
        self.run_btn.grid(row=0, column=2, padx=(12, 0))
        self.cancel_btn = ttk.Button(bottom, text="Cancel", command=self._cancel_task, state="disabled")
        self.cancel_btn.grid(row=0, column=3, padx=(6, 0))
        self.progress = ttk.Progressbar(bottom, mode="indeterminate")
        self.progress.grid(row=1, column=0, columnspan=4, sticky="we", pady=(12, 4))
        self.v_status = tk.StringVar(value="Only names, sizes and dates are recorded. No file contents are copied.")
        ttk.Label(bottom, textvariable=self.v_status, style="Muted.TLabel").grid(row=2, column=0, columnspan=4, sticky="w")

        self._refresh_sources()
        self._refresh_dests()

    def _default_name(self):
        return "Snapshot " + datetime.now().strftime("%Y-%m-%d %H-%M")

    def _refresh_sources(self):
        self.src_tree.delete(*self.src_tree.get_children())
        for i, s in enumerate(self.sources):
            missing = "" if os.path.isdir(s.path) else "   (not found)"
            self.src_tree.insert("", "end", iid=str(i), text=s.path + missing, image=self.icons["folder"],
                                 values=(s.describe(), "Yes" if s.files else "No",
                                         "Yes" if s.hidden else "No", s.exclude or "—"))

    def _refresh_dests(self):
        self.dest_list.delete(0, "end")
        for d in self.cfg["save_dirs"]:
            self.dest_list.insert("end", "  " + d)

    def add_source(self):
        path = filedialog.askdirectory(title="Choose a folder to snapshot", mustexist=True)
        if not path:
            return
        path = os.path.normpath(path)
        if any(os.path.normcase(s.path) == os.path.normcase(path) for s in self.sources):
            messagebox.showinfo(APP_NAME, "That folder is already in the list.")
            return
        self.sources.append(Source(path))
        self._refresh_sources()
        self._persist()

    def edit_source(self):
        sel = self.src_tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "Select a folder first.")
            return
        i = int(sel[0])
        dlg = SourceDialog(self, self.sources[i])
        if dlg.result:
            self.sources[i] = dlg.result
            self._refresh_sources()
            self.src_tree.selection_set(str(i))
            self._persist()

    def remove_source(self):
        sel = sorted((int(i) for i in self.src_tree.selection()), reverse=True)
        for i in sel:
            del self.sources[i]
        if sel:
            self._refresh_sources()
            self._persist()

    def add_dest(self):
        path = filedialog.askdirectory(title="Choose where to save snapshots")
        if not path:
            return
        path = os.path.normpath(path)
        if path not in self.cfg["save_dirs"]:
            self.cfg["save_dirs"].append(path)
            self._refresh_dests()
            self._persist()
            self.refresh_snapshot_list()

    def remove_dest(self):
        sel = self.dest_list.curselection()
        if sel:
            del self.cfg["save_dirs"][sel[0]]
            self._refresh_dests()
            self._persist()
            self.refresh_snapshot_list()

    def _set_busy(self, busy, message=None):
        self.busy = busy
        self.run_btn.configure(state="disabled" if busy else "normal")
        self.cancel_btn.configure(state="normal" if busy else "disabled")
        self.export_btn.configure(state="disabled" if busy or not self.loaded else "normal")
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()
        if message:
            self.v_status.set(message)

    def _cancel_task(self):
        if self.cancel_event:
            self.cancel_event.set()
            self.v_status.set("Cancelling…")

    def run_snapshot(self):
        if self.busy:
            return
        if not self.sources:
            messagebox.showinfo(APP_NAME, "Add at least one folder to snapshot.")
            return
        if not self.cfg["save_dirs"]:
            messagebox.showinfo(APP_NAME, "Add at least one location to save the snapshot to.")
            return
        name = self.v_name.get().strip() or self._default_name()
        sources = [Source(**asdict(s)) for s in self.sources]
        save_dirs = list(self.cfg["save_dirs"])
        cancel = self.cancel_event = threading.Event()
        self._set_busy(True, "Scanning…")

        def report(i, st, path):
            self.post(lambda: self.v_status.set(
                f"Folder {i + 1} of {len(sources)}  ·  {st['folders']:,} folders, {st['files']:,} files  ·  {path}"))

        def work():
            try:
                roots, trees = [], []
                totals = {"folders": 0, "files": 0, "bytes": 0, "errors": 0}
                for i, src in enumerate(sources):
                    if not os.path.isdir(src.path):
                        roots.append({"path": src.path, "settings": asdict(src), "missing": True,
                                      "stats": {"folders": 0, "files": 0, "bytes": 0, "errors": 1}})
                        totals["errors"] += 1
                        continue
                    tree, st = scan_source(src, lambda s, p, i=i: report(i, s, p), cancel)
                    trees.append(tree)
                    roots.append({"path": tree["n"], "settings": asdict(src), "stats": st})
                    for k in totals:
                        totals[k] += st[k]
                header = {"format": FORMAT_ID, "version": FORMAT_VERSION, "name": name,
                          "created": datetime.now().isoformat(timespec="seconds"),
                          "computer": socket.gethostname(), "roots": roots, "totals": totals}
                self.post(lambda: self.v_status.set("Saving…"))
                data = build_snapshot_bytes(header, trees)
                saved, failed = [], []
                for d in save_dirs:
                    try:
                        os.makedirs(d, exist_ok=True)
                        p = unique_path(d, name)
                        write_bytes_atomic(p, data)
                        saved.append(str(p))
                    except OSError as e:
                        failed.append(f"{d}\n    {e.strerror or e}")
                self.post(lambda: self._snapshot_done(header, saved, failed, len(data)))
            except Cancelled:
                self.post(lambda: self._set_busy(False, "Snapshot cancelled."))
            except Exception as e:  # noqa: BLE001 - report anything unexpected to the user
                self.post(lambda e=e: (self._set_busy(False, "Snapshot failed."),
                                       messagebox.showerror(APP_NAME, f"Snapshot failed:\n{e}")))

        threading.Thread(target=work, daemon=True).start()

    def _snapshot_done(self, header, saved, failed, nbytes):
        t = header["totals"]
        summary = (f"{t['folders']:,} folders and {t['files']:,} files recorded "
                   f"(originally {human_size(t['bytes'])}). Snapshot file: {human_size(nbytes)}.")
        self._set_busy(False, "Done. " + summary)
        for p in saved:
            if p not in self.cfg["known_snapshots"]:
                self.cfg["known_snapshots"].append(p)
        self._persist()
        self.v_name.set(self._default_name())
        msg = summary
        if t["errors"]:
            msg += f"\n\n{t['errors']:,} item(s) could not be read (access denied or missing)."
        if saved:
            msg += "\n\nSaved to:\n" + "\n".join(saved)
        if failed:
            msg += "\n\nCould not save to:\n" + "\n".join(failed)
        (messagebox.showwarning if failed else messagebox.showinfo)(APP_NAME, msg)
        self.refresh_snapshot_list(select=saved[0] if saved else None)
        if saved:
            self.nb.select(self.tab_saved)

    # ---------- tab 2: saved snapshots

    def _build_saved_tab(self):
        t = self.tab_saved
        pane = ttk.PanedWindow(t, orient="horizontal")
        pane.pack(fill="both", expand=True)

        left = ttk.Frame(pane)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        ttk.Label(left, text="Saved snapshots", style="Title.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        box = ttk.Frame(left)
        box.grid(row=1, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.snap_list = ttk.Treeview(box, columns=("created", "items"), show="tree headings", selectmode="browse")
        self.snap_list.heading("#0", text="Name")
        self.snap_list.heading("created", text="Taken")
        self.snap_list.heading("items", text="Items")
        self.snap_list.column("#0", width=int(200 * self.ui_scale), stretch=False)
        self.snap_list.column("created", width=int(120 * self.ui_scale), stretch=False)
        self.snap_list.column("items", width=int(62 * self.ui_scale), stretch=False, anchor="e")
        self.snap_list.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(box, orient="vertical", command=self.snap_list.yview)
        sb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(box, orient="horizontal", command=self.snap_list.xview)
        hsb.grid(row=1, column=0, sticky="we")
        self.snap_list.configure(yscrollcommand=sb.set, xscrollcommand=hsb.set)
        self.snap_list.bind("<<TreeviewSelect>>", lambda _e: self._on_snapshot_selected())
        self.snap_list.bind("<Delete>", lambda _e: self.delete_snapshot())
        f = tkfont.nametofont("TkDefaultFont")
        date_w = f.measure("2026-09-28 09:24") + round(20 * self.ui_scale)
        self.snap_fitter = ColumnFitter(self.snap_list, {"created": date_w, "items": f.measure("000,000") + round(20 * self.ui_scale)},
                                        {"#0": 1})
        self.v_snap_path = tk.StringVar()
        ttk.Label(left, textvariable=self.v_snap_path, style="Muted.TLabel", wraplength=int(360 * self.ui_scale),
                  justify="left").grid(row=2, column=0, sticky="we", pady=4)
        btns = ttk.Frame(left)
        btns.grid(row=3, column=0, sticky="we")
        for i in range(2):
            btns.columnconfigure(i, weight=1)
        ttk.Button(btns, text="Open File…", command=self.open_snapshot_file).grid(row=0, column=0, sticky="we", padx=(0, 2))
        ttk.Button(btns, text="Refresh", command=self.refresh_snapshot_list).grid(row=0, column=1, sticky="we", padx=(2, 0))
        ttk.Button(btns, text="Show in Folder", command=self.show_in_folder).grid(row=1, column=0, sticky="we", padx=(0, 2), pady=4)
        ttk.Button(btns, text="Delete…  (Del)", command=self.delete_snapshot).grid(row=1, column=1, sticky="we", padx=(2, 0), pady=4)
        self.export_btn = ttk.Button(btns, text="Recreate as 0-byte Folder Tree…", command=self.export_snapshot, state="disabled")
        self.export_btn.grid(row=2, column=0, columnspan=2, sticky="we")
        pane.add(left, weight=1)

        right = ttk.Frame(pane, padding=(12, 0, 0, 0))
        right.columnconfigure(0, weight=1)
        right.rowconfigure(3, weight=1)
        self.v_view_title = tk.StringVar(value="Select a snapshot to browse it")
        ttk.Label(right, textvariable=self.v_view_title, style="Title.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        sbar = ttk.Frame(right)
        sbar.grid(row=1, column=0, sticky="we", pady=(0, 6))
        sbar.columnconfigure(1, weight=1)
        ttk.Label(sbar, text="Find:").grid(row=0, column=0, padx=(0, 6))
        self.v_search = tk.StringVar()
        ent = ttk.Entry(sbar, textvariable=self.v_search)
        ent.grid(row=0, column=1, sticky="we")
        ent.bind("<Return>", lambda _e: self.run_search())
        ent.bind("<Escape>", lambda _e: self.clear_search())
        ttk.Button(sbar, text="Search", command=self.run_search).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(sbar, text="Clear", command=self.clear_search).grid(row=0, column=3, padx=(4, 0))
        self.v_full_paths = tk.BooleanVar(value=self.cfg.get("viewer_full_paths", False))
        ttk.Checkbutton(sbar, text="Show full paths", variable=self.v_full_paths,
                        command=self._toggle_full_paths).grid(row=0, column=4, padx=(12, 0))
        ttk.Label(sbar, text="Ctrl + scroll: icon view", style="Muted.TLabel").grid(row=0, column=5, padx=(12, 0))

        # Address bar, only shown in folder (zoomed-in) view.
        self.addr_bar = ttk.Frame(right)
        self.addr_bar.grid(row=2, column=0, sticky="we", pady=(0, 6))
        self.back_btn = ttk.Button(self.addr_bar, text="←", width=3, command=self._go_back)
        self.back_btn.pack(side="left")
        self.up_btn = ttk.Button(self.addr_bar, text="↑", width=3, command=self._go_up)
        self.up_btn.pack(side="left", padx=(2, 8))
        self.crumbs = ttk.Frame(self.addr_bar, relief="solid", borderwidth=1, padding=(4, 1))
        self.crumbs.pack(side="left", fill="x", expand=True)
        self.addr_bar.grid_remove()

        area = ttk.Frame(right)
        area.grid(row=3, column=0, sticky="nsew")
        area.columnconfigure(0, weight=1)
        area.rowconfigure(0, weight=1)
        self.icon_grid = IconGrid(area, on_select=self._on_grid_select, on_open=self._on_grid_open,
                                  on_up=self._go_up, on_back=self._go_back, on_zoom=self._zoom)
        self.icon_grid.grid(row=0, column=0, sticky="nsew")
        self.icon_grid.grid_remove()
        box = self.tree_box = ttk.Frame(area)
        box.grid(row=0, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.view = ttk.Treeview(box, columns=("size", "items", "modified", "location"),
                                 show="tree headings", selectmode="browse")
        self.view.heading("#0", text="Name")
        self.view.heading("size", text="Size")
        self.view.heading("items", text="Items")
        self.view.heading("modified", text="Modified")
        self.view.heading("location", text="Location")
        # stretch=False everywhere so columns keep whatever width you drag them to;
        # ColumnFitter sizes them to fill the box until you do.
        self.view.column("#0", width=int(380 * self.ui_scale), minwidth=60, stretch=False)
        self.view.column("size", width=int(100 * self.ui_scale), minwidth=40, stretch=False, anchor="e")
        self.view.column("items", width=int(90 * self.ui_scale), minwidth=40, stretch=False, anchor="e")
        self.view.column("modified", width=int(130 * self.ui_scale), minwidth=40, stretch=False)
        self.view.column("location", width=int(400 * self.ui_scale), minwidth=40, stretch=False)
        self.view["displaycolumns"] = ("size", "items", "modified")
        self.view.tag_configure("note", foreground="#9A6700")
        self.view_fitter = ColumnFitter(self.view, {"size": f.measure("000.0 MB+") + round(24 * self.ui_scale),
                                                    "items": f.measure("00,000 items") + round(20 * self.ui_scale),
                                                    "modified": date_w},
                                        {"#0": 1, "location": 1}, min_flex=round(120 * self.ui_scale))
        self.view.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(box, orient="vertical", command=self.view.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(box, orient="horizontal", command=self.view.xview)
        hsb.grid(row=1, column=0, sticky="we")
        self.view.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.view.bind("<<TreeviewOpen>>", lambda _e: self._populate(self.view.focus()))
        self.view.bind("<<TreeviewSelect>>", lambda _e: self._on_item_selected())
        self.view.bind("<Double-1>", self._on_view_double)
        self.view.bind("<Return>", self._on_view_enter)
        self.view.bind("<BackSpace>", lambda _e: self._go_up())
        self.view.bind("<Alt-Left>", lambda _e: self._go_back())
        self.view.bind("<Control-MouseWheel>", self._on_ctrl_wheel)
        for key in ("<Control-plus>", "<Control-equal>", "<Control-KP_Add>"):
            self.view.bind(key, lambda _e: self._zoom(1))
        for key in ("<Control-minus>", "<Control-KP_Subtract>"):
            self.view.bind(key, lambda _e: self._zoom(-1))
        self.v_details = tk.StringVar()
        ttk.Label(right, textvariable=self.v_details, style="Muted.TLabel", wraplength=int(720 * self.ui_scale),
                  justify="left").grid(row=4, column=0, sticky="we", pady=(6, 0))
        pane.add(right, weight=3)

        self.loaded = None          # (path, header, trees)
        self.nodes = {}             # view iid -> (kind, node, full_path, chain)
        self.search_mode = False
        self.zoom = 0               # 0 = tree view; 1..3 = single-folder view at growing sizes
        self.cur_chain = []         # folder shown in folder view ([] = snapshot top level)
        self.history = []
        self._summary = ""
        self._load_token = 0

    def refresh_snapshot_list(self, select=None):
        prev = select or (self.snap_list.selection() or [None])[0]
        paths = []
        seen = set()

        def add(p):
            key = os.path.normcase(os.path.abspath(p))
            if key not in seen and os.path.isfile(p):
                seen.add(key)
                paths.append(os.path.abspath(p))

        for d in self.cfg["save_dirs"]:
            try:
                for p in sorted(Path(d).glob("*" + EXT)):
                    add(str(p))
            except OSError:
                pass
        for p in self.cfg["known_snapshots"]:
            add(p)
        self.cfg["known_snapshots"] = [p for p in self.cfg["known_snapshots"] if os.path.isfile(p)]

        rows = []
        for p in paths:
            try:
                h = read_header(p)
            except (OSError, ValueError, EOFError, gzip.BadGzipFile):
                continue
            rows.append((h.get("created", ""), p, h))
        rows.sort(key=lambda r: r[0], reverse=True)
        self.snap_list.delete(*self.snap_list.get_children())
        for created, p, h in rows:
            tot = h.get("totals", {})
            items = tot.get("folders", 0) + tot.get("files", 0)
            self.snap_list.insert("", "end", iid=p, text=h.get("name", Path(p).stem), image=self.icons["drive"],
                                  values=(created.replace("T", " ")[:16], f"{items:,}"))
        if prev and self.snap_list.exists(prev):
            self.snap_list.selection_set(prev)
            self.snap_list.see(prev)
        elif not rows:
            self.v_snap_path.set("No snapshots yet. Take one on the first tab, or open a .dsnap file.")

    def _selected_snapshot(self):
        sel = self.snap_list.selection()
        return sel[0] if sel else None

    def _clear_view(self):
        self.view.delete(*self.view.get_children())
        self.nodes.clear()
        if not self.loaded:
            self.icon_grid.set_items([], self.icon_grid.size, empty_text="")

    def _on_snapshot_selected(self):
        path = self._selected_snapshot()
        if not path:
            return
        self.v_snap_path.set(path)
        if self.loaded and self.loaded[0] == path:
            return
        self._load_token += 1
        token = self._load_token
        self.loaded = None
        self.export_btn.configure(state="disabled")
        self._clear_view()
        self.v_view_title.set("Loading…")
        self.v_details.set("")

        def work():
            try:
                header, trees = read_snapshot(path)
                compute_folder_totals(trees)
                self.post(lambda: self._show_snapshot(token, path, header, trees))
            except Exception as e:  # noqa: BLE001
                self.post(lambda e=e: token == self._load_token and self.v_view_title.set(f"Could not open snapshot: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _show_snapshot(self, token, path, header, trees):
        if token != self._load_token:
            return
        self.loaded = (path, header, trees)
        if not self.busy:
            self.export_btn.configure(state="normal")
        self.v_search.set("")
        self.search_mode = False
        tot = header.get("totals", {})
        self.v_view_title.set(header.get("name", "Snapshot"))
        created = header.get("created", "").replace("T", " ")
        self._summary = (f"Taken {created} on {header.get('computer', '?')}  ·  {tot.get('folders', 0):,} folders, "
                         f"{tot.get('files', 0):,} files, originally {human_size(tot.get('bytes', 0))}. "
                         "Files here are names only (0-byte placeholders). Nothing was copied.")
        missing = [r["path"] for r in header.get("roots", []) if r.get("missing")]
        if missing:
            self._summary += "\nNot found when taken: " + ", ".join(missing)
        self.history.clear()
        if self.zoom:
            self._show_folder(self._default_chain(), record=False)
            self.v_details.set(self._summary)
        else:
            self._show_tree()

    def _default_chain(self):
        trees = self.loaded[2] if self.loaded else []
        return [trees[0]] if len(trees) == 1 else []

    # ---------- rows

    @staticmethod
    def _dir_note(d):
        return (" (access denied)" if d.get("x") else " (contents not scanned)" if d.get("u")
                else " (link, not followed)" if d.get("l") else "")

    @staticmethod
    def _dir_values(d, location=""):
        if d.get("x") or d.get("u") or d.get("l"):
            return ("—", "—", fmt_time(d.get("m")), location)
        n = len(d.get("d", ())) + len(d.get("f", ()))
        size = human_size(d.get("_s", 0)) + ("+" if d.get("_p") else "")
        return (size, f"{n:,} item{'s' if n != 1 else ''}", fmt_time(d.get("m")), location)

    def _insert_dir(self, parent, d, full, chain, lazy, label=None, location=""):
        icons = self.icons
        note = self._dir_note(d)
        iid = self.view.insert(parent, "end", text=(label or d["n"]) + note,
                               image=icons["drive" if label else "folder"],
                               tags=("note",) if note else (), values=self._dir_values(d, location))
        self.nodes[iid] = ("d", d, full, chain)
        if lazy and (d.get("d") or d.get("f")):
            self.view.insert(iid, "end", text="…", tags=("dummy",))
        return iid

    def _insert_file(self, parent, f, full, chain, location=""):
        iid = self.view.insert(parent, "end", text=f[0], image=self.icons["file"],
                               values=(human_size(f[1]), "", fmt_time(f[2]), location))
        self.nodes[iid] = ("f", f, full, chain)
        return iid

    def _insert_contents(self, parent, node, full, chain, lazy):
        for d in sorted(node.get("d", ()), key=lambda x: x["n"].lower()):
            self._insert_dir(parent, d, os.path.join(full, d["n"]), chain + [d], lazy)
        for f in sorted(node.get("f", ()), key=lambda x: x[0].lower()):
            self._insert_file(parent, f, os.path.join(full, f[0]), chain + [f])

    # ---------- tree view (zoom level 0)

    def _root_label(self, root):
        """Top-level folder label: just its name (e.g. "FileShare") unless "Show full paths" is on."""
        if self.v_full_paths.get():
            return root["n"]
        return os.path.basename(root["n"].rstrip("\\/")) or root["n"]

    def _toggle_full_paths(self):
        self.cfg["viewer_full_paths"] = self.v_full_paths.get()
        self._persist()
        if not self.loaded or self.search_mode:
            return
        if self.zoom:
            sel = self.icon_grid.selected()
            self._show_folder(self.cur_chain, select=sel[1] if sel else None, record=False)
        else:
            for iid in self.view.get_children(""):
                node = self.nodes[iid][1]
                self.view.item(iid, text=self._root_label(node) + self._dir_note(node))

    def _use_grid(self, on):
        if on:
            self.tree_box.grid_remove()
            self.icon_grid.grid()
            self.addr_bar.grid()
        else:
            self.icon_grid.grid_remove()
            self.addr_bar.grid_remove()
            self.tree_box.grid()

    def _show_tree(self):
        self.search_mode = False
        self._use_grid(False)
        self.view["displaycolumns"] = ("size", "items", "modified")
        self.view_fitter.fit()
        self._clear_view()
        if not self.loaded:
            return
        for root in self.loaded[2]:
            self._insert_dir("", root, root["n"], [root], lazy=True, label=self._root_label(root))
        kids = self.view.get_children()
        if len(kids) == 1:
            self._populate(kids[0])
            self.view.item(kids[0], open=True)
        self.v_details.set(self._summary)

    def _populate(self, iid):
        if not iid or iid not in self.nodes:
            return
        kids = self.view.get_children(iid)
        if not (len(kids) == 1 and "dummy" in self.view.item(kids[0], "tags")):
            return
        self.view.delete(kids[0])
        _kind, node, full, chain = self.nodes[iid]
        self._insert_contents(iid, node, full, chain, lazy=True)

    def _reveal(self, chain, open_last=False):
        """Show the tree view with the item at `chain` expanded into view and selected."""
        self._show_tree()
        parent, target = "", None
        for depth, node in enumerate(chain):
            target = next((i for i in self.view.get_children(parent)
                           if i in self.nodes and self.nodes[i][1] is node), None)
            if target is None:
                return
            if depth < len(chain) - 1 or open_last:
                self._populate(target)
                self.view.item(target, open=True)
            parent = target
        self.view.see(target)
        self.view.selection_set(target)
        self.view.focus(target)

    # ---------- icon view (zoom levels 1..3)

    def _show_folder(self, chain, select=None, record=True):
        if record and not same_chain(chain, self.cur_chain):
            self.history.append(self.cur_chain)
            del self.history[:-100]
        self.cur_chain = chain
        self.search_mode = False
        self._use_grid(True)
        size = self.grid_sizes[self.zoom - 1]
        if not self.loaded:
            self.icon_grid.set_items([], size, empty_text="")
            self._update_crumbs()
            return
        items = []
        if not chain:
            for root in self.loaded[2]:
                items.append(("d", root, root["n"], [root], self._root_label(root)))
        else:
            node, full = chain[-1], chain_path(chain)
            for d in sorted(node.get("d", ()), key=lambda x: x["n"].lower()):
                items.append(("d", d, os.path.join(full, d["n"]), chain + [d], d["n"]))
            for f in sorted(node.get("f", ()), key=lambda x: x[0].lower()):
                items.append(("f", f, os.path.join(full, f[0]), chain + [f], f[0]))
        self.icon_grid.set_items(items, size, select=select)
        self._update_crumbs()
        sel = self.icon_grid.selected()
        if sel:
            self._describe(sel[0], sel[1], sel[2])
        else:
            self._describe_folder(chain)
        self.icon_grid.focus()

    def _on_grid_select(self, item):
        if item:
            self._describe(item[0], item[1], item[2])
        else:
            self._describe_folder(self.cur_chain)

    def _on_grid_open(self, item):
        if item[0] == "d":
            self._show_folder(item[3])

    def _describe_folder(self, chain):
        if not chain:
            self.v_details.set(self._summary)
            return
        d = chain[-1]
        self.v_details.set(f"{chain_path(chain)}\n{self._folder_totals_text(d)}")

    def _update_crumbs(self):
        for w in self.crumbs.winfo_children():
            w.destroy()
        header = self.loaded[1] if self.loaded else {}
        parts = [(header.get("name", "Snapshot"), [])]
        for i in range(len(self.cur_chain)):
            label = self._root_label(self.cur_chain[0]) if i == 0 else self.cur_chain[i]["n"]
            parts.append((label, self.cur_chain[:i + 1]))
        if len(parts) > 7:
            parts = parts[:1] + [None] + parts[-5:]
        for i, part in enumerate(parts):
            if i:
                ttk.Label(self.crumbs, text="›").pack(side="left", padx=1)
            if part is None:
                ttk.Label(self.crumbs, text="…").pack(side="left")
                continue
            label, ch = part
            ttk.Button(self.crumbs, text=label, style="Toolbutton",
                       command=lambda ch=ch: self._go_to(ch)).pack(side="left")
        self.up_btn.state(["!disabled"] if self.cur_chain else ["disabled"])
        self.back_btn.state(["!disabled"] if self.history else ["disabled"])

    def _go_to(self, chain):
        cur = self.cur_chain
        select = cur[len(chain)] if len(cur) > len(chain) and same_chain(cur[:len(chain)], chain) else None
        self._show_folder(chain, select=select)

    def _go_up(self):
        if self.zoom and not self.search_mode and self.cur_chain:
            self._show_folder(self.cur_chain[:-1], select=self.cur_chain[-1])
        return "break"

    def _go_back(self):
        if self.zoom and not self.search_mode and self.history:
            prev = self.history.pop()
            cur = self.cur_chain
            select = cur[len(prev)] if len(cur) > len(prev) and same_chain(cur[:len(prev)], prev) else None
            self._show_folder(prev, select=select, record=False)
        return "break"

    def _open_item(self, iid):
        """Double-click / Enter in the list: jump from a search result to where it lives."""
        if iid not in self.nodes or not self.search_mode:
            return False
        chain = self.nodes[iid][3]
        if self.zoom:
            self.v_search.set("")
            self._show_folder(chain[:-1], select=chain[-1], record=False)
        else:
            self._reveal(chain)
        return True

    def _on_view_double(self, event):
        iid = self.view.identify_row(event.y)
        if iid and self._open_item(iid):
            return "break"

    def _on_view_enter(self, _event):
        if self._open_item(self.view.focus()):
            return "break"

    # ---------- zoom

    def _on_ctrl_wheel(self, event):
        self._zoom(1 if event.delta > 0 else -1)
        return "break"

    def _selected_chain(self):
        if self.zoom and not self.search_mode:
            item = self.icon_grid.selected()
            return item[3] if item else None
        sel = self.view.selection()
        return self.nodes[sel[0]][3] if sel and sel[0] in self.nodes else None

    def _zoom(self, step):
        new = max(0, min(len(GRID_ICON_SIZES), self.zoom + step))
        if new == self.zoom or not self.loaded:
            return "break"
        sel = self._selected_chain()
        old = self.zoom
        self.zoom = new
        was_search = self.search_mode
        if was_search:
            self.search_mode = False
            self.v_search.set("")
        if new == 0:
            # Back to the tree: expand down to whatever was selected (or the folder we were in).
            if sel:
                self._reveal(sel)
            elif self.cur_chain:
                self._reveal(self.cur_chain, open_last=True)
            else:
                self._show_tree()
            self.view.focus_set()
        elif old == 0 or was_search:
            # Enter icon view in the folder that contains the selected item.
            self.history.clear()
            if sel:
                self._show_folder(sel[:-1], select=sel[-1], record=False)
            else:
                self._show_folder(self._default_chain(), record=False)
        else:
            self.icon_grid.set_size(self.grid_sizes[new - 1])
        return "break"

    # ---------- details / search

    @staticmethod
    def _folder_totals_text(d):
        direct = f"{plural(len(d.get('d', ())), 'folder')} and {plural(len(d.get('f', ())), 'file')} directly inside"
        if d.get("x"):
            return direct + "  ·  Access was denied when the snapshot was taken."
        if d.get("u"):
            return direct + "  ·  Contents were not scanned (subfolder setting)."
        if d.get("l"):
            return direct + "  ·  This is a link/junction; it was not followed."
        total = (f"Total: {human_size(d.get('_s', 0))} in {plural(d.get('_nf', 0), 'file')}"
                 f" and {plural(d.get('_nd', 0), 'subfolder')}")
        if d.get("_p"):
            total += " (at least; some subfolders couldn't be read or weren't scanned)"
        return f"{direct}  ·  {total}  ·  Modified: {fmt_time(d.get('m')) or 'unknown'}"

    def _on_item_selected(self):
        sel = self.view.selection()
        if sel and sel[0] in self.nodes:
            self._describe(*self.nodes[sel[0]][:3])

    def _describe(self, kind, node, full):
        if kind == "f":
            self.v_details.set(f"{full}\nOriginal size: {human_size(node[1])} ({node[1]:,} bytes)  ·  "
                               f"Modified: {fmt_time(node[2]) or 'unknown'}  ·  "
                               "Stored in the snapshot as a 0-byte placeholder (name only).")
        else:
            self.v_details.set(f"{full}\n{self._folder_totals_text(node)}")

    def run_search(self):
        if not self.loaded:
            return
        q = self.v_search.get().strip().lower()
        if not q:
            self.clear_search()
            return
        results = []
        stack = [(r, r["n"], [r]) for r in reversed(self.loaded[2])]
        truncated = False
        while stack and not truncated:
            node, full, chain = stack.pop()
            for f in node.get("f", ()):
                if q in f[0].lower():
                    results.append(("f", f, full, chain + [f]))
            for d in reversed(node.get("d", ())):
                if q in d["n"].lower():
                    results.append(("d", d, full, chain + [d]))
                stack.append((d, os.path.join(full, d["n"]), chain + [d]))
            truncated = len(results) >= SEARCH_LIMIT
        results = results[:SEARCH_LIMIT]
        self.search_mode = True
        self._use_grid(False)
        self.view["displaycolumns"] = ("size", "items", "modified", "location")
        self.view_fitter.fit()
        self._clear_view()
        for kind, node, parent, chain in results:
            if kind == "f":
                self._insert_file("", node, os.path.join(parent, node[0]), chain, location=parent)
            else:
                self._insert_dir("", node, os.path.join(parent, node["n"]), chain, lazy=False, location=parent)
        n = len(results)
        more = f" (showing first {SEARCH_LIMIT:,})" if truncated else ""
        self.v_details.set(f"{n:,} match{'es' if n != 1 else ''} for \u201c{q}\u201d{more}. "
                           "Double-click a result to show where it is. Press Esc to clear.")

    def clear_search(self):
        self.v_search.set("")
        if not self.search_mode:
            return
        if self.zoom:
            self._show_folder(self.cur_chain, record=False)
        else:
            self._show_tree()

    # ---------- snapshot file actions

    def open_snapshot_file(self):
        path = filedialog.askopenfilename(title="Open snapshot",
                                          filetypes=[("Directory snapshots", "*" + EXT), ("All files", "*.*")])
        if not path:
            return
        path = os.path.abspath(path)
        try:
            read_header(path)
        except Exception as e:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"That file isn't a readable snapshot:\n{e}")
            return
        if path not in self.cfg["known_snapshots"]:
            self.cfg["known_snapshots"].append(path)
            self._persist()
        self.refresh_snapshot_list(select=path)

    def show_in_folder(self):
        path = self._selected_snapshot()
        if not path:
            return
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path)])

    def delete_snapshot(self):
        path = self._selected_snapshot()
        if not path:
            return
        if not messagebox.askyesno(APP_NAME, f"Delete this snapshot file?\n\n{path}\n\n"
                                             "Copies saved in other locations are not affected.", icon="warning"):
            return
        # Pick the neighbouring row so repeated Del presses keep working down the list.
        rows = list(self.snap_list.get_children())
        idx = rows.index(path)
        neighbour = rows[idx + 1] if idx + 1 < len(rows) else (rows[idx - 1] if idx > 0 else None)
        try:
            os.remove(path)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Could not delete:\n{e}")
            return
        if self.loaded and self.loaded[0] == path:
            self.loaded = None
            self._clear_view()
            if self.zoom:
                self._show_folder([], record=False)
            self.v_view_title.set("Select a snapshot to browse it")
            self.v_details.set("")
            self.export_btn.configure(state="disabled")
        self.cfg["known_snapshots"] = [p for p in self.cfg["known_snapshots"] if p != path]
        self._persist()
        self.v_snap_path.set("")
        self.snap_list.selection_remove(*self.snap_list.selection())
        self.refresh_snapshot_list(select=neighbour)
        self.snap_list.focus_set()
        if neighbour and self.snap_list.exists(neighbour):
            self.snap_list.focus(neighbour)

    def export_snapshot(self):
        if not self.loaded or self.busy:
            return
        _path, header, trees = self.loaded
        dlg = ExportDialog(self, header, trees, self.cfg.get("export_dir", ""), self.cfg.get("export_full_path", False))
        if not dlg.result:
            return
        target, base, full_path = dlg.result
        self.cfg["export_dir"], self.cfg["export_full_path"] = target, full_path
        self._persist()
        tot = header.get("totals", {})
        count = tot.get("folders", 0) + tot.get("files", 0)
        cancel = self.cancel_event = threading.Event()
        self._set_busy(True, "Creating folder tree…")
        self.v_details.set("Creating folder tree…")

        def work():
            try:
                done, errors = export_empty_tree(
                    trees, base, full_path,
                    lambda n: self.post(lambda: self.v_details.set(f"Creating folder tree… {n:,} of {count:,}")), cancel)
                self.post(lambda: self._export_done(base, done, errors))
            except Cancelled:
                self.post(lambda: (self._set_busy(False, "Export cancelled."), self.v_details.set("Export cancelled.")))
            except Exception as e:  # noqa: BLE001
                self.post(lambda e=e: (self._set_busy(False, "Export failed."),
                                       messagebox.showerror(APP_NAME, f"Export failed:\n{e}")))

        threading.Thread(target=work, daemon=True).start()

    def _export_done(self, base, done, errors):
        self._set_busy(False, "Folder tree created.")
        self.v_details.set(f"Created {done:,} items in {base}")
        msg = f"Created {done:,} empty folders and 0-byte files in:\n\n{base}"
        if errors:
            msg += f"\n\n{errors:,} item(s) could not be created."
        if messagebox.askyesno(APP_NAME, msg + "\n\nOpen the folder now?"):
            if os.name == "nt":
                os.startfile(base)
            else:
                subprocess.Popen(["xdg-open", str(base)])


def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            # Own taskbar identity so Windows shows our icon rather than Python's.
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DirectorySnapshotter.App")
        except (AttributeError, OSError):
            pass
    App().mainloop()


if __name__ == "__main__":
    sys.exit(main())
