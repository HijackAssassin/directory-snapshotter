"""
Update check for Directory Snapshotter.

Reads the app's page on teamwolfgamers.online - the same "Latest Version" and
Download button the page shows. The website fills those in from the download's
file name when it is published ("3 push to website.bat"), so publishing a new
build is all it takes for every copy of the app to see it. The domain is tried
first and the duckdns name second, in case the domain ever lapses.

Nothing is downloaded or installed here: the app offers to open the download
in the browser.

The version of this copy is in version.txt (bundled into the exe by build.py).
"""

import os
import re
import sys
import urllib.request

PAGE_KEY = "directory-snapshotter"
PAGES = ("https://teamwolfgamers.online/" + PAGE_KEY,
         "https://pattywatty.duckdns.org/" + PAGE_KEY)


def _read_version():
    # Bundled into the build; run from source it is the project's Website\version.txt (missing in a clone).
    here = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    for path in (os.path.join(here, "version.txt"), os.path.join(here, "..", "Website", "version.txt")):
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip() or "0.0.0"
        except OSError:
            pass
    return "0.0.0"


APP_VERSION = _read_version()


def version_tuple(text):
    nums = [int(n) for n in re.findall(r"\d+", text or "")[:4]]
    return tuple(nums) + (0,) * (4 - len(nums))


def is_newer(remote, local=APP_VERSION):
    return version_tuple(remote) > version_tuple(local)


def check(timeout=10):
    """
    What the website offers: {'version', 'download', 'page'} with full URLs
    ('download' falls back to the page). Raises OSError if neither address answers.
    """
    last = None
    for page in PAGES:
        try:
            req = urllib.request.Request(
                page, headers={"User-Agent": f"DirectorySnapshotter/{APP_VERSION}"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                html = r.read().decode("utf-8", "replace")
        except Exception as exc:          # try the other address
            last = exc
            continue
        m = re.search(rf'data-version="{PAGE_KEY}">\s*V?([^<\s]+)\s*<', html)
        if not m:
            last = OSError(f"no version on {page}")
            continue
        base = "/".join(page.split("/", 3)[:3])
        download = page
        hit = re.search(rf'data-download="{PAGE_KEY}" href="([^"]+)"', html)
        if hit and not hit.group(1).rstrip("/").endswith("downloads"):
            href = hit.group(1)
            download = href if href.startswith("http") else base + href
        return {"version": m.group(1), "download": download, "page": page}
    raise OSError(f"Could not reach the website ({last})")
