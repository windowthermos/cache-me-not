"""
Cache Cleaner for macOS.

Switches  (Clean button): desktop-app caches, browser caches, other app caches,
                          old logs, developer caches, Claude VM, Codex runtimes.
Review    (you pick):     leftovers from removed apps, old installers -> moved to Trash;
                          Empty Trash.

Never touches cookies, logins, Keychain, Apple system caches or app data.
Needs:  pip install pywebview
"""

import datetime
import json
import os
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME = Path.home()
LIB = HOME / "Library"
SUPPORT = LIB / "Application Support"
CACHES = LIB / "Caches"
APP_DIR = SUPPORT / "CacheCleaner"
CONFIG = APP_DIR / "config.json"
LOG = LIB / "Logs/CacheCleaner.log"
BUNDLE_ID = "local.cachecleaner"
OLD_PLIST = LIB / "LaunchAgents/local.cachecleaner.plist"  # legacy schedule from older versions
SYS_LIB = Path("/Library")

DEFAULTS = {"desktop": True, "browsers": True, "other": True, "logs": True,
            "developer": False, "claude_vm": False, "codex": False}

# Cache folders inside an Electron app's support folder (logins live elsewhere)
ELECTRON_CACHES = ("Cache", "Code Cache", "GPUCache", "GraphiteDawnCache",
                   "DawnGraphiteCache", "DawnWebGPUCache", "ShaderCache", "GrShaderCache")
ELECTRON_MARKERS = ("Code Cache", "GPUCache")
NICE_NAMES = {"Code": "VS Code", "discord": "Discord", "whatsapp": "WhatsApp"}

BROWSERS = [
    ("Chrome", "Google Chrome", [CACHES / "Google/Chrome"]),
    ("Edge", "Microsoft Edge", [CACHES / "Microsoft Edge"]),
    ("Brave", "Brave Browser", [CACHES / "BraveSoftware"]),
    ("Arc", "Arc", [CACHES / "Arc"]),
    ("Firefox", "Firefox", [CACHES / "Firefox"]),
    ("Safari", "Safari", [CACHES / "com.apple.Safari"]),
]

DEV_CACHES = [("pip", CACHES / "pip"), ("Homebrew", CACHES / "Homebrew"),
              ("npm", HOME / ".npm/_cacache"), ("Yarn", CACHES / "Yarn"), ("uv", HOME / ".cache/uv")]

OTHER_SKIP = {"Google", "Microsoft Edge", "BraveSoftware", "Arc", "Firefox", "pip", "Homebrew",
              "Yarn", "com.openai.chat", "com.spotify.client", BUNDLE_ID, "Cache Cleaner"}

# Big folders tools leave in your home folder: (folder, label, app names, commands).
# Listed only when none of the apps or commands are found, and never pre-ticked.
KNOWN_LEFTOVERS = [
    (HOME / ".ollama", "Ollama models", ["Ollama"], ["ollama"]),
    (HOME / ".lmstudio", "LM Studio models", ["LM Studio"], ["lms"]),
    (HOME / ".cache/lm-studio", "LM Studio models", ["LM Studio"], ["lms"]),
    (HOME / ".docker", "Docker", ["Docker"], ["docker"]),
    (HOME / ".gradle", "Gradle", ["Android Studio"], ["gradle"]),
    (HOME / ".codex", "Codex settings", ["Codex", "ChatGPT"], ["codex"]),
]

# Application Support folders that belong to macOS or to apps that keep data there
PROTECTED_SUPPORT = {
    "addressbook", "callhistorydb", "callhistorytransactions", "clouddocs", "crashreporter",
    "diskimages", "dock", "facetime", "fileprovider", "knowledge", "mobilesync", "syncservices",
    "icloud", "animoji", "accountsd", "icdd", "networkserviceproxy", "appstore", "applepushservice",
    "accessibility", "coreparsec", "caches", "familycircle", "differentialprivacy", "stickersd",
    "transparencyd", "siri", "mail", "safari", "photos", "music", "tv", "podcasts", "books", "maps",
    "notes", "reminders", "calendars", "contacts", "keychains", "preferences", "cloudkit",
    "controlcenter", "notificationcenter", "spotlight", "freeform", "shortcuts", "weather",
    "wallpaper", "screen sharing", "quick look", "containers", "group containers", "cachecleaner",
    "mcneel", "autodesk", "crashpad", "homeenergyd", "locationaccessstored", "identityservicesd",
}

LEFTOVER_MIN = 20_000_000          # named folders: only list if this big
LEFTOVER_IDLE_DAYS = 30            # ...and unused this long
LOG_AGE_DAYS = 7

# ---- Leftovers matched by app ID (the deep scan) ----
# Every place macOS apps keep files, named by their bundle ID. (folder, needs admin)
ID_PLACES = [
    (SUPPORT, False), (CACHES, False), (LIB / "Containers", False),
    (LIB / "Group Containers", False), (LIB / "Preferences", False),
    (LIB / "HTTPStorages", False), (LIB / "Logs", False),
    (LIB / "Saved Application State", False), (LIB / "WebKit", False),
    (LIB / "Application Scripts", False), (LIB / "Cookies", False),
    (LIB / "LaunchAgents", False),
    (SYS_LIB / "Application Support", True), (SYS_LIB / "Caches", True),
    (SYS_LIB / "Preferences", True), (SYS_LIB / "LaunchAgents", True),
    (SYS_LIB / "LaunchDaemons", True), (SYS_LIB / "PrivilegedHelperTools", True),
]
# Places that only exist for real apps (proof an app was once installed)
APP_EVIDENCE = {"Containers", "Saved Application State", "Application Scripts"}
# Never listed, whatever the scan says: macOS, printing, Shortcuts, Python, your own jobs
NEVER_TOUCH = ("com.apple.", "org.cups.", "is.workflow.", "org.python.", "org.pythonmac.",
               BUNDLE_ID)
DOMAIN_ENDS = {"com", "org", "net", "io", "co", "us", "de", "uk", "fr", "jp", "me", "app", "dev",
               "ai", "tv", "cc", "ch", "nl", "se", "it", "in", "ru", "cn", "eu", "info", "edu", "is"}
GROUP_MIN = 100_000                # hide app groups smaller than this (unless they run in the background)
PRETTY = {"microsoft": "Microsoft", "teams2": "Teams", "teams": "Teams", "onenote": "OneNote",
          "onedrive": "OneDrive", "sidefx": "Houdini (SideFX)", "zoom": "Zoom", "openai": "OpenAI",
          "autodesk": "Autodesk", "jetbrains": "JetBrains", "google": "Google", "adobe": "Adobe",
          "mcneel": "Rhino (McNeel)", "logitech": "Logitech", "logi": "Logitech", "wdc": "WD",
          "sharepoint-mac": "SharePoint", "github": "GitHub", "spotify": "Spotify"}


# ---------------------------------------------------------------- helpers

def human(n):
    """Bytes as Finder shows them (1000-based)."""
    if n is None:
        return "—"
    if n < 1000:
        return "Zero KB" if n == 0 else f"{n} bytes"
    for unit, div in (("KB", 1e3), ("MB", 1e6), ("GB", 1e9), ("TB", 1e12)):
        v = n / div
        if v < 1000 or unit == "TB":
            return f"{v:.0f} {unit}" if unit == "KB" or v >= 100 else f"{v:.1f} {unit}"


def size_of(paths):
    paths = [str(p) for p in paths if os.path.lexists(p)]
    if not paths:
        return 0
    r = subprocess.run(["du", "-skc", *paths], capture_output=True, text=True)
    try:
        return int(r.stdout.strip().splitlines()[-1].split()[0]) * 1024
    except (IndexError, ValueError):
        return 0


def readable(p):
    try:
        os.listdir(p)
        return True
    except PermissionError:
        return False
    except OSError:
        return True


def subdirs(p):
    try:
        return sorted(x for x in Path(p).iterdir() if x.is_dir() and not x.is_symlink())
    except OSError:
        return []


def delete(path):
    p = Path(path)
    if p.is_symlink() or p.is_file():
        p.unlink()
    elif p.is_dir():
        shutil.rmtree(p)


def recently_used(path, days):
    """True if anything inside was modified in the last `days` days."""
    cutoff = time.time() - days * 86400
    try:
        if os.lstat(path).st_mtime > cutoff:
            return True
        for root, dirs, files in os.walk(path):
            for n in files + dirs:
                try:
                    if os.lstat(os.path.join(root, n)).st_mtime > cutoff:
                        return True
                except OSError:
                    continue
    except OSError:
        return True
    return False


def last_modified(path):
    try:
        return os.lstat(path).st_mtime
    except OSError:
        return 0


def ago(ts):
    if not ts:
        return ""
    d = (time.time() - ts) / 86400
    if d < 1:
        return "today"
    if d < 2:
        return "yesterday"
    if d < 60:
        return f"{int(d)} days ago"
    if d < 730:
        return f"{int(d / 30)} months ago"
    return f"{int(d / 365)} years ago"


def to_trash(path):
    """Move to the Trash the way Finder does (keeps Put Back). Falls back to a plain move."""
    try:
        from Foundation import NSFileManager, NSURL
        ok, _, err = NSFileManager.defaultManager().trashItemAtURL_resultingItemURL_error_(
            NSURL.fileURLWithPath_(str(path)), None, None)
        if ok:
            return True
    except Exception:
        pass
    try:
        dest = HOME / ".Trash" / Path(path).name
        if dest.exists():
            dest = dest.with_name(f"{dest.name} {int(time.time())}")
        shutil.move(str(path), dest)
        return True
    except OSError:
        return False


def running_processes():
    """Full paths of every running process."""
    r = subprocess.run(["ps", "-axo", "comm="], capture_output=True, text=True)
    return [line.strip() for line in r.stdout.splitlines() if line.strip()]


def running_apps():
    """Lower-case names, bundle IDs and executables of everything running."""
    names = set()
    try:
        from AppKit import NSWorkspace
        for a in NSWorkspace.sharedWorkspace().runningApplications():
            for v in (a.localizedName(), a.bundleIdentifier(),
                      a.executableURL() and a.executableURL().lastPathComponent()):
                if v:
                    names.add(str(v).lower())
    except Exception:
        pass
    for line in running_processes():
        names.add(os.path.basename(line).lower())
        if ".app/" in line:
            names.add(line.split(".app/")[0].rsplit("/", 1)[-1].lower())
    return names


def is_open(app, running):
    a = app.lower()
    return a in running or any(n.startswith(a + " helper") for n in running)


STOPWORDS = {"com", "org", "net", "io", "co", "de", "us", "app", "apps", "mac", "macos", "osx",
             "desktop", "helper", "inc", "ltd", "the"}


def bundle_of(path):
    """The outermost .app a file path sits in, if any."""
    p = str(path)
    i = p.find(".app/")
    return Path(p[:i + 4]) if i >= 0 else None


def find_apps():
    """Every installed .app: usual folders, other drives, Spotlight and running apps."""
    roots = [Path("/Applications"), Path("/Applications/Utilities"), HOME / "Applications",
             Path("/System/Applications"), Path("/System/Applications/Utilities")]
    roots += [v / "Applications" for v in subdirs("/Volumes")]
    apps = set()
    for r in roots:
        for p in subdirs(r):
            if p.suffix == ".app":
                apps.add(p)
            else:
                apps.update(q for q in subdirs(p) if q.suffix == ".app")
    try:
        out = subprocess.run(["mdfind", "kMDItemContentType == 'com.apple.application-bundle'"],
                             capture_output=True, text=True, timeout=15).stdout
        apps.update(Path(l) for l in out.splitlines() if l.endswith(".app") and ".app/" not in l)
    except Exception:
        pass
    for line in running_processes():
        b = bundle_of(line)
        if b:
            apps.add(b)
    return apps


def read_plist(p):
    try:
        with open(p, "rb") as f:
            return plistlib.load(f)
    except Exception:
        return {}


NESTED_SKIP = {"Resources", "_CodeSignature", "MacOS", "Headers", "Modules", "Versions", "Current"}


def nested_ids(app, limit=600):
    """Bundle IDs of an app and of the helpers, extensions and agents inside it."""
    ids = []
    seen = 0
    for root, dirs, _ in os.walk(app):
        seen += 1
        if seen > limit:
            break
        dirs[:] = [d for d in dirs if d not in NESTED_SKIP and not d.endswith(".lproj")]
        if root.endswith((".app", ".appex", ".xpc", ".systemextension", ".bundle", ".plugin")):
            bid = read_plist(Path(root) / "Contents/Info.plist").get("CFBundleIdentifier")
            if bid:
                ids.append(str(bid).lower())
    return ids


def launch_agent_words():
    """Words from your own background jobs, so tools they run count as installed."""
    words = set()
    for p in (LIB / "LaunchAgents").glob("*.plist"):
        d = read_plist(p)
        for a in [d.get("Program")] + list(d.get("ProgramArguments") or []):
            if isinstance(a, str):
                words.update(w.lower() for w in re.split(r"[\s/=:;'\"-]+", a) if len(w) > 2)
    return words


class Installed:
    """What is installed: app names (tokens), bundle IDs, vendors."""

    def __init__(self):
        self.apps = find_apps()
        self.tokens = set()
        self.ids = set()
        for a in self.apps:
            self._add(a.stem)
            info = read_plist(a / "Contents/Info.plist")
            for k in ("CFBundleIdentifier", "CFBundleName", "CFBundleDisplayName", "CFBundleExecutable"):
                self._add(info.get(k))
            for part in str(info.get("CFBundleIdentifier", "")).lower().split("."):
                if part and part not in STOPWORDS:
                    self._add(part)
            self.ids.update(nested_ids(a))
        self.tokens |= launch_agent_words()
        self.prefixes = set()
        self.pairs = set()
        self.vendors = set()
        for i in self.ids:
            parts = i.split(".")
            for k in range(2, len(parts) + 1):
                self.prefixes.add(".".join(parts[:k]))
            v, p = vendor_product(i)
            self.vendors.add(v)
            self.pairs.add((v, p))

    def _add(self, s):
        if s:
            s = str(s).lower()
            self.tokens.add(s)
            self.tokens.add(s.replace(" ", ""))

    def owns(self, lid):
        """True if an installed app (or something inside one) uses this ID."""
        if lid in self.prefixes:
            return True
        parts = lid.split(".")
        if any(".".join(parts[:k]) in self.ids for k in range(2, len(parts))):
            return True
        v, p = vendor_product(lid)
        if (v, p) in self.pairs:
            return True
        return len(p) > 3 and p not in STOPWORDS and p in self.tokens

    def has_vendor(self, v):
        return v in self.vendors or v in self.tokens


def on_path(cmd):
    path = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin:" + str(HOME / ".local/bin")
    return shutil.which(cmd, path=path) is not None


# ---------------------------------------------------------------- switch rows

def desktop_units():
    units = []
    for d in subdirs(SUPPORT):
        if d.name in ("Google", "Microsoft Edge", "BraveSoftware", "Arc", "Firefox", "CacheCleaner"):
            continue
        if not any((d / m).is_dir() for m in ELECTRON_MARKERS):
            continue
        paths = [d / c for c in ELECTRON_CACHES if (d / c).is_dir()]
        units.append({"name": NICE_NAMES.get(d.name, d.name), "app": d.name, "paths": paths})
    chatgpt = CACHES / "com.openai.chat"
    if chatgpt.exists():
        units.append({"name": "ChatGPT", "app": "ChatGPT", "paths": [chatgpt]})
    return units


def browser_units():
    units, locked = [], []
    for name, app, paths in BROWSERS:
        paths = [p for p in paths if os.path.lexists(p)]
        if name == "Chrome":
            base = SUPPORT / "Google/Chrome"
            for prof in subdirs(base):
                if prof.name == "Default" or prof.name.startswith("Profile "):
                    paths += [prof / c for c in ("Code Cache", "GPUCache") if (prof / c).is_dir()]
            paths += [base / c for c in ("GraphiteDawnCache", "GrShaderCache", "ShaderCache") if (base / c).is_dir()]
        if not paths:
            continue
        if not all(readable(p) for p in paths):
            locked.append(name)
            continue
        units.append({"name": name, "app": app, "paths": paths})
    return units, locked


def other_units():
    claimed = {p.name for _, _, ps in BROWSERS for p in ps}
    units = []
    for d in subdirs(CACHES):
        n = d.name
        if n in OTHER_SKIP or n in claimed or n.startswith("com.apple.") or n.lower() == "apple":
            continue
        units.append({"name": n, "app": n.rsplit(".", 1)[-1], "bundle": n, "paths": [d]})
    return units


def log_files():
    cutoff = time.time() - LOG_AGE_DAYS * 86400
    files, total = [], 0
    for root, _, names in os.walk(LIB / "Logs"):
        for n in names:
            p = os.path.join(root, n)
            if p == str(LOG):
                continue
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if st.st_mtime < cutoff:
                files.append(p)
                total += st.st_size
    return files, total


def scan_switches(running):
    out = {}

    def group(units, extra_open=lambda u: False):
        ok, blocked, size = [], [], 0
        for u in units:
            u["size"] = size_of(u["paths"])
            if u["size"] == 0:
                continue
            if (is_open(u["app"], running) or ("bundle" in u and u["bundle"].lower() in running)
                    or extra_open(u)):
                blocked.append(u)
                continue
            ok.append(u)
            size += u["size"]
        return ok, blocked, size

    def names(units, n=4):
        ns = sorted({u["name"] for u in units}, key=str.lower)
        return ", ".join(ns[:n]) + (f" + {len(ns) - n} more" if len(ns) > n else "")

    ok, bl, sz = group(desktop_units())
    out["desktop"] = dict(units=ok, size=sz, blocked=[u["name"] for u in bl],
                          sub=names(ok) or "Nothing to clean")

    bu, locked = browser_units()
    ok, bl, sz = group(bu)
    sub = ", ".join(u["name"] for u in ok) or ("" if bl else "No browser caches found")
    out["browsers"] = dict(units=ok, size=sz, blocked=[u["name"] for u in bl], sub=sub, locked=locked)

    ok, bl, sz = group(other_units())
    out["other"] = dict(units=ok, size=sz, blocked=[u["name"] for u in bl],
                        sub=f"{len(ok)} app{'' if len(ok) == 1 else 's'} in Library/Caches"
                            + (f", {len(bl)} open apps skipped" if bl else ""))

    files, sz = log_files()
    out["logs"] = dict(units=[{"name": "logs", "app": "", "files": files}], size=sz, blocked=[],
                       sub=f"Older than {LOG_AGE_DAYS} days")

    dev = [{"name": n, "app": n, "paths": [p]} for n, p in DEV_CACHES if p.exists()]
    ok, bl, sz = group(dev, lambda u: is_open("brew", running) and u["name"] == "Homebrew")
    out["developer"] = dict(units=ok, size=sz, blocked=[u["name"] for u in bl],
                            sub=", ".join(u["name"] for u in dev) or "None found")

    vm = SUPPORT / "Claude/vm_bundles"
    ok, bl, sz = group([{"name": "Claude", "app": "Claude", "paths": [vm]}])
    out["claude_vm"] = dict(units=ok, size=sz, blocked=[u["name"] for u in bl],
                            sub="Downloads again when Claude needs it" if ok or bl else "Not downloaded")

    cx = HOME / ".cache/codex-runtimes"
    ok, bl, sz = group([{"name": "Codex", "app": "codex", "paths": [cx]}])
    out["codex"] = dict(units=ok, size=sz, blocked=[u["name"] for u in bl],
                        sub="Downloads again when Codex needs it" if ok or bl else "Not installed")
    return out


# ---------------------------------------------------------------- leftovers

def leftover_id(name):
    """Turn a Library entry name into the app ID it belongs to, or None."""
    n = name
    for suf in (".plist", ".savedState", ".binarycookies"):
        if n.lower().endswith(suf.lower()):
            n = n[:-len(suf)]
            break
    if n.lower().startswith("group."):
        n = n[6:]
    if re.match(r"^[A-Z0-9]{10}\.", n) and re.search(r"\d", n[:10]):   # Team ID prefix
        n = n[11:]
    n = n.lower()
    if n.startswith("group."):
        n = n[6:]
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*(\.[a-z0-9_-]+)+", n):
        return None
    parts = n.split(".")
    if parts[0] not in DOMAIN_ENDS and parts[-1] not in DOMAIN_ENDS:
        return None
    return n


def vendor_product(lid):
    parts = lid.split(".")
    if parts[0] in DOMAIN_ENDS and len(parts) > 1:
        v = parts[1]
        return v, (parts[2] if len(parts) > 2 else v)
    return parts[0], parts[0]                       # zoom.us style


def pretty(s):
    if s in PRETTY:
        return PRETTY[s]
    s = re.sub(r"\d+$", "", s).replace("-", " ").replace("_", " ")
    return s[:1].upper() + s[1:]


def launch_program(p):
    d = read_plist(p)
    prog = d.get("Program") or (d.get("ProgramArguments") or [None])[0]
    return prog if isinstance(prog, str) else None


def launch_is_own_or_live(p):
    """Skip background jobs that are yours (scripts, Homebrew) or whose app is still there."""
    prog = launch_program(p)
    if not prog:
        return False
    if os.path.exists(prog):
        if ".app/" in prog:
            return True
        app_helper = prog.startswith(("/Library/Application Support/", "/Library/PrivilegedHelperTools/",
                                      str(SUPPORT) + "/"))
        return not app_helper
    return False


def scan_id_leftovers(inst, running):
    """Group everything named after an app ID that no installed app owns."""
    items = {}
    for place, admin in ID_PLACES:
        try:
            entries = sorted(place.iterdir())
        except OSError:
            continue
        for e in entries:
            lid = leftover_id(e.name)
            if not lid or lid.startswith(NEVER_TOUCH) or inst.owns(lid):
                continue
            launch = place.name in ("LaunchAgents", "LaunchDaemons")
            if launch and launch_is_own_or_live(e):
                continue
            if lid in running or any(r.startswith(lid) for r in running if "." in r):
                continue
            v, p = vendor_product(lid)
            if v == "apple":
                continue
            prog = launch_program(e) if launch else None
            items.setdefault((v, p), []).append(
                {"path": str(e), "admin": admin, "launch": launch, "where": place.name,
                 "live": bool(prog and os.path.exists(prog))})

    groups = {}
    for (v, p), its in items.items():
        if inst.has_vendor(v):
            # Company still has an app here: list only real apps (proof: container or saved state),
            # and pull in that app's helpers (OneDrive -> OneDriveUpdaterDaemon ...).
            if not any(i["where"] in APP_EVIDENCE for i in its):
                continue
            key = f"{v}.{p}"
            label = f"{pretty(v)} {pretty(p)}"
            ticked = False
        else:
            key, label, ticked = v, pretty(v), True
        g = groups.setdefault(key, {"name": label, "members": [], "ticked": ticked, "vendor": v, "product": p})
        g["members"] += its

    # helpers of a listed app whose names only start with its product name
    for (v, p), its in items.items():
        if not inst.has_vendor(v):
            continue
        for key, g in groups.items():
            gp = g["product"]
            if g["vendor"] == v and p != gp and len(gp) >= 4 and p.startswith(gp):
                g["members"] += its
                break

    found = []
    for key, g in groups.items():
        seen, members = set(), []
        for m in g["members"]:
            if m["path"] not in seen:
                seen.add(m["path"])
                m["size"] = size_of([m["path"]])
                members.append(m)
        size = sum(m["size"] for m in members)
        bg = any(m["launch"] or m["where"] == "PrivilegedHelperTools" for m in members)
        if size < GROUP_MIN and not bg:
            continue
        newest = max(last_modified(m["path"]) for m in members)
        recent = any(recently_used(m["path"], LEFTOVER_IDLE_DAYS) for m in members if not m["admin"])
        admin = sum(m["admin"] for m in members)
        bits = [f"{len(members)} item{'s' if len(members) != 1 else ''}"]
        if bg:
            bits.append("background item")
        bits.append(f"last used {ago(newest)}" if newest else "")
        found.append({"path": "app:" + key, "name": g["name"], "size": size,
                      "checked": g["ticked"] and not recent and not any(m.get("live") for m in members),
                      "sub": " · ".join(b for b in bits if b), "members": members,
                      "admin": admin > 0})
    return found


def scan_leftovers(inst=None, running=None):
    inst = inst or Installed()
    running = running if running is not None else running_apps()
    tokens = inst.tokens
    found = scan_id_leftovers(inst, running)
    claimed = {m["path"] for g in found for m in g["members"]}
    seen = set()

    def single(path, name, size, sub):
        found.append({"path": str(path), "name": name, "size": size, "checked": False, "sub": sub,
                      "members": [{"path": str(path), "admin": False, "launch": False,
                                   "where": "", "size": size}], "admin": False})

    for folder, name, apps, cmds in KNOWN_LEFTOVERS:
        if (not folder.exists() or any(a.lower() in tokens for a in apps)
                or any(on_path(c) for c in cmds) or any(c in tokens for c in cmds)):
            continue
        size = size_of([folder])
        if size < 1_000_000:
            continue
        single(folder, name, size, f"~/{folder.relative_to(HOME)} · {name.split()[0]} is not installed")
        seen.add(str(folder))

    for d in subdirs(SUPPORT):
        n = d.name.lower()
        if n in PROTECTED_SUPPORT or n.startswith("com.apple") or "apple" in n:
            continue
        if n in tokens or n.replace(" ", "") in tokens:
            continue
        if any(part in tokens for part in n.split(".") if part not in STOPWORDS and len(part) > 2):
            continue
        if str(d) in seen or str(d) in claimed:
            continue
        size = size_of([d])
        if size < LEFTOVER_MIN or recently_used(d, LEFTOVER_IDLE_DAYS):
            continue
        single(d, d.name, size, f"Last used {ago(last_modified(d))} · ~/{d.relative_to(HOME)}")
    return sorted(found, key=lambda x: -x["size"])


def scan_installers():
    found = []
    week = time.time() - 604800
    for p in sorted((HOME / "Downloads").glob("*")):
        if p.suffix.lower() not in (".dmg", ".pkg", ".mpkg"):
            continue
        m = last_modified(p)
        found.append({"path": str(p), "name": p.name, "size": size_of([p]), "checked": m < week,
                      "sub": f"Downloaded {ago(m)}"})
    return sorted(found, key=lambda x: -x["size"])


def trash_size():
    t = HOME / ".Trash"
    return size_of([t]) if readable(t) else None


def allowed_leftover(path, admin):
    """Only ever move things that sit directly in one of the scanned places."""
    parent = str(Path(path).parent)
    return any(parent == str(place) and a == admin for place, a in ID_PLACES) or (
        not admin and (parent == str(SUPPORT) or parent == str(HOME) or parent == str(HOME / ".cache")))


def stop_user_agent(path):
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", path], capture_output=True)


def move_system_items(members):
    """Stop background items and move system files to a dated folder in the Trash, with one password prompt."""
    dest = HOME / ".Trash" / f"Leftovers {datetime.datetime.now():%Y-%m-%d %H.%M}"
    dest.mkdir(parents=True, exist_ok=True)
    uid, gid = os.getuid(), os.getgid()
    cmds = []
    for k, m in enumerate(members, 1):
        p = m["path"]
        if m["launch"]:
            dom = "system" if "/LaunchDaemons/" in p else f"gui/{uid}"
            cmds.append(f"/bin/launchctl bootout {dom} {shlex.quote(p)} >/dev/null 2>&1")
        cmds.append(f"/bin/mv -n {shlex.quote(p)} {shlex.quote(str(dest / f'{k:02d} {Path(p).name}'))}")
    cmds.append(f"/usr/sbin/chown -R {uid}:{gid} {shlex.quote(str(dest))}")
    script = "; ".join(cmds).replace("\\", "\\\\").replace('"', '\\"')
    n = len(members)
    prompt = f"Cache Cleaner needs your password to move {n} system item{'s' if n != 1 else ''} to the Trash."
    try:
        r = subprocess.run(["osascript", "-e",
                            f'do shell script "{script}" with administrator privileges with prompt "{prompt}"'],
                           capture_output=True, text=True)
        ok = r.returncode == 0
    except OSError:
        ok = False
    try:
        dest.rmdir()            # only removes it if nothing was moved
    except OSError:
        pass
    return ok


def trash_leftovers(groups):
    """Move whole app groups. Returns (freed, failed, needs_fda, cancelled)."""
    freed = failed = 0
    needs_fda = cancelled = False
    system = []
    for g in groups:
        for m in g["members"]:
            p = m["path"]
            if not os.path.lexists(p) or not allowed_leftover(p, m["admin"]):
                continue
            if m["admin"]:
                system.append(m)
                continue
            if m["launch"]:
                stop_user_agent(p)
            if to_trash(p):
                freed += m["size"]
            else:
                failed += 1
                needs_fda = needs_fda or not os.access(os.path.dirname(p), os.W_OK) or "Containers" in p
    if system:
        if not move_system_items(system):
            cancelled = True
        for m in system:
            if os.path.lexists(m["path"]):
                failed += 0 if cancelled else 1
            else:
                freed += m["size"]
    return freed, failed, needs_fda, cancelled


def clean(switches, ids):
    freed, lines = 0, []
    for i in ids:
        g = switches.get(i)
        if not g:
            continue
        errors = 0
        for u in g["units"]:
            for p in u.get("paths", []) + u.get("files", []):
                try:
                    delete(p)
                except OSError:
                    errors += 1
        after = sum(size_of(u.get("paths", [])) for u in g["units"])
        if i == "logs":
            after = sum(os.path.getsize(f) for f in g["units"][0]["files"] if os.path.exists(f))
        gone = max(g["size"] - after, 0)
        freed += gone
        lines.append(f"{i}: {human(gone)}" + (f" ({errors} protected items left)" if errors else ""))
        if g["blocked"]:
            lines.append(f"{i}: skipped open apps {', '.join(g['blocked'])}")
    return freed, lines


def write_log(lines):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a") as f:
        f.write(f"--- {datetime.datetime.now():%Y-%m-%d %H:%M}\n")
        f.writelines(line + "\n" for line in lines)


def load_config():
    cfg = {"selected": [k for k, v in DEFAULTS.items() if v], "last": None}
    try:
        cfg.update(json.loads(CONFIG.read_text()))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(**changes):
    cfg = load_config()
    cfg.update(changes)
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=2))


def remove_old_schedule():
    if OLD_PLIST.exists():
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{BUNDLE_ID}"],
                       capture_output=True)
        OLD_PLIST.unlink(missing_ok=True)


def disk():
    root = "/System/Volumes/Data" if os.path.exists("/System/Volumes/Data") else "/"
    return shutil.disk_usage(root)


ROWS = [
    ("desktop", "Desktop apps", "apps", "cache"),
    ("browsers", "Browsers", "globe", "cache"),
    ("other", "Other app caches", "box", "cache"),
    ("logs", "Old logs", "doc", "cache"),
    ("developer", "Developer", "term", "cache"),
    ("claude_vm", "Claude virtual machine", "cloud", "redl"),
    ("codex", "Codex runtimes", "cloud", "redl"),
]


# ---------------------------------------------------------------- window API

class Api:
    def __init__(self):
        self._window = None
        self._switches = {}
        self._review = {}
        self._lock = threading.Lock()

    def human(self, n):
        return human(n)

    def scan(self):
        with self._lock:
            running = running_apps()
            self._switches = scan_switches(running)
            leftovers, installers, tsize = scan_leftovers(running=running), scan_installers(), trash_size()
            self._review = {"leftovers": leftovers, "installers": installers}
            cfg, d = load_config(), disk()
            rows = []
            for rid, title, icon, section in ROWS:
                g = self._switches[rid]
                sub, warn, link = g["sub"], False, None
                if g["blocked"]:
                    sub, warn = ("Quit " + ", ".join(g["blocked"][:3]) + " to clean "
                                 + ("it" if len(g["blocked"]) == 1 else "them")), True
                    if rid == "other":
                        sub, warn = g["sub"], False
                if rid == "browsers" and g.get("locked"):
                    link = "Allow access for " + ", ".join(g["locked"])
                rows.append({"id": rid, "name": title, "icon": icon, "section": section, "sub": sub,
                             "warn": warn, "link": link, "size": g["size"], "size_h": human(g["size"]),
                             "selected": rid in cfg["selected"]})

            def rv(items, empty):
                s = sum(i["size"] for i in items)
                return {"count": len(items), "size": s, "size_h": human(s) if items else "",
                        "sub": ", ".join(sorted({i["name"].split()[0] for i in items})[:3]) if items else empty}

            inst = rv(installers, "None in Downloads")
            if installers:
                inst["sub"] = f"{len(installers)} file{'s' if len(installers) > 1 else ''} in Downloads"
            review = {
                "leftovers": rv(leftovers, "None found"),
                "installers": inst,
                "trash": {"size_h": human(tsize) if tsize is not None else "",
                          "sub": "Permanently delete" if tsize != 0 else "Empty"},
            }
            return {"rows": rows, "review": review, "free_h": human(d.free), "total_h": human(d.total),
                    "used": d.used / d.total, "last": cfg["last"]}

    def set_selected(self, ids):
        save_config(selected=ids)

    def clean(self, ids):
        total = human(sum(self._switches[i]["size"] for i in ids if i in self._switches))
        if not self._window.create_confirmation_dialog(
                f"Clear {total} of cache?",
                "Logins, cookies, files and settings are not affected. Apps rebuild these as needed."):
            return {"cancelled": True}
        freed, lines = clean(self._switches, ids)
        write_log(lines)
        save_config(last={"at": datetime.datetime.now().isoformat(timespec="minutes"), "freed": freed})
        return {"freed_h": human(freed)}

    def review_list(self, kind):
        return [{k: v for k, v in dict(i, size_h=human(i["size"])).items() if k != "members"}
                for i in self._review.get(kind, [])]

    def trash_items(self, kind, paths):
        allowed = {i["path"]: i for i in self._review.get(kind, [])}
        picked = [allowed[p] for p in paths if p in allowed]
        hint = ""
        if kind == "leftovers":
            freed, failed, needs_fda, cancelled = trash_leftovers(picked)
            failed += len(paths) - len(picked)
            if cancelled:
                hint = "system items skipped"
            elif needs_fda and failed:
                hint = "allow Full Disk Access to move the rest"
            moved = [m["path"] for g in picked for m in g["members"] if not os.path.lexists(m["path"])]
        else:
            freed = failed = 0
            moved = []
            for i in picked:
                if to_trash(i["path"]):
                    freed += i["size"]
                    moved.append(i["path"])
                else:
                    failed += 1
            failed += len(paths) - len(picked)
        write_log([f"Moved to Trash ({kind}): {human(freed)}"] + [f"  {p}" for p in moved])
        return {"freed_h": human(freed), "failed": failed, "hint": hint}

    def empty_trash(self):
        if not self._window.create_confirmation_dialog(
                "Empty the Trash?", "Items in the Trash will be deleted permanently. This can't be undone."):
            return {"cancelled": True}
        r = subprocess.run(["osascript", "-e", 'tell application "Finder" to empty trash'],
                           capture_output=True, text=True)
        return {"ok": r.returncode == 0}

    def open_fda(self):
        subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"])


def _modern_titlebar(window):
    """Hide the title bar so the content runs to the top, like System Settings."""
    try:
        import AppKit
        from PyObjCTools import AppHelper
    except Exception:
        return
    done = threading.Event()

    def apply():
        try:
            w = window.native
            w.setTitlebarAppearsTransparent_(True)
            w.setTitleVisibility_(AppKit.NSWindowTitleHidden)
            w.setStyleMask_(w.styleMask() | AppKit.NSWindowStyleMaskFullSizeContentView)
            done.set()
        except Exception:
            pass

    window.events.shown.wait(10)
    AppHelper.callAfter(apply)
    if done.wait(3):
        window.events.loaded.wait(10)
        window.evaluate_js("document.documentElement.classList.add('fullsize')")


def dark_mode():
    r = subprocess.run(["defaults", "read", "-g", "AppleInterfaceStyle"], capture_output=True, text=True)
    return "Dark" in r.stdout


def main():
    import webview
    remove_old_schedule()
    api = Api()
    window = webview.create_window(
        "Cache Cleaner", html=HTML, js_api=api, width=460, height=800, min_size=(420, 560),
        background_color="#1C1C1E" if dark_mode() else "#F0F0F3")
    api._window = window
    webview.start(_modern_titlebar, (window,))


HTML = r"""<!doctype html>
<html><head><meta charset="utf-8">
<style>
:root{
  --bg:#F0F0F3; --bg2:#E6E7EC; --card:rgba(255,255,255,.78); --solid:#FFFFFF; --edge:rgba(255,255,255,.9);
  --line:rgba(0,0,0,.07); --sep:rgba(0,0,0,.08);
  --text:#1D1D1F; --sec:rgba(60,60,67,.66); --ter:rgba(60,60,67,.36);
  --accent:#007AFF; --track:rgba(120,120,128,.16); --warn:#C93400; --scrim:rgba(0,0,0,.16);
  --shadow:0 1px 2px rgba(0,0,0,.05),0 6px 18px rgba(0,0,0,.05);
}
@media (prefers-color-scheme:dark){:root{
  --bg:#1C1C1E; --bg2:#141416; --card:rgba(58,58,62,.62); --solid:#2C2C2E; --edge:rgba(255,255,255,.08);
  --line:rgba(255,255,255,.06); --sep:rgba(255,255,255,.08);
  --text:#F5F5F7; --sec:rgba(235,235,245,.64); --ter:rgba(235,235,245,.3);
  --accent:#0A84FF; --track:rgba(120,120,128,.3); --warn:#FF9F0A; --scrim:rgba(0,0,0,.42);
  --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 22px rgba(0,0,0,.25);
}}
@supports (color:-apple-system-control-accent){:root{--accent:-apple-system-control-accent}}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;color:var(--text);
  background:radial-gradient(120% 70% at 50% 0%,var(--bg) 0%,var(--bg2) 100%) fixed;
  font:13px/1.35 -apple-system,BlinkMacSystemFont,"SF Pro Text",sans-serif;
  -webkit-font-smoothing:antialiased;-webkit-user-select:none;cursor:default}
body{display:flex;flex-direction:column;overflow:hidden}
main{flex:1;overflow-y:auto;padding:20px 18px 84px}
.fullsize main{padding-top:46px}
.drag{position:fixed;top:0;left:80px;right:0;height:38px;z-index:5}
.num{font-variant-numeric:tabular-nums}

header{display:flex;align-items:center;justify-content:space-between;margin:0 6px 12px}
.free{font:700 30px/1.1 -apple-system,"SF Pro Display",sans-serif;letter-spacing:-.6px}
.free small{font:500 13px -apple-system;color:var(--sec);letter-spacing:0;margin-left:5px}
.of{color:var(--sec);font-size:12px;margin-top:4px}
.icon{width:30px;height:30px;border:0;border-radius:50%;color:var(--sec);display:grid;place-items:center;
  background:var(--card);box-shadow:var(--shadow),inset 0 0 0 .5px var(--edge);backdrop-filter:blur(20px)}
.icon:hover{color:var(--text)} .icon:active{transform:scale(.94)}
.spin svg{animation:spin .8s linear infinite} @keyframes spin{to{transform:rotate(360deg)}}
.bar{height:6px;border-radius:99px;background:var(--track);margin:0 6px 24px;overflow:hidden}
.bar i{display:block;height:100%;width:0;background:var(--accent);border-radius:99px;transition:width .5s ease}

h2{font-size:11px;font-weight:600;color:var(--sec);margin:0 14px 7px;letter-spacing:.3px}
h2 em{font-style:normal;font-weight:400;opacity:.85;margin-left:3px}
.card{background:var(--card);border-radius:18px;margin-bottom:18px;overflow:hidden;
  box-shadow:var(--shadow),inset 0 0 0 .5px var(--edge);backdrop-filter:blur(30px) saturate(1.8)}
.row{display:flex;align-items:center;gap:12px;padding:9px 14px;min-height:50px;position:relative}
.row+.row::before{content:"";position:absolute;top:0;left:56px;right:14px;height:1px;background:var(--sep);transform:scaleY(.5)}
.tile{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;flex:none;
  background-image:linear-gradient(180deg,rgba(255,255,255,.32),rgba(255,255,255,0) 60%);
  box-shadow:inset 0 0 0 .5px rgba(255,255,255,.25),0 1px 2px rgba(0,0,0,.12)}
.txt{flex:1;min-width:0}
.name{font-weight:500}
.sub{font-size:11px;color:var(--sec);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:1px}
.sub.warn{color:var(--warn)}
.sub a{color:var(--accent);text-decoration:none;cursor:pointer}
.size{color:var(--sec);font-size:12px;text-align:right;min-width:58px}
.size.zero{color:var(--ter)}
.sw{position:relative;width:38px;height:22px;flex:none}
.sw input{position:absolute;opacity:0;inset:0;margin:0}
.sw span{position:absolute;inset:0;border-radius:99px;background:var(--track);transition:background .2s}
.sw span::after{content:"";position:absolute;top:2px;left:2px;width:18px;height:18px;border-radius:50%;
  background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.22),0 0 0 .5px rgba(0,0,0,.04);transition:transform .2s cubic-bezier(.3,1.4,.6,1)}
.sw input:checked+span{background:var(--accent)}
.sw input:checked+span::after{transform:translateX(16px)}
.pill{border:0;border-radius:99px;padding:4px 12px;font:500 12px -apple-system;background:var(--track);color:var(--text);flex:none}
.pill:hover{filter:brightness(.97)} .pill:active{transform:scale(.96)} .pill:disabled{color:var(--ter)}

footer{position:fixed;left:14px;right:14px;bottom:14px;display:flex;align-items:center;justify-content:space-between;
  gap:12px;padding:8px 8px 8px 18px;border-radius:99px;background:var(--card);z-index:4;
  box-shadow:0 2px 6px rgba(0,0,0,.08),0 12px 30px rgba(0,0,0,.12),inset 0 0 0 .5px var(--edge);
  backdrop-filter:blur(30px) saturate(1.8)}
.status{color:var(--sec);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.btn{border:0;border-radius:99px;padding:8px 18px;font:600 13px -apple-system;color:#fff;
  background:var(--accent);background-image:linear-gradient(180deg,rgba(255,255,255,.22),rgba(255,255,255,0));
  box-shadow:inset 0 .5px 0 rgba(255,255,255,.4),0 1px 3px rgba(0,0,0,.18);white-space:nowrap}
.btn:active{transform:scale(.97);filter:brightness(.92)}
.btn:disabled{background:var(--track);color:var(--ter);box-shadow:none;background-image:none}
.btn2{border:0;border-radius:99px;padding:8px 16px;font:500 13px -apple-system;background:var(--track);color:var(--text)}
.btn2:active{transform:scale(.97)}

.scrim{position:fixed;inset:0;background:var(--scrim);display:none;justify-content:center;align-items:flex-start;
  padding-top:44px;z-index:10;backdrop-filter:blur(2px)}
.scrim.show{display:flex;animation:fade .18s}
@keyframes fade{from{opacity:0}}
.sheet{width:min(410px,calc(100% - 28px));max-height:calc(100% - 70px);display:flex;flex-direction:column;
  background:var(--card);border-radius:24px;padding:20px 16px 16px;animation:drop .22s cubic-bezier(.2,.9,.3,1.1);
  box-shadow:0 24px 60px rgba(0,0,0,.28),inset 0 0 0 .5px var(--edge);backdrop-filter:blur(40px) saturate(1.8)}
@keyframes drop{from{transform:translateY(-10px) scale(.98);opacity:0}}
.sheet h3{font:600 15px -apple-system;margin:0 4px 3px}
.sheet p{font-size:12px;color:var(--sec);margin:0 4px 14px}
.sheet .list{overflow-y:auto;background:var(--solid);border-radius:14px;box-shadow:inset 0 0 0 .5px var(--line)}
.sheet .row{min-height:46px;padding:8px 12px}
.sheet .row+.row::before{left:39px;right:12px}
.cb{width:16px;height:16px;flex:none;appearance:none;-webkit-appearance:none;border-radius:5px;
  box-shadow:inset 0 0 0 1px var(--ter);background:var(--solid);display:grid;place-items:center;margin:0}
.cb:checked{background:var(--accent);box-shadow:none}
.cb:checked::after{content:"";width:7px;height:4px;border:solid #fff;border-width:0 0 1.8px 1.8px;transform:rotate(-45deg) translate(1px,-1px)}
.lock{display:inline-block;vertical-align:-1px;margin-right:3px;opacity:.8}
.acts{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}
.empty{padding:24px;text-align:center;color:var(--sec)}
</style></head>
<body>
<div class="drag pywebview-drag-region"></div>
<main>
  <header>
    <div><div class="free num" id="free">&nbsp;</div><div class="of num" id="of">Scanning…</div></div>
    <button class="icon" id="rescan" title="Rescan">
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6"
        stroke-linecap="round" stroke-linejoin="round"><path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9"/><path d="M13.6 2.2v2.9h-2.9"/></svg>
    </button>
  </header>
  <div class="bar"><i id="used"></i></div>
  <h2>CACHES<em>· rebuilt automatically, logins kept</em></h2>
  <div class="card" id="cache"></div>
  <h2>DOWNLOADS AGAIN WHEN NEEDED</h2>
  <div class="card" id="redl"></div>
  <h2>REVIEW<em>· you choose what goes</em></h2>
  <div class="card" id="review"></div>
</main>
<footer>
  <div class="status num" id="status"></div>
  <button class="btn num" id="go" disabled>Clean</button>
</footer>

<div class="scrim" id="scrim"><div class="sheet">
  <h3 id="sh-title"></h3><p id="sh-text"></p>
  <div class="list" id="sh-list"></div>
  <div class="acts"><button class="btn2" id="sh-cancel">Cancel</button><button class="btn num" id="sh-go">Move to Trash</button></div>
</div></div>

<script>
const $ = id => document.getElementById(id);
const api = () => window.pywebview.api;
let S = null, busy = false, hold = 0, lastScan = 0;

const P = {
  apps:['#5856D6','<rect x="2.5" y="2.5" width="4.5" height="4.5" rx="1"/><rect x="9" y="2.5" width="4.5" height="4.5" rx="1"/><rect x="2.5" y="9" width="4.5" height="4.5" rx="1"/><rect x="9" y="9" width="4.5" height="4.5" rx="1"/>'],
  globe:['#007AFF','<circle cx="8" cy="8" r="5.5"/><path d="M2.5 8h11M8 2.5c-2 2-2 9 0 11M8 2.5c2 2 2 9 0 11"/>'],
  box:['#8E8E93','<path d="M2.5 5 8 2.5 13.5 5v6L8 13.5 2.5 11z"/><path d="M2.5 5 8 7.5 13.5 5M8 7.5v6"/>'],
  doc:['#8E8E93','<path d="M4 2.5h5l3 3v8H4z"/><path d="M6 8h4M6 10.5h4"/>'],
  term:['#30B0C7','<path d="M3.5 5 6.5 8l-3 3M8.5 11.5h4"/>'],
  cloud:['#FF9500','<path d="M5 12h6.5a2.5 2.5 0 0 0 .3-5A3.5 3.5 0 0 0 5 6.3 2.9 2.9 0 0 0 5 12z"/><path d="M8 7.5v3.5M6.5 9.5 8 11l1.5-1.5"/>'],
  ghost:['#AF52DE','<path d="M4 13.5V7a4 4 0 0 1 8 0v6.5l-1.5-1-1.25 1L8 12.5l-1.25 1-1.25-1z"/>'],
  dl:['#34C759','<path d="M8 2.5v8M5 7.5l3 3 3-3M3 13.5h10"/>'],
  trash:['#8E8E93','<path d="M3 4.5h10M6.5 4.5V3h3v1.5M4.5 4.5l.6 9h5.8l.6-9"/>'],
};
const tile = k => `<div class="tile" style="background:${P[k][0]}"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="#fff" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">${P[k][1]}</svg></div>`;
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const when = iso => { const d = (Date.now() - new Date(iso)) / 864e5;
  return d < 1 ? "today" : d < 2 ? "yesterday" : Math.floor(d) + " days ago"; };
const selected = () => [...document.querySelectorAll('#cache input,#redl input')].filter(i => i.checked).map(i => i.dataset.id);

function row(r){
  const sub = r.link ? `${esc(r.sub)}${r.sub ? ' · ' : ''}<a data-fda>${esc(r.link)}</a>` : esc(r.sub);
  return `<div class="row">${tile(r.icon)}
    <div class="txt"><div class="name">${esc(r.name)}</div><div class="sub ${r.warn ? 'warn' : ''}">${sub}</div></div>
    <div class="size num ${r.size ? '' : 'zero'}">${r.size_h}</div>
    <label class="sw"><input type="checkbox" data-id="${r.id}" ${r.selected ? 'checked' : ''}><span></span></label></div>`;
}
function rrow(id, icon, name, d, label){
  return `<div class="row">${tile(icon)}
    <div class="txt"><div class="name">${name}</div><div class="sub">${esc(d.sub)}</div></div>
    <div class="size num">${d.size_h}</div>
    <button class="pill" data-review="${id}" ${d.count === 0 || d.sub === 'Empty' ? 'disabled' : ''}>${label}</button></div>`;
}

async function refreshButton(){
  const ids = selected(), b = $('go');
  const bytes = S.rows.filter(r => ids.includes(r.id)).reduce((a, r) => a + r.size, 0);
  b.disabled = busy || bytes === 0;
  b.textContent = bytes ? "Clean " + await api().human(bytes) : "Nothing to clean";
}

function render(){
  $('free').innerHTML = S.free_h.replace(/ (\w+)$/, '<small>$1 available</small>');
  $('of').textContent = "of " + S.total_h + " on Macintosh HD";
  $('used').style.width = (S.used * 100).toFixed(1) + "%";
  $('cache').innerHTML = S.rows.filter(r => r.section === 'cache').map(row).join('');
  $('redl').innerHTML = S.rows.filter(r => r.section === 'redl').map(row).join('');
  const R = S.review;
  $('review').innerHTML =
    rrow('leftovers', 'ghost', 'Leftovers from removed apps', R.leftovers, 'Review') +
    rrow('installers', 'dl', 'Old installers', R.installers, 'Review') +
    rrow('trash', 'trash', 'Trash', R.trash, 'Empty…');
  document.querySelectorAll('#cache input,#redl input').forEach(el => el.onchange = () => {
    S.rows.find(r => r.id === el.dataset.id).selected = el.checked;
    api().set_selected(selected()); refreshButton();
  });
  document.querySelectorAll('[data-review]').forEach(b => b.onclick = () => openReview(b.dataset.review));
  document.querySelectorAll('[data-fda]').forEach(a => a.onclick = () => api().open_fda());
  if (Date.now() > hold) $('status').textContent = S.last ? "Last cleaned " + when(S.last.at) : "";
  refreshButton();
}

async function scan(){
  if (busy) return;
  $('rescan').classList.add('spin');
  if (!S) $('status').textContent = "Scanning…";
  S = await api().scan(); lastScan = Date.now();
  $('rescan').classList.remove('spin');
  render();
}
function flash(msg){ $('status').textContent = msg; hold = Date.now() + 6000; }

$('rescan').onclick = scan;
$('go').onclick = async () => {
  busy = true; $('go').disabled = true; $('go').textContent = "Cleaning…";
  const r = await api().clean(selected());
  busy = false;
  if (!r.cancelled) flash("Cleared " + r.freed_h);
  await scan();
};

/* review sheet */
let sheetKind = null, sheetItems = [];
const LOCK = '<svg class="lock" width="10" height="11" viewBox="0 0 10 11" fill="currentColor"><path d="M2.5 4.5V3.2a2.5 2.5 0 0 1 5 0v1.3h.4c.6 0 1.1.5 1.1 1.1v3.8c0 .6-.5 1.1-1.1 1.1H2.1c-.6 0-1.1-.5-1.1-1.1V5.6c0-.6.5-1.1 1.1-1.1zm1.2 0h2.6V3.2a1.3 1.3 0 0 0-2.6 0z"/></svg>';
const TEXT = {
  leftovers: ["Leftovers from removed apps", "Ticked apps have nothing from their maker left on this Mac. A lock means your password is needed. All of it goes to the Trash."],
  installers: ["Old installers", "Installer files in your Downloads folder. Ticked items move to the Trash."],
};
async function openReview(kind){
  if (kind === 'trash'){
    const r = await api().empty_trash();
    if (!r.cancelled) flash(r.ok ? "Trash emptied" : "Couldn't empty the Trash");
    return scan();
  }
  sheetKind = kind; sheetItems = await api().review_list(kind);
  $('sh-title').textContent = TEXT[kind][0]; $('sh-text').textContent = TEXT[kind][1];
  $('sh-list').innerHTML = sheetItems.length ? sheetItems.map((it, i) => `<label class="row">
      <input type="checkbox" class="cb" data-i="${i}" ${it.checked ? 'checked' : ''}>
      <div class="txt"><div class="name">${it.admin ? LOCK : ''}${esc(it.name)}</div><div class="sub" title="${esc(it.path)}">${esc(it.sub)}</div></div>
      <div class="size num">${it.size_h}</div></label>`).join('') : '<div class="empty">Nothing found</div>';
  $('sh-list').querySelectorAll('input').forEach(c => c.onchange = sheetButton);
  sheetButton(); $('scrim').classList.add('show');
}
async function sheetButton(){
  const picked = [...$('sh-list').querySelectorAll('input:checked')].map(c => sheetItems[+c.dataset.i]);
  const b = $('sh-go'), bytes = picked.reduce((a, i) => a + i.size, 0);
  b.disabled = !picked.length;
  b.textContent = picked.length ? "Move " + await api().human(bytes) + " to Trash" : "Move to Trash";
}
function closeSheet(){ $('scrim').classList.remove('show'); }
$('sh-cancel').onclick = closeSheet;
$('scrim').onclick = e => { if (e.target.id === 'scrim') closeSheet(); };
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeSheet(); });
$('sh-go').onclick = async () => {
  const paths = [...$('sh-list').querySelectorAll('input:checked')].map(c => sheetItems[+c.dataset.i].path);
  $('sh-go').disabled = true; $('sh-go').textContent = "Moving…";
  const r = await api().trash_items(sheetKind, paths);
  closeSheet(); flash("Moved " + r.freed_h + " to Trash" + (r.failed ? ` · ${r.failed} couldn't be moved` : "") + (r.hint ? ` · ${r.hint}` : ""));
  scan();
};

window.addEventListener('pywebviewready', scan);
window.addEventListener('focus', () => S && !busy && Date.now() - lastScan > 60000 && !$('scrim').classList.contains('show') && scan());
document.addEventListener('contextmenu', e => e.preventDefault());
</script>
</body></html>"""

if __name__ == "__main__":
    main()
